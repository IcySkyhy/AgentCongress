"""Dependency-free HTTP providers for the three supported protocols.

Only ``urllib.request`` from the standard library is used; requests run in a
worker thread so the event loop stays responsive.  API keys are passed in per
request and are never persisted to disk.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .base import ChatMessage, ProviderError, ProviderResponse, ToolCall, ToolSpec


def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:1000]
        raise ProviderError(f"provider request failed ({error.code}): {detail}") from error
    except urllib.error.URLError as error:
        raise ProviderError(f"provider request failed: {error.reason}") from error


async def _post_json_async(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    return await asyncio.to_thread(_post_json, url, headers, payload, timeout_seconds)


# ---------------------------------------------------------------------------
# OpenAI Chat Completions
# ---------------------------------------------------------------------------


def openai_chat_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role in {"system", "user"}:
            converted.append({"role": message.role, "content": message.content})
        elif message.role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": message.content or None}
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in message.tool_calls
                ]
            converted.append(item)
        elif message.role == "tool":
            converted.append(
                {"role": "tool", "tool_call_id": message.tool_call_id or "", "content": message.content}
            )
        else:
            raise ProviderError(f"unsupported message role: {message.role}")
    return converted


def openai_chat_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
        }
        for tool in tools
    ]


@dataclass(slots=True)
class OpenAIChatProvider:
    name = "openai-chat"

    model: str
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 120.0
    max_tokens: int | None = None
    temperature: float | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", **self.extra_headers}

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
    ) -> ProviderResponse:
        payload: dict[str, Any] = {"model": self.model, "messages": openai_chat_messages(messages)}
        if tools:
            payload["tools"] = openai_chat_tools(tools)
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        data = await _post_json_async(
            f"{self.base_url.rstrip('/')}/chat/completions",
            self._headers(),
            payload,
            self.timeout_seconds,
        )
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get("message") or {}
        content = message.get("content")
        tool_calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            tool_calls.append(
                ToolCall(
                    call.get("id") or "",
                    function.get("name") or "",
                    function.get("arguments") or "{}",
                )
            )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        return ProviderResponse(
            content if isinstance(content, str) else "",
            tool_calls,
            choice.get("finish_reason"),
            usage,
            data,
        )


# ---------------------------------------------------------------------------
# OpenAI Responses API
# ---------------------------------------------------------------------------


def openai_responses_input(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            items.append({"role": "system", "content": [{"type": "input_text", "text": message.content}]})
        elif message.role == "user":
            items.append({"role": "user", "content": [{"type": "input_text", "text": message.content}]})
        elif message.role == "assistant":
            parts: list[dict[str, Any]] = []
            if message.content:
                parts.append({"type": "output_text", "text": message.content})
            items.append({"role": "assistant", "content": parts})
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                )
        elif message.role == "tool":
            items.append(
                {"type": "function_call_output", "call_id": message.tool_call_id or "", "output": message.content}
            )
        else:
            raise ProviderError(f"unsupported message role: {message.role}")
    return items


def openai_responses_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "name": tool.name, "description": tool.description, "parameters": tool.parameters}
        for tool in tools
    ]


@dataclass(slots=True)
class OpenAIResponsesProvider:
    name = "openai-responses"

    model: str
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 120.0
    extra_headers: dict[str, str] = field(default_factory=dict)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
    ) -> ProviderResponse:
        payload: dict[str, Any] = {"model": self.model, "input": openai_responses_input(messages)}
        if tools:
            payload["tools"] = openai_responses_tools(tools)
        data = await _post_json_async(
            f"{self.base_url.rstrip('/')}/responses",
            {"Authorization": f"Bearer {self.api_key}", **self.extra_headers},
            payload,
            self.timeout_seconds,
        )
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        output = data.get("output") if isinstance(data.get("output"), list) else []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        text = part.get("text")
                        if isinstance(text, str):
                            text_parts.append(text)
            elif item.get("type") == "function_call":
                arguments = item.get("arguments")
                tool_calls.append(
                    ToolCall(
                        item.get("call_id") or "",
                        item.get("name") or "",
                        arguments if isinstance(arguments, str) else json.dumps(arguments or {}),
                    )
                )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        return ProviderResponse("".join(text_parts), tool_calls, None, usage, data)


# ---------------------------------------------------------------------------
# Anthropic Messages (Claude)
# ---------------------------------------------------------------------------


def _json_arguments(arguments: str) -> dict[str, Any]:
    try:
        value = json.loads(arguments or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _is_tool_result_block(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == "tool_result"


def _merge_tool_result_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Claude requires consecutive tool results to share one user message."""
    merged: list[dict[str, Any]] = []
    for message in messages:
        content = message["content"]
        is_tool_result = (
            message["role"] == "user"
            and isinstance(content, list)
            and bool(content)
            and all(_is_tool_result_block(part) for part in content)
        )
        if is_tool_result and merged:
            previous = merged[-1]
            previous_content = previous["content"]
            if (
                previous["role"] == "user"
                and isinstance(previous_content, list)
                and bool(previous_content)
                and all(_is_tool_result_block(part) for part in previous_content)
            ):
                previous_content.extend(content)
                continue
        merged.append(message)
    return merged


def anthropic_messages(messages: Sequence[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(system, converted)`` for a Messages API request."""
    system_blocks = [message.content for message in messages if message.role == "system"]
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "user":
            converted.append({"role": "user", "content": message.content})
        elif message.role == "assistant":
            parts: list[dict[str, Any]] = []
            if message.content:
                parts.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                parts.append(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": _json_arguments(call.arguments)}
                )
            converted.append({"role": "assistant", "content": parts})
        elif message.role == "tool":
            converted.append(
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": message.tool_call_id or "", "content": message.content}],
                }
            )
        else:
            raise ProviderError(f"unsupported message role: {message.role}")
    return "\n\n".join(system_blocks), _merge_tool_result_messages(converted)


def anthropic_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}
        for tool in tools
    ]


@dataclass(slots=True)
class AnthropicProvider:
    name = "anthropic"

    model: str
    api_key: str
    base_url: str = "https://api.anthropic.com/v1"
    timeout_seconds: float = 120.0
    max_tokens: int = 4096
    extra_headers: dict[str, str] = field(default_factory=dict)

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", **self.extra_headers}

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
    ) -> ProviderResponse:
        system, converted = anthropic_messages(messages)
        payload: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "messages": converted}
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = anthropic_tools(tools)
        data = await _post_json_async(
            f"{self.base_url.rstrip('/')}/messages",
            self._headers(),
            payload,
            self.timeout_seconds,
        )
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        block.get("id") or "",
                        block.get("name") or "",
                        json.dumps(block.get("input") or {}),
                    )
                )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        return ProviderResponse("".join(text_parts), tool_calls, data.get("stop_reason"), usage, data)
