from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .events import MeetingFileLock, SQLiteEventStore, meeting_lock_path
from .floor import FloorPolicy
from .models import (
    ApprovalDecision,
    BlackboardEntry,
    Event,
    FloorIntent,
    FloorRequest,
    GitIdentity,
    MeetingPhase,
    Task,
    TaskReport,
    TaskStatus,
    ValidationResult,
)


_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PROPOSED: frozenset({TaskStatus.ASSIGNED, TaskStatus.CANCELLED}),
    TaskStatus.ASSIGNED: frozenset({TaskStatus.ACCEPTED, TaskStatus.CANCELLED}),
    TaskStatus.ACCEPTED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset({TaskStatus.BLOCKED, TaskStatus.READY_FOR_REPORT, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.BLOCKED: frozenset({TaskStatus.ACCEPTED, TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.READY_FOR_REPORT: frozenset({TaskStatus.IN_REVIEW, TaskStatus.INTEGRATED, TaskStatus.REJECTED, TaskStatus.CANCELLED}),
    TaskStatus.IN_REVIEW: frozenset({TaskStatus.READY_FOR_REPORT, TaskStatus.INTEGRATED, TaskStatus.REJECTED, TaskStatus.CANCELLED}),
    TaskStatus.INTEGRATED: frozenset({TaskStatus.MERGED, TaskStatus.REJECTED}),
    TaskStatus.MERGED: frozenset(),
    TaskStatus.REJECTED: frozenset({TaskStatus.ACCEPTED, TaskStatus.CANCELLED}),
    TaskStatus.FAILED: frozenset({TaskStatus.ACCEPTED, TaskStatus.CANCELLED}),
    TaskStatus.CANCELLED: frozenset(),
}

_PHASE_TRANSITIONS: dict[MeetingPhase, frozenset[MeetingPhase]] = {
    MeetingPhase.PREPARING: frozenset({MeetingPhase.DISCUSSING, MeetingPhase.EXECUTING, MeetingPhase.PAUSED, MeetingPhase.FAILED}),
    MeetingPhase.DISCUSSING: frozenset({MeetingPhase.EXECUTING, MeetingPhase.REPORTING, MeetingPhase.DECIDING, MeetingPhase.PAUSED, MeetingPhase.COMPLETED, MeetingPhase.FAILED, MeetingPhase.BUDGET_EXHAUSTED}),
    MeetingPhase.EXECUTING: frozenset({MeetingPhase.DISCUSSING, MeetingPhase.REPORTING, MeetingPhase.DECIDING, MeetingPhase.PAUSED, MeetingPhase.FAILED, MeetingPhase.BUDGET_EXHAUSTED}),
    MeetingPhase.REPORTING: frozenset({MeetingPhase.DISCUSSING, MeetingPhase.EXECUTING, MeetingPhase.DECIDING, MeetingPhase.PAUSED, MeetingPhase.FAILED, MeetingPhase.BUDGET_EXHAUSTED}),
    MeetingPhase.DECIDING: frozenset({MeetingPhase.DISCUSSING, MeetingPhase.EXECUTING, MeetingPhase.COMPLETED, MeetingPhase.PAUSED, MeetingPhase.FAILED, MeetingPhase.BUDGET_EXHAUSTED}),
    MeetingPhase.PAUSED: frozenset({MeetingPhase.DISCUSSING, MeetingPhase.EXECUTING, MeetingPhase.REPORTING, MeetingPhase.DECIDING, MeetingPhase.COMPLETED, MeetingPhase.FAILED, MeetingPhase.BUDGET_EXHAUSTED}),
    MeetingPhase.BUDGET_EXHAUSTED: frozenset({MeetingPhase.PAUSED, MeetingPhase.COMPLETED, MeetingPhase.FAILED}),
    MeetingPhase.COMPLETED: frozenset(),
    MeetingPhase.FAILED: frozenset(),
}


@dataclass(slots=True)
class MeetingState:
    meeting_id: str
    phase: MeetingPhase = MeetingPhase.PREPARING
    speaker_id: str | None = None
    addressee_id: str | None = None
    return_speaker_id: str | None = None
    roster: set[str] = field(default_factory=set)
    tasks: dict[str, Task] = field(default_factory=dict)
    approvals: dict[str, ApprovalDecision] = field(default_factory=dict)
    approval_git_identities: dict[str, GitIdentity] = field(default_factory=dict)
    task_reports: dict[str, TaskReport] = field(default_factory=dict)
    validation_results: dict[str, ValidationResult] = field(default_factory=dict)
    task_merge_commits: dict[str, str] = field(default_factory=dict)
    integration_validation_result: ValidationResult | None = None
    task_base_revisions: dict[str, str] = field(default_factory=dict)
    task_worktrees: dict[str, dict[str, str]] = field(default_factory=dict)
    blackboard: list[BlackboardEntry] = field(default_factory=list)
    transcript: list[dict[str, str]] = field(default_factory=list)


class CongressRuntime:
    def __init__(self, meeting_id: str, database: Path, roster: list[str], *, lock_path: Path | None = None) -> None:
        self.state = MeetingState(meeting_id=meeting_id, roster=set(roster))
        self._lock = MeetingFileLock(lock_path or meeting_lock_path(database, meeting_id))
        self._closed = True
        self._lock.acquire()
        try:
            self.store = SQLiteEventStore(database)
        except BaseException:
            self._lock.release()
            raise
        self.floor = FloorPolicy()
        self._closed = False

    @classmethod
    def resume(cls, meeting_id: str, database: Path, roster: list[str], *, lock_path: Path | None = None) -> "CongressRuntime":
        runtime = cls(meeting_id, database, roster, lock_path=lock_path)
        try:
            events = runtime.store.replay(meeting_id)
            started = next((event for event in events if event.type == "meeting.started"), None)
            persisted_roster = started.payload.get("roster") if started is not None else None
            if persisted_roster is not None and set(map(str, persisted_roster)) != runtime.state.roster:
                raise ValueError("configured roster does not match the persisted meeting roster")
            for event in events:
                runtime.apply(event, replaying=True)
        except BaseException:
            runtime.close()
            raise
        return runtime

    def record(self, event_type: str, actor_id: str, payload: dict | None = None) -> Event:
        event = self.store.append(Event(type=event_type, actor_id=actor_id, payload=payload or {}, meeting_id=self.state.meeting_id))
        self.apply(event)
        return event

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.store.close()
        finally:
            self._lock.release()
            self._closed = True

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def start(self, speaker_id: str, addressee_id: str) -> Event:
        if self.state.phase != MeetingPhase.PREPARING:
            raise ValueError("meeting has already started")
        if speaker_id not in self.state.roster or addressee_id not in self.state.roster or speaker_id == addressee_id:
            raise ValueError("initial floor members must be distinct roster members")
        return self.record(
            "meeting.started",
            "runtime",
            {"speaker_id": speaker_id, "addressee_id": addressee_id, "roster": sorted(self.state.roster)},
        )

    def transition_phase(self, phase: MeetingPhase, actor_id: str = "runtime") -> Event:
        if phase == self.state.phase:
            raise ValueError("meeting is already in this phase")
        if phase not in _PHASE_TRANSITIONS[self.state.phase]:
            raise ValueError(f"invalid phase transition: {self.state.phase} -> {phase}")
        return self.record("meeting.phase_changed", actor_id, {"phase": phase})

    def request_floor(self, request: FloorRequest) -> Event:
        if self.state.phase != MeetingPhase.DISCUSSING:
            raise ValueError("floor requests require a discussion phase")
        if request.agent_id not in self.state.roster:
            raise ValueError("only roster members may request the floor")
        return self.record("floor.requested", request.agent_id, {"intent": request.intent, "score": self.floor.score(request), "reason": request.public_reason})

    def resolve_floor(self, requests: list[FloorRequest]) -> Event:
        winner = self.floor.select(requests)
        if winner is None:
            return self.record("floor.retained", "runtime")
        payload: dict[str, str] = {"agent_id": winner.agent_id, "intent": winner.intent}
        if winner.intent == FloorIntent.BRIEF_INTERJECTION:
            if not self.state.speaker_id:
                raise ValueError("brief interjection requires a current speaker")
            payload["return_speaker_id"] = self.state.speaker_id
        return self.record("floor.granted", "runtime", payload)

    def complete_brief_interjection(self, actor_id: str = "runtime") -> Event:
        if not self.state.return_speaker_id:
            raise ValueError("there is no brief interjection to complete")
        return self.record("brief.interjection_completed", actor_id, {"speaker_id": self.state.return_speaker_id})

    def supersede_brief_interjection(self, actor_id: str = "runtime") -> Event:
        if not self.state.return_speaker_id:
            raise ValueError("there is no brief interjection to supersede")
        return self.record("brief.interjection_superseded", actor_id)

    def commit_segment(self, content: str) -> Event:
        if self.state.phase != MeetingPhase.DISCUSSING or not self.state.speaker_id:
            raise ValueError("speech may only be committed while a meeting is discussing")
        return self.record("speech.segment_committed", self.state.speaker_id, {"content": content, "addressee_id": self.state.addressee_id})

    def add_blackboard(
        self,
        kind: str,
        content: str,
        actor_id: str,
        *,
        task_id: str | None = None,
        evidence: list[str] | None = None,
    ) -> Event:
        if not kind.strip() or not content.strip():
            raise ValueError("blackboard entries require kind and content")
        if task_id is not None and task_id not in self.state.tasks:
            raise ValueError("blackboard task must exist")
        return self.record(
            "blackboard.updated",
            actor_id,
            BlackboardEntry(kind.strip(), content.strip(), actor_id, task_id, tuple(evidence or [])).as_payload(),
        )

    def blackboard_context(self, limit: int = 20, max_chars: int = 12_000) -> str:
        if limit < 1 or max_chars < 1:
            raise ValueError("blackboard context limits must be positive")
        entries = self.state.blackboard[-limit:]
        if not entries:
            return "No confirmed blackboard entries yet."
        blocks: list[str] = []
        for entry in entries:
            content = entry.content[:2_000]
            line = f"[{entry.kind}] {content}" + (f" (task: {entry.task_id})" if entry.task_id else "")
            lines = [line]
            # Evidence is the executable part of planner handoffs (hypotheses,
            # validation steps, and risks).  It is persisted separately from
            # the summary, so omitting it here silently reduced the shared
            # blackboard to a headline.
            lines.extend(f"  - {item[:500]}" for item in entry.evidence[:12])
            blocks.append("\n".join(lines))
        # Preserve complete, recent blocks when the prompt budget is tight.
        selected: list[str] = []
        used = 0
        for block in reversed(blocks):
            separator = 1 if selected else 0
            if used + separator + len(block) > max_chars:
                if not selected:
                    selected.append(block[:max_chars])
                break
            selected.append(block)
            used += separator + len(block)
        return "\n".join(reversed(selected))

    def recent_transcript(self, limit: int = 8) -> str:
        segments = self.state.transcript[-limit:]
        if not segments:
            return "No prior discussion segments."
        return "\n".join(f"{segment['speaker_id']} -> {segment['addressee_id']}: {segment['content']}" for segment in segments)

    def propose_task(self, task: Task) -> Event:
        if task.task_id in self.state.tasks:
            raise ValueError(f"task already exists: {task.task_id}")
        if task.assignee_id not in self.state.roster:
            raise ValueError("task assignee must be in the fixed roster")
        return self.record("task.proposed", "runtime", {"task_id": task.task_id, "title": task.title, "assignee_id": task.assignee_id, "acceptance_criteria": task.acceptance_criteria, "allowed_paths": task.allowed_paths, "validation_commands": task.validation_commands})

    def transition_task(self, task_id: str, status: TaskStatus, actor_id: str = "runtime") -> Event:
        task = self.state.tasks.get(task_id)
        if task is None:
            raise ValueError(f"unknown task: {task_id}")
        if status not in _TASK_TRANSITIONS[task.status]:
            raise ValueError(f"invalid task transition: {task.status} -> {status}")
        return self.record("task.status_changed", actor_id, {"task_id": task_id, "status": status})

    def record_task_worktree(self, task_id: str, branch: str, path: str, base_revision: str) -> Event:
        if task_id not in self.state.tasks:
            raise ValueError(f"unknown task: {task_id}")
        record = {"branch": branch, "path": path, "base_revision": base_revision}
        persisted = self.state.task_worktrees.get(task_id)
        if persisted is not None and persisted != record:
            raise ValueError("task worktree identity conflicts with the persisted record")
        return self.record("workspace.task_created", "runtime", {"task_id": task_id, "branch": branch, "path": path, "base_revision": base_revision})

    def submit_task_report(self, task_id: str, report: TaskReport, actor_id: str) -> Event:
        task = self.state.tasks.get(task_id)
        if task is None:
            raise ValueError(f"unknown task: {task_id}")
        if task.status != TaskStatus.RUNNING:
            raise ValueError("task reports require a running task")
        if actor_id != task.assignee_id:
            raise ValueError("only the assignee may submit a task report")
        event = self.record("task.reported", actor_id, {"task_id": task_id, "report": report.as_payload()})
        if report.needs_human_input:
            self.transition_task(task_id, TaskStatus.BLOCKED, actor_id)
        return event

    def record_validation(self, task_id: str, result: ValidationResult, actor_id: str = "runtime") -> Event:
        if task_id not in self.state.tasks:
            raise ValueError(f"unknown task: {task_id}")
        return self.record("task.validation_completed", actor_id, {"task_id": task_id, "result": result.as_payload()})

    def mark_task_ready(self, task_id: str, actor_id: str = "runtime") -> Event:
        report = self.state.task_reports.get(task_id)
        if report is None:
            raise ValueError("task requires a structured report before it can be ready")
        if report.needs_human_input:
            raise ValueError("task report requires human input before it can be ready")
        validation = self.state.validation_results.get(task_id)
        if validation is None or not validation.passed or validation.git_identity is None:
            raise ValueError("task requires passing system validation before it can be ready")
        return self.transition_task(task_id, TaskStatus.READY_FOR_REPORT, actor_id)

    def request_merge_approval(self, task_id: str, actor_id: str = "runtime") -> Event:
        if task_id not in self.state.tasks:
            raise ValueError(f"unknown task: {task_id}")
        if self.state.tasks[task_id].status not in {TaskStatus.READY_FOR_REPORT, TaskStatus.IN_REVIEW}:
            raise ValueError("only validated, reported tasks may request merge approval")
        return self.record("approval.requested", actor_id, {"task_id": task_id})

    def decide_merge_approval(self, task_id: str, approved: bool, actor_id: str) -> Event:
        if self.state.approvals.get(task_id) != ApprovalDecision.PENDING:
            raise ValueError("merge approval is not pending")
        payload: dict[str, object] = {"task_id": task_id}
        if approved:
            validation = self.state.validation_results.get(task_id)
            if validation is None or not validation.passed or validation.git_identity is None:
                raise ValueError("approval requires a passing validation bound to a Git identity")
            payload["git_identity"] = validation.git_identity.as_payload()
        return self.record("approval.granted" if approved else "approval.rejected", actor_id, payload)

    def assert_task_verified(self, task_id: str) -> None:
        validation = self.state.validation_results.get(task_id)
        if validation is None or not validation.passed or validation.git_identity is None:
            raise ValueError("task has not passed system validation")

    def record_task_integration(self, task_id: str, merge_commit: str, actor_id: str = "runtime") -> Event:
        task = self.state.tasks.get(task_id)
        if task is None:
            raise ValueError(f"unknown task: {task_id}")
        if task.status not in {TaskStatus.READY_FOR_REPORT, TaskStatus.IN_REVIEW}:
            raise ValueError("only a validated ready task may be recorded as integrated")
        self.assert_task_verified(task_id)
        if not merge_commit.strip():
            raise ValueError("integration requires the actual merge commit")
        return self.record(
            "integration.task_integrated",
            actor_id,
            {"task_id": task_id, "status": TaskStatus.INTEGRATED, "merge_commit": merge_commit.strip()},
        )

    def integrated_validation_commands(self) -> tuple[str, ...]:
        """Stable union of checks owned by tasks currently in the integration tree."""
        seen: set[str] = set()
        commands: list[str] = []
        for task in self.state.tasks.values():
            if task.status != TaskStatus.INTEGRATED:
                continue
            for command in task.validation_commands:
                if command not in seen:
                    seen.add(command)
                    commands.append(command)
        return tuple(commands)

    def record_integration_validation(self, result: ValidationResult, actor_id: str = "runtime") -> Event:
        return self.record("integration.validation_completed", actor_id, {"result": result.as_payload()})

    def assert_integration_verified(self) -> None:
        result = self.state.integration_validation_result
        if result is None or not result.passed or result.git_identity is None:
            raise ValueError("combined integration validation has not passed")

    def apply(self, event: Event, replaying: bool = False) -> None:
        payload = event.payload
        if event.type == "meeting.started":
            self.state.phase = MeetingPhase.DISCUSSING
            self.state.speaker_id = payload["speaker_id"]
            self.state.addressee_id = payload["addressee_id"]
        elif event.type == "meeting.phase_changed":
            self.state.phase = MeetingPhase(payload["phase"])
        elif event.type == "floor.granted":
            winner = payload["agent_id"]
            self.floor.grant(winner)
            intent = FloorIntent(payload["intent"])
            if intent in {FloorIntent.REPLACE_SPEAKER, FloorIntent.BRIEF_INTERJECTION}:
                self.state.speaker_id = winner
            if intent == FloorIntent.REPLACE_ADDRESSEE:
                self.state.addressee_id = winner
            if intent == FloorIntent.BRIEF_INTERJECTION:
                self.state.return_speaker_id = payload["return_speaker_id"]
        elif event.type == "brief.interjection_completed":
            self.state.speaker_id = payload["speaker_id"]
            self.state.return_speaker_id = None
        elif event.type == "brief.interjection_superseded":
            self.state.return_speaker_id = None
        elif event.type == "floor.rotated":
            self.state.speaker_id = payload["speaker_id"]
            self.state.addressee_id = payload["addressee_id"]
        elif event.type == "speech.segment_committed":
            self.state.transcript.append({"speaker_id": event.actor_id, "addressee_id": payload.get("addressee_id", ""), "content": payload["content"]})
        elif event.type == "blackboard.updated":
            self.state.blackboard.append(
                BlackboardEntry(
                    kind=payload["kind"],
                    content=payload["content"],
                    actor_id=payload["actor_id"],
                    task_id=payload.get("task_id"),
                    evidence=tuple(payload.get("evidence", [])),
                )
            )
        elif event.type == "task.proposed":
            self.state.tasks[payload["task_id"]] = Task(task_id=payload["task_id"], title=payload["title"], assignee_id=payload["assignee_id"], acceptance_criteria=payload["acceptance_criteria"], allowed_paths=payload["allowed_paths"], validation_commands=payload["validation_commands"])
        elif event.type == "task.status_changed":
            self.state.tasks[payload["task_id"]].status = TaskStatus(payload["status"])
        elif event.type == "workspace.task_created":
            self.state.task_base_revisions[payload["task_id"]] = payload["base_revision"]
            self.state.task_worktrees[payload["task_id"]] = {
                "branch": payload["branch"],
                "path": payload["path"],
                "base_revision": payload["base_revision"],
            }
        elif event.type == "task.reported":
            self.state.task_reports[payload["task_id"]] = TaskReport.from_payload(payload["report"])
        elif event.type == "task.validation_completed":
            result = payload["result"]
            self.state.validation_results[payload["task_id"]] = ValidationResult(
                passed=bool(result["passed"]),
                changed_files=tuple(result["changed_files"]),
                commands=tuple(result["commands"]),
                errors=tuple(result.get("errors", [])),
                git_identity=GitIdentity.from_payload(result["git_identity"]) if result.get("git_identity") else None,
            )
        elif event.type == "integration.task_integrated":
            self.state.task_merge_commits[payload["task_id"]] = payload["merge_commit"]
            self.state.tasks[payload["task_id"]].status = TaskStatus(payload["status"])
            # Any prior aggregate result predates the newly merged task.
            self.state.integration_validation_result = None
        elif event.type == "integration.validation_completed":
            result = payload["result"]
            self.state.integration_validation_result = ValidationResult(
                passed=bool(result["passed"]),
                changed_files=tuple(result["changed_files"]),
                commands=tuple(result["commands"]),
                errors=tuple(result.get("errors", [])),
                git_identity=GitIdentity.from_payload(result["git_identity"]) if result.get("git_identity") else None,
            )
        elif event.type == "approval.requested":
            self.state.approvals[payload["task_id"]] = ApprovalDecision.PENDING
        elif event.type == "approval.granted":
            self.state.approvals[payload["task_id"]] = ApprovalDecision.APPROVED
            if payload.get("git_identity"):
                self.state.approval_git_identities[payload["task_id"]] = GitIdentity.from_payload(payload["git_identity"])
        elif event.type == "approval.rejected":
            self.state.approvals[payload["task_id"]] = ApprovalDecision.REJECTED
            self.state.approval_git_identities.pop(payload["task_id"], None)
