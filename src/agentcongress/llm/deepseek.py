"""DeepSeek preset for the generic LLM layer (compatible branch).

DeepSeek speaks the OpenAI Chat Completions protocol, so this preset only pins
the endpoint, the API-key environment variable, and the default model.  It
lives on the ``compatible`` branch so that ``main`` stays provider-neutral;
the same service also works on ``main`` via ``--provider openai-chat
--base-url https://api.deepseek.com``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .providers import OpenAIChatProvider

DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


@dataclass(slots=True)
class DeepSeekProvider(OpenAIChatProvider):
    """OpenAI Chat Completions provider pinned to the DeepSeek endpoint."""

    name = "deepseek"

    model: str = DEFAULT_DEEPSEEK_MODEL
    api_key: str = ""
    base_url: str = DEFAULT_DEEPSEEK_BASE_URL
