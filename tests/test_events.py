import os
import subprocess
import sys
import time
from pathlib import Path

from agentcongress.events import MeetingFileLock


def test_meeting_file_lock_serializes_processes(tmp_path: Path) -> None:
    lock_path = tmp_path / "meeting.lock"
    ready_marker = tmp_path / "child-ready"
    acquired_marker = tmp_path / "child-acquired"
    lock = MeetingFileLock(lock_path)
    lock.acquire()
    project_src = Path(__file__).parents[1] / "src"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(project_src), environment.get("PYTHONPATH", "")))
    )
    script = """
import sys
from pathlib import Path
from agentcongress.events import MeetingFileLock

lock = MeetingFileLock(Path(sys.argv[1]))
Path(sys.argv[2]).write_text("ready", encoding="utf-8")
lock.acquire()
Path(sys.argv[3]).write_text("acquired", encoding="utf-8")
lock.release()
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(lock_path),
            str(ready_marker),
            str(acquired_marker),
        ],
        env=environment,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_marker.exists()
        time.sleep(0.1)
        assert child.poll() is None
        assert not acquired_marker.exists()
        lock.release()
        assert child.wait(timeout=5) == 0
        assert acquired_marker.read_text(encoding="utf-8") == "acquired"
    finally:
        lock.release()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
