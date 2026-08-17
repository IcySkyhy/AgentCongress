from pathlib import Path

import pytest

from agentcongress.models import ApprovalDecision, FloorIntent, FloorRequest, GitIdentity, Task, TaskReport, TaskStatus, ValidationResult
from agentcongress.runtime import CongressRuntime


def test_meeting_replays_and_replaces_speaker(tmp_path: Path) -> None:
    runtime = CongressRuntime("m1", tmp_path / "events.db", ["a", "b", "c"])
    runtime.start("a", "b")
    request = FloorRequest("c", FloorIntent.REPLACE_SPEAKER, 1, 1, 1, 1, "critical correction")
    runtime.request_floor(request)
    runtime.resolve_floor([request])
    assert runtime.state.speaker_id == "c"
    assert [event.type for event in runtime.store.replay("m1")] == ["meeting.started", "floor.requested", "floor.granted"]
    runtime.close()


def test_task_is_owned_by_fixed_roster_member(tmp_path: Path) -> None:
    runtime = CongressRuntime("m2", tmp_path / "events.db", ["a", "b"])
    task = Task("review", "Review change", "b", ["report findings"])
    runtime.propose_task(task)
    runtime.transition_task("review", TaskStatus.ASSIGNED, "b")
    runtime.transition_task("review", TaskStatus.ACCEPTED, "b")
    runtime.transition_task("review", TaskStatus.RUNNING, "b")
    assert runtime.state.tasks["review"].status == TaskStatus.RUNNING
    runtime.close()


def test_runtime_resumes_from_event_store(tmp_path: Path) -> None:
    database = tmp_path / "events.db"
    runtime = CongressRuntime("m3", database, ["a", "b", "c"])
    runtime.start("a", "b")
    request = FloorRequest("c", FloorIntent.REPLACE_ADDRESSEE, 1, 1, 1, 1, "I own this concern")
    runtime.resolve_floor([request])
    runtime.close()
    recovered = CongressRuntime.resume("m3", database, ["a", "b", "c"])
    assert recovered.state.speaker_id == "a"
    assert recovered.state.addressee_id == "c"
    assert recovered.floor.speaking_turns == {"c": 1}
    recovered.close()


def test_runtime_rejects_roster_drift_on_resume(tmp_path: Path) -> None:
    database = tmp_path / "events.db"
    runtime = CongressRuntime("m", database, ["a", "b"])
    runtime.start("a", "b")
    runtime.close()

    with pytest.raises(ValueError, match="persisted meeting roster"):
        CongressRuntime.resume("m", database, ["a", "c"])


def test_merge_approval_is_explicit_and_replayable(tmp_path: Path) -> None:
    runtime = CongressRuntime("m4", tmp_path / "events.db", ["a", "b"])
    runtime.propose_task(Task("ship", "Ship change", "b", ["tests pass"]))
    runtime.transition_task("ship", TaskStatus.ASSIGNED, "b")
    runtime.transition_task("ship", TaskStatus.ACCEPTED, "b")
    runtime.transition_task("ship", TaskStatus.RUNNING, "b")
    runtime.submit_task_report("ship", TaskReport("done", (), (), (), None, False), "b")
    identity = GitIdentity("agentcongress/m4/ship", "head", "tree")
    runtime.record_validation("ship", ValidationResult(True, (), (), git_identity=identity))
    runtime.mark_task_ready("ship", "b")
    runtime.request_merge_approval("ship")
    runtime.decide_merge_approval("ship", True, "operator")
    assert runtime.state.approvals["ship"] == ApprovalDecision.APPROVED
    assert runtime.state.approval_git_identities["ship"] == identity
    runtime.close()

    recovered = CongressRuntime.resume("m4", tmp_path / "events.db", ["a", "b"])
    assert recovered.state.validation_results["ship"].git_identity == identity
    assert recovered.state.approval_git_identities["ship"] == identity
    recovered.close()


def test_integration_records_merge_commits_and_deduplicates_task_checks(tmp_path: Path) -> None:
    database = tmp_path / "events.db"
    runtime = CongressRuntime("m5", database, ["a", "b"])
    for task_id, commands in (
        ("one", ["check-common", "check-one"]),
        ("two", ["check-common", "check-two"]),
    ):
        runtime.propose_task(Task(task_id, task_id, "b", ["done"], validation_commands=commands))
        runtime.transition_task(task_id, TaskStatus.ASSIGNED, "b")
        runtime.transition_task(task_id, TaskStatus.ACCEPTED, "b")
        runtime.transition_task(task_id, TaskStatus.RUNNING, "b")
        runtime.submit_task_report(task_id, TaskReport("done", (), (), (), None, False), "b")
        runtime.record_validation(task_id, ValidationResult(True, (), (), git_identity=GitIdentity(f"agentcongress/m5/{task_id}", f"head-{task_id}", f"tree-{task_id}")))
        runtime.mark_task_ready(task_id, "b")
        runtime.record_task_integration(task_id, f"merge-{task_id}")

    assert runtime.integrated_validation_commands() == ("check-common", "check-one", "check-two")
    assert runtime.state.tasks["one"].status == TaskStatus.INTEGRATED
    assert runtime.state.task_merge_commits == {"one": "merge-one", "two": "merge-two"}
    runtime.record_integration_validation(ValidationResult(False, (), (), ("combined failure",)))
    with pytest.raises(ValueError, match="combined integration validation"):
        runtime.assert_integration_verified()
    runtime.close()

    recovered = CongressRuntime.resume("m5", database, ["a", "b"])
    assert recovered.state.task_merge_commits == {"one": "merge-one", "two": "merge-two"}
    assert recovered.state.integration_validation_result is not None
    assert not recovered.state.integration_validation_result.passed
    recovered.close()
