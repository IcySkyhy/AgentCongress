from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .adapters import WorkerEvent
from .models import TaskReport


def _candidate_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _candidate_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _candidate_dicts(nested)
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return
            yield from _candidate_dicts(parsed)


def extract_task_report(events: Iterable[WorkerEvent]) -> TaskReport | None:
    """Find the final schema-shaped report in a trusted terminal message.

    Tool results and command output are untrusted task data: either can contain
    an echoed schema example that happens to validate.  Codex emits the
    output-schema response as a completed ``agent_message``, so only that
    channel (plus an explicit adapter-level ``task.report`` event) may satisfy
    the worker handoff contract.
    """
    report: TaskReport | None = None
    for event in events:
        candidates: Iterable[dict[str, Any]] = ()
        if event.type == "task.report":
            candidates = _candidate_dicts(event.payload)
        elif event.type == "codex.event" and event.payload.get("type") == "item.completed":
            item = event.payload.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                candidates = _candidate_dicts(item.get("text"))
        for candidate in candidates:
            try:
                report = TaskReport.from_payload(candidate)
            except ValueError:
                continue
    return report
