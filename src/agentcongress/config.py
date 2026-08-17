from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class AgentConfig:
    agent_id: str
    role: str
    capability_tags: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    repository: Path
    base_ref: str = "HEAD"
    merge_policy: str = "manual"


@dataclass(frozen=True, slots=True)
class MeetingConfig:
    meeting_id: str
    agents: tuple[AgentConfig, ...]
    initial_speaker: str
    initial_addressee: str
    execution_mode: str = "recess"
    workspace: WorkspaceConfig | None = None

    @property
    def roster(self) -> list[str]:
        return [agent.agent_id for agent in self.agents]


def load_config(path: Path) -> MeetingConfig:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    meeting = raw.get("meeting", raw)
    agents = tuple(AgentConfig(agent_id=str(item["id"]), role=str(item["role"]), capability_tags=frozenset(map(str, item.get("capability_tags", [])))) for item in meeting.get("agents", []))
    if len(agents) < 2:
        raise ValueError("meeting requires at least two agents")
    ids = [agent.agent_id for agent in agents]
    if len(ids) != len(set(ids)):
        raise ValueError("agent ids must be unique")
    speaker = str(meeting["initial_speaker"])
    addressee = str(meeting["initial_addressee"])
    if speaker not in ids or addressee not in ids or speaker == addressee:
        raise ValueError("initial speaker and addressee must be distinct roster members")
    mode = str(meeting.get("execution_mode", "recess"))
    if mode not in {"recess", "continuous"}:
        raise ValueError("execution_mode must be recess or continuous")
    workspace_raw = meeting.get("workspace")
    workspace = None
    if workspace_raw:
        policy = str(workspace_raw.get("merge_policy", "manual"))
        if policy not in {"manual", "tests-pass-auto"}:
            raise ValueError("merge_policy must be manual or tests-pass-auto")
        workspace = WorkspaceConfig(Path(workspace_raw["repository"]), str(workspace_raw.get("base_ref", "HEAD")), policy)
    return MeetingConfig(str(meeting["id"]), agents, speaker, addressee, mode, workspace)
