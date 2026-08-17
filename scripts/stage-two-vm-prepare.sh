#!/usr/bin/env bash
set -euo pipefail

# Build a reusable, credential-free Stage 2 control-plane guest.  The result
# remains a qcow2 overlay backed by the pinned Ubuntu cloud image; benchmark
# tests, solutions, and model credentials are deliberately out of scope.

readonly BASE_URL="https://cloud-images.ubuntu.com/noble/20260801/noble-server-cloudimg-amd64.img"
readonly BASE_SHA256="0533b0655c32e68b31d792ecd6ccfca95abdbc536c4446874fe0513bd4140ffe"
readonly HARBOR_VERSION="0.20.0"
readonly HARBOR_WHEEL="harbor-0.20.0-py3-none-any.whl"
readonly HARBOR_WHEEL_SHA256="4b7e48223aea2384cdb8c9eff35eaebd482fc9b1ec09f8193a121c47356ff19a"
readonly PREPARED_PROFILE="compose-v2-r1"
readonly COMPOSE_PACKAGE="docker-compose-v2"
readonly COMPOSE_PACKAGE_VERSION="2.40.3+ds1-0ubuntu1~24.04.1"
readonly COMPOSE_CLI_VERSION="2.40.3+ds1-0ubuntu1~24.04.1"
readonly COMPOSE_PLUGIN_PATH="/usr/libexec/docker/cli-plugins/docker-compose"
readonly COMPOSE_PLUGIN_SHA256="d87a11e944c990dc9f2186115b1136c1cbffffc870845caff0cbdcce0780f41d"

root="${1:-/root/AgentCongress/.agentcongress/stage-two/vm}"
ssh_port="${STAGE2_VM_SSH_PORT:-50222}"
case "$root" in
  /*) ;;
  *) echo "stage-two VM root must be absolute" >&2; exit 2 ;;
esac
if [[ "$root" == "/" || ! "$ssh_port" =~ ^[0-9]+$ ]] ||
    (( ssh_port < 1 || ssh_port > 65535 )); then
  echo "unsafe VM root or invalid SSH port" >&2
  exit 2
fi

for command in curl sha256sum qemu-img qemu-system-x86_64 cloud-localds \
    ssh ssh-keygen tar python3 flock; do
  command -v "$command" >/dev/null || {
    echo "missing command: $command" >&2
    exit 2
  }
done

cache="$root/cache"
runs="$root/runs"
prepared_dir="$root/prepared"
evidence_root="$root/evidence/prepared/$PREPARED_PROFILE"
mkdir -p "$cache" "$runs" "$prepared_dir" "$evidence_root"
exec 9>"$root/prepare.lock"
flock -x 9

base="$cache/noble-server-cloudimg-amd64-20260801.img"
prepared_stem="$prepared_dir/agentcongress-stage2-noble-20260801-harbor-0.20.0-compose-v2-r1"
prepared="$prepared_stem.qcow2"
manifest="$prepared_stem.manifest.json"
prepared_hash_file="$prepared.sha256"
base_hash_file="$prepared_stem.backing-base.sha256"

valid_prepared_image() {
  [[ -f "$base" && -f "$prepared" && -f "$manifest" &&
     -f "$prepared_hash_file" && -f "$base_hash_file" &&
     ! -L "$base" && ! -L "$prepared" && ! -L "$manifest" &&
     ! -L "$prepared_hash_file" && ! -L "$base_hash_file" ]] || return 1
  printf '%s  %s\n' "$BASE_SHA256" "$base" | sha256sum -c - >/dev/null 2>&1 || return 1

  python3 - "$manifest" "$base" "$prepared" "$prepared_hash_file" \
      "$base_hash_file" "$evidence_root" "$BASE_URL" "$BASE_SHA256" \
      "$HARBOR_VERSION" "$HARBOR_WHEEL_SHA256" "$PREPARED_PROFILE" \
      "$COMPOSE_PACKAGE" "$COMPOSE_PACKAGE_VERSION" "$COMPOSE_CLI_VERSION" \
      "$COMPOSE_PLUGIN_PATH" "$COMPOSE_PLUGIN_SHA256" <<'PY' || return 1
import hashlib
import json
import re
import sys
from pathlib import Path

(
    manifest_path,
    base_path,
    prepared_path,
    prepared_hash_path,
    base_hash_path,
    evidence_root,
    base_url,
    base_sha,
    harbor_version,
    harbor_wheel_sha,
    profile,
    compose_package,
    compose_package_version,
    compose_cli_version,
    compose_plugin_path,
    compose_plugin_sha,
) = (*map(Path, sys.argv[1:7]), *sys.argv[7:])

def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

try:
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(document) == {
        "base", "built_at", "docker", "evidence", "harbor", "kind",
        "prepared", "profile", "schema_version",
    }
    assert document["schema_version"] == 2
    assert document["kind"] == "agentcongress.stage-two.prepared-guest"
    assert document["profile"] == profile
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", document["built_at"])
    assert document["base"] == {
        "url": base_url, "path": str(base_path), "sha256": base_sha,
    }
    assert set(document["prepared"]) == {"format", "path", "sha256"}
    assert document["prepared"]["path"] == str(prepared_path)
    assert document["prepared"]["format"] == "qcow2"
    assert document["harbor"] == {
        "version": harbor_version, "wheel_sha256": harbor_wheel_sha,
    }
    assert document["docker"] == {"compose": {
        "package": compose_package,
        "package_version": compose_package_version,
        "cli_version": compose_cli_version,
        "plugin_path": compose_plugin_path,
        "plugin_sha256": compose_plugin_sha,
    }}
    prepared_sha = digest(prepared_path)
    assert document["prepared"]["sha256"] == prepared_sha
    assert prepared_hash_path.read_text(encoding="utf-8") == (
        f"{prepared_sha}  {prepared_path.name}\n"
    )
    assert base_hash_path.read_text(encoding="utf-8") == (
        f"{base_sha}  {base_path}\n"
    )

    assert set(document["evidence"]) == {"path", "sha256s_sha256"}
    assert re.fullmatch(r"[0-9a-f]{64}", document["evidence"]["sha256s_sha256"])
    root = evidence_root.resolve(strict=True)
    evidence_source = Path(document["evidence"]["path"])
    assert evidence_source.is_absolute() and not evidence_source.is_symlink()
    evidence = evidence_source.resolve(strict=True)
    assert evidence.parent == root
    assert re.fullmatch(r"prepare\.[A-Za-z0-9]{8}", evidence.name)
    assert evidence.is_dir() and not evidence.is_symlink()
    sums = evidence / "SHA256SUMS"
    assert digest(sums) == document["evidence"]["sha256s_sha256"]
    expected = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        assert match and match.group(2) != "SHA256SUMS"
        assert match.group(2) not in expected
        expected[match.group(2)] = match.group(1)
    entries = list(evidence.iterdir())
    assert all(not item.is_symlink() and item.is_file() for item in entries)
    actual = {item.name for item in entries if item.name != "SHA256SUMS"}
    required = {
        "artifact-hashes.txt",
        "cleanup-attestation.txt",
        "cloud-init-output.log",
        "cloud-init-status.txt",
        "cloud-init.log",
        "docker-journal.log",
        "docker-compose-plugin.json",
        "docker-compose-smoke.txt",
        "docker-compose-version.txt",
        "docker-version.json",
        "harbor-help.txt",
        "harbor-version.txt",
        "os-release.txt",
        "package-versions.txt",
        "pip-download.log",
        "pip-freeze.txt",
        "pip-install.log",
        "primary-wheel.sha256",
        "qemu-console.log",
        "qemu-image-info.json",
        "qemu-version.txt",
        "shutdown.log",
        "wheel-cache.sha256",
    }
    assert expected and required == actual == set(expected)
    for name, expected_sha in expected.items():
        item = evidence / name
        assert not item.is_symlink() and digest(item) == expected_sha
    compose = json.loads((evidence / "docker-compose-plugin.json").read_text(encoding="utf-8"))
    assert compose == {
        "cli_version": compose_cli_version,
        "mode": "0755",
        "owner": "root:root",
        "package": compose_package,
        "package_version": compose_package_version,
        "path": compose_plugin_path,
        "realpath": compose_plugin_path,
        "sha256": compose_plugin_sha,
    }
    assert (evidence / "docker-compose-version.txt").read_text(encoding="utf-8") == compose_cli_version + "\n"
    assert (evidence / "docker-compose-smoke.txt").read_text(encoding="utf-8") == "compose_config_smoke=passed\n"
    package_lines = (evidence / "package-versions.txt").read_text(encoding="utf-8").splitlines()
    packages = dict(line.split("\t", 1) for line in package_lines)
    assert len(package_lines) == len(packages)
    assert set(packages) == {
        "curl", "docker-compose-v2", "docker.io", "git", "python3.12",
        "python3.12-venv",
    }
    assert packages[compose_package] == compose_package_version
    artifact_hashes = dict(
        line.split("=", 1)
        for line in (evidence / "artifact-hashes.txt").read_text(encoding="utf-8").splitlines()
    )
    assert artifact_hashes == {
        "backing_base_sha256": base_sha,
        "prepared_qcow2_sha256": prepared_sha,
    }
    assert (evidence / "harbor-version.txt").read_text(encoding="utf-8") == harbor_version + "\n"
    assert (evidence / "primary-wheel.sha256").read_text(encoding="utf-8") == (
        "/opt/agentcongress-harbor-wheel-cache/harbor-0.20.0-py3-none-any.whl: OK\n"
    )
    assert f"{harbor_wheel_sha}  ./harbor-0.20.0-py3-none-any.whl" in (
        evidence / "wheel-cache.sha256"
    ).read_text(encoding="utf-8").splitlines()
except (AssertionError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
PY

  qemu-img info --output=json "$prepared" | python3 -c '
import json, os, sys
data = json.load(sys.stdin)
expected = os.path.realpath(sys.argv[1])
backing = data.get("full-backing-filename") or data.get("backing-filename")
raise SystemExit(0 if data.get("format") == "qcow2" and backing and os.path.realpath(backing) == expected else 1)
' "$base" || return 1
}

partial="$base.partial"
if [[ ! -f "$base" ]] ||
    ! printf '%s  %s\n' "$BASE_SHA256" "$base" | sha256sum -c - >/dev/null 2>&1; then
  rm -f -- "$partial"
  curl --fail --location --retry 3 --output "$partial" "$BASE_URL"
  printf '%s  %s\n' "$BASE_SHA256" "$partial" | sha256sum -c -
  mv -f -- "$partial" "$base"
fi
printf '%s  %s\n' "$BASE_SHA256" "$base" | sha256sum -c -
chmod 0444 "$base"

if valid_prepared_image; then
  echo "Stage 2 prepared guest already valid; image=$prepared"
  exit 0
fi

# A new profile is append-only.  A partial or invalid published bundle is an
# audit failure and must be inspected, never repaired in place.
for published in "$prepared" "$manifest" "$prepared_hash_file" "$base_hash_file"; do
  if [[ -e "$published" || -L "$published" ]]; then
    echo "refusing to overwrite invalid prepared profile member: $published" >&2
    exit 1
  fi
done

run_dir="$(mktemp -d "$runs/prepare.XXXXXXXX")"
run_id="$(basename "$run_dir")"
overlay="$run_dir/prepared.qcow2"
seed="$run_dir/seed.img"
key="$run_dir/id_ed25519"
pidfile="$run_dir/qemu.pid"
known_hosts="$run_dir/known_hosts"
user_data="$run_dir/user-data"
meta_data="$run_dir/meta-data"
manifest_tmp="$run_dir/manifest.json"
evidence_tmp="$run_dir/evidence"
mkdir "$evidence_tmp"
console="$evidence_tmp/qemu-console.log"
qemu_pid=""

evidence_files=(
  SHA256SUMS artifact-hashes.txt cleanup-attestation.txt cloud-init-output.log
  cloud-init-status.txt cloud-init.log docker-compose-plugin.json
  docker-compose-smoke.txt docker-compose-version.txt docker-journal.log docker-version.json
  harbor-help.txt harbor-version.txt os-release.txt package-versions.txt
  pip-download.log pip-freeze.txt pip-install.log primary-wheel.sha256
  qemu-console.log qemu-image-info.json qemu-version.txt shutdown.log
  wheel-cache.sha256
)

qemu_pid_is_ours() {
  [[ "$qemu_pid" =~ ^[0-9]+$ && -r "/proc/$qemu_pid/cmdline" ]] || return 1
  tr '\0' '\n' <"/proc/$qemu_pid/cmdline" |
    grep -Fqx -- "file=$overlay,if=virtio,format=qcow2"
}

stop_qemu() {
  if [[ -z "$qemu_pid" && -f "$pidfile" ]]; then
    qemu_pid="$(cat "$pidfile" 2>/dev/null || true)"
  fi
  if qemu_pid_is_ours && kill -0 "$qemu_pid" 2>/dev/null; then
    kill "$qemu_pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$qemu_pid" 2>/dev/null || break
      sleep 0.25
    done
    if qemu_pid_is_ours && kill -0 "$qemu_pid" 2>/dev/null; then
      kill -9 "$qemu_pid" 2>/dev/null || true
    fi
    wait "$qemu_pid" 2>/dev/null || true
  fi
  qemu_pid=""
  rm -f -- "$pidfile"
}

cleanup() {
  stop_qemu
  rm -f -- "$overlay" "$seed" "$key" "$key.pub" "$known_hosts" \
    "$user_data" "$meta_data" "$manifest_tmp" "$pidfile" \
    "$prepared_hash_file.tmp" "$base_hash_file.tmp"
  if [[ -d "$evidence_tmp" ]]; then
    for name in "${evidence_files[@]}"; do
      rm -f -- "$evidence_tmp/$name"
    done
    rmdir -- "$evidence_tmp" 2>/dev/null || true
  fi
  rmdir -- "$run_dir" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ssh-keygen -q -t ed25519 -N '' -f "$key"
public_key="$(cat "$key.pub")"
cat >"$user_data" <<EOF
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
packages: [docker.io, docker-compose-v2, python3.12-venv, git, curl, ca-certificates]
runcmd:
  - [systemctl, enable, --now, docker]
EOF
cat >"$meta_data" <<EOF
instance-id: agentcongress-prepare-$run_id
local-hostname: agentcongress-stage2-prepare
EOF

cloud-localds "$seed" "$user_data" "$meta_data"
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
qemu_pid="$(cat "$pidfile")"
qemu_pid_is_ours && kill -0 "$qemu_pid"

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
ssh "${ssh_args[@]}" 'sudo cloud-init status --wait --long' \
  >"$evidence_tmp/cloud-init-status.txt"

ssh "${ssh_args[@]}" bash -s -- "$HARBOR_VERSION" "$HARBOR_WHEEL" \
    "$HARBOR_WHEEL_SHA256" "$COMPOSE_PACKAGE" "$COMPOSE_PACKAGE_VERSION" \
    "$COMPOSE_CLI_VERSION" "$COMPOSE_PLUGIN_PATH" \
    "$COMPOSE_PLUGIN_SHA256" <<'GUEST'
set -euo pipefail
harbor_version="$1"
harbor_wheel="$2"
harbor_wheel_sha="$3"
compose_package="$4"
compose_package_version="$5"
compose_cli_version="$6"
compose_plugin_path="$7"
compose_plugin_sha="$8"
venv=/opt/agentcongress-harbor
wheel_cache=/opt/agentcongress-harbor-wheel-cache
evidence=/var/tmp/agentcongress-prepared-evidence

sudo install -d -m 0755 -o stage2 -g stage2 "$evidence"
sudo install -d -m 0755 "$wheel_cache"
sudo python3.12 -m venv "$venv"
sudo "$venv/bin/python" -m pip download --only-binary :all: \
  --dest "$wheel_cache" "harbor==$harbor_version" >"$evidence/pip-download.log" 2>&1
printf '%s  %s\n' "$harbor_wheel_sha" "$wheel_cache/$harbor_wheel" |
  sha256sum -c - | tee "$evidence/primary-wheel.sha256"
sudo "$venv/bin/python" -m pip install --no-index --find-links "$wheel_cache" \
  "$wheel_cache/$harbor_wheel" >"$evidence/pip-install.log" 2>&1

installed_version="$(sudo "$venv/bin/python" -c \
  'from importlib.metadata import version; print(version("harbor"))')"
test "$installed_version" = "$harbor_version"
printf '%s\n' "$installed_version" >"$evidence/harbor-version.txt"
NO_COLOR=1 sudo "$venv/bin/harbor" --help >"$evidence/harbor-help.txt"
sudo "$venv/bin/python" -m pip freeze --all >"$evidence/pip-freeze.txt"
(
  cd "$wheel_cache"
  find . -maxdepth 1 -type f -name '*.whl' -print0 |
    sort -z | xargs -0 sha256sum
) >"$evidence/wheel-cache.sha256"

sudo systemctl is-active --quiet docker
sudo docker version --format '{{json .}}' >"$evidence/docker-version.json"
sudo journalctl -u docker -b --no-pager >"$evidence/docker-journal.log"

installed_compose_version="$(dpkg-query -W -f='${db:Status-Abbrev}\t${Architecture}\t${Version}' \
  "$compose_package")"
expected_compose_version="$(printf 'ii \tamd64\t%s' "$compose_package_version")"
test "$installed_compose_version" = "$expected_compose_version"
mapfile -t compose_plugins < <(dpkg-query -L "$compose_package" | \
  grep -Fx "$compose_plugin_path")
test "${#compose_plugins[@]}" = 1
compose_plugin_realpath="$(readlink -f -- "$compose_plugin_path")"
test "$compose_plugin_realpath" = "$compose_plugin_path"
test -f "$compose_plugin_path" && test ! -L "$compose_plugin_path"
compose_plugin_owner="$(stat -c '%U:%G' "$compose_plugin_path")"
test "$compose_plugin_owner" = root:root
compose_plugin_mode="$(stat -c '%a' "$compose_plugin_path")"
test "$compose_plugin_mode" = 755
actual_compose_plugin_sha="$(sha256sum "$compose_plugin_path" | awk '{print $1}')"
test "$actual_compose_plugin_sha" = "$compose_plugin_sha"
actual_compose_cli_version="$(sudo docker compose version --short)"
test "$actual_compose_cli_version" = "$compose_cli_version"
printf '%s\n' "$actual_compose_cli_version" >"$evidence/docker-compose-version.txt"
compose_probe="$(mktemp -d /var/tmp/agentcongress-compose-probe.XXXXXXXX)"
trap 'rm -rf -- "$compose_probe"' EXIT
cat >"$compose_probe/compose.yaml" <<'EOF'
services:
  main:
    image: scratch
    network_mode: none
    read_only: true
EOF
sudo docker compose --project-name agentcongress-prepare-probe \
  --file "$compose_probe/compose.yaml" config --quiet
printf 'compose_config_smoke=passed\n' >"$evidence/docker-compose-smoke.txt"
python3 - "$evidence/docker-compose-plugin.json" "$compose_package" \
    "$compose_package_version" "$actual_compose_cli_version" \
    "$compose_plugin_path" "$compose_plugin_realpath" \
    "$actual_compose_plugin_sha" "$compose_plugin_owner" \
    "$compose_plugin_mode" <<'PY'
import json
import sys

(
    output, package, package_version, cli_version, path, realpath, sha256,
    owner, mode,
) = sys.argv[1:]
document = {
    "cli_version": cli_version,
    "mode": "0" + mode,
    "owner": owner,
    "package": package,
    "package_version": package_version,
    "path": path,
    "realpath": realpath,
    "sha256": sha256,
}
with open(output, "w", encoding="utf-8", newline="\n") as handle:
    json.dump(document, handle, sort_keys=True, indent=2)
    handle.write("\n")
PY
rm -rf -- "$compose_probe"
compose_probe=""
trap - EXIT
dpkg-query -W -f='${Package}\t${Version}\n' \
  docker.io docker-compose-v2 python3.12 python3.12-venv git curl \
  >"$evidence/package-versions.txt"
cp /etc/os-release "$evidence/os-release.txt"
sudo cp /var/log/cloud-init.log "$evidence/cloud-init.log"
sudo cp /var/log/cloud-init-output.log "$evidence/cloud-init-output.log"
sudo chown -R stage2:stage2 "$evidence"
GUEST

guest_evidence=/var/tmp/agentcongress-prepared-evidence
ssh "${ssh_args[@]}" "tar -C '$guest_evidence' -cf - ." |
  tar -C "$evidence_tmp" -xf -

shutdown_rc=0
ssh "${ssh_args[@]}" 'sudo bash -s' >"$evidence_tmp/shutdown.log" 2>&1 <<'SANITIZE' || shutdown_rc=$?
set -euo pipefail
cloud-init clean --logs --seed --machine-id
rm -f -- /home/stage2/.ssh/authorized_keys /home/stage2/.bash_history \
  /root/.bash_history /var/lib/dbus/machine-id \
  /etc/ssh/ssh_host_dsa_key /etc/ssh/ssh_host_dsa_key.pub \
  /etc/ssh/ssh_host_ecdsa_key /etc/ssh/ssh_host_ecdsa_key.pub \
  /etc/ssh/ssh_host_ed25519_key /etc/ssh/ssh_host_ed25519_key.pub \
  /etc/ssh/ssh_host_rsa_key /etc/ssh/ssh_host_rsa_key.pub
truncate -s 0 /etc/machine-id
test ! -e /home/stage2/.ssh/authorized_keys
test ! -e /var/lib/dbus/machine-id
test ! -e /var/lib/cloud/instance
test ! -s /etc/machine-id
for host_key in /etc/ssh/ssh_host_dsa_key /etc/ssh/ssh_host_ecdsa_key \
    /etc/ssh/ssh_host_ed25519_key /etc/ssh/ssh_host_rsa_key; do
  test ! -e "$host_key"
  test ! -e "$host_key.pub"
done
sync
echo IMAGE_SANITIZED
systemctl poweroff --no-wall
SANITIZE

grep -Fx 'IMAGE_SANITIZED' "$evidence_tmp/shutdown.log" >/dev/null || {
  echo "guest sanitization did not complete (ssh rc=$shutdown_rc)" >&2
  exit 1
}
printf 'cloud_init_clean=true\nmachine_id_cleared=true\nssh_host_keys_cleared=true\nstage2_authorized_keys_cleared=true\n' \
  >"$evidence_tmp/cleanup-attestation.txt"

powered_off=false
for _ in $(seq 1 180); do
  if ! qemu_pid_is_ours || ! kill -0 "$qemu_pid" 2>/dev/null; then
    powered_off=true
    break
  fi
  sleep 1
done
if [[ "$powered_off" != true ]]; then
  echo "guest did not power off normally" >&2
  exit 1
fi
qemu_pid=""
rm -f -- "$pidfile"

# QEMU is now stopped, so both the serial log and qcow2 are immutable while
# their hashes are calculated.
qemu-system-x86_64 --version >"$evidence_tmp/qemu-version.txt"
qemu-img info --output=json "$overlay" >"$evidence_tmp/qemu-image-info.json"
prepared_sha="$(sha256sum "$overlay" | awk '{print $1}')"
printf 'backing_base_sha256=%s\nprepared_qcow2_sha256=%s\n' \
  "$BASE_SHA256" "$prepared_sha" >"$evidence_tmp/artifact-hashes.txt"
(
  cd "$evidence_tmp"
  find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%f\0' |
    sort -z | xargs -0 sha256sum >SHA256SUMS
)

built_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
final_evidence="$evidence_root/$run_id"
[[ ! -e "$final_evidence" ]] || {
  echo "refusing to overwrite evidence: $final_evidence" >&2
  exit 1
}
evidence_sums_sha="$(sha256sum "$evidence_tmp/SHA256SUMS" | awk '{print $1}')"
python3 - "$manifest_tmp" "$base" "$prepared" "$prepared_sha" \
    "$final_evidence" "$built_at" "$BASE_URL" "$BASE_SHA256" \
    "$HARBOR_VERSION" "$HARBOR_WHEEL_SHA256" "$PREPARED_PROFILE" \
    "$COMPOSE_PACKAGE" "$COMPOSE_PACKAGE_VERSION" "$COMPOSE_CLI_VERSION" \
    "$COMPOSE_PLUGIN_PATH" "$COMPOSE_PLUGIN_SHA256" "$evidence_sums_sha" <<'PY'
import json
import sys

(
    output,
    base,
    prepared,
    prepared_sha,
    evidence,
    built_at,
    base_url,
    base_sha,
    harbor_version,
    harbor_wheel_sha,
    profile,
    compose_package,
    compose_package_version,
    compose_cli_version,
    compose_plugin_path,
    compose_plugin_sha,
    evidence_sums_sha,
) = sys.argv[1:]
document = {
    "schema_version": 2,
    "kind": "agentcongress.stage-two.prepared-guest",
    "profile": profile,
    "built_at": built_at,
    "base": {"url": base_url, "path": base, "sha256": base_sha},
    "prepared": {"path": prepared, "format": "qcow2", "sha256": prepared_sha},
    "harbor": {"version": harbor_version, "wheel_sha256": harbor_wheel_sha},
    "docker": {"compose": {
        "package": compose_package,
        "package_version": compose_package_version,
        "cli_version": compose_cli_version,
        "plugin_path": compose_plugin_path,
        "plugin_sha256": compose_plugin_sha,
    }},
    "evidence": {"path": evidence, "sha256s_sha256": evidence_sums_sha},
}
with open(output, "w", encoding="utf-8", newline="\n") as handle:
    json.dump(document, handle, sort_keys=True, indent=2)
    handle.write("\n")
PY

mv -- "$evidence_tmp" "$final_evidence"
evidence_tmp=""
chmod 0444 "$overlay"
mv -- "$overlay" "$prepared"
printf '%s  %s\n' "$prepared_sha" "$(basename "$prepared")" \
  >"$prepared_hash_file.tmp"
mv -- "$prepared_hash_file.tmp" "$prepared_hash_file"
printf '%s  %s\n' "$BASE_SHA256" "$base" >"$base_hash_file.tmp"
mv -- "$base_hash_file.tmp" "$base_hash_file"
mv -- "$manifest_tmp" "$manifest"

valid_prepared_image || {
  echo "prepared guest failed final integrity verification" >&2
  exit 1
}
echo "Stage 2 prepared guest built; image=$prepared evidence=$final_evidence"
