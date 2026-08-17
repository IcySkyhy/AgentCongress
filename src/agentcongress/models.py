from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class MeetingPhase(StrEnum):
    PREPARING = "preparing"
    DISCUSSING = "discussing"
    EXECUTING = "executing"
    REPORTING = "reporting"
    DECIDING = "deciding"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    BUDGET_EXHAUSTED = "budget_exhausted"


class FloorIntent(StrEnum):
    BRIEF_INTERJECTION = "brief_interjection"
    REPLACE_SPEAKER = "replace_speaker"
    REPLACE_ADDRESSEE = "replace_addressee"


class TaskStatus(StrEnum):
    PROPOSED = "proposed"
    ASSIGNED = "assigned"
    ACCEPTED = "accepted"
    RUNNING = "running"
    BLOCKED = "blocked"
    READY_FOR_REPORT = "ready_for_report"
    IN_REVIEW = "in_review"
    INTEGRATED = "integrated"
    MERGED = "merged"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    actor_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    meeting_id: str = ""
    event_id: str = ""
    sequence: int = 0
    timestamp: float = 0.0
    causation_id: str | None = None
    correlation_id: str | None = None
    schema_version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FloorRequest:
    agent_id: str
    intent: FloorIntent
    urgency: float
    relevance: float
    novelty: float
    confidence: float
    public_reason: str
    estimated_segments: int = 1
    explicitly_addressed: bool = False


@dataclass(slots=True)
class Task:
    task_id: str
    title: str
    assignee_id: str
    acceptance_criteria: list[str]
    allowed_paths: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PROPOSED


@dataclass(frozen=True, slots=True)
class TaskReport:
    """Structured, worker-supplied handoff. It is evidence, not verification."""

    summary: str
    changed_files: tuple[str, ...]
    validation: tuple[str, ...]
    risks: tuple[str, ...]
    commit: str | None
    needs_human_input: bool

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TaskReport":
        required = {"summary", "changed_files", "validation", "risks", "commit", "needs_human_input"}
        if set(payload) != required:
            raise ValueError("task report must contain exactly the declared schema fields")
        if not isinstance(payload["summary"], str) or not payload["summary"].strip():
            raise ValueError("task report summary must be a non-empty string")
        for field_name in ("changed_files", "validation", "risks"):
            if not isinstance(payload[field_name], list) or not all(isinstance(value, str) for value in payload[field_name]):
                raise ValueError(f"task report {field_name} must be a list of strings")
        commit = payload["commit"]
        if commit is not None and not isinstance(commit, str):
            raise ValueError("task report commit must be a string or null")
        if not isinstance(payload["needs_human_input"], bool):
            raise ValueError("task report needs_human_input must be boolean")
        return cls(
            summary=payload["summary"].strip(),
            changed_files=tuple(payload["changed_files"]),
            validation=tuple(payload["validation"]),
            risks=tuple(payload["risks"]),
            commit=commit,
            needs_human_input=payload["needs_human_input"],
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "changed_files": list(self.changed_files),
            "validation": list(self.validation),
            "risks": list(self.risks),
            "commit": self.commit,
            "needs_human_input": self.needs_human_input,
        }


@dataclass(frozen=True, slots=True)
class GitIdentity:
    """Exact Git state whose contents were observed by the verifier."""

    branch: str
    head: str
    tree: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "GitIdentity":
        return cls(branch=str(payload["branch"]), head=str(payload["head"]), tree=str(payload["tree"]))

    def as_payload(self) -> dict[str, str]:
        return {"branch": self.branch, "head": self.head, "tree": self.tree}


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """System-observed validation; unlike TaskReport this gates integration."""

    passed: bool
    changed_files: tuple[str, ...]
    commands: tuple[dict[str, Any], ...]
    errors: tuple[str, ...] = ()
    git_identity: GitIdentity | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "changed_files": list(self.changed_files),
            "commands": list(self.commands),
            "errors": list(self.errors),
            "git_identity": None if self.git_identity is None else self.git_identity.as_payload(),
        }


@dataclass(frozen=True, slots=True)
class BlackboardEntry:
    kind: str
    content: str
    actor_id: str
    task_id: str | None = None
    evidence: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "content": self.content,
            "actor_id": self.actor_id,
            "task_id": self.task_id,
            "evidence": list(self.evidence),
        }
