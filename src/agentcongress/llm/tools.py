"""Meeting-scoped tools for the agent loop.

Every effect stays inside the meeting event store (blackboard entries are
persisted as ``blackboard.updated`` events); the only filesystem access is a
read-only, jail-rooted file reader over the meeting workspace.  This is the
deliberate trust boundary: meeting agents act like lightweight Codex agents
for inspection and shared state, while executable work remains with the
sandboxed worker slots.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime import CongressRuntime
from .agent import ToolExecutor
from .base import ToolSpec

_MAX_FILE_BYTES = 64 * 1024
_BLACKBOARD_LIMIT = 20
_TRANSCRIPT_LIMIT = 20


def _json_result(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


@dataclass(slots=True)
class MeetingToolExecutor:
    """Runs meeting tools against the live runtime; actor is the current speaker."""

    runtime: CongressRuntime
    workspace_root: Path | None = None

    def _actor(self) -> str:
        return self.runtime.state.speaker_id or "operator"

    async def run(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "blackboard_add":
            return self._blackboard_add(arguments)
        if name == "blackboard_get":
            return _json_result(
                {"entries": [entry.as_payload() for entry in self.runtime.state.blackboard[-_BLACKBOARD_LIMIT:]]}
            )
        if name == "transcript_get":
            return _json_result({"segments": self.runtime.state.transcript[-_TRANSCRIPT_LIMIT:]})
        if name == "floor_status":
            state = self.runtime.state
            return _json_result(
                {
                    "phase": state.phase,
                    "speaker_id": state.speaker_id,
                    "addressee_id": state.addressee_id,
                    "return_speaker_id": state.return_speaker_id,
                }
            )
        if name == "task_list":
            return _json_result(
                {
                    "tasks": [
                        {
                            "task_id": task.task_id,
                            "title": task.title,
                            "assignee_id": task.assignee_id,
                            "status": task.status,
                        }
                        for task in self.runtime.state.tasks.values()
                    ]
                }
            )
        if name == "task_get":
            return self._task_get(arguments)
        if name == "read_file":
            return self._read_file(arguments)
        return _json_result({"error": f"unknown tool: {name}"})

    def _blackboard_add(self, arguments: dict[str, Any]) -> str:
        try:
            kind = str(arguments.get("kind", "")).strip()
            content = str(arguments.get("content", "")).strip()
            evidence_raw = arguments.get("evidence", [])
            evidence = [str(item) for item in evidence_raw] if isinstance(evidence_raw, list) else []
            self.runtime.add_blackboard(kind, content, self._actor(), evidence=evidence[:12])
            return _json_result({"ok": True, "kind": kind})
        except ValueError as error:
            return _json_result({"error": str(error)})

    def _task_get(self, arguments: dict[str, Any]) -> str:
        task = self.runtime.state.tasks.get(str(arguments.get("task_id", "")))
        if task is None:
            return _json_result({"error": "unknown task"})
        return _json_result(
            {
                "task_id": task.task_id,
                "title": task.title,
                "assignee_id": task.assignee_id,
                "status": task.status,
                "acceptance_criteria": task.acceptance_criteria,
                "allowed_paths": task.allowed_paths,
                "validation_commands": task.validation_commands,
            }
        )

    def _read_file(self, arguments: dict[str, Any]) -> str:
        if self.workspace_root is None:
            return _json_result({"error": "no meeting workspace configured"})
        raw = arguments.get("path")
        if not isinstance(raw, str) or not raw.strip():
            return _json_result({"error": "path is required"})
        try:
            root = self.workspace_root.resolve()
            candidate = (self.workspace_root / raw).resolve()
            if not candidate.is_relative_to(root):
                return _json_result({"error": "path escapes the meeting workspace"})
            if not candidate.is_file():
                return _json_result({"error": "not a file in the meeting workspace"})
            if candidate.stat().st_size > _MAX_FILE_BYTES:
                return _json_result({"error": "file exceeds the 64 KiB read limit"})
            return _json_result(
                {
                    "path": str(candidate.relative_to(root)),
                    "content": candidate.read_text(encoding="utf-8", errors="replace"),
                }
            )
        except OSError as error:
            return _json_result({"error": str(error)})


def meeting_tools(
    runtime: CongressRuntime,
    *,
    workspace_root: Path | None = None,
) -> tuple[list[ToolSpec], ToolExecutor]:
    specs: list[ToolSpec] = [
        ToolSpec(
            "blackboard_add",
            "Record a confirmed decision, observation, or requirement on the shared meeting blackboard.",
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "Entry kind, e.g. decision, question, risk."},
                    "content": {"type": "string", "description": "The confirmed statement to record."},
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Supporting evidence lines, up to 12.",
                    },
                },
                "required": ["kind", "content"],
            },
        ),
        ToolSpec("blackboard_get", "Read the current shared blackboard entries.", {"type": "object", "properties": {}}),
        ToolSpec("transcript_get", "Read recent committed discussion segments.", {"type": "object", "properties": {}}),
        ToolSpec(
            "floor_status",
            "Read the current meeting phase, speaker, and addressee.",
            {"type": "object", "properties": {}},
        ),
        ToolSpec("task_list", "List the meeting's tasks and their statuses.", {"type": "object", "properties": {}}),
        ToolSpec(
            "task_get",
            "Read one task's details, including its acceptance criteria.",
            {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        ),
    ]
    if workspace_root is not None:
        specs.append(
            ToolSpec(
                "read_file",
                "Read a text file inside the meeting workspace (read-only, 64 KiB limit).",
                {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Path relative to the meeting workspace root."}},
                    "required": ["path"],
                },
            )
        )
    return specs, MeetingToolExecutor(runtime, workspace_root)


def floor_request_tool() -> ToolSpec:
    return ToolSpec(
        "request_floor",
        "Request the meeting floor to make a concrete, material contribution. Call only when the current speaker's segment demands an immediate correction or a strictly additive insight that cannot wait for your own turn.",
        {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["brief_interjection", "replace_speaker", "replace_addressee"],
                },
                "urgency": {"type": "number", "minimum": 0, "maximum": 1},
                "relevance": {"type": "number", "minimum": 0, "maximum": 1},
                "novelty": {"type": "number", "minimum": 0, "maximum": 1},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string", "description": "Short public reason for the request."},
            },
            "required": ["intent", "urgency", "relevance", "novelty", "confidence", "reason"],
        },
    )


class FloorRequestToolExecutor:
    """The request_floor tool has no side effect of its own; the call itself is the request."""

    async def run(self, name: str, arguments: dict[str, Any]) -> str:
        return _json_result({"received": True, "name": name})
