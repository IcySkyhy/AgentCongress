"""Protocol-neutral types shared by every provider and the agent loop."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderError(RuntimeError):
    """Raised when an upstream provider request fails."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool declaration. ``parameters`` is a JSON Schema for the arguments.

    OpenAI Chat Completions and the Responses API accept this schema directly;
    the Anthropic provider passes it through as ``input_schema``.
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A normalized tool invocation requested by a model."""

    id: str
    name: str
    arguments: str  # JSON-encoded object string


@dataclass(slots=True)
class ChatMessage:
    """One message in a provider conversation.

    Roles: ``system``, ``user``, ``assistant`` (optionally carrying
    ``tool_calls``), and ``tool`` (a tool result with ``tool_call_id``).
    """

    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(slots=True)
class ProviderResponse:
    """Normalized completion: text and/or tool calls."""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    raw: Any = None


class ChatProvider(Protocol):
    """A single non-streaming completion request against one protocol."""

    name: str

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
    ) -> ProviderResponse: ...
