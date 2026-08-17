import subprocess
import sys
import json
from pathlib import Path

import pytest

from agentcongress.cli import main
from agentcongress.models import TaskStatus
from agentcongress.runtime import CongressRuntime
from agentcongress.workspace import WorkspaceError, WorkspaceManager


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _run(monkeypatch, *arguments: str) -> None:
    monkeypatch.setattr(sys, "argv", ["agentcongress", *arguments])
    assert main() == 0


def test_manual_task_workflow_promotes_an_approved_change(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    config = tmp_path / "meeting.yaml"
    config.write_text(f'''meeting:\n  id: demo\n  initial_speaker: architect\n  initial_addressee: implementer\n  workspace:\n    repository: "{repo.as_posix()}"\n    merge_policy: manual\n  agents:\n    - {{id: architect, role: architect}}\n    - {{id: implementer, role: implementer}}\n''', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(monkeypatch, "run", str(config))
    _run(monkeypatch, "task-create", str(config), "change", "implementer", "Change README", "--criterion", "README changes")
    _run(monkeypatch, "task-prepare", str(config), "change")
    worktree = repo / ".agentcongress" / "worktrees" / "demo" / "change"
    (worktree / "README.md").write_text("changed", encoding="utf-8")
    _git(worktree, "add", "README.md")
    _git(worktree, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "change")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"summary": "Changed the README", "changed_files": ["README.md"], "validation": [], "risks": [], "commit": None, "needs_human_input": False}), encoding="utf-8")
    _run(monkeypatch, "task-report", str(config), "change", "--file", str(report))
    _run(monkeypatch, "task-request-approval", str(config), "change")
    _run(monkeypatch, "approve", str(config), "change")
    (worktree / "README.md").write_text("changed after approval", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["agentcongress", "task-integrate", str(config), "change"])
    with pytest.raises(ValueError, match="approved Git identity"):
        main()
    (worktree / "README.md").write_text("changed", encoding="utf-8")
    _run(monkeypatch, "task-integrate", str(config), "change")
    _run(monkeypatch, "task-promote", str(config))
    assert (repo / "README.md").read_text(encoding="utf-8") == "changed"

    database = tmp_path / ".agentcongress" / "runs" / "demo" / "events.db"
    recovered = CongressRuntime.resume("demo", database, ["architect", "implementer"])
    merge_commit = recovered.state.task_merge_commits["change"]
    assert merge_commit == _git_output(repo, "rev-parse", "agentcongress/integration/demo")
    assert recovered.state.integration_validation_result is not None
    assert recovered.state.integration_validation_result.passed
    assert recovered.state.integration_validation_result.git_identity is not None
    assert recovered.state.integration_validation_result.git_identity.tree == _git_output(integration_path := repo / ".agentcongress" / "worktrees" / "demo" / "integration", "rev-parse", "HEAD^{tree}")
    assert recovered.state.approval_git_identities["change"] == recovered.state.validation_results["change"].git_identity
    recovered.close()


def test_promotion_persists_failed_combined_validation_and_does_not_merge(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    config = tmp_path / "meeting.yaml"
    config.write_text(f'''meeting:\n  id: validation-demo\n  initial_speaker: architect\n  initial_addressee: implementer\n  workspace:\n    repository: "{repo.as_posix()}"\n    merge_policy: manual\n  agents:\n    - {{id: architect, role: architect}}\n    - {{id: implementer, role: implementer}}\n''', encoding="utf-8")
    check = "{python} -c \"from pathlib import Path; raise SystemExit(Path('README.md').read_text() != 'changed')\""
    monkeypatch.chdir(tmp_path)
    _run(monkeypatch, "run", str(config))
    _run(
        monkeypatch,
        "task-create", str(config), "change", "implementer", "Change README",
        "--criterion", "README changes", "--validate", check,
    )
    _run(monkeypatch, "task-prepare", str(config), "change")
    worktree = repo / ".agentcongress" / "worktrees" / "validation-demo" / "change"
    (worktree / "README.md").write_text("changed", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"summary": "Changed README", "changed_files": ["README.md"], "validation": [check], "risks": [], "commit": None, "needs_human_input": False}), encoding="utf-8")
    _run(monkeypatch, "task-report", str(config), "change", "--file", str(report))
    _run(monkeypatch, "task-request-approval", str(config), "change")
    _run(monkeypatch, "approve", str(config), "change")
    _run(monkeypatch, "task-integrate", str(config), "change")

    integration = repo / ".agentcongress" / "worktrees" / "validation-demo" / "integration"
    (integration / "README.md").write_text("broken combination", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["agentcongress", "task-promote", str(config)])
    with pytest.raises(ValueError, match="combined integration validation"):
        main()

    assert (repo / "README.md").read_text(encoding="utf-8") == "base"
    database = tmp_path / ".agentcongress" / "runs" / "validation-demo" / "events.db"
    recovered = CongressRuntime.resume("validation-demo", database, ["architect", "implementer"])
    assert recovered.state.tasks["change"].status == TaskStatus.INTEGRATED
    assert recovered.state.integration_validation_result is not None
    assert not recovered.state.integration_validation_result.passed
    assert recovered.store.replay("validation-demo")[-1].type == "integration.validation_completed"
    recovered.close()


def test_retry_reuses_a_blocked_tasks_existing_worktree(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    config = tmp_path / "meeting.yaml"
    config.write_text(f'''meeting:\n  id: retry-demo\n  initial_speaker: architect\n  initial_addressee: implementer\n  workspace:\n    repository: "{repo.as_posix()}"\n  agents:\n    - {{id: architect, role: architect}}\n    - {{id: implementer, role: implementer}}\n''', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(monkeypatch, "run", str(config))
    _run(monkeypatch, "task-create", str(config), "change", "implementer", "Change README", "--criterion", "README changes")
    _run(monkeypatch, "task-prepare", str(config), "change")
    database = tmp_path / ".agentcongress" / "runs" / "retry-demo" / "events.db"
    runtime = CongressRuntime.resume("retry-demo", database, ["architect", "implementer"])
    runtime.transition_task("change", TaskStatus.RUNNING, "implementer")
    runtime.transition_task("change", TaskStatus.BLOCKED, "implementer")
    runtime.close()

    _run(monkeypatch, "task-retry", str(config), "change")

    recovered = CongressRuntime.resume("retry-demo", database, ["architect", "implementer"])
    assert recovered.state.tasks["change"].status == TaskStatus.ACCEPTED
    assert "change" in recovered.state.task_base_revisions
    recovered.close()


def test_manual_human_input_report_blocks_without_recording_validation(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    config = tmp_path / "meeting.yaml"
    config.write_text(f'''meeting:\n  id: manual-block\n  initial_speaker: architect\n  initial_addressee: implementer\n  workspace:\n    repository: "{repo.as_posix()}"\n  agents:\n    - {{id: architect, role: architect}}\n    - {{id: implementer, role: implementer}}\n''', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(monkeypatch, "run", str(config))
    _run(monkeypatch, "task-create", str(config), "change", "implementer", "Change README", "--criterion", "README changes")
    _run(monkeypatch, "task-prepare", str(config), "change")
    report = tmp_path / "blocked.json"
    report.write_text(json.dumps({"summary": "Need a decision", "changed_files": [], "validation": [], "risks": [], "commit": None, "needs_human_input": True}), encoding="utf-8")
    _run(monkeypatch, "task-report", str(config), "change", "--file", str(report))
    runtime = CongressRuntime.resume("manual-block", tmp_path / ".agentcongress" / "runs" / "manual-block" / "events.db", ["architect", "implementer"])
    assert runtime.state.tasks["change"].status == TaskStatus.BLOCKED
    assert "change" not in runtime.state.validation_results
    runtime.close()


@pytest.mark.parametrize("accepted_before_crash", [False, True])
def test_task_prepare_recovers_verified_worktrees_without_duplicate_events(
    tmp_path: Path, monkeypatch, accepted_before_crash: bool
) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    config = tmp_path / "meeting.yaml"
    config.write_text(f'''meeting:\n  id: recover-prepare\n  initial_speaker: architect\n  initial_addressee: implementer\n  workspace:\n    repository: "{repo.as_posix()}"\n  agents:\n    - {{id: architect, role: architect}}\n    - {{id: implementer, role: implementer}}\n''', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(monkeypatch, "run", str(config))
    _run(monkeypatch, "task-create", str(config), "change", "implementer", "Change README", "--criterion", "README changes")

    manager = WorkspaceManager(repo, "recover-prepare")
    manager.get_or_create_integration()
    orphaned = manager.create_task("change")
    database = tmp_path / ".agentcongress" / "runs" / "recover-prepare" / "events.db"
    if accepted_before_crash:
        runtime = CongressRuntime.resume("recover-prepare", database, ["architect", "implementer"])
        runtime.transition_task("change", TaskStatus.ACCEPTED)
        runtime.close()

    _run(monkeypatch, "task-prepare", str(config), "change")
    recovered = CongressRuntime.resume("recover-prepare", database, ["architect", "implementer"])
    events_after_recovery = recovered.store.replay("recover-prepare")
    assert recovered.state.tasks["change"].status == TaskStatus.ACCEPTED
    assert recovered.state.task_base_revisions["change"] == orphaned.base_revision
    assert sum(event.type == "workspace.task_created" for event in events_after_recovery) == 1
    recovered.close()

    _run(monkeypatch, "task-prepare", str(config), "change")
    replayed = CongressRuntime.resume("recover-prepare", database, ["architect", "implementer"])
    assert len(replayed.store.replay("recover-prepare")) == len(events_after_recovery)
    replayed.close()


def test_task_prepare_rejects_an_existing_worktree_with_the_wrong_identity(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    config = tmp_path / "meeting.yaml"
    config.write_text(f'''meeting:\n  id: reject-prepare\n  initial_speaker: architect\n  initial_addressee: implementer\n  workspace:\n    repository: "{repo.as_posix()}"\n  agents:\n    - {{id: architect, role: architect}}\n    - {{id: implementer, role: implementer}}\n''', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(monkeypatch, "run", str(config))
    _run(monkeypatch, "task-create", str(config), "change", "implementer", "Change README", "--criterion", "README changes")
    expected = repo / ".agentcongress" / "worktrees" / "reject-prepare" / "change"
    expected.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "-b", "wrong-branch", str(expected), "HEAD")

    monkeypatch.setattr(sys, "argv", ["agentcongress", "task-prepare", str(config), "change"])
    with pytest.raises(WorkspaceError, match="branch mismatch"):
        main()

    database = tmp_path / ".agentcongress" / "runs" / "reject-prepare" / "events.db"
    recovered = CongressRuntime.resume("reject-prepare", database, ["architect", "implementer"])
    assert recovered.state.tasks["change"].status == TaskStatus.ASSIGNED
    assert "change" not in recovered.state.task_base_revisions
    recovered.close()


def _git_output(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()
