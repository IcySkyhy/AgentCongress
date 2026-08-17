import json

import pytest

from agentcongress.llm.base import ChatMessage, ProviderError, ToolCall, ToolSpec
from agentcongress.llm.providers import (
    AnthropicProvider,
    OpenAIChatProvider,
    OpenAIResponsesProvider,
    anthropic_messages,
    openai_chat_messages,
    openai_responses_input,
)

_TOOL = ToolSpec("inspect", "Inspect state.", {"type": "object", "properties": {"path": {"type": "string"}}})
_CALL = ToolCall("call-1", "inspect", json.dumps({"path": "src"}))


def _messages() -> list[ChatMessage]:
    return [
        ChatMessage("system", "Be concise."),
        ChatMessage("user", "Check the repo."),
        ChatMessage("assistant", "Let me look.", tool_calls=[_CALL]),
        ChatMessage("tool", '{"ok": true}', tool_call_id="call-1", name="inspect"),
    ]


def test_openai_chat_message_translation_round_trips_tool_calls() -> None:
    converted = openai_chat_messages(_messages())
    assert converted == [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "Check the repo."},
        {
            "role": "assistant",
            "content": "Let me look.",
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "inspect", "arguments": '{"path": "src"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"ok": true}'},
    ]


def test_openai_responses_input_emits_function_call_items() -> None:
    items = openai_responses_input(_messages())
    assert items[0] == {"role": "system", "content": [{"type": "input_text", "text": "Be concise."}]}
    assert items[1] == {"role": "user", "content": [{"type": "input_text", "text": "Check the repo."}]}
    assert items[2] == {"role": "assistant", "content": [{"type": "output_text", "text": "Let me look."}]}
    assert items[3] == {"type": "function_call", "call_id": "call-1", "name": "inspect", "arguments": '{"path": "src"}'}
    assert items[4] == {"type": "function_call_output", "call_id": "call-1", "output": '{"ok": true}'}


def test_anthropic_translation_extracts_system_and_merges_tool_results() -> None:
    system, converted = anthropic_messages(
        _messages() + [ChatMessage("tool", '{"ok": false}', tool_call_id="call-2", name="inspect")]
    )
    assert system == "Be concise."
    assert converted == [
        {"role": "user", "content": "Check the repo."},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "id": "call-1", "name": "inspect", "input": {"path": "src"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call-1", "content": '{"ok": true}'},
                {"type": "tool_result", "tool_use_id": "call-2", "content": '{"ok": false}'},
            ],
        },
    ]


async def _run(provider, messages=_messages(), tools=()):
    return await provider.complete(messages, tools)


def test_openai_chat_provider_parses_content_and_tool_calls(monkeypatch) -> None:
    captured = {}

    async def fake_post(url, headers, payload, timeout):
        captured.update(url=url, headers=headers, payload=payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"id": "c1", "type": "function", "function": {"name": "inspect", "arguments": '{"path": "a"}'}}
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"total_tokens": 7},
        }

    monkeypatch.setattr("agentcongress.llm.providers._post_json_async", fake_post)
    provider = OpenAIChatProvider("m", "key")
    response = asyncio_run(_run(provider, _messages(), [_TOOL]))
    assert response.content == ""
    assert response.tool_calls == [ToolCall("c1", "inspect", '{"path": "a"}')]
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["payload"]["tools"][0]["type"] == "function"


def test_openai_responses_provider_parses_output_items(monkeypatch) -> None:
    async def fake_post(url, headers, payload, timeout):
        return {
            "output": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done."}]},
                {"type": "function_call", "call_id": "c9", "name": "inspect", "arguments": '{"path": "b"}'},
            ]
        }

    monkeypatch.setattr("agentcongress.llm.providers._post_json_async", fake_post)
    provider = OpenAIResponsesProvider("m", "key")
    response = asyncio_run(_run(provider, _messages(), [_TOOL]))
    assert response.content == "Done."
    assert response.tool_calls == [ToolCall("c9", "inspect", '{"path": "b"}')]


def test_anthropic_provider_parses_tool_use_blocks(monkeypatch) -> None:
    captured = {}

    async def fake_post(url, headers, payload, timeout):
        captured.update(url=url, headers=headers, payload=payload)
        return {
            "content": [
                {"type": "text", "text": "Checking."},
                {"type": "tool_use", "id": "t1", "name": "inspect", "input": {"path": "c"}},
            ],
            "stop_reason": "tool_use",
        }

    monkeypatch.setattr("agentcongress.llm.providers._post_json_async", fake_post)
    provider = AnthropicProvider("claude-x", "key")
    response = asyncio_run(_run(provider, _messages(), [_TOOL]))
    assert response.content == "Checking."
    assert response.tool_calls == [ToolCall("t1", "inspect", '{"path": "c"}')]
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "key"
    assert captured["payload"]["max_tokens"] == 4096
    assert captured["payload"]["system"] == "Be concise."
    assert captured["payload"]["tools"][0]["input_schema"]["type"] == "object"


def test_http_errors_surface_as_provider_error(monkeypatch) -> None:
    import urllib.error

    def fake_open(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr("agentcongress.llm.providers.urllib.request.urlopen", fake_open)
    provider = OpenAIChatProvider("m", "bad-key")
    with pytest.raises(ProviderError, match="401"):
        asyncio_run(_run(provider))


def asyncio_run(coroutine):
    import asyncio

    return asyncio.run(coroutine)
