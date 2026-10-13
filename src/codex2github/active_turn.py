"""Read the newest lifecycle event from the original thread log, without writes."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sqlite3

from .session_sync import db


def _reverse_lines(path: Path):
    with path.open("rb") as stream:
        stream.seek(0, 2)
        position, suffix = stream.tell(), b""
        while position:
            length = min(position, 128 * 1024)
            position -= length
            stream.seek(position)
            parts = (stream.read(length) + suffix).split(b"\n")
            suffix = parts[0]
            yield from reversed(parts[1:])
        if suffix:
            yield suffix


def active_turn(home: Path, sid: str, record: dict | None = None) -> dict:
    value = (record or {}).get("rollout_path")
    if not value:
        try:
            with contextlib.closing(db(home / "state_5.sqlite")) as connection:
                row = connection.execute("SELECT rollout_path FROM threads WHERE id=?", (sid,)).fetchone()
            value = row[0] if row else None
        except (OSError, sqlite3.Error):
            pass
    if not value:
        return {"status": "unknown"}
    path = Path(value)
    if not path.is_file():
        return {"status": "idle"} if Path(str(path) + ".zst").is_file() else {"status": "unknown"}
    try:
        for line in _reverse_lines(path):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            payload = row.get("payload", {})
            if row.get("type") != "event_msg" or not isinstance(payload, dict):
                continue
            kind = payload.get("type")
            if kind == "task_started" and payload.get("turn_id"):
                return {"status": "active", "turn_id": payload["turn_id"]}
            if kind in {"task_complete", "turn_aborted", "task_failed"}:
                return {"status": "idle", "turn_id": payload.get("turn_id")}
    except OSError:
        return {"status": "unknown"}
    return {"status": "unknown"}
