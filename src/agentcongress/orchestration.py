from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol

from .models import FloorRequest
from .runtime import CongressRuntime
from .streaming import ListenerGate, ListenerProfile, SentenceSegmenter


class SegmentObserver(Protocol):
    async def evaluate(self, profile: ListenerProfile, segment: str, context: str = "") -> FloorRequest | None: ...


async def _evaluate_listener(observer: SegmentObserver, profile: ListenerProfile, segment: str, context: str) -> FloorRequest | None:
    try:
        return await observer.evaluate(profile, segment, context)
    except TypeError as error:
        # Maintain compatibility with external observers written for the first
        # two-argument protocol while the shared context capability is adopted.
        try:
            return await observer.evaluate(profile, segment)  # type: ignore[call-arg]
        except TypeError:
            raise error


async def run_speaking_turn(
    runtime: CongressRuntime,
    stream: AsyncIterator[str],
    listeners: list[ListenerProfile],
    observer: SegmentObserver,
    *,
    gate: ListenerGate | None = None,
    evaluation_timeout_seconds: float = 30.0,
) -> FloorRequest | None:
    """Commit safe segments and stop a turn only when a listener wins the floor."""
    gate = gate or ListenerGate()
    segmenter = SentenceSegmenter()
    segment_number = 0
    async for chunk in stream:
        for segment in segmenter.push(chunk):
            segment_number += 1
            runtime.commit_segment(segment)
            candidates = [profile for profile in listeners if profile.agent_id != runtime.state.speaker_id and gate.should_evaluate(profile, segment, segment_number)]
            if not candidates:
                continue
            context = f"Blackboard:\n{runtime.blackboard_context()}\n\nRecent discussion:\n{runtime.recent_transcript()}"
            responses = await asyncio.gather(
                *(asyncio.wait_for(_evaluate_listener(observer, profile, segment, context), timeout=evaluation_timeout_seconds) for profile in candidates),
                return_exceptions=True,
            )
            for profile, response in zip(candidates, responses, strict=True):
                if isinstance(response, Exception):
                    runtime.record("listener.evaluation_failed", profile.agent_id, {"error": str(response)[:500], "segment_number": segment_number})
            requests = [response for response in responses if isinstance(response, FloorRequest)]
            for request in requests:
                runtime.request_floor(request)
            winner = runtime.floor.select(requests)
            if winner is not None:
                runtime.resolve_floor(requests)
                return winner
    remaining = segmenter.flush()
    if remaining:
        runtime.commit_segment(remaining)
    return None
