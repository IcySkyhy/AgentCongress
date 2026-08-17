from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Any

import pytest

from agentcongress.ssh_task_environment import (
    ExecResult,
    SshDockerTaskEnvironment,
    SshTaskEnvironmentConfig,
)


def _file(path: Path, data: bytes = b"x", *, executable: bool = False) -> Path:
    path.write_bytes(data)
    path.chmod(0o700 if executable else 0o600)
    return path.resolve()


def _config(tmp_path: Path, **overrides: Any) -> SshTaskEnvironmentConfig:
    values: dict[str, Any] = {
        "ssh_executable": str(_file(tmp_path / "ssh", executable=True)),
        "port": 2222,
        "private_key": str(_file(tmp_path / "id_key")),
        "known_hosts": str(_file(tmp_path / "known_hosts")),
        "container_name": "task-container_1",
    }
    values.update(overrides)
    return SshTaskEnvironmentConfig(**values)


class ByteReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.read_bytes = 0

    async def read(self, size: int) -> bytes:
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        self.read_bytes += len(chunk)
        return chunk


class FakeProcess:
    def __init__(
        self,
        *,
        stdout: list[bytes] | None = None,
        stderr: list[bytes] | None = None,
        return_code: int = 0,
        hang: bool = False,
        resist_terminate: bool = False,
    ) -> None:
        self.stdout = ByteReader(stdout or [])
        self.stderr = ByteReader(stderr or [])
        self.returncode: int | None = None if hang else return_code
        self._return_code = return_code
        self._done = asyncio.Event()
        if not hang:
            self._done.set()
        self.resist_terminate = resist_terminate
        self.terminated = 0
        self.killed = 0
        self.waited = 0

    def terminate(self) -> None:
        self.terminated += 1
        if not self.resist_terminate:
            self.returncode = -15
            self._done.set()

    def kill(self) -> None:
        self.killed += 1
        self.returncode = -9
        self._done.set()

    async def wait(self) -> int:
        self.waited += 1
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode


def test_exec_uses_exact_ssh_argv_no_shell_and_fixed_docker_command(tmp_path: Path) -> None:
    process = FakeProcess(stdout=[b"ok"], stderr=[b"warn"], return_code=7)
    captured: dict[str, Any] = {}

    async def factory(*argv: str, **kwargs: Any) -> FakeProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return process

    async def scenario() -> None:
        config = _config(tmp_path)
        environment = SshDockerTaskEnvironment(config, process_factory=factory)
        result = await environment.exec(
            "python -c 'print(1)'", cwd="/task/space dir", timeout_sec=17
        )
        assert result == ExecResult("ok", "warn", 7)
        assert captured["argv"][:-2] == (
            config.ssh_executable,
            "-F",
            "none",
            "-T",
            "-p",
            "2222",
            "-i",
            config.private_key,
            "-o",
            f"UserKnownHostsFile={config.known_hosts}",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ForwardX11=no",
        )
        assert captured["argv"][-2] == "stage2@127.0.0.1"
        assert shlex.split(captured["argv"][-1]) == [
            "sudo",
            "docker",
            "exec",
            "-w",
            "/task/space dir",
            "task-container_1",
            "timeout",
            "--signal=KILL",
            "17",
            "bash",
            "-lc",
            "python -c 'print(1)'",
        ]
        kwargs = captured["kwargs"]
        assert "shell" not in kwargs
        assert "env" not in kwargs
        assert kwargs["stdin"] is asyncio.subprocess.DEVNULL
        assert kwargs["stdout"] is asyncio.subprocess.PIPE
        assert kwargs["stderr"] is asyncio.subprocess.PIPE

    asyncio.run(scenario())


def test_stdout_and_stderr_are_independently_capped_but_fully_drained(tmp_path: Path) -> None:
    stdout = [b"a" * 40_000, b"b" * 40_000]
    stderr = [b"c" * 50_000, b"d" * 50_000]
    process = FakeProcess(stdout=stdout, stderr=stderr)

    async def factory(*argv: str, **kwargs: Any) -> FakeProcess:
        return process

    async def scenario() -> None:
        result = await SshDockerTaskEnvironment(
            _config(tmp_path), process_factory=factory
        ).exec("true", cwd="/task", timeout_sec=5)
        assert len(result.stdout.encode()) == 65_536
        assert len(result.stderr.encode()) == 65_536
        assert result.stdout == "a" * 40_000 + "b" * 25_536
        assert result.stderr == "c" * 50_000 + "d" * 15_536
        assert process.stdout.read_bytes == 80_000
        assert process.stderr.read_bytes == 100_000

    asyncio.run(scenario())


def test_invalid_utf8_replacement_still_respects_encoded_output_cap(tmp_path: Path) -> None:
    process = FakeProcess(stdout=[b"\xff" * 70_000])

    async def factory(*argv: str, **kwargs: Any) -> FakeProcess:
        return process

    async def scenario() -> None:
        result = await SshDockerTaskEnvironment(
            _config(tmp_path), process_factory=factory
        ).exec("true", cwd="/task", timeout_sec=5)
        assert len(result.stdout.encode("utf-8")) <= 65_536
        assert process.stdout.read_bytes == 70_000

    asyncio.run(scenario())


def test_outer_timeout_terminates_then_kills_and_returns_fixed_timeout(tmp_path: Path) -> None:
    process = FakeProcess(
        stdout=[b"secret stdout"],
        stderr=[b"secret stderr"],
        hang=True,
        resist_terminate=True,
    )

    async def factory(*argv: str, **kwargs: Any) -> FakeProcess:
        return process

    async def scenario() -> None:
        config = _config(tmp_path, termination_grace_seconds=0.01)
        result = await SshDockerTaskEnvironment(config, process_factory=factory).exec(
            "sleep 99", cwd="/task", timeout_sec=1
        )
        assert result == ExecResult("", "task execution timed out", 124)
        assert process.terminated == 1
        assert process.killed == 1
        assert process.returncode == -9
        assert process.stdout.read_bytes == len(b"secret stdout")
        assert process.stderr.read_bytes == len(b"secret stderr")

    asyncio.run(scenario())


def test_spawn_exception_is_fixed_and_does_not_reflect_secret(tmp_path: Path) -> None:
    async def factory(*argv: str, **kwargs: Any) -> FakeProcess:
        raise OSError("HOST-SENTINEL-SECRET")

    async def scenario() -> None:
        environment = SshDockerTaskEnvironment(_config(tmp_path), process_factory=factory)
        with pytest.raises(RuntimeError) as raised:
            await environment.exec("true", cwd="/task", timeout_sec=1)
        assert str(raised.value) == "SSH task environment operation failed"
        assert raised.value.__cause__ is None
        assert "HOST-SENTINEL-SECRET" not in str(raised.value)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"host": "localhost"}, "127.0.0.1"),
        ({"remote_user": "root;bad"}, "remote_user"),
        ({"port": 0}, "port"),
        ({"port": True}, "port"),
        ({"container_name": "bad;name"}, "unsafe"),
        ({"container_name": "-bad"}, "unsafe"),
        ({"output_cap_bytes": 10}, "65536"),
    ],
)
def test_unsafe_config_is_rejected(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _config(tmp_path, **overrides)


def test_relative_link_and_public_private_key_are_rejected(tmp_path: Path) -> None:
    executable = _file(tmp_path / "ssh", executable=True)
    key = _file(tmp_path / "key")
    known_hosts = _file(tmp_path / "known_hosts")
    with pytest.raises(ValueError, match="absolute"):
        SshTaskEnvironmentConfig("ssh", 22, str(key), str(known_hosts), "container")
    link = tmp_path / "ssh-link"
    try:
        link.symlink_to(executable)
    except OSError:
        pytest.skip("file symlinks are unavailable")
    with pytest.raises(ValueError, match="trusted"):
        SshTaskEnvironmentConfig(str(link), 22, str(key), str(known_hosts), "container")
    if os.name == "posix":
        key.chmod(0o644)
        with pytest.raises(ValueError, match="trusted"):
            SshTaskEnvironmentConfig(
                str(executable), 22, str(key), str(known_hosts), "container"
            )


@pytest.mark.parametrize(
    ("command", "cwd", "timeout"),
    [
        ("", "/task", 1),
        ("true\x00bad", "/task", 1),
        ("true", "relative", 1),
        ("true", "/task/../host", 1),
        ("true", "/task", 0),
        ("true", "/task", True),
    ],
)
def test_unsafe_exec_request_is_rejected_before_spawn(
    tmp_path: Path, command: str, cwd: str, timeout: int
) -> None:
    opened = False

    async def factory(*argv: str, **kwargs: Any) -> FakeProcess:
        nonlocal opened
        opened = True
        return FakeProcess()

    async def scenario() -> None:
        environment = SshDockerTaskEnvironment(_config(tmp_path), process_factory=factory)
        with pytest.raises(ValueError):
            await environment.exec(command, cwd=cwd, timeout_sec=timeout)
        assert opened is False

    asyncio.run(scenario())
