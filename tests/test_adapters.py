from pathlib import Path

import asyncio
import json

import pytest

from agentcongress.adapters import CodexWorkerAdapter, detect_codex_infrastructure_failure
from agentcongress.errors import WorkerInfrastructureError


def test_codex_worker_is_scoped_to_its_worktree() -> None:
    command = CodexWorkerAdapter(model="gpt-5.6-luna", reasoning_effort="high", enabled_features=("alpha", "beta")).command("implement task", Path("C:/task"), Path("C:/schema.json"))
    assert command[:8] == ["codex", "exec", "--enable", "alpha", "--enable", "beta", "--ignore-user-config", "--ephemeral"]
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert command[command.index("--config") + 1] == 'model_reasoning_effort="high"'
    assert 'web_search="disabled"' in command
    assert command[-1] == "implement task"


def test_codex_worker_rejects_uncontained_sandbox_modes() -> None:
    with pytest.raises(ValueError, match="read-only or workspace-write"):
        CodexWorkerAdapter(sandbox="danger-full-access")


def test_codex_worker_uses_only_frozen_builtin_permission_profiles() -> None:
    command = CodexWorkerAdapter(
        sandbox=None,
        permission_profile=":read-only",
    ).command("inspect", Path("C:/task"), Path("C:/schema.json"))
    assert "--sandbox" not in command
    assert 'default_permissions=":read-only"' in command

    with pytest.raises(ValueError, match="cannot combine"):
        CodexWorkerAdapter(sandbox="read-only", permission_profile=":read-only")
    with pytest.raises(ValueError, match="built-in"):
        CodexWorkerAdapter(sandbox=None, permission_profile="my-profile")
    with pytest.raises(ValueError, match="legacy Landlock"):
        CodexWorkerAdapter(
            sandbox=None,
            permission_profile=":workspace",
            enabled_features=("use_legacy_landlock",),
        )


def test_codex_worker_closes_standard_input(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    class _Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if getattr(self, "sent", False):
                raise StopAsyncIteration
            self.sent = True
            return (json.dumps({"type": "turn.completed"}) + "\n").encode()

    class _EmptyStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _Process:
        stdout = _Stream()
        stderr = _EmptyStream()
        pid = 123

        async def wait(self):
            return 0

    async def fake_spawn(*command, **kwargs):
        captured.update(kwargs)
        return _Process()

    monkeypatch.setattr("agentcongress.adapters.asyncio.create_subprocess_exec", fake_spawn)

    async def collect():
        return [event async for event in CodexWorkerAdapter().run_task("test", tmp_path, tmp_path / "schema.json")]

    assert asyncio.run(collect())[0].type == "codex.event"
    assert captured["stdin"] is asyncio.subprocess.DEVNULL


def test_codex_worker_timeout_covers_process_exit_and_kills_it(monkeypatch, tmp_path: Path) -> None:
    class _EmptyStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _Process:
        stdout = _EmptyStream()
        stderr = _EmptyStream()
        pid = 123
        returncode = None
        killed = False

        async def wait(self):
            if self.killed:
                self.returncode = -1
                return -1
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True

    process = _Process()

    async def fake_spawn(*command, **kwargs):
        return process

    async def fake_stop(self, child):
        child.kill()
        await child.wait()

    monkeypatch.setattr("agentcongress.adapters.asyncio.create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(CodexWorkerAdapter, "_stop_process_tree", fake_stop)

    async def collect():
        return [event async for event in CodexWorkerAdapter(timeout_seconds=0.01).run_task("test", tmp_path, tmp_path / "schema.json")]

    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(collect())
    assert process.killed


def test_codex_worker_drains_stderr_while_stdout_is_active(monkeypatch, tmp_path: Path) -> None:
    class _Stream:
        def __init__(self, chunks):
            self.chunks = iter(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.chunks)
            except StopIteration:
                raise StopAsyncIteration

    class _Process:
        stdout = _Stream([])
        stderr = _Stream([b"diagnostic\n"] * 100)
        returncode = 1

        async def wait(self):
            return 1

    async def fake_spawn(*command, **kwargs):
        return _Process()

    monkeypatch.setattr("agentcongress.adapters.asyncio.create_subprocess_exec", fake_spawn)

    async def collect():
        return [event async for event in CodexWorkerAdapter().run_task("test", tmp_path, tmp_path / "schema.json")]

    with pytest.raises(RuntimeError, match="diagnostic"):
        asyncio.run(collect())


@pytest.mark.parametrize(
    "diagnostic,code",
    [
        ("bwrap: No permissions to create new namespace, likely because the kernel does not allow non-privileged user namespaces.", "bwrap_userns_unavailable"),
        ("windows sandbox: orchestrator_helper_launch_failed: helper=codex-windows-sandbox-setup.exe, error=拒绝访问。 (os error 5)", "windows_helper_access_denied"),
    ],
)
def test_codex_sandbox_bootstrap_failure_uses_trusted_command_event(diagnostic: str, code: str) -> None:
    payload = {"type": "item.completed", "item": {"type": "command_execution", "status": "failed", "exit_code": 1, "aggregated_output": diagnostic}}
    assert detect_codex_infrastructure_failure(payload)[0] == code
    echoed = {"type": "item.completed", "item": {"type": "agent_message", "text": diagnostic}}
    assert detect_codex_infrastructure_failure(echoed) is None


def test_codex_worker_stops_after_sandbox_bootstrap_failure(monkeypatch, tmp_path: Path) -> None:
    payload = {"type": "item.completed", "item": {"type": "command_execution", "status": "failed", "exit_code": 1, "aggregated_output": "bwrap: No permissions to create new namespace"}}

    class _Stream:
        def __init__(self, chunks):
            self.chunks = iter(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.chunks)
            except StopIteration:
                raise StopAsyncIteration

    class _Process:
        stdout = _Stream([(json.dumps(payload) + "\n").encode()])
        stderr = _Stream([])
        pid = 123
        returncode = None
        stopped = False

    process = _Process()

    async def fake_spawn(*command, **kwargs):
        return process

    async def fake_stop(self, child):
        child.stopped = True
        child.returncode = -1

    monkeypatch.setattr("agentcongress.adapters.asyncio.create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(CodexWorkerAdapter, "_stop_process_tree", fake_stop)

    async def collect():
        return [event async for event in CodexWorkerAdapter().run_task("test", tmp_path, tmp_path / "schema.json")]

    with pytest.raises(WorkerInfrastructureError) as raised:
        asyncio.run(collect())
    assert raised.value.code == "bwrap_userns_unavailable"
    assert process.stopped
