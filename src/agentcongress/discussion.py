from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .adapters import DialogueAdapter
from .config import MeetingConfig
from .models import FloorIntent, FloorRequest, MeetingPhase
from .orchestration import SegmentObserver, run_speaking_turn
from .runtime import CongressRuntime
from .streaming import ListenerProfile


@dataclass(frozen=True, slots=True)
class NoopObserver:
    """A safe default: listeners may be gated but never auto-interrupt."""

    async def evaluate(self, profile: ListenerProfile, segment: str, context: str = "") -> FloorRequest | None:
        return None


def listener_profiles(config: MeetingConfig) -> list[ListenerProfile]:
    return [ListenerProfile(agent.agent_id, agent.capability_tags, agent.role) for agent in config.agents]


def _agent_role(config: MeetingConfig, agent_id: str) -> str:
    return next(agent.role for agent in config.agents if agent.agent_id == agent_id)


def dialogue_prompt(runtime: CongressRuntime, config: MeetingConfig, instruction: str) -> str:
    speaker = runtime.state.speaker_id
    addressee = runtime.state.addressee_id
    if not speaker or not addressee:
        raise ValueError("meeting has no active speaker/addressee pair")
    return f"""You are meeting participant {speaker}, acting as {_agent_role(config, speaker)}. Address {addressee}.
Keep your contribution focused, evidence-based, and made of complete sentences. Do not restate settled points.

You may use tools to inspect the meeting (transcript, blackboard, tasks, floor) and, when a workspace is configured, to read files inside it; you may also record confirmed entries on the blackboard. Use tools before asserting facts you cannot verify from the context above.

Meeting instruction:
{instruction.strip()}

Confirmed shared blackboard:
{runtime.blackboard_context()}

Recent discussion:
{runtime.recent_transcript()}
"""


async def run_dialogue_turn(
    runtime: CongressRuntime,
    config: MeetingConfig,
    adapter: DialogueAdapter,
    instruction: str,
    observer: SegmentObserver | None = None,
) -> FloorRequest | None:
    if runtime.state.phase != MeetingPhase.DISCUSSING:
        raise ValueError("dialogue turns require a discussion phase")
    return await run_speaking_turn(runtime, adapter.stream_turn(dialogue_prompt(runtime, config, instruction)), listener_profiles(config), observer or NoopObserver())


@dataclass(slots=True)
class MeetingController:
    """Runs a bounded, recoverable discussion loop over the event-sourced floor."""

    runtime: CongressRuntime
    config: MeetingConfig
    adapters: dict[str, DialogueAdapter]
    observer: SegmentObserver = NoopObserver()

    async def run(self, instruction: str, *, max_turns: int) -> int:
        if max_turns < 1:
            raise ValueError("max_turns must be at least one")
        if self.runtime.state.phase == MeetingPhase.PREPARING:
            self.runtime.start(self.config.initial_speaker, self.config.initial_addressee)
        if self.runtime.state.phase != MeetingPhase.DISCUSSING:
            raise ValueError("meeting cannot run dialogue from its current phase")
        completed = 0
        for _ in range(max_turns):
            speaker = self.runtime.state.speaker_id
            if not speaker:
                raise ValueError("meeting has no active speaker")
            adapter = self.adapters.get(speaker)
            if adapter is None:
                raise ValueError(f"no dialogue adapter configured for {speaker}")
            winner = await run_dialogue_turn(self.runtime, self.config, adapter, instruction, self.observer)
            completed += 1
            if winner is not None and winner.intent == FloorIntent.BRIEF_INTERJECTION:
                # The listener is now the temporary speaker. Its next bounded
                # turn is the interjection, then the previous speaker resumes.
                continue
            if self.runtime.state.return_speaker_id:
                if winner is None:
                    self.runtime.complete_brief_interjection()
                else:
                    self.runtime.supersede_brief_interjection()
                continue
            if winner is None:
                self._advance_floor()
        return completed

    def _advance_floor(self) -> None:
        speaker = self.runtime.state.speaker_id
        if speaker is None:
            return
        roster = self.config.roster
        index = roster.index(speaker)
        next_speaker = roster[(index + 1) % len(roster)]
        if next_speaker == speaker:
            return
        self.runtime.record("floor.rotated", "runtime", {"speaker_id": next_speaker, "addressee_id": speaker})
