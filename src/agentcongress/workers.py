from __future__ import annotations

import asyncio
from pathlib import Path

from .accounting import BudgetGovernor
from .adapters import WorkerAdapter, WorkerEvent
from .errors import WorkerProtocolError, WorkerValidationError
from .models import TaskStatus
from .reports import extract_task_report
from .runtime import CongressRuntime
from .verification import verify_task


async def execute_worker_task(
    runtime: CongressRuntime,
    task_id: str,
    adapter: WorkerAdapter,
    prompt: str,
    worktree: Path,
    report_schema: Path,
    *,
    governor: BudgetGovernor | None = None,
    base_revision: str | None = None,
    usage_model: str | None = None,
    session_max_seconds: float | None = None,
) -> list[WorkerEvent]:
    """Run an isolated worker and gate readiness on report plus system validation."""
    task = runtime.state.tasks.get(task_id)
    if task is None:
        raise ValueError(f"unknown task: {task_id}")
    if task.status != TaskStatus.ACCEPTED:
        raise ValueError("worker execution requires an accepted task")
    runtime.transition_task(task_id, TaskStatus.RUNNING, task.assignee_id)
    events: list[WorkerEvent] = []
    session_started = False

    def finish_worker_session() -> None:
        nonlocal session_started
        if governor is None or not session_started:
            return
        # Clear the guard before either call: if finishing or persistence fails,
        # the outer cleanup must not attempt to close the same session twice.
        session_started = False
        elapsed = governor.finish_session()
        runtime.record("budget.worker_session_finished", task.assignee_id, {"elapsed_seconds": elapsed, "snapshot": governor.snapshot()})

    try:
        if governor is not None:
            session_seconds = governor.start_session(session_max_seconds)
            session_started = True
            runtime.record("budget.worker_session_started", task.assignee_id, {"task_id": task_id, "reserved_seconds": session_seconds, "snapshot": governor.snapshot()})
        try:
            async for event in adapter.run_task(prompt, worktree, report_schema):
                events.append(event)
                runtime.record("worker.event", task.assignee_id, {"task_id": task_id, "worker_type": event.type, "payload": event.payload})
                if governor is not None:
                    usage = governor.observe_event(event.payload, model=usage_model)
                    if usage is not None:
                        runtime.record("budget.usage_observed", task.assignee_id, {"task_id": task_id, "usage": usage.as_dict(), "snapshot": governor.snapshot()})
        finally:
            # Report parsing, verification, and readiness transitions are
            # system work and must not consume the agent's fixed model slot.
            finish_worker_session()
        report = extract_task_report(events)
        if report is None:
            raise WorkerProtocolError("worker finished without a schema-valid task report")
        runtime.submit_task_report(task_id, report, task.assignee_id)
        if report.needs_human_input:
            return events
        validation = verify_task(worktree, task, report, base_revision or runtime.state.task_base_revisions.get(task_id, "HEAD"))
        runtime.record_validation(task_id, validation)
        if not validation.passed:
            raise WorkerValidationError("worker changes failed system validation: " + "; ".join(validation.errors))
        runtime.mark_task_ready(task_id, task.assignee_id)
    except asyncio.CancelledError:
        runtime.record(
            "worker.failed",
            task.assignee_id,
            {"task_id": task_id, "error": "worker execution was cancelled"},
        )
        if runtime.state.tasks[task_id].status == TaskStatus.RUNNING:
            runtime.transition_task(task_id, TaskStatus.FAILED, task.assignee_id)
        raise
    except Exception as error:
        runtime.record("worker.failed", task.assignee_id, {"task_id": task_id, "error": str(error)})
        if runtime.state.tasks[task_id].status == TaskStatus.RUNNING:
            runtime.transition_task(task_id, TaskStatus.FAILED, task.assignee_id)
        raise
    finally:
        # Covers failures between start_session() and entering/completing the
        # adapter flow while remaining a no-op after its normal cleanup.
        finish_worker_session()
    return events
