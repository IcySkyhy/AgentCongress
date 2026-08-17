from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from .errors import WorkerInfrastructureError, WorkerTimeoutError


class DialogueAdapter(Protocol):
    def stream_turn(self, prompt: str) -> AsyncIterator[str]: ...


def _post_json(url: str, api_key: str, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"OpenAI-compatible API request failed ({error.code}): {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"OpenAI-compatible API request failed: {error.reason}") from error


@dataclass(slots=True)
class OpenAICompatibleDialogueAdapter:
    """Minimal OpenAI Chat Completions adapter for meeting discussion turns."""

    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 120.0
    max_tokens: int = 512
    thinking_enabled: bool | None = None

    async def complete(self, prompt: str, *, json_output: bool = False) -> str:
        payload: dict[str, Any] = {"model": self.model, "messages": [{"role": "user", "content": prompt}], "stream": False, "max_tokens": self.max_tokens}
        if self.thinking_enabled is not None:
            payload["thinking"] = {"type": "enabled" if self.thinking_enabled else "disabled"}
        if json_output:
            payload["response_format"] = {"type": "json_object"}
        response = await asyncio.to_thread(_post_json, f"{self.base_url.rstrip('/')}/v1/chat/completions", self.api_key, payload, self.timeout_seconds)
        try:
            content = response["choices"][0]["message"]["content"]
        except (IndexError, KeyError, TypeError) as error:
            raise RuntimeError("OpenAI-compatible API returned no assistant message") from error
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("OpenAI-compatible API returned an empty assistant message")
        return content

    async def stream_turn(self, prompt: str) -> AsyncIterator[str]:
        yield await self.complete(prompt)


def deepseek_dialogue_adapter(model: str = "deepseek-v4-flash", api_key_env: str = "DEEPSEEK_API_KEY") -> OpenAICompatibleDialogueAdapter:
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ValueError(f"{api_key_env} is required for the DeepSeek adapter")
    return OpenAICompatibleDialogueAdapter("https://api.deepseek.com", api_key, model, thinking_enabled=False)


@dataclass(frozen=True, slots=True)
class WorkerEvent:
    type: str
    payload: dict[str, Any]


def detect_codex_infrastructure_failure(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    """Recognize narrow, trusted Codex sandbox-bootstrap failures.

    Matching only failed ``command_execution`` events prevents a repository
    file or an assistant message that merely mentions an error from becoming
    an infrastructure failure.
    """

    if payload.get("type") != "item.completed":
        return None
    item = payload.get("item")
    if not isinstance(item, Mapping) or item.get("type") != "command_execution" or item.get("status") != "failed":
        return None
    exit_code = item.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code == 0:
        return None
    diagnostic = item.get("aggregated_output", item.get("error", ""))
    if not isinstance(diagnostic, str):
        return None
    folded = " ".join(diagnostic.casefold().split())
    if "bwrap:" in folded and "no permissions to create new namespace" in folded:
        return "bwrap_userns_unavailable", diagnostic.strip()
    windows_signature = (
        "windows sandbox:" in folded
        and "orchestrator_helper_launch_failed" in folded
        and "codex-windows-sandbox-setup.exe" in folded
        and any(marker in folded for marker in ("os error 5", "access denied", "拒绝访问"))
    )
    if windows_signature:
        return "windows_helper_access_denied", diagnostic.strip()
    return None


class WorkerAdapter(Protocol):
    async def run_task(self, prompt: str, worktree: Path, report_schema: Path) -> AsyncIterator[WorkerEvent]: ...


@dataclass(slots=True)
class CodexWorkerAdapter:
    executable: str = "codex"
    enabled_features: tuple[str, ...] = ()
    model: str | None = None
    reasoning_effort: str | None = None
    # ``sandbox`` is the legacy Codex backend.  Modern Codex permission
    # profiles are selected with ``permission_profile`` instead; keeping the
    # two knobs mutually exclusive prevents an ambiguous or silently weakened
    # containment policy.
    sandbox: str | None = "workspace-write"
    permission_profile: str | None = None
    timeout_seconds: float | None = None
    ignore_user_config: bool = True
    ephemeral: bool = True
    web_search: str = "disabled"

    def __post_init__(self) -> None:
        if self.sandbox is not None and self.permission_profile is not None:
            raise ValueError("Codex workers cannot combine a legacy sandbox with a permission profile")
        if self.sandbox is None and self.permission_profile is None:
            raise ValueError("Codex workers require a legacy sandbox or permission profile")
        # Keep this interface deliberately narrower than Codex's arbitrary
        # custom profiles: benchmark containment must be frozen and auditable.
        if self.sandbox is not None and self.sandbox not in {"read-only", "workspace-write"}:
            raise ValueError("Codex workers require read-only or workspace-write sandboxing")
        if self.permission_profile is not None and self.permission_profile not in {":read-only", ":workspace"}:
            raise ValueError("Codex workers require the built-in :read-only or :workspace permission profile")
        self.enabled_features = tuple(self.enabled_features)
        if any(not isinstance(feature, str) or not feature.strip() for feature in self.enabled_features):
            raise ValueError("Codex enabled feature names must be non-empty strings")
        if self.permission_profile is not None and "use_legacy_landlock" in self.enabled_features:
            raise ValueError("the legacy Landlock feature is incompatible with permission profiles")

    async def _stop_process_tree(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            process.kill()
        await process.wait()

    def command(self, prompt: str, worktree: Path, report_schema: Path) -> list[str]:
        command = [self.executable, "exec"]
        for feature in self.enabled_features:
            command.extend(["--enable", feature])
        if self.ignore_user_config:
            command.append("--ignore-user-config")
        if self.ephemeral:
            command.append("--ephemeral")
        command.append("--json")
        if self.permission_profile is not None:
            command.extend(["--config", f'default_permissions="{self.permission_profile}"'])
        else:
            assert self.sandbox is not None
            command.extend(["--sandbox", self.sandbox])
        command.extend(["-C", str(worktree), "--output-schema", str(report_schema)])
        if self.model:
            command.extend(["--model", self.model])
        if self.reasoning_effort:
            command.extend(["--config", f'model_reasoning_effort="{self.reasoning_effort}"'])
        if self.web_search:
            command.extend(["--config", f'web_search="{self.web_search}"'])
        command.append(prompt)
        return command

    async def run_task(self, prompt: str, worktree: Path, report_schema: Path) -> AsyncIterator[WorkerEvent]:
        spawn_options: dict[str, Any] = {"stdin": asyncio.subprocess.DEVNULL, "stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE}
        if os.name == "nt":
            spawn_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            spawn_options["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(*self.command(prompt, worktree, report_schema), **spawn_options)
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_buffer = bytearray()

        async def drain_stderr() -> None:
            async for chunk in process.stderr:
                stderr_buffer.extend(chunk)
                # Bound diagnostics without ever blocking the child process.
                if len(stderr_buffer) > 64_000:
                    del stderr_buffer[:-32_000]

        stderr_task = asyncio.create_task(drain_stderr())

        try:
            async with asyncio.timeout(self.timeout_seconds):
                async for raw in process.stdout:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                        event = WorkerEvent("codex.event", payload)
                        yield event
                        failure = detect_codex_infrastructure_failure(payload)
                        if failure is not None:
                            code, diagnostic = failure
                            await self._stop_process_tree(process)
                            await stderr_task
                            raise WorkerInfrastructureError(f"Codex sandbox could not start ({code}): {diagnostic}", code=code)
                    except json.JSONDecodeError:
                        yield WorkerEvent("codex.output", {"text": line})
                # Keep process termination inside the same deadline as output
                # consumption.  A child can close stdout and remain alive.
                return_code = await process.wait()
                await stderr_task
        except TimeoutError as error:
            await self._stop_process_tree(process)
            await stderr_task
            raise WorkerTimeoutError(f"Codex worker timed out after {self.timeout_seconds} seconds") from error
        except BaseException:
            # Async-generator cancellation (for example, a stopped meeting)
            # must not leave a paid worker running in the background.
            await self._stop_process_tree(process)
            await stderr_task
            raise
        if return_code:
            error = bytes(stderr_buffer).decode("utf-8", errors="replace")
            raise WorkerInfrastructureError(f"Codex worker failed ({return_code}): {error.strip()}")
