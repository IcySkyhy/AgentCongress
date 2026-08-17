from pathlib import Path

from agentcongress.models import FloorIntent, FloorRequest
from agentcongress.runtime import CongressRuntime


def test_brief_interjection_restores_prior_speaker_and_blackboard_replays(tmp_path: Path) -> None:
    database = tmp_path / "events.db"
    runtime = CongressRuntime("m", database, ["architect", "reviewer", "implementer"])
    runtime.start("architect", "reviewer")
    request = FloorRequest("implementer", FloorIntent.BRIEF_INTERJECTION, 1, 1, 1, 1, "missing edge case")
    runtime.resolve_floor([request])
    assert runtime.state.speaker_id == "implementer"
    runtime.add_blackboard("risk", "The API needs an empty-input case.", "implementer", evidence=["unit test pending"])
    runtime.complete_brief_interjection()
    assert runtime.state.speaker_id == "architect"
    runtime.close()
    recovered = CongressRuntime.resume("m", database, ["architect", "reviewer", "implementer"])
    assert "empty-input" in recovered.blackboard_context()
    assert "unit test pending" in recovered.blackboard_context()
    assert recovered.state.speaker_id == "architect"
    recovered.close()


def test_blackboard_context_is_bounded_and_prioritizes_recent_evidence(tmp_path: Path) -> None:
    runtime = CongressRuntime("m", tmp_path / "events.db", ["a", "b"])
    runtime.add_blackboard("old", "x" * 2_000, "a", evidence=["old evidence"])
    runtime.add_blackboard("new", "latest", "b", evidence=["run focused test"])

    context = runtime.blackboard_context(max_chars=100)

    assert len(context) <= 100
    assert "latest" in context
    assert "run focused test" in context
    assert "old evidence" not in context
    runtime.close()
