from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .adapters import OpenAICompatibleDialogueAdapter
from .models import FloorIntent, FloorRequest
from .streaming import ListenerProfile


def _bounded_score(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _parse_json_object(content: str) -> dict[str, Any] | None:
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


@dataclass(slots=True)
class DeepSeekFloorObserver:
    """Second-stage listener evaluator backed by a JSON-mode chat completion."""

    adapter: OpenAICompatibleDialogueAdapter

    async def evaluate(self, profile: ListenerProfile, segment: str, context: str = "") -> FloorRequest | None:
        tags = ", ".join(sorted(profile.capability_tags)) or "none"
        prompt = f"""You are a silent meeting listener named {profile.agent_id}, acting as {profile.role}. Your capabilities are: {tags}.

Shared context:
{context}

Evaluate whether you must interrupt after this completed speaker segment:
{segment}

Return exactly one JSON object with these keys: request (boolean), intent (brief_interjection|replace_speaker|replace_addressee), urgency (0..1), relevance (0..1), novelty (0..1), confidence (0..1), reason (short public string). Request the floor only for a concrete, material contribution; otherwise set request to false."""
        data = _parse_json_object(await self.adapter.complete(prompt, json_output=True))
        if not data or not data.get("request"):
            return None
        try:
            intent = FloorIntent(str(data.get("intent", FloorIntent.BRIEF_INTERJECTION)))
        except ValueError:
            intent = FloorIntent.BRIEF_INTERJECTION
        return FloorRequest(
            agent_id=profile.agent_id,
            intent=intent,
            urgency=_bounded_score(data.get("urgency")),
            relevance=_bounded_score(data.get("relevance")),
            novelty=_bounded_score(data.get("novelty")),
            confidence=_bounded_score(data.get("confidence")),
            public_reason=str(data.get("reason", "listener contribution"))[:500],
        )
