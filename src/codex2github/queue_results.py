"""Follow a submitted CLI queue message in the read-only original session log."""
from __future__ import annotations

import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import sqlite3
import time

from .session_sync import db, item_text


def submission_snapshot(home: Path, sid: str) -> dict:
    snapshot = {"submitted_at_ms": int(time.time() * 1000)}
    database = home / "state_5.sqlite"
    if not database.exists():
        return snapshot
    try:
        with contextlib.closing(db(database)) as connection:
            row = connection.execute("SELECT rollout_path FROM threads WHERE id=?", (sid,)).fetchone()
        if row:
            path = Path(row[0])
            snapshot.update(rollout_path=str(path), rollout_offset=path.stat().st_size if path.exists() else 0)
    except (OSError, sqlite3.Error):
        pass
    return snapshot


def _milliseconds(value) -> int:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except (AttributeError, ValueError, TypeError):
        return 0


def follow(job: dict, receipt: dict, record: dict, prompt: str) -> dict | None:
    """Advance a persisted log cursor and return only this request's terminal result.

    The queue message ID is used when Codex preserves it as a client ID. Some
    Desktop versions generate a separate ID; in that case require the exact
    user message after the submission boundary. Tool output never participates.
    """
    value = record.get("rollout_path") or receipt.get("rollout_path")
    if not value:
        return None
    path = Path(value)
    if not path.exists() and Path(str(path) + ".zst").exists():
        path = Path(str(path) + ".zst")
    if not path.exists():
        return None
    state = job.setdefault("queue_monitor", {})
    if state.get("path") != str(path):
        offset = receipt.get("rollout_offset", 0) if str(path) == receipt.get("rollout_path") else 0
        state.update(path=str(path), offset=offset)
    if path.suffix != ".zst" and path.stat().st_size < state.get("offset", 0):
        state["offset"] = 0
    with contextlib.ExitStack() as stack:
        stream = stack.enter_context(path.open("rb"))
        if path.suffix == ".zst":
            import zstandard
            stream = stack.enter_context(zstandard.ZstdDecompressor().stream_reader(stream))
            if state.get("offset", 0):
                stream.seek(state["offset"])
            stream = stack.enter_context(io.BufferedReader(stream))
        else:
            stream.seek(state.get("offset", 0))
        while True:
            start = stream.tell()
            line = stream.readline()
            if not line or not line.endswith(b"\n"):
                state["offset"] = start
                break
            state["offset"] = stream.tell()
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            payload = row.get("payload", {})
            if not isinstance(payload, dict):
                continue
            timestamp = _milliseconds(row.get("timestamp"))
            if timestamp and timestamp < receipt.get("submitted_at_ms", 0):
                continue
            kind = payload.get("type")
            if row.get("type") == "event_msg" and kind == "task_started":
                state["current_turn_id"] = payload.get("turn_id")
            item = payload.get("item", {}) if kind == "item_completed" else payload
            if not isinstance(item, dict):
                continue
            metadata = item.get("internal_chat_message_metadata_passthrough", {})
            turn = payload.get("turn_id") or (metadata.get("turn_id") if isinstance(metadata, dict) else None) or state.get("current_turn_id")
            native = row.get("type") == "event_msg" and kind == "item_completed"
            response = row.get("type") == "response_item" and kind == "message"
            user = native and str(item.get("type", "")).casefold() == "usermessage" or response and item.get("role") == "user"
            if user and turn:
                exact_id = bool(receipt.get("queue_message_id")) and item.get("client_id", item.get("clientId")) == receipt["queue_message_id"]
                if exact_id or item_text(item).strip() == prompt.strip():
                    if not state.get("turn_id") or exact_id:
                        state["turn_id"] = turn
            if not state.get("turn_id") or turn != state["turn_id"]:
                continue
            final = native and str(item.get("type", "")).casefold() == "agentmessage" and item.get("phase") in ("final", "final_answer")
            final = final or response and item.get("role") == "assistant" and item.get("channel") == "final"
            if final:
                state["final"] = item_text(item).strip()
            if row.get("type") == "event_msg" and kind == "task_complete":
                state["completed"] = True
                if isinstance(payload.get("last_agent_message"), str) and payload["last_agent_message"].strip():
                    state["final"] = payload["last_agent_message"].strip()
            elif row.get("type") == "event_msg" and kind in ("turn_aborted", "task_failed"):
                state.update(completed=True, failed=True, final="本机对应任务已中断或失败；未重新执行原留言。")
    if state.get("completed") and state.get("final"):
        return {"turn_id": state["turn_id"], "final": state["final"],
                "ok": not state.get("failed", False), "execution_complete": True}
    return None
