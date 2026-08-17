from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import GitIdentity
from .verification import git_identity


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TaskWorktree:
    task_id: str
    branch: str
    path: Path
    base_revision: str | None = None


class WorkspaceManager:
    """Creates isolated task branches and a meeting-level integration branch."""

    def __init__(self, repository: Path, meeting_id: str, base_ref: str = "HEAD") -> None:
        self.repository = repository.resolve()
        self.meeting_id = meeting_id
        self.base_ref = base_ref
        self.root = self.repository / ".agentcongress" / "worktrees" / meeting_id

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={self.repository.as_posix()}", "-C", str(self.repository), *args],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise WorkspaceError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    @staticmethod
    def _git_at(path: Path, *args: str) -> str:
        resolved = path.resolve()
        result = subprocess.run(
            ["git", "-c", f"safe.directory={resolved.as_posix()}", "-C", str(resolved), *args],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise WorkspaceError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    @staticmethod
    def _absolute_git_path(worktree: Path, value: str) -> Path:
        path = Path(value)
        return path.resolve() if path.is_absolute() else (worktree / path).resolve()

    def _verify_existing(
        self,
        task_id: str,
        branch: str,
        path: Path,
        *,
        expected_base_revision: str | None = None,
    ) -> TaskWorktree:
        if not path.is_dir():
            raise WorkspaceError(f"expected worktree is missing: {path}")
        top_level = Path(self._git_at(path, "rev-parse", "--show-toplevel")).resolve()
        if top_level != path.resolve():
            raise WorkspaceError(f"existing path is not the expected worktree root: {path}")
        repository_common = self._absolute_git_path(
            self.repository, self._git("rev-parse", "--git-common-dir")
        )
        worktree_common = self._absolute_git_path(
            path, self._git_at(path, "rev-parse", "--git-common-dir")
        )
        if worktree_common != repository_common:
            raise WorkspaceError(f"existing worktree belongs to a different repository: {path}")
        actual_branch = self._git_at(path, "symbolic-ref", "--quiet", "--short", "HEAD")
        if actual_branch != branch:
            raise WorkspaceError(
                f"existing worktree branch mismatch: expected {branch}, found {actual_branch}"
            )
        head = self._git_at(path, "rev-parse", "HEAD")
        if expected_base_revision is not None:
            result = subprocess.run(
                [
                    "git", "-c", f"safe.directory={path.resolve().as_posix()}",
                    "-C", str(path), "merge-base", "--is-ancestor",
                    expected_base_revision, head,
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode:
                raise WorkspaceError("existing worktree does not descend from its persisted base revision")
            base_revision = expected_base_revision
        elif task_id == "integration":
            base_revision = head
        else:
            integration_branch = f"agentcongress/integration/{self.meeting_id}"
            base_revision = self._git("merge-base", branch, integration_branch)
            if head != base_revision or self._git_at(path, "status", "--porcelain"):
                raise WorkspaceError(
                    "unrecorded task worktree is not an untouched preparation artifact"
                )
        return TaskWorktree(task_id, branch, path, base_revision)

    @staticmethod
    def _merge_command(worktree: Path, source_branch: str) -> list[str]:
        return ["git", "-C", str(worktree), "-c", "user.name=AgentCongress", "-c", "user.email=agentcongress@local", "merge", "--no-ff", "--no-edit", source_branch]

    def ensure_clean_base(self) -> None:
        changes = [line for line in self._git("status", "--porcelain").splitlines() if not line.endswith(".agentcongress/") and ".agentcongress/" not in line]
        if changes:
            raise WorkspaceError("write-capable meetings require a clean base checkout")

    def create_integration(self) -> TaskWorktree:
        branch = f"agentcongress/integration/{self.meeting_id}"
        path = self.root / "integration"
        return self._create("integration", branch, path)

    def get_or_create_integration(self) -> TaskWorktree:
        branch = f"agentcongress/integration/{self.meeting_id}"
        path = self.root / "integration"
        if path.exists():
            return self._verify_existing("integration", branch, path)
        return self.create_integration()

    def create_task(self, task_id: str) -> TaskWorktree:
        if not task_id.replace("-", "").replace("_", "").isalnum():
            raise WorkspaceError("task id must be alphanumeric, hyphen, or underscore")
        branch = f"agentcongress/{self.meeting_id}/{task_id}"
        return self._create(task_id, branch, self.root / task_id)

    def get_or_create_task(
        self, task_id: str, *, expected_base_revision: str | None = None
    ) -> TaskWorktree:
        if not task_id.replace("-", "").replace("_", "").isalnum():
            raise WorkspaceError("task id must be alphanumeric, hyphen, or underscore")
        branch = f"agentcongress/{self.meeting_id}/{task_id}"
        path = self.root / task_id
        if path.exists():
            return self._verify_existing(
                task_id, branch, path, expected_base_revision=expected_base_revision
            )
        if expected_base_revision is not None:
            raise WorkspaceError(f"persisted task worktree is missing: {path}")
        return self._create(task_id, branch, path)

    def _create(self, task_id: str, branch: str, path: Path) -> TaskWorktree:
        if path.exists():
            raise WorkspaceError(f"worktree already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        base_revision = self._git("rev-parse", self.base_ref)
        self._git("worktree", "add", "-b", branch, str(path), self.base_ref)
        return TaskWorktree(task_id=task_id, branch=branch, path=path, base_revision=base_revision)

    def peer_diff(self, task: TaskWorktree) -> str:
        return self._git("-C", str(task.path), "diff", "--no-ext-diff", "HEAD")

    @staticmethod
    def _assert_identity(worktree: TaskWorktree, expected: GitIdentity) -> None:
        current = git_identity(worktree.path)
        if current != expected:
            raise WorkspaceError("Git identity changed after validation")
        if current.branch != worktree.branch:
            raise WorkspaceError(f"worktree is on {current.branch}, expected {worktree.branch}")

    def integrate(self, task: TaskWorktree, integration: TaskWorktree, validated_identity: GitIdentity) -> str:
        self._assert_identity(task, validated_identity)
        # Workers commonly leave a validated working-tree patch rather than a
        # commit.  Snapshot it on the task branch so the subsequent Git merge
        # cannot silently discard the actual deliverable.
        status = subprocess.run(
            ["git", "-c", f"safe.directory={task.path.resolve().as_posix()}", "-C", str(task.path), "status", "--porcelain"],
            capture_output=True,
            text=True,
        )
        if status.returncode:
            raise WorkspaceError(status.stderr.strip() or status.stdout.strip())
        if status.stdout.strip():
            add = subprocess.run(
                ["git", "-c", f"safe.directory={task.path.resolve().as_posix()}", "-C", str(task.path), "add", "--all"],
                capture_output=True,
                text=True,
            )
            if add.returncode:
                raise WorkspaceError(add.stderr.strip() or add.stdout.strip())
            commit = subprocess.run(
                [
                    "git", "-c", f"safe.directory={task.path.resolve().as_posix()}",
                    "-C", str(task.path), "-c", "user.name=AgentCongress",
                    "-c", "user.email=agentcongress@local", "commit",
                    "-m", f"AgentCongress task {task.task_id}",
                ],
                capture_output=True,
                text=True,
            )
            if commit.returncode:
                raise WorkspaceError(commit.stderr.strip() or commit.stdout.strip())
        source_commit = self._git("-C", str(task.path), "rev-parse", "HEAD")
        source_tree = self._git("-C", str(task.path), "rev-parse", "HEAD^{tree}")
        source_branch = self._git("-C", str(task.path), "symbolic-ref", "--quiet", "--short", "HEAD")
        if source_tree != validated_identity.tree or source_branch != task.branch:
            raise WorkspaceError("task changed while its validated snapshot was being committed")
        result = subprocess.run(
            self._merge_command(integration.path, source_commit),
            capture_output=True,
            text=True,
        )
        if result.returncode:
            subprocess.run(["git", "-C", str(integration.path), "merge", "--abort"], capture_output=True, text=True)
            raise WorkspaceError(result.stderr.strip() or result.stdout.strip())
        revision = subprocess.run(
            [
                "git", "-c", f"safe.directory={integration.path.resolve().as_posix()}",
                "-C", str(integration.path), "rev-parse", "HEAD",
            ],
            capture_output=True,
            text=True,
        )
        if revision.returncode:
            raise WorkspaceError(revision.stderr.strip() or revision.stdout.strip())
        return revision.stdout.strip()

    def promote(self, integration: TaskWorktree, validated_identity: GitIdentity, target_branch: str = "main") -> None:
        self._assert_identity(integration, validated_identity)
        committed_tree = self._git("-C", str(integration.path), "rev-parse", "HEAD^{tree}")
        if validated_identity.tree != committed_tree:
            raise WorkspaceError("promotion requires a clean, committed integration tree")
        self.ensure_clean_base()
        if self._git("branch", "--show-current") != target_branch:
            raise WorkspaceError(f"promotion requires target branch {target_branch}")
        result = subprocess.run(
            self._merge_command(self.repository, validated_identity.head),
            capture_output=True,
            text=True,
        )
        if result.returncode:
            subprocess.run(["git", "-C", str(self.repository), "merge", "--abort"], capture_output=True, text=True)
            raise WorkspaceError(result.stderr.strip() or result.stdout.strip())
