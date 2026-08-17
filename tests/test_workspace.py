import subprocess
from pathlib import Path

import pytest

from agentcongress.workspace import WorkspaceError, WorkspaceManager
from agentcongress.verification import git_identity


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_creates_isolated_task_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    manager = WorkspaceManager(repo, "meeting")
    manager.ensure_clean_base()
    task = manager.create_task("backend")
    assert task.path.exists()
    assert task.branch == "agentcongress/meeting/backend"


def test_integrates_and_promotes_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    manager = WorkspaceManager(repo, "meeting")
    integration = manager.create_integration()
    task = manager.create_task("backend")
    (task.path / "README.md").write_text("changed", encoding="utf-8")
    _git(task.path, "add", "README.md")
    _git(task.path, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "task")
    merge_commit = manager.integrate(task, integration, git_identity(task.path))
    assert merge_commit == _git(integration.path, "rev-parse", "HEAD")
    assert len(_git(integration.path, "rev-list", "--parents", "-n", "1", merge_commit).split()) == 3
    manager.promote(integration, git_identity(integration.path))
    assert (repo / "README.md").read_text(encoding="utf-8") == "changed"


def test_integrate_snapshots_uncommitted_worker_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    manager = WorkspaceManager(repo, "meeting")
    integration = manager.create_integration()
    task = manager.create_task("backend")
    (task.path / "README.md").write_text("uncommitted change", encoding="utf-8")
    (task.path / "new.py").write_text("value = 1\n", encoding="utf-8")

    manager.integrate(task, integration, git_identity(task.path))

    assert (integration.path / "README.md").read_text(encoding="utf-8") == "uncommitted change"
    assert (integration.path / "new.py").read_text(encoding="utf-8") == "value = 1\n"


def test_integrate_rejects_content_changed_after_validation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    manager = WorkspaceManager(repo, "meeting")
    integration = manager.create_integration()
    task = manager.create_task("backend")
    (task.path / "README.md").write_text("validated", encoding="utf-8")
    validated = git_identity(task.path)

    (task.path / "README.md").write_text("changed later", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="changed after validation"):
        manager.integrate(task, integration, validated)


def test_promote_rejects_branch_switched_after_validation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    manager = WorkspaceManager(repo, "meeting")
    integration = manager.create_integration()
    validated = git_identity(integration.path)
    _git(integration.path, "switch", "-c", "unexpected")

    with pytest.raises(WorkspaceError, match="changed after validation"):
        manager.promote(integration, validated)


def test_get_or_create_task_verifies_existing_git_identity(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    manager = WorkspaceManager(repo, "meeting")
    manager.create_integration()
    created = manager.create_task("backend")

    recovered = manager.get_or_create_task(
        "backend", expected_base_revision=created.base_revision
    )
    assert recovered == created

    _git(created.path, "switch", "-c", "wrong")
    with pytest.raises(WorkspaceError, match="branch mismatch"):
        manager.get_or_create_task("backend", expected_base_revision=created.base_revision)


def test_get_or_create_task_rejects_modified_unrecorded_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    manager = WorkspaceManager(repo, "meeting")
    manager.create_integration()
    created = manager.create_task("backend")
    (created.path / "README.md").write_text("untrusted", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="untouched preparation artifact"):
        manager.get_or_create_task("backend")
