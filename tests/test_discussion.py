import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from agentcongress.config import AgentConfig, MeetingConfig
from agentcongress.discussion import MeetingController, run_dialogue_turn
from agentcongress.runtime import CongressRuntime


class _Dialogue:
    def stream_turn(self, prompt: str) -> AsyncIterator[str]:
        async def stream() -> AsyncIterator[str]:
            assert "architect" in prompt
            assert "reviewer" in prompt
            yield "We should use an event log."
            yield " SQLite makes recovery straightforward."

        return stream()


def test_api_backed_dialogue_turn_records_safe_segments(tmp_path: Path) -> None:
    config = MeetingConfig("m", (AgentConfig("architect", "architect"), AgentConfig("reviewer", "reviewer")), "architect", "reviewer")
    runtime = CongressRuntime("m", tmp_path / "events.db", config.roster)
    runtime.start("architect", "reviewer")
    winner = asyncio.run(run_dialogue_turn(runtime, config, _Dialogue(), "Propose a storage design."))
    assert winner is None
    events = runtime.store.replay("m")
    assert [event.type for event in events] == ["meeting.started", "speech.segment_committed", "speech.segment_committed"]
    assert events[-1].payload["content"] == "SQLite makes recovery straightforward."
    runtime.close()


class _ShortDialogue:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def stream_turn(self, prompt: str) -> AsyncIterator[str]:
        self.prompts.append(prompt)

        async def stream() -> AsyncIterator[str]:
            yield "I have one concrete update."

        return stream()


def test_meeting_controller_rotates_bounded_turns_and_shares_blackboard(tmp_path: Path) -> None:
    config = MeetingConfig(
        "m",
        (AgentConfig("architect", "architect"), AgentConfig("reviewer", "reviewer"), AgentConfig("implementer", "implementer")),
        "architect",
        "reviewer",
        "continuous",
    )
    runtime = CongressRuntime("m", tmp_path / "events.db", config.roster)
    runtime.start("architect", "reviewer")
    runtime.add_blackboard("decision", "Use an event log.", "architect")
    adapters = {agent: _ShortDialogue() for agent in config.roster}
    turns = asyncio.run(MeetingController(runtime, config, adapters).run("Decide the storage approach.", max_turns=2))
    assert turns == 2
    assert runtime.state.speaker_id == "implementer"
    assert "Use an event log" in adapters["architect"].prompts[0]
    assert [event.type for event in runtime.store.replay("m")].count("floor.rotated") == 2
    runtime.close()
