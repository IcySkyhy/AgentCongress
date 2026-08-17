import asyncio
from pathlib import Path

from agentcongress.llm.tools import MeetingToolExecutor, meeting_tools
from agentcongress.runtime import CongressRuntime


def _runtime(tmp_path: Path) -> CongressRuntime:
    runtime = CongressRuntime("m", tmp_path / "events.db", ["architect", "reviewer"])
    runtime.start("architect", "reviewer")
    return runtime


async def _run(executor: MeetingToolExecutor, name: str, **arguments) -> dict:
    import json

    return json.loads(await executor.run(name, arguments))


def test_blackboard_tools_persist_and_read_shared_context(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    try:
        executor = MeetingToolExecutor(runtime)
        added = asyncio.run(_run(executor, "blackboard_add", kind="decision", content="Use SQLite."))
        assert added == {"ok": True, "kind": "decision"}
        assert runtime.blackboard_context().startswith("[decision] Use SQLite.")
        entries = asyncio.run(_run(executor, "blackboard_get"))
        assert entries["entries"][0]["kind"] == "decision"
    finally:
        runtime.close()


def test_floor_and_transcript_tools_read_live_state(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    try:
        runtime.commit_segment("We should use an event log.")
        executor = MeetingToolExecutor(runtime)
        floor = asyncio.run(_run(executor, "floor_status"))
        assert floor["speaker_id"] == "architect" and floor["addressee_id"] == "reviewer"
        transcript = asyncio.run(_run(executor, "transcript_get"))
        assert transcript["segments"][-1]["content"] == "We should use an event log."
    finally:
        runtime.close()


def test_task_tools_list_and_read_tasks(tmp_path: Path) -> None:
    from agentcongress.models import Task, TaskStatus

    runtime = _runtime(tmp_path)
    try:
        runtime.propose_task(Task("t1", "Store traces.", "reviewer", ("events persisted",), [], []))
        runtime.transition_task("t1", TaskStatus.ASSIGNED, "reviewer")
        executor = MeetingToolExecutor(runtime)
        listing = asyncio.run(_run(executor, "task_list"))
        assert listing["tasks"][0]["task_id"] == "t1"
        detail = asyncio.run(_run(executor, "task_get", task_id="t1"))
        assert detail["acceptance_criteria"] == ["events persisted"]
        missing = asyncio.run(_run(executor, "task_get", task_id="nope"))
        assert "error" in missing
    finally:
        runtime.close()


def test_read_file_is_jailed_and_size_capped(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "design.md").write_text("Proposal text.", encoding="utf-8")
    runtime = _runtime(tmp_path)
    try:
        executor = MeetingToolExecutor(runtime, workspace_root=workspace)
        result = asyncio.run(_run(executor, "read_file", path="design.md"))
        assert result["content"] == "Proposal text."

        escape = asyncio.run(_run(executor, "read_file", path="../outside.txt"))
        assert "escapes" in escape["error"]

        missing = asyncio.run(_run(executor, "read_file", path="missing.md"))
        assert "not a file" in missing["error"]

        (workspace / "big.bin").write_bytes(b"x" * (65 * 1024))
        capped = asyncio.run(_run(executor, "read_file", path="big.bin"))
        assert "64 KiB" in capped["error"]
    finally:
        runtime.close()


def test_meeting_tools_include_file_reader_only_with_workspace(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    try:
        specs, executor = meeting_tools(runtime)
        assert [tool.name for tool in specs] == [
            "blackboard_add",
            "blackboard_get",
            "transcript_get",
            "floor_status",
            "task_list",
            "task_get",
        ]
        assert executor.workspace_root is None
        specs_with, executor_with = meeting_tools(runtime, workspace_root=tmp_path)
        assert specs_with[-1].name == "read_file"
        assert executor_with.workspace_root == tmp_path
    finally:
        runtime.close()


def test_unknown_tool_returns_error(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    try:
        executor = MeetingToolExecutor(runtime)
        result = asyncio.run(_run(executor, "nope"))
        assert result == {"error": "unknown tool: nope"}
    finally:
        runtime.close()
