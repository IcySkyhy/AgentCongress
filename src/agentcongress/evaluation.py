from __future__ import annotations

import asyncio
import json
import math
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from .accounting import Budget, BudgetExceeded, BudgetGovernor
from .analysis import condition_for
from .adapters import CodexWorkerAdapter, WorkerEvent
from .errors import WorkerHumanInputRequired, WorkerInfrastructureError, WorkerProtocolError, WorkerTimeoutError, WorkerValidationError
from .manifest import RunManifest, git_revision, sha256_file
from .models import FloorIntent, FloorRequest, MeetingPhase, Task, TaskStatus
from .prompts import build_worker_prompt
from .runtime import CongressRuntime
from .verification import _verification_environment
from .workers import execute_worker_task


Strategy = Literal["self", "congress"]
ScoreDirection = Literal["lower", "higher"]
BackendMode = Literal["permission-profiles", "legacy"]
_PROTOCOL_VERSION = "three-slot-v1"


def _host_fingerprint(codex_executable: str = "codex") -> dict[str, str | None]:
    try:
        codex = subprocess.run([codex_executable, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        codex_version = codex.stdout.strip() if codex.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        codex_version = None
    return {
        "operating_system": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "codex_cli": codex_version,
    }


def _codex_backend_snapshot(codex_executable: str, codex_features: tuple[str, ...], codex_version: str | None, backend_mode: BackendMode) -> dict[str, Any]:
    resolved = shutil.which(codex_executable)
    return {
        "executable_as_supplied": codex_executable,
        "executable_resolved": str(Path(resolved).resolve()) if resolved is not None else None,
        "version": codex_version,
        "enabled_features": list(codex_features),
        "mode": backend_mode,
    }


def _effective_timeout(slot_cap: float, remaining: float) -> float:
    """Keep a slot fixed-cap while respecting the run-wide deadline."""
    return max(0.001, min(slot_cap, remaining))


def five_arm_definitions() -> tuple[tuple[str, Strategy, str, str], ...]:
    return (
        ("A", "self", "gpt-5.6-luna", "gpt-5.6-luna"),
        ("B", "congress", "gpt-5.6-luna", "gpt-5.6-luna"),
        ("C", "self", "gpt-5.6-sol", "gpt-5.6-sol"),
        ("D", "self", "gpt-5.6-luna", "gpt-5.6-sol"),
        ("E", "congress", "gpt-5.6-luna", "gpt-5.6-sol"),
    )


def _manifest_patch(harness_root: Path) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={harness_root.resolve().as_posix()}", "-C", str(harness_root.resolve()), "diff", "--binary", "HEAD", "--", "README.md", "docs", "examples/benchmarks", "src/agentcongress", "tests"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    patch = result.stdout if result.returncode == 0 else ""
    untracked = subprocess.run(
        ["git", "-c", f"safe.directory={harness_root.resolve().as_posix()}", "-C", str(harness_root.resolve()), "ls-files", "--others", "--exclude-standard", "-z", "--", "README.md", "docs", "examples/benchmarks", "src/agentcongress", "tests"],
        capture_output=True,
    )
    if untracked.returncode:
        return patch
    for raw_name in sorted(name for name in untracked.stdout.split(b"\0") if name):
        name = raw_name.decode("utf-8", errors="surrogateescape")
        candidate = harness_root.resolve() / name
        if not candidate.is_file():
            continue
        relative = name.replace("\\", "/")
        content = candidate.read_text(encoding="utf-8", errors="replace")
        patch += f"diff --git a/{relative} b/{relative}\nnew file mode 100644\n--- /dev/null\n+++ b/{relative}\n"
        lines = content.splitlines(keepends=True)
        if lines:
            patch += f"@@ -0,0 +1,{len(lines)} @@\n"
        for line in lines:
            patch += "+" + line
        if content and not content.endswith("\n"):
            patch += "\n\\ No newline at end of file\n"
    return patch


@dataclass(frozen=True, slots=True)
class ScoreSpec:
    direction: ScoreDirection
    baseline: float
    success_value: float | None = None


@dataclass(frozen=True, slots=True)
class BenchmarkTask:
    task_id: str
    title: str
    prompt: str
    allowed_paths: tuple[str, ...]
    validation_commands: tuple[str, ...]
    scoring_command: str
    score: ScoreSpec

    @classmethod
    def load(cls, path: Path) -> "BenchmarkTask":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        task = raw.get("task", raw)
        required = {"id", "title", "prompt", "allowed_paths", "validation_commands", "scoring_command", "score"}
        missing = required - set(task)
        if missing:
            raise ValueError(f"benchmark task config is missing: {', '.join(sorted(missing))}")
        if not isinstance(task["allowed_paths"], list) or not isinstance(task["validation_commands"], list):
            raise ValueError("allowed_paths and validation_commands must be lists")
        score = task["score"]
        if not isinstance(score, dict) or score.get("direction") not in {"lower", "higher"}:
            raise ValueError("score must declare direction as lower or higher")
        baseline = score.get("baseline")
        if isinstance(baseline, bool) or not isinstance(baseline, (int, float)) or not math.isfinite(float(baseline)):
            raise ValueError("score baseline must be a finite number")
        success_value = score.get("success_value")
        if success_value is not None and (isinstance(success_value, bool) or not isinstance(success_value, (int, float)) or not math.isfinite(float(success_value))):
            raise ValueError("score success_value must be a finite number when provided")
        return cls(
            task_id=str(task["id"]),
            title=str(task["title"]),
            prompt=str(task["prompt"]),
            allowed_paths=tuple(map(str, task["allowed_paths"])),
            validation_commands=tuple(map(str, task["validation_commands"])),
            scoring_command=str(task["scoring_command"]),
            score=ScoreSpec(str(score["direction"]), float(baseline), float(success_value) if success_value is not None else None),  # type: ignore[arg-type]
        )


def _trusted_structured(events: list[WorkerEvent], required: set[str]) -> dict[str, Any] | None:
    """Read only explicit reports or terminal assistant messages, never tool output."""

    def candidates(value: Any):
        if isinstance(value, dict):
            yield value
            for nested in value.values():
                yield from candidates(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from candidates(nested)
        elif isinstance(value, str):
            text = value.strip()
            if text.startswith("{") and text.endswith("}"):
                try:
                    yield from candidates(json.loads(text))
                except json.JSONDecodeError:
                    return

    result: dict[str, Any] | None = None
    for event in events:
        values: Any = None
        if event.type in {"deliberation.report", "critic.report"}:
            values = event.payload
        elif event.type == "codex.event" and event.payload.get("type") == "item.completed":
            item = event.payload.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                values = item.get("text")
        if values is None:
            continue
        for value in candidates(values):
            if set(value) == required:
                result = value
    return result


def _planner_memo(events: list[WorkerEvent]) -> dict[str, Any] | None:
    memo = _trusted_structured(events, {"summary", "hypotheses", "validation_plan", "risks"})
    if memo is None or not isinstance(memo["summary"], str):
        return None
    if not all(isinstance(memo[name], list) and all(isinstance(item, str) for item in memo[name]) for name in ("hypotheses", "validation_plan", "risks")):
        return None
    return memo


def _critic_report(events: list[WorkerEvent]) -> dict[str, Any] | None:
    report = _trusted_structured(events, {"intent", "reason", "content", "urgency", "relevance", "novelty", "confidence"})
    if report is None or report["intent"] not in {"abstain", "interject", "replace"}:
        return None
    if not isinstance(report["reason"], str) or not isinstance(report["content"], str):
        return None
    for name in ("urgency", "relevance", "novelty", "confidence"):
        value = report[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            return None
    return report


def _copy_repository(source: Path, destination: Path) -> str:
    if destination.exists():
        raise ValueError(f"experiment worktree already exists: {destination}")
    source = source.resolve()
    result = subprocess.run(
        ["git", "-c", f"safe.directory={source.as_posix()}", "-c", f"safe.directory={(source / '.git').as_posix()}", "clone", "--no-hardlinks", str(source), str(destination)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    revision = git_revision(destination)
    if revision is None:
        raise RuntimeError("benchmark repository clone has no Git revision")
    return revision


def _score(worktree: Path, harness_root: Path, command_template: str, expected: ScoreSpec, timeout_seconds: float = 180.0) -> dict[str, Any]:
    command = command_template.format(python=str(Path(sys.executable).resolve()), harness=str(harness_root.resolve()), worktree=str(worktree.resolve()))
    try:
        result = subprocess.run(command, cwd=worktree, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds, env=_verification_environment())
    except subprocess.TimeoutExpired:
        return {"valid": False, "command": command, "returncode": None, "error": "timed out", "direction": expected.direction, "baseline": expected.baseline}
    output = (result.stdout + result.stderr)[-8000:]
    if result.returncode:
        return {"valid": False, "command": command, "returncode": result.returncode, "error": "scorer command failed", "output": output, "direction": expected.direction, "baseline": expected.baseline}
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return {"valid": False, "command": command, "returncode": result.returncode, "error": "scorer did not emit a final JSON object", "output": output, "direction": expected.direction, "baseline": expected.baseline}
    value = payload.get("value")
    seed_results = payload.get("seed_results")
    thresholds = payload.get("thresholds_passed")
    valid = (
        payload.get("valid") is True
        and payload.get("direction") == expected.direction
        and payload.get("baseline") == expected.baseline
        and payload.get("success_value") == expected.success_value
        and not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and isinstance(seed_results, list)
        and all(not isinstance(item, bool) and isinstance(item, (int, float)) and math.isfinite(float(item)) for item in seed_results)
        and isinstance(thresholds, int)
        and thresholds >= 0
    )
    compact_output = output if not valid else "structured scorer output accepted"
    return {**payload, "valid": valid, "command": command, "returncode": result.returncode, "output": compact_output}


def _improved(value: float, score: ScoreSpec) -> bool:
    return value < score.baseline if score.direction == "lower" else value > score.baseline


def _objective_success(value: float, score: ScoreSpec) -> bool:
    if score.success_value is None:
        return _improved(value, score)
    return value <= score.success_value if score.direction == "lower" else value >= score.success_value


def _memo_text(memo: dict[str, Any]) -> str:
    return json.dumps(memo, ensure_ascii=False, sort_keys=True)


class ExperimentRunner:
    """Three fixed model slots: two read-only deliberations, then one executor."""

    def __init__(
        self,
        task_config: Path,
        repository: Path,
        runs_root: Path,
        harness_root: Path,
        *,
        codex_executable: str = "codex",
        codex_features: tuple[str, ...] = (),
        backend_mode: BackendMode = "permission-profiles",
    ) -> None:
        self.task_config = task_config.resolve()
        self.task = BenchmarkTask.load(self.task_config)
        self.repository = repository.resolve()
        self.runs_root = runs_root.resolve()
        self.harness_root = harness_root.resolve()
        self.codex_executable = codex_executable
        self.codex_features = tuple(codex_features)
        if backend_mode not in {"permission-profiles", "legacy"}:
            raise ValueError("backend_mode must be permission-profiles or legacy")
        if backend_mode == "permission-profiles" and "use_legacy_landlock" in self.codex_features:
            raise ValueError("use_legacy_landlock requires backend_mode='legacy'")
        self.backend_mode: BackendMode = backend_mode
        if git_revision(self.repository) is None:
            raise ValueError("benchmark repository must be a Git checkout")

    def _containment(self, sandbox: Literal["read-only", "workspace-write"]) -> dict[str, str | None]:
        if self.backend_mode == "permission-profiles":
            return {
                "sandbox": None,
                "permission_profile": ":read-only" if sandbox == "read-only" else ":workspace",
            }
        return {"sandbox": sandbox, "permission_profile": None}

    async def _raw_session(
        self,
        runtime: CongressRuntime,
        governor: BudgetGovernor,
        *,
        actor_id: str,
        prompt: str,
        worktree: Path,
        model: str,
        reasoning_effort: str,
        schema: Path,
        max_seconds: float,
    ) -> list[WorkerEvent]:
        timeout = governor.start_session(max_seconds)
        runtime.record("budget.worker_session_started", actor_id, {"reserved_seconds": timeout, "slot_cap_seconds": max_seconds, "snapshot": governor.snapshot()})
        events: list[WorkerEvent] = []
        try:
            adapter = CodexWorkerAdapter(
                executable=self.codex_executable,
                enabled_features=self.codex_features,
                model=model,
                reasoning_effort=reasoning_effort,
                timeout_seconds=timeout,
                **self._containment("read-only"),
            )
            async for event in adapter.run_task(prompt, worktree, schema):
                events.append(event)
                runtime.record("worker.event", actor_id, {"worker_type": event.type, "payload": event.payload})
                usage = governor.observe_event(event.payload, model=model)
                if usage is not None:
                    runtime.record("budget.usage_observed", actor_id, {"usage": usage.as_dict(), "snapshot": governor.snapshot()})
            return events
        finally:
            elapsed = governor.finish_session()
            runtime.record("budget.worker_session_finished", actor_id, {"elapsed_seconds": elapsed, "snapshot": governor.snapshot()})

    async def run(
        self,
        *,
        strategy: Strategy,
        model: str,
        planner_model: str | None = None,
        reasoning_effort: str,
        budget: Budget,
        deliberation_max_seconds: float = 180.0,
        executor_max_seconds: float = 840.0,
        run_id: str | None = None,
        pair_id: str | None = None,
        randomization_seed: int | None = None,
        realized_order: list[str] | None = None,
        arm_id: str | None = None,
    ) -> Path:
        if strategy not in {"self", "congress"}:
            raise ValueError("strategy must be self or congress")
        if budget.max_worker_sessions != 3:
            raise ValueError("the three-slot protocol requires exactly 3 worker sessions")
        if min(deliberation_max_seconds, executor_max_seconds) <= 0:
            raise ValueError("slot caps must be positive")
        if 2 * deliberation_max_seconds + executor_max_seconds > budget.max_wall_seconds:
            raise ValueError("run wall budget must cover all three fixed slot caps")
        planner_model = planner_model or model
        governor = BudgetGovernor(budget, model)
        condition = condition_for(strategy, planner_model, model)
        host = _host_fingerprint(self.codex_executable)
        codex_backend = _codex_backend_snapshot(self.codex_executable, self.codex_features, host["codex_cli"], self.backend_mode)
        manifest = RunManifest.create(condition=condition, task_id=self.task.task_id, model=model, reasoning_effort=reasoning_effort, task_config=self.task_config, repository=self.repository, harness_root=self.harness_root, governor=governor, codex_backend=codex_backend, run_id=run_id)
        run_dir = self.runs_root / manifest.run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        manifest_path = run_dir / "manifest.json"
        patch = _manifest_patch(self.harness_root)
        if patch:
            (run_dir / "harness.patch").write_text(patch, encoding="utf-8")
        manifest.write(manifest_path)
        worktree = run_dir / "worktree"
        runtime: CongressRuntime | None = None
        protocol = {
            "version": _PROTOCOL_VERSION,
            "strategy": strategy,
            "pair_id": pair_id,
            "arm_id": arm_id,
            "randomization_seed": randomization_seed,
            "realized_order": list(realized_order) if realized_order is not None else None,
            "network": "disabled",
            "codex_backend": codex_backend,
            "validation_command_timeout_seconds": 120,
            "scoring_timeout_seconds": 180,
            "slots": [
                {"actor": "analyst", "model": planner_model, "sandbox": "read-only", "permission_profile": self._containment("read-only")["permission_profile"], "max_seconds": deliberation_max_seconds},
                {"actor": "analyst" if strategy == "self" else "critic", "model": planner_model, "sandbox": "read-only", "permission_profile": self._containment("read-only")["permission_profile"], "max_seconds": deliberation_max_seconds},
                {"actor": "executor", "model": model, "sandbox": "workspace-write", "permission_profile": self._containment("workspace-write")["permission_profile"], "max_seconds": executor_max_seconds},
            ],
            "schemas": {},
        }
        outcome: dict[str, Any] = {
            "protocol": protocol,
            "pair_id": pair_id,
            "arm_id": arm_id,
            "strategy": strategy,
            "executor_model": model,
            "planner_model": planner_model,
            "execution_status": "infra_error",
            "objective_success": False,
            "infrastructure_failure": False,
            "score_config": {"direction": self.task.score.direction, "baseline": self.task.score.baseline, "success_value": self.task.score.success_value},
            "host": host,
            "harness_patch": "harness.patch" if patch else None,
        }
        status = "failed"
        active_stage = "setup"
        try:
            base_revision = _copy_repository(self.repository, worktree)
            roster = ["analyst", "executor"] if strategy == "self" else ["analyst", "critic", "executor"]
            runtime = CongressRuntime(manifest.run_id, run_dir / "events.db", roster)
            runtime.start("analyst", "executor")
            task = Task("implementation", self.task.title, "executor", ["Pass all configured validation commands", "Produce a valid structured score"], list(self.task.allowed_paths), list(self.task.validation_commands))
            runtime.propose_task(task)
            runtime.transition_task(task.task_id, TaskStatus.ASSIGNED, "executor")
            runtime.transition_task(task.task_id, TaskStatus.ACCEPTED, "executor")
            runtime.record_task_worktree(task.task_id, "benchmark-worktree", str(worktree), base_revision)
            task_schema = self.harness_root / "src" / "agentcongress" / "task-report.schema.json"
            planner_schema = self.harness_root / "src" / "agentcongress" / "planner-report.schema.json"
            critic_schema = self.harness_root / "src" / "agentcongress" / "critic-report.schema.json"
            protocol["schemas"] = {"task": sha256_file(task_schema), "planner": sha256_file(planner_schema), "critic": sha256_file(critic_schema)}
            runtime.record("experiment.protocol_frozen", "runtime", protocol)

            active_stage = "analyst"
            analyst_events = await self._raw_session(
                runtime,
                governor,
                actor_id="analyst",
                prompt=f"""You are the analyst in a coding deliberation. Inspect the repository read-only and return the required structured memo. Do not edit files or access the network.\n\nTask:\n{self.task.prompt}\n\nGive evidence-based hypotheses, an executable validation plan, and concrete risks.""",
                worktree=worktree,
                model=planner_model,
                reasoning_effort=reasoning_effort,
                schema=planner_schema,
                max_seconds=deliberation_max_seconds,
            )
            first_memo = _planner_memo(analyst_events)
            if first_memo is None:
                raise WorkerProtocolError("analyst finished without a schema-valid terminal memo")
            runtime.commit_segment(first_memo["summary"])
            runtime.add_blackboard("analyst_memo", first_memo["summary"], "analyst", task_id=task.task_id, evidence=[*first_memo["hypotheses"], *first_memo["validation_plan"], *first_memo["risks"]])

            floor_outcome = "not_applicable"
            second_artifact: dict[str, Any]
            if strategy == "self":
                active_stage = "self_critique"
                second_events = await self._raw_session(
                    runtime,
                    governor,
                    actor_id="analyst",
                    prompt=f"""You are the same analyst identity performing a read-only self-critique. Do not edit files or access the network. Correct weak assumptions and return a complete revised memo in the required schema.\n\nTask:\n{self.task.prompt}\n\nYour first memo:\n{_memo_text(first_memo)}\n\nTranscript:\n{runtime.recent_transcript()}""",
                    worktree=worktree,
                    model=planner_model,
                    reasoning_effort=reasoning_effort,
                    schema=planner_schema,
                    max_seconds=deliberation_max_seconds,
                )
                revised = _planner_memo(second_events)
                if revised is None:
                    raise WorkerProtocolError("self-critic finished without a schema-valid terminal memo")
                runtime.commit_segment(revised["summary"])
                runtime.add_blackboard("revised_memo", revised["summary"], "analyst", task_id=task.task_id, evidence=[*revised["hypotheses"], *revised["validation_plan"], *revised["risks"]])
                second_artifact = revised
            else:
                active_stage = "critic"
                critic_events = await self._raw_session(
                    runtime,
                    governor,
                    actor_id="critic",
                    prompt=f"""You are an independent listener in a coding meeting. Inspect the repository read-only, see the shared transcript, and decide autonomously whether to abstain, briefly interject, or replace the analyst. Do not edit files or access the network. Return only the required structured report.\n\nTask:\n{self.task.prompt}\n\nAnalyst memo:\n{_memo_text(first_memo)}\n\nTranscript:\n{runtime.recent_transcript()}""",
                    worktree=worktree,
                    model=planner_model,
                    reasoning_effort=reasoning_effort,
                    schema=critic_schema,
                    max_seconds=deliberation_max_seconds,
                )
                critic = _critic_report(critic_events)
                if critic is None:
                    raise WorkerProtocolError("critic finished without a schema-valid terminal report")
                intent = critic["intent"]
                if intent != "abstain" and not critic["content"].strip():
                    raise WorkerProtocolError("a critic requesting the floor must provide non-empty content")
                shared_content = ""
                if intent == "abstain":
                    runtime.resolve_floor([])
                    floor_outcome = "retained"
                else:
                    floor_intent = FloorIntent.BRIEF_INTERJECTION if intent == "interject" else FloorIntent.REPLACE_SPEAKER
                    request = FloorRequest("critic", floor_intent, float(critic["urgency"]), float(critic["relevance"]), float(critic["novelty"]), float(critic["confidence"]), critic["reason"])
                    runtime.request_floor(request)
                    decision = runtime.resolve_floor([request])
                    floor_outcome = "granted" if decision.type == "floor.granted" else "retained"
                    if decision.type == "floor.granted":
                        shared_content = critic["content"]
                        runtime.commit_segment(critic["content"])
                        if floor_intent == FloorIntent.BRIEF_INTERJECTION:
                            runtime.complete_brief_interjection()
                second_artifact = {"intent": intent, "reason": critic["reason"], "content": shared_content, "floor_outcome": floor_outcome}
                runtime.add_blackboard("floor_decision", f"{intent}: {floor_outcome}", "critic", task_id=task.task_id, evidence=[critic["reason"]])
            outcome["floor_outcome"] = floor_outcome
            outcome["deliberation_valid"] = True

            full_handoff = json.dumps({"analyst_memo": first_memo, "second_deliberation": second_artifact, "floor_outcome": floor_outcome, "transcript": runtime.state.transcript}, ensure_ascii=False, sort_keys=True)
            outcome["deliberation"] = {"analyst_memo": first_memo, "second_deliberation": second_artifact, "floor_outcome": floor_outcome}
            outcome["transcript"] = list(runtime.state.transcript)
            runtime.transition_phase(MeetingPhase.EXECUTING)
            active_stage = "executor"
            final_instruction = f"""You are the only participant allowed to edit. Independently verify the discussion, implement the task, and run validation. Do not access the network.\n\nTask:\n{self.task.prompt}\n\nComplete deliberation handoff:\n{full_handoff}\n\nBounded shared blackboard:\n{runtime.blackboard_context()}"""
            adapter = CodexWorkerAdapter(
                executable=self.codex_executable,
                enabled_features=self.codex_features,
                model=model,
                reasoning_effort=reasoning_effort,
                timeout_seconds=_effective_timeout(executor_max_seconds, governor.remaining_seconds),
                **self._containment("workspace-write"),
            )
            worker_events = await execute_worker_task(runtime, task.task_id, adapter, build_worker_prompt(task, final_instruction), worktree, task_schema, governor=governor, base_revision=base_revision, usage_model=model, session_max_seconds=executor_max_seconds)
            outcome["worker_events"] = len(worker_events)
            task_status = runtime.state.tasks[task.task_id].status
            outcome["task_status"] = task_status
            if task_status == TaskStatus.BLOCKED:
                raise WorkerHumanInputRequired("executor requested human input")
            validation = runtime.state.validation_results.get(task.task_id)
            if validation is None:
                raise WorkerProtocolError("executor produced no harness validation result")
            outcome["validation"] = validation.as_payload()
            runtime.transition_phase(MeetingPhase.REPORTING)
            active_stage = "scoring"
            score = _score(worktree, self.harness_root, self.task.scoring_command, self.task.score)
            outcome["score"] = score
            if not score.get("valid"):
                outcome["execution_status"] = "scorer_error"
            else:
                objective_success = _objective_success(float(score["value"]), self.task.score)
                outcome["objective_success"] = objective_success
                outcome["execution_status"] = "valid_submission" if validation.changed_files else "valid_noop"
            try:
                governor.assert_cost_within_limit()
            except BudgetExceeded:
                outcome.update(
                    execution_status="budget_timeout",
                    objective_success=False,
                    error="estimated cost exceeded the post-hoc run cap",
                    cost_limit_exceeded=True,
                )
                status = "failed"
                runtime.transition_phase(MeetingPhase.BUDGET_EXHAUSTED)
            else:
                status = "completed"
                runtime.transition_phase(MeetingPhase.DECIDING)
        except (WorkerTimeoutError, BudgetExceeded) as error:
            outcome.update(error=str(error), execution_status="budget_timeout")
            if runtime is not None and runtime.state.phase not in {MeetingPhase.BUDGET_EXHAUSTED, MeetingPhase.COMPLETED, MeetingPhase.FAILED}:
                runtime.transition_phase(MeetingPhase.BUDGET_EXHAUSTED)
        except WorkerInfrastructureError as error:
            outcome.update(
                error=str(error),
                execution_status="infra_error",
                infrastructure_failure=True,
                failure_stage=active_stage,
                failure_component="sandbox_launcher" if error.code in {"bwrap_userns_unavailable", "windows_helper_access_denied"} else "worker_runtime",
                failure_code=error.code,
            )
            if runtime is not None and runtime.state.phase not in {MeetingPhase.COMPLETED, MeetingPhase.FAILED}:
                runtime.transition_phase(MeetingPhase.FAILED)
        except WorkerValidationError as error:
            outcome.update(error=str(error), execution_status="validation_failure")
            if runtime is not None:
                validation = runtime.state.validation_results.get("implementation")
                if validation is not None:
                    outcome["validation"] = validation.as_payload()
            if runtime is not None and runtime.state.phase not in {MeetingPhase.COMPLETED, MeetingPhase.FAILED}:
                runtime.transition_phase(MeetingPhase.FAILED)
        except WorkerProtocolError as error:
            outcome.update(error=str(error), execution_status="protocol_failure")
            if runtime is not None and runtime.state.phase not in {MeetingPhase.COMPLETED, MeetingPhase.FAILED}:
                runtime.transition_phase(MeetingPhase.FAILED)
        except WorkerHumanInputRequired as error:
            outcome.update(error=str(error), execution_status="human_input_required")
            if runtime is not None and runtime.state.phase not in {MeetingPhase.COMPLETED, MeetingPhase.FAILED}:
                runtime.transition_phase(MeetingPhase.FAILED)
        except Exception as error:
            outcome.update(error=str(error), execution_status="infra_error", infrastructure_failure=True)
            if runtime is not None and runtime.state.phase not in {MeetingPhase.COMPLETED, MeetingPhase.FAILED}:
                runtime.transition_phase(MeetingPhase.FAILED)
        finally:
            if runtime is not None:
                task_state = runtime.state.tasks.get("implementation")
                if task_state is not None:
                    outcome["task_status"] = task_state.status
                runtime.close()
            manifest.complete(status, outcome, governor)
            manifest.write(manifest_path)
        return manifest_path


def stage_one_summary(manifests: list[Path], output: Path) -> None:
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in manifests]
    lines = ["# AgentCongress experiment comparison", "", "| model | condition | status | execution status | validation | score | direction | objective success | estimated cost | elapsed seconds |", "|---|---|---|---|---|---:|---|---|---:|---:|"]
    for row in rows:
        budget = row["budget"]
        outcome = row.get("outcome", {})
        score = outcome.get("score", {})
        lines.append(f"| {row.get('model', '-')} | {row['condition']} | {row['status']} | {outcome.get('execution_status', '-')} | {outcome.get('validation', {}).get('passed', False)} | {score.get('value', score.get('cycles', '-'))} | {score.get('direction', '-')} | {outcome.get('objective_success', False)} | {budget.get('estimated_api_equivalent_cost_usd', '-')} | {budget.get('elapsed_seconds', '-')} |")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
