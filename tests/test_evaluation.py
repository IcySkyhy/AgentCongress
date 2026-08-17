import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentcongress.accounting import Budget
from agentcongress.adapters import WorkerEvent
from agentcongress.evaluation import BenchmarkTask, ExperimentRunner, ScoreSpec, _critic_report, _effective_timeout, _manifest_patch, _objective_success, _planner_memo, _score, five_arm_definitions, stage_one_summary
from agentcongress.events import SQLiteEventStore
from agentcongress.manifest import working_tree_sha256


def test_benchmark_task_config_is_frozen_and_summary_is_rendered(tmp_path: Path) -> None:
    config = tmp_path / "task.yaml"
    config.write_text("""task:
  id: demo
  title: Demo
  prompt: Fix it
  allowed_paths: [src/]
  validation_commands: [\"{python} -c \\\"pass\\\"\"]
  scoring_command: \"{python} -c \\\"pass\\\"\"
  score:
    direction: lower
    baseline: 10
""", encoding="utf-8")
    task = BenchmarkTask.load(config)
    assert task.allowed_paths == ("src/",)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"model": "gpt-5.6-luna", "condition": "single", "status": "completed", "budget": {"estimated_api_equivalent_cost_usd": 0.01, "elapsed_seconds": 2}, "outcome": {"validation": {"passed": True}, "score": {"returncode": 0}}}), encoding="utf-8")
    summary = tmp_path / "summary.md"
    stage_one_summary([manifest], summary)
    assert "gpt-5.6-luna" in summary.read_text(encoding="utf-8")


def test_working_tree_fingerprint_changes_for_uncommitted_source(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    source = tmp_path / "module.py"
    source.write_text("value = 1\n", encoding="utf-8")
    before = working_tree_sha256(tmp_path)
    source.write_text("value = 2\n", encoding="utf-8")
    assert before != working_tree_sha256(tmp_path)


def test_manifest_patch_archives_untracked_harness_sources(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    source = tmp_path / "src" / "agentcongress" / "new.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    patch = _manifest_patch(tmp_path)
    assert "src/agentcongress/new.py" in patch
    assert "@@ -0,0 +1,1 @@" in patch
    assert "+value = 1" in patch
    source.unlink()
    patch_file = tmp_path / "harness.patch"
    patch_file.write_text(patch, encoding="utf-8")
    applied = subprocess.run(["git", "-C", str(tmp_path), "apply", "--check", str(patch_file)], capture_output=True, text=True)
    assert applied.returncode == 0, applied.stderr


def _terminal_message(payload: dict) -> WorkerEvent:
    return WorkerEvent("codex.event", {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload)}})


def test_deliberation_reports_trust_only_terminal_agent_messages() -> None:
    memo = {"summary": "inspect cache", "hypotheses": ["stale key"], "validation_plan": ["run tests"], "risks": []}
    tool_echo = WorkerEvent("codex.event", {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": json.dumps(memo)}})
    assert _planner_memo([tool_echo]) is None
    assert _planner_memo([tool_echo, _terminal_message(memo)]) == memo

    critic = {"intent": "abstain", "reason": "memo is sufficient", "content": "private thought", "urgency": 0, "relevance": 0.8, "novelty": 0, "confidence": 0.9}
    assert _critic_report([_terminal_message(critic)]) == critic


def test_structured_scorer_contract_rejects_invalid_output(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    valid = tmp_path / "valid.py"
    valid.write_text('import json; print(json.dumps({"valid": True, "value": 8, "direction": "lower", "baseline": 10.0, "success_value": None, "thresholds_passed": 1, "seed_results": [8, 8, 8]}))\n', encoding="utf-8")
    result = _score(worktree, tmp_path, '"{python}" "' + str(valid) + '"', ScoreSpec("lower", 10.0))
    assert result["valid"] is True
    assert result["value"] == 8

    invalid = tmp_path / "invalid.py"
    invalid.write_text('print("CYCLES: 1")\n', encoding="utf-8")
    assert _score(worktree, tmp_path, '"{python}" "' + str(invalid) + '"', ScoreSpec("lower", 10.0))["valid"] is False


def test_objective_threshold_is_distinct_from_any_improvement() -> None:
    score = ScoreSpec("lower", 100, success_value=50)
    assert _objective_success(99, score) is False
    assert _objective_success(50, score) is True
    assert _effective_timeout(960, -0.1) > 0
    assert [arm[0] for arm in five_arm_definitions()] == list("ABCDE")


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def test_experiment_runner_keeps_legacy_backend_explicit(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    config = tmp_path / "task.yaml"
    config.write_text(
        """task:
  id: legacy-demo
  title: Legacy demo
  prompt: Inspect it.
  allowed_paths: [tracked.txt]
  validation_commands: [\"{python} -c \\\"pass\\\"\"]
  scoring_command: \"{python} -c \\\"pass\\\"\"
  score:
    direction: lower
    baseline: 1
""",
        encoding="utf-8",
    )
    runner = ExperimentRunner(
        config,
        repository,
        tmp_path / "runs",
        Path.cwd(),
        codex_features=("use_legacy_landlock",),
        backend_mode="legacy",
    )
    assert runner._containment("read-only") == {"sandbox": "read-only", "permission_profile": None}
    assert runner._containment("workspace-write") == {"sandbox": "workspace-write", "permission_profile": None}

    with pytest.raises(ValueError, match="requires backend_mode='legacy'"):
        ExperimentRunner(
            config,
            repository,
            tmp_path / "runs",
            Path.cwd(),
            codex_features=("use_legacy_landlock",),
        )


@pytest.mark.parametrize("strategy,critic_intent", [("self", None), ("congress", "interject"), ("congress", "abstain")])
def test_three_slot_protocol_runs_end_to_end_without_a_model(tmp_path: Path, monkeypatch, strategy: str, critic_intent: str | None) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    (repository / "target.txt").write_text("pending", encoding="utf-8")
    (repository / "score.py").write_text(
        'import json\nfrom pathlib import Path\nvalue = int(Path("target.txt").read_text() == "done")\nprint(json.dumps({"valid": True, "value": value, "direction": "higher", "baseline": 0.0, "success_value": 1.0, "thresholds_passed": value, "seed_results": [value]}))\n',
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    config = tmp_path / "task.yaml"
    config.write_text(
        """task:
  id: protocol-demo
  title: Protocol demo
  prompt: Change target.txt to done.
  allowed_paths: [target.txt]
  validation_commands:
    - '{python} -c "from pathlib import Path; assert Path(''target.txt'').read_text() == ''done''"'
  scoring_command: '"{python}" score.py'
  score:
    direction: higher
    baseline: 0
    success_value: 1
""",
        encoding="utf-8",
    )

    class FakeCodexWorkerAdapter:
        calls: list[dict[str, object]] = []

        def __init__(self, **kwargs):
            self.sandbox = kwargs["sandbox"]
            self.permission_profile = kwargs["permission_profile"]
            self.timeout_seconds = kwargs["timeout_seconds"]
            self.model = kwargs["model"]
            self.executable = kwargs["executable"]
            self.enabled_features = kwargs["enabled_features"]

        async def run_task(self, prompt: str, worktree: Path, report_schema: Path):
            initial_manifest = json.loads((tmp_path / "runs" / "fixed-run" / "manifest.json").read_text(encoding="utf-8"))
            self.calls.append({
                "sandbox": self.sandbox,
                "permission_profile": self.permission_profile,
                "timeout": self.timeout_seconds,
                "schema": report_schema.name,
                "prompt": prompt,
                "executable": self.executable,
                "enabled_features": self.enabled_features,
                "initial_backend": initial_manifest["codex_backend"],
            })
            if report_schema.name == "task-report.schema.json":
                (worktree / "target.txt").write_text("done", encoding="utf-8")
                payload = {"summary": "implemented", "changed_files": ["target.txt"], "validation": ["offline"], "risks": [], "commit": None, "needs_human_input": False}
            elif report_schema.name == "critic-report.schema.json":
                payload = {"intent": critic_intent, "reason": "independent check", "content": "PRIVATE CRITIQUE" if critic_intent == "abstain" else "fix the target", "urgency": 1, "relevance": 1, "novelty": 1, "confidence": 1}
            else:
                is_second_self_pass = strategy == "self" and sum(call["schema"] == "planner-report.schema.json" for call in self.calls) == 2
                payload = {"summary": "revised target plan" if is_second_self_pass else "inspect target", "hypotheses": ["target is pending"], "validation_plan": ["read it"], "risks": ["none"]}
            yield _terminal_message(payload)

    monkeypatch.setattr("agentcongress.evaluation.CodexWorkerAdapter", FakeCodexWorkerAdapter)
    features = ("example_feature",)
    runner = ExperimentRunner(config, repository, tmp_path / "runs", Path.cwd(), codex_executable=sys.executable, codex_features=features)
    manifest_path = __import__("asyncio").run(
        runner.run(strategy=strategy, model="gpt-5.6-luna", reasoning_effort="high", budget=Budget(3, 1200), pair_id="pair-1", run_id="fixed-run")
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["outcome"]["execution_status"] == "valid_submission"
    assert manifest["outcome"]["objective_success"] is True
    assert [call["sandbox"] for call in FakeCodexWorkerAdapter.calls] == [None, None, None]
    assert [call["permission_profile"] for call in FakeCodexWorkerAdapter.calls] == [":read-only", ":read-only", ":workspace"]
    assert len(FakeCodexWorkerAdapter.calls) == 3
    assert all(call["executable"] == sys.executable for call in FakeCodexWorkerAdapter.calls)
    assert all(call["enabled_features"] == features for call in FakeCodexWorkerAdapter.calls)
    expected_backend = {
        "executable_as_supplied": sys.executable,
        "executable_resolved": str(Path(sys.executable).resolve()),
        "version": manifest["outcome"]["host"]["codex_cli"],
        "enabled_features": list(features),
        "mode": "permission-profiles",
    }
    assert manifest["codex_backend"] == expected_backend
    assert manifest["outcome"]["protocol"]["codex_backend"] == expected_backend
    assert [slot["permission_profile"] for slot in manifest["outcome"]["protocol"]["slots"]] == [":read-only", ":read-only", ":workspace"]
    assert all(call["initial_backend"] == expected_backend for call in FakeCodexWorkerAdapter.calls)

    store = SQLiteEventStore(manifest_path.with_name("events.db"))
    events = store.replay(manifest["run_id"])
    store.close()
    types = [event.type for event in events]
    assert types.index("speech.segment_committed") < types.index("meeting.phase_changed") < types.index("task.status_changed", types.index("meeting.phase_changed"))
    if strategy == "self":
        assert not any(value.startswith("floor.") for value in types)
        assert "revised target plan" in str(FakeCodexWorkerAdapter.calls[-1]["prompt"])
    elif critic_intent == "interject":
        assert "floor.requested" in types and "floor.granted" in types and "brief.interjection_completed" in types
        assert "fix the target" in str(FakeCodexWorkerAdapter.calls[-1]["prompt"])
    else:
        assert "floor.retained" in types and "floor.requested" not in types
        assert "PRIVATE CRITIQUE" not in str(FakeCodexWorkerAdapter.calls[-1]["prompt"])
