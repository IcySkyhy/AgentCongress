from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

from .accounting import Budget, BudgetGovernor
from .adapters import CodexWorkerAdapter
from .config import DiscussionConfig, MeetingConfig, load_config
from .discussion import MeetingController, NoopObserver, run_dialogue_turn
from .events import SQLiteEventStore
from .evaluation import ExperimentRunner, five_arm_definitions, stage_one_summary
from .llm.agent import AgentLoop, DialogueAgentAdapter
from .llm.base import ChatMessage
from .llm.registry import PROTOCOLS, create_provider, provider_defaults
from .llm.tools import meeting_tools
from .listeners import ToolFloorObserver, floor_observer_loop
from .models import ApprovalDecision, MeetingPhase, Task, TaskReport, TaskStatus
from .prompts import build_worker_prompt
from .runtime import CongressRuntime
from .sandbox_preflight import (
    SandboxPreflightResult,
    WorkerSandboxPreflightResult,
    run_sandbox_preflight,
    run_worker_sandbox_preflight,
)
from .stage_two import (
    build_stage_two_plan,
    load_stage_two_environment_lock,
    load_stage_two_suite,
)
from .verification import verify_integration, verify_task
from .workspace import TaskWorktree, WorkspaceManager
from .workers import execute_worker_task


def _runtime_for_config(config_path: str, database: str | None) -> tuple[CongressRuntime, MeetingConfig]:
    config = load_config(Path(config_path))
    path = Path(database) if database else Path(".agentcongress") / "runs" / config.meeting_id / "events.db"
    return CongressRuntime.resume(config.meeting_id, path, config.roster), config


def _task_report_schema() -> Path:
    return Path(__file__).with_name("task-report.schema.json").resolve()


def _add_codex_backend_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--codex-executable", default="codex")
    parser.add_argument(
        "--codex-feature",
        dest="codex_features",
        action="append",
        default=[],
        help="Repeatable Codex sandbox feature flag.",
    )


_SPEAKER_SYSTEM_PROMPT = (
    "You are a participant in an auditable multi-agent coding meeting. The harness records "
    "your contribution as committed speech segments and persists every tool call you make as "
    "meeting evidence. Use tools to ground your claims in the meeting state; record only "
    "confirmed, defensible conclusions on the blackboard."
)


def _add_provider_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=list(PROTOCOLS), default=None, help="Provider protocol.")
    parser.add_argument("--model", help="Model name; defaults per protocol.")
    parser.add_argument("--api-key-env", help="Environment variable holding the API key.")
    parser.add_argument("--base-url", help="Optional base URL override for OpenAI-compatible endpoints.")
    parser.add_argument("--max-tool-rounds", type=int, default=None, help="Tool round budget per agent turn (default 8).")


def _provider_from_args(config: MeetingConfig, args: argparse.Namespace):
    discussion = config.discussion
    provider = args.provider or (discussion.provider if discussion else None) or "openai-chat"
    defaults = provider_defaults(provider)
    model = args.model or (discussion.model if discussion else None) or defaults["model"]
    api_key_env = args.api_key_env or (discussion.api_key_env if discussion else None) or defaults["api_key_env"]
    base_url = args.base_url or (discussion.base_url if discussion else None)
    return create_provider(provider, model=model, api_key_env=api_key_env, base_url=base_url)


def _workspace_root(config: MeetingConfig) -> Path | None:
    if config.workspace is None:
        return None
    return WorkspaceManager(config.workspace.repository, config.meeting_id, config.workspace.base_ref).root


def _speaker_loop(
    runtime: CongressRuntime,
    config: MeetingConfig,
    provider,
    max_tool_rounds: int | None,
) -> AgentLoop:
    discussion = config.discussion
    rounds = max_tool_rounds or (discussion.max_tool_rounds if discussion else None) or 8
    tools, executor = meeting_tools(runtime, workspace_root=_workspace_root(config))
    return AgentLoop(provider, tools, executor, system_prompt=_SPEAKER_SYSTEM_PROMPT, max_tool_rounds=rounds)


def _listener_mode(config: MeetingConfig, args: argparse.Namespace) -> str:
    discussion = config.discussion
    return args.listener_mode or (discussion.listener_mode if discussion else None) or "silent"


def _llm_observer(config: MeetingConfig, provider) -> ToolFloorObserver:
    return ToolFloorObserver(
        loops={
            agent.agent_id: floor_observer_loop(provider, listener_id=agent.agent_id, role=agent.role)
            for agent in config.agents
        }
    )


def _deepseek_observer(args: argparse.Namespace) -> ToolFloorObserver:
    """Legacy shortcut: a DeepSeek-backed listener evaluator (compatible branch)."""
    provider = create_provider(
        "deepseek",
        model=args.model,
        api_key_env=args.api_key_env or "DEEPSEEK_API_KEY",
        base_url=args.base_url,
    )
    return ToolFloorObserver(default_loop=floor_observer_loop(provider))


def _discussion_observer(config: MeetingConfig, args: argparse.Namespace, provider):
    mode = _listener_mode(config, args)
    if mode == "llm":
        return _llm_observer(config, provider)
    if mode == "deepseek":
        return _deepseek_observer(args)
    return None


def _sandbox_preflight_failure(
    result: SandboxPreflightResult | WorkerSandboxPreflightResult,
) -> dict[str, object]:
    payload = result.as_dict()
    return {
        "ready": False,
        "codex_executable": payload.get("codex_executable"),
        "codex_version": payload.get("codex_version"),
        "enabled_features": payload.get("enabled_features", []),
        "backend": payload.get("backend"),
        "diagnostic": payload.get("diagnostic"),
    }


def _manifest_execution_status(path: Path) -> str | None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    outcome = payload.get("outcome")
    return outcome.get("execution_status") if isinstance(outcome, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser(prog="agentcongress")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    init = subparsers.add_parser("init", help="Create an event-sourced meeting.")
    init.add_argument("--database", default=".agentcongress/runs/default/events.db")
    init.add_argument("--meeting-id", default="default")
    init.add_argument("--roster", nargs="+", required=True)
    export = subparsers.add_parser("export", help="Export a meeting event log as JSONL.")
    export.add_argument("meeting_id")
    export.add_argument("--database", default=".agentcongress/runs/default/events.db")
    export.add_argument("--output", required=True)
    status = subparsers.add_parser("status", help="Show the current meeting state.")
    status.add_argument("meeting_id")
    status.add_argument("--database", default=".agentcongress/runs/default/events.db")
    validate = subparsers.add_parser("validate", help="Validate a meeting configuration.")
    validate.add_argument("config")
    run = subparsers.add_parser("run", help="Start a configured meeting.")
    run.add_argument("config")
    run.add_argument("--database")
    sandbox_preflight = subparsers.add_parser(
        "sandbox-preflight", help="Verify the model-free Codex worker sandbox."
    )
    sandbox_preflight.add_argument("--codex-executable", default="codex")
    sandbox_preflight.add_argument(
        "--codex-feature",
        "--enable-feature",
        dest="codex_features",
        action="append",
        default=[],
        help="Repeatable Codex feature flag; only use_legacy_landlock is supported for diagnostic preflight.",
    )
    sandbox_preflight.add_argument("--timeout-seconds", type=float, default=20.0)
    sandbox_preflight.add_argument(
        "--network-timeout-seconds", type=float, default=1.0
    )
    sandbox_preflight.add_argument(
        "--all-worker-profiles",
        action="store_true",
        help="Probe both :read-only and :workspace, as required by the three-slot worker protocol.",
    )
    api_check = subparsers.add_parser("api-check", help="Probe one discussion provider protocol.")
    api_check.add_argument("--provider", choices=list(PROTOCOLS), default="openai-chat")
    api_check.add_argument("--model")
    api_check.add_argument("--api-key-env")
    api_check.add_argument("--base-url")
    api_check.add_argument("--prompt", default="Reply with exactly: READY")
    talk = subparsers.add_parser("talk", help="Record one agent-loop-backed meeting turn.")
    talk.add_argument("config")
    talk.add_argument("--prompt", required=True)
    talk.add_argument("--database")
    _add_provider_arguments(talk)
    talk.add_argument("--listener-mode", choices=["silent", "llm", "deepseek"], default=None)
    meeting = subparsers.add_parser("meeting-run", help="Run a bounded autonomous meeting.")
    meeting.add_argument("config")
    meeting.add_argument("--prompt", required=True)
    meeting.add_argument("--turns", type=int, default=3)
    meeting.add_argument("--database")
    _add_provider_arguments(meeting)
    meeting.add_argument("--listener-mode", choices=["silent", "llm", "deepseek"], default=None)
    blackboard = subparsers.add_parser("blackboard-add", help="Add confirmed shared context.")
    blackboard.add_argument("config")
    blackboard.add_argument("kind")
    blackboard.add_argument("content")
    blackboard.add_argument("--actor", default="operator")
    blackboard.add_argument("--task-id")
    blackboard.add_argument("--evidence", action="append")
    blackboard.add_argument("--database")
    phase = subparsers.add_parser("phase")
    phase.add_argument("config")
    phase.add_argument("value", choices=[phase.value for phase in MeetingPhase])
    phase.add_argument("--database")
    for decision in ("approve", "reject"):
        command = subparsers.add_parser(decision)
        command.add_argument("config")
        command.add_argument("task_id")
        command.add_argument("--database")
        command.add_argument("--operator", default="operator")
    task_create = subparsers.add_parser("task-create", help="Create a meeting task.")
    task_create.add_argument("config")
    task_create.add_argument("task_id")
    task_create.add_argument("assignee")
    task_create.add_argument("title")
    task_create.add_argument("--criterion", action="append", required=True)
    task_create.add_argument("--allow-path", action="append", default=[])
    task_create.add_argument("--validate", action="append", default=[])
    task_create.add_argument("--database")
    task_prepare = subparsers.add_parser("task-prepare")
    task_prepare.add_argument("config")
    task_prepare.add_argument("task_id")
    task_prepare.add_argument("--database")
    task_retry = subparsers.add_parser("task-retry")
    task_retry.add_argument("config")
    task_retry.add_argument("task_id")
    task_retry.add_argument("--database")
    task_ready = subparsers.add_parser("task-ready")
    task_ready.add_argument("config")
    task_ready.add_argument("task_id")
    task_ready.add_argument("--database")
    task_report = subparsers.add_parser("task-report")
    task_report.add_argument("config")
    task_report.add_argument("task_id")
    task_report.add_argument("--file", required=True)
    task_report.add_argument("--database")
    task_execute = subparsers.add_parser("task-execute", help="Execute one prepared task in its worktree.")
    task_execute.add_argument("config")
    task_execute.add_argument("task_id")
    task_execute.add_argument("--database")
    task_execute.add_argument("--model")
    task_execute.add_argument("--reasoning-effort", choices=["none", "low", "medium", "high", "xhigh", "max"])
    task_execute.add_argument("--instruction", default="")
    task_execute.add_argument("--max-wall-seconds", type=float, default=900.0)
    task_execute.add_argument("--max-estimated-cost-usd", type=float)
    _add_codex_backend_arguments(task_execute)
    task_request = subparsers.add_parser("task-request-approval")
    task_request.add_argument("config")
    task_request.add_argument("task_id")
    task_request.add_argument("--database")
    task_integrate = subparsers.add_parser("task-integrate")
    task_integrate.add_argument("config")
    task_integrate.add_argument("task_id")
    task_integrate.add_argument("--database")
    task_promote = subparsers.add_parser("task-promote", help="Promote verified integrated work.")
    task_promote.add_argument("config")
    task_promote.add_argument("--database")
    experiment = subparsers.add_parser("experiment-run")
    experiment.add_argument("task_config")
    experiment.add_argument("--repository", required=True)
    experiment.add_argument("--strategy", choices=["self", "congress"], required=True)
    experiment.add_argument("--model", required=True)
    experiment.add_argument("--planner-model", help="Model for both read-only deliberation slots; the executor uses --model.")
    experiment.add_argument("--deliberation-max-seconds", type=float, default=180.0, help="Fixed cap for each of the two read-only deliberation slots.")
    experiment.add_argument("--executor-max-seconds", type=float, default=840.0, help="Fixed cap for the only workspace-write slot.")
    experiment.add_argument("--reasoning-effort", choices=["none", "low", "medium", "high", "xhigh", "max"], default="high")
    experiment.add_argument("--max-worker-sessions", type=int, default=3)
    experiment.add_argument("--max-wall-seconds", type=float, default=1200.0)
    experiment.add_argument("--max-estimated-cost-usd", type=float)
    experiment.add_argument("--runs-root", default=".agentcongress/experiments")
    experiment.add_argument("--run-id")
    experiment.add_argument("--pair-id", help="Frozen replicate/block identifier used for paired analysis.")
    _add_codex_backend_arguments(experiment)
    stage_one = subparsers.add_parser("experiment-stage-one")
    stage_one.add_argument("task_config")
    stage_one.add_argument("--repository", required=True)
    stage_one.add_argument("--models", nargs="+", default=["gpt-5.6-luna", "gpt-5.6-sol"])
    stage_one.add_argument("--reasoning-effort", choices=["none", "low", "medium", "high", "xhigh", "max"], default="high")
    stage_one.add_argument("--max-worker-sessions", type=int, default=3)
    stage_one.add_argument("--max-wall-seconds", type=float, default=1200.0)
    stage_one.add_argument("--max-estimated-cost-usd", type=float)
    stage_one.add_argument("--runs-root", default=".agentcongress/experiments")
    stage_one.add_argument("--summary")
    stage_one.add_argument("--pair-id", help="Frozen replicate/block identifier shared across generated arms.")
    _add_codex_backend_arguments(stage_one)
    analyze = subparsers.add_parser("experiment-analyze")
    analyze.add_argument("manifests", nargs="+")
    analyze.add_argument("--baseline-condition", required=True)
    analyze.add_argument("--comparison-condition", required=True)
    suite = subparsers.add_parser("experiment-five-arm")
    suite.add_argument("task_config")
    suite.add_argument("--repository", required=True)
    suite.add_argument("--reasoning-effort", choices=["none", "low", "medium", "high", "xhigh", "max"], default="high")
    suite.add_argument("--max-wall-seconds", type=float, default=1200.0)
    suite.add_argument("--max-estimated-cost-usd", type=float)
    suite.add_argument("--runs-root", default=".agentcongress/experiments")
    suite.add_argument("--pair-id", required=True)
    suite.add_argument("--randomization-seed", type=int, required=True)
    _add_codex_backend_arguments(suite)
    stage_two_plan = subparsers.add_parser(
        "stage-two-plan",
    )
    stage_two_plan.add_argument("suite")
    stage_two_plan.add_argument(
        "--phase", choices=["pilot", "confirmatory"], default="pilot"
    )
    stage_two_plan.add_argument(
        "--environment-lock",
        help="Measured Stage 2 environment lock; every referenced evidence file is rehashed.",
    )
    stage_two_plan.add_argument("--output")
    args = parser.parse_args()
    if args.command == "sandbox-preflight":
        probe = (
            run_worker_sandbox_preflight
            if args.all_worker_profiles
            else run_sandbox_preflight
        )
        result = probe(
            codex_executable=args.codex_executable,
            enabled_features=args.codex_features,
            timeout_seconds=args.timeout_seconds,
            network_timeout_seconds=args.network_timeout_seconds,
        )
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
        return 0 if result.ready else 2
    if args.command == "stage-two-plan":
        frozen_suite = load_stage_two_suite(Path(args.suite))
        environment_lock = (
            load_stage_two_environment_lock(
                Path(args.environment_lock), frozen_suite
            )
            if args.environment_lock
            else None
        )
        plan = build_stage_two_plan(
            frozen_suite, args.phase, environment_lock
        )
        rendered = plan.to_json()
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 0 if plan.ready else 2
    if args.command in {
        "task-execute",
        "experiment-run",
        "experiment-stage-one",
        "experiment-five-arm",
    }:
        preflight = run_worker_sandbox_preflight(
            codex_executable=args.codex_executable,
            enabled_features=args.codex_features,
        )
        if not preflight.ready:
            print(
                json.dumps(
                    _sandbox_preflight_failure(preflight),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                file=sys.stderr,
            )
            return 2
    if args.command == "init":
        runtime = CongressRuntime(args.meeting_id, Path(args.database), args.roster)
        runtime.close()
        print(f"initialized meeting {args.meeting_id}")
    elif args.command == "export":
        store = SQLiteEventStore(Path(args.database))
        count = store.export_jsonl(args.meeting_id, Path(args.output))
        store.close()
        print(f"exported {count} events")
    elif args.command == "status":
        store = SQLiteEventStore(Path(args.database))
        events = store.replay(args.meeting_id)
        store.close()
        print(f"meeting={args.meeting_id} events={len(events)} last={events[-1].type if events else 'none'}")
    elif args.command == "validate":
        config = load_config(Path(args.config))
        print(f"valid meeting={config.meeting_id} agents={len(config.agents)} mode={config.execution_mode}")
    elif args.command == "run":
        config = load_config(Path(args.config))
        database = Path(args.database) if args.database else Path(".agentcongress") / "runs" / config.meeting_id / "events.db"
        runtime = CongressRuntime.resume(config.meeting_id, database, config.roster)
        if runtime.state.phase == MeetingPhase.PREPARING:
            runtime.start(config.initial_speaker, config.initial_addressee)
            message = "started"
        else:
            message = "resumed"
        runtime.close()
        print(f"{message} meeting {config.meeting_id}")
    elif args.command == "api-check":
        defaults = provider_defaults(args.provider)
        provider = create_provider(
            args.provider,
            model=args.model or defaults["model"],
            api_key_env=args.api_key_env or defaults["api_key_env"],
            base_url=args.base_url,
        )
        response = asyncio.run(provider.complete([ChatMessage("user", args.prompt)]))
        print(f"provider={provider.name} model={provider.model} response={response.content}")
    elif args.command == "talk":
        runtime, config = _runtime_for_config(args.config, args.database)
        provider = _provider_from_args(config, args)
        loop = _speaker_loop(runtime, config, provider, args.max_tool_rounds)
        observer = _discussion_observer(config, args, provider)
        try:
            winner = asyncio.run(run_dialogue_turn(runtime, config, DialogueAgentAdapter(loop), args.prompt, observer))
        finally:
            runtime.close()
        print(f"recorded discussion turn; floor={'retained' if winner is None else winner.agent_id}")
    elif args.command == "meeting-run":
        runtime, config = _runtime_for_config(args.config, args.database)
        provider = _provider_from_args(config, args)
        loop = _speaker_loop(runtime, config, provider, args.max_tool_rounds)
        adapter = DialogueAgentAdapter(loop)
        observer = _discussion_observer(config, args, provider)
        try:
            controller = MeetingController(runtime, config, {agent.agent_id: adapter for agent in config.agents}, observer or NoopObserver())
            turns = asyncio.run(controller.run(args.prompt, max_turns=args.turns))
        finally:
            runtime.close()
        print(f"recorded {turns} discussion turns")
    elif args.command == "blackboard-add":
        runtime, _ = _runtime_for_config(args.config, args.database)
        runtime.add_blackboard(args.kind, args.content, args.actor, task_id=args.task_id, evidence=args.evidence)
        runtime.close()
        print("updated shared blackboard")
    elif args.command == "phase":
        runtime, _ = _runtime_for_config(args.config, args.database)
        runtime.transition_phase(MeetingPhase(args.value), "operator")
        runtime.close()
        print(f"meeting phase is now {args.value}")
    elif args.command in {"approve", "reject"}:
        runtime, config = _runtime_for_config(args.config, args.database)
        runtime.decide_merge_approval(args.task_id, args.command == "approve", args.operator)
        runtime.close()
        print(f"{args.command}d merge for {args.task_id}")
    elif args.command == "task-create":
        runtime, _ = _runtime_for_config(args.config, args.database)
        runtime.propose_task(Task(args.task_id, args.title, args.assignee, args.criterion, args.allow_path, args.validate))
        runtime.transition_task(args.task_id, TaskStatus.ASSIGNED, args.assignee)
        runtime.close()
        print(f"created task {args.task_id}")
    elif args.command == "task-prepare":
        runtime, config = _runtime_for_config(args.config, args.database)
        try:
            if config.workspace is None:
                raise ValueError("task preparation requires meeting.workspace")
            task = runtime.state.tasks.get(args.task_id)
            if task is None:
                raise ValueError(f"unknown task: {args.task_id}")
            persisted = runtime.state.task_worktrees.get(args.task_id)
            if persisted is None and task.status not in {TaskStatus.ASSIGNED, TaskStatus.ACCEPTED}:
                raise ValueError("unrecorded task worktree cannot be recovered from this task status")
            manager = WorkspaceManager(config.workspace.repository, config.meeting_id, config.workspace.base_ref)
            manager.ensure_clean_base()
            manager.get_or_create_integration()
            expected_branch = f"agentcongress/{config.meeting_id}/{args.task_id}"
            expected_path = manager.root / args.task_id
            if persisted is not None and (
                persisted["branch"] != expected_branch
                or Path(persisted["path"]).resolve() != expected_path.resolve()
            ):
                raise ValueError("persisted task worktree identity does not match the meeting workspace")
            worktree = manager.get_or_create_task(
                args.task_id,
                expected_base_revision=(persisted or {}).get("base_revision"),
            )
            if worktree.base_revision is None:
                raise ValueError("task worktree did not provide a base revision")
            if task.status == TaskStatus.ASSIGNED:
                runtime.transition_task(args.task_id, TaskStatus.ACCEPTED)
            if persisted is None:
                runtime.record_task_worktree(
                    args.task_id,
                    worktree.branch,
                    str(worktree.path),
                    worktree.base_revision,
                )
        finally:
            runtime.close()
        print(f"prepared task {args.task_id} at {worktree.path}")
    elif args.command == "task-retry":
        runtime, config = _runtime_for_config(args.config, args.database)
        try:
            task = runtime.state.tasks.get(args.task_id)
            if task is None:
                raise ValueError(f"unknown task: {args.task_id}")
            if task.status not in {TaskStatus.BLOCKED, TaskStatus.FAILED}:
                raise ValueError("only blocked or failed tasks may be retried")
            if args.task_id not in runtime.state.task_base_revisions:
                raise ValueError("task retry requires its existing prepared worktree base")
            if config.workspace is None:
                raise ValueError("task retry requires meeting.workspace")
            worktree = WorkspaceManager(config.workspace.repository, config.meeting_id, config.workspace.base_ref).root / args.task_id
            if not worktree.is_dir():
                raise ValueError("task retry requires its existing prepared worktree")
            runtime.transition_task(args.task_id, TaskStatus.ACCEPTED, "operator")
        finally:
            runtime.close()
        print(f"task {args.task_id} is accepted for retry")
    elif args.command == "task-ready":
        runtime, _ = _runtime_for_config(args.config, args.database)
        runtime.mark_task_ready(args.task_id)
        runtime.close()
        print(f"task {args.task_id} is ready for report")
    elif args.command == "task-report":
        runtime, config = _runtime_for_config(args.config, args.database)
        if config.workspace is None:
            raise ValueError("task report requires meeting.workspace")
        task = runtime.state.tasks.get(args.task_id)
        if task is None:
            raise ValueError(f"unknown task: {args.task_id}")
        worktree = WorkspaceManager(config.workspace.repository, config.meeting_id, config.workspace.base_ref).root / args.task_id
        if not worktree.is_dir():
            raise ValueError("task worktree has not been prepared")
        if task.status == TaskStatus.ACCEPTED:
            runtime.transition_task(args.task_id, TaskStatus.RUNNING, task.assignee_id)
        report = TaskReport.from_payload(json.loads(Path(args.file).read_text(encoding="utf-8")))
        runtime.submit_task_report(args.task_id, report, task.assignee_id)
        if report.needs_human_input:
            runtime.close()
            print(f"task {args.task_id} is blocked pending human input")
            return 0
        base_revision = runtime.state.task_base_revisions.get(args.task_id)
        if base_revision is None:
            raise ValueError("task worktree base revision is unavailable")
        runtime.record_validation(args.task_id, verify_task(worktree, task, report, base_revision))
        runtime.mark_task_ready(args.task_id, task.assignee_id)
        runtime.close()
        print(f"recorded and validated report for {args.task_id}")
    elif args.command == "task-execute":
        runtime, config = _runtime_for_config(args.config, args.database)
        if config.workspace is None:
            raise ValueError("task execution requires meeting.workspace")
        task = runtime.state.tasks.get(args.task_id)
        if task is None:
            raise ValueError(f"unknown task: {args.task_id}")
        if task.status != TaskStatus.ACCEPTED:
            raise ValueError("task execution requires an accepted task")
        manager = WorkspaceManager(config.workspace.repository, config.meeting_id, config.workspace.base_ref)
        worktree = manager.root / args.task_id
        if not worktree.is_dir():
            raise ValueError("task worktree has not been prepared")
        model = args.model or "codex-default"
        governor = BudgetGovernor(Budget(1, args.max_wall_seconds, args.max_estimated_cost_usd), model)
        adapter = CodexWorkerAdapter(
            executable=args.codex_executable,
            enabled_features=tuple(args.codex_features),
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            sandbox=None,
            permission_profile=":workspace",
            timeout_seconds=args.max_wall_seconds,
        )
        try:
            events = asyncio.run(execute_worker_task(runtime, args.task_id, adapter, build_worker_prompt(task, args.instruction), worktree, _task_report_schema(), governor=governor, base_revision=runtime.state.task_base_revisions.get(args.task_id), usage_model=args.model))
        finally:
            runtime.close()
        print(f"executed task {args.task_id}; recorded {len(events)} worker events")
    elif args.command == "task-request-approval":
        runtime, _ = _runtime_for_config(args.config, args.database)
        runtime.request_merge_approval(args.task_id)
        runtime.close()
        print(f"requested merge approval for {args.task_id}")
    elif args.command == "task-integrate":
        runtime, config = _runtime_for_config(args.config, args.database)
        try:
            if config.workspace is None:
                raise ValueError("task integration requires meeting.workspace")
            if config.workspace.merge_policy == "manual" and runtime.state.approvals.get(args.task_id) != ApprovalDecision.APPROVED:
                raise ValueError("manual merge policy requires approved task")
            manager = WorkspaceManager(config.workspace.repository, config.meeting_id, config.workspace.base_ref)
            integration = manager.get_or_create_integration()
            task_path = manager.root / args.task_id
            if not task_path.exists():
                raise ValueError("task worktree has not been prepared")
            task_model = runtime.state.tasks.get(args.task_id)
            report = runtime.state.task_reports.get(args.task_id)
            base_revision = runtime.state.task_base_revisions.get(args.task_id)
            if task_model is None or report is None or base_revision is None:
                raise ValueError("task must have a prepared worktree and structured report")
            runtime.record_validation(args.task_id, verify_task(task_path, task_model, report, base_revision))
            runtime.assert_task_verified(args.task_id)
            if task_model.status not in {TaskStatus.READY_FOR_REPORT, TaskStatus.IN_REVIEW}:
                raise ValueError("only a validated ready task may be integrated")
            task = TaskWorktree(args.task_id, f"agentcongress/{config.meeting_id}/{args.task_id}", task_path, base_revision)
            validation = runtime.state.validation_results[args.task_id]
            if config.workspace.merge_policy == "manual" and runtime.state.approval_git_identities.get(args.task_id) != validation.git_identity:
                raise ValueError("approved Git identity no longer matches the validated task")
            assert validation.git_identity is not None
            merge_commit = manager.integrate(task, integration, validation.git_identity)
            runtime.record_task_integration(args.task_id, merge_commit)
        finally:
            runtime.close()
        print(f"integrated task {args.task_id} at {merge_commit}")
    elif args.command == "task-promote":
        runtime, config = _runtime_for_config(args.config, args.database)
        try:
            if config.workspace is None:
                raise ValueError("task promotion requires meeting.workspace")
            manager = WorkspaceManager(config.workspace.repository, config.meeting_id, config.workspace.base_ref)
            integration = manager.get_or_create_integration()
            integration_result = verify_integration(integration.path, runtime.integrated_validation_commands())
            runtime.record_integration_validation(integration_result)
            runtime.assert_integration_verified()
            assert integration_result.git_identity is not None
            manager.promote(integration, integration_result.git_identity)
            runtime.record("workspace.promoted", "operator")
            for task_id, task in list(runtime.state.tasks.items()):
                if task.status == TaskStatus.INTEGRATED:
                    runtime.transition_task(task_id, TaskStatus.MERGED, "operator")
            if runtime.state.phase not in {MeetingPhase.COMPLETED, MeetingPhase.FAILED}:
                runtime.transition_phase(MeetingPhase.COMPLETED, "operator")
        finally:
            runtime.close()
        print(f"promoted meeting {config.meeting_id}")
    elif args.command == "experiment-run":
        runner = ExperimentRunner(
            Path(args.task_config),
            Path(args.repository),
            Path(args.runs_root),
            Path.cwd(),
            codex_executable=args.codex_executable,
            codex_features=tuple(args.codex_features),
        )
        manifest = asyncio.run(
            runner.run(
                strategy=args.strategy,
                model=args.model,
                planner_model=args.planner_model,
                reasoning_effort=args.reasoning_effort,
                budget=Budget(args.max_worker_sessions, args.max_wall_seconds, args.max_estimated_cost_usd),
                deliberation_max_seconds=args.deliberation_max_seconds,
                executor_max_seconds=args.executor_max_seconds,
                run_id=args.run_id,
                pair_id=args.pair_id,
            )
        )
        print(f"experiment manifest={manifest}")
    elif args.command == "experiment-stage-one":
        manifests: list[Path] = []
        for model in args.models:
            for strategy in ("self", "congress"):
                runner = ExperimentRunner(
                    Path(args.task_config),
                    Path(args.repository),
                    Path(args.runs_root),
                    Path.cwd(),
                    codex_executable=args.codex_executable,
                    codex_features=tuple(args.codex_features),
                )
                manifests.append(
                    asyncio.run(
                        runner.run(
                            strategy=strategy,
                            model=model,
                            reasoning_effort=args.reasoning_effort,
                            budget=Budget(args.max_worker_sessions, args.max_wall_seconds, args.max_estimated_cost_usd),
                            pair_id=args.pair_id,
                        )
                    )
                )
        summary = Path(args.summary) if args.summary else Path(args.runs_root) / "stage-one-summary.md"
        stage_one_summary(manifests, summary)
        print(f"completed stage one; summary={summary}")
    elif args.command == "experiment-analyze":
        from .analysis import analyze_manifests

        result = analyze_manifests(
            [Path(path) for path in args.manifests],
            baseline_condition=args.baseline_condition,
            comparison_condition=args.comparison_condition,
        )
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    elif args.command == "experiment-five-arm":
        arms = list(five_arm_definitions())
        random.Random(args.randomization_seed).shuffle(arms)
        realized_order = [arm[0] for arm in arms]
        manifests: list[Path] = []
        for arm_id, strategy, planner_model, executor_model in arms:
            runner = ExperimentRunner(
                Path(args.task_config),
                Path(args.repository),
                Path(args.runs_root),
                Path.cwd(),
                codex_executable=args.codex_executable,
                codex_features=tuple(args.codex_features),
            )
            manifests.append(
                asyncio.run(
                    runner.run(
                        strategy=strategy,
                        model=executor_model,
                        planner_model=planner_model,
                        reasoning_effort=args.reasoning_effort,
                        budget=Budget(3, args.max_wall_seconds, args.max_estimated_cost_usd),
                        pair_id=args.pair_id,
                        run_id=f"{runner.task.task_id}-{args.pair_id}-{arm_id.lower()}",
                        randomization_seed=args.randomization_seed,
                        realized_order=realized_order,
                        arm_id=arm_id,
                    )
                )
            )
        summary = Path(args.runs_root) / f"{args.pair_id}-five-arm-summary.md"
        stage_one_summary(manifests, summary)
        invalidating = {
            "infra_error",
            "scorer_error",
        }
        invalid_arms = [
            arm_id
            for arm_id, manifest in zip(realized_order, manifests, strict=True)
            if _manifest_execution_status(manifest) in invalidating
        ]
        block_path = Path(args.runs_root) / f"{args.pair_id}-five-arm-block.json"
        block = {
            "schema_version": 1,
            "pair_id": args.pair_id,
            "randomization_seed": args.randomization_seed,
            "realized_order": realized_order,
            "manifests": [str(path.resolve()) for path in manifests],
            "status": "invalid" if invalid_arms else "completed",
            "invalid_arms": invalid_arms,
        }
        block_path.parent.mkdir(parents=True, exist_ok=True)
        block_path.write_text(
            json.dumps(block, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if invalid_arms:
            print(
                f"invalid randomized A-E block; rerun all arms; invalid={','.join(invalid_arms)} block={block_path}",
                file=sys.stderr,
            )
            return 2
        print(f"completed randomized A-E block; order={','.join(realized_order)} summary={summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
