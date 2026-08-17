from __future__ import annotations

import json
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


_SCHEMA_VERSION = 1
_SUPPORTED_FEATURES = frozenset({"use_legacy_landlock"})
_PROBE_NAMES = (
    "sandbox_launch",
    "subprocess",
    "workspace_read",
    "workspace_write",
    "outside_workspace_read",
    "outside_workspace_write",
    "network_connect",
)
_PROBE_SOURCE = r'''from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path


workspace = Path(sys.argv[1])
outside = Path(sys.argv[2])
host = sys.argv[3]
port = int(sys.argv[4])
network_timeout = float(sys.argv[5])
workspace_write_expected = sys.argv[6] == "allowed"
probes = {}


def result(name, expected, passed, observation):
    probes[name] = {
        "expected": expected,
        "status": "passed" if passed else "failed",
        "passed": bool(passed),
        "observation": observation,
    }


try:
    child = subprocess.run(
        [str(Path(sys.executable).resolve()), "-c", "print('agentcongress-child-ok')"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3,
    )
    ok = child.returncode == 0 and child.stdout.strip() == "agentcongress-child-ok"
    result("subprocess", "allowed", ok, "child process completed" if ok else "child process failed")
except Exception as error:
    result("subprocess", "allowed", False, f"{type(error).__name__}: {error}")

try:
    ok = (workspace / "seed.txt").read_text(encoding="utf-8") == "agentcongress-seed"
    result("workspace_read", "allowed", ok, "seed read" if ok else "seed content mismatch")
except Exception as error:
    result("workspace_read", "allowed", False, f"{type(error).__name__}: {error}")

try:
    target = workspace / "write.txt"
    target.write_text("agentcongress-write", encoding="utf-8")
    ok = target.read_text(encoding="utf-8") == "agentcongress-write"
    result(
        "workspace_write",
        "allowed" if workspace_write_expected else "denied",
        ok if workspace_write_expected else False,
        "workspace write completed" if workspace_write_expected and ok else "write unexpectedly succeeded",
    )
except OSError as error:
    result(
        "workspace_write",
        "allowed" if workspace_write_expected else "denied",
        not workspace_write_expected,
        f"denied: {type(error).__name__}",
    )
except Exception as error:
    result(
        "workspace_write",
        "allowed" if workspace_write_expected else "denied",
        False,
        f"unexpected {type(error).__name__}: {error}",
    )

escape = outside / "escape.txt"
secret = outside / "secret-canary.txt"
try:
    secret.read_bytes()
except OSError as error:
    result("outside_workspace_read", "denied", True, f"denied: {type(error).__name__}")
except Exception as error:
    result("outside_workspace_read", "denied", False, f"unexpected {type(error).__name__}: {error}")
else:
    result("outside_workspace_read", "denied", False, "read unexpectedly succeeded")

try:
    escape.write_text("sandbox-escaped", encoding="utf-8")
except OSError as error:
    result("outside_workspace_write", "denied", not escape.exists(), f"denied: {type(error).__name__}")
except Exception as error:
    result("outside_workspace_write", "denied", False, f"unexpected {type(error).__name__}: {error}")
else:
    result("outside_workspace_write", "denied", False, "write unexpectedly succeeded")

connection = None
try:
    connection = socket.create_connection((host, port), timeout=network_timeout)
except OSError as error:
    result("network_connect", "denied", True, f"denied: {type(error).__name__}")
except Exception as error:
    result("network_connect", "denied", False, f"unexpected {type(error).__name__}: {error}")
else:
    result("network_connect", "denied", False, "connection unexpectedly succeeded")
finally:
    if connection is not None:
        connection.close()

print(json.dumps({"schema_version": 1, "probes": probes}, sort_keys=True))
'''


def _probe(expected: str, status: str, observation: str) -> dict[str, Any]:
    return {
        "expected": expected,
        "status": status,
        "passed": status == "passed",
        "observation": observation,
    }


def _not_run_probes(
    observation: str, *, permission_profile: str = ":workspace"
) -> dict[str, dict[str, Any]]:
    expectations = {
        "sandbox_launch": "allowed",
        "subprocess": "allowed",
        "workspace_read": "allowed",
        "workspace_write": (
            "denied" if permission_profile == ":read-only" else "allowed"
        ),
        "outside_workspace_read": "denied",
        "outside_workspace_write": "denied",
        "network_connect": "denied",
    }
    return {
        name: _probe(expectations[name], "not_run", observation)
        for name in _PROBE_NAMES
    }


def _backend(system: str, enabled_features: Iterable[str]) -> str:
    features = set(enabled_features)
    if system == "Windows":
        return "windows-permission-profile"
    if system == "Linux":
        return (
            "linux-legacy-landlock-diagnostic"
            if "use_legacy_landlock" in features
            else "linux-bwrap-permission-profile"
        )
    if system == "Darwin":
        return "macos-seatbelt-permission-profile"
    return "unsupported"


def _sandbox_subcommand(system: str) -> str:
    try:
        return {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}[system]
    except KeyError as error:
        raise ValueError(f"unsupported sandbox host platform: {system}") from error


def _uses_host_subcommand(codex_version: str | None, system: str) -> bool:
    """Older POSIX CLIs nested debug sandboxes under a host subcommand.

    Current CLIs dispatch ``codex sandbox`` directly to the host backend.  The
    legacy shape is kept only for versions that are known to require it; a
    version string that cannot be parsed fails toward the current interface.
    """

    if system not in {"Linux", "Darwin"} or not codex_version:
        return False
    match = __import__("re").search(r"codex-cli\s+(\d+)\.(\d+)\.(\d+)", codex_version)
    if match is None:
        return False
    return tuple(map(int, match.groups())) < (0, 138, 0)


def classify_sandbox_diagnostic(text: str) -> str:
    """Classify narrow Codex sandbox bootstrap diagnostics."""

    folded = " ".join(text.casefold().split())
    if "bwrap:" in folded:
        if "failed to make / slave" in folded:
            return "bwrap_mount_namespace_denied"
        if "failed rtm_newaddr" in folded or "failed rtm_newlink" in folded:
            return "bwrap_network_namespace_denied"
        if any(
            marker in folded
            for marker in (
                "no permissions to create new namespace",
                "setting up uid map",
                "write failed /proc/self/uid_map",
                "creating new namespace failed",
            )
        ):
            return "bwrap_user_namespace_denied"
        if any(
            marker in folded
            for marker in (
                "command not found",
                "not found",
                "no such file or directory",
                "could not find",
            )
        ):
            return "bwrap_missing"
        return "bwrap_failed"
    helper_named = "codex-windows-sandbox-setup.exe" in folded
    helper_launch_failed = "orchestrator_helper_launch_failed" in folded
    access_denied = any(
        marker in folded
        for marker in (
            "access denied",
            "access is denied",
            "os error 5",
            "error 5",
            "拒绝访问",
            "鎷掔粷璁块棶",
        )
    )
    if (helper_named or helper_launch_failed) and access_denied:
        return "windows_helper_access_denied"
    if helper_named or helper_launch_failed:
        return "windows_helper_launch_failed"
    if "windows sandbox failed" in folded and access_denied:
        return "windows_sandbox_access_denied"
    if "windows sandbox failed" in folded:
        return "windows_sandbox_failed"
    if "sandbox(landlockrestrict)" in folded or "error running landlock" in folded:
        return "landlock_unavailable"
    return "sandbox_launch_failed"


def _diagnostic(code: str, message: str, *, legacy_landlock: bool) -> dict[str, str]:
    value = {"code": code, "message": message}
    if code.startswith("bwrap_") and not legacy_landlock:
        value["remediation"] = (
            "Repair bubblewrap/user-namespace support or use an externally "
            "hardened runner. Legacy Landlock has full host read access and "
            "does not satisfy the experiment isolation gate."
        )
    return value


def build_sandbox_command(
    codex_executable: str,
    probe_script: Path,
    workspace: Path,
    outside: Path,
    network_host: str,
    network_port: int,
    network_timeout_seconds: float,
    *,
    enabled_features: Iterable[str] = (),
    python_executable: Path | None = None,
    system: str | None = None,
    codex_version: str | None = None,
    permission_profile: str = ":workspace",
) -> list[str]:
    """Build a shell-free, model-free Codex sandbox command."""

    features = tuple(dict.fromkeys(enabled_features))
    unsupported = set(features) - _SUPPORTED_FEATURES
    if unsupported:
        raise ValueError(f"unsupported sandbox preflight feature: {sorted(unsupported)[0]}")
    python = (python_executable or Path(sys.executable)).resolve()
    host_system = system or platform.system()
    if permission_profile not in {":workspace", ":read-only"}:
        raise ValueError("sandbox preflight supports only :workspace or :read-only")
    command = [codex_executable, "sandbox"]
    if _uses_host_subcommand(codex_version, host_system):
        command.append(_sandbox_subcommand(host_system))
    for feature in features:
        command.extend(["--enable", feature])
    legacy_shape = _uses_host_subcommand(codex_version, host_system)
    if legacy_shape:
        # Permission profiles did not become a supported automation surface
        # until Codex 0.138. Legacy debug sandboxes are diagnostic-only and
        # will fail the outside-read canary on their full-read policy.
        command.extend(
            [
                "--full-auto",
                "-c",
                'sandbox_mode="workspace-write"',
                "-c",
                "sandbox_workspace_write.network_access=false",
                "-c",
                "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                "-c",
                "sandbox_workspace_write.exclude_slash_tmp=true",
            ]
        )
    else:
        command.extend(["-P", permission_profile, "-C", str(workspace.resolve())])
    command.extend(
        [
            "--",
            str(python),
            str(probe_script.resolve()),
            str(workspace.resolve()),
            str(outside.resolve()),
            network_host,
            str(network_port),
            str(network_timeout_seconds),
            "allowed" if permission_profile == ":workspace" else "denied",
        ]
    )
    return command


@dataclass(frozen=True, slots=True)
class SandboxPreflightResult:
    ready: bool
    codex_version: str | None
    platform: dict[str, str]
    backend: str
    flags: dict[str, Any]
    probes: dict[str, dict[str, Any]]
    diagnostic: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "ready": self.ready,
            "codex_executable": self.flags["codex_executable"],
            "codex_version": self.codex_version,
            "enabled_features": self.flags["enabled_features"],
            "outside_read_denied": self.probes["outside_workspace_read"]["passed"],
            "platform": self.platform,
            "backend": self.backend,
            "flags": self.flags,
            "probes": self.probes,
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True, slots=True)
class WorkerSandboxPreflightResult:
    """The two permission profiles used by the fixed three-slot protocol."""

    read_only: SandboxPreflightResult
    workspace: SandboxPreflightResult

    @property
    def ready(self) -> bool:
        return self.read_only.ready and self.workspace.ready

    def as_dict(self) -> dict[str, Any]:
        workspace = self.workspace.as_dict()
        read_only = self.read_only.as_dict()
        failed = [
            profile
            for profile, result in (
                (":read-only", self.read_only),
                (":workspace", self.workspace),
            )
            if not result.ready
        ]
        return {
            "schema_version": _SCHEMA_VERSION,
            "ready": self.ready,
            "codex_executable": workspace["codex_executable"],
            "codex_version": workspace["codex_version"],
            "enabled_features": workspace["enabled_features"],
            "backend": workspace["backend"],
            "diagnostic": (
                None
                if self.ready
                else {
                    "code": "worker_profile_preflight_failed",
                    "message": "one or more required worker permission profiles failed",
                    "failed_profiles": failed,
                }
            ),
            "profiles": {
                ":read-only": read_only,
                ":workspace": workspace,
            },
        }


def run_worker_sandbox_preflight(
    *,
    codex_executable: str = "codex",
    enabled_features: Iterable[str] = (),
    timeout_seconds: float = 20.0,
    network_timeout_seconds: float = 1.0,
    system: str | None = None,
) -> WorkerSandboxPreflightResult:
    """Fail closed unless every permission profile used by a worker is sound."""

    common = {
        "codex_executable": codex_executable,
        "enabled_features": tuple(enabled_features),
        "timeout_seconds": timeout_seconds,
        "network_timeout_seconds": network_timeout_seconds,
        "system": system,
    }
    return WorkerSandboxPreflightResult(
        read_only=run_sandbox_preflight(permission_profile=":read-only", **common),
        workspace=run_sandbox_preflight(permission_profile=":workspace", **common),
    )


def run_sandbox_preflight(
    *,
    codex_executable: str = "codex",
    enabled_features: Iterable[str] = (),
    permission_profile: str = ":workspace",
    timeout_seconds: float = 20.0,
    network_timeout_seconds: float = 1.0,
    system: str | None = None,
) -> SandboxPreflightResult:
    """Verify the worker sandbox without starting a model session."""

    if timeout_seconds <= 0 or network_timeout_seconds <= 0:
        raise ValueError("sandbox preflight timeouts must be positive")
    features = tuple(dict.fromkeys(enabled_features))
    unsupported = set(features) - _SUPPORTED_FEATURES
    if unsupported:
        raise ValueError(f"unsupported sandbox preflight feature: {sorted(unsupported)[0]}")
    host_system = system or platform.system()
    executable_path = shutil.which(codex_executable)
    if executable_path is not None:
        executable_path = str(Path(executable_path).resolve())
    if executable_path is None and (
        Path(codex_executable).is_absolute()
        or Path(codex_executable).parent != Path(".")
        or "/" in codex_executable
        or "\\" in codex_executable
    ):
        executable_path = str(Path(codex_executable).resolve())
    frozen_executable = executable_path or codex_executable
    platform_info = {
        "system": host_system,
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }
    backend = _backend(host_system, features)
    flags = {
        "command": "sandbox",
        "host_subcommand": (
            _sandbox_subcommand(host_system)
            if backend != "unsupported" and _uses_host_subcommand(None, host_system)
            else None
        ),
        "full_auto": False,
        "codex_executable": frozen_executable,
        "sandbox_mode": "workspace-write",
        "permission_profile": permission_profile,
        "network_access": False,
        "exclude_tmpdir_env_var": True,
        "exclude_slash_tmp": True,
        "enabled_features": list(features),
        "shell": False,
    }
    if backend == "unsupported":
        return SandboxPreflightResult(
            False,
            None,
            platform_info,
            backend,
            flags,
            _not_run_probes(
                "unsupported host platform", permission_profile=permission_profile
            ),
            {"code": "unsupported_platform", "message": host_system},
        )

    try:
        version = subprocess.run(
            [frozen_executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=min(timeout_seconds, 5.0),
        )
    except FileNotFoundError:
        return SandboxPreflightResult(
            False,
            None,
            platform_info,
            backend,
            flags,
            _not_run_probes(
                "Codex CLI was not found", permission_profile=permission_profile
            ),
            {"code": "codex_not_found", "message": frozen_executable},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return SandboxPreflightResult(
            False,
            None,
            platform_info,
            backend,
            flags,
            _not_run_probes(
                "Codex version check failed", permission_profile=permission_profile
            ),
            {"code": "codex_version_failed", "message": type(error).__name__},
        )
    codex_version = version.stdout.strip() or None if version.returncode == 0 else None
    if codex_version is None:
        message = (version.stderr or version.stdout).strip()[:1000] or f"exit {version.returncode}"
        return SandboxPreflightResult(
            False,
            None,
            platform_info,
            backend,
            flags,
            _not_run_probes(
                "Codex version check failed", permission_profile=permission_profile
            ),
            {"code": "codex_version_failed", "message": message},
        )
    flags["host_subcommand"] = (
        _sandbox_subcommand(host_system)
        if _uses_host_subcommand(codex_version, host_system)
        else None
    )
    flags["full_auto"] = _uses_host_subcommand(codex_version, host_system)

    with tempfile.TemporaryDirectory(
        prefix="agentcongress-sandbox-preflight-", ignore_cleanup_errors=True
    ) as raw_root:
        root = Path(raw_root).resolve()
        workspace = root / "workspace"
        outside = root / "outside"
        workspace.mkdir()
        outside.mkdir()
        (outside / "secret-canary.txt").write_text(
            secrets.token_hex(32), encoding="utf-8"
        )
        (workspace / "seed.txt").write_text("agentcongress-seed", encoding="utf-8")
        probe_script = workspace / "probe.py"
        probe_script.write_text(_PROBE_SOURCE, encoding="utf-8")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        network_host, network_port = listener.getsockname()
        command = build_sandbox_command(
            frozen_executable,
            probe_script,
            workspace,
            outside,
            network_host,
            network_port,
            network_timeout_seconds,
            enabled_features=features,
            system=host_system,
            codex_version=codex_version,
            permission_profile=permission_profile,
        )
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            listener.close()
            return SandboxPreflightResult(
                False,
                codex_version,
                platform_info,
                backend,
                flags,
                _not_run_probes(
                    "sandbox command timed out",
                    permission_profile=permission_profile,
                ),
                {"code": "sandbox_timeout", "message": f"timeout after {error.timeout} seconds"},
            )
        except OSError as error:
            listener.close()
            return SandboxPreflightResult(
                False,
                codex_version,
                platform_info,
                backend,
                flags,
                _not_run_probes(
                    "sandbox command could not start",
                    permission_profile=permission_profile,
                ),
                {"code": "sandbox_spawn_failed", "message": f"{type(error).__name__}: {error}"},
            )
        finally:
            listener.close()

        payload: dict[str, Any] | None = None
        for line in reversed(completed.stdout.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and candidate.get("schema_version") == _SCHEMA_VERSION:
                payload = candidate
                break

        redacted_output = "\n".join(
            part for part in (completed.stderr.strip(), completed.stdout.strip()) if part
        ).replace(str(root), "<preflight-root>")[:2000]
        if completed.returncode != 0 or payload is None:
            code = classify_sandbox_diagnostic(redacted_output)
            return SandboxPreflightResult(
                False,
                codex_version,
                platform_info,
                backend,
                flags,
                _not_run_probes(
                    "sandbox command did not complete",
                    permission_profile=permission_profile,
                ),
                _diagnostic(
                    code,
                    redacted_output or f"sandbox exited {completed.returncode}",
                    legacy_landlock="use_legacy_landlock" in features,
                ),
            )

        raw_probes = payload.get("probes")
        if not isinstance(raw_probes, dict):
            raw_probes = {}
        probes = _not_run_probes(
            "probe result missing", permission_profile=permission_profile
        )
        probes["sandbox_launch"] = _probe("allowed", "passed", "sandbox command completed")
        for name in _PROBE_NAMES[1:]:
            value = raw_probes.get(name)
            if not isinstance(value, dict):
                continue
            expected = (
                "denied"
                if name
                in {
                    "outside_workspace_read",
                    "outside_workspace_write",
                    "network_connect",
                }
                or (name == "workspace_write" and permission_profile == ":read-only")
                else "allowed"
            )
            passed = value.get("passed") is True
            observation = str(value.get("observation", "probe returned no observation"))[:500]
            probes[name] = _probe(expected, "passed" if passed else "failed", observation)

        write_path = workspace / "write.txt"
        if permission_profile == ":workspace":
            try:
                write_persisted = (
                    write_path.is_file()
                    and write_path.read_text(encoding="utf-8", errors="replace")
                    == "agentcongress-write"
                )
            except OSError as error:
                write_persisted = False
                write_observation = (
                    "workspace output is not readable by the host verifier: "
                    f"{type(error).__name__}"
                )
            else:
                write_observation = "workspace write did not persist to the host"
            if not write_persisted:
                probes["workspace_write"] = _probe(
                    "allowed", "failed", write_observation
                )
        else:
            probes["workspace_write"]["expected"] = "denied"
            if write_path.exists():
                probes["workspace_write"] = _probe(
                    "denied", "failed", "read-only workspace write persisted to the host"
                )
        if (outside / "escape.txt").exists():
            probes["outside_workspace_write"] = _probe(
                "denied", "failed", "outside-workspace write persisted to the host"
            )
        ready = all(value["passed"] for value in probes.values())
        diagnostic = None
        if not ready:
            diagnostic = {
                "code": "sandbox_policy_mismatch",
                "message": "one or more sandbox capability probes failed",
            }
        return SandboxPreflightResult(
            ready,
            codex_version,
            platform_info,
            backend,
            flags,
            probes,
            diagnostic,
        )
