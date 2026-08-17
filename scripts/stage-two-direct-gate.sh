#!/usr/bin/env bash
set -euo pipefail

# Minimal Stage 2 control and single-task runner.  A disposable VM contains trial
# containers, but tests and the reference solution are copied in only after the
# agent phase.  No model credential enters the VM.

readonly TASK_ID="fix-code-vulnerability"
readonly TB2_REVISION="2fd12b88aafdd04a52c298e3940bcb189f9766d6"
readonly AGENT_IMAGE="docker.io/alexgshaw/fix-code-vulnerability@sha256:cac325252991f823713b2d0441502972901dd782bd67f66c03d9b1e410dac5c0"
readonly PREPARED_NAME="agentcongress-stage2-noble-20260801-harbor-0.20.0-compose-v2-r1.qcow2"
readonly PREPARED_SHA256="40147a265d6b5d7ea4d5785dbf7513a60c395ef7417611a20f81ae11248ac07d"

root="${1:-/tmp/agentcongress-stage-two-vm}"
source_repo="${2:-/tmp/agentcongress-tb2-2fd12b88}"
arm="${3:-}"
ssh_port="${STAGE2_VM_SSH_PORT:-50222}"
code_mode_port="${STAGE2_CODE_MODE_PORT:-50333}"
prepared="$root/prepared/$PREPARED_NAME"

(( $# <= 3 )) || { echo "usage: $0 [root [source-repo [arm]]]" >&2; exit 2; }
case "$arm" in
  ""|standalone-luna|standalone-sol|luna-congress|luna-sol-congress) ;;
  *) echo "invalid arm: $arm" >&2; exit 2 ;;
esac
case "$root:$source_repo" in /*:/*) ;; *) echo "paths must be absolute" >&2; exit 2 ;; esac
[[ "$ssh_port" =~ ^[0-9]+$ ]] && (( ssh_port > 0 && ssh_port < 65536 ))
[[ "$code_mode_port" =~ ^[0-9]+$ ]] && (( code_mode_port > 0 && code_mode_port < 65536 ))
for command in cloud-localds flock git qemu-img qemu-system-x86_64 sha256sum ssh ssh-keygen; do
  command -v "$command" >/dev/null || { echo "missing command: $command" >&2; exit 2; }
done
[[ -f "$prepared" && ! -L "$prepared" ]]
printf '%s  %s\n' "$PREPARED_SHA256" "$prepared" | sha256sum -c - >/dev/null
GIT_NO_REPLACE_OBJECTS=1 git -C "$source_repo" cat-file -e "$TB2_REVISION^{commit}"

mkdir -p "$root/runs/direct"
exec 9>"$root/direct-gate.lock"
flock 9
run_dir="$(mktemp -d "$root/runs/direct/direct-gate.XXXXXXXX")"
chmod 0700 "$run_dir"
overlay="$run_dir/overlay.qcow2"
seed="$run_dir/seed.img"
key="$run_dir/id_ed25519"
known_hosts="$run_dir/known_hosts"
pidfile="$run_dir/qemu.pid"
console="$run_dir/qemu-console.log"
task_archive="$run_dir/task.tar"
result="$run_dir/result.json"
instruction="$run_dir/instruction.md"
codex_home="$run_dir/codex-home"
qemu_pid=""
tunnel_pid=""

cleanup() {
  if [[ "$tunnel_pid" =~ ^[0-9]+$ ]] && kill -0 "$tunnel_pid" 2>/dev/null; then
    kill "$tunnel_pid" 2>/dev/null || true
    wait "$tunnel_pid" 2>/dev/null || true
  fi
  if [[ -z "$qemu_pid" && -f "$pidfile" ]]; then qemu_pid="$(cat "$pidfile" 2>/dev/null || true)"; fi
  if [[ "$qemu_pid" =~ ^[0-9]+$ && -r "/proc/$qemu_pid/cmdline" ]] && \
      tr '\0' '\n' <"/proc/$qemu_pid/cmdline" | grep -Fqx -- "file=$overlay,if=virtio,format=qcow2" && \
      kill -0 "$qemu_pid" 2>/dev/null; then
    kill "$qemu_pid" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$qemu_pid" 2>/dev/null || break; sleep 0.25; done
    kill -9 "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true
  fi
  rm -f -- "$codex_home/auth.json" 2>/dev/null || true
  rmdir -- "$codex_home" 2>/dev/null || true
  rm -f -- "$overlay" "$seed" "$key" "$key.pub" "$known_hosts" "$pidfile" "$task_archive"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

GIT_NO_REPLACE_OBJECTS=1 git -c tar.umask=0022 -C "$source_repo" archive \
  --format=tar --prefix=task/ "$TB2_REVISION" "$TASK_ID" >"$task_archive"
if [[ -n "$arm" ]]; then
  GIT_NO_REPLACE_OBJECTS=1 git -C "$source_repo" show \
    "$TB2_REVISION:$TASK_ID/instruction.md" >"$instruction"
  chmod 0600 "$instruction"
fi
ssh-keygen -q -t ed25519 -N '' -f "$key"
public_key="$(cat "$key.pub")"
cat >"$run_dir/user-data" <<EOF
#cloud-config
users:
  - name: stage2
    groups: [sudo, docker]
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $public_key
ssh_pwauth: false
runcmd:
  - [systemctl, enable, --now, docker]
EOF
cat >"$run_dir/meta-data" <<EOF
instance-id: $(basename "$run_dir")
local-hostname: agentcongress-direct-gate
EOF
cloud-localds "$seed" "$run_dir/user-data" "$run_dir/meta-data"
qemu-img create -q -f qcow2 -F qcow2 -b "$prepared" "$overlay"
qemu-img resize -q "$overlay" 40G
qemu-system-x86_64 -machine accel=tcg -cpu max -smp 8 -m 8192 \
  -drive "file=$overlay,if=virtio,format=qcow2" \
  -drive "file=$seed,if=virtio,format=raw,readonly=on" \
  -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${ssh_port}-:22" \
  -device virtio-net-pci,netdev=net0 -display none -serial "file:$console" \
  -monitor none -daemonize -pidfile "$pidfile"
qemu_pid="$(cat "$pidfile")"

for _ in $(seq 1 180); do
  if ssh -p "$ssh_port" -i "$key" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
      -o "UserKnownHostsFile=$known_hosts" -o ConnectTimeout=3 stage2@127.0.0.1 true 2>/dev/null; then
    break
  fi
  sleep 5
done
ssh_args=(-p "$ssh_port" -i "$key" -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$known_hosts" -o ConnectTimeout=5 stage2@127.0.0.1)
ssh "${ssh_args[@]}" 'sudo cloud-init status --wait >/dev/null'
ssh "${ssh_args[@]}" 'install -d -m 0700 /var/tmp/agentcongress-direct-gate'
ssh "${ssh_args[@]}" 'cat >/var/tmp/agentcongress-direct-gate/task.tar' <"$task_archive"

if [[ -n "$arm" ]]; then
  readonly runner_python="/root/AgentCongress/.venv/bin/python"
  readonly codex_executable="/usr/local/lib/node_modules/@openai/codex/bin/codex.js"
  readonly auth_source="/root/.codex/auth.json"
  readonly code_mode_helper="/usr/local/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex-code-mode-host"
  readonly code_mode_helper_sha256="00ecf5d040865b97884c488883abd342581c2a432debe7a54e4646bceee3d2d6"
  [[ -x "$runner_python" && -f "$codex_executable" && ! -L "$codex_executable" ]]
  [[ -f "$auth_source" && ! -L "$auth_source" ]]
  [[ -x "$code_mode_helper" && ! -L "$code_mode_helper" ]]
  printf '%s  %s\n' "$code_mode_helper_sha256" "$code_mode_helper" | sha256sum -c - >/dev/null
  install -d -m 0700 "$codex_home"
  install -m 0600 "$auth_source" "$codex_home/auth.json"
  cat >"$codex_home/config.toml" <<'EOF'
[features]
apps = false
browser_use = false
browser_use_external = false
browser_use_full_cdp_access = false
code_mode_host = true
computer_use = false
goals = false
image_generation = false
in_app_browser = false
multi_agent = false
plugins = false
remote_plugin = false
shell_tool = false
unified_exec = false
view_image = false
EOF
  chmod 0600 "$codex_home/config.toml"
  CODEX_HOME="$codex_home" "$codex_executable" features list |
    awk '$1 == "apps" { found=1; if ($3 != "false") exit 1 } END { if (!found) exit 1 }'
  chmod 0600 "$key" "$known_hosts"

  run_token="$(basename "$run_dir" | tr -cd 'A-Za-z0-9_.-')"
  container="agentcongress-${arm}-${run_token}"
  volume="${container}-app"
  network="${container}-net"
  score="$run_dir/score.json"

  ssh "${ssh_args[@]}" 'cat >/var/tmp/agentcongress-direct-gate/codex-code-mode-host && chmod 0555 /var/tmp/agentcongress-direct-gate/codex-code-mode-host' <"$code_mode_helper"
  ssh "${ssh_args[@]}" "printf '%s  %s\n' '$code_mode_helper_sha256' /var/tmp/agentcongress-direct-gate/codex-code-mode-host | sha256sum -c - >/dev/null"

  container_ip="$(ssh "${ssh_args[@]}" bash -s -- "$AGENT_IMAGE" "$container" "$volume" "$network" <<'GUEST'
set -euo pipefail
readonly image="$1"
readonly container="$2"
readonly volume="$3"
readonly network="$4"
root=/var/tmp/agentcongress-direct-gate
tar -xf "$root/task.tar" -C "$root"
docker pull "$image" >/dev/null
docker volume create "$volume" >/dev/null
docker network create --internal "$network" >/dev/null
docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges:true \
  -v "$volume:/dst" "$image" sh -c 'cp -a /app/. /dst/'
docker create --name "$container" --network "$network" --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --pids-limit 256 \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m -v "$volume:/app" \
  -v "$root/codex-code-mode-host:/usr/local/bin/codex-code-mode-host:ro" \
  "$image" /usr/local/bin/codex-code-mode-host --listen ws://0.0.0.0:8765 >/dev/null
docker start "$container" >/dev/null
docker exec "$container" sh -c 'test ! -e /tests && test ! -e /solution'
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$container"
GUEST
  )"
  [[ "$container_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]
  ssh "${ssh_args[@]}" -o ExitOnForwardFailure=yes -N \
    -L "127.0.0.1:${code_mode_port}:${container_ip}:8765" &
  tunnel_pid=$!
  sleep 1
  kill -0 "$tunnel_pid"

  PYTHONPATH=/root/AgentCongress/src "$runner_python" -m agentcongress.stage_two_direct_runner run \
    --arm "$arm" \
    --instruction "$instruction" \
    --run-dir "$run_dir" \
    --ssh-port "$ssh_port" \
    --ssh-key "$key" \
    --known-hosts "$known_hosts" \
    --container "$container" \
    --codex-executable "$codex_executable" \
    --codex-home "$codex_home" \
    --code-mode-host-url "ws://127.0.0.1:${code_mode_port}"

  agent_status="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["agent_status"])' "$run_dir/agent-result.json")"
  if [[ "$agent_status" != "agent_completed" ]]; then
    ssh "${ssh_args[@]}" bash -s -- "$container" "$volume" "$network" <<'GUEST'
set -euo pipefail
docker rm -f "$1" >/dev/null 2>&1 || true
docker volume rm "$2" >/dev/null 2>&1 || true
docker network rm "$3" >/dev/null 2>&1 || true
GUEST
    printf '{"reward":0}\n' >"$score"
    PYTHONPATH=/root/AgentCongress/src "$runner_python" -m agentcongress.stage_two_direct_runner finalize \
      --run-dir "$run_dir" --score "$score"
    cat "$score"
    exit 0
  fi

  ssh "${ssh_args[@]}" bash -s -- "$AGENT_IMAGE" "$container" "$volume" "$network" "$run_token" >"$score" <<'GUEST'
set -euo pipefail
readonly image="$1"
readonly container="$2"
readonly volume="$3"
readonly network="$4"
readonly run_token="$5"
root=/var/tmp/agentcongress-direct-gate
logs="$root/logs-$run_token"
cleanup_trial() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup_trial EXIT
install -d -m 0777 "$logs"
docker stop -t 3 "$container" >/dev/null
cat >"$root/Dockerfile.verifier" <<EOF
FROM $image
USER root
RUN python -m pip install --no-cache-dir pytest==8.4.1 pytest-json-ctrf==0.3.5
COPY task/fix-code-vulnerability/tests/ /tests/
RUN chmod 0555 /tests/test.sh && mkdir -p /logs/verifier /app
ENV PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
EOF
docker build -q -f "$root/Dockerfile.verifier" -t agentcongress-direct-verifier "$root" >/dev/null
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --pids-limit 256 \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m \
  -v "$volume:/app" -v "$logs:/logs/verifier" agentcongress-direct-verifier \
  bash /tests/test.sh >/dev/null
reward="$(tr -d '\r\n ' <"$logs/reward.txt")"
[[ "$reward" == 0 || "$reward" == 1 ]]
printf '{"reward":%s}\n' "$reward"
GUEST

  python3 - "$score" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert value in ({"reward": 0}, {"reward": 1})
PY
  PYTHONPATH=/root/AgentCongress/src "$runner_python" -m agentcongress.stage_two_direct_runner finalize \
    --run-dir "$run_dir" --score "$score"
  cat "$score"
  exit 0
fi

ssh "${ssh_args[@]}" bash -s -- "$AGENT_IMAGE" "$result" <<'GUEST'
set -euo pipefail
shopt -s inherit_errexit
readonly image="$1"
readonly host_result="$2"
root=/var/tmp/agentcongress-direct-gate
tar -xf "$root/task.tar" -C "$root"
task="$root/task/fix-code-vulnerability"
docker pull "$image" >/dev/null
cat >"$root/Dockerfile.verifier" <<EOF
FROM $image
USER root
RUN python -m pip install --no-cache-dir pytest==8.4.1 pytest-json-ctrf==0.3.5
COPY task/fix-code-vulnerability/tests/ /tests/
RUN chmod 0555 /tests/test.sh && mkdir -p /logs/verifier /app
ENV PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
EOF
docker build -q -f "$root/Dockerfile.verifier" -t agentcongress-direct-verifier "$root" >/dev/null

run_control() {
  local kind="$1" base="agentcongress-direct-$1-$$" reward
  local volume="$base-app" agent="$base-agent" logs="$root/logs-$kind"
  install -d -m 0777 "$logs"
  docker volume create "$volume" >/dev/null
  docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges:true \
    -v "$volume:/dst" "$image" sh -c 'cp -a /app/. /dst/'
  docker create --name "$agent" --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 256 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m -v "$volume:/app" "$image" tail -f /dev/null >/dev/null
  docker start "$agent" >/dev/null
  docker exec "$agent" sh -c 'test ! -e /tests && test ! -e /solution'
  if [[ "$kind" == oracle ]]; then
    docker exec -i -w /app "$agent" bash -s <"$task/solution/solve.sh" >/dev/null
  fi
  docker stop -t 3 "$agent" >/dev/null
  docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 256 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m \
    -v "$volume:/app" -v "$logs:/logs/verifier" agentcongress-direct-verifier \
    bash /tests/test.sh >/dev/null
  reward="$(tr -d '\r\n ' <"$logs/reward.txt")"
  [[ "$reward" == 0 || "$reward" == 1 ]]
  docker rm "$agent" >/dev/null
  docker volume rm "$volume" >/dev/null
  printf '%s' "$reward"
}

oracle="$(run_control oracle)"
nop="$(run_control nop)"
printf '{"oracle":%s,"nop":%s,"status":"%s"}\n' "$oracle" "$nop" \
  "$([[ "$oracle" == 1 && "$nop" == 0 ]] && echo passed || echo failed)" >"$root/result.json"
cat "$root/result.json"
GUEST

ssh "${ssh_args[@]}" 'cat /var/tmp/agentcongress-direct-gate/result.json' >"$result"
python3 - "$result" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert value == {"oracle": 1, "nop": 0, "status": "passed"}
PY
cat "$result"
