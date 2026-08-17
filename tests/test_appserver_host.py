from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pytest
import agentcongress.appserver_host as appserver_host

from agentcongress.appserver_client import PERMISSION_PROFILE, SlotConfig
from agentcongress.appserver_host import (
    DEFAULT_APP_SERVER_ARGV,
    AppServerHostError,
    AppServerProcessOwner,
    AppServerProcessSpec,
    HarborAppServerAgent,
    HarborTrialPlan,
    HostJail,
    build_subprocess_environment,
    validate_codex_home,
    validate_empty_host_jail,
    validate_harbor_discovery,
)


def _private_dir(path: Path) -> Path:
    path.mkdir()
    path.chmod(0o700)
    return path


def _executable(path: Path) -> Path:
    path.write_bytes(b"fake executable")
    path.chmod(0o700)
    return path.resolve()


class FakeWriter:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class LineReader:
    def __init__(self, lines: list[bytes] | None = None, *, block: bool = False) -> None:
        self.lines = list(lines or [])
        self.block = block
        self.cancelled = False

    async def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        if self.block:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return b""


class ByteReader:
    def __init__(self, data: bytes = b"") -> None:
        self.data = data

    async def read(self, size: int) -> bytes:
        if not self.data:
            return b""
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


class FakeProcess:
    def __init__(
        self,
        *,
        stdout: LineReader | None = None,
        stderr: ByteReader | None = None,
        resist_terminate: bool = False,
    ) -> None:
        self.stdin = FakeWriter()
        self.stdout = stdout or LineReader([b'{"id":0,"result":{"userAgent":"fake"}}\n'])
        self.stderr = stderr or ByteReader()
        self.returncode: int | None = None
        self.resist_terminate = resist_terminate
        self.terminated = 0
        self.killed = 0
        self.waited = 0
        self._finished = asyncio.Event()

    def terminate(self) -> None:
        self.terminated += 1
        if not self.resist_terminate:
            self.returncode = -15
            self._finished.set()

    def kill(self) -> None:
        self.killed += 1
        self.returncode = -9
        self._finished.set()

    async def wait(self) -> int:
        self.waited += 1
        await self._finished.wait()
        assert self.returncode is not None
        return self.returncode


@dataclass
class ExecResult:
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0


class FakeEnvironment:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.secret = "environment-secret"

    async def exec(self, command: str, *, cwd: str, timeout_sec: int) -> ExecResult:
        self.calls.append((command, cwd, timeout_sec))
        return ExecResult()


def _owner(
    tmp_path: Path,
    factory: Any,
    *,
    host_environment: Mapping[str, str] | None = None,
    grace: float = 0.05,
) -> tuple[AppServerProcessOwner, Path, Path]:
    executable = _executable(tmp_path / ("codex.exe" if os.name == "nt" else "codex"))
    codex_home = _private_dir(tmp_path / "codex-home")
    jail_parent = _private_dir(tmp_path / "jails")
    spec = AppServerProcessSpec(
        str(executable), terminate_grace_seconds=grace, stderr_cap_bytes=32
    )
    return (
        AppServerProcessOwner(
            spec,
            codex_home=codex_home,
            host_jail_parent=jail_parent,
            host_environment=host_environment or {"PATH": "safe-path"},
            process_factory=factory,
        ),
        codex_home,
        jail_parent,
    )


def test_process_spec_freezes_stdio_strict_config_and_absolute_executable(tmp_path: Path) -> None:
    executable = _executable(tmp_path / ("codex.exe" if os.name == "nt" else "codex"))
    assert AppServerProcessSpec(str(executable)).argv == DEFAULT_APP_SERVER_ARGV
    remote = AppServerProcessSpec(
        str(executable), code_mode_host_url="ws://127.0.0.1:50333"
    )
    assert remote.process_argv == (
        *DEFAULT_APP_SERVER_ARGV,
        "--code-mode-host",
        "ws://127.0.0.1:50333",
    )
    for invalid in (
        "ws://0.0.0.0:50333",
        "ws://localhost:50333",
        "wss://127.0.0.1:50333",
        "ws://127.0.0.1:0",
        "ws://127.0.0.1:65536",
        "ws://127.0.0.1:50333/path",
    ):
        with pytest.raises(ValueError, match="loopback"):
            AppServerProcessSpec(str(executable), code_mode_host_url=invalid)
    with pytest.raises(ValueError, match="absolute"):
        AppServerProcessSpec("codex")
    with pytest.raises(ValueError, match="exactly equal"):
        AppServerProcessSpec(str(executable), ("app-server", "--stdio"))
    with pytest.raises(ValueError, match="exactly equal"):
        AppServerProcessSpec(
            str(executable),
            ("app-server", "--stdio", "--strict-config", "--listen", "ws://0.0.0.0:9"),
        )
    for suffix in (
        ("--listen=ws://127.0.0.1:9",),
        ("--code-mode-host=ws://127.0.0.1:9",),
        ("-c", "features.web_search=true"),
        ("--enable", "web_search"),
        ("daemon",),
    ):
        with pytest.raises(ValueError, match="exactly equal"):
            AppServerProcessSpec(str(executable), DEFAULT_APP_SERVER_ARGV + suffix)


def test_environment_is_allowlist_only_with_exact_private_codex_home(tmp_path: Path) -> None:
    home = _private_dir(tmp_path / "codex-home")
    result = build_subprocess_environment(
        {
            "PATH": "safe",
            "LANG": "C.UTF-8",
            "OPENAI_API_KEY": "SECRET-API-KEY",
            "AGENT_EXTRA_ENV": "SECRET-AGENT-ENV",
            "CODEX_HOME": "attacker-home",
        },
        home,
    )
    assert result == {"PATH": "safe", "LANG": "C.UTF-8", "CODEX_HOME": str(home.resolve())}
    assert "SECRET" not in repr(result)


def test_codex_home_and_host_jail_reject_links_nonempty_and_insecure_mode(
    tmp_path: Path,
) -> None:
    home = _private_dir(tmp_path / "home")
    assert validate_codex_home(home) == home.resolve()
    nonempty = _private_dir(tmp_path / "nonempty")
    (nonempty / "AGENTS.md").write_text("host instructions", encoding="utf-8")
    with pytest.raises(AppServerHostError, match="empty"):
        validate_empty_host_jail(nonempty)
    link = tmp_path / "home-link"
    try:
        link.symlink_to(home, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(AppServerHostError, match="secure directory"):
        validate_codex_home(link)
    if os.name == "posix":
        home.chmod(0o755)
        with pytest.raises(AppServerHostError, match="private"):
            validate_codex_home(home)


@pytest.mark.parametrize(
    "relative",
    ["AGENTS.md", "skills/demo/SKILL.md", "plugins/plugin.json", "mcp.json", ".mcp/server"],
)
def test_codex_home_rejects_agent_discovery_but_keeps_auth_and_config(
    tmp_path: Path, relative: str
) -> None:
    clean = _private_dir(tmp_path / "clean-home")
    (clean / "auth.json").write_text("SECRET-AUTH", encoding="utf-8")
    (clean / "config.toml").write_text("default_permissions='sealed'", encoding="utf-8")
    assert validate_codex_home(clean) == clean.resolve()

    hostile = _private_dir(tmp_path / "hostile-home")
    target = hostile / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix:
        target.write_text("discovery", encoding="utf-8")
    else:
        target.mkdir()
    with pytest.raises(AppServerHostError) as raised:
        validate_codex_home(hostile)
    assert raised.value.code == "discovery_material_present"


def test_host_jail_parent_chain_rejects_discovery_material_to_explicit_boundary(
    tmp_path: Path,
) -> None:
    executable = _executable(tmp_path / ("codex.exe" if os.name == "nt" else "codex"))
    codex_home = _private_dir(tmp_path / "codex-home")
    boundary = _private_dir(tmp_path / "boundary")
    nested = boundary / "nested"
    nested.mkdir()
    nested.chmod(0o700)
    (boundary / "AGENTS.md").write_text("host instruction", encoding="utf-8")
    with pytest.raises(AppServerHostError) as raised:
        AppServerProcessOwner(
            AppServerProcessSpec(str(executable)),
            codex_home=codex_home,
            host_jail_parent=nested,
            host_jail_trust_boundary=boundary,
        )
    assert raised.value.code == "discovery_material_present"


def test_host_jail_is_unique_empty_private_and_removed(tmp_path: Path) -> None:
    parent = _private_dir(tmp_path / "jails")
    first = HostJail.create(parent, "trial-1")
    second = HostJail.create(parent, "trial-1")
    try:
        assert first.path != second.path
        assert list(first.path.iterdir()) == []
        assert validate_empty_host_jail(first.path) == first.path
        if os.name == "posix":
            assert first.path.stat().st_mode & 0o777 == 0o700
    finally:
        first_path, second_path = first.path, second.path
        first.cleanup()
        second.cleanup()
    assert not first_path.exists()
    assert not second_path.exists()


def test_process_spawn_is_exec_not_shell_allowlisted_and_cleanup_is_bounded(tmp_path: Path) -> None:
    process = FakeProcess(
        stderr=ByteReader(b"SENTINEL-SECRET" * 10), resist_terminate=True
    )
    captured: dict[str, Any] = {}

    async def factory(*argv: str, **kwargs: Any) -> FakeProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return process

    async def scenario() -> None:
        owner, codex_home, jail_parent = _owner(
            tmp_path,
            factory,
            host_environment={"PATH": "safe", "OPENAI_API_KEY": "SENTINEL-SECRET"},
        )
        trial = owner.open_trial(FakeEnvironment(), task_root="/task", trial_id="trial-a")
        async with trial:
            jail = trial.host_jail
            assert jail.exists()
            assert trial.client is not None
        assert not jail.exists()
        assert process.terminated == 1
        assert process.killed == 1
        assert process.stdin.closed is True
        assert trial.stderr_summary.observed_bytes == 32
        assert trial.stderr_summary.total_bytes == len(b"SENTINEL-SECRET" * 10)
        assert trial.stderr_summary.truncated is True
        assert trial.stderr_summary.sha256 == hashlib.sha256(
            b"SENTINEL-SECRET" * 10
        ).hexdigest()
        assert "SENTINEL-SECRET" not in repr(trial.stderr_summary)
        assert captured["argv"] == (
            owner.spec.executable,
            "app-server",
            "--stdio",
            "--strict-config",
        )
        kwargs = captured["kwargs"]
        assert "shell" not in kwargs
        assert kwargs["cwd"].startswith(str(jail_parent))
        assert kwargs["env"] == {"PATH": "safe", "CODEX_HOME": str(codex_home)}

    asyncio.run(scenario())


def test_startup_protocol_exception_terminates_process_and_removes_jail(tmp_path: Path) -> None:
    process = FakeProcess(stdout=LineReader([b"not-json\n"]))

    async def factory(*argv: str, **kwargs: Any) -> FakeProcess:
        return process

    async def scenario() -> None:
        owner, _, jail_parent = _owner(tmp_path, factory)
        trial = owner.open_trial(FakeEnvironment(), task_root="/task", trial_id="bad-start")
        with pytest.raises(AppServerHostError) as raised:
            async with trial:
                raise AssertionError("unreachable")
        assert raised.value.code == "app_server_start_failed"
        assert process.terminated == 1
        assert list(jail_parent.iterdir()) == []

    asyncio.run(scenario())


def test_cancellation_during_initialize_cleans_process_and_jail(tmp_path: Path) -> None:
    reader = LineReader(block=True)
    process = FakeProcess(stdout=reader)

    async def factory(*argv: str, **kwargs: Any) -> FakeProcess:
        return process

    async def scenario() -> None:
        owner, _, jail_parent = _owner(tmp_path, factory)
        trial = owner.open_trial(FakeEnvironment(), task_root="/task", trial_id="cancelled")

        async def enter() -> None:
            await trial.__aenter__()

        task = asyncio.create_task(enter())
        for _ in range(50):
            if process.stdin.data:
                break
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert reader.cancelled is True
        assert process.terminated == 1
        assert list(jail_parent.iterdir()) == []

    asyncio.run(scenario())


def test_external_timeout_during_initialize_cleans_process_and_jail(tmp_path: Path) -> None:
    process = FakeProcess(stdout=LineReader(block=True))

    async def factory(*argv: str, **kwargs: Any) -> FakeProcess:
        return process

    async def scenario() -> None:
        owner, _, jail_parent = _owner(tmp_path, factory)
        trial = owner.open_trial(FakeEnvironment(), task_root="/task", trial_id="timed-out")
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await trial.__aenter__()
        assert process.terminated == 1
        assert list(jail_parent.iterdir()) == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "source",
    [
        {"extra_env": {"OPENAI_API_KEY": "secret"}},
        {"mcp": ["server"]},
        {"skills": ["host-skill"]},
        {"setup": {"mcp_servers": {"x": {}}}},
        {"MCPServers": {"x": {}}},
        {"extraEnv": {"TOKEN": "secret"}},
        {"auth": {"token": "secret"}},
        {"uploads": ["/host/file"]},
    ],
)
def test_harbor_discovery_rejects_injection_and_authority(source: Mapping[str, Any]) -> None:
    with pytest.raises(AppServerHostError) as raised:
        validate_harbor_discovery(source)
    assert raised.value.code == "unsafe_harbor_configuration"
    assert "secret" not in raised.value.message


def test_harbor_discovery_accepts_only_explicitly_empty_optional_injections() -> None:
    validate_harbor_discovery(
        {"extra_env": {}, "mcp": [], "skills": (), "auth": None, "uploads": []},
        None,
    )


def _slot(position: int) -> SlotConfig:
    return SlotConfig(
        position=position,
        slot_id=f"slot-{position}",
        actor="agent",
        model="model",
        prompt="prompt",
        output_schema={"type": "object", "additionalProperties": False},
    )


def test_harbor_plan_requires_exact_three_slots() -> None:
    plan = HarborTrialPlan("trial", "/task", (_slot(1), _slot(2), _slot(3)))
    assert [slot.max_seconds for slot in plan.slots] == [240, 120, 840]
    with pytest.raises(ValueError, match="fixed slots"):
        HarborTrialPlan("trial", "/task", (_slot(1), _slot(3), _slot(2)))


def test_harbor_agent_wraps_environment_as_exec_only_and_checks_before_opening(
    tmp_path: Path,
) -> None:
    opened = False

    async def factory(*argv: str, **kwargs: Any) -> FakeProcess:
        nonlocal opened
        opened = True
        return FakeProcess()

    async def scenario() -> None:
        owner, _, _ = _owner(tmp_path, factory)
        agent = HarborAppServerAgent(owner)
        plan = HarborTrialPlan("trial", "/task", (_slot(1), _slot(2), _slot(3)))
        with pytest.raises(AppServerHostError) as raised:
            await agent.run(
                FakeEnvironment(), plan, discovered_setup={"extra_env": {"TOKEN": "secret"}}
            )
        assert raised.value.code == "unsafe_harbor_configuration"
        assert opened is False

    asyncio.run(scenario())


def test_harbor_agent_keeps_real_tools_inventory_as_a_formal_blocker(tmp_path: Path) -> None:
    opened = False

    async def factory(*argv: str, **kwargs: Any) -> FakeProcess:
        nonlocal opened
        opened = True
        return FakeProcess()

    async def scenario() -> None:
        owner, _, _ = _owner(tmp_path, factory)
        plan = HarborTrialPlan("trial", "/task", (_slot(1), _slot(2), _slot(3)))
        with pytest.raises(AppServerHostError) as raised:
            await HarborAppServerAgent(owner).run(FakeEnvironment(), plan)
        assert raised.value.code == "formal_evidence_loader_missing"
        assert opened is False

    asyncio.run(scenario())


def test_legacy_mutable_formal_boolean_cannot_bypass_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = False

    async def factory(*argv: str, **kwargs: Any) -> FakeProcess:
        nonlocal opened
        opened = True
        return FakeProcess()

    async def scenario() -> None:
        owner, _, _ = _owner(tmp_path, factory)
        plan = HarborTrialPlan("trial", "/task", (_slot(1), _slot(2), _slot(3)))
        monkeypatch.setattr(
            appserver_host, "FORMAL_CODEX_TOOL_INVENTORY_VERIFIED", True, raising=False
        )
        with pytest.raises(AppServerHostError) as raised:
            await HarborAppServerAgent(owner).run(FakeEnvironment(), plan)
        assert raised.value.code == "formal_evidence_loader_missing"
        assert opened is False

    asyncio.run(scenario())
