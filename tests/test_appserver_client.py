from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping

import pytest

from agentcongress.appserver_client import (
    AppServerClient,
    AppServerProtocolError,
    FIXED_SLOT_SECONDS,
    JsonlStreamTransport,
    PERMISSION_PROFILE,
    SlotConfig,
    StandaloneSlotConfig,
)


class FakeTransport:
    def __init__(self, incoming: list[dict[str, Any]]) -> None:
        self.incoming = deque(incoming)
        self.sent: list[dict[str, Any]] = []

    async def send(self, message: Mapping[str, Any]) -> None:
        self.sent.append(dict(message))

    async def receive(self) -> dict[str, Any]:
        if not self.incoming:
            raise AssertionError("fake transport has no incoming message")
        return self.incoming.popleft()


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    return_code: int
    ignored_secret: str = "must-not-be-serialized"


class FakeEnvironment:
    def __init__(self, result: ExecResult | None = None) -> None:
        self.result = result or ExecResult("ok", "", 0)
        self.calls: list[tuple[str, str, int]] = []

    async def exec(self, command: str, *, cwd: str, timeout_sec: int) -> ExecResult:
        self.calls.append((command, cwd, timeout_sec))
        return self.result


def _slot(position: int = 1, *, slot_id: str = "slot-1") -> SlotConfig:
    return SlotConfig(
        position=position,
        slot_id=slot_id,
        actor="analyst",
        model="gpt-test",
        prompt="Inspect the task.",
        output_schema={"type": "object", "additionalProperties": False},
    )


def _thread_response(thread_id: str = "thr-1") -> dict[str, Any]:
    return {
        "id": 1,
        "result": {
            "model": "gpt-test",
            "cwd": "/empty-host-jail",
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "runtimeWorkspaceRoots": [],
            "activePermissionProfile": {"id": PERMISSION_PROFILE, "extends": None},
            "thread": {
                "id": thread_id,
                "ephemeral": True,
                "path": None,
                "cwd": "/empty-host-jail",
                "environments": [],
            },
        },
    }


def _turn_response(turn_id: str = "turn-1") -> dict[str, Any]:
    return {
        "id": 2,
        "result": {"turn": {"id": turn_id, "threadId": "thr-1", "status": "inProgress"}},
    }


def _completion(
    status: str = "completed", *, items: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    if items is None:
        items = [
            {
                "id": "final-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "{}",
            }
        ]
    return {
        "method": "turn/completed",
        "params": {
            "threadId": "thr-1",
            "turn": {"id": "turn-1", "status": status, "items": items},
        },
    }


def _completed_sequence(
    status: str = "completed", *, items: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    completion = _completion(status, items=items)
    final_items = completion["params"]["turn"]["items"]
    return [
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr-1",
                "turnId": "turn-1",
                "completedAtMs": index + 1,
                "item": item,
            },
        }
        for index, item in enumerate(final_items)
    ] + [completion]


def _token_counts(
    *, input_tokens: int = 10, cached_input_tokens: int = 3,
    output_tokens: int = 4, reasoning_output_tokens: int = 2,
) -> dict[str, int]:
    return {
        "inputTokens": input_tokens,
        "cachedInputTokens": cached_input_tokens,
        "outputTokens": output_tokens,
        "reasoningOutputTokens": reasoning_output_tokens,
        "totalTokens": input_tokens + output_tokens,
    }


def _token_usage_event(
    *,
    thread_id: str = "thr-1",
    turn_id: str = "turn-1",
    total: dict[str, int] | None = None,
    last: dict[str, int] | None = None,
    model_context_window: int | None = 200_000,
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "total": total or _token_counts(),
        "last": last or _token_counts(input_tokens=2, cached_input_tokens=1, output_tokens=1),
    }
    if model_context_window is not None:
        usage["modelContextWindow"] = model_context_window
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "tokenUsage": usage,
        },
    }


async def _initialized_client(
    tail: list[dict[str, Any]], env: FakeEnvironment | None = None, **kwargs: Any
) -> tuple[AppServerClient, FakeTransport, FakeEnvironment]:
    transport = FakeTransport([{"id": 0, "result": {"userAgent": "fake"}}, *tail])
    environment = env or FakeEnvironment()
    client = AppServerClient(
        transport,
        environment,
        host_control_cwd="/empty-host-jail",
        task_root="/task",
        **kwargs,
    )
    await client.initialize()
    return client, transport, environment


def test_initialize_handshake_is_exact_and_omits_jsonrpc() -> None:
    async def scenario() -> None:
        client, transport, _ = await _initialized_client([])
        assert transport.sent == [
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "agentcongress",
                        "title": "AgentCongress sealed task host",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            },
            {"method": "initialized"},
        ]
        with pytest.raises(AppServerProtocolError, match="already initialized"):
            await client.initialize()

    asyncio.run(scenario())


def test_host_control_cwd_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute host path"):
        AppServerClient(
            FakeTransport([]),
            FakeEnvironment(),
            host_control_cwd="relative-jail",
            task_root="/task",
        )


def test_fixed_three_slot_deadlines_and_sealed_thread_turn_params() -> None:
    assert FIXED_SLOT_SECONDS == (240, 120, 840)
    assert [_slot(n, slot_id=f"slot-{n}").max_seconds for n in (1, 2, 3)] == [240, 120, 840]

    async def scenario() -> None:
        client, transport, _ = await _initialized_client(
            [_thread_response(), _turn_response(), *_completed_sequence()]
        )
        result = await client.run_slot(_slot())
        assert result.status == "completed"
        thread = transport.sent[2]
        assert thread["method"] == "thread/start"
        assert thread["params"]["ephemeral"] is True
        assert thread["params"]["permissions"] == PERMISSION_PROFILE
        assert thread["params"]["runtimeWorkspaceRoots"] == []
        assert thread["params"]["environments"] == []
        assert thread["params"]["selectedCapabilityRoots"] == []
        assert [tool["name"] for tool in thread["params"]["dynamicTools"]] == [
            "taskenv_read",
            "taskenv_list",
            "taskenv_search",
        ]
        turn = transport.sent[3]
        assert turn["method"] == "turn/start"
        assert turn["params"] == {
            "threadId": "thr-1",
            "clientUserMessageId": "slot-1",
            "input": [{"type": "text", "text": "Inspect the task."}],
            "cwd": "/empty-host-jail",
            "environments": [],
            "runtimeWorkspaceRoots": [],
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "permissions": PERMISSION_PROFILE,
            "model": "gpt-test",
            "effort": "high",
            "outputSchema": {"type": "object", "additionalProperties": False},
        }

    asyncio.run(scenario())


def test_executor_slot_adds_exec_without_changing_host_or_task_roots() -> None:
    async def scenario() -> None:
        client, transport, _ = await _initialized_client(
            [_thread_response(), _turn_response(), *_completed_sequence()]
        )
        await client.run_slot(_slot(3))
        thread = transport.sent[2]["params"]
        assert thread["cwd"] == "/empty-host-jail"
        assert [tool["name"] for tool in thread["dynamicTools"]] == [
            "taskenv_exec",
            "taskenv_read",
            "taskenv_list",
            "taskenv_search",
        ]

    asyncio.run(scenario())


def test_standalone_baseline_has_fixed_total_budget_and_executor_tools() -> None:
    slot = StandaloneSlotConfig(
        slot_id="standalone-luna",
        model="gpt-test",
        prompt="Solve the task.",
        output_schema={"type": "object", "additionalProperties": False},
    )
    assert slot.actor == "executor"
    assert slot.position == 3
    assert slot.max_seconds == 1200

    async def scenario() -> None:
        client, transport, _ = await _initialized_client(
            [_thread_response(), _turn_response(), *_completed_sequence()]
        )
        await client.run_slot(slot)
        thread = transport.sent[2]["params"]
        assert [tool["name"] for tool in thread["dynamicTools"]] == [
            "taskenv_exec",
            "taskenv_read",
            "taskenv_list",
            "taskenv_search",
        ]

    asyncio.run(scenario())


def test_three_slot_deadlines_cannot_be_overridden() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        SlotConfig(
            position=3,
            slot_id="slot-3",
            actor="executor",
            model="gpt-test",
            prompt="Solve the task.",
            output_schema={"type": "object", "additionalProperties": False},
            max_seconds=1200,  # type: ignore[call-arg]
        )


def test_dynamic_exec_is_scoped_and_only_forwards_to_task_environment() -> None:
    call = {
        "method": "item/tool/call",
        "id": 60,
        "params": {
            "threadId": "thr-1",
            "turnId": "turn-1",
            "callId": "call-1",
            "namespace": None,
            "tool": "taskenv_exec",
            "arguments": {"argv": ["python", "-c", "print('hello world')"], "cwd": "/task/src", "timeoutSec": 7},
        },
    }

    async def scenario() -> None:
        client, transport, env = await _initialized_client(
            [_thread_response(), _turn_response(), call, *_completed_sequence()]
        )
        await client.run_slot(_slot(3))
        assert env.calls == [("python -c 'print('\"'\"'hello world'\"'\"')'", "/task/src", 7)]
        response = next(message for message in transport.sent if message.get("id") == 60)
        assert response["result"]["success"] is True
        assert json.loads(response["result"]["contentItems"][0]["text"]) == {
            "returnCode": 0,
            "stderr": "",
            "stdout": "ok",
            "truncated": False,
        }
        assert "must-not-be-serialized" not in response["result"]["contentItems"][0]["text"]

    asyncio.run(scenario())


def test_read_only_slots_reject_undeclared_exec_even_if_server_requests_it() -> None:
    call = {
        "method": "item/tool/call",
        "id": 60,
        "params": {
            "threadId": "thr-1",
            "turnId": "turn-1",
            "callId": "call-1",
            "namespace": None,
            "tool": "taskenv_exec",
            "arguments": {"argv": ["true"]},
        },
    }

    async def scenario() -> None:
        client, transport, env = await _initialized_client(
            [_thread_response(), _turn_response(), call]
        )
        with pytest.raises(AppServerProtocolError) as raised:
            await client.run_slot(_slot(1))
        assert raised.value.code == "tool_not_allowed_in_slot"
        assert env.calls == []
        assert transport.sent[-1]["method"] == "turn/interrupt"

    asyncio.run(scenario())


def test_read_only_tool_budget_returns_fixed_stop_signal_without_forwarding_extra_call() -> None:
    calls = []
    for index in range(9):
        calls.append(
            {
                "method": "item/tool/call",
                "id": 60 + index,
                "params": {
                    "threadId": "thr-1",
                    "turnId": "turn-1",
                    "callId": f"call-{index}",
                    "namespace": None,
                    "tool": "taskenv_read",
                    "arguments": {"path": f"file-{index}"},
                },
            }
        )

    async def scenario() -> None:
        client, transport, env = await _initialized_client(
            [_thread_response(), _turn_response(), *calls, *_completed_sequence()]
        )
        result = await client.run_slot(_slot(1))
        assert result.tool_call_count == 9
        assert len(env.calls) == 8
        response = next(message for message in transport.sent if message.get("id") == 68)
        assert response["result"]["success"] is False
        assert "tool_call_budget_exhausted" in response["result"]["contentItems"][0]["text"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("read", {"path": "README.md"}, "cat -- /task/README.md"),
        ("list", {"path": "."}, "find /task -maxdepth 2 -print"),
        ("search", {"query": "TODO value", "path": "src"}, "grep -RIn -- 'TODO value' /task/src"),
    ],
)
def test_fixed_read_list_search_argv(tool: str, arguments: dict[str, Any], expected: str) -> None:
    call = {
        "method": "item/tool/call",
        "id": 60,
        "params": {
            "threadId": "thr-1",
            "turnId": "turn-1",
            "callId": "call-1",
            "namespace": None,
            "tool": f"taskenv_{tool}",
            "arguments": arguments,
        },
    }

    async def scenario() -> None:
        client, _, env = await _initialized_client(
            [_thread_response(), _turn_response(), call, *_completed_sequence()]
        )
        await client.run_slot(_slot(3))
        assert env.calls[0][0] == expected

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"namespace": "browser"}, "invalid_tool_namespace"),
        ({"threadId": "another"}, "tool_scope_mismatch"),
        ({"turnId": "another"}, "tool_scope_mismatch"),
        ({"arguments": {"argv": ["true"], "extra": True}}, "invalid_tool_arguments"),
        ({"arguments": {"argv": ["true"], "cwd": "/host"}}, "task_path_escape"),
    ],
)
def test_malformed_or_cross_scope_tool_call_fails_closed(
    patch: dict[str, Any], code: str
) -> None:
    params = {
        "threadId": "thr-1",
        "turnId": "turn-1",
        "callId": "call-1",
        "namespace": None,
        "tool": "taskenv_exec",
        "arguments": {"argv": ["true"]},
    }
    params.update(patch)
    call = {"method": "item/tool/call", "id": 60, "params": params}

    async def scenario() -> None:
        client, transport, env = await _initialized_client(
            [_thread_response(), _turn_response(), call]
        )
        with pytest.raises(AppServerProtocolError) as raised:
            await client.run_slot(_slot(3))
        assert raised.value.code == code
        assert env.calls == []
        assert transport.sent[-1]["method"] == "turn/interrupt"
        assert transport.sent[-1]["params"] == {"threadId": "thr-1", "turnId": "turn-1"}
        error = next(message for message in transport.sent if message.get("id") == 60)
        assert error["error"]["data"]["code"] == code

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("method", "expected_result"),
    [
        ("item/commandExecution/requestApproval", {"decision": "decline"}),
        ("item/fileChange/requestApproval", {"decision": "decline"}),
        ("item/permissions/requestApproval", {"permissions": {}}),
        ("mcpServer/tool/call", None),
        ("browser/open", None),
        ("unknown/request", None),
    ],
)
def test_native_approval_mcp_browser_and_unknown_requests_interrupt(
    method: str, expected_result: dict[str, Any] | None
) -> None:
    request = {
        "method": method,
        "id": 70,
        "params": {"threadId": "thr-1", "turnId": "turn-1"},
    }

    async def scenario() -> None:
        client, transport, env = await _initialized_client(
            [_thread_response(), _turn_response(), request]
        )
        with pytest.raises(AppServerProtocolError) as raised:
            await client.run_slot(_slot())
        assert raised.value.code == "forbidden_server_request"
        assert env.calls == []
        reply = next(message for message in transport.sent if message.get("id") == 70)
        if expected_result is None:
            assert reply["error"]["code"] == -32601
        else:
            assert reply["result"] == expected_result
        assert transport.sent[-1]["method"] == "turn/interrupt"

    asyncio.run(scenario())


def test_native_item_notification_interrupts_without_host_execution() -> None:
    event = {
        "method": "item/started",
        "params": {
            "threadId": "thr-1",
            "turnId": "turn-1",
            "item": {"id": "native-1", "type": "commandExecution", "status": "inProgress"},
        },
    }

    async def scenario() -> None:
        client, transport, env = await _initialized_client(
            [_thread_response(), _turn_response(), event]
        )
        with pytest.raises(AppServerProtocolError) as raised:
            await client.run_slot(_slot())
        assert raised.value.code == "native_capability_observed"
        assert env.calls == []
        assert transport.sent[-1]["method"] == "turn/interrupt"

    asyncio.run(scenario())


def test_unknown_item_type_fails_closed() -> None:
    event = {
        "method": "item/started",
        "params": {
            "threadId": "thr-1",
            "turnId": "turn-1",
            "item": {"id": "native-1", "type": "futureNativeTool", "status": "inProgress"},
        },
    }

    async def scenario() -> None:
        client, transport, env = await _initialized_client(
            [_thread_response(), _turn_response(), event]
        )
        with pytest.raises(AppServerProtocolError) as raised:
            await client.run_slot(_slot())
        assert raised.value.code == "native_capability_observed"
        assert env.calls == []
        assert transport.sent[-1]["method"] == "turn/interrupt"

    asyncio.run(scenario())


def test_thread_start_response_must_prove_model_profile_ephemeral_and_empty_roots() -> None:
    response = _thread_response()
    response["result"]["thread"]["path"] = "/host/session.jsonl"

    async def scenario() -> None:
        client, _, _ = await _initialized_client([response])
        with pytest.raises(AppServerProtocolError) as raised:
            await client.run_slot(_slot())
        assert raised.value.code == "thread_not_ephemeral"

    asyncio.run(scenario())


def test_tool_output_cap_returns_failure_without_leaking_large_output() -> None:
    call = {
        "method": "item/tool/call",
        "id": 60,
        "params": {
            "threadId": "thr-1",
            "turnId": "turn-1",
            "callId": "call-1",
            "namespace": None,
            "tool": "taskenv_exec",
            "arguments": {"argv": ["true"]},
        },
    }

    async def scenario() -> None:
        env = FakeEnvironment(ExecResult("x" * 300, "", 0))
        client, transport, _ = await _initialized_client(
            [_thread_response(), _turn_response(), call, *_completed_sequence()],
            env,
            output_cap_bytes=256,
        )
        await client.run_slot(_slot(3))
        response = next(message for message in transport.sent if message.get("id") == 60)
        assert response["result"]["success"] is True
        text = response["result"]["contentItems"][0]["text"]
        assert len(text.encode()) <= 256
        assert "x" * 100 not in text
        assert json.loads(text)["truncated"] is True

    asyncio.run(scenario())


def test_task_environment_exception_is_fixed_and_does_not_leak_secret() -> None:
    class BrokenEnvironment(FakeEnvironment):
        async def exec(self, command: str, *, cwd: str, timeout_sec: int) -> ExecResult:
            raise RuntimeError("TOP-SECRET-CREDENTIAL")

    call = {
        "method": "item/tool/call",
        "id": 60,
        "params": {
            "threadId": "thr-1",
            "turnId": "turn-1",
            "callId": "call-1",
            "namespace": None,
            "tool": "taskenv_exec",
            "arguments": {"argv": ["true"]},
        },
    }

    async def scenario() -> None:
        client, transport, _ = await _initialized_client(
            [_thread_response(), _turn_response(), call, *_completed_sequence()], BrokenEnvironment()
        )
        await client.run_slot(_slot(3))
        response = next(message for message in transport.sent if message.get("id") == 60)
        serialized = json.dumps(response)
        assert response["result"]["success"] is False
        assert "task environment operation failed" in serialized
        assert "TOP-SECRET-CREDENTIAL" not in serialized

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("items", "code"),
    [
        ([], "terminal_message_count"),
        (
            [
                {"id": "a", "type": "agentMessage", "phase": "final_answer", "text": "{}"},
                {"id": "b", "type": "agentMessage", "phase": "final_answer", "text": "{}"},
            ],
            "terminal_message_count",
        ),
        (
            [
                {
                    "id": "a",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": '{"answer":""}',
                }
            ],
            "structured_output_schema_mismatch",
        ),
    ],
)
def test_terminal_message_and_structured_schema_are_enforced(
    items: list[dict[str, Any]], code: str
) -> None:
    slot = SlotConfig(
        position=1,
        slot_id="slot-1",
        actor="analyst",
        model="gpt-test",
        prompt="Inspect.",
        output_schema={
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "string", "minLength": 1}},
            "additionalProperties": False,
        },
    )

    async def scenario() -> None:
        client, transport, _ = await _initialized_client(
            [_thread_response(), _turn_response(), *_completed_sequence(items=items)]
        )
        with pytest.raises(AppServerProtocolError) as raised:
            await client.run_slot(slot)
        assert raised.value.code == code
        assert transport.sent[-1]["method"] == "turn/interrupt"

    asyncio.run(scenario())


def test_completed_item_produces_typed_output() -> None:
    terminal = {
        "id": "final-typed",
        "type": "agentMessage",
        "phase": "final_answer",
        "text": '{"answer":"done"}',
    }
    event = {
        "method": "item/completed",
        "params": {
            "threadId": "thr-1",
            "turnId": "turn-1",
            "completedAtMs": 1,
            "item": terminal,
        },
    }
    slot = SlotConfig(
        position=1,
        slot_id="slot-1",
        actor="analyst",
        model="gpt-test",
        prompt="Inspect.",
        output_schema={
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "string", "minLength": 1}},
            "additionalProperties": False,
        },
    )

    async def scenario() -> None:
        client, _, _ = await _initialized_client(
            [_thread_response(), _turn_response(), event, _completion(items=[terminal])]
        )
        result = await client.run_slot(slot)
        assert result.text == '{"answer":"done"}'
        assert result.typed_output == {"answer": "done"}

    asyncio.run(scenario())


def test_latest_scoped_token_usage_total_is_returned_without_double_counting() -> None:
    first = _token_usage_event(
        total=_token_counts(input_tokens=10, cached_input_tokens=3, output_tokens=4),
    )
    latest = _token_usage_event(
        total=_token_counts(
            input_tokens=25,
            cached_input_tokens=8,
            output_tokens=9,
            reasoning_output_tokens=5,
        ),
        # ``last`` is deliberately different: SlotResult reports cumulative
        # total, not the most recent increment.
        last=_token_counts(input_tokens=999, cached_input_tokens=0, output_tokens=1),
        model_context_window=None,
    )

    async def scenario() -> None:
        client, _, _ = await _initialized_client(
            [
                _thread_response(),
                _turn_response(),
                first,
                latest,
                *_completed_sequence(),
            ]
        )
        result = await client.run_slot(_slot())
        assert result.usage == {
            "input_tokens": 25,
            "cached_input_tokens": 8,
            "cache_write_input_tokens": 0,
            "output_tokens": 9,
            "reasoning_output_tokens": 5,
            "total_tokens": 34,
        }

    asyncio.run(scenario())


def test_optional_cache_write_tokens_from_v0147_schema_are_preserved() -> None:
    event = _token_usage_event()
    event["params"]["tokenUsage"]["total"]["cacheWriteInputTokens"] = 7
    event["params"]["tokenUsage"]["last"]["cacheWriteInputTokens"] = 2

    async def scenario() -> None:
        client, _, _ = await _initialized_client(
            [_thread_response(), _turn_response(), event, *_completed_sequence()]
        )
        result = await client.run_slot(_slot())
        assert result.usage is not None
        assert result.usage["cache_write_input_tokens"] == 7

    asyncio.run(scenario())


def test_slot_without_token_usage_notification_returns_none() -> None:
    async def scenario() -> None:
        client, _, _ = await _initialized_client(
            [_thread_response(), _turn_response(), *_completed_sequence()]
        )
        result = await client.run_slot(_slot())
        assert result.usage is None

    asyncio.run(scenario())


def test_null_model_context_window_is_valid_protocol_data() -> None:
    event = _token_usage_event()
    event["params"]["tokenUsage"]["modelContextWindow"] = None

    async def scenario() -> None:
        client, _, _ = await _initialized_client(
            [_thread_response(), _turn_response(), event, *_completed_sequence()]
        )
        result = await client.run_slot(_slot())
        assert result.usage is not None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "event",
    [
        _token_usage_event(thread_id="other-thread"),
        _token_usage_event(turn_id="other-turn"),
    ],
)
def test_token_usage_must_match_active_thread_and_turn(event: dict[str, Any]) -> None:
    async def scenario() -> None:
        client, transport, _ = await _initialized_client(
            [_thread_response(), _turn_response(), event]
        )
        with pytest.raises(AppServerProtocolError) as raised:
            await client.run_slot(_slot())
        assert raised.value.code == "token_usage_scope_mismatch"
        assert transport.sent[-1]["method"] == "turn/interrupt"

    asyncio.run(scenario())


def test_token_usage_before_active_turn_is_rejected() -> None:
    async def scenario() -> None:
        client, transport, _ = await _initialized_client(
            [_token_usage_event(), _thread_response()]
        )
        with pytest.raises(AppServerProtocolError) as raised:
            await client.run_slot(_slot())
        assert raised.value.code == "token_usage_out_of_phase"
        assert not any(message.get("method") == "turn/interrupt" for message in transport.sent)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event["params"].update({"extra": True}),
        lambda event: event["params"]["tokenUsage"].update({"extra": True}),
        lambda event: event["params"]["tokenUsage"].pop("last"),
        lambda event: event["params"]["tokenUsage"]["total"].pop("totalTokens"),
        lambda event: event["params"]["tokenUsage"]["last"].update({"inputTokens": -1}),
        lambda event: event["params"]["tokenUsage"]["total"].update({"outputTokens": True}),
        lambda event: event["params"]["tokenUsage"].update({"modelContextWindow": -1}),
    ],
)
def test_malformed_token_usage_fails_closed(mutate: Any) -> None:
    event = _token_usage_event()
    mutate(event)

    async def scenario() -> None:
        client, transport, _ = await _initialized_client(
            [_thread_response(), _turn_response(), event]
        )
        with pytest.raises(AppServerProtocolError) as raised:
            await client.run_slot(_slot())
        assert raised.value.code == "invalid_token_usage"
        assert transport.sent[-1]["method"] == "turn/interrupt"

    asyncio.run(scenario())


def test_terminal_turn_summary_cannot_replace_completed_item_event() -> None:
    async def scenario() -> None:
        client, transport, _ = await _initialized_client(
            [_thread_response(), _turn_response(), _completion()]
        )
        with pytest.raises(AppServerProtocolError) as raised:
            await client.run_slot(_slot())
        assert raised.value.code == "terminal_message_count"
        assert transport.sent[-1]["method"] == "turn/interrupt"

    asyncio.run(scenario())


def test_terminal_turn_may_omit_streamed_items() -> None:
    terminal = {
        "id": "final-streamed",
        "type": "agentMessage",
        "phase": "final_answer",
        "text": "{}",
    }
    event = {
        "method": "item/completed",
        "params": {
            "threadId": "thr-1",
            "turnId": "turn-1",
            "completedAtMs": 1,
            "item": terminal,
        },
    }

    async def scenario() -> None:
        completion = _completion(items=[])
        client, _, _ = await _initialized_client(
            [_thread_response(), _turn_response(), event, completion]
        )
        result = await client.run_slot(_slot())
        assert result.typed_output == {}

    asyncio.run(scenario())


def test_failed_turn_is_not_a_normal_completion() -> None:
    async def scenario() -> None:
        client, transport, _ = await _initialized_client(
            [_thread_response(), _turn_response(), *_completed_sequence("failed")]
        )
        with pytest.raises(AppServerProtocolError) as raised:
            await client.run_slot(_slot())
        assert raised.value.code == "turn_failed"
        assert transport.sent[-1]["method"] == "turn/interrupt"

    asyncio.run(scenario())


def test_jsonl_transport_round_trip_and_cap() -> None:
    class Writer:
        def __init__(self) -> None:
            self.data = b""

        def write(self, data: bytes) -> None:
            self.data += data

        async def drain(self) -> None:
            pass

    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"id":1,"result":{}}\n')
        reader.feed_eof()
        writer = Writer()
        transport = JsonlStreamTransport(reader, writer, max_bytes=64)
        assert await transport.receive() == {"id": 1, "result": {}}
        await transport.send({"method": "initialized"})
        assert writer.data == b'{"method":"initialized"}\n'
        with pytest.raises(AppServerProtocolError) as raised:
            await transport.send({"payload": "x" * 100})
        assert raised.value.code == "outgoing_message_too_large"

    asyncio.run(scenario())
