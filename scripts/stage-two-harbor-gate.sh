#!/usr/bin/env bash
set -euo pipefail

# Run the first real Stage 2 Oracle/NOP control gate without a model.  The
# trusted control plane is a disposable QEMU overlay backed by the image made
# by stage-two-vm-prepare.sh.  Only one pinned TB2 task and the frozen suite are
# copied into the guest; no API/model credential is accepted or forwarded.

readonly TB2_URL="https://github.com/harbor-framework/terminal-bench-2.git"
readonly TB2_REVISION="2fd12b88aafdd04a52c298e3940bcb189f9766d6"
readonly TASK_ID="fix-code-vulnerability"
readonly TASK_TREE="9d0967e66487fa2beeb9c40f48802427f43a267a"
readonly AGENT_IMAGE="docker.io/alexgshaw/fix-code-vulnerability@sha256:cac325252991f823713b2d0441502972901dd782bd67f66c03d9b1e410dac5c0"
readonly AGENT_DIGEST="sha256:cac325252991f823713b2d0441502972901dd782bd67f66c03d9b1e410dac5c0"
readonly EXPECTED_SUITE_SHA256="583aebdfd7a46c2d45313de8e4d7ede0fcdb358781a096293bd5f7097389e944"
readonly PREPARED_NAME="agentcongress-stage2-noble-20260801-harbor-0.20.0-compose-v2-r1.qcow2"
readonly PREPARED_SHA256="40147a265d6b5d7ea4d5785dbf7513a60c395ef7417611a20f81ae11248ac07d"
readonly PREPARED_MANIFEST_SHA256="f73b4c5ec08a53ec62ee534acef9ffc0445ed388f90fca4fdbfe41a0260ce90f"
readonly PREPARED_PROFILE="compose-v2-r1"
readonly BASE_NAME="noble-server-cloudimg-amd64-20260801.img"
readonly BASE_URL="https://cloud-images.ubuntu.com/noble/20260801/noble-server-cloudimg-amd64.img"
readonly BASE_SHA256="0533b0655c32e68b31d792ecd6ccfca95abdbc536c4446874fe0513bd4140ffe"
readonly HARBOR_VERSION="0.20.0"
readonly HARBOR_WHEEL_SHA256="4b7e48223aea2384cdb8c9eff35eaebd482fc9b1ec09f8193a121c47356ff19a"
readonly COMPOSE_PACKAGE="docker-compose-v2"
readonly COMPOSE_PACKAGE_VERSION="2.40.3+ds1-0ubuntu1~24.04.1"
readonly COMPOSE_CLI_VERSION="2.40.3+ds1-0ubuntu1~24.04.1"
readonly COMPOSE_PLUGIN_PATH="/usr/libexec/docker/cli-plugins/docker-compose"
readonly COMPOSE_PLUGIN_SHA256="d87a11e944c990dc9f2186115b1136c1cbffffc870845caff0cbdcce0780f41d"

root="${1:-/root/AgentCongress/.agentcongress/stage-two/vm}"
suite_path="${2:-${STAGE2_SUITE_PATH:-}}"
suite_sha256="${3:-${STAGE2_SUITE_SHA256:-}}"
source_repo="${4:-${STAGE2_TB2_SOURCE:-$root/sources/terminal-bench-2}}"
ssh_port="${STAGE2_VM_SSH_PORT:-50222}"

usage() {
  echo "usage: $0 [vm-root] SUITE_PATH SUITE_SHA256 [TB2_SOURCE_REPO]" >&2
  echo "or set STAGE2_SUITE_PATH and STAGE2_SUITE_SHA256" >&2
  exit 2
}

case "$root" in /*) ;; *) echo "VM root must be absolute" >&2; exit 2 ;; esac
case "$source_repo" in /*) ;; *) echo "TB2 source path must be absolute" >&2; exit 2 ;; esac
[[ "$root" != "/" && -n "$suite_path" && -n "$suite_sha256" ]] || usage
case "$suite_path" in /*) ;; *) echo "suite path must be absolute" >&2; exit 2 ;; esac
[[ "$suite_sha256" =~ ^[0-9a-f]{64}$ ]] || {
  echo "suite SHA256 must be 64 lowercase hexadecimal characters" >&2
  exit 2
}
[[ "$suite_sha256" == "$EXPECTED_SUITE_SHA256" ]] || {
  echo "suite SHA256 does not identify the frozen Stage 2 protocol" >&2
  exit 2
}
[[ "$ssh_port" =~ ^[0-9]+$ ]] && (( ssh_port >= 1 && ssh_port <= 65535 )) || {
  echo "invalid SSH port" >&2
  exit 2
}
[[ -f "$suite_path" && ! -L "$suite_path" ]] || {
  echo "suite must be an existing regular non-symlink file" >&2
  exit 2
}
printf '%s  %s\n' "$suite_sha256" "$suite_path" | sha256sum -c -

for command in cloud-localds flock git qemu-img qemu-system-x86_64 sha256sum \
    ssh ssh-keygen tar python3; do
  command -v "$command" >/dev/null || {
    echo "missing command: $command" >&2
    exit 2
  }
done
python3 -m pip --version >/dev/null || {
  echo "host Python pip is required for acquisition-only wheel download" >&2
  exit 2
}

prepared_dir="$root/prepared"
prepared="$prepared_dir/$PREPARED_NAME"
base="$root/cache/$BASE_NAME"
prepared_stem="${prepared%.qcow2}"
prepared_manifest="$prepared_stem.manifest.json"
prepared_sha_file="$prepared.sha256"
base_sha_file="$prepared_stem.backing-base.sha256"
runs="$root/runs"
evidence_root="$root/evidence/harbor-gate"
mkdir -p "$runs" "$evidence_root" "$(dirname "$source_repo")"
# The image builder takes this lock exclusively.  Hold a shared lock for the
# whole gate so the verified backing bytes cannot change after preflight.
exec 8>"$root/prepare.lock"
flock -s 8
exec 9>"$root/harbor-gate.lock"
flock 9
# Serialize this script's acquisition and archive operations.  The archive is
# independently re-attested below, so an external writer can only make the
# gate fail, never substitute unchecked bytes.
exec 7>"$root/tb2-source.lock"
flock 7

# Recompute the prepared image identity before using it as a backing file.
python3 - "$base" "$prepared" "$prepared_manifest" "$prepared_sha_file" \
    "$base_sha_file" "$BASE_URL" "$BASE_SHA256" "$HARBOR_VERSION" \
    "$HARBOR_WHEEL_SHA256" "$PREPARED_SHA256" \
    "$PREPARED_MANIFEST_SHA256" "$PREPARED_PROFILE" "$COMPOSE_PACKAGE" \
    "$COMPOSE_PACKAGE_VERSION" "$COMPOSE_CLI_VERSION" \
    "$COMPOSE_PLUGIN_PATH" "$COMPOSE_PLUGIN_SHA256" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

base, image, manifest_path, sum_path, base_sum_path = map(Path, sys.argv[1:6])
(
    base_url, base_sha, expected_harbor, harbor_wheel_sha, trusted_image_sha,
    trusted_manifest_sha, profile, compose_package, compose_package_version,
    compose_cli_version, compose_plugin_path, compose_plugin_sha,
) = sys.argv[6:]

def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()

try:
    assert base.is_file() and not base.is_symlink()
    assert image.is_file() and not image.is_symlink()
    assert manifest_path.is_file() and not manifest_path.is_symlink()
    assert sum_path.is_file() and not sum_path.is_symlink()
    assert base_sum_path.is_file() and not base_sum_path.is_symlink()
    assert digest(manifest_path) == trusted_manifest_sha
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = digest(image)
    assert digest(base) == base_sha
    assert set(manifest) == {
        "base", "built_at", "docker", "evidence", "harbor", "kind",
        "prepared", "profile", "schema_version",
    }
    assert manifest["schema_version"] == 2
    assert manifest["kind"] == "agentcongress.stage-two.prepared-guest"
    assert manifest["profile"] == profile
    assert manifest["base"] == {
        "url": base_url, "path": str(base), "sha256": base_sha,
    }
    assert manifest["prepared"]["path"] == str(image)
    assert manifest["prepared"]["format"] == "qcow2"
    assert actual == trusted_image_sha
    assert manifest["prepared"]["sha256"] == trusted_image_sha
    assert manifest["harbor"]["version"] == expected_harbor
    assert manifest["harbor"]["wheel_sha256"] == harbor_wheel_sha
    assert manifest["docker"] == {"compose": {
        "package": compose_package,
        "package_version": compose_package_version,
        "cli_version": compose_cli_version,
        "plugin_path": compose_plugin_path,
        "plugin_sha256": compose_plugin_sha,
    }}
    assert sum_path.read_text(encoding="utf-8") == f"{trusted_image_sha}  {image.name}\n"
    assert base_sum_path.read_text(encoding="utf-8") == f"{base_sha}  {base}\n"
    evidence_doc = manifest["evidence"]
    assert set(evidence_doc) == {"path", "sha256s_sha256"}
    evidence = Path(evidence_doc["path"])
    assert evidence.is_absolute() and not evidence.is_symlink()
    evidence = evidence.resolve(strict=True)
    expected_evidence_root = (image.parents[1] / "evidence" / "prepared" / profile).resolve(strict=True)
    assert evidence.parent == expected_evidence_root
    assert re.fullmatch(r"prepare\.[A-Za-z0-9]{8}", evidence.name)
    sums = evidence / "SHA256SUMS"
    assert sums.is_file() and not sums.is_symlink()
    assert digest(sums) == evidence_doc["sha256s_sha256"]
    expected = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        assert match and match.group(2) != "SHA256SUMS" and match.group(2) not in expected
        expected[match.group(2)] = match.group(1)
    actual_files = {item.name for item in evidence.iterdir() if item.name != "SHA256SUMS"}
    required = {
        "artifact-hashes.txt", "cleanup-attestation.txt", "cloud-init-output.log",
        "cloud-init-status.txt", "cloud-init.log", "docker-compose-plugin.json",
        "docker-compose-smoke.txt", "docker-compose-version.txt",
        "docker-journal.log", "docker-version.json", "harbor-help.txt",
        "harbor-version.txt", "os-release.txt", "package-versions.txt",
        "pip-download.log", "pip-freeze.txt", "pip-install.log",
        "primary-wheel.sha256", "qemu-console.log", "qemu-image-info.json",
        "qemu-version.txt", "shutdown.log", "wheel-cache.sha256",
    }
    assert actual_files == set(expected) == required
    assert all(item.is_file() and not item.is_symlink() for item in evidence.iterdir())
    for name, expected_sha in expected.items():
        assert digest(evidence / name) == expected_sha
except (AssertionError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit("prepared guest integrity check failed")
PY

qemu-img info --output=json "$prepared" | python3 -c '
import json, os, sys
data = json.load(sys.stdin)
backing = data.get("full-backing-filename") or data.get("backing-filename")
valid = (data.get("format") == "qcow2" and backing and
         os.path.realpath(backing) == os.path.realpath(sys.argv[1]))
raise SystemExit(0 if valid else 1)
' "$base" || { echo "prepared guest backing file identity mismatch" >&2; exit 2; }

# Acquisition is deliberately outside the sealed runtime.  Keep a shallow Git
# object store and address the task by the frozen commit, never by its checkout.
if [[ ! -e "$source_repo" ]]; then
  GIT_NO_REPLACE_OBJECTS=1 git init -q "$source_repo"
  GIT_NO_REPLACE_OBJECTS=1 git -C "$source_repo" remote add origin "$TB2_URL"
elif [[ ! -d "$source_repo/.git" || -L "$source_repo" ]]; then
  echo "TB2 source exists but is not a non-symlink Git repository" >&2
  exit 2
fi
origin_url="$(GIT_NO_REPLACE_OBJECTS=1 git -C "$source_repo" remote get-url origin 2>/dev/null || true)"
[[ "$origin_url" == "$TB2_URL" || "$origin_url" == "${TB2_URL%.git}" ]] || {
  echo "TB2 source origin does not match the frozen repository" >&2
  exit 2
}
if ! GIT_NO_REPLACE_OBJECTS=1 git -C "$source_repo" cat-file -e "$TB2_REVISION^{commit}" 2>/dev/null; then
  GIT_NO_REPLACE_OBJECTS=1 git -C "$source_repo" fetch --depth 1 --no-tags origin "$TB2_REVISION"
fi
[[ "$(GIT_NO_REPLACE_OBJECTS=1 git -C "$source_repo" rev-parse "$TB2_REVISION^{commit}")" == "$TB2_REVISION" ]] || {
  echo "TB2 revision resolution mismatch" >&2
  exit 2
}
task_tree="$(GIT_NO_REPLACE_OBJECTS=1 git -C "$source_repo" rev-parse "$TB2_REVISION:$TASK_ID")"
[[ "$task_tree" == "$TASK_TREE" ]] || {
  echo "TB2 task tree identity mismatch" >&2
  exit 2
}
GIT_NO_REPLACE_OBJECTS=1 git -C "$source_repo" ls-tree -r -z --full-tree \
  "$TB2_REVISION:$TASK_ID" | python3 -c '
import sys
expected = {
    ".gitignore": ("100644", "ecd089e8"),
    "README.md": ("100644", "6ba62989"),
    "environment/Dockerfile": ("100644", "0cd79310"),
    "instruction.md": ("100644", "8431db14"),
    "solution/solve.sh": ("100644", "1e8d8f46"),
    "task.toml": ("100644", "eb534985"),
    "tests/test.sh": ("100644", "2d2aefa5"),
    "tests/test_outputs.py": ("100644", "26a7ce20"),
}
actual = {}
for entry in sys.stdin.buffer.read().split(b"\0"):
    if not entry:
        continue
    header, raw_path = entry.split(b"\t", 1)
    mode, kind, oid = header.decode().split()
    path = raw_path.decode()
    if kind != "blob":
        raise SystemExit("task tree contains a non-regular entry")
    actual[path] = (mode, oid)
if set(actual) != set(expected):
    raise SystemExit("task tree file set differs from the frozen eight files")
if any(actual[path][0] != mode or not actual[path][1].startswith(prefix)
       for path, (mode, prefix) in expected.items()):
    raise SystemExit("task tree blob identity mismatch")
'
verify_task_file() {
  local path="$1"
  local expected="$2"
  local actual
  actual="$(GIT_NO_REPLACE_OBJECTS=1 git -C "$source_repo" cat-file blob \
    "$TB2_REVISION:$TASK_ID/$path" | sha256sum | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || {
    echo "frozen byte hash mismatch: $path" >&2
    exit 2
  }
}
verify_task_file environment/Dockerfile 0917470986224df4daf4854a71302ae81c73ba5dd6e25be1232d29b758a5b3ab
verify_task_file instruction.md 89b1abdf0af19399f720233b5cdf6b2e8e0ff7b6e3fe3e2a3af0d4d298a8641a
verify_task_file solution/solve.sh 7a0131b369d83ae8546ecc3f9b4e23d38240f1bd1f0941e4430fa85b5182ee03
verify_task_file task.toml 9aa2bd14ce9408289d199d0608bc3fa65b8b5e23374c9641020de62197dbf1e2
verify_task_file tests/test.sh f1611da693cb4cfc5e8482698c6a1858a5c67220ae9db89b50aed66584e9b17f
verify_task_file tests/test_outputs.py c006a5de95b69bd78055aa26cc055734dbd50718ede25d331f767bfee4ef7f26

run_dir="$(mktemp -d "$runs/harbor-gate.XXXXXXXX")"
run_id="$(basename "$run_dir")"
overlay="$run_dir/overlay.qcow2"
seed="$run_dir/seed.img"
key="$run_dir/id_ed25519"
known_hosts="$run_dir/known_hosts"
pidfile="$run_dir/qemu.pid"
user_data="$run_dir/user-data"
meta_data="$run_dir/meta-data"
task_archive="$run_dir/task.tar"
wheel_bundle="$run_dir/verifier-wheel-bundle"
wheel_archive="$run_dir/verifier-wheels.tar"
evidence="$evidence_root/$run_id"
mkdir "$evidence"
console="$evidence/qemu-console.log"
qemu_pid=""

GIT_NO_REPLACE_OBJECTS=1 git -c tar.umask=0022 -C "$source_repo" archive \
  --format=tar --prefix=original/ \
  "$TB2_REVISION" "$TASK_ID" >"$task_archive"
python3 - "$task_archive" "$TASK_ID" <<'PY'
import hashlib
import sys
import tarfile
from pathlib import PurePosixPath

archive, task_id = sys.argv[1:]
prefix = PurePosixPath("original") / task_id
expected_modes = {
    ".gitignore": 0o644,
    "README.md": 0o644,
    "environment/Dockerfile": 0o644,
    "instruction.md": 0o644,
    "solution/solve.sh": 0o644,
    "task.toml": 0o644,
    "tests/test.sh": 0o644,
    "tests/test_outputs.py": 0o644,
}
expected_sha256 = {
    "environment/Dockerfile": "0917470986224df4daf4854a71302ae81c73ba5dd6e25be1232d29b758a5b3ab",
    "instruction.md": "89b1abdf0af19399f720233b5cdf6b2e8e0ff7b6e3fe3e2a3af0d4d298a8641a",
    "solution/solve.sh": "7a0131b369d83ae8546ecc3f9b4e23d38240f1bd1f0941e4430fa85b5182ee03",
    "task.toml": "9aa2bd14ce9408289d199d0608bc3fa65b8b5e23374c9641020de62197dbf1e2",
    "tests/test.sh": "f1611da693cb4cfc5e8482698c6a1858a5c67220ae9db89b50aed66584e9b17f",
    "tests/test_outputs.py": "c006a5de95b69bd78055aa26cc055734dbd50718ede25d331f767bfee4ef7f26",
}
files = {}
with tarfile.open(archive, mode="r:") as bundle:
    for member in bundle.getmembers():
        path = PurePosixPath(member.name)
        if member.isdir():
            continue
        try:
            relative = str(path.relative_to(prefix))
        except ValueError as exc:
            raise SystemExit("task archive member escapes the frozen prefix") from exc
        if not member.isfile() or relative in files:
            raise SystemExit("task archive contains a non-regular or duplicate member")
        if relative not in expected_modes or member.mode & 0o7777 != expected_modes[relative]:
            raise SystemExit(f"task archive mode/file-set mismatch: {relative}")
        source = bundle.extractfile(member)
        if source is None:
            raise SystemExit(f"task archive member cannot be read: {relative}")
        files[relative] = hashlib.sha256(source.read()).hexdigest()
if set(files) != set(expected_modes):
    raise SystemExit("task archive does not contain the frozen eight-file closure")
for relative, expected in expected_sha256.items():
    if files[relative] != expected:
        raise SystemExit(f"task archive byte hash mismatch: {relative}")
PY
task_archive_sha256="$(sha256sum "$task_archive" | awk '{print $1}')"

# Acquisition is the only phase allowed to contact PyPI.  The six-wheel set,
# including every pytest runtime dependency, is closed and byte-pinned.
mkdir -p "$wheel_bundle/wheels"
python3 -m pip download --disable-pip-version-check --only-binary=:all: \
  --no-deps --dest "$wheel_bundle/wheels" \
  pytest==8.4.1 pytest-json-ctrf==0.3.5 iniconfig==2.1.0 packaging==25.0 \
  pluggy==1.6.0 pygments==2.19.2 >"$evidence/wheel-download.log" 2>&1
cat >"$wheel_bundle/wheels.sha256" <<'EOF'
539c70ba6fcead8e78eebbf1115e8b589e7565830d7d006a8723f19ac8a0afb7  pytest-8.4.1-py3-none-any.whl
e82fd1d69be2f92385bc33540063e5ad7b17b36de67764c84f3ceb9815a895e9  pytest_json_ctrf-0.3.5-py3-none-any.whl
9deba5723312380e77435581c6bf4935c94cbfab9b1ed33ef8d238ea168eb760  iniconfig-2.1.0-py3-none-any.whl
29572ef2b1f17581046b3a2227d5c611fb25ec70ca1ba8554b24b0e69331a484  packaging-25.0-py3-none-any.whl
e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746  pluggy-1.6.0-py3-none-any.whl
86540386c03d588bb81d44bc3928634ff26449851e99741617ecb9037ee5ec0b  pygments-2.19.2-py3-none-any.whl
EOF
python3 - "$wheel_bundle" <<'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected = {}
for line in (root / "wheels.sha256").read_text(encoding="utf-8").splitlines():
    match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.+-]+\.whl)", line)
    if not match or match.group(2) in expected:
        raise SystemExit("invalid closed wheel manifest")
    expected[match.group(2)] = match.group(1)
actual = {item.name for item in (root / "wheels").iterdir()
          if item.is_file() and not item.is_symlink()}
if actual != set(expected):
    raise SystemExit(f"wheel acquisition is not the fixed six-file set: {sorted(actual)}")
PY
(
  cd "$wheel_bundle/wheels"
  sha256sum -c ../wheels.sha256
)
cat >"$wheel_bundle/requirements.lock" <<'EOF'
pytest==8.4.1 --hash=sha256:539c70ba6fcead8e78eebbf1115e8b589e7565830d7d006a8723f19ac8a0afb7
pytest-json-ctrf==0.3.5 --hash=sha256:e82fd1d69be2f92385bc33540063e5ad7b17b36de67764c84f3ceb9815a895e9
iniconfig==2.1.0 --hash=sha256:9deba5723312380e77435581c6bf4935c94cbfab9b1ed33ef8d238ea168eb760
packaging==25.0 --hash=sha256:29572ef2b1f17581046b3a2227d5c611fb25ec70ca1ba8554b24b0e69331a484
pluggy==1.6.0 --hash=sha256:e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746
pygments==2.19.2 --hash=sha256:86540386c03d588bb81d44bc3928634ff26449851e99741617ecb9037ee5ec0b
EOF
tar --sort=name --mtime='@0' --owner=0 --group=0 --numeric-owner \
  -C "$wheel_bundle" -cf "$wheel_archive" requirements.lock wheels.sha256 wheels
wheel_archive_sha256="$(sha256sum "$wheel_archive" | awk '{print $1}')"
wheel_manifest_sha256="$(sha256sum "$wheel_bundle/wheels.sha256" | awk '{print $1}')"
requirements_lock_sha256="$(sha256sum "$wheel_bundle/requirements.lock" | awk '{print $1}')"

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
    for _ in $(seq 1 40); do
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
    "$pidfile" "$user_data" "$meta_data" "$task_archive" "$wheel_archive"
  rm -rf -- "$wheel_bundle"
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
runcmd:
  - [systemctl, enable, --now, docker]
EOF
cat >"$meta_data" <<EOF
instance-id: agentcongress-harbor-gate-$run_id
local-hostname: agentcongress-stage2-gate
EOF

cloud-localds "$seed" "$user_data" "$meta_data"
qemu-img create -q -f qcow2 -F qcow2 -b "$prepared" "$overlay"
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
ssh "${ssh_args[@]}" 'sudo cloud-init status --wait --long' >"$evidence/cloud-init-status.txt"

guest_control="/var/tmp/agentcongress-harbor-gate-$run_id"
ssh "${ssh_args[@]}" "install -d -m 0700 '$guest_control'"
ssh "${ssh_args[@]}" "cat >'$guest_control/task.tar'" <"$task_archive"
ssh "${ssh_args[@]}" "cat >'$guest_control/suite.yaml'" <"$suite_path"
ssh "${ssh_args[@]}" "cat >'$guest_control/verifier-wheels.tar'" <"$wheel_archive"

ssh "${ssh_args[@]}" bash -s -- "$guest_control" "$suite_sha256" \
    "$task_archive_sha256" "$TB2_REVISION" "$task_tree" "$TASK_ID" \
    "$AGENT_IMAGE" "$AGENT_DIGEST" "$HARBOR_VERSION" "$run_id" \
    "$wheel_archive_sha256" "$wheel_manifest_sha256" \
    "$requirements_lock_sha256" "$COMPOSE_PACKAGE" \
    "$COMPOSE_PACKAGE_VERSION" "$COMPOSE_CLI_VERSION" \
    "$COMPOSE_PLUGIN_PATH" "$COMPOSE_PLUGIN_SHA256" <<'GUEST'
set -euo pipefail
control="$1"
suite_sha="$2"
archive_sha="$3"
revision="$4"
task_tree="$5"
task_id="$6"
agent_image="$7"
agent_digest="$8"
harbor_version="$9"
run_id="${10}"
wheel_archive_sha="${11}"
wheel_manifest_sha="${12}"
requirements_lock_sha="${13}"
compose_package="${14}"
compose_package_version="${15}"
compose_cli_version="${16}"
compose_plugin_path="${17}"
compose_plugin_sha="${18}"
original="$control/original/$task_id"
derived="$control/derived/$task_id"
wheel_bundle="$control/verifier-wheel-bundle"
jobs="$control/jobs"
out="$control/evidence"
mkdir -p "$control/original" "$control/derived" "$jobs" "$out"

# Re-attest Compose inside the actual gate guest before Harbor can create a
# Job.  This is measurement-only: the sealed runtime is never package-mutated.
installed_compose_version="$(dpkg-query -W \
  -f='${db:Status-Abbrev}\t${Architecture}\t${Version}' "$compose_package")"
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
actual_compose_cli_version="$(docker compose version --short)"
test "$actual_compose_cli_version" = "$compose_cli_version"
compose_probe="$control/compose-runtime-probe.yaml"
cat >"$compose_probe" <<'EOF'
services:
  main:
    image: scratch
    network_mode: none
    read_only: true
EOF
docker compose --project-name agentcongress-gate-probe \
  --file "$compose_probe" config --quiet
rm -f -- "$compose_probe"
python3 - "$out/docker-compose-runtime.json" "$compose_package" \
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
    "config_smoke": "passed",
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

printf '%s  %s\n' "$suite_sha" "$control/suite.yaml" | sha256sum -c -
printf '%s  %s\n' "$archive_sha" "$control/task.tar" | sha256sum -c -
printf '%s  %s\n' "$wheel_archive_sha" "$control/verifier-wheels.tar" | sha256sum -c -
tar -C "$control" -xf "$control/task.tar"
mkdir "$wheel_bundle"
tar -C "$wheel_bundle" -xf "$control/verifier-wheels.tar"
test "$(sha256sum "$wheel_bundle/wheels.sha256" | awk '{print $1}')" = "$wheel_manifest_sha"
test "$(sha256sum "$wheel_bundle/requirements.lock" | awk '{print $1}')" = "$requirements_lock_sha"
(
  cd "$wheel_bundle/wheels"
  test "$(find . -maxdepth 1 -type f -name '*.whl' -printf '%f\n' | sort | wc -l)" = 6
  sha256sum -c ../wheels.sha256
)
test -f "$original/task.toml"
test -f "$original/instruction.md"
test -f "$original/tests/test.sh"
test -f "$original/solution/solve.sh"
test ! -L "$original"

# Validate that the uploaded suite itself declares this exact source/image lock.
/opt/agentcongress-harbor/bin/python - "$control/suite.yaml" "$suite_sha" \
    "$revision" "$task_id" "$agent_digest" <<'PY'
import hashlib
import sys
from pathlib import Path
import yaml

path = Path(sys.argv[1])
expected_sha, revision, task_id, image_digest = sys.argv[2:]
assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha
suite = yaml.safe_load(path.read_text(encoding="utf-8"))
assert suite["suite"]["id"] == "stage-two-v1"
assert suite["sources"]["terminal_bench_2"]["revision"] == revision
matches = [task for task in suite["tasks"] if task.get("id") == task_id]
assert len(matches) == 1
task = matches[0]
assert task["source"] == "terminal_bench_2"
assert task["source_locator"] == task_id
assert task["image"]["digest"] == image_digest
assert task["image"]["platform"] == "linux/amd64"
PY

cp -a -- "$original" "$derived"
original_task_sha="$(sha256sum "$original/task.toml" | awk '{print $1}')"
original_tests_manifest="$control/original-tests.sha256"
original_solution_manifest="$control/original-solution.sha256"
(
  cd "$original/tests"
  find . -type f -printf '%P\0' | sort -z | xargs -0 -r sha256sum
) >"$original_tests_manifest"
(
  cd "$original/solution"
  find . -type f -printf '%P\0' | sort -z | xargs -0 -r sha256sum
) >"$original_solution_manifest"
tests_sha="$(sha256sum "$original_tests_manifest" | awk '{print $1}')"
solution_sha="$(sha256sum "$original_solution_manifest" | awk '{print $1}')"

# The original test and solution bytes remain unchanged.  Generated files only
# provide the isolated runtime and immutable verifier image.
cp -a "$wheel_bundle/wheels" "$derived/tests/wheels"
cp "$wheel_bundle/requirements.lock" "$derived/tests/requirements.lock"
cat >"$derived/tests/Dockerfile" <<EOF
FROM $agent_image
USER root
COPY wheels /wheels
COPY requirements.lock /requirements.lock
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels \
      --require-hashes -r /requirements.lock && \
    python -m pip check && rm -rf /wheels /root/.cache
COPY test.sh test_outputs.py /tests/
RUN chmod 0555 /tests/test.sh && mkdir -p /app /logs/verifier && chmod 0777 /app /logs/verifier
ENV PIP_NO_INDEX=1 UV_OFFLINE=1
WORKDIR /app
EOF

cat >"$derived/environment/docker-compose.yaml" <<EOF
services:
  main:
    image: $agent_image
    labels:
      agentcongress.stage2.gate: "$run_id"
      agentcongress.stage2.role: agent-main
    network_mode: none
    read_only: true
    privileged: false
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    pids_limit: 256
    working_dir: /app
    tmpfs:
      - /tmp:rw,nosuid,nodev,noexec,size=256m
      - /run:rw,nosuid,nodev,noexec,size=32m
    volumes:
      - agent-app:/app
      - oracle-solution:/solution
volumes:
  agent-app: {}
  oracle-solution: {}
EOF

# Pull and verify the agent manifest before deriving the separate verifier.
docker pull "$agent_image" >"$out/agent-image-pull.txt"
docker image inspect "$agent_image" >"$out/agent-image-inspect.json"
/opt/agentcongress-harbor/bin/python - "$out/agent-image-inspect.json" "$agent_digest" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
assert len(doc) == 1
item = doc[0]
assert item["Os"] == "linux" and item["Architecture"] == "amd64"
assert any(value.endswith("@" + sys.argv[2]) for value in item.get("RepoDigests") or [])
PY

verifier_tag="agentcongress-stage2-verifier:${suite_sha:0:16}"
docker build --pull=false --network=none -t "$verifier_tag" \
  "$derived/tests" >"$out/verifier-image-build.txt"
verifier_image_id="$(docker image inspect --format '{{.Id}}' "$verifier_tag")"
[[ "$verifier_image_id" =~ ^sha256:[0-9a-f]{64}$ && "$verifier_image_id" != "$agent_digest" ]]
docker image inspect "$verifier_image_id" >"$out/verifier-image-inspect.json"
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp:rw,nosuid,nodev,noexec \
  "$verifier_image_id" sh -c \
  'test "$PIP_NO_INDEX" = 1; python -m pip show pytest pytest-json-ctrf' \
  >"$out/verifier-packages.txt"
grep -Fx 'Version: 8.4.1' "$out/verifier-packages.txt" >/dev/null
grep -Fx 'Version: 0.3.5' "$out/verifier-packages.txt" >/dev/null

cat >"$derived/tests/docker-compose.yaml" <<EOF
services:
  main:
    image: $verifier_image_id
    labels:
      agentcongress.stage2.gate: "$run_id"
      agentcongress.stage2.role: verifier-main
    network_mode: none
    read_only: true
    privileged: false
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    pids_limit: 256
    working_dir: /app
    environment:
      PIP_NO_INDEX: "1"
      UV_OFFLINE: "1"
    tmpfs:
      - /tmp:rw,nosuid,nodev,noexec,size=256m
      - /run:rw,nosuid,nodev,noexec,size=32m
    volumes:
      - verifier-app:/app
volumes:
  verifier-app: {}
EOF

cat >"$control/hardening-compose.yaml" <<'EOF'
services:
  main:
    network_mode: none
    read_only: true
    privileged: false
    pids_limit: 256
EOF

cat >"$derived/task.toml" <<EOF
schema_version = "1.3"
artifacts = ["/app"]

[task]
name = "agentcongress/$task_id-stage2"
description = "Faithful isolated overlay of TB2 $task_id"
authors = [{ name = "Terminal-Bench 2 authors" }]
keywords = ["terminal-bench-2", "stage-two", "security"]

[metadata]
source_revision = "$revision"
source_task_tree = "$task_tree"
source_task_toml_sha256 = "$original_task_sha"
source_tests_sha256 = "$tests_sha"
source_solution_sha256 = "$solution_sha"
suite_sha256 = "$suite_sha"
agent_image_digest = "$agent_digest"
verifier_image_id = "$verifier_image_id"

[agent]
timeout_sec = 900.0
network_mode = "no-network"

[verifier]
timeout_sec = 900.0
network_mode = "no-network"
environment_mode = "separate"
env = { PIP_NO_INDEX = "1", UV_OFFLINE = "1" }

[environment]
network_mode = "no-network"
docker_image = "$agent_image"
os = "linux"
build_timeout_sec = 600.0
cpus = 1
memory_mb = 2048
storage_mb = 10240

[verifier.environment]
network_mode = "no-network"
os = "linux"
build_timeout_sec = 600.0
cpus = 1
memory_mb = 2048
storage_mb = 10240
EOF

# Recheck every upstream test/solution byte after overlay creation.
(
  cd "$derived/tests"
  while read -r expected rel; do
    printf '%s  %s\n' "$expected" "$rel" | sha256sum -c - >/dev/null
  done <"$original_tests_manifest"
)
(
  cd "$derived/solution"
  sha256sum -c "$original_solution_manifest" >/dev/null
)

task_config_sha="$(sha256sum "$derived/task.toml" | awk '{print $1}')"
cat >"$out/task-lock.json" <<EOF
{"agent_image":{"digest":"$agent_digest","platform":"linux/amd64","reference":"$agent_image"},"requirements_lock_sha256":"$requirements_lock_sha","source_revision":"$revision","source_task_tree":"$task_tree","suite_id":"stage-two-v1","suite_sha256":"$suite_sha","task_id":"$task_id","task_metadata_sha256":"$task_config_sha","upstream_task_metadata_sha256":"$original_task_sha","verifier_image":{"digest":"$verifier_image_id","platform":"linux/amd64","reference":"$verifier_tag"},"verifier_sha256":"$tests_sha","verifier_wheel_archive_sha256":"$wheel_archive_sha","verifier_wheel_manifest_sha256":"$wheel_manifest_sha","solution_sha256":"$solution_sha"}
EOF
cp "$control/suite.yaml" "$out/suite.yaml"
cp "$original_tests_manifest" "$out/original-tests.sha256"
cp "$original_solution_manifest" "$out/original-solution.sha256"

cat >"$control/run-control.py" <<'PY'
import asyncio
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path

from harbor.job import Job
from harbor.models.environment_type import EnvironmentType
from harbor.models.job.config import JobConfig, RetryConfig
from harbor.models.job.lock import TrialLock
from harbor.models.trial.config import (
    AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig,
)
from harbor.models.trial.result import TrialResult

control, task_raw, jobs_raw, evidence_raw = sys.argv[1:]
if control not in {"oracle", "nop"}:
    raise SystemExit("invalid zero-model control")
task = Path(task_raw).resolve(strict=True)
jobs = Path(jobs_raw).resolve()
evidence = Path(evidence_raw).resolve()
job_name = f"{control}-job"

sensitive = ("OPENAI", "ANTHROPIC", "DEEPSEEK", "CODEX", "API_KEY", "AUTH_TOKEN", "GITHUB_TOKEN")
leaked = sorted(name for name in os.environ if any(word in name.upper() for word in sensitive))
if leaked:
    raise SystemExit(f"credential-shaped environment names present: {leaked}")
hardening_compose = Path(os.environ.get("STAGE2_HARDENING_COMPOSE", "")).resolve(strict=True)
sealed_root = Path(task_raw).resolve(strict=True).parents[1]
if hardening_compose.parent != sealed_root:
    # Both are under this run's sealed control root; reject any external file.
    raise SystemExit("hardening Compose path escapes the sealed control root")

env_fields = EnvironmentConfig.model_fields
if not {"delete", "kwargs"}.issubset(env_fields):
    raise SystemExit("Harbor 0.20 EnvironmentConfig retention fields are absent")
environment = EnvironmentConfig(
    type=EnvironmentType.DOCKER,
    delete=True,
    kwargs={"keep_containers": True},
    extra_docker_compose=[hardening_compose],
)
kwargs = {
    "job_name": job_name,
    "jobs_dir": jobs,
    "n_attempts": 1,
    "agents": [AgentConfig(name=control)],
    "tasks": [TaskConfig(path=task)],
    "environment": environment,
    "n_concurrent_trials": 1,
    "quiet": True,
    "retry": RetryConfig(max_retries=0),
}
config = JobConfig(**kwargs)
async def execute():
    job = await Job.create(config)
    return await job.run()

result = asyncio.run(execute())
trial_results = list(result.trial_results)
if len(trial_results) != 1:
    raise SystemExit(f"expected one {control} trial, got {len(trial_results)}")
trial_result = trial_results[0]
if trial_result.exception_info is not None:
    raise SystemExit(f"{control} trial recorded an exception")
if trial_result.agent_info.name != control:
    raise SystemExit(f"{control} agent identity mismatch")
if not trial_result.task_checksum:
    raise SystemExit(f"{control} task checksum is empty")
verifier = trial_result.verifier_result
if verifier is None or verifier.rewards is None or "reward" not in verifier.rewards:
    raise SystemExit(f"{control} trial has no structured reward")
reward = verifier.rewards["reward"]
if isinstance(reward, bool) or not isinstance(reward, (int, float)):
    raise SystemExit(f"{control} reward is not numeric")

job_root = jobs / job_name
candidates = []
for result_path in job_root.rglob("result.json"):
    trial = result_path.parent
    if ((trial / "config.json").is_file() and (trial / "lock.json").is_file()
            and (trial / "artifacts" / "manifest.json").is_file()
            and (trial / "verifier").is_dir()):
        candidates.append(trial)
if len(candidates) != 1:
    raise SystemExit(f"expected one materialized {control} trial directory, got {len(candidates)}")
trial = candidates[0]
reward_files = [path for path in (trial / "verifier").rglob("reward.*") if path.is_file()]
if len(reward_files) != 1:
    raise SystemExit(f"expected one verifier reward file, got {len(reward_files)}")

config_doc = json.loads((trial / "config.json").read_text(encoding="utf-8"))
lock_doc = json.loads((trial / "lock.json").read_text(encoding="utf-8"))
result_doc = json.loads((trial / "result.json").read_text(encoding="utf-8"))
artifact_doc = json.loads(
    (trial / "artifacts" / "manifest.json").read_text(encoding="utf-8")
)
api_doc = trial_result.model_dump(mode="json")
persisted_config = TrialConfig.model_validate(config_doc)
persisted_lock = TrialLock.model_validate(lock_doc)
persisted_result = TrialResult.model_validate(result_doc)
if persisted_result != trial_result or result_doc != api_doc:
    raise SystemExit(f"{control} persisted result does not bind to API result")
if persisted_result.config != persisted_config:
    raise SystemExit(f"{control} persisted config does not bind to result config")
if result_doc.get("exception_info") is not None:
    raise SystemExit(f"{control} persisted result records an exception")
if (result_doc.get("agent_info") or {}).get("name") != control:
    raise SystemExit(f"{control} persisted agent identity mismatch")
if result_doc.get("task_checksum") != trial_result.task_checksum:
    raise SystemExit(f"{control} persisted task checksum mismatch")
task_lock = persisted_lock.task
if (persisted_lock.schema_version != 1 or task_lock.type != "local"
        or not task_lock.digest.startswith("sha256:")):
    raise SystemExit(f"{control} trial lock does not bind a local task digest")

def read_reward(path):
    text = path.read_text(encoding="utf-8").strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = float(text)
    if isinstance(value, dict):
        value = value.get("reward")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SystemExit(f"{control} reward file is not numeric")
    return float(value)

file_reward = read_reward(reward_files[0])
if float(reward) != file_reward:
    raise SystemExit(f"{control} API reward differs from reward file")
if not isinstance(artifact_doc, list):
    raise SystemExit(f"{control} artifact manifest is not a list")
app_entries = [entry for entry in artifact_doc
               if isinstance(entry, dict) and entry.get("source") == "/app"]
if len(app_entries) != 1 or app_entries[0].get("status") != "ok":
    raise SystemExit(f"{control} /app artifact collection is not status ok")
if any(entry.get("status") == "failed" for entry in artifact_doc
       if isinstance(entry, dict)):
    raise SystemExit(f"{control} artifact manifest contains a failed entry")

destination = evidence / control
destination.mkdir(parents=True, exist_ok=False)
for name in ("config.json", "lock.json", "result.json"):
    shutil.copy2(trial / name, destination / name)
shutil.copy2(reward_files[0], destination / ("reward" + reward_files[0].suffix))
shutil.copy2(trial / "artifacts" / "manifest.json", destination / "artifacts-manifest.json")
with tarfile.open(destination / "verifier.tar", "w") as archive:
    archive.add(trial / "verifier", arcname="verifier", recursive=True)
summary = {
    "agent": control,
    "job_id": job_name,
    "trial_id": trial.name,
    "trial_uuid": str(trial_result.id),
    "task_checksum": trial_result.task_checksum,
    "task_lock_digest": task_lock.digest,
    "reward": reward,
    "reward_file": file_reward,
}
(destination / "api-result.json").write_text(
    json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps({"control": control, "reward": reward, "trial": str(trial)}))
PY

declare -a gate_projects=()
discover_gate_projects() {
  local inspect_file="$control/owned-main-inspect.json"
  local projects_file="$control/owned-projects.candidate"
  local -a ids=()
  mapfile -t ids < <(docker ps -aq --no-trunc \
    --filter "label=agentcongress.stage2.gate=$run_id" \
    --filter 'label=com.docker.compose.service=main')
  ((${#ids[@]} > 0)) || return 0
  docker inspect "${ids[@]}" >"$inspect_file" || return 1
  /opt/agentcongress-harbor/bin/python - "$inspect_file" "$jobs" "$run_id" \
      >"$projects_file" <<'PY' || return 1
import json
import re
import sys
from pathlib import Path

containers = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
jobs = Path(sys.argv[2]).resolve(strict=True)
run_id = sys.argv[3]
projects = set()
for item in containers:
    labels = (item.get("Config") or {}).get("Labels") or {}
    project = labels.get("com.docker.compose.project")
    role = labels.get("agentcongress.stage2.role")
    if (labels.get("agentcongress.stage2.gate") != run_id
            or labels.get("com.docker.compose.service") != "main"
            or role not in {"agent-main", "verifier-main"}
            or not isinstance(project, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project)):
        raise SystemExit("labelled main is not an owned Harbor Compose container")
    log_binds = []
    for mount in item.get("Mounts") or []:
        if mount.get("Type") != "bind":
            continue
        destination = mount.get("Destination", "")
        source = Path(mount.get("Source", "")).resolve(strict=True)
        if source.is_relative_to(jobs) and destination.startswith("/logs"):
            log_binds.append((source, destination))
    if not log_binds:
        raise SystemExit("labelled main lacks a current-job logs ownership bind")
    projects.add(project)
for project in sorted(projects):
    print(project)
PY
  local project
  while read -r project; do
    [[ -n "$project" ]] || continue
    if [[ ! " ${gate_projects[*]:-} " =~ [[:space:]]${project}[[:space:]] ]]; then
      gate_projects+=("$project")
    fi
  done <"$projects_file"
}
cleanup_gate_projects() {
  local original_rc=$?
  local mode="${1:-exit}"
  local cleanup_rc=0
  local project id actual
  trap - EXIT
  set +e
  discover_gate_projects || cleanup_rc=1
  # Probe containers carry the run label but no Compose project.  Main labels
  # grant project ownership only through discover_gate_projects above; remove
  # other run-labelled containers individually without widening that authority.
  while read -r id; do
    [[ -n "$id" ]] || continue
    actual="$(docker inspect --format '{{index .Config.Labels "agentcongress.stage2.gate"}}' "$id" 2>/dev/null)"
    if [[ "$actual" == "$run_id" ]]; then
      docker rm -f "$id" >/dev/null || cleanup_rc=1
    fi
  done < <(docker ps -aq --no-trunc --filter "label=agentcongress.stage2.gate=$run_id")
  for project in "${gate_projects[@]:-}"; do
    [[ "$project" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || continue
    while read -r id; do
      [[ -n "$id" ]] || continue
      actual="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$id" 2>/dev/null)"
      if [[ "$actual" == "$project" ]]; then
        docker rm -f "$id" >/dev/null || cleanup_rc=1
      fi
    done < <(docker ps -aq --no-trunc --filter "label=com.docker.compose.project=$project")
    while read -r id; do
      [[ -n "$id" ]] || continue
      actual="$(docker network inspect --format '{{index .Labels "com.docker.compose.project"}}' "$id" 2>/dev/null)"
      if [[ "$actual" == "$project" ]]; then
        docker network rm "$id" >/dev/null || cleanup_rc=1
      fi
    done < <(docker network ls -q --filter "label=com.docker.compose.project=$project")
    while read -r id; do
      [[ -n "$id" ]] || continue
      actual="$(docker volume inspect --format '{{index .Labels "com.docker.compose.project"}}' "$id" 2>/dev/null)"
      if [[ "$actual" == "$project" ]]; then
        docker volume rm "$id" >/dev/null || cleanup_rc=1
      fi
    done < <(docker volume ls -q --filter "label=com.docker.compose.project=$project")
    [[ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$project")" ]] || cleanup_rc=1
    [[ -z "$(docker network ls -q --filter "label=com.docker.compose.project=$project")" ]] || cleanup_rc=1
    [[ -z "$(docker volume ls -q --filter "label=com.docker.compose.project=$project")" ]] || cleanup_rc=1
  done
  if [[ -n "${out:-}" && -d "$out" ]]; then
    printf 'scope=exact-compose-project-labels\nprojects=%s\nstatus=%s\n' \
      "${gate_projects[*]:-}" "$([[ $cleanup_rc == 0 ]] && echo ok || echo failed)" \
      >"$out/docker-cleanup.txt"
  fi
  set -e
  if [[ "$mode" == return ]]; then
    if (( original_rc != 0 || cleanup_rc != 0 )); then
      return 97
    fi
    return 0
  fi
  if (( original_rc != 0 )); then
    exit "$original_rc"
  fi
  if (( cleanup_rc != 0 )); then
    exit 97
  fi
  exit 0
}
trap cleanup_gate_projects EXIT

run_control() {
  local name="$1"
  local before_projects="$control/$name-before-owned.projects"
  local before_all_projects="$control/$name-before-all.projects"
  local current_projects="$control/$name-compose-projects.txt"
  local runner_rc=0
  printf '%s\n' "${gate_projects[@]:-}" | sed '/^$/d' | sort -u >"$before_projects"
  : >"$before_all_projects"
  docker ps -aq --no-trunc | xargs -r docker inspect --format \
    '{{index .Config.Labels "com.docker.compose.project"}}' | sed '/^$/d' | sort -u \
    >>"$before_all_projects"
  docker network ls -q | xargs -r docker network inspect --format \
    '{{index .Labels "com.docker.compose.project"}}' | sed '/^$/d' \
    >>"$before_all_projects"
  docker volume ls -q | xargs -r docker volume inspect --format \
    '{{index .Labels "com.docker.compose.project"}}' | sed '/^$/d' \
    >>"$before_all_projects"
  sort -u -o "$before_all_projects" "$before_all_projects"
  env -i PATH=/opt/agentcongress-harbor/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/home/stage2 LANG=C.UTF-8 HARBOR_TELEMETRY_ENABLED=false \
    STAGE2_HARDENING_COMPOSE="$control/hardening-compose.yaml" \
    /opt/agentcongress-harbor/bin/python "$control/run-control.py" \
      "$name" "$derived" "$jobs" "$out" || runner_rc=$?
  discover_gate_projects || return 98
  printf '%s\n' "${gate_projects[@]:-}" | sed '/^$/d' | sort -u | \
    comm -13 "$before_projects" - >"$current_projects"
  (( runner_rc == 0 )) || return "$runner_rc"
  test -s "$current_projects"
  test -z "$(comm -12 "$before_all_projects" "$current_projects")"
  cp "$current_projects" "$out/$name/compose-projects.txt"
  : >"$out/$name/container-ids.txt"
  local project
  while read -r project; do
    docker ps -aq --no-trunc --filter "label=com.docker.compose.project=$project" \
      >>"$out/$name/container-ids.txt"
  done <"$current_projects"
  sort -u -o "$out/$name/container-ids.txt" "$out/$name/container-ids.txt"
  test -s "$out/$name/container-ids.txt"
  local -a ids=()
  mapfile -t ids <"$out/$name/container-ids.txt"
  docker inspect "${ids[@]}" >"$out/$name/docker-inspect.json"
  /opt/agentcongress-harbor/bin/python - "$out/$name/docker-inspect.json" \
      "$current_projects" <<'PY'
import json
import re
import sys
from pathlib import Path

containers = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = set(Path(sys.argv[2]).read_text(encoding="utf-8").splitlines())
projects = set()
for item in containers:
    labels = (item.get("Config") or {}).get("Labels") or {}
    project = labels.get("com.docker.compose.project")
    service = labels.get("com.docker.compose.service")
    if (not isinstance(project, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project)
            or not isinstance(service, str) or not service):
        raise SystemExit("new Harbor container lacks safe Compose project/service labels")
    projects.add(project)
if not projects or projects != expected:
    raise SystemExit("retained container projects differ from owned fresh projects")
PY
}

run_control oracle
run_control nop

# Validate actual runtime properties and role-classify Compose main containers
# separately from Harbor's egress-control sidecars.
/opt/agentcongress-harbor/bin/python - "$out" "$jobs" "$verifier_image_id" \
    "$agent_digest" <<'PY'
import json
import re
import sys
from pathlib import Path

out = Path(sys.argv[1])
jobs = Path(sys.argv[2])
verifier_image, agent_digest = sys.argv[3:]
jobs = jobs.resolve(strict=True)
sensitive = re.compile(r"^(OPENAI_|ANTHROPIC_|DEEPSEEK_|CODEX_|AWS_|GITHUB_TOKEN|SSH_AUTH_SOCK)")
sidecar_service = "harbor-docker-egress-control-sidecar"
control_state = {}
for control in ("oracle", "nop"):
    path = out / control / "docker-inspect.json"
    containers = json.loads(path.read_text(encoding="utf-8"))
    if len(containers) < 2:
        raise SystemExit(f"{control}: fewer than agent and verifier containers retained")
    expected_ids = set((out / control / "container-ids.txt").read_text().splitlines())
    if {item.get("Id") for item in containers} != expected_ids:
        raise SystemExit(f"{control}: inspect set differs from fresh container set")
    mains = {"agent": [], "verifier": []}
    projects, app_volumes, all_volumes = set(), set(), set()
    roles = []
    for item in containers:
        config, host = item["Config"], item["HostConfig"]
        labels = config.get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        service = labels.get("com.docker.compose.service")
        if not project or not service:
            raise SystemExit(f"{control}: unknown/unlabelled container")
        projects.add(project)
        for entry in config.get("Env") or []:
            name, _, value = entry.partition("=")
            if sensitive.match(name) or value.startswith("sk-"):
                raise SystemExit(f"{control}: credential-shaped container environment")
        for mount in item.get("Mounts") or []:
            destination = mount.get("Destination", "")
            if mount.get("Type") == "volume":
                all_volumes.add(mount.get("Name"))
            if destination in {"/var/run/docker.sock", "/run/docker.sock"}:
                raise SystemExit(f"{control}: Docker socket exposed")
            if mount.get("Type") == "bind":
                if service == sidecar_service:
                    raise SystemExit(f"{control}: egress sidecar has a host bind")
                source = Path(mount["Source"]).resolve(strict=True)
                if not source.is_relative_to(jobs) or not destination.startswith("/logs"):
                    raise SystemExit(f"{control}: non-job/log host bind exposed")
            if destination == "/app" and mount.get("Type") == "volume":
                app_volumes.add(mount["Name"])
        if host.get("Devices") or host.get("Privileged"):
            raise SystemExit(f"{control}: privileged/device-bearing container")
        if service == "main":
            state = item.get("State") or {}
            if state.get("Running") is not False or state.get("Status") != "exited":
                raise SystemExit(f"{control}: retained main is not stopped cleanly")
            if (host.get("NetworkMode") != "none" or not host.get("ReadonlyRootfs")
                    or "ALL" not in (host.get("CapDrop") or [])
                    or "no-new-privileges:true" not in (host.get("SecurityOpt") or [])):
                raise SystemExit(f"{control}: main container hardening mismatch")
            pids = host.get("PidsLimit")
            if not isinstance(pids, int) or not 0 < pids <= 256:
                raise SystemExit(f"{control}: main PID limit mismatch")
            if item["Image"] == verifier_image:
                role = "verifier-main"
                mains["verifier"].append(item["Id"])
            else:
                if not str(config.get("Image", "")).endswith("@" + agent_digest):
                    raise SystemExit(f"{control}: agent main is not the pinned digest")
                role = "agent-main"
                mains["agent"].append(item["Id"])
            roles.append({"id": item["Id"], "project": project,
                          "service": service, "role": role, "image_id": item["Image"]})
        elif service == sidecar_service:
            image_ref = str(config.get("Image", ""))
            if image_ref != "harbor-prebuilt:harbor-docker-egress-control-sidecar":
                raise SystemExit(f"{control}: unknown image under egress sidecar role")
            if any(mount.get("Type") == "bind" for mount in item.get("Mounts") or []):
                raise SystemExit(f"{control}: sidecar bind mount present")
            if set(host.get("CapAdd") or []) != {"NET_ADMIN", "NET_RAW"}:
                raise SystemExit(f"{control}: sidecar has unexpected capabilities")
            roles.append({"id": item["Id"], "project": project,
                          "service": service, "role": "egress-sidecar",
                          "image_id": item["Image"]})
        else:
            raise SystemExit(f"{control}: unknown Compose service {service!r}")
    if len(mains["agent"]) != 1 or len(mains["verifier"]) != 1:
        raise SystemExit(f"{control}: expected one agent main and one verifier main")
    sidecars = [role for role in roles if role["role"] == "egress-sidecar"]
    if len(sidecars) != 2:
        raise SystemExit(f"{control}: expected one egress sidecar per environment")
    if {role["project"] for role in sidecars} != projects:
        raise SystemExit(f"{control}: egress sidecars do not cover both projects")
    if len(app_volumes) != 2:
        raise SystemExit(f"{control}: agent and verifier /app volumes are not distinct")
    if len(projects) != 2:
        raise SystemExit(f"{control}: expected fresh agent and verifier projects")
    if set((out / control / "compose-projects.txt").read_text().splitlines()) != projects:
        raise SystemExit(f"{control}: Compose project evidence mismatch")
    solution_volumes = {
        mount.get("Name") for item in containers
        if (item.get("Config", {}).get("Labels") or {}).get("com.docker.compose.service") == "main"
        and item["Id"] in mains["agent"]
        for mount in item.get("Mounts") or []
        if mount.get("Type") == "volume" and mount.get("Destination") == "/solution"
    }
    if len(solution_volumes) != 1:
        raise SystemExit(f"{control}: expected exactly one agent /solution volume")
    control_state[control] = {"ids": expected_ids, "projects": projects,
                              "app_volumes": app_volumes, "volumes": all_volumes}
    (out / control / "container-roles.json").write_text(
        json.dumps(roles, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (out / control / "solution-volume.txt").write_text(
        next(iter(solution_volumes)) + "\n", encoding="utf-8")
for field in ("ids", "projects", "app_volumes", "volumes"):
    if control_state["oracle"][field] & control_state["nop"][field]:
        raise SystemExit(f"Oracle and NOP reused {field}")
PY

# Resolve an already role-classified retained main without executing it.
role_container_id() {
  local control_name="$1"
  local role="$2"
  /opt/agentcongress-harbor/bin/python - \
      "$out/$control_name/container-roles.json" "$role" <<'PY'
import json
import sys

roles = json.load(open(sys.argv[1], encoding="utf-8"))
matches = [item["id"] for item in roles if item.get("role") == sys.argv[2]]
if len(matches) != 1:
    raise SystemExit("retained main role is not unique")
print(matches[0])
PY
}

# docker cp reads a stopped container's effective filesystem, including mounted
# volumes.  Accept absence only when the pinned daemon explicitly reports that
# exact path missing; daemon/transport/permission failures remain failures.
attest_stopped_path_absent() {
  local control_name="$1"
  local role="$2"
  local path="$3"
  local container_id error_file rc
  container_id="$(role_container_id "$control_name" "$role")"
  error_file="$control/cp-error-$control_name-${role}-${path#/}.txt"
  set +e
  docker cp "$container_id:$path/." - >/dev/null 2>"$error_file"
  rc=$?
  set -e
  if ((rc == 0)) || ! /opt/agentcongress-harbor/bin/python - \
      "$error_file" "$container_id" "$path" <<'PY'
import re
import sys
from pathlib import Path

message = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
container_id, path = sys.argv[2:]
pattern = rf"Error response from daemon: Could not find the file {re.escape(path)}(?:/\.)? in container {re.escape(container_id)}"
raise SystemExit(0 if re.fullmatch(pattern, message) else 1)
PY
  then
    rm -f -- "$error_file"
    echo "$control_name $role $path absence is not proven" >&2
    return 1
  fi
  rm -f -- "$error_file"
  printf 'stopped_path_absent_%s_%s_%s=passed\n' \
    "$control_name" "$role" "${path#/}" \
    >>"$out/actual-container-isolation.txt"
}

attest_stopped_tests_present() {
  local control_name="$1"
  local container_id
  local -a pipeline_status=()
  container_id="$(role_container_id "$control_name" verifier-main)"
  set +e
  docker cp "$container_id:/tests/." - 2>/dev/null | \
    /opt/agentcongress-harbor/bin/python -c '
import sys
import tarfile

found = False
try:
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as archive:
        for member in archive:
            if member.name.lstrip("./") == "test.sh" and member.isfile():
                found = True
except (OSError, tarfile.TarError):
    raise SystemExit(90)
raise SystemExit(0 if found else 91)
'
  pipeline_status=("${PIPESTATUS[@]}")
  set -e
  if ((${pipeline_status[0]} != 0 || ${pipeline_status[1]} != 0)); then
    echo "$control_name verifier actual /tests check failed" >&2
    return 1
  fi
  printf 'stopped_tests_present_%s_verifier-main=passed\n' "$control_name" \
    >>"$out/actual-container-isolation.txt"
}

attest_stopped_path_absent oracle agent-main /tests
attest_stopped_path_absent nop agent-main /tests
attest_stopped_tests_present oracle
attest_stopped_tests_present nop
attest_stopped_path_absent oracle verifier-main /solution
attest_stopped_path_absent nop verifier-main /solution

# Inspect the NOP agent's actual stopped /solution mount.  docker cp works for
# stopped containers; only tar metadata is consumed, and any entry fails closed.
nop_agent_id="$(role_container_id nop agent-main)"
set +e
docker cp "$nop_agent_id:/solution/." - | /opt/agentcongress-harbor/bin/python -c '
import sys
import tarfile

try:
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as archive:
        for member in archive:
            if member.name.strip("./"):
                raise SystemExit(91)
except (OSError, tarfile.TarError):
    raise SystemExit(90)
' >/dev/null
nop_solution_status=("${PIPESTATUS[@]}")
set -e
if ((${nop_solution_status[0]} != 0 || ${nop_solution_status[1]} != 0)); then
  echo "NOP stopped agent /solution is not proven empty" >&2
  exit 1
fi
printf 'stopped_nop_solution_mount=empty\n' >>"$out/actual-container-isolation.txt"

# Do not revive stopped trial containers.  Active canaries use disposable,
# separately labelled probe containers with the same hardening envelope.
run_probe() {
  local name="$1"
  local image="$2"
  local mode="$3"
  local probe_id
  probe_id="$(docker create --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 64 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m \
    --label "agentcongress.stage2.gate=$run_id" \
    --label "agentcongress.stage2.probe=$name" --entrypoint sh "$image" -c '
    set -eu
    test ! -S /var/run/docker.sock
    test ! -S /run/docker.sock
    test "$(awk "/^CapEff:/ {print \$2}" /proc/self/status)" = 0000000000000000
    test "$(awk "/^NoNewPrivs:/ {print \$2}" /proc/self/status)" = 1
    if [ "$1" = agent ]; then
      test ! -e /tests
      test ! -e /solution
    else
      test -f /tests/test.sh
      test ! -e /solution
      test "$PIP_NO_INDEX" = 1
      python -m pip show pytest pytest-json-ctrf >/dev/null
    fi
    python -c "import socket
for target in ((\"1.1.1.1\", 80), (\"example.com\", 80)):
    try:
        socket.create_connection(target, 2)
    except OSError:
        continue
    raise SystemExit(1)"
  ' probe "$mode")"
  docker inspect "$probe_id" >"$out/$name-probe-inspect.json"
  docker start -a "$probe_id" >"$out/$name-probe.txt" 2>&1
  test "$(docker inspect --format '{{.State.ExitCode}}' "$probe_id")" = 0
  docker rm "$probe_id" >/dev/null
  printf 'active_isolation_probe=passed\n' >>"$out/$name-probe.txt"
}

run_probe agent "$agent_image" agent
run_probe verifier "$verifier_image_id" verifier

/opt/agentcongress-harbor/bin/python - "$out" "$suite_sha" "$revision" \
    "$task_id" "$agent_digest" "$verifier_image_id" <<'PY'
import hashlib
import json
import math
import sys
from pathlib import Path

out = Path(sys.argv[1])
suite_sha, revision, task_id, agent_digest, verifier_digest = sys.argv[2:]
summaries = {}
for control in ("oracle", "nop"):
    summary = json.loads((out / control / "api-result.json").read_text(encoding="utf-8"))
    reward = summary["reward"]
    if isinstance(reward, bool) or not isinstance(reward, (int, float)) or not math.isfinite(reward):
        raise SystemExit(f"{control}: invalid reward")
    summaries[control] = summary
if summaries["oracle"]["reward"] != 1:
    raise SystemExit("Oracle did not reach reward 1")
if summaries["nop"]["reward"] >= 1:
    raise SystemExit("NOP unexpectedly reached success")
if summaries["oracle"]["trial_id"] == summaries["nop"]["trial_id"]:
    raise SystemExit("controls reused a trial id")
if summaries["oracle"]["trial_uuid"] == summaries["nop"]["trial_uuid"]:
    raise SystemExit("controls reused a trial UUID")
if summaries["oracle"]["task_checksum"] != summaries["nop"]["task_checksum"]:
    raise SystemExit("controls did not execute the same fixed task checksum")
if summaries["oracle"]["task_lock_digest"] != summaries["nop"]["task_lock_digest"]:
    raise SystemExit("controls did not execute the same locked task digest")
if any(summary["reward"] != summary["reward_file"] for summary in summaries.values()):
    raise SystemExit("API/file reward binding failed")

required = {
    "config": "config.json",
    "lock": "lock.json",
    "result": "result.json",
    "verifier": "verifier.tar",
    "artifacts_manifest": "artifacts-manifest.json",
}
manifest = {"schema_version": 1, "controls": {}}
for control in ("oracle", "nop"):
    directory = out / control
    reward = next(directory.glob("reward.*"), None)
    if reward is None:
        raise SystemExit(f"{control}: reward artifact absent")
    entries = {**required, "reward": reward.name}
    manifest["controls"][control] = {}
    for kind, name in entries.items():
        path = directory / name
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"{control}: required {kind} artifact absent")
        manifest["controls"][control][kind] = {
            "path": f"{control}/{name}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
(out / "control-artifacts.json").write_text(
    json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
)
gate = {
    "schema_version": 1,
    "kind": "agentcongress.stage-two.harbor-oracle-gate",
    "status": "ok",
    "evidence_level": "measured",
    "scope": "single-task-gate-evidence-not-five-task-EnvironmentLock",
    "model_calls": 0,
    "suite_sha256": suite_sha,
    "source_revision": revision,
    "task_id": task_id,
    "agent_image_digest": agent_digest,
    "verifier_image_digest": verifier_digest,
    "oracle": {"job_id": "oracle-job", "trial_id": summaries["oracle"]["trial_id"], "reward": summaries["oracle"]["reward"]},
    "isolation_nop": {"job_id": "nop-job", "trial_id": summaries["nop"]["trial_id"], "reward": summaries["nop"]["reward"]},
}
(out / "gate-result.json").write_text(
    json.dumps(gate, sort_keys=True, indent=2) + "\n", encoding="utf-8"
)
PY

docker version --format '{{json .}}' >"$out/docker-version.json"
/opt/agentcongress-harbor/bin/python -c \
  'from importlib.metadata import version; print(version("harbor"))' >"$out/harbor-version.txt"
test "$(cat "$out/harbor-version.txt")" = "$harbor_version"
cp /etc/os-release "$out/os-release.txt"
uname -a >"$out/uname.txt"
printf 'source_revision=%s\nsource_task_tree=%s\ntask_archive_sha256=%s\nsuite_sha256=%s\n' \
  "$revision" "$task_tree" "$archive_sha" "$suite_sha" >"$out/source-attestation.txt"
printf 'verifier_wheel_archive_sha256=%s\nverifier_wheel_manifest_sha256=%s\nrequirements_lock_sha256=%s\n' \
  "$wheel_archive_sha" "$wheel_manifest_sha" "$requirements_lock_sha" \
  >>"$out/source-attestation.txt"
printf 'scope=single-task gate evidence; not a five-task EnvironmentLock\n' \
  >"$out/evidence-scope.txt"
cleanup_gate_projects return || exit 97
chmod -R go-rwx "$control"
GUEST

ssh "${ssh_args[@]}" "tar -C '$guest_control/evidence' -cf - ." |
  tar -C "$evidence" -xf -

# Power off before hashing the console and overlay so both are immutable.
shutdown_rc=0
ssh "${ssh_args[@]}" 'sudo systemctl poweroff --no-wall' \
  >"$evidence/guest-poweroff.txt" 2>&1 || shutdown_rc=$?
powered_off=false
for _ in $(seq 1 180); do
  if ! qemu_pid_is_ours || ! kill -0 "$qemu_pid" 2>/dev/null; then
    powered_off=true
    break
  fi
  sleep 1
done
[[ "$powered_off" == true ]] || {
  echo "guest failed to power off after successful gate (ssh rc=$shutdown_rc)" >&2
  exit 1
}
qemu_pid=""
rm -f -- "$pidfile"

qemu-system-x86_64 --version | head -n 1 >"$evidence/qemu-version.txt"
qemu-img info --output=json "$overlay" >"$evidence/qemu-overlay-info.json"
printf 'prepared_qcow2_sha256=%s\nfresh_overlay_sha256=%s\n' \
  "$(sha256sum "$prepared" | awk '{print $1}')" \
  "$(sha256sum "$overlay" | awk '{print $1}')" >"$evidence/vm-image-hashes.txt"
printf 'task_archive_sha256=%s\nsource_task_tree=%s\nsuite_sha256=%s\nverifier_wheel_archive_sha256=%s\nverifier_wheel_manifest_sha256=%s\nrequirements_lock_sha256=%s\n' \
  "$task_archive_sha256" "$task_tree" "$suite_sha256" \
  "$wheel_archive_sha256" "$wheel_manifest_sha256" \
  "$requirements_lock_sha256" >"$evidence/host-input-attestation.txt"
(
  cd "$evidence"
  find . -type f ! -name SHA256SUMS -printf '%P\0' |
    sort -z | xargs -0 sha256sum >SHA256SUMS
  sha256sum -c SHA256SUMS >/dev/null
)

echo "Stage 2 Harbor Oracle/NOP gate passed; evidence=$evidence"
