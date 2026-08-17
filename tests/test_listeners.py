import asyncio
import json

from agentcongress.llm.agent import AgentTurnResult, ToolExecution
from agentcongress.llm.base import ToolCall
from agentcongress.listeners import ToolFloorObserver, floor_observer_loop
from agentcongress.streaming import ListenerProfile


class _Loop:
    """Fake agent loop: returns scripted tool executions or plain text."""

    def __init__(self, result: AgentTurnResult):
        self._result = result
        self.prompts: list[str] = []

    async def run(self, prompt: str, history=()):
        self.prompts.append(prompt)
        return self._result


def _request_result(arguments: dict) -> AgentTurnResult:
    return AgentTurnResult(
        text="",
        tool_executions=[ToolExecution(ToolCall("call-1", "request_floor", json.dumps(arguments)), '{"received": true}')],
    )


def test_observer_converts_request_floor_tool_call_to_bounded_floor_request() -> None:
    loop = _Loop(
        _request_result(
            {
                "intent": "replace_speaker",
                "urgency": 1.2,
                "relevance": 0.9,
                "novelty": 0.8,
                "confidence": 0.7,
                "reason": "security gap",
            }
        )
    )
    observer = ToolFloorObserver(loops={"reviewer": loop})
    request = asyncio.run(observer.evaluate(ListenerProfile("reviewer", frozenset({"security"})), "We can skip security."))
    assert request is not None
    assert request.agent_id == "reviewer"
    assert request.intent == "replace_speaker"
    assert request.urgency == 1.0
    assert request.public_reason == "security gap"
    assert "We can skip security." in loop.prompts[0]


def test_observer_abstains_when_no_floor_tool_was_called() -> None:
    loop = _Loop(AgentTurnResult(text="abstain"))
    observer = ToolFloorObserver(loops={"reviewer": loop})
    request = asyncio.run(observer.evaluate(ListenerProfile("reviewer"), "A routine update."))
    assert request is None


def test_observer_requires_a_loop_and_uses_default_loop() -> None:
    try:
        ToolFloorObserver(loops={})
    except ValueError as error:
        assert "at least one agent loop" in str(error)
    else:
        raise AssertionError("expected ValueError for an empty observer")

    loop = _Loop(AgentTurnResult(text="abstain"))
    observer = ToolFloorObserver(loops={}, default_loop=loop)
    assert asyncio.run(observer.evaluate(ListenerProfile("reviewer"), "Routine.")) is None


def test_floor_observer_loop_uses_request_floor_tool_only() -> None:
    class _Provider:
        name = "fake"

    loop = floor_observer_loop(_Provider(), listener_id="reviewer", role="checks changes")
    assert loop.max_tool_rounds == 2
    assert [tool.name for tool in loop.tools] == ["request_floor"]
    assert "reviewer" in loop.system_prompt
