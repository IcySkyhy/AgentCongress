import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import agentcongress.workers as workers_module
from agentcongress.accounting import Budget, BudgetGovernor
from agentcongress.adapters import WorkerEvent
from agentcongress.models import Task, TaskStatus
from agentcongress.prompts import build_worker_prompt
from agentcongress.runtime import CongressRuntime
from agentcongress.workers import execute_worker_task


class _Worker:
    async def run_task(self, prompt: str, worktree: Path, report_schema: Path) -> AsyncIterator[WorkerEvent]:
        yield WorkerEvent("codex.event", {"type": "item.completed"})
        yield WorkerEvent("codex.event", {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"summary": "done", "changed_files": [], "validation": [], "risks": [], "commit": None, "needs_human_input": False})}})
        yield WorkerEvent("codex.event", {"type": "turn.completed"})


class _FailingWorker:
    async def run_task(self, prompt: str, worktree: Path, report_schema: Path) -> AsyncIterator[WorkerEvent]:
        raise RuntimeError("worker unavailable")
        yield WorkerEvent("never", {})


class _BlockedWorker:
    async def run_task(self, prompt: str, worktree: Path, report_schema: Path) -> AsyncIterator[WorkerEvent]:
        report = {"summary": "Need the API contract", "changed_files": [], "validation": [], "risks": ["contract unknown"], "commit": None, "needs_human_input": True}
        yield WorkerEvent("codex.event", {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(report)}})


class _CancellingWorker:
    async def run_task(self, prompt: str, worktree: Path, report_schema: Path) -> AsyncIterator[WorkerEvent]:
        raise asyncio.CancelledError
        yield WorkerEvent("never", {})


class _TrackingGovernor(BudgetGovernor):
    def __init__(self) -> None:
        super().__init__(Budget(1, 60), "gpt-5.6-luna")
        self.finish_calls = 0

    def finish_session(self) -> float:
        self.finish_calls += 1
        return super().finish_session()


def _git(path: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    _git(worktree, "init", "-b", "main")
    (worktree / "README.md").write_text("base", encoding="utf-8")
    _git(worktree, "add", "README.md")
    _git(worktree, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    return worktree


def _accept(runtime: CongressRuntime, task_id: str, actor: str) -> None:
    runtime.transition_task(task_id, TaskStatus.ASSIGNED, actor)
    runtime.transition_task(task_id, TaskStatus.ACCEPTED, actor)


def test_worker_events_and_status_are_persisted(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path)
    runtime = CongressRuntime("m", tmp_path / "events.db", ["worker", "chair"])
    runtime.propose_task(Task("build", "Build", "worker", ["report"]))
    _accept(runtime, "build", "worker")
    events = asyncio.run(execute_worker_task(runtime, "build", _Worker(), "do work", worktree, tmp_path / "schema.json", base_revision="HEAD"))
    assert len(events) == 3
    assert runtime.state.tasks["build"].status == "ready_for_report"
    assert [event.type for event in runtime.store.replay("m")][-3:] == ["task.reported", "task.validation_completed", "task.status_changed"]
    runtime.close()


def test_worker_session_ends_before_system_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = _worktree(tmp_path)
    runtime = CongressRuntime("m", tmp_path / "events.db", ["worker", "chair"])
    runtime.propose_task(Task("build", "Build", "worker", ["report"]))
    _accept(runtime, "build", "worker")
    governor = _TrackingGovernor()
    original_verify_task = workers_module.verify_task

    def verify_after_agent_slot(*args: object, **kwargs: object):
        assert governor._active_session_started_at is None
        assert governor.finish_calls == 1
        return original_verify_task(*args, **kwargs)

    monkeypatch.setattr(workers_module, "verify_task", verify_after_agent_slot)
    asyncio.run(
        execute_worker_task(
            runtime,
            "build",
            _Worker(),
            "do work",
            worktree,
            tmp_path / "schema.json",
            governor=governor,
            base_revision="HEAD",
        )
    )

    assert governor.finish_calls == 1
    event_types = [event.type for event in runtime.store.replay("m")]
    assert event_types.index("budget.worker_session_finished") < event_types.index("task.validation_completed")
    runtime.close()


def test_worker_prompt_keeps_task_boundaries_explicit() -> None:
    task = Task("build", "Build", "worker", ["report"], ["src/"], ["pytest -q"])
    prompt = build_worker_prompt(task, "Use small commits.")
    assert "src/" in prompt
    assert "pytest -q" in prompt
    assert "Do not merge branches" in prompt
    assert "Use small commits." in prompt


def test_task_report_schema_is_strict_response_compatible() -> None:
    schema_path = Path(__file__).parents[1] / "src" / "agentcongress" / "task-report.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert set(schema["required"]) == set(schema["properties"])


def test_worker_failure_is_persisted_and_marks_the_task_failed(tmp_path: Path) -> None:
    runtime = CongressRuntime("m", tmp_path / "events.db", ["worker", "chair"])
    runtime.propose_task(Task("build", "Build", "worker", ["report"]))
    _accept(runtime, "build", "worker")
    governor = _TrackingGovernor()
    with pytest.raises(RuntimeError, match="worker unavailable"):
        asyncio.run(execute_worker_task(runtime, "build", _FailingWorker(), "do work", tmp_path, tmp_path / "schema.json", governor=governor))
    assert governor.finish_calls == 1
    assert governor._active_session_started_at is None
    assert runtime.state.tasks["build"].status == "failed"
    assert [event.type for event in runtime.store.replay("m")][-2:] == ["worker.failed", "task.status_changed"]
    runtime.close()


def test_worker_cancellation_finishes_budget_session_once(tmp_path: Path) -> None:
    runtime = CongressRuntime("m", tmp_path / "events.db", ["worker", "chair"])
    runtime.propose_task(Task("build", "Build", "worker", ["report"]))
    _accept(runtime, "build", "worker")
    governor = _TrackingGovernor()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            execute_worker_task(
                runtime,
                "build",
                _CancellingWorker(),
                "do work",
                tmp_path,
                tmp_path / "schema.json",
                governor=governor,
            )
        )

    assert governor.finish_calls == 1
    assert governor._active_session_started_at is None
    assert runtime.state.tasks["build"].status == TaskStatus.FAILED
    assert [event.type for event in runtime.store.replay("m")][-2:] == [
        "worker.failed",
        "task.status_changed",
    ]
    runtime.close()


def test_worker_requesting_human_input_is_persisted_as_blocked(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path)
    database = tmp_path / "events.db"
    runtime = CongressRuntime("m", database, ["worker", "chair"])
    runtime.propose_task(Task("build", "Build", "worker", ["report"]))
    _accept(runtime, "build", "worker")

    asyncio.run(execute_worker_task(runtime, "build", _BlockedWorker(), "do work", worktree, tmp_path / "schema.json", base_revision="HEAD"))

    assert runtime.state.tasks["build"].status == TaskStatus.BLOCKED
    assert "build" not in runtime.state.validation_results
    runtime.close()
    recovered = CongressRuntime.resume("m", database, ["worker", "chair"])
    assert recovered.state.tasks["build"].status == TaskStatus.BLOCKED
    assert recovered.state.task_reports["build"].needs_human_input
    recovered.close()
