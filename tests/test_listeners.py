import asyncio

from agentcongress.listeners import DeepSeekFloorObserver
from agentcongress.streaming import ListenerProfile


class _JsonAdapter:
    async def complete(self, prompt: str, *, json_output: bool = False) -> str:
        assert json_output
        assert "reviewer" in prompt
        return '{"request": true, "intent": "replace_speaker", "urgency": 1.2, "relevance": 0.9, "novelty": 0.8, "confidence": 0.7, "reason": "security gap"}'


def test_deepseek_observer_converts_json_to_bounded_floor_request() -> None:
    observer = DeepSeekFloorObserver(_JsonAdapter())
    request = asyncio.run(observer.evaluate(ListenerProfile("reviewer", frozenset({"security"})), "We can skip security."))
    assert request is not None
    assert request.agent_id == "reviewer"
    assert request.intent == "replace_speaker"
    assert request.urgency == 1.0
    assert request.public_reason == "security gap"
