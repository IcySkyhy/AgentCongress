from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from hashlib import sha256
from pathlib import Path

from .models import Event


def meeting_lock_path(database: Path, meeting_id: str) -> Path:
    """Return a stable, path-safe lock file for one meeting."""
    digest = sha256(meeting_id.encode("utf-8")).hexdigest()[:20]
    return database.resolve().parent / f".{database.name}.meeting-{digest}.lock"


class MeetingFileLock:
    """Small cross-process exclusive lock backed by one local file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        try:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except BaseException:
            lock_file.close()
            raise
        self._file = lock_file

    def release(self) -> None:
        lock_file = self._file
        if lock_file is None:
            return
        try:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()
            self._file = None


class SQLiteEventStore:
    """Append-only meeting event store; SQLite is the recovery source of truth."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
            meeting_id TEXT NOT NULL, type TEXT NOT NULL, actor_id TEXT NOT NULL,
            timestamp REAL NOT NULL, causation_id TEXT, correlation_id TEXT,
            schema_version INTEGER NOT NULL, payload TEXT NOT NULL)"""
        )
        self.connection.commit()

    def append(self, event: Event) -> Event:
        event_id = event.event_id or str(uuid.uuid4())
        timestamp = event.timestamp or time.time()
        cursor = self.connection.execute(
            "INSERT INTO events(event_id, meeting_id, type, actor_id, timestamp, causation_id, correlation_id, schema_version, payload) VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, event.meeting_id, event.type, event.actor_id, timestamp, event.causation_id, event.correlation_id, event.schema_version, json.dumps(event.payload, sort_keys=True)),
        )
        self.connection.commit()
        return Event(**{**event.as_dict(), "event_id": event_id, "timestamp": timestamp, "sequence": cursor.lastrowid})

    def replay(self, meeting_id: str) -> list[Event]:
        rows = self.connection.execute("SELECT sequence,event_id,type,actor_id,timestamp,causation_id,correlation_id,schema_version,payload FROM events WHERE meeting_id=? ORDER BY sequence", (meeting_id,))
        return [Event(sequence=row[0], event_id=row[1], type=row[2], actor_id=row[3], timestamp=row[4], causation_id=row[5], correlation_id=row[6], schema_version=row[7], payload=json.loads(row[8]), meeting_id=meeting_id) for row in rows]

    def export_jsonl(self, meeting_id: str, output: Path) -> int:
        events = self.replay(meeting_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("".join(json.dumps(event.as_dict(), sort_keys=True) + "\n" for event in events), encoding="utf-8")
        return len(events)

    def close(self) -> None:
        self.connection.close()
