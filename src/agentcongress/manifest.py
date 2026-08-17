from __future__ import annotations

import hashlib
import json
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .accounting import BudgetGovernor


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_revision(path: Path) -> str | None:
    resolved = path.resolve()
    result = subprocess.run(["git", "-c", f"safe.directory={resolved.as_posix()}", "-C", str(resolved), "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return result.stdout.strip() if result.returncode == 0 else None


def working_tree_sha256(path: Path) -> str | None:
    """Fingerprint tracked and unignored files as they exist at run start.

    A Git revision alone is insufficient when a local harness has deliberate,
    uncommitted changes.  This keeps generated/ignored run artifacts out while
    including both modified tracked files and untracked source files.
    """
    resolved = path.resolve()
    listed = subprocess.run(
        ["git", "-c", f"safe.directory={resolved.as_posix()}", "-C", str(resolved), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True,
    )
    if listed.returncode:
        return None
    digest = hashlib.sha256()
    for raw_name in sorted(name for name in listed.stdout.split(b"\0") if name):
        name = raw_name.decode("utf-8", errors="surrogateescape")
        candidate = resolved / name
        if not candidate.is_file():
            continue
        digest.update(raw_name)
        digest.update(b"\0")
        digest.update(sha256_file(candidate).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass(slots=True)
class RunManifest:
    """Immutable run inputs plus append-only outcome data for a benchmark trial."""

    run_id: str
    condition: str
    task_id: str
    model: str
    reasoning_effort: str
    task_config_sha256: str
    repository_revision: str | None
    harness_revision: str | None
    harness_tree_sha256: str | None
    budget: dict[str, Any]
    codex_backend: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    status: str = "running"
    outcome: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        condition: str,
        task_id: str,
        model: str,
        reasoning_effort: str,
        task_config: Path,
        repository: Path,
        harness_root: Path,
        governor: BudgetGovernor,
        codex_backend: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> "RunManifest":
        return cls(
            run_id=run_id or f"{task_id}-{condition}-{uuid.uuid4().hex[:10]}",
            condition=condition,
            task_id=task_id,
            model=model,
            reasoning_effort=reasoning_effort,
            task_config_sha256=sha256_file(task_config),
            repository_revision=git_revision(repository),
            harness_revision=git_revision(harness_root),
            harness_tree_sha256=working_tree_sha256(harness_root),
            budget=governor.snapshot(),
            codex_backend=codex_backend,
        )

    def complete(self, status: str, outcome: dict[str, Any], governor: BudgetGovernor) -> None:
        self.status = status
        self.completed_at = time.time()
        self.outcome = outcome
        self.budget = governor.snapshot()

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n", encoding="utf-8")
