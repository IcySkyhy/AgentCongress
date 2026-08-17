from __future__ import annotations

import subprocess
import sys
import os
import tempfile
from pathlib import Path

from .models import GitIdentity, Task, TaskReport, ValidationResult


class VerificationError(RuntimeError):
    pass


def _verification_environment() -> dict[str, str]:
    """Minimal host environment; a container remains the required hard boundary."""
    keep = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL")
    return {name: os.environ[name] for name in keep if name in os.environ}


def _git(worktree: Path, *args: str, env: dict[str, str] | None = None) -> str:
    resolved = worktree.resolve()
    result = subprocess.run(
        ["git", "-c", f"safe.directory={resolved.as_posix()}", "-C", str(resolved), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if result.returncode:
        raise VerificationError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def git_identity(worktree: Path) -> GitIdentity:
    """Return branch, HEAD, and the exact tracked/untracked deliverable tree.

    A temporary index lets Git hash unstaged and untracked (non-ignored) files
    without changing the worker's real index or worktree.
    """
    resolved = worktree.resolve()
    branch = _git(resolved, "symbolic-ref", "--quiet", "--short", "HEAD")
    head = _git(resolved, "rev-parse", "HEAD")
    descriptor, index_name = tempfile.mkstemp(prefix="agentcongress-index-")
    os.close(descriptor)
    os.unlink(index_name)
    environment = os.environ.copy()
    environment["GIT_INDEX_FILE"] = index_name
    try:
        _git(resolved, "read-tree", "HEAD", env=environment)
        _git(resolved, "add", "--all", "--", ".", env=environment)
        tree = _git(resolved, "write-tree", env=environment)
    finally:
        try:
            os.unlink(index_name)
        except FileNotFoundError:
            pass
    return GitIdentity(branch=branch, head=head, tree=tree)


def changed_files(worktree: Path, base_revision: str) -> tuple[str, ...]:
    """Include committed, staged, and unstaged changes since the captured base."""
    names: set[str] = set()
    for arguments in (("diff", "--name-only", f"{base_revision}...HEAD"), ("diff", "--name-only"), ("diff", "--cached", "--name-only")):
        names.update(name.replace("\\", "/") for name in _git(worktree, *arguments).splitlines() if name.strip())
    # `git diff` deliberately omits untracked files, but they are part of the
    # worker's deliverable and can affect validation/scoring just as much as a
    # tracked edit.
    names.update(
        name.replace("\\", "/")
        for name in _git(worktree, "ls-files", "--others", "--exclude-standard").splitlines()
        if name.strip()
    )
    return tuple(sorted(names))


def _is_allowed(path: str, allowed_paths: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    for allowed in allowed_paths:
        root = allowed.replace("\\", "/").strip()
        while root.startswith("./"):
            root = root[2:]
        root = root.rstrip("/")
        if root and (normalized == root or normalized.startswith(f"{root}/")):
            return True
    return False


def _run_validation_commands(
    worktree: Path,
    templates: list[str] | tuple[str, ...],
    command_timeout_seconds: float,
) -> tuple[tuple[dict[str, object], ...], tuple[str, ...]]:
    commands: list[dict[str, object]] = []
    errors: list[str] = []
    for template in templates:
        command = template.format(python=sys.executable)
        try:
            result = subprocess.run(
                command,
                cwd=worktree,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=command_timeout_seconds,
                env=_verification_environment(),
            )
            output = (result.stdout + result.stderr)[-4000:]
            commands.append({"command": command, "returncode": result.returncode, "output": output})
            if result.returncode:
                errors.append(f"validation failed: {command}")
        except subprocess.TimeoutExpired:
            commands.append({"command": command, "returncode": None, "output": "timed out"})
            errors.append(f"validation timed out: {command}")
    return tuple(commands), tuple(errors)


def verify_integration(
    worktree: Path,
    validation_commands: list[str] | tuple[str, ...],
    *,
    command_timeout_seconds: float = 120.0,
) -> ValidationResult:
    """Run the deduplicated task checks against their combined integration tree."""
    commands, errors = _run_validation_commands(worktree, validation_commands, command_timeout_seconds)
    return ValidationResult(not errors, (), commands, errors, git_identity(worktree))


def verify_task(
    worktree: Path,
    task: Task,
    report: TaskReport,
    base_revision: str,
    *,
    command_timeout_seconds: float = 120.0,
) -> ValidationResult:
    errors: list[str] = []
    actual_files = changed_files(worktree, base_revision)
    if task.allowed_paths:
        disallowed = [path for path in actual_files if not _is_allowed(path, task.allowed_paths)]
        if disallowed:
            errors.append(f"changes outside allowed_paths: {', '.join(disallowed)}")
    declared = tuple(sorted(path.replace("\\", "/") for path in report.changed_files))
    if declared != actual_files:
        errors.append("task report changed_files does not match the Git diff")
    commands, validation_errors = _run_validation_commands(worktree, task.validation_commands, command_timeout_seconds)
    errors.extend(validation_errors)
    return ValidationResult(not errors, actual_files, commands, tuple(errors), git_identity(worktree))
