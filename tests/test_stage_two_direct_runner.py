from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agentcongress.appserver_client import (
    AppServerProtocolError,
    SlotConfig,
    SlotResult,
    StandaloneSlotConfig,
)
from agentcongress.stage_two_direct_runner import (
    _add_metrics,
    _critic_prompt,
    _executor_prompt,
    execute_arm,
    finalize_run,
)


def _result(slot_id: str, payload: dict) -> SlotResult:
    return SlotResult(slot_id, f"thread-{slot_id}", f"turn-{slot_id}", "completed", {}, json.dumps(payload), payload)


class FakeClient:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.slots: list[SlotConfig | StandaloneSlotConfig] = []

    async def run_slot(self, slot: SlotConfig | StandaloneSlotConfig) -> SlotResult:
        self.slots.append(slot)
        return _result(slot.slot_id, self.payloads.pop(0))


REPORT = {
    "summary": "implemented",
    "changed_files": ["bottle.py"],
    "validation": ["local check"],
    "risks": [],
    "commit": None,
    "needs_human_input": False,
}
MEMO = {
    "summary": "header parsing is unsafe",
    "hypotheses": ["CRLF reaches header output"],
    "validation_plan": ["add focused regression"],
    "risks": ["avoid compatibility regression"],
}
CRITIC = {
    "intent": "interject",
    "reason": "also inspect bytes handling",
    "content": "Preserve byte/string compatibility while rejecting CRLF.",
    "urgency": 1.0,
    "relevance": 1.0,
    "novelty": 1.0,
    "confidence": 1.0,
}


def test_standalone_is_one_luna_slot_with_full_equal_budget(tmp_path: Path) -> None:
    client = FakeClient([REPORT])
    result = asyncio.run(execute_arm("standalone-luna", "fix it", tmp_path, client))
    assert result["agent_status"] == "agent_completed"
    assert len(client.slots) == 1
    assert isinstance(client.slots[0], StandaloneSlotConfig)
    assert client.slots[0].model == "gpt-5.6-luna"
    assert client.slots[0].max_seconds == 1200


def test_standalone_sol_is_the_same_single_slot_protocol(tmp_path: Path) -> None:
    client = FakeClient([REPORT])
    result = asyncio.run(execute_arm("standalone-sol", "fix it", tmp_path, client))
    assert result["agent_status"] == "agent_completed"
    assert len(client.slots) == 1
    assert isinstance(client.slots[0], StandaloneSlotConfig)
    assert client.slots[0].model == "gpt-5.6-sol"
    assert client.slots[0].max_seconds == 1200


def test_congress_records_real_floor_transcript_and_mixed_executor(tmp_path: Path) -> None:
    client = FakeClient([MEMO, CRITIC, REPORT])
    result = asyncio.run(execute_arm("luna-sol-congress", "fix it", tmp_path, client))
    assert result["agent_status"] == "agent_completed"
    assert [slot.model for slot in client.slots] == [
        "gpt-5.6-luna",
        "gpt-5.6-luna",
        "gpt-5.6-sol",
    ]
    assert [slot.max_seconds for slot in client.slots] == [240, 120, 840]
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    types = [event["type"] for event in events]
    assert "floor.requested" in types
    assert "floor.granted" in types
    assert types.count("speech.segment_committed") == 2
    assert "experiment.protocol_frozen" in types
    assert result["deliberation"]["critic"]["content"] == CRITIC["content"]


def test_abstaining_listener_content_is_not_shared(tmp_path: Path) -> None:
    critic = {**CRITIC, "intent": "abstain", "content": "private discarded thought"}
    result = asyncio.run(execute_arm("luna-congress", "fix it", tmp_path, FakeClient([MEMO, critic, REPORT])))
    assert result["deliberation"]["critic"]["content"] == ""
    assert result["deliberation"]["critic"]["floor_outcome"] == "retained"


def test_v3_prompts_require_falsification_without_adding_roles() -> None:
    critic = _critic_prompt("fix it", MEMO, "analyst spoke")
    executor = _executor_prompt("fix it", {"analyst_memo": MEMO})
    assert "falsification listener" in critic
    assert "concrete evidence" in critic
    assert "untrusted hypotheses" in executor
    assert "disconfirming check" in executor


def test_finalize_records_binary_verifier_and_completes_meeting(tmp_path: Path) -> None:
    result = asyncio.run(execute_arm("luna-congress", "fix it", tmp_path, FakeClient([MEMO, CRITIC, REPORT])))
    (tmp_path / "agent-result.json").write_text(json.dumps(result), encoding="utf-8")
    score = tmp_path / "score.json"
    score.write_text('{"reward":1}', encoding="utf-8")
    final = finalize_run(tmp_path, score)
    assert final["status"] == "valid_submission"
    assert final["objective_success"] is True
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events[-1]["payload"]["phase"] == "completed"
    assert any(event["type"] == "benchmark.verification_completed" for event in events)
    assert not any(
        event["type"] == "task.status_changed"
        and event["payload"].get("status") == "ready_for_report"
        for event in events
    )


def test_failure_is_fixed_and_secret_is_not_reflected(tmp_path: Path) -> None:
    class Broken:
        async def run_slot(self, slot):
            raise RuntimeError("SECRET-HOST-PATH")

    result = asyncio.run(execute_arm("standalone-luna", "fix it", tmp_path, Broken()))
    serialized = json.dumps(result)
    assert result["agent_status"] == "agent_failed"
    assert result["error_code"] == "direct_runner_error"
    assert "SECRET" not in serialized


def test_protocol_failure_preserves_only_safe_event_identity(tmp_path: Path) -> None:
    class Broken:
        async def run_slot(self, slot):
            raise AppServerProtocolError(
                "native_capability_observed",
                "blocked",
                method="item/started",
                itemType="plan",
                name="taskenv",
                status="starting",
                secret="DO-NOT-REFLECT",
            )

    result = asyncio.run(execute_arm("standalone-luna", "fix it", tmp_path, Broken()))
    assert result["error_details"] == {
        "method": "item/started",
        "itemType": "plan",
        "name": "taskenv",
        "status": "starting",
    }
    assert result["runner_elapsed_seconds"] >= 0
    assert "DO-NOT-REFLECT" not in json.dumps(result)


def test_failed_slot_keeps_elapsed_and_usage_for_fair_accounting(tmp_path: Path) -> None:
    class TimedOut:
        async def run_slot(self, slot):
            raise AppServerProtocolError(
                "slot_deadline_exceeded",
                "timed out",
                threadId="thread-timeout",
                turnId="turn-timeout",
                usage={
                    "input_tokens": 100,
                    "cached_input_tokens": 80,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 5,
                    "reasoning_output_tokens": 2,
                    "total_tokens": 105,
                },
            )

    result = asyncio.run(execute_arm("luna-congress", "fix it", tmp_path, TimedOut()))
    assert result["error_code"] == "slot_deadline_exceeded"
    assert len(result["slots"]) == 1
    assert result["slots"][0]["actor"] == "analyst"
    assert result["slots"][0]["thread_id"] == "thread-timeout"
    assert result["slots"][0]["usage"]["total_tokens"] == 105
    _add_metrics(result)
    assert result["metrics"]["usage_by_model"]["gpt-5.6-luna"]["total_tokens"] == 105


def test_metrics_aggregate_usage_and_label_cost_as_api_equivalent() -> None:
    result = {
        "slots": [
            {
                "model": "gpt-5.6-luna",
                "elapsed_seconds": 12.5,
                "usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 400,
                    "output_tokens": 100,
                    "reasoning_output_tokens": 50,
                    "total_tokens": 1100,
                },
            }
        ]
    }
    _add_metrics(result)
    assert result["metrics"]["model_elapsed_seconds"] == 12.5
    assert result["metrics"]["estimated_api_equivalent_cost_usd"] == 0.00124
    assert "not the actual" in result["metrics"]["billing_note"]
