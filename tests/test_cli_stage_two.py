from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from agentcongress.cli import main


SUITE = Path("examples/benchmarks/stage-two-suite.yaml")


def test_stage_two_plan_is_zero_model_and_fails_closed(
    monkeypatch, tmp_path: Path
) -> None:
    output = tmp_path / "pilot.json"
    monkeypatch.setattr(
        "agentcongress.cli.run_worker_sandbox_preflight",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("Stage 2 planning must not start a worker preflight")
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agentcongress",
            "stage-two-plan",
            str(SUITE),
            "--phase",
            "pilot",
            "--output",
            str(output),
        ],
    )

    assert main() == 2
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["ready"] is False
    assert len(plan["blocks"]) == 5
    assert all(len(block["arms"]) == 5 for block in plan["blocks"])
    assert "execution_backend_missing" in {
        blocker["code"] for blocker in plan["blockers"]
    }


def test_stage_two_plan_prints_confirmatory_plan(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agentcongress",
            "stage-two-plan",
            str(SUITE),
            "--phase",
            "confirmatory",
        ],
    )

    assert main() == 2
    plan = json.loads(capsys.readouterr().out)
    assert plan["phase"] == "confirmatory"
    assert len(plan["blocks"]) == 10


def test_stage_two_plan_verifies_environment_lock_before_building(
    monkeypatch, tmp_path: Path
) -> None:
    lock_path = tmp_path / "environment.lock.json"
    lock_path.write_text("{}", encoding="utf-8")
    marker = object()
    calls: dict[str, object] = {}

    def load_lock(path: Path, suite):
        calls["path"] = path
        calls["suite"] = suite
        return marker

    def build(suite, phase, environment_lock=None):
        calls["phase"] = phase
        calls["environment_lock"] = environment_lock
        return SimpleNamespace(ready=False, to_json=lambda: "{}\n")

    monkeypatch.setattr("agentcongress.cli.load_stage_two_environment_lock", load_lock)
    monkeypatch.setattr("agentcongress.cli.build_stage_two_plan", build)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agentcongress",
            "stage-two-plan",
            str(SUITE),
            "--environment-lock",
            str(lock_path),
        ],
    )

    assert main() == 2
    assert calls["path"] == lock_path
    assert calls["environment_lock"] is marker
    assert calls["suite"] is not None
