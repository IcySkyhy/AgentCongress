"""A lightweight Codex-style agent loop shared by every meeting participant.

Codex sessions follow one shape: the model proposes tool calls, the harness
executes them inside a defined trust boundary, and the results feed the next
model request until a final text answer appears.  This module reproduces that
shape in-process so speakers and listeners alike can act on meeting state
instead of only chatting about it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .base import ChatMessage, ChatProvider, ToolCall, ToolSpec


class ToolExecutor(Protocol):
    async def run(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute one tool call and return a JSON-encoded string result."""


@dataclass(slots=True)
class ToolExecution:
    call: ToolCall
    result: str


@dataclass(slots=True)
class AgentTurnResult:
    text: str
    tool_rounds: int = 0
    tool_executions: list[ToolExecution] = field(default_factory=list)
    usage: dict[str, Any] | None = None


def _parse_arguments(arguments: str) -> dict[str, Any]:
    try:
        value = json.loads(arguments or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


@dataclass(slots=True)
class AgentLoop:
    """Model -> tools -> model until a final answer.

    Tool failures are captured as JSON errors and fed back to the model, so a
    bad tool call degrades gracefully instead of killing the turn.  When the
    tool budget is exhausted, one final completion without tools forces a text
    answer.
    """

    provider: ChatProvider
    tools: Sequence[ToolSpec] = ()
    executor: ToolExecutor | None = None
    system_prompt: str = ""
    max_tool_rounds: int = 8

    def __post_init__(self) -> None:
        if self.max_tool_rounds < 1:
            raise ValueError("max_tool_rounds must be at least one")
        if self.tools and self.executor is None:
            raise ValueError("an executor is required when tools are provided")

    async def run(self, prompt: str, history: Sequence[ChatMessage] = ()) -> AgentTurnResult:
        messages: list[ChatMessage] = (
            [ChatMessage("system", self.system_prompt)] + list(history) + [ChatMessage("user", prompt)]
        )
        executions: list[ToolExecution] = []
        rounds = 0
        while rounds < self.max_tool_rounds:
            response = await self.provider.complete(messages, self.tools if self.tools else ())
            if not response.tool_calls:
                return AgentTurnResult(response.content, rounds, executions, response.usage)
            rounds += 1
            messages.append(ChatMessage("assistant", response.content, tool_calls=response.tool_calls))
            for call in response.tool_calls:
                result = await self._execute(call)
                executions.append(ToolExecution(call, result))
                messages.append(ChatMessage("tool", result, tool_call_id=call.id, name=call.name))
        response = await self.provider.complete(messages, ())
        return AgentTurnResult(response.content, rounds, executions, response.usage)

    async def _execute(self, call: ToolCall) -> str:
        assert self.executor is not None
        try:
            return await self.executor.run(call.name, _parse_arguments(call.arguments))
        except Exception as error:
            return json.dumps({"error": f"{type(error).__name__}: {error}"})


class DialogueAgentAdapter:
    """Adapts an AgentLoop to the discussion ``stream_turn`` protocol."""

    def __init__(self, loop: AgentLoop):
        self.loop = loop
        self.last_result: AgentTurnResult | None = None

    def stream_turn(self, prompt: str) -> AsyncIterator[str]:
        async def stream() -> AsyncIterator[str]:
            result = await self.loop.run(prompt)
            self.last_result = result
            if result.text:
                yield result.text

        return stream()
