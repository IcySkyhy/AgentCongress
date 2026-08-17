"""Provider registry: protocol names, defaults, and factory."""

from __future__ import annotations

import os

from .base import ChatProvider
from .deepseek import DEFAULT_DEEPSEEK_BASE_URL, DEFAULT_DEEPSEEK_MODEL, DeepSeekProvider
from .providers import AnthropicProvider, OpenAIChatProvider, OpenAIResponsesProvider

_DEFAULTS: dict[str, dict[str, str]] = {
    "openai-chat": {"model": "gpt-4o-mini", "api_key_env": "OPENAI_API_KEY"},
    "openai-responses": {"model": "gpt-4o-mini", "api_key_env": "OPENAI_API_KEY"},
    "anthropic": {"model": "claude-3-5-haiku-latest", "api_key_env": "ANTHROPIC_API_KEY"},
    "deepseek": {"model": DEFAULT_DEEPSEEK_MODEL, "api_key_env": "DEEPSEEK_API_KEY"},
}

PROTOCOLS = tuple(_DEFAULTS)


def provider_defaults(protocol: str) -> dict[str, str]:
    if protocol not in _DEFAULTS:
        raise ValueError(f"unknown provider protocol: {protocol} (expected one of {', '.join(sorted(_DEFAULTS))})")
    return dict(_DEFAULTS[protocol])


def create_provider(
    protocol: str,
    *,
    model: str | None = None,
    api_key_env: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 120.0,
    max_tokens: int | None = None,
) -> ChatProvider:
    """Build a provider from a protocol name; API keys come from the environment."""
    defaults = provider_defaults(protocol)
    model = model or defaults["model"]
    api_key_env = api_key_env or defaults["api_key_env"]
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ValueError(f"{api_key_env} is required for the {protocol} provider")
    if protocol == "openai-chat":
        return OpenAIChatProvider(
            model,
            api_key,
            base_url or "https://api.openai.com/v1",
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )
    if protocol == "openai-responses":
        return OpenAIResponsesProvider(
            model,
            api_key,
            base_url or "https://api.openai.com/v1",
            timeout_seconds=timeout_seconds,
        )
    if protocol == "anthropic":
        return AnthropicProvider(
            model,
            api_key,
            base_url or "https://api.anthropic.com/v1",
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens or 4096,
        )
    if protocol == "deepseek":
        return DeepSeekProvider(
            model,
            api_key,
            base_url or DEFAULT_DEEPSEEK_BASE_URL,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )
    raise ValueError(f"unknown provider protocol: {protocol}")
