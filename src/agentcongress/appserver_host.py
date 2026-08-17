from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .appserver_client import (
    AppServerClient,
    JsonlStreamTransport,
    SlotConfig,
    SlotResult,
    TaskEnvironment,
    TaskExecResult,
)


HOST_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)
DEFAULT_APP_SERVER_ARGV = ("app-server", "--stdio", "--strict-config")
_TRIAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_DISCOVERY_NAMES = frozenset(
    {
        ".git",
        ".agents",
        ".codex",
        ".mcp",
        "agents",
        "agents.md",
        "mcp",
        "mcp.json",
        "mcp_servers",
        "mcp-servers",
        "plugin",
        "plugins",
        ".codex-plugin",
        "skills",
        "skill.md",
    }
)
_CODEX_HOME_FORBIDDEN_NAMES = _DISCOVERY_NAMES - {".codex"}


class AppServerHostError(RuntimeError):
    """A fixed-message host boundary failure that never reflects child data."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AppServerProcessSpec:
    """Immutable production process launch contract for Codex app-server."""

    executable: str
    argv: tuple[str, ...] = DEFAULT_APP_SERVER_ARGV
    code_mode_host_url: str | None = None
    inherited_env_allowlist: frozenset[str] = HOST_ENV_ALLOWLIST
    stderr_cap_bytes: int = 65_536
    terminate_grace_seconds: float = 2.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.executable, str)
            or not self.executable
            or "\x00" in self.executable
            or not Path(self.executable).is_absolute()
        ):
            raise ValueError("executable must be an absolute path")
        if (
            not isinstance(self.argv, tuple) or self.argv != DEFAULT_APP_SERVER_ARGV
        ):
            raise ValueError("argv must exactly equal the frozen app-server command")
        if self.code_mode_host_url is not None:
            match = re.fullmatch(r"ws://127\.0\.0\.1:([1-9][0-9]{0,4})", self.code_mode_host_url)
            if match is None or int(match.group(1)) > 65535:
                raise ValueError("code-mode host URL must be loopback ws with a valid port")
        if (
            not isinstance(self.inherited_env_allowlist, frozenset)
            or any(
                not isinstance(key, str) or not key or key.upper() == "CODEX_HOME"
                for key in self.inherited_env_allowlist
            )
        ):
            raise ValueError("environment allowlist is invalid")
        if self.stderr_cap_bytes < 1 or self.terminate_grace_seconds <= 0:
            raise ValueError("stderr cap and termination grace must be positive")

    @property
    def process_argv(self) -> tuple[str, ...]:
        if self.code_mode_host_url is None:
            return self.argv
        return (*self.argv, "--code-mode-host", self.code_mode_host_url)


@dataclass(frozen=True, slots=True)
class StderrSummary:
    # ``observed_bytes`` is bounded for compact evidence; ``total_bytes`` and
    # ``sha256`` cover the complete drained stream.
    observed_bytes: int = 0
    total_bytes: int = 0
    truncated: bool = False
    sha256: str = field(default_factory=lambda: hashlib.sha256(b"").hexdigest())


def validate_codex_home(path: str | Path) -> Path:
    """Validate a private auth/config home without reading secret contents."""

    home = _validate_private_directory(path, label="CODEX_HOME", require_empty=False)
    _reject_discovery_material(home, forbidden=_CODEX_HOME_FORBIDDEN_NAMES, recursive=True)
    return home


def build_subprocess_environment(
    source: Mapping[str, str],
    codex_home: str | Path,
    *,
    allowlist: frozenset[str] = HOST_ENV_ALLOWLIST,
) -> dict[str, str]:
    home = validate_codex_home(codex_home)
    allowed = {key.upper() for key in allowlist}
    result: dict[str, str] = {}
    seen: set[str] = set()
    for key, value in source.items():
        folded = key.upper() if isinstance(key, str) else ""
        if folded not in allowed:
            continue
        if folded in seen or not isinstance(value, str) or "\x00" in value:
            raise AppServerHostError(
                "invalid_host_environment", "host environment contains an invalid allowed value"
            )
        seen.add(folded)
        result[folded] = value
    result["CODEX_HOME"] = str(home)
    return result


class HostJail:
    """Owned per-trial empty directory with deterministic secure cleanup."""

    __slots__ = ("path", "_temporary")

    def __init__(self, path: Path, temporary: tempfile.TemporaryDirectory[str]) -> None:
        self.path = path
        self._temporary = temporary

    @classmethod
    def create(cls, parent: str | Path, trial_id: str) -> HostJail:
        if not isinstance(trial_id, str) or _TRIAL_ID.fullmatch(trial_id) is None:
            raise AppServerHostError("invalid_trial_id", "trial id is not safe for a host jail")
        parent_path = _validate_directory_root(parent, label="host jail parent")
        temporary = tempfile.TemporaryDirectory(prefix=f"agentcongress-{trial_id}-", dir=parent_path)
        path = Path(temporary.name)
        try:
            os.chmod(path, 0o700)
            path = validate_empty_host_jail(path)
        except BaseException:
            temporary.cleanup()
            raise
        return cls(path, temporary)

    def cleanup(self) -> None:
        self._temporary.cleanup()


def validate_empty_host_jail(path: str | Path) -> Path:
    jail = _validate_private_directory(path, label="host jail", require_empty=True)
    # Empty is the stronger invariant, but name the prohibited discovery roots
    # explicitly so this remains evident if the emptiness rule changes later.
    forbidden = {".git", "AGENTS.md", "AGENTS", ".agents", ".codex", "skills", "SKILL.md"}
    if any((jail / name).exists() for name in forbidden):
        raise AppServerHostError("host_jail_not_empty", "host jail contains discovery material")
    return jail


class _Process(Protocol):
    stdin: Any
    stdout: Any
    stderr: Any
    returncode: int | None

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


ProcessFactory = Callable[..., Awaitable[_Process]]


class AppServerProcessOwner:
    """Own exactly one app-server process and one empty jail per trial."""

    def __init__(
        self,
        spec: AppServerProcessSpec,
        *,
        codex_home: str | Path,
        host_jail_parent: str | Path,
        host_jail_trust_boundary: str | Path | None = None,
        host_environment: Mapping[str, str] | None = None,
        process_factory: ProcessFactory | None = None,
    ) -> None:
        self.spec = spec
        self.codex_home = validate_codex_home(codex_home)
        self.host_jail_parent = _validate_private_directory(
            host_jail_parent, label="host jail parent", require_empty=False
        )
        boundary_raw = self.host_jail_parent if host_jail_trust_boundary is None else host_jail_trust_boundary
        self.host_jail_trust_boundary = _validate_directory_root(
            boundary_raw, label="host jail trust boundary"
        )
        _validate_discovery_free_chain(
            self.host_jail_parent, self.host_jail_trust_boundary
        )
        self._host_environment = dict(os.environ if host_environment is None else host_environment)
        self._process_factory = process_factory or asyncio.create_subprocess_exec

    def open_trial(
        self,
        task_environment: TaskEnvironment,
        *,
        task_root: str,
        trial_id: str,
    ) -> AppServerTrial:
        return AppServerTrial(
            self,
            task_environment=task_environment,
            task_root=task_root,
            trial_id=trial_id,
        )


class AppServerTrial:
    """Async lifetime for a bound, initialized AppServerClient."""

    def __init__(
        self,
        owner: AppServerProcessOwner,
        *,
        task_environment: TaskEnvironment,
        task_root: str,
        trial_id: str,
    ) -> None:
        self._owner = owner
        self._task_environment = task_environment
        self._task_root = task_root
        self._trial_id = trial_id
        self._jail: HostJail | None = None
        self._process: _Process | None = None
        self._stderr_task: asyncio.Task[StderrSummary] | None = None
        self.client: AppServerClient | None = None
        self.stderr_summary = StderrSummary()

    @property
    def host_jail(self) -> Path:
        if self._jail is None:
            raise AppServerHostError("trial_not_started", "app-server trial is not active")
        return self._jail.path

    async def __aenter__(self) -> AppServerTrial:
        try:
            _validate_private_directory(
                self._owner.host_jail_parent,
                label="host jail parent",
                require_empty=False,
            )
            _validate_discovery_free_chain(
                self._owner.host_jail_parent,
                self._owner.host_jail_trust_boundary,
            )
            self._jail = HostJail.create(self._owner.host_jail_parent, self._trial_id)
            environment = build_subprocess_environment(
                self._owner._host_environment,
                self._owner.codex_home,
                allowlist=self._owner.spec.inherited_env_allowlist,
            )
            executable = _validate_executable(self._owner.spec.executable)
            self._process = await self._owner._process_factory(
                str(executable),
                *self._owner.spec.process_argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._jail.path),
                env=environment,
            )
            if (
                self._process.stdin is None
                or self._process.stdout is None
                or self._process.stderr is None
            ):
                raise AppServerHostError(
                    "missing_process_pipe", "app-server did not provide all stdio pipes"
                )
            self._stderr_task = asyncio.create_task(
                _drain_stderr(
                    self._process.stderr, cap=self._owner.spec.stderr_cap_bytes
                )
            )
            transport = JsonlStreamTransport(self._process.stdout, self._process.stdin)
            self.client = AppServerClient(
                transport,
                self._task_environment,
                host_control_cwd=str(self._jail.path),
                task_root=self._task_root,
            )
            await self.client.initialize()
            return self
        except asyncio.CancelledError:
            await asyncio.shield(self.close())
            raise
        except AppServerHostError:
            await self.close()
            raise
        except BaseException as exc:
            await self.close()
            raise AppServerHostError(
                "app_server_start_failed", "app-server failed during sealed startup"
            ) from exc

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await asyncio.shield(self.close())

    async def close(self) -> None:
        cleanup_error: BaseException | None = None
        try:
            process, self._process = self._process, None
            if process is not None:
                await _close_stdin(process.stdin)
                if process.returncode is None:
                    try:
                        process.terminate()
                    except (ProcessLookupError, OSError):
                        pass
                    try:
                        await asyncio.wait_for(
                            process.wait(), timeout=self._owner.spec.terminate_grace_seconds
                        )
                    except TimeoutError:
                        try:
                            process.kill()
                        except (ProcessLookupError, OSError):
                            pass
                        await process.wait()
                else:
                    await process.wait()
        except BaseException as exc:
            cleanup_error = exc
        try:
            if self._stderr_task is not None:
                task, self._stderr_task = self._stderr_task, None
                try:
                    self.stderr_summary = await asyncio.wait_for(
                        asyncio.shield(task), timeout=self._owner.spec.terminate_grace_seconds
                    )
                except TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        finally:
            self.client = None
            if self._jail is not None:
                jail, self._jail = self._jail, None
                jail.cleanup()
        if cleanup_error is not None:
            raise AppServerHostError(
                "app_server_cleanup_failed", "app-server could not be reaped cleanly"
            ) from cleanup_error


class _ExecOnlyEnvironment:
    """Capability membrane: AppServerClient receives only TaskEnvironment.exec."""

    __slots__ = ("_exec",)

    def __init__(self, environment: TaskEnvironment) -> None:
        try:
            self._exec = environment.exec
        except BaseException as exc:
            raise AppServerHostError(
                "invalid_task_environment", "task environment does not expose exec"
            ) from exc

    async def exec(self, command: str, *, cwd: str, timeout_sec: int) -> TaskExecResult:
        return await self._exec(command, cwd=cwd, timeout_sec=timeout_sec)


@dataclass(frozen=True, slots=True)
class HarborTrialPlan:
    trial_id: str
    task_root: str
    slots: tuple[SlotConfig, SlotConfig, SlotConfig]

    def __post_init__(self) -> None:
        if _TRIAL_ID.fullmatch(self.trial_id) is None:
            raise ValueError("trial_id is invalid")
        if not isinstance(self.task_root, str) or not self.task_root.startswith("/"):
            raise ValueError("task_root must be an absolute POSIX path")
        if len(self.slots) != 3 or [slot.position for slot in self.slots] != [1, 2, 3]:
            raise ValueError("Harbor trial requires fixed slots 1, 2, and 3")
        if len({slot.slot_id for slot in self.slots}) != 3:
            raise ValueError("slot ids must be unique")


class HarborAppServerAgent:
    """Blocked scaffold for a future Harbor ``BaseAgent`` adapter.

    It cannot run until a formal evidence loader and real Harbor 0.20 adapter
    exist. This class is deliberately not registered or advertised as ready.
    """

    def __init__(self, owner: AppServerProcessOwner) -> None:
        self._owner = owner

    async def run(
        self,
        environment: TaskEnvironment,
        plan: HarborTrialPlan,
        *,
        discovered_setup: Mapping[str, Any] | object | None = None,
        discovered_config: Mapping[str, Any] | object | None = None,
    ) -> tuple[SlotResult, ...]:
        validate_harbor_discovery(discovered_setup, discovered_config)
        raise AppServerHostError(
            "formal_evidence_loader_missing",
            "Harbor execution is blocked until formal evidence can be loaded and verified",
        )


_FORBIDDEN_DISCOVERY_FIELDS = {
    "extra_env",
    "extraenv",
    "env",
    "environment",
    "environment_variables",
    "mcp",
    "mcps",
    "mcp_servers",
    "mcpservers",
    "skills",
    "skill_paths",
    "auth",
    "api_key",
    "apikey",
    "user",
    "upload",
    "uploads",
}
_NORMALIZED_FORBIDDEN_DISCOVERY_FIELDS = {
    "".join(character for character in field.casefold() if character.isalnum())
    for field in _FORBIDDEN_DISCOVERY_FIELDS
}


def validate_harbor_discovery(*sources: Mapping[str, Any] | object | None) -> None:
    """Reject discovered authority-bearing agent setup before starting Codex."""

    for source in sources:
        _validate_discovery_source(source, depth=0, visited=set())


def _validate_discovery_source(source: Any, *, depth: int, visited: set[int]) -> None:
    if source is None:
        return
    if id(source) in visited or depth > 2:
        raise AppServerHostError(
            "unsafe_harbor_configuration", "Harbor setup contains recursive configuration"
        )
    visited.add(id(source))
    fields = _discovery_fields(source)
    for key, value in fields.items():
        normalized = "".join(character for character in str(key).casefold() if character.isalnum())
        if normalized in _NORMALIZED_FORBIDDEN_DISCOVERY_FIELDS and not _is_explicitly_empty(value):
            raise AppServerHostError(
                "unsafe_harbor_configuration",
                "Harbor setup contains external authority or injection configuration",
            )
    for key, value in fields.items():
        normalized = "".join(character for character in str(key).casefold() if character.isalnum())
        if normalized in {"setup", "config"} and value is not None:
            _validate_discovery_source(value, depth=depth + 1, visited=visited)


def _discovery_fields(source: Any) -> Mapping[Any, Any]:
    if isinstance(source, Mapping):
        return source
    try:
        fields = vars(source)
    except (TypeError, AttributeError) as exc:
        raise AppServerHostError(
            "unsafe_harbor_configuration", "Harbor setup cannot be inspected safely"
        ) from exc
    if not isinstance(fields, Mapping):
        raise AppServerHostError(
            "unsafe_harbor_configuration", "Harbor setup cannot be inspected safely"
        )
    return fields


def _is_explicitly_empty(value: Any) -> bool:
    if value is None or value is False:
        return True
    if isinstance(value, (str, bytes, tuple, list, dict, set, frozenset)):
        return len(value) == 0
    return False


async def _drain_stderr(reader: Any, *, cap: int) -> StderrSummary:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = await reader.read(8192)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise AppServerHostError("invalid_stderr", "app-server stderr was not bytes")
        digest.update(chunk)
        total += len(chunk)
    return StderrSummary(
        observed_bytes=min(total, cap),
        total_bytes=total,
        truncated=total > cap,
        sha256=digest.hexdigest(),
    )


async def _close_stdin(writer: Any) -> None:
    if writer is None:
        return
    try:
        writer.close()
        wait_closed = getattr(writer, "wait_closed", None)
        if wait_closed is not None:
            await wait_closed()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def _validate_executable(path: str | Path) -> Path:
    raw = Path(path)
    try:
        if not raw.is_absolute() or raw.is_symlink() or _is_junction(raw):
            raise OSError
        resolved = raw.resolve(strict=True)
        if not resolved.is_file() or not _same_spelling(raw, resolved):
            raise OSError
    except OSError as exc:
        raise AppServerHostError(
            "invalid_codex_executable", "Codex executable is not a fixed regular file"
        ) from exc
    return resolved


def _validate_directory_root(path: str | Path, *, label: str) -> Path:
    raw = Path(path)
    try:
        if not raw.is_absolute() or raw.is_symlink() or _is_junction(raw):
            raise OSError
        resolved = raw.resolve(strict=True)
        if not resolved.is_dir() or not _same_spelling(raw, resolved):
            raise OSError
    except OSError as exc:
        raise AppServerHostError("invalid_secure_directory", f"{label} is not a secure directory") from exc
    return resolved


def _validate_private_directory(
    path: str | Path, *, label: str, require_empty: bool
) -> Path:
    resolved = _validate_directory_root(path, label=label)
    try:
        metadata = resolved.stat()
        if os.name == "posix":
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise OSError
        if require_empty and any(resolved.iterdir()):
            raise AppServerHostError("host_jail_not_empty", "host jail must be empty")
    except AppServerHostError:
        raise
    except OSError as exc:
        raise AppServerHostError("invalid_secure_directory", f"{label} is not private") from exc
    return resolved


def _validate_discovery_free_chain(parent: Path, trust_boundary: Path) -> None:
    try:
        parent.relative_to(trust_boundary)
    except ValueError as exc:
        raise AppServerHostError(
            "invalid_trust_boundary", "host jail parent is outside its trust boundary"
        ) from exc
    current = parent
    while True:
        if _is_forbidden_discovery_name(current.name, _DISCOVERY_NAMES):
            raise AppServerHostError(
                "discovery_material_present",
                "host jail ancestry contains an agent discovery path",
            )
        _reject_discovery_material(current, forbidden=_DISCOVERY_NAMES, recursive=False)
        if current == trust_boundary:
            return
        current = current.parent


def _reject_discovery_material(
    directory: Path, *, forbidden: frozenset[str], recursive: bool
) -> None:
    try:
        entries = directory.rglob("*") if recursive else directory.iterdir()
        for entry in entries:
            if _is_forbidden_discovery_name(entry.name, forbidden):
                raise AppServerHostError(
                    "discovery_material_present",
                    "secure host directory contains agent discovery material",
                )
            if entry.is_symlink() or _is_junction(entry):
                raise AppServerHostError(
                    "discovery_material_present",
                    "secure host directory contains an untrusted link",
                )
    except AppServerHostError:
        raise
    except OSError as exc:
        raise AppServerHostError(
            "invalid_secure_directory", "secure host directory could not be inspected"
        ) from exc


def _is_forbidden_discovery_name(name: str, forbidden: frozenset[str]) -> bool:
    folded = name.casefold()
    return folded in forbidden or folded.startswith(("agents.", "mcp.", "mcp-", "mcp_", "plugin.", "plugin-", "plugin_"))


def _same_spelling(raw: Path, resolved: Path) -> bool:
    return os.path.normcase(os.path.abspath(raw)) == os.path.normcase(str(resolved))


def _is_junction(path: Path) -> bool:
    checker = getattr(os.path, "isjunction", None)
    return bool(checker is not None and checker(path))
