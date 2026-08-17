from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from agentcongress import sandbox_preflight
from agentcongress.cli import main


def _payload(*, all_passed: bool = True, workspace_write_expected: str = "allowed") -> str:
    probes = {}
    for name in sandbox_preflight._PROBE_NAMES[1:]:
        expected = "denied" if name in {
            "outside_workspace_read",
            "outside_workspace_write",
            "network_connect",
        } else workspace_write_expected if name == "workspace_write" else "allowed"
        probes[name] = {
            "expected": expected,
            "status": "passed" if all_passed else "failed",
            "passed": all_passed,
            "observation": "mocked",
        }
    return json.dumps({"schema_version": 1, "probes": probes}) + "\n"


def test_build_command_is_shell_free_and_uses_absolute_python(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    script = workspace / "probe.py"
    script.write_text("", encoding="utf-8")

    command = sandbox_preflight.build_sandbox_command(
        "/opt/codex/bin/codex",
        script,
        workspace,
        outside,
        "127.0.0.1",
        54321,
        0.5,
        enabled_features=["use_legacy_landlock"],
        python_executable=Path(sys.executable),
        system="Linux",
        codex_version="codex-cli 0.125.0",
    )

    assert command[:5] == [
        "/opt/codex/bin/codex",
        "sandbox",
        "linux",
        "--enable",
        "use_legacy_landlock",
    ]
    assert "--full-auto" in command
    assert "exec" not in command
    separator = command.index("--")
    assert Path(command[separator + 1]).is_absolute()
    assert command[separator + 1] == str(Path(sys.executable).resolve())
    assert command[separator + 2] == str(script.resolve())


def test_modern_and_windows_sandbox_commands_have_no_host_subcommand(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    script = workspace / "probe.py"
    script.write_text("", encoding="utf-8")

    modern_linux = sandbox_preflight.build_sandbox_command(
        "codex",
        script,
        workspace,
        outside,
        "127.0.0.1",
        1,
        0.5,
        system="Linux",
        codex_version="codex-cli 0.147.0",
    )
    windows = sandbox_preflight.build_sandbox_command(
        "codex.exe",
        script,
        workspace,
        outside,
        "127.0.0.1",
        1,
        0.5,
        system="Windows",
        codex_version="codex-cli 0.146.0",
    )

    assert modern_linux[:2] == ["codex", "sandbox"]
    assert modern_linux[2] != "linux"
    assert windows[:2] == ["codex.exe", "sandbox"]
    assert windows[2] != "windows"


def test_read_only_command_tells_probe_to_expect_workspace_write_denial(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    script = workspace / "probe.py"
    script.write_text("", encoding="utf-8")

    command = sandbox_preflight.build_sandbox_command(
        "codex",
        script,
        workspace,
        outside,
        "127.0.0.1",
        1,
        0.5,
        permission_profile=":read-only",
        system="Linux",
        codex_version="codex-cli 0.147.0",
    )

    assert command[-1] == "denied"
    assert command[command.index("-P") + 1] == ":read-only"


def test_expected_denials_are_ready_and_freeze_invocation(
    monkeypatch, tmp_path: Path
) -> None:
    codex = tmp_path / "codex"
    monkeypatch.setattr(sandbox_preflight.shutil, "which", lambda _: str(codex))
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((list(command), kwargs))
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "codex-cli 0.125.0\n", "")
        workspace = Path(command[command.index("--") + 3])
        (workspace / "write.txt").write_text("agentcongress-write", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, _payload(), "")

    monkeypatch.setattr(sandbox_preflight.subprocess, "run", fake_run)
    result = sandbox_preflight.run_sandbox_preflight(
        codex_executable="codex-local",
        enabled_features=["use_legacy_landlock"],
        system="Linux",
    )
    body = result.as_dict()

    assert result.ready
    assert body["codex_executable"] == str(codex)
    assert body["codex_version"] == "codex-cli 0.125.0"
    assert body["enabled_features"] == ["use_legacy_landlock"]
    assert body["backend"] == "linux-legacy-landlock-diagnostic"
    assert body["outside_read_denied"]
    assert body["probes"]["outside_workspace_write"]["passed"]
    assert body["probes"]["network_connect"]["passed"]
    assert all(kwargs["stdin"] is subprocess.DEVNULL for _, kwargs in calls)


def test_worker_preflight_requires_both_permission_profiles(monkeypatch) -> None:
    calls: list[str] = []

    def fake_preflight(**kwargs):
        profile = kwargs["permission_profile"]
        calls.append(profile)
        ready = profile == ":workspace"
        probes = sandbox_preflight._not_run_probes("mocked")
        return sandbox_preflight.SandboxPreflightResult(
            ready,
            "codex-cli test",
            {"system": "Linux", "release": "x", "machine": "x", "python": "3.12"},
            "linux-bwrap-permission-profile",
            {
                "codex_executable": "/opt/codex",
                "enabled_features": [],
                "permission_profile": profile,
            },
            probes,
            None if ready else {"code": "mock", "message": "mocked"},
        )

    monkeypatch.setattr(sandbox_preflight, "run_sandbox_preflight", fake_preflight)
    result = sandbox_preflight.run_worker_sandbox_preflight()

    assert calls == [":read-only", ":workspace"]
    assert not result.ready
    assert result.as_dict()["diagnostic"]["failed_profiles"] == [":read-only"]


def test_outside_write_host_evidence_fails_closed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sandbox_preflight.shutil, "which", lambda _: "codex")

    def fake_run(command, **kwargs):
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "codex-cli 0.146.0\n", "")
        workspace = Path(command[command.index("--") + 3])
        outside = Path(command[command.index("--") + 4])
        (workspace / "write.txt").write_text("agentcongress-write", encoding="utf-8")
        (outside / "escape.txt").write_text("sandbox-escaped", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, _payload(), "")

    monkeypatch.setattr(sandbox_preflight.subprocess, "run", fake_run)
    result = sandbox_preflight.run_sandbox_preflight(system="Linux")

    assert not result.ready
    assert not result.probes["outside_workspace_write"]["passed"]
    assert result.diagnostic == {
        "code": "sandbox_policy_mismatch",
        "message": "one or more sandbox capability probes failed",
    }


def test_readable_outside_canary_fails_closed_without_leaking_secret(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sandbox_preflight.shutil, "which", lambda _: "codex")

    def fake_run(command, **kwargs):
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "codex-cli 0.125.0\n", "")
        workspace = Path(command[command.index("--") + 3])
        (workspace / "write.txt").write_text("agentcongress-write", encoding="utf-8")
        payload = json.loads(_payload())
        payload["probes"]["outside_workspace_read"] = {
            "expected": "denied",
            "status": "failed",
            "passed": False,
            "observation": "read unexpectedly succeeded",
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(sandbox_preflight.subprocess, "run", fake_run)
    result = sandbox_preflight.run_sandbox_preflight(
        system="Linux", enabled_features=["use_legacy_landlock"]
    )
    body = result.as_dict()

    assert not result.ready
    assert body["outside_read_denied"] is False
    assert "secret-canary" not in json.dumps(body)


def test_bwrap_and_windows_helper_failures_are_precisely_classified() -> None:
    assert sandbox_preflight.classify_sandbox_diagnostic(
        "bwrap: Failed to make / slave: Permission denied"
    ) == "bwrap_mount_namespace_denied"
    assert sandbox_preflight.classify_sandbox_diagnostic(
        "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"
    ) == "bwrap_network_namespace_denied"
    assert sandbox_preflight.classify_sandbox_diagnostic(
        "windows sandbox: orchestrator_helper_launch_failed: "
        "helper=codex-windows-sandbox-setup.exe, error=Access is denied. (os error 5)"
    ) == "windows_helper_access_denied"


def test_bwrap_failure_is_non_ready_with_explicit_landlock_remediation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sandbox_preflight.shutil, "which", lambda _: "codex")

    def fake_run(command, **kwargs):
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "codex-cli 0.146.0\n", "")
        return subprocess.CompletedProcess(
            command, 1, "", "bwrap: Failed to make / slave: Permission denied"
        )

    monkeypatch.setattr(sandbox_preflight.subprocess, "run", fake_run)
    result = sandbox_preflight.run_sandbox_preflight(system="Linux")

    assert not result.ready
    assert result.diagnostic is not None
    assert result.diagnostic["code"] == "bwrap_mount_namespace_denied"
    assert "Legacy Landlock has full host read access" in result.diagnostic["remediation"]


def test_cli_prints_json_and_returns_nonzero_when_not_ready(
    monkeypatch, capsys
) -> None:
    result = sandbox_preflight.SandboxPreflightResult(
        False,
        "codex-cli 0.146.0",
        {"system": "Windows", "release": "x", "machine": "AMD64", "python": "3.12"},
        "windows-restricted-token",
        {
            "command": "sandbox",
            "host_subcommand": "windows",
            "full_auto": True,
            "codex_executable": "C:/codex.exe",
            "sandbox_mode": "workspace-write",
            "network_access": False,
            "exclude_tmpdir_env_var": True,
            "exclude_slash_tmp": True,
            "enabled_features": [],
            "shell": False,
        },
        sandbox_preflight._not_run_probes("mock failure"),
        {"code": "windows_helper_access_denied", "message": "mocked"},
    )
    monkeypatch.setattr(
        "agentcongress.cli.run_sandbox_preflight", lambda **_: result
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agentcongress",
            "sandbox-preflight",
            "--codex-executable",
            "C:/codex.exe",
            "--codex-feature",
            "use_legacy_landlock",
        ],
    )

    assert main() == 2
    body = json.loads(capsys.readouterr().out)
    assert body["ready"] is False
    assert body["diagnostic"]["code"] == "windows_helper_access_denied"


def test_cli_can_probe_both_worker_profiles(monkeypatch, capsys) -> None:
    calls: list[dict[str, object]] = []

    class _Combined:
        ready = True

        def as_dict(self) -> dict[str, object]:
            return {"schema_version": 1, "ready": True, "profiles": {}}

    monkeypatch.setattr(
        "agentcongress.cli.run_worker_sandbox_preflight",
        lambda **kwargs: calls.append(kwargs) or _Combined(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agentcongress",
            "sandbox-preflight",
            "--all-worker-profiles",
            "--codex-executable",
            "C:/codex.exe",
        ],
    )

    assert main() == 0
    assert calls == [
        {
            "codex_executable": "C:/codex.exe",
            "enabled_features": [],
            "timeout_seconds": 20.0,
            "network_timeout_seconds": 1.0,
        }
    ]
    assert json.loads(capsys.readouterr().out)["ready"] is True
