import asyncio
from pathlib import Path

from agentcongress.models import FloorIntent, FloorRequest
from agentcongress.orchestration import run_speaking_turn
from agentcongress.runtime import CongressRuntime
from agentcongress.streaming import ListenerProfile


class _Observer:
    async def evaluate(self, profile: ListenerProfile, segment: str) -> FloorRequest | None:
        if profile.agent_id == "reviewer" and "security" in segment:
            return FloorRequest("reviewer", FloorIntent.REPLACE_SPEAKER, 1, 1, 1, 1, "security issue")
        return None


async def _stream():
    yield "We should skip security."
    yield " This should not be spoken."


def test_listener_can_take_floor_at_safe_segment(tmp_path: Path) -> None:
    runtime = CongressRuntime("m", tmp_path / "events.db", ["architect", "implementer", "reviewer"])
    runtime.start("architect", "implementer")
    winner = asyncio.run(run_speaking_turn(runtime, _stream(), [ListenerProfile("reviewer", frozenset({"security"}))], _Observer()))
    assert winner is not None
    assert winner.agent_id == "reviewer"
    assert runtime.state.speaker_id == "reviewer"
    assert [event.type for event in runtime.store.replay("m")] == ["meeting.started", "speech.segment_committed", "floor.requested", "floor.granted"]
    runtime.close()
