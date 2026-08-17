from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from agentcongress.cli import main


class _FailedPreflight:
    ready = False

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": False,
            "codex_executable": "/opt/codex",
            "codex_version": "codex-cli test",
            "enabled_features": ["use_legacy_landlock"],
            "backend": "linux-legacy-landlock",
            "diagnostic": {
                "code": "sandbox_policy_mismatch",
                "message": "mocked",
            },
        }


class _ReadyPreflight:
    ready = True


def test_experiment_preflight_failure_does_not_construct_runner(
    monkeypatch, capsys
) -> None:
    constructed = 0

    class ForbiddenRunner:
        def __init__(self, *args, **kwargs) -> None:
            nonlocal constructed
            constructed += 1

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "agentcongress.cli.run_worker_sandbox_preflight",
        lambda **kwargs: calls.append(kwargs) or _FailedPreflight(),
    )
    monkeypatch.setattr("agentcongress.cli.ExperimentRunner", ForbiddenRunner)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agentcongress",
            "experiment-run",
            "task.yaml",
            "--repository",
            "target",
            "--strategy",
            "self",
            "--model",
            "gpt-test",
            "--codex-executable",
            "/opt/codex",
            "--codex-feature",
            "use_legacy_landlock",
        ],
    )

    assert main() == 2
    assert constructed == 0
    assert calls == [
        {
            "codex_executable": "/opt/codex",
            "enabled_features": ["use_legacy_landlock"],
        }
    ]
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload == {
        "ready": False,
        "codex_executable": "/opt/codex",
        "codex_version": "codex-cli test",
        "enabled_features": ["use_legacy_landlock"],
        "backend": "linux-legacy-landlock",
        "diagnostic": {
            "code": "sandbox_policy_mismatch",
            "message": "mocked",
        },
    }


def test_experiment_passes_frozen_codex_backend_to_runner(monkeypatch) -> None:
    preflight_calls: list[dict[str, object]] = []
    constructor_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeRunner:
        def __init__(self, *args, **kwargs) -> None:
            constructor_calls.append((args, kwargs))

        async def run(self, **kwargs) -> Path:
            return Path("manifest.json")

    monkeypatch.setattr(
        "agentcongress.cli.run_worker_sandbox_preflight",
        lambda **kwargs: preflight_calls.append(kwargs) or _ReadyPreflight(),
    )
    monkeypatch.setattr("agentcongress.cli.ExperimentRunner", FakeRunner)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agentcongress",
            "experiment-run",
            "task.yaml",
            "--repository",
            "target",
            "--strategy",
            "congress",
            "--model",
            "gpt-test",
            "--codex-executable",
            "/opt/codex",
            "--codex-feature",
            "alpha",
            "--codex-feature",
            "beta",
        ],
    )

    assert main() == 0
    assert preflight_calls == [
        {"codex_executable": "/opt/codex", "enabled_features": ["alpha", "beta"]}
    ]
    assert len(constructor_calls) == 1
    assert constructor_calls[0][1] == {
        "codex_executable": "/opt/codex",
        "codex_features": ("alpha", "beta"),
    }


def test_five_arm_block_preflights_once_and_reuses_backend(
    monkeypatch, tmp_path: Path
) -> None:
    preflight_calls: list[dict[str, object]] = []
    constructor_kwargs: list[dict[str, object]] = []

    class FakeRunner:
        def __init__(self, *args, **kwargs) -> None:
            constructor_kwargs.append(kwargs)
            self.task = SimpleNamespace(task_id="frozen-task")

        async def run(self, **kwargs) -> Path:
            path = tmp_path / f"{kwargs['arm_id']}.json"
            path.write_text(
                json.dumps(
                    {"outcome": {"execution_status": "valid_submission"}}
                ),
                encoding="utf-8",
            )
            return path

    monkeypatch.setattr(
        "agentcongress.cli.run_worker_sandbox_preflight",
        lambda **kwargs: preflight_calls.append(kwargs) or _ReadyPreflight(),
    )
    monkeypatch.setattr("agentcongress.cli.ExperimentRunner", FakeRunner)
    monkeypatch.setattr("agentcongress.cli.stage_one_summary", lambda *args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agentcongress",
            "experiment-five-arm",
            "task.yaml",
            "--repository",
            "target",
            "--pair-id",
            "replicate-1",
            "--randomization-seed",
            "17",
            "--codex-executable",
            "/opt/codex",
            "--codex-feature",
            "use_legacy_landlock",
            "--runs-root",
            str(tmp_path),
        ],
    )

    assert main() == 0
    assert preflight_calls == [
        {
            "codex_executable": "/opt/codex",
            "enabled_features": ["use_legacy_landlock"],
        }
    ]
    assert len(constructor_kwargs) == 5
    assert all(
        kwargs
        == {
            "codex_executable": "/opt/codex",
            "codex_features": ("use_legacy_landlock",),
        }
        for kwargs in constructor_kwargs
    )
    block = json.loads(
        (tmp_path / "replicate-1-five-arm-block.json").read_text(encoding="utf-8")
    )
    assert block["status"] == "completed"
    assert block["invalid_arms"] == []


def test_five_arm_infrastructure_error_invalidates_whole_block(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    class FakeRunner:
        def __init__(self, *args, **kwargs) -> None:
            self.task = SimpleNamespace(task_id="frozen-task")

        async def run(self, **kwargs) -> Path:
            arm_id = kwargs["arm_id"]
            status = "infra_error" if arm_id == "C" else "valid_submission"
            path = tmp_path / f"{arm_id}.json"
            path.write_text(
                json.dumps({"outcome": {"execution_status": status}}),
                encoding="utf-8",
            )
            return path

    monkeypatch.setattr(
        "agentcongress.cli.run_worker_sandbox_preflight",
        lambda **kwargs: _ReadyPreflight(),
    )
    monkeypatch.setattr("agentcongress.cli.ExperimentRunner", FakeRunner)
    monkeypatch.setattr("agentcongress.cli.stage_one_summary", lambda *args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agentcongress",
            "experiment-five-arm",
            "task.yaml",
            "--repository",
            "target",
            "--pair-id",
            "replicate-infra",
            "--randomization-seed",
            "23",
            "--runs-root",
            str(tmp_path),
        ],
    )

    assert main() == 2
    block = json.loads(
        (tmp_path / "replicate-infra-five-arm-block.json").read_text(
            encoding="utf-8"
        )
    )
    assert block["status"] == "invalid"
    assert block["invalid_arms"] == ["C"]
    assert "rerun all arms" in capsys.readouterr().err


def test_five_arm_protocol_failure_is_a_completed_outcome(
    monkeypatch, tmp_path: Path
) -> None:
    class FakeRunner:
        def __init__(self, *args, **kwargs) -> None:
            self.task = SimpleNamespace(task_id="frozen-task")

        async def run(self, **kwargs) -> Path:
            arm_id = kwargs["arm_id"]
            status = "protocol_failure" if arm_id == "B" else "valid_submission"
            path = tmp_path / f"{arm_id}.json"
            path.write_text(
                json.dumps({"outcome": {"execution_status": status}}),
                encoding="utf-8",
            )
            return path

    monkeypatch.setattr(
        "agentcongress.cli.run_worker_sandbox_preflight",
        lambda **kwargs: _ReadyPreflight(),
    )
    monkeypatch.setattr("agentcongress.cli.ExperimentRunner", FakeRunner)
    monkeypatch.setattr("agentcongress.cli.stage_one_summary", lambda *args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agentcongress",
            "experiment-five-arm",
            "task.yaml",
            "--repository",
            "target",
            "--pair-id",
            "replicate-protocol",
            "--randomization-seed",
            "29",
            "--runs-root",
            str(tmp_path),
        ],
    )

    assert main() == 0
    block = json.loads(
        (tmp_path / "replicate-protocol-five-arm-block.json").read_text(
            encoding="utf-8"
        )
    )
    assert block["status"] == "completed"
    assert block["invalid_arms"] == []
