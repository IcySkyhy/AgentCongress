#!/usr/bin/env bash
set -euo pipefail

# Zero-model Stage 2 infrastructure probe for hosts that cannot run Docker
# safely themselves.  QEMU is the host boundary; Docker runs only in a fresh
# guest.  This script intentionally carries no model credential or benchmark
# solution/test payload.

readonly BASE_URL="https://cloud-images.ubuntu.com/noble/20260801/noble-server-cloudimg-amd64.img"
readonly BASE_SHA256="0533b0655c32e68b31d792ecd6ccfca95abdbc536c4446874fe0513bd4140ffe"
readonly BUSYBOX_REF="docker.io/library/busybox@sha256:b7f3d86d6e84fc17718c48bcde1450807faa2d56704205c697b4bd5df7b9e29f"

root="${1:-/root/AgentCongress/.agentcongress/stage-two/vm}"
ssh_port="${STAGE2_VM_SSH_PORT:-50222}"
case "$root" in
  /*) ;;
  *) echo "stage-two VM root must be absolute" >&2; exit 2 ;;
esac
if [[ "$root" == "/" || ! "$ssh_port" =~ ^[0-9]+$ ]]; then
  echo "unsafe VM root or invalid SSH port" >&2
  exit 2
fi

for command in curl sha256sum qemu-img qemu-system-x86_64 cloud-localds ssh ssh-keygen tar; do
  command -v "$command" >/dev/null || { echo "missing command: $command" >&2; exit 2; }
done

cache="$root/cache"
runs="$root/runs"
evidence_root="$root/evidence"
mkdir -p "$cache" "$runs" "$evidence_root"
run_dir="$(mktemp -d "$runs/run.XXXXXXXX")"
run_id="$(basename "$run_dir")"
evidence="$evidence_root/$run_id"
mkdir -p "$evidence"

base="$cache/noble-server-cloudimg-amd64-20260801.img"
overlay="$run_dir/overlay.qcow2"
seed="$run_dir/seed.img"
key="$run_dir/id_ed25519"
pidfile="$run_dir/qemu.pid"
console="$evidence/qemu-console.log"
known_hosts="$run_dir/known_hosts"
host_canary="/tmp/agentcongress-host-canary-${run_id}"

qemu_pid=""
stop_qemu() {
  if [[ -f "$pidfile" ]]; then
    qemu_pid="$(cat "$pidfile" 2>/dev/null || true)"
  fi
  if [[ "$qemu_pid" =~ ^[0-9]+$ ]] && kill -0 "$qemu_pid" 2>/dev/null; then
    kill "$qemu_pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$qemu_pid" 2>/dev/null || break
      sleep 0.25
    done
    kill -9 "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true
  fi
  qemu_pid=""
  rm -f -- "$pidfile"
}
cleanup() {
  stop_qemu
  rm -f -- "$overlay" "$seed" "$key" "$key.pub" "$pidfile" "$known_hosts" \
    "$run_dir/user-data" "$run_dir/meta-data" "$host_canary"
  rmdir -- "$run_dir" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [[ ! -f "$base" ]] || ! printf '%s  %s\n' "$BASE_SHA256" "$base" | sha256sum -c - >/dev/null 2>&1; then
  partial="$base.partial"
  rm -f -- "$partial"
  curl --fail --location --retry 3 --output "$partial" "$BASE_URL"
  printf '%s  %s\n' "$BASE_SHA256" "$partial" | sha256sum -c -
  mv -- "$partial" "$base"
fi
printf '%s  %s\n' "$BASE_SHA256" "$base" | sha256sum -c -

ssh-keygen -q -t ed25519 -N '' -f "$key"
public_key="$(cat "$key.pub")"
head -c 64 /dev/urandom >"$host_canary"
chmod 600 "$host_canary"

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
package_update: true
packages: [docker.io]
runcmd:
  - [systemctl, enable, --now, docker]
EOF
cat >"$run_dir/meta-data" <<EOF
instance-id: agentcongress-$run_id
local-hostname: agentcongress-stage2
EOF

cloud-localds "$seed" "$run_dir/user-data" "$run_dir/meta-data"
qemu-img create -q -f qcow2 -F qcow2 -b "$base" "$overlay"
qemu-img resize -q "$overlay" 40G

qemu-system-x86_64 \
  -machine accel=tcg -cpu max -smp 8 -m 8192 \
  -drive "file=$overlay,if=virtio,format=qcow2" \
  -drive "file=$seed,if=virtio,format=raw,readonly=on" \
  -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${ssh_port}-:22" \
  -device virtio-net-pci,netdev=net0 \
  -display none -serial "file:$console" -monitor none \
  -daemonize -pidfile "$pidfile"

ssh_args=(
  -p "$ssh_port" -i "$key" -o BatchMode=yes
  -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$known_hosts"
  -o ConnectTimeout=5 stage2@127.0.0.1
)
for _ in $(seq 1 180); do
  if ssh -p "$ssh_port" -i "$key" -o BatchMode=yes \
      -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=$known_hosts" \
      -o ConnectTimeout=3 stage2@127.0.0.1 true 2>/dev/null; then
    break
  fi
  sleep 5
done
ssh "${ssh_args[@]}" true
ssh "${ssh_args[@]}" 'sudo cloud-init status --wait --long' >"$evidence/cloud-init.txt"

ssh "${ssh_args[@]}" bash -s -- "$BUSYBOX_REF" "$host_canary" "$run_id" <<'GUEST'
set -euo pipefail
image="$1"
host_canary="$2"
run_id="$3"
out="/tmp/agentcongress-stage2-smoke-$run_id"
agent="ac-agent-$run_id"
verifier="ac-verifier-$run_id"
submission="ac-submission-$run_id"
tests="ac-tests-$run_id"
logs="ac-logs-$run_id"
mkdir -p "$out"

cleanup_guest() {
  sudo docker rm -f "$agent" "$verifier" >/dev/null 2>&1 || true
  sudo docker volume rm "$submission" "$tests" "$logs" >/dev/null 2>&1 || true
}
trap cleanup_guest EXIT INT TERM

sudo docker version --format '{{json .}}' >"$out/docker-version.json"
sudo docker pull "$image" >"$out/image-pull.txt"
sudo docker image inspect "$image" >"$out/image-inspect.json"
sudo docker volume create "$submission" >/dev/null

agent_script='set -eu
test ! -e /tests
test ! -e /solution
test ! -e /root/.codex/auth.json
test ! -e /var/run/docker.sock
test ! -e /run/docker.sock
test ! -e "$HOST_CANARY"
! env | grep -Eq "^(OPENAI_API_KEY|CODEX_HOME|AWS_|GITHUB_TOKEN|SSH_AUTH_SOCK)="
test "$(awk "/^CapEff:/ {print \$2}" /proc/self/status)" = "0000000000000000"
test "$(awk "/^NoNewPrivs:/ {print \$2}" /proc/self/status)" = "1"
! wget -q -T 2 -O /dev/null http://1.1.1.1
! nslookup example.com
printf "agent-submission\n" >/work/result.txt'

sudo docker create --name "$agent" --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --pids-limit 64 --memory 128m --cpus 1 \
  --env "HOST_CANARY=$host_canary" \
  --mount "type=volume,src=$submission,dst=/work" \
  --workdir /work "$image" sh -c "$agent_script" >/dev/null
sudo docker start --attach "$agent" >"$out/agent.stdout" 2>"$out/agent.stderr"
sudo docker inspect "$agent" >"$out/agent-inspect.json"

# Trusted control plane creates verifier-only inputs only after the agent exits.
sudo docker volume create "$tests" >/dev/null
sudo docker volume create "$logs" >/dev/null
sudo docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --mount "type=volume,src=$tests,dst=/tests" \
  "$image" sh -c 'printf "verifier-only\n" >/tests/marker.txt'

verifier_script='set -eu
test "$(cat /tests/marker.txt)" = "verifier-only"
test "$(cat /submission/result.txt)" = "agent-submission"
test ! -e /var/run/docker.sock
! wget -q -T 2 -O /dev/null http://1.1.1.1
printf "1\n" >/logs/reward.txt'
sudo docker create --name "$verifier" --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --pids-limit 64 --memory 128m --cpus 1 \
  --mount "type=volume,src=$submission,dst=/submission,readonly" \
  --mount "type=volume,src=$tests,dst=/tests,readonly" \
  --mount "type=volume,src=$logs,dst=/logs" \
  "$image" sh -c "$verifier_script" >/dev/null
sudo docker start --attach "$verifier" >"$out/verifier.stdout" 2>"$out/verifier.stderr"
sudo docker inspect "$verifier" >"$out/verifier-inspect.json"
sudo docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --mount "type=volume,src=$logs,dst=/logs,readonly" \
  "$image" cat /logs/reward.txt >"$out/reward.txt"
test "$(cat "$out/reward.txt")" = "1"
printf '{"ready":true,"agent_network":"none","verifier_network":"none","credential_in_guest":false}\n' >"$out/result.json"
sudo chown -R stage2:stage2 "$out"
GUEST

guest_out="/tmp/agentcongress-stage2-smoke-$run_id"
ssh "${ssh_args[@]}" "tar -C '$guest_out' -cf - ." | tar -C "$evidence" -xf -
# Freeze the console before hashing evidence.  Otherwise QEMU may append a
# final serial line after SHA256SUMS is written, creating a non-replayable run.
stop_qemu
qemu-system-x86_64 --version | head -n 1 >"$evidence/qemu-version.txt"
printf '%s  %s\n' "$BASE_SHA256" "$BASE_URL" >"$evidence/base-image.sha256"
printf '%s\n' "$BUSYBOX_REF" >"$evidence/smoke-image.txt"
(
  cd "$evidence"
  find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS
)
echo "Stage 2 VM smoke passed; evidence=$evidence"
