from __future__ import annotations

import asyncio
import json
import ntpath
import posixpath
import math
import shlex
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol, Sequence


JsonObject = dict[str, Any]
# The app-server itself never edits the task.  All task access is delegated to
# ``taskenv`` dynamic tools, so the narrow built-in host profile is sufficient
# and does not require a project-local custom profile.
PERMISSION_PROFILE = ":read-only"
TOOL_NAMESPACE = "taskenv"
FIXED_SLOT_SECONDS = (240, 120, 840)
READ_ONLY_TOOL_LIMITS = {1: 8, 2: 4}
TOKEN_USAGE_UPDATED_METHOD = "thread/tokenUsage/updated"
_TOKEN_COUNT_FIELDS = (
    "inputTokens",
    "cachedInputTokens",
    "outputTokens",
    "reasoningOutputTokens",
    "totalTokens",
)
_OPTIONAL_TOKEN_COUNT_FIELDS = ("cacheWriteInputTokens",)


class AppServerProtocolError(RuntimeError):
    """A fail-closed, machine-readable app-server protocol failure."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> JsonObject:
        payload: JsonObject = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class AppServerTransport(Protocol):
    async def send(self, message: Mapping[str, Any]) -> None: ...

    async def receive(self) -> JsonObject: ...


class TaskExecResult(Protocol):
    """Narrow result shape accepted from a Harbor-like task environment."""

    stdout: str
    stderr: str
    return_code: int


class TaskEnvironment(Protocol):
    """The only execution authority available to dynamic tools.

    Implementations run in the task environment.  This module never invokes a
    host shell or reads host files.
    """

    async def exec(
        self, command: str, *, cwd: str, timeout_sec: int
    ) -> TaskExecResult: ...


class JsonlStreamTransport:
    """Newline-delimited JSON transport for an asyncio subprocess stdio pair."""

    def __init__(self, reader: asyncio.StreamReader, writer: Any, *, max_bytes: int = 1_048_576) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._reader = reader
        self._writer = writer
        self._max_bytes = max_bytes

    async def send(self, message: Mapping[str, Any]) -> None:
        try:
            encoded = json.dumps(
                dict(message), ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise AppServerProtocolError("invalid_outgoing_json", str(exc)) from exc
        if len(encoded) > self._max_bytes:
            raise AppServerProtocolError(
                "outgoing_message_too_large",
                "outgoing JSONL message exceeds the configured cap",
                size=len(encoded),
                cap=self._max_bytes,
            )
        self._writer.write(encoded + b"\n")
        await self._writer.drain()

    async def receive(self) -> JsonObject:
        try:
            line = await self._reader.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise AppServerProtocolError("incoming_message_too_large", str(exc)) from exc
        if not line:
            raise AppServerProtocolError("transport_eof", "app-server closed its stdout")
        if len(line) > self._max_bytes + 1:
            raise AppServerProtocolError(
                "incoming_message_too_large",
                "incoming JSONL message exceeds the configured cap",
                size=len(line),
                cap=self._max_bytes,
            )
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppServerProtocolError("invalid_incoming_json", str(exc)) from exc
        if not isinstance(value, dict):
            raise AppServerProtocolError("invalid_message", "JSONL message must be an object")
        return value


@dataclass(frozen=True, slots=True)
class SlotConfig:
    """One member of the fixed 240/120/840-second three-slot protocol."""

    position: Literal[1, 2, 3]
    slot_id: str
    actor: str
    model: str
    prompt: str
    output_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.position not in (1, 2, 3):
            raise ValueError("position must be 1, 2, or 3")
        _validate_slot_payload(
            slot_id=self.slot_id,
            actor=self.actor,
            model=self.model,
            prompt=self.prompt,
            output_schema=self.output_schema,
        )

    @property
    def max_seconds(self) -> int:
        return FIXED_SLOT_SECONDS[self.position - 1]


@dataclass(frozen=True, slots=True)
class StandaloneSlotConfig:
    """A single-agent baseline with the same total budget as three slots.

    The actor and position are deliberately fixed so the baseline receives the
    executor's task tools without making the three-slot protocol configurable.
    """

    slot_id: str
    model: str
    prompt: str
    output_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        _validate_slot_payload(
            slot_id=self.slot_id,
            actor=self.actor,
            model=self.model,
            prompt=self.prompt,
            output_schema=self.output_schema,
        )

    @property
    def actor(self) -> Literal["executor"]:
        return "executor"

    @property
    def position(self) -> Literal[3]:
        return 3

    @property
    def max_seconds(self) -> int:
        return sum(FIXED_SLOT_SECONDS)


def _validate_slot_payload(
    *,
    slot_id: str,
    actor: str,
    model: str,
    prompt: str,
    output_schema: Mapping[str, Any],
) -> None:
    for name, value in (
        ("slot_id", slot_id),
        ("actor", actor),
        ("model", model),
        ("prompt", prompt),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    if not isinstance(output_schema, Mapping):
        raise ValueError("output_schema must be an object")
    try:
        json.dumps(output_schema, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("output_schema must be JSON serializable") from exc
    try:
        _check_supported_schema(output_schema)
    except AppServerProtocolError as exc:
        raise ValueError(f"unsupported output_schema: {exc.message}") from exc


@dataclass(frozen=True, slots=True)
class SlotResult:
    slot_id: str
    thread_id: str
    turn_id: str
    status: str
    completion: Mapping[str, Any]
    text: str
    typed_output: Any
    # Latest cumulative ``tokenUsage.total`` notification for this exact turn,
    # normalized to the project's snake_case reporting convention.  App-server
    # may omit usage notifications, in which case this remains ``None``.
    usage: Mapping[str, int] | None = None
    tool_call_count: int = 0


@dataclass(slots=True)
class _ActiveTurn:
    thread_id: str
    turn_id: str
    deadline: float
    allow_exec: bool
    output_schema: Mapping[str, Any]
    calls: set[str] = field(default_factory=set)
    completed_items: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    usage: Mapping[str, int] | None = None
    interrupted: bool = False
    tool_call_limit: int | None = None


def _function(name: str, description: str, properties: JsonObject, required: Sequence[str]) -> JsonObject:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
    }


_CWD = {"type": "string", "minLength": 1}
_TIMEOUT = {"type": "integer", "minimum": 1}
TASKENV_DYNAMIC_TOOLS: tuple[JsonObject, ...] = (
            _function(
                "taskenv_exec",
                "Execute an argv vector inside the task environment.",
                {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 128,
                    },
                    "cwd": _CWD,
                    "timeoutSec": _TIMEOUT,
                },
                ("argv",),
            ),
            _function(
                "taskenv_read",
                "Read a file inside the task environment.",
                {"path": {"type": "string", "minLength": 1}, "cwd": _CWD, "timeoutSec": _TIMEOUT},
                ("path",),
            ),
            _function(
                "taskenv_list",
                "List files below one task-environment path.",
                {"path": {"type": "string", "minLength": 1}, "cwd": _CWD, "timeoutSec": _TIMEOUT},
                ("path",),
            ),
            _function(
                "taskenv_search",
                "Search text below one task-environment path.",
                {
                    "query": {"type": "string", "minLength": 1},
                    "path": {"type": "string", "minLength": 1},
                    "cwd": _CWD,
                    "timeoutSec": _TIMEOUT,
                },
                ("query", "path"),
            ),
)


class AppServerClient:
    """Minimal fail-closed host client for sealed task-environment turns.

    ``host_control_cwd`` is an absolute, caller-created empty host jail used
    only by app-server. ``task_root`` is the independent in-environment path
    enforced for every forwarded task operation.
    """

    def __init__(
        self,
        transport: AppServerTransport,
        task_environment: TaskEnvironment,
        *,
        host_control_cwd: str,
        task_root: str,
        output_cap_bytes: int = 65_536,
        max_tool_timeout_seconds: int = 120,
    ) -> None:
        if (
            not isinstance(host_control_cwd, str)
            or not host_control_cwd.strip()
            or not (posixpath.isabs(host_control_cwd) or ntpath.isabs(host_control_cwd))
        ):
            raise ValueError("host_control_cwd must be an absolute host path")
        if "\x00" in host_control_cwd:
            raise ValueError("host_control_cwd must not contain NUL")
        if not isinstance(task_root, str) or not task_root.startswith("/"):
            raise ValueError("task_root must be an absolute POSIX task-environment path")
        normalized_task_root = posixpath.normpath(task_root)
        if "\x00" in normalized_task_root:
            raise ValueError("task_root must not contain NUL")
        if output_cap_bytes < 256 or max_tool_timeout_seconds < 1:
            raise ValueError("output cap and tool timeout must be positive")
        self._transport = transport
        self._task_environment = task_environment
        self._host_control_cwd = host_control_cwd
        self._task_root = normalized_task_root
        self._output_cap_bytes = output_cap_bytes
        self._max_tool_timeout_seconds = max_tool_timeout_seconds
        self._next_id = 0
        self._initialized = False
        self._thread_ids: set[str] = set()
        self._turn_ids: set[str] = set()

    async def initialize(self) -> Mapping[str, Any]:
        if self._initialized:
            raise AppServerProtocolError("already_initialized", "client is already initialized")
        if self._next_id != 0:
            raise AppServerProtocolError("invalid_state", "initialize must be request id 0")
        result = await self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "agentcongress",
                    "title": "AgentCongress sealed task host",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._transport.send({"method": "initialized"})
        self._initialized = True
        return result

    async def run_slot(self, slot: SlotConfig | StandaloneSlotConfig) -> SlotResult:
        if not self._initialized:
            raise AppServerProtocolError("not_initialized", "initialize must complete first")
        active: _ActiveTurn | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + slot.max_seconds
        try:
            async with asyncio.timeout_at(deadline):
                thread_result = await self._request("thread/start", self._thread_params(slot))
                thread_id = self._validate_thread_start(thread_result, slot)
                turn_result = await self._request("turn/start", self._turn_params(slot, thread_id))
                turn_id = self._validate_turn_start(turn_result, thread_id)
                active = _ActiveTurn(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    deadline=deadline,
                    allow_exec=slot.position == 3,
                    output_schema=slot.output_schema,
                    tool_call_limit=READ_ONLY_TOOL_LIMITS.get(slot.position),
                )
                while True:
                    message = await self._transport.receive()
                    completion = await self._route_turn_message(message, active)
                    if completion is not None:
                        turn = completion["turn"]
                        text, typed_output = self._terminal_output(turn, active)
                        return SlotResult(
                            slot.slot_id,
                            thread_id,
                            turn_id,
                            turn["status"],
                            completion,
                            text,
                            typed_output,
                            active.usage,
                            len(active.calls),
                        )
        except TimeoutError as exc:
            if active is not None:
                await self._interrupt_once(active)
            raise AppServerProtocolError(
                "slot_deadline_exceeded",
                "slot exhausted its fixed deadline; time does not roll over",
                slot=slot.position,
                maxSeconds=slot.max_seconds,
                threadId=active.thread_id if active is not None else None,
                turnId=active.turn_id if active is not None else None,
                usage=dict(active.usage) if active is not None and active.usage is not None else None,
                toolCallCount=len(active.calls) if active is not None else 0,
            ) from exc
        except AppServerProtocolError:
            if active is not None:
                await self._interrupt_once(active)
            raise

    def _thread_params(self, slot: SlotConfig | StandaloneSlotConfig) -> JsonObject:
        dynamic_tools = deepcopy(TASKENV_DYNAMIC_TOOLS)
        if slot.position < 3:
            dynamic_tools = [tool for tool in dynamic_tools if tool["name"] != "taskenv_exec"]
        return {
            "model": slot.model,
            "cwd": self._host_control_cwd,
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "permissions": PERMISSION_PROFILE,
            "ephemeral": True,
            "runtimeWorkspaceRoots": [],
            "environments": [],
            "selectedCapabilityRoots": [],
            "dynamicTools": dynamic_tools,
        }

    def _turn_params(
        self, slot: SlotConfig | StandaloneSlotConfig, thread_id: str
    ) -> JsonObject:
        return {
            "threadId": thread_id,
            "clientUserMessageId": slot.slot_id,
            "input": [{"type": "text", "text": slot.prompt}],
            "cwd": self._host_control_cwd,
            "environments": [],
            "runtimeWorkspaceRoots": [],
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "permissions": PERMISSION_PROFILE,
            "model": slot.model,
            "effort": "high",
            "outputSchema": deepcopy(dict(slot.output_schema)),
        }

    async def _request(self, method: str, params: JsonObject) -> Mapping[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        await self._transport.send({"method": method, "id": request_id, "params": params})
        while True:
            message = await self._transport.receive()
            if "method" in message:
                if "id" in message:
                    await self._send_request_error(message, "request_out_of_phase")
                    raise AppServerProtocolError(
                        "request_out_of_phase",
                        "server request arrived before a turn was active",
                        method=message.get("method"),
                    )
                if message.get("method") == TOKEN_USAGE_UPDATED_METHOD:
                    raise AppServerProtocolError(
                        "token_usage_out_of_phase",
                        "token usage notification arrived without an active turn",
                    )
                self._reject_forbidden_notification(message)
                continue
            if message.get("id") != request_id:
                raise AppServerProtocolError(
                    "unexpected_response_id",
                    "app-server response id does not match the outstanding request",
                    expected=request_id,
                    actual=message.get("id"),
                )
            if "error" in message:
                raise AppServerProtocolError(
                    "server_error",
                    "app-server returned an error",
                    method=method,
                    error=message["error"],
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise AppServerProtocolError(
                    "invalid_response", "app-server response result must be an object", method=method
                )
            return result

    def _validate_thread_start(
        self, result: Mapping[str, Any], slot: SlotConfig | StandaloneSlotConfig
    ) -> str:
        if result.get("model") != slot.model:
            raise AppServerProtocolError("model_mismatch", "thread/start returned a different model")
        if result.get("cwd") != self._host_control_cwd:
            raise AppServerProtocolError("cwd_mismatch", "thread/start returned a different cwd")
        if result.get("approvalPolicy") != "never":
            raise AppServerProtocolError(
                "approval_policy_mismatch", "thread/start did not preserve the approval policy"
            )
        if result.get("approvalsReviewer") != "user":
            raise AppServerProtocolError(
                "approval_reviewer_mismatch", "thread/start did not preserve the approval reviewer"
            )
        if result.get("runtimeWorkspaceRoots") != []:
            raise AppServerProtocolError(
                "runtime_roots_enabled", "thread/start did not preserve empty runtime roots"
            )
        profile = result.get("activePermissionProfile")
        if (
            not isinstance(profile, dict)
            or set(profile) - {"id", "extends"}
            or profile.get("id") != PERMISSION_PROFILE
            or profile.get("extends") is not None
        ):
            raise AppServerProtocolError(
                "permission_profile_mismatch", "thread/start did not activate the sealed profile"
            )
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise AppServerProtocolError("invalid_thread", "thread/start omitted the thread object")
        thread_id = thread.get("id")
        if not _nonempty_string(thread_id) or thread_id in self._thread_ids:
            raise AppServerProtocolError("thread_not_fresh", "thread/start did not return a fresh thread id")
        if thread.get("ephemeral") is not True or thread.get("path", object()) is not None:
            raise AppServerProtocolError(
                "thread_not_ephemeral", "thread must be ephemeral and have a null persistence path"
            )
        if thread.get("cwd") != self._host_control_cwd:
            raise AppServerProtocolError("cwd_mismatch", "thread object returned a different cwd")
        for owner in (result, thread):
            if "environments" in owner and owner["environments"] not in (None, []):
                raise AppServerProtocolError(
                    "environments_enabled", "thread/start returned a non-empty environment selection"
                )
            if "selectedCapabilityRoots" in owner and owner["selectedCapabilityRoots"] not in (None, []):
                raise AppServerProtocolError(
                    "capability_roots_enabled",
                    "thread/start returned non-empty selected capability roots",
                )
        self._thread_ids.add(thread_id)
        return thread_id

    def _validate_turn_start(self, result: Mapping[str, Any], thread_id: str) -> str:
        turn = result.get("turn")
        if not isinstance(turn, dict):
            raise AppServerProtocolError("invalid_turn", "turn/start omitted the turn object")
        turn_id = turn.get("id")
        if not _nonempty_string(turn_id) or turn_id in self._turn_ids:
            raise AppServerProtocolError("turn_not_fresh", "turn/start did not return a fresh turn id")
        if "threadId" in turn and turn["threadId"] != thread_id:
            raise AppServerProtocolError("turn_thread_mismatch", "turn/start returned the wrong thread")
        if turn.get("status") != "inProgress":
            raise AppServerProtocolError("turn_not_running", "turn/start did not start an in-progress turn")
        self._turn_ids.add(turn_id)
        return turn_id

    async def _route_turn_message(
        self, message: JsonObject, active: _ActiveTurn
    ) -> Mapping[str, Any] | None:
        method = message.get("method")
        if not isinstance(method, str):
            raise AppServerProtocolError("invalid_message", "turn message must have a method")
        if "id" in message:
            if method == "item/tool/call":
                await self._handle_dynamic_tool(message, active)
                return None
            await self._decline_or_error(message)
            await self._interrupt_once(active)
            raise AppServerProtocolError(
                "forbidden_server_request", "server requested a non-taskenv capability", method=method
            )
        self._reject_forbidden_notification(message)
        if method == TOKEN_USAGE_UPDATED_METHOD:
            self._record_token_usage(message, active)
            return None
        if method in {"item/started", "item/completed"}:
            self._validate_item_scope(message, active)
        if method == "item/completed":
            self._record_completed_item(message, active)
            return None
        if method != "turn/completed":
            return None
        params = message.get("params")
        if not isinstance(params, dict) or params.get("threadId") != active.thread_id:
            raise AppServerProtocolError("completion_mismatch", "turn completion has the wrong thread")
        turn = params.get("turn")
        if not isinstance(turn, dict) or turn.get("id") != active.turn_id:
            raise AppServerProtocolError("completion_mismatch", "turn completion has the wrong turn")
        status = turn.get("status")
        if status == "failed":
            raise AppServerProtocolError("turn_failed", "app-server turn failed")
        if status != "completed":
            raise AppServerProtocolError(
                "unexpected_turn_status", "turn ended with a non-terminal or interrupted status", status=status
            )
        return params

    def _record_token_usage(
        self, message: Mapping[str, Any], active: _ActiveTurn
    ) -> None:
        params = message.get("params")
        if not isinstance(params, dict) or set(params) != {"threadId", "turnId", "tokenUsage"}:
            raise AppServerProtocolError(
                "invalid_token_usage", "token usage params have missing or additional fields"
            )
        if params["threadId"] != active.thread_id or params["turnId"] != active.turn_id:
            raise AppServerProtocolError(
                "token_usage_scope_mismatch",
                "token usage event targets another thread or turn",
            )
        usage = params["tokenUsage"]
        if not isinstance(usage, dict):
            raise AppServerProtocolError(
                "invalid_token_usage", "tokenUsage must be an object"
            )
        usage_fields = set(usage)
        if not {"total", "last"} <= usage_fields or usage_fields - {
            "total",
            "last",
            "modelContextWindow",
        }:
            raise AppServerProtocolError(
                "invalid_token_usage", "tokenUsage has missing or additional fields"
            )
        total = _validate_token_counts(usage["total"], section="total")
        _validate_token_counts(usage["last"], section="last")
        if "modelContextWindow" in usage:
            context_window = usage["modelContextWindow"]
            if context_window is not None and (
                type(context_window) is not int or context_window < 0
            ):
                raise AppServerProtocolError(
                    "invalid_token_usage",
                    "tokenUsage.modelContextWindow must be null or a non-negative integer",
                )
        # ``total`` is cumulative.  Notifications can repeat, so retain the
        # latest validated snapshot rather than summing them.
        active.usage = {
            "input_tokens": total["inputTokens"],
            "cached_input_tokens": total["cachedInputTokens"],
            "cache_write_input_tokens": total.get("cacheWriteInputTokens", 0),
            "output_tokens": total["outputTokens"],
            "reasoning_output_tokens": total["reasoningOutputTokens"],
            "total_tokens": total["totalTokens"],
        }

    def _record_completed_item(self, message: Mapping[str, Any], active: _ActiveTurn) -> None:
        params = message.get("params")
        assert isinstance(params, dict)
        item = params.get("item")
        if not isinstance(item, dict):
            raise AppServerProtocolError("invalid_item", "completed item omitted its item object")
        item_id = item.get("id")
        if not _nonempty_string(item_id) or item_id in active.completed_items:
            raise AppServerProtocolError(
                "duplicate_item", "completed item id is empty or was already completed"
            )
        active.completed_items[item_id] = deepcopy(item)

    def _validate_item_scope(self, message: Mapping[str, Any], active: _ActiveTurn) -> None:
        params = message.get("params")
        if (
            not isinstance(params, dict)
            or params.get("threadId") != active.thread_id
            or params.get("turnId") != active.turn_id
        ):
            raise AppServerProtocolError(
                "item_scope_mismatch", "item event targets another thread or turn"
            )

    def _terminal_output(
        self, turn: Mapping[str, Any], active: _ActiveTurn
    ) -> tuple[str, Any]:
        # The canonical item stream is item/completed.  App-server's terminal
        # turn is only a summary and may contain an empty item list or just the
        # final assistant message, so it must not be required to echo every
        # streamed reasoning/tool item.
        terminal = [
            item
            for item in active.completed_items.values()
            if item.get("type") == "agentMessage" and item.get("phase") == "final_answer"
        ]
        if len(terminal) != 1:
            raise AppServerProtocolError(
                "terminal_message_count",
                "completed turn must contain exactly one final agent message",
                count=len(terminal),
            )
        message = terminal[0]
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            raise AppServerProtocolError(
                "invalid_terminal_message", "final agent message must contain non-empty text"
            )
        try:
            typed_output = json.loads(text, parse_constant=_reject_nonfinite_json)
        except (json.JSONDecodeError, ValueError) as exc:
            raise AppServerProtocolError(
                "invalid_structured_output", "final agent message is not valid JSON"
            ) from exc
        _validate_json_value(typed_output, active.output_schema, path="$")
        return text, typed_output

    async def _handle_dynamic_tool(self, message: JsonObject, active: _ActiveTurn) -> None:
        request_id = message.get("id")
        if not _request_id(request_id):
            raise AppServerProtocolError("invalid_request_id", "tool request id must be a string or integer")
        params = message.get("params")
        try:
            command, cwd, timeout_sec, call_id = self._validate_tool_call(params, active)
        except AppServerProtocolError as exc:
            await self._transport.send(
                {"id": request_id, "error": {"code": -32602, "message": exc.message, "data": exc.as_dict()}}
            )
            await self._interrupt_once(active)
            raise
        active.calls.add(call_id)
        if active.tool_call_limit is not None and len(active.calls) > active.tool_call_limit:
            text = json.dumps(
                {
                    "error": {
                        "code": "tool_call_budget_exhausted",
                        "message": "Read-only investigation budget is exhausted; return the required structured conclusion now.",
                    }
                },
                separators=(",", ":"),
            )
            await self._transport.send(
                {
                    "id": request_id,
                    "result": {
                        "contentItems": [{"type": "inputText", "text": text}],
                        "success": False,
                    },
                }
            )
            return
        try:
            remaining = active.deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            result = await asyncio.wait_for(
                self._task_environment.exec(command, cwd=cwd, timeout_sec=timeout_sec),
                timeout=remaining,
            )
            payload = _task_result_payload(result)
            text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if len(text.encode("utf-8")) > self._output_cap_bytes:
                payload = {
                    "stdout": "",
                    "stderr": "",
                    "returnCode": payload["returnCode"],
                    "truncated": True,
                }
                text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                if len(text.encode("utf-8")) > self._output_cap_bytes:
                    raise ValueError
            response = {"contentItems": [{"type": "inputText", "text": text}], "success": True}
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            error_text = json.dumps(
                {
                    "error": {
                        "code": "task_environment_error",
                        "message": "task environment operation failed",
                    }
                },
                separators=(",", ":"),
            )
            response = {"contentItems": [{"type": "inputText", "text": error_text}], "success": False}
        await self._transport.send({"id": request_id, "result": response})

    def _validate_tool_call(
        self, params: Any, active: _ActiveTurn
    ) -> tuple[str, str, int, str]:
        if not isinstance(params, dict):
            raise AppServerProtocolError("invalid_tool_call", "tool params must be an object")
        required = {"threadId", "turnId", "callId", "namespace", "tool", "arguments"}
        if set(params) != required:
            raise AppServerProtocolError(
                "invalid_tool_call", "tool params have missing or additional fields"
            )
        if params["threadId"] != active.thread_id or params["turnId"] != active.turn_id:
            raise AppServerProtocolError("tool_scope_mismatch", "tool call targets another thread or turn")
        call_id = params["callId"]
        if not _nonempty_string(call_id) or call_id in active.calls:
            raise AppServerProtocolError("invalid_call_id", "tool call id is empty or was already used")
        if params["namespace"] is not None:
            raise AppServerProtocolError("invalid_tool_namespace", "namespaced tools are not allowed")
        tool = params["tool"]
        arguments = params["arguments"]
        if not isinstance(tool, str) or not isinstance(arguments, dict):
            raise AppServerProtocolError("invalid_tool_call", "tool and arguments have invalid types")
        if not isinstance(tool, str) or not tool.startswith("taskenv_"):
            raise AppServerProtocolError("invalid_tool_namespace", "only taskenv tools are allowed")
        return (*self._tool_command(tool.removeprefix("taskenv_"), arguments, active), call_id)

    def _tool_command(
        self, tool: str, arguments: JsonObject, active: _ActiveTurn
    ) -> tuple[str, str, int]:
        allowed: dict[str, tuple[set[str], set[str]]] = {
            "exec": ({"argv"}, {"cwd", "timeoutSec"}),
            "read": ({"path"}, {"cwd", "timeoutSec"}),
            "list": ({"path"}, {"cwd", "timeoutSec"}),
            "search": ({"query", "path"}, {"cwd", "timeoutSec"}),
        }
        if tool not in allowed:
            raise AppServerProtocolError("unknown_taskenv_tool", "unknown taskenv tool", tool=tool)
        if tool == "exec" and not active.allow_exec:
            raise AppServerProtocolError(
                "tool_not_allowed_in_slot", "exec is available only in the executor slot"
            )
        required, optional = allowed[tool]
        if not required <= set(arguments) or set(arguments) - required - optional:
            raise AppServerProtocolError("invalid_tool_arguments", "tool arguments do not match its schema")
        cwd = self._safe_cwd(arguments.get("cwd", self._task_root))
        timeout = arguments.get("timeoutSec", self._max_tool_timeout_seconds)
        if type(timeout) is not int or timeout < 1:
            raise AppServerProtocolError("invalid_tool_arguments", "timeoutSec must be a positive integer")
        remaining = max(1, int(active.deadline - asyncio.get_running_loop().time()))
        per_call_cap = self._max_tool_timeout_seconds if active.allow_exec else 30
        timeout = min(timeout, per_call_cap, remaining)
        if tool == "exec":
            argv = arguments["argv"]
            if (
                not isinstance(argv, list)
                or not 1 <= len(argv) <= 128
                or any(not _safe_argument(value) for value in argv)
            ):
                raise AppServerProtocolError("invalid_tool_arguments", "argv must be 1-128 safe strings")
        elif tool == "read":
            argv = ["cat", "--", self._safe_path(arguments["path"], cwd)]
        elif tool == "list":
            argv = ["find", self._safe_path(arguments["path"], cwd), "-maxdepth", "2", "-print"]
        else:
            query = arguments["query"]
            if not _safe_argument(query) or not query:
                raise AppServerProtocolError("invalid_tool_arguments", "query must be a non-empty string")
            # Task images are not guaranteed to ship ripgrep.  POSIX grep is
            # part of the selected Linux benchmark images and keeps the
            # read-only search tool independent of harness-specific packages.
            argv = ["grep", "-RIn", "--", query, self._safe_path(arguments["path"], cwd)]
        return shlex.join(argv), cwd, timeout

    def _safe_cwd(self, value: Any) -> str:
        if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
            raise AppServerProtocolError("invalid_task_path", "cwd must be an absolute task path")
        normalized = posixpath.normpath(value)
        if posixpath.commonpath((self._task_root, normalized)) != self._task_root:
            raise AppServerProtocolError("task_path_escape", "cwd escapes the task jail")
        return normalized

    def _safe_path(self, value: Any, cwd: str) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise AppServerProtocolError("invalid_task_path", "path must be a non-empty task path")
        normalized = posixpath.normpath(value if value.startswith("/") else posixpath.join(cwd, value))
        if posixpath.commonpath((self._task_root, normalized)) != self._task_root:
            raise AppServerProtocolError("task_path_escape", "path escapes the task jail")
        return normalized

    def _reject_forbidden_notification(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        folded = method.lower() if isinstance(method, str) else ""
        forbidden_method = any(
            token in folded
            for token in ("commandexecution", "filechange", "mcp", "browser", "websearch", "imagegeneration")
        )
        params = message.get("params")
        item = params.get("item") if isinstance(params, dict) else None
        item_type = item.get("type") if isinstance(item, dict) else None
        forbidden_item = item_type in {
            "commandExecution",
            "fileChange",
            "mcpToolCall",
            "webSearch",
            "imageGeneration",
            "imageView",
            "collabToolCall",
        }
        allowed_item_types = {"userMessage", "agentMessage", "reasoning", "dynamicToolCall"}
        unknown_item = (
            method in {"item/started", "item/completed"}
            and (not isinstance(item_type, str) or item_type not in allowed_item_types)
        )
        if forbidden_method or forbidden_item or unknown_item:
            safe_details: dict[str, Any] = {"method": method, "itemType": item_type}
            if method == "mcpServer/startupStatus/updated" and isinstance(params, dict):
                for key in ("name", "status", "threadId"):
                    value = params.get(key)
                    if isinstance(value, str) or value is None:
                        safe_details[key] = value
            raise AppServerProtocolError(
                "native_capability_observed",
                "app-server emitted an unapproved item or native capability event",
                **safe_details,
            )

    async def _decline_or_error(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if not _request_id(request_id):
            return
        if method == "item/permissions/requestApproval":
            await self._transport.send({"id": request_id, "result": {"permissions": {}}})
        elif isinstance(method, str) and method.endswith("/requestApproval"):
            await self._transport.send({"id": request_id, "result": {"decision": "decline"}})
        else:
            await self._send_request_error(message, "forbidden_server_request")

    async def _send_request_error(self, message: Mapping[str, Any], code: str) -> None:
        request_id = message.get("id")
        if _request_id(request_id):
            await self._transport.send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": "request is outside the sealed AgentCongress protocol",
                        "data": {"code": code, "method": message.get("method")},
                    },
                }
            )

    async def _interrupt_once(self, active: _ActiveTurn) -> None:
        if active.interrupted:
            return
        active.interrupted = True
        request_id = self._next_id
        self._next_id += 1
        await self._transport.send(
            {
                "method": "turn/interrupt",
                "id": request_id,
                "params": {"threadId": active.thread_id, "turnId": active.turn_id},
            }
        )


def _validate_token_counts(value: Any, *, section: str) -> Mapping[str, int]:
    if (
        not isinstance(value, dict)
        or not set(_TOKEN_COUNT_FIELDS) <= set(value)
        or set(value) - set(_TOKEN_COUNT_FIELDS) - set(_OPTIONAL_TOKEN_COUNT_FIELDS)
    ):
        raise AppServerProtocolError(
            "invalid_token_usage",
            f"tokenUsage.{section} has missing or unsupported token count fields",
        )
    if any(
        type(value[field]) is not int or value[field] < 0
        for field in (*_TOKEN_COUNT_FIELDS, *(_OPTIONAL_TOKEN_COUNT_FIELDS if "cacheWriteInputTokens" in value else ()))
    ):
        raise AppServerProtocolError(
            "invalid_token_usage",
            f"tokenUsage.{section} counts must be non-negative integers",
        )
    return value


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _request_id(value: Any) -> bool:
    return (isinstance(value, str) and bool(value)) or (type(value) is int)


def _safe_argument(value: Any) -> bool:
    return isinstance(value, str) and "\x00" not in value and len(value.encode("utf-8")) <= 8192


def _task_result_payload(result: Any) -> JsonObject:
    try:
        stdout = result.stdout
        stderr = result.stderr
        return_code = result.return_code
    except (AttributeError, TypeError) as exc:
        raise TypeError("invalid task environment result") from exc
    if not isinstance(stdout, str) or not isinstance(stderr, str) or type(return_code) is not int:
        raise TypeError("invalid task environment result")
    return {
        "stdout": stdout,
        "stderr": stderr,
        "returnCode": return_code,
        "truncated": False,
    }


_SCHEMA_KEYS = {
    "$schema",
    "title",
    "type",
    "enum",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
}


def _check_supported_schema(schema: Any) -> None:
    if not isinstance(schema, Mapping):
        raise AppServerProtocolError("unsupported_output_schema", "schema node must be an object")
    unknown = set(schema) - _SCHEMA_KEYS
    if unknown:
        raise AppServerProtocolError(
            "unsupported_output_schema", "schema contains unsupported keywords"
        )
    schema_type = schema.get("type")
    valid_types = {"object", "array", "string", "number", "integer", "boolean", "null"}
    if schema_type is not None:
        types = schema_type if isinstance(schema_type, list) else [schema_type]
        if not types or any(value not in valid_types for value in types) or len(set(types)) != len(types):
            raise AppServerProtocolError("unsupported_output_schema", "schema type is unsupported")
    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, list) or not values:
            raise AppServerProtocolError("unsupported_output_schema", "enum must be a non-empty array")
        try:
            json.dumps(values, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise AppServerProtocolError("unsupported_output_schema", "enum must contain JSON values") from exc
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping) or schema_type != "object":
            raise AppServerProtocolError("unsupported_output_schema", "properties requires object type")
        for subschema in properties.values():
            _check_supported_schema(subschema)
    required = schema.get("required")
    if required is not None:
        if (
            not isinstance(required, list)
            or any(not isinstance(key, str) for key in required)
            or len(set(required)) != len(required)
            or not isinstance(properties, Mapping)
            or not set(required) <= set(properties)
        ):
            raise AppServerProtocolError("unsupported_output_schema", "required fields are invalid")
    additional = schema.get("additionalProperties")
    if additional is not None and type(additional) is not bool:
        raise AppServerProtocolError(
            "unsupported_output_schema", "additionalProperties must be boolean"
        )
    if "items" in schema:
        if schema_type != "array":
            raise AppServerProtocolError("unsupported_output_schema", "items requires array type")
        _check_supported_schema(schema["items"])
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise AppServerProtocolError("unsupported_output_schema", f"{key} must be non-negative")
    for key in ("minimum", "maximum"):
        value = schema.get(key)
        if key in schema and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise AppServerProtocolError("unsupported_output_schema", f"{key} must be finite")


def _validate_json_value(value: Any, schema: Mapping[str, Any], *, path: str) -> None:
    if "enum" in schema and value not in schema["enum"]:
        raise AppServerProtocolError(
            "structured_output_schema_mismatch", "structured output is not an allowed enum value", path=path
        )
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_matches_json_type(value, candidate) for candidate in types):
            raise AppServerProtocolError(
                "structured_output_schema_mismatch", "structured output has the wrong type", path=path
            )
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise AppServerProtocolError(
                "structured_output_schema_mismatch", "structured output is missing required fields", path=path
            )
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise AppServerProtocolError(
                "structured_output_schema_mismatch", "structured output has additional fields", path=path
            )
        for key, subschema in properties.items():
            if key in value:
                _validate_json_value(value[key], subschema, path=f"{path}.{key}")
    elif isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise AppServerProtocolError("structured_output_schema_mismatch", "array is too short", path=path)
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise AppServerProtocolError("structured_output_schema_mismatch", "array is too long", path=path)
        if "items" in schema:
            for index, item in enumerate(value):
                _validate_json_value(item, schema["items"], path=f"{path}[{index}]")
    elif isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise AppServerProtocolError("structured_output_schema_mismatch", "string is too short", path=path)
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise AppServerProtocolError("structured_output_schema_mismatch", "string is too long", path=path)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise AppServerProtocolError("structured_output_schema_mismatch", "number is below minimum", path=path)
        if "maximum" in schema and value > schema["maximum"]:
            raise AppServerProtocolError("structured_output_schema_mismatch", "number is above maximum", path=path)


def _matches_json_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": (
            type(value) is int or (type(value) is float and math.isfinite(value))
        ),
        "integer": type(value) is int,
        "boolean": type(value) is bool,
        "null": value is None,
    }[expected]


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")
