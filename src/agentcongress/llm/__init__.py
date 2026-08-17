"""Provider-neutral LLM layer for AgentCongress.

This package implements a small, dependency-free abstraction over three wire
protocols (OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages)
plus a lightweight Codex-style agent loop that gives every meeting participant
tool-calling ability.
"""

from .agent import AgentLoop, AgentTurnResult, DialogueAgentAdapter, ToolExecution
from .base import (
    ChatMessage,
    ChatProvider,
    ProviderError,
    ProviderResponse,
    ToolCall,
    ToolSpec,
)
from .providers import AnthropicProvider, OpenAIChatProvider, OpenAIResponsesProvider
from .registry import PROTOCOLS, create_provider, provider_defaults

__all__ = [
    "AgentLoop",
    "AgentTurnResult",
    "AnthropicProvider",
    "ChatMessage",
    "ChatProvider",
    "DialogueAgentAdapter",
    "OpenAIChatProvider",
    "OpenAIResponsesProvider",
    "PROTOCOLS",
    "ProviderError",
    "ProviderResponse",
    "ToolCall",
    "ToolExecution",
    "ToolSpec",
    "create_provider",
    "provider_defaults",
]
