from __future__ import annotations

import asyncio
import ntpath
import os
import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol


_CONTAINER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_REMOTE_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_DEFAULT_OUTPUT_CAP = 65_536


@dataclass(frozen=True, slots=True)
class ExecResult:
    stdout: str
    stderr: str
    return_code: int


@dataclass(frozen=True, slots=True)
class SshTaskEnvironmentConfig:
    ssh_executable: str
    port: int
    private_key: str
    known_hosts: str
    container_name: str
    host: str = "127.0.0.1"
    remote_user: str = "stage2"
    output_cap_bytes: int = _DEFAULT_OUTPUT_CAP
    termination_grace_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("SSH host must be exactly 127.0.0.1")
        if _REMOTE_USER.fullmatch(self.remote_user) is None:
            raise ValueError("remote_user is unsafe")
        if type(self.port) is not int or not 1 <= self.port <= 65_535:
            raise ValueError("SSH port must be an integer from 1 to 65535")
        if _CONTAINER_NAME.fullmatch(self.container_name) is None:
            raise ValueError("container_name is unsafe")
        if type(self.output_cap_bytes) is not int or self.output_cap_bytes < 1:
            raise ValueError("output_cap_bytes must be exactly 65536")
        if self.output_cap_bytes != _DEFAULT_OUTPUT_CAP:
            raise ValueError("output_cap_bytes must be exactly 65536")
        if (
            isinstance(self.termination_grace_seconds, bool)
            or not isinstance(self.termination_grace_seconds, (int, float))
            or self.termination_grace_seconds <= 0
        ):
            raise ValueError("termination_grace_seconds must be positive")
        object.__setattr__(
            self,
            "ssh_executable",
            str(_validated_file(self.ssh_executable, "ssh_executable", executable=True)),
        )
        object.__setattr__(
            self,
            "private_key",
            str(_validated_file(self.private_key, "private_key", private=True)),
        )
        object.__setattr__(
            self,
            "known_hosts",
            str(_validated_file(self.known_hosts, "known_hosts", private=True)),
        )


class _Process(Protocol):
    stdout: Any
    stderr: Any
    returncode: int | None

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


ProcessFactory = Callable[..., Awaitable[_Process]]


class SshDockerTaskEnvironment:
    """Forward the narrow TaskEnvironment.exec contract to one fixed container."""

    def __init__(
        self,
        config: SshTaskEnvironmentConfig,
        *,
        process_factory: ProcessFactory | None = None,
    ) -> None:
        self._config = config
        self._process_factory = process_factory or asyncio.create_subprocess_exec

    async def exec(self, command: str, *, cwd: str, timeout_sec: int) -> ExecResult:
        _validate_exec_request(command, cwd, timeout_sec)
        remote_argv = [
            "sudo",
            "docker",
            "exec",
            "-w",
            cwd,
            self._config.container_name,
            "timeout",
            "--signal=KILL",
            str(timeout_sec),
            "bash",
            "-lc",
            command,
        ]
        ssh_argv = [
            self._config.ssh_executable,
            "-F",
            "none",
            "-T",
            "-p",
            str(self._config.port),
            "-i",
            self._config.private_key,
            "-o",
            f"UserKnownHostsFile={self._config.known_hosts}",
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
            f"{self._config.remote_user}@{self._config.host}",
            shlex.join(remote_argv),
        ]
        process: _Process | None = None
        stdout_task: asyncio.Task[str] | None = None
        stderr_task: asyncio.Task[str] | None = None
        try:
            process = await self._process_factory(
                *ssh_argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            if process.stdout is None or process.stderr is None:
                raise RuntimeError
            stdout_task = asyncio.create_task(
                _drain_capped(process.stdout, self._config.output_cap_bytes)
            )
            stderr_task = asyncio.create_task(
                _drain_capped(process.stderr, self._config.output_cap_bytes)
            )
            try:
                return_code = await asyncio.wait_for(
                    process.wait(), timeout=timeout_sec + self._config.termination_grace_seconds
                )
            except TimeoutError:
                await _terminate_then_kill(process, self._config.termination_grace_seconds)
                await asyncio.gather(stdout_task, stderr_task)
                return ExecResult("", "task execution timed out", 124)
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            return ExecResult(stdout, stderr, return_code)
        except asyncio.CancelledError:
            if process is not None:
                await asyncio.shield(
                    _terminate_then_kill(process, self._config.termination_grace_seconds)
                )
            await asyncio.shield(_finish_drain_tasks(stdout_task, stderr_task))
            raise
        except BaseException:
            if process is not None:
                await _terminate_then_kill(process, self._config.termination_grace_seconds)
            await _finish_drain_tasks(stdout_task, stderr_task)
            raise RuntimeError("SSH task environment operation failed") from None


async def _drain_capped(reader: Any, cap: int) -> str:
    chunks: list[bytes] = []
    captured = 0
    while True:
        chunk = await reader.read(8192)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise RuntimeError
        remaining = cap - captured
        if remaining > 0:
            chunks.append(chunk[:remaining])
            captured += min(len(chunk), remaining)
    return _decode_with_utf8_cap(b"".join(chunks), cap)


def _decode_with_utf8_cap(raw: bytes, cap: int) -> str:
    text = raw.decode("utf-8", errors="replace")
    if len(text.encode("utf-8")) <= cap:
        return text
    low, high = 0, len(text)
    while low < high:
        midpoint = (low + high + 1) // 2
        if len(text[:midpoint].encode("utf-8")) <= cap:
            low = midpoint
        else:
            high = midpoint - 1
    return text[:low]


async def _terminate_then_kill(process: _Process, grace: float) -> None:
    if process.returncode is not None:
        await process.wait()
        return
    try:
        process.terminate()
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
        return
    except TimeoutError:
        pass
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass
    await process.wait()


async def _finish_drain_tasks(*tasks: asyncio.Task[str] | None) -> None:
    active = [task for task in tasks if task is not None]
    if active:
        await asyncio.gather(*active, return_exceptions=True)


def _validate_exec_request(command: Any, cwd: Any, timeout_sec: Any) -> None:
    if (
        not isinstance(command, str)
        or not command
        or "\x00" in command
        or len(command.encode("utf-8")) > 131_072
    ):
        raise ValueError("command is invalid")
    if (
        not isinstance(cwd, str)
        or not cwd.startswith("/")
        or "\x00" in cwd
        or posixpath.normpath(cwd) != cwd
    ):
        raise ValueError("cwd must be a normalized absolute POSIX path")
    if type(timeout_sec) is not int or timeout_sec < 1:
        raise ValueError("timeout_sec must be a positive integer")


def _validated_file(
    value: Any, label: str, *, executable: bool = False, private: bool = False
) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be an absolute regular file")
    raw = Path(value)
    if not (posixpath.isabs(value) or ntpath.isabs(value)):
        raise ValueError(f"{label} must be an absolute regular file")
    try:
        if raw.is_symlink() or _is_junction(raw):
            raise OSError
        resolved = raw.resolve(strict=True)
        if not resolved.is_file() or os.path.normcase(os.path.abspath(raw)) != os.path.normcase(str(resolved)):
            raise OSError
        metadata = resolved.stat()
        if executable and os.name == "posix" and not os.access(resolved, os.X_OK):
            raise OSError
        if private and os.name == "posix" and metadata.st_mode & 0o077:
            raise OSError
    except OSError as exc:
        raise ValueError(f"{label} must be a trusted regular file") from exc
    return resolved


def _is_junction(path: Path) -> bool:
    checker = getattr(os.path, "isjunction", None)
    return bool(checker is not None and checker(path))
