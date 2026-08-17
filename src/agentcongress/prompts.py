from __future__ import annotations

from .models import Task


def build_worker_prompt(task: Task, additional_instruction: str = "") -> str:
    """Create the bounded instruction passed to an isolated coding worker."""
    criteria = "\n".join(f"- {criterion}" for criterion in task.acceptance_criteria)
    paths = "\n".join(f"- {path}" for path in task.allowed_paths) or "- No path restriction was specified."
    validation = "\n".join(f"- {command}" for command in task.validation_commands) or "- Run the relevant project checks when practical."
    suffix = f"\nAdditional meeting instruction:\n{additional_instruction.strip()}" if additional_instruction.strip() else ""
    return f"""You are the worker assigned to an AgentCongress task. Work only in the current Git worktree.

Task id: {task.task_id}
Title: {task.title}

Acceptance criteria:
{criteria}

Allowed paths:
{paths}

Suggested validation:
{validation}

Implement the task, inspect existing code before changing it, and run appropriate validation. Do not merge branches, promote changes, modify Git configuration, access peer worktrees, or use a destructive Git reset. Make a normal commit only if the repository's task workflow requires it. Your final response must conform to the supplied JSON report schema.{suffix}
"""
