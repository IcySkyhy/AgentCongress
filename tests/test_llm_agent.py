import asyncio
import json

import pytest

from agentcongress.llm.agent import AgentLoop, DialogueAgentAdapter, ToolExecution
from agentcongress.llm.base import ChatMessage, ProviderResponse, ToolCall, ToolSpec

_TOOL = ToolSpec("inspect", "Inspect state.", {"type": "object", "properties": {"path": {"type": "string"}}})


class _ScriptedProvider:
    """Returns scripted responses in order and records every request."""

    def __init__(self, *responses: ProviderResponse):
        self.responses = list(responses)
        self.requests: list[tuple[list[ChatMessage], list[ToolSpec]]] = []

    async def complete(self, messages, tools=()):
        self.requests.append((list(messages), list(tools)))
        if not self.responses:
            return ProviderResponse("")
        return self.responses.pop(0)


class _Executor:
    def __init__(self, results: dict[str, str] | None = None):
        self.results = results or {}
        self.calls: list[tuple[str, dict]] = []

    async def run(self, name, arguments):
        self.calls.append((name, arguments))
        return self.results.get(name, '{"ok": true}')


def test_agent_loop_runs_tools_until_final_text() -> None:
    provider = _ScriptedProvider(
        ProviderResponse("Checking.", tool_calls=[ToolCall("c1", "inspect", json.dumps({"path": "src"}))]),
        ProviderResponse("All clear."),
    )
    executor = _Executor()
    loop = AgentLoop(provider, [_TOOL], executor, system_prompt="Auditor.")
    result = asyncio.run(loop.run("Inspect the tree."))
    assert result.text == "All clear."
    assert result.tool_rounds == 1
    assert [execution.call.name for execution in result.tool_executions] == ["inspect"]
    assert executor.calls == [("inspect", {"path": "src"})]
    # The first request carried the tool; the final one did not need to.
    assert len(provider.requests) == 2
    system, user = provider.requests[0][0][0], provider.requests[0][0][1]
    assert system.role == "system" and system.content == "Auditor."
    assert user.role == "user" and user.content == "Inspect the tree."
    assert [tool.name for tool in provider.requests[0][1]] == ["inspect"]
    # Tool result messages are fed back with the call id.
    tool_message = provider.requests[1][0][-1]
    assert tool_message.role == "tool" and tool_message.tool_call_id == "c1"


def test_agent_loop_captures_executor_failures_as_tool_results() -> None:
    provider = _ScriptedProvider(
        ProviderResponse("", tool_calls=[ToolCall("c1", "inspect", "{}")]),
        ProviderResponse("Recovered."),
    )

    class _Broken:
        async def run(self, name, arguments):
            raise RuntimeError("boom")

    loop = AgentLoop(provider, [_TOOL], _Broken())
    result = asyncio.run(loop.run("Go."))
    assert result.text == "Recovered."
    assert json.loads(result.tool_executions[0].result)["error"].startswith("RuntimeError")


def test_agent_loop_forces_text_answer_after_tool_budget() -> None:
    provider = _ScriptedProvider(
        ProviderResponse("", tool_calls=[ToolCall("c1", "inspect", "{}")]),
        ProviderResponse("", tool_calls=[ToolCall("c2", "inspect", "{}")]),
        ProviderResponse("Final answer."),
    )
    loop = AgentLoop(provider, [_TOOL], _Executor(), max_tool_rounds=2)
    result = asyncio.run(loop.run("Go."))
    assert result.tool_rounds == 2
    assert result.text == "Final answer."
    # The exhausted-budget completion must be made without tools.
    assert provider.requests[-1][1] == []


def test_agent_loop_rejects_tools_without_executor_and_bad_budget() -> None:
    with pytest.raises(ValueError, match="executor"):
        AgentLoop(_ScriptedProvider(), [_TOOL])
    with pytest.raises(ValueError, match="max_tool_rounds"):
        AgentLoop(_ScriptedProvider(), max_tool_rounds=0)


def test_dialogue_agent_adapter_streams_final_text() -> None:
    provider = _ScriptedProvider(ProviderResponse("We should use an event log. SQLite works."))
    adapter = DialogueAgentAdapter(AgentLoop(provider))
    collected = asyncio.run(collect_stream(adapter.stream_turn("Propose a design.")))
    assert collected == "We should use an event log. SQLite works."
    assert adapter.last_result is not None and adapter.last_result.tool_rounds == 0


async def collect_stream(stream):
    return "".join([chunk async for chunk in stream])
