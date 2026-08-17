from agentcongress.adapters import WorkerEvent
from agentcongress.reports import extract_task_report


_REPORT = {
    "summary": "done",
    "changed_files": ["src/app.py"],
    "validation": ["tests passed"],
    "risks": [],
    "commit": None,
    "needs_human_input": False,
}


def test_extracts_report_only_from_completed_agent_message() -> None:
    events = [
        WorkerEvent(
            "codex.event",
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": __import__("json").dumps(_REPORT)},
            },
        )
    ]
    assert extract_task_report(events).summary == "done"


def test_does_not_trust_schema_shaped_tool_output() -> None:
    events = [
        WorkerEvent(
            "codex.event",
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "aggregated_output": _REPORT},
            },
        )
    ]
    assert extract_task_report(events) is None


def test_explicit_adapter_report_is_supported() -> None:
    assert extract_task_report([WorkerEvent("task.report", _REPORT)]).summary == "done"
