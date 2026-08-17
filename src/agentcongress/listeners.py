from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .llm.agent import AgentLoop, AgentTurnResult
from .llm.base import ChatProvider
from .llm.deepseek import DEFAULT_DEEPSEEK_MODEL
from .llm.tools import FloorRequestToolExecutor, floor_request_tool
from .models import FloorIntent, FloorRequest
from .streaming import ListenerProfile

_LISTENER_SYSTEM_PROMPT = (
    "You are a silent meeting listener. You never speak through this channel; you only decide "
    "whether to request the floor. Ground every decision in the shared context you are given "
    "and never fabricate meeting facts."
)


def _bounded_score(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def floor_observer_loop(
    provider: ChatProvider,
    *,
    listener_id: str = "listener",
    role: str = "meeting participant",
) -> AgentLoop:
    """Build the per-listener agent loop whose only tool is request_floor."""
    system = f"{_LISTENER_SYSTEM_PROMPT} You are {listener_id}, acting as {role}."
    return AgentLoop(
        provider=provider,
        tools=[floor_request_tool()],
        executor=FloorRequestToolExecutor(),
        system_prompt=system,
        max_tool_rounds=2,
    )


@dataclass(slots=True)
class ToolFloorObserver:
    """Provider-neutral listener evaluator.

    A listener only takes the floor by calling the ``request_floor`` tool
    through its own agent loop; anything else counts as abstaining.  The
    arguments of that tool call become the bounded ``FloorRequest``.
    """

    loops: dict[str, AgentLoop] = field(default_factory=dict)
    default_loop: AgentLoop | None = None

    def __post_init__(self) -> None:
        if not self.loops and self.default_loop is None:
            raise ValueError("ToolFloorObserver requires at least one agent loop")

    async def evaluate(self, profile: ListenerProfile, segment: str, context: str = "") -> FloorRequest | None:
        loop = self.loops.get(profile.agent_id, self.default_loop)
        if loop is None:
            return None
        tags = ", ".join(sorted(profile.capability_tags)) or "none"
        prompt = f"""You are a silent meeting listener named {profile.agent_id}, acting as {profile.role}. Your capabilities are: {tags}.

Shared context:
{context}

Evaluate whether you must interrupt after this completed speaker segment:
{segment}

Call the request_floor tool only for a concrete, material contribution that cannot wait for your own turn. If you do not call the tool, you abstain; answer with the single word "abstain"."""
        result = await loop.run(prompt)
        return self._parse_request(profile.agent_id, result)

    def _parse_request(self, agent_id: str, result: AgentTurnResult) -> FloorRequest | None:
        for execution in result.tool_executions:
            if execution.call.name != "request_floor":
                continue
            try:
                data = json.loads(execution.call.arguments or "{}")
            except json.JSONDecodeError:
                return None
            if not isinstance(data, dict) or not data.get("intent"):
                return None
            try:
                intent = FloorIntent(str(data["intent"]))
            except ValueError:
                intent = FloorIntent.BRIEF_INTERJECTION
            return FloorRequest(
                agent_id=agent_id,
                intent=intent,
                urgency=_bounded_score(data.get("urgency")),
                relevance=_bounded_score(data.get("relevance")),
                novelty=_bounded_score(data.get("novelty")),
                confidence=_bounded_score(data.get("confidence")),
                public_reason=str(data.get("reason", "listener contribution"))[:500],
            )
        return None


class DeepSeekFloorObserver:
    """DeepSeek-backed listener evaluator (compatible branch).

    Migrated from the original JSON-mode observer onto the generic tool
    observer: listeners still produce the same bounded ``FloorRequest``, but
    now through a ``request_floor`` tool call inside a DeepSeek agent loop.
    """

    def __init__(
        self,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        api_key_env: str = "DEEPSEEK_API_KEY",
        base_url: str | None = None,
    ) -> None:
        from .llm.registry import create_provider

        provider = create_provider("deepseek", model=model, api_key_env=api_key_env, base_url=base_url)
        self.observer = ToolFloorObserver(default_loop=floor_observer_loop(provider))

    async def evaluate(self, profile: ListenerProfile, segment: str, context: str = "") -> FloorRequest | None:
        return await self.observer.evaluate(profile, segment, context)
