"""Route GitHub conversation comments to exact existing Codex thread IDs."""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime
from urllib.parse import urlencode

from .session_sync import (BASE, api, db, history_messages, redact, title_of, label,
                            write_if_changed, singleton, is_subagent)
from . import steer_visual

from .config import SETTINGS, current_route_fingerprint, current_route_model, is_private_task_repository
from .route_models import model_catalog as read_route_models
from .queue_results import follow as follow_queued_result
from .reasoning import model_efforts, model_efforts_many, default_effort, validate as validate_effort
from .active_turn import active_turn
from .thread_settings import steer_existing_thread

# Kept as a compatibility symbol for older integrations/tests.  It is never
# used as a routing default; the active Codex route is read from codex_home.
MODEL = SETTINGS.model

REPO = SETTINGS.repo
PREFIX = f"repos/{REPO}"
WORKSPACE = SETTINGS.workspace
HOME = SETTINGS.codex_home
STATE = BASE / "conversation-state.json"
BOT = "<!-- codex2github:reply -->"
HUB_REVISION = 12
HELP = """直接留言即可继续当前绑定的本机会话。

- `/session 会话名或ID`：连接已有会话，也接受唯一的 ID 前缀。
- `/model 模型ID [思考档位]`：切换当前会话模型，可同时选择档位，下一条留言生效。
- `/models`：重新读取当前路由的工作模型列表。
- `/effort`：查看当前模型的思考档位；`/effort high` 切换，`/effort auto` 跟随本机默认。
- `/status`：查看会话、模型及排队状态。
- `/steer 内容`：引导当前正在执行的任务，不等待本轮结束。
  发送后出现送达状态卡：👀 已读 → 🟡 已提交 → 🟢 会话已接收；未确认会明确显示。
- `/queue 内容`：等当前回复结束后再执行；普通留言默认排队。
- `/queue`：查看当前 Issue 的排队消息及消息 ID。
- `/cancel` 或 `/cancel 消息ID`：撤回最近一条或指定的排队消息。
- `/cancel all`：清空当前 Issue 的等待队列，正在执行的任务继续运行。
- `/close`：按你的明确指令关闭当前任务。

任务回复后保持打开，只有你手动关闭或明确发送关闭指令才关闭。

模型指令只影响当前 Issue 绑定的单个会话，不会写入 Codex 或 CC Switch 配置。运行中的这一轮会完成，后续留言按新模型执行。"""


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def model_catalog(home: Path = HOME, *, refresh: bool = False) -> list[str]:
    return read_route_models(home, refresh=refresh)


def threads(home: Path = HOME) -> dict:
    names = {}
    path = home / "session_index.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line); names[item["id"]] = item.get("thread_name", "")
            except (ValueError, KeyError):
                pass
    with contextlib.closing(db(home / "state_5.sqlite")) as connection:
        records = [dict(row) for row in connection.execute("SELECT * FROM threads ORDER BY updated_at DESC")]
    for item in records:
        item["display_title"] = title_of(item, names)
    return {item["id"]: item for item in records}


def resolve(selector: str, records: dict) -> str:
    selector = selector.strip()
    if selector in records:
        return selector
    exact = [sid for sid, row in records.items() if selector.casefold() in (row["display_title"].casefold(), (row.get("name") or "").casefold())]
    found = exact or [sid for sid, row in records.items() if sid.startswith(selector.lower()) or selector.casefold() in row["display_title"].casefold()]
    if len(found) == 1:
        return found[0]
    if not found:
        raise ValueError("未找到这个会话。请使用会话名或目录中的会话 ID。")
    choices = "\n".join(f"- `{sid[:12]}`：{records[sid]['display_title']}" for sid in found[:12])
    raise ValueError("有多个匹配，请用下面的 ID 前缀选择：\n\n" + choices)


def command(text: str) -> tuple[str, str, str] | None:
    first, _, rest = text.strip().partition("\n")
    match = re.fullmatch(r"/(session|model|models|effort|reasoning|status|help|close|steer|queue|cancel)(?:\s+(.*))?", first, re.I)
    if match:
        return match[1].lower(), (match[2] or "").strip(), rest.strip()
    if not rest.strip() and re.fullmatch(r"(?:请)?关闭(?:当前|这个|本)(?:任务|会话|[Ii]ssue)[。！!]?", first):
        return "close", "", ""
    match = re.fullmatch(r"(?:切换模型(?:为|到)?|模型切换(?:成|为)?)[：:\s]*([A-Za-z0-9._/:-]+)", first)
    if match:
        return "model", match[1], rest.strip()
    return None


def process_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0); return True
        except ProcessLookupError:
            return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def page_list(endpoint: str) -> list:
    results, number = [], 1
    while True:
        separator = "&" if "?" in endpoint else "?"
        rows = api(f"{endpoint}{separator}per_page=100&page={number}")
        results.extend(rows)
        if len(rows) < 100:
            return results
        number += 1


class Router:
    def __init__(self, home: Path = HOME, state_path: Path = STATE):
        self.home, self.path = home, state_path
        self.state = read_json(state_path, {"version": 6, "models": {}, "issues": {}, "rooms": {}, "jobs": {}})
        # Migrate old bridge-only state without retaining a global model knob.
        self.state.pop("default_model", None)
        self.state.setdefault("efforts", {})
        self.records = self.load_records()
        self.catalog = model_catalog(home)
        self.sync_route()

    def load_records(self, force: bool = False) -> dict:
        signal = read_json(self.path.parent / "session-events.json", {})
        cache_path = self.path.parent / "thread-index-cache.json"
        cache = read_json(cache_path, {})
        revision = signal.get("metadata", "")
        home = str(self.home.resolve())
        if not force and cache.get("home") == home and cache.get("revision") == revision and time.time() - cache.get("created", 0) < 900:
            return cache["records"]
        records = threads(self.home)
        write_if_changed(cache_path, json.dumps({"home": home, "revision": revision, "created": time.time(), "records": records}, ensure_ascii=False))
        return records

    def save(self):
        self.state.pop("default_model", None)
        write_if_changed(self.path, json.dumps(self.state, ensure_ascii=False, indent=2) + "\n")

    def sync_route(self):
        """Invalidate bridge-only model overrides when the local route changes."""
        fingerprint = current_route_fingerprint(self.home)
        previous = self.state.get("route", {}).get("fingerprint")
        if previous == fingerprint:
            return False
        # A missing marker means state from an older bridge version.  Its
        # overrides cannot be proven to belong to the current route, so they
        # are discarded before any new request is accepted.
        self.state.setdefault("models", {}).clear()
        self.state.setdefault("efforts", {}).clear()
        for info in self.state.setdefault("issues", {}).values():
            info.pop("model", None)
            info.pop("effort", None)
            for request in info.get("queue", []):
                request["model"] = "auto"
                request["effort"] = "auto"
        self.state["route"] = {"fingerprint": fingerprint,
                                "model": current_route_model(self.home) or "auto"}
        self.save()
        return True

    def reply(self, number: int, text: str) -> dict:
        return api(f"{PREFIX}/issues/{number}/comments", "POST", {"body": BOT + "\n" + text})

    def steer_card_body(self, number: int, comment: int) -> str:
        receipt = self.state["issues"][str(number)]["steers"][str(comment)]
        sid = receipt.get("sid")
        title = self.records.get(sid, {}).get("display_title", "原会话" if sid else "尚未绑定会话")
        return BOT + "\n" + steer_visual.render(REPO, number, comment, receipt, title, self.state["rooms"].get(sid))

    def publish_steer_card(self, number: int, comment: int):
        """Update one card; a failed UI write must never resubmit model input."""
        receipt = self.state["issues"][str(number)]["steers"][str(comment)]
        body = self.steer_card_body(number, comment)
        digest = hashlib.sha256(body.encode()).hexdigest()
        try:
            if receipt.get("visual_body_hash") != digest:
                card_id = receipt.get("visual_comment")
                if not card_id and receipt.get("visual_post_attempted"):
                    # Recover an accepted POST whose response was lost.
                    prefix = BOT + "\n" + steer_visual.marker(comment)
                    existing = next((item for item in page_list(f"{PREFIX}/issues/{number}/comments")
                                     if (item.get("body") or "").startswith(prefix)), None)
                    if existing:
                        card_id = receipt["visual_comment"] = existing["id"]
                if card_id:
                    api(f"{PREFIX}/issues/comments/{card_id}", "PATCH", {"body": body})
                else:
                    receipt["visual_post_attempted"] = True
                    self.save()
                    card = api(f"{PREFIX}/issues/{number}/comments", "POST", {"body": body})
                    receipt["visual_comment"] = card["id"]
                receipt["visual_body_hash"] = digest
                receipt["visual_version"] = steer_visual.VERSION
                self.save()
            if comment:
                reactions = receipt.setdefault("visual_reactions", [])
                wanted = ["eyes"] + (["rocket"] if receipt.get("ok") else [])
                for reaction in wanted:
                    if reaction not in reactions:
                        api(f"{PREFIX}/issues/comments/{comment}/reactions", "POST", {"content": reaction})
                        reactions.append(reaction)
                        self.save()
        except (OSError, RuntimeError, KeyError, subprocess.TimeoutExpired) as error:
            # Retry presentation on the next tick; delivery state is authoritative.
            print(f"Steer card #{number}/{comment} will retry: {redact(str(error))[:300]}", flush=True)

    def current_model(self, info: dict) -> str:
        self.sync_route()
        sid = info.get("sid")
        return self.state["models"].get(sid) or (info.get("model") if not sid else "") or current_route_model(self.home) or "auto"

    def current_effort(self, info: dict) -> str:
        self.sync_route()
        sid = info.get("sid")
        selected = self.state["efforts"].get(sid) or (info.get("effort") if not sid else "")
        if selected:
            return selected
        return default_effort(self.home, self.current_model(info))

    def set_effort(self, info: dict, effort: str):
        store = self.state["efforts"] if info.get("sid") else info
        key = info["sid"] if info.get("sid") else "effort"
        if effort == "auto":
            store.pop(key, None)
        else:
            store[key] = effort

    def effort_options(self, model: str) -> str:
        options = model_efforts(self.home, model)
        available = " / ".join(f"`{level}`" for level in options["levels"]) or "目录未提供可调整档位"
        note = "" if options["verified"] else "\n\n当前路由目录未标注该模型的档位；以上为客户端可选值，实际支持范围由路由执行时校验。"
        return available + note

    def discover_job_sessions(self):
        changed = False
        for number, job in self.state["jobs"].items():
            if job.get("sid"):
                continue
            path = Path(job["directory"]) / "result.md"
            try:
                with path.open(encoding="utf-8-sig", errors="replace") as stream:
                    header = stream.read(8192)
            except OSError:
                continue
            match = re.search(r"session id:\s*([0-9a-f-]{36})", header)
            if not match or match[1] not in self.records:
                continue
            sid = match[1]
            info = self.state["issues"][number]
            info["sid"] = job["sid"] = sid
            self.state["rooms"].setdefault(sid, int(number))
            changed = True
        if changed:
            self.save()

    def render_hub(self) -> str:
        records = sorted(self.records.items(), key=lambda item: (item[1].get("updated_at_ms") or item[1].get("updated_at", 0) * 1000, item[0]), reverse=True)
        main = [(sid, row) for sid, row in records if not is_subagent(row)]
        agents = [(sid, row) for sid, row in records if is_subagent(row)]
        running = {job.get("sid") for job in self.state["jobs"].values() if job.get("sid")}
        queued = {request.get("sid") or info.get("sid") for info in self.state["issues"].values() for request in info.get("queue", [])}
        current = self.state["issues"].get(str(self.state.get("hub")), {})
        current_id = current.get("sid")
        current_title = self.records.get(current_id, {}).get("display_title", "尚未选择")
        new_chat = f"https://github.com/{REPO}/issues/new?" + urlencode({"title": "新会话", "labels": "codex-session"})
        text = (
            "<!-- codex2github:hub -->\n# 本机 Codex 对话入口\n\n"
            f"**[➕ 新建会话]({new_chat})**：新建 Issue 后直接发送任务，也可以先选择模型和思考档位，无需先在桌面端创建。\n\n"
            "**本机会话列表由文件变化自动触发更新，连续写入合并处理，不调用模型、不消耗模型 token。GitHub 留言每 15 秒检查；手机端下拉刷新或重新打开即可看到最新列表。**\n\n"
            "最近主会话和其余主会话仅显示主会话；子代理统一放在下方的“子代理会话”列表。\n\n"
            "点击已有的聊天链接直接交流；没有聊天页的会话，在下面评论框发送该行的 `/session ID前缀` 即可连接。\n\n"
            f"当前入口连接：{label(current_title[:70])} · ID：`{current_id or '未选择'}` · 模型：`{self.current_model(current)}` · 思考档位：`{self.current_effort(current)}`\n\n"
            f"共 {len(records)} 个会话：主会话 {len(main)}，子代理 {len(agents)}，已归档 {sum(bool(row.get('archived')) for sid, row in records)}。\n\n"
            + HELP + "\n\n## 最近主会话\n\n| 会话 | 完整会话 ID | 进入对话或选择指令 | 状态 |\n|---|---|---|---|\n"
        )

        def row_text(sid, row, full=False):
            number = self.state["rooms"].get(sid)
            info = self.state["issues"].get(str(number), {}) if number else {}
            name = info.get("title") or row["display_title"]
            name = label(re.sub(r"\s+", " ", redact(name)).strip()[:40 if full else 30])
            select = f"`/session {sid[:12]}`"
            if number:
                select = f"[进入对话 #{number}](https://github.com/{REPO}/issues/{number})"
            status = "执行中" if sid in running else "排队中" if sid in queued else "已归档" if row.get("archived") else "可连接"
            if full:
                return f"| {name} | `{sid}` | {select} | {status} |\n"
            return f"| {name} | {select} | {status} |\n"

        recent = main[:20]
        text += "".join(row_text(sid, row, True) for sid, row in recent)
        # Keep all currently indexed records in expandable sections. Bound only
        # future growth to the GitHub Issue body character limit, without losing
        # the ability to select any record by name/ID.
        shown = len(recent)
        for heading, group in (("其余主会话", main[20:]), ("子代理会话", agents)):
            if not group:
                continue
            text += f"\n<details>\n<summary>{heading}（{len(group)}）</summary>\n\n| 会话 | 进入对话或选择指令 | 状态 |\n|---|---|---|\n"
            for sid, row in group:
                line = row_text(sid, row)
                if len(text) + len(line) > 58000:
                    break
                text += line
                shown += 1
            text += "\n</details>\n"
        if shown < len(records):
            text += f"\n本页显示 {shown} 条，其余 {len(records) - shown} 条仍可用 `/session 会话名或ID` 连接。\n"
        text += f"\n[完整历史与会话目录](https://github.com/{REPO}/blob/main/sessions/all-001.md)\n"
        return text

    def refresh_hub(self, issue: dict | None = None):
        number = self.state.get("hub")
        if not number:
            return
        if issue is None:
            issue = api(f"{PREFIX}/issues/{number}")
        previous = re.sub(r"(?m)^列表更新时间：[^\n]*\n\n", "", issue.get("body") or "")
        previous_hash = hashlib.sha256(previous.encode()).hexdigest()
        relevant = [HUB_REVISION, self.records, self.state["rooms"], self.state["models"], self.state["efforts"], self.state.get("route"), number,
                    {key: {"sid": info.get("sid"), "title": info.get("title"), "model": info.get("model"), "effort": info.get("effort"),
                           "queue": [request.get("sid") for request in info.get("queue", [])]}
                     for key, info in self.state["issues"].items()},
                    sorted(job.get("sid") or "" for job in self.state["jobs"].values())]
        fingerprint = hashlib.sha256(json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if self.state.get("hub_fingerprint") == fingerprint and self.state.get("hub_body_hash") == previous_hash:
            return
        body = self.render_hub()
        if previous == body:
            self.state.update(hub_fingerprint=fingerprint, hub_body_hash=previous_hash)
            self.save()
            return
        stamped = body.replace("# 本机 Codex 对话入口\n\n", "# 本机 Codex 对话入口\n\n列表更新时间：" + datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z") + "\n\n", 1)
        api(f"{PREFIX}/issues/{number}", "PATCH", {"body": stamped})
        self.state.update(hub_fingerprint=fingerprint, hub_body_hash=hashlib.sha256(body.encode()).hexdigest())
        self.state["hub_list_count"] = len(self.records)
        self.state["hub_updated_at"] = datetime.now().astimezone().isoformat()
        self.save()

    def room_body(self, sid: str) -> str:
        record = self.records[sid]
        history = f"https://github.com/{REPO}/blob/main/sessions/{sid}/page-001.md"
        with contextlib.closing(db(self.home / "thread_history_1.sqlite")) as connection:
            messages = history_messages(connection, sid)[-4:]
        preview = "\n\n".join(f"**{item['role']}**\n\n{item['text'][:1800]}" for item in messages)
        return f"<!-- codex2github:bind {sid} -->\n# {record['display_title']}\n\n**在下面的评论框直接发消息，即可继续这条本机会话。**\n\n会话 ID：`{sid}`\n\n[查看完整历史]({history})\n\n{HELP}\n\n## 连接前最近对话\n\n{preview}"

    def ensure_room(self, sid: str) -> int:
        existing = self.state["rooms"].get(sid)
        if existing:
            return int(existing)
        if not is_private_task_repository(api(PREFIX)):
            raise RuntimeError("Only a private task repository, not a source template, can host local conversations")
        result = api(f"{PREFIX}/issues", "POST", {"title": "对话：" + self.records[sid]["display_title"][:100], "body": self.room_body(sid), "labels": ["codex-session"]})
        number = result["number"]
        self.state["rooms"][sid] = number
        self.state["issues"][str(number)] = {"sid": sid, "cursor": 0, "queue": [], "body_done": True, "count": 0}
        self.save()
        return number

    def ensure_history_link(self, issue: dict):
        """Decorate a consumed new-task body without changing its task text."""
        number = issue["number"]
        info = self.state["issues"].get(str(number), {})
        sid = info.get("sid")
        if (not sid or not info.get("new_task") or not info.get("body_done")
                or number == self.state.get("hub")):
            return
        start = "<!-- codex2github:history:start -->"
        end = "<!-- codex2github:history:end -->"
        history = f"https://github.com/{REPO}/blob/main/sessions/{sid}/page-001.md"
        panel = (f"{start}\n<!-- codex2github:bind {sid} -->\n"
                 f"**[查看完整会话记录]({history})**\n\n"
                 "首次同步完成后链接可用；后续对话会自动更新到记录中。\n"
                 f"{end}")
        if panel in (issue.get("body") or ""):
            return
        # Read again immediately before editing to retain intervening user edits.
        current = api(f"{PREFIX}/issues/{number}")
        body = current.get("body") or ""
        pattern = re.escape(start) + r"[\s\S]*?" + re.escape(end)
        if re.search(pattern, body):
            updated = re.sub(pattern, lambda match: panel, body, count=1)
        else:
            updated = body + ("\n\n" if body else "") + panel
        if updated != body:
            api(f"{PREFIX}/issues/{number}", "PATCH", {"body": updated})
        issue["body"] = updated

    def setup(self):
        metadata = api(PREFIX)
        if not is_private_task_repository(metadata):
            raise RuntimeError("Only a private task repository, not a source template, can host local conversations")
        labels = {item["name"] for item in page_list(f"{PREFIX}/labels")}
        if "codex-session" not in labels:
            api(f"{PREFIX}/labels", "POST", {"name": "codex-session", "color": "2F5597", "description": "与本机已有 Codex 会话直接对话"})
        if not self.state.get("hub"):
            lines = []
            main = [(sid, row) for sid, row in self.records.items() if not is_subagent(row)]
            for sid, row in main[:12]:
                lines.append(f"- {row['display_title']}\n  ```\n  /session {sid[:12]}\n  ```")
            body = "<!-- codex2github:hub -->\n# 本机 Codex 对话入口\n\n在评论框发送 `/session 会话名或ID`，连接后继续在评论框直接聊天。也可以点会话页面上的“进入对话”，为那条会话建立固定聊天页。连接会沿用本机原会话的历史。\n\n" + HELP + "\n\n## 最近主会话\n\n" + "\n".join(lines)
            hub = api(f"{PREFIX}/issues", "POST", {"title": "Codex 远程对话入口：选择会话、切换模型", "body": body, "labels": ["codex-session"]})
            self.state["hub"] = hub["number"]
            self.state["issues"][str(hub["number"])] = {"cursor": 0, "queue": [], "body_done": True, "count": 0}
        # Existing task conversations become usable directly, whether open/closed.
        for path in (BASE / "sessions.json",):
            for number, sid in read_json(path, {}).items():
                if sid in self.records:
                    self.state["issues"].setdefault(number, {"sid": sid, "queue": [], "legacy": True})
                    self.state["rooms"].setdefault(sid, int(number))
        self.save()

    def handle(self, number: int, text: str, comment_id: int):
        info = self.state["issues"][str(number)]
        if str(comment_id) in info.get("canceled_messages", {}):
            return  # A withdrawn message cannot reappear during cursor recovery.
        parsed = command(text)
        message = text.strip()
        if parsed:
            verb, value, message = parsed
            if verb in {"steer", "queue"}:
                message = "\n".join(part for part in (value, message) if part).strip()
                if not message:
                    if verb == "queue":
                        self.reply(number, self.queue_listing(number))
                        return
                    raise ValueError(f"请使用 `/{verb} 内容`，或在指令下一行填写消息。")
                if verb == "steer":
                    self.steer(number, message, comment_id)
                    return
                if str(comment_id) in info.get("queue_commands", {}):
                    return
            elif verb == "cancel":
                if message:
                    raise ValueError("请单独发送 `/cancel`、`/cancel 消息ID` 或 `/cancel all`，不要附加其他内容。")
                self.cancel_queue(number, value, comment_id)
                return
            elif verb == "session":
                try:
                    sid = resolve(value, self.records)
                except ValueError:
                    self.records = self.load_records(force=True)
                    sid = resolve(value, self.records)
                info["sid"] = sid
                room = self.ensure_room(sid)
                self.save()
                self.reply(number, f"已连接本机原会话：**{self.records[sid]['display_title']}**。\n\n模型：`{self.current_model(info)}`\n\n[进入这条会话的固定聊天页](https://github.com/{REPO}/issues/{room})。也可以继续在这里留言。")
            elif verb == "model":
                if not value:
                    selected = self.current_model(info)
                    self.reply(number, f"当前模型：`{selected}`；思考档位：`{self.current_effort(info)}`。\n\n可选档位：{self.effort_options(selected)}\n\n使用 `/model 模型ID [档位]` 或 `/effort 档位` 切换。"); return
                parts = value.split()
                if len(parts) > 2:
                    raise ValueError("使用 `/model 模型ID [思考档位]`，例如 `/model gpt-6.1-sol xhigh`。")
                selected = parts[0]
                self.sync_route()
                self.catalog = model_catalog(self.home, refresh=True)
                self.sync_route()
                if selected not in self.catalog:
                    route = current_route_model(self.home) or "auto"
                    raise ValueError(f"当前路由中没有读到模型 `{selected}`；本条保持当前路由模型 `{route}`。请先由你在 CC Switch 中调整路由，再重试 `/model {selected}`。")
                effort = validate_effort(self.home, selected, parts[1]) if len(parts) == 2 else "auto" if selected != self.current_model(info) else None
                if info.get("sid"):
                    self.state["models"][info["sid"]] = selected
                    info.pop("model", None)
                else:
                    info["model"] = selected
                if effort is not None:
                    self.set_effort(info, effort)
                self.save()
                guidance = "\n\n直接在这个 Issue 留言即可开始新会话，无需添加标签。" if info.get("new_task") and not info.get("sid") else ""
                self.reply(number, f"当前会话模型已设为 `{selected}`，思考档位：`{self.current_effort(info)}`。只影响这个 Issue 绑定的会话，下一条留言生效。不会修改 Codex 或 CC Switch 配置。正在运行的一轮会正常完成。{guidance}")
            elif verb in {"effort", "reasoning"}:
                selected = self.current_model(info)
                if not value:
                    self.reply(number, f"当前模型：`{selected}`；思考档位：`{self.current_effort(info)}`。\n\n可选档位：{self.effort_options(selected)}\n\n使用 `/effort 档位` 切换；`/effort auto` 恢复本机默认。"); return
                self.set_effort(info, validate_effort(self.home, selected, value))
                self.save()
                self.reply(number, f"当前对话思考档位：`{self.current_effort(info)}`。只影响这条对话的后续任务；当前正在运行的一轮继续完成，本机配置保持只读。")
            elif verb == "models":
                self.sync_route()
                self.catalog = model_catalog(self.home, refresh=True)
                self.sync_route()
                capabilities = model_efforts_many(self.home, self.catalog)
                self.reply(number, f"当前路由工作模型（共 {len(self.catalog)} 个）：\n\n"
                           f"当前对话：`{self.current_model(info)}`；思考档位：`{self.current_effort(info)}`；路由默认：`{current_route_model(self.home) or 'auto'}`。\n\n"
                           + "| 模型 | 思考档位 |\n|---|---|\n"
                           + "\n".join(f"| `{name}` | " + ((" / ".join(capabilities[name]["levels"]) or "不可调整") if capabilities[name]["verified"] else "目录未标注（执行时校验）") + " |" for name in self.catalog)
                           + "\n\n使用 `/model 模型ID [档位]` 切换模型；`/effort 档位` 只调整当前对话的思考档位。"); return
            elif verb == "status":
                sid = info.get("sid")
                latest = list(info.get("steers", {}).values())[-1:]
                steering = ""
                if latest:
                    receipt = latest[0]
                    status = steer_visual.status(receipt)[0]
                    steering = f"\n\n最近一次引导：{status}"
                self.reply(number, f"会话：{self.records.get(sid, {}).get('display_title', '尚未连接')}\n\nID：`{sid or '未选择'}`\n\n模型：`{self.current_model(info)}`\n\n思考档位：`{self.current_effort(info)}`\n\n排队消息：{len(self.pending_queue(number))}{steering}\n\n`/effort 档位` 调整当前对话；`/steer 内容` 引导当前任务；`/queue 内容` 排队，`/queue` 查看队列；`/cancel` 撤回最近一条，`/cancel 消息ID` 撤回指定消息，`/cancel all` 撤回全部等待消息。"); return
            elif verb == "help":
                self.reply(number, HELP); return
            elif verb == "close":
                if value or message:
                    raise ValueError("关闭当前任务请单独发送 `/close`，不要附加其他消息。")
                api(f"{PREFIX}/issues/{number}", "PATCH", {"state": "closed"})
                self.reply(number, "已按你的指令关闭当前任务。")
                return
        if not message:
            return
        if not info.get("sid") and not info.get("new_task"):
            raise ValueError("先发送 `/session 会话名或ID` 连接你的本机会话，然后直接留言。")
        info["queue"].append({"comment": comment_id, "text": message, "sid": info.get("sid"), "model": self.current_model(info), "effort": self.current_effort(info)})
        if parsed and parsed[0] == "queue":
            info.setdefault("queue_commands", {})[str(comment_id)] = True
        self.save()
        if parsed and parsed[0] == "queue":
            self.reply(number, f"已排队（当前 {len(self.pending_queue(number))} 条），消息 ID：`{comment_id}`。当前回复结束后按顺序执行；可用 `/cancel {comment_id}` 撤回，`/queue` 查看列表。")

    def pending_queue(self, number: int) -> list[dict]:
        # A crash between launch/save and dequeue can leave the running request
        # in the queue. Never present or remove that request as waiting input.
        active = self.state["jobs"].get(str(number), {}).get("comment")
        return [request for request in self.state["issues"][str(number)]["queue"]
                if active is None or request["comment"] != active]

    def queue_listing(self, number: int) -> str:
        pending = self.pending_queue(number)
        if not pending:
            return "当前 Issue 没有待处理的排队消息。正在执行的任务继续运行。"
        text = f"当前 Issue 有 {len(pending)} 条排队消息，按执行顺序列出：\n\n| 消息 ID | 内容摘要 | 撤回指令 |\n|---|---|---|\n"
        for request in pending[:50]:
            comment = request["comment"]
            summary = label(re.sub(r"\s+", " ", redact(request["text"])).strip()[:100])
            url = f"https://github.com/{REPO}/issues/{number}" + (f"#issuecomment-{comment}" if comment else "")
            text += f"| [`{comment}`]({url}) | {summary} | `/cancel {comment}` |\n"
        if len(pending) > 50:
            text += f"\n本次显示前 50 条，还有 {len(pending) - 50} 条；撤回部分后可再次查看，或用 `/cancel all` 撤回全部等待消息。\n"
        return text + "\n`/cancel` 撤回最近一条；`/cancel all` 撤回全部等待消息。这里只管理当前 Issue 的队列。"

    def cancel_queue(self, number: int, selector: str, comment_id: int):
        info = self.state["issues"][str(number)]
        receipts = info.setdefault("queue_cancellations", {})
        if str(comment_id) in receipts:
            return  # Replaying '/cancel' must not withdraw another newer message.
        selector = selector.strip().lower() or "last"
        if selector not in {"last", "all"} and not re.fullmatch(r"[0-9]+", selector):
            raise ValueError("请使用 `/cancel`、`/cancel 消息ID` 或 `/cancel all`；`/queue` 查看可撤回的消息 ID。")
        pending = self.pending_queue(number)
        if selector == "last":
            removed = pending[-1:]
        elif selector == "all":
            removed = pending
        else:
            removed = [request for request in pending if request["comment"] == int(selector)]
        ids = {request["comment"] for request in removed}
        receipts[str(comment_id)] = {"removed": sorted(ids), "selector": selector}
        info["queue"] = [request for request in info["queue"] if request["comment"] not in ids]
        for request in removed:
            key = str(request["comment"])
            info.setdefault("canceled_messages", {})[key] = {"by": comment_id}
            if key in info.get("steers", {}):
                info["steers"][key].update(canceled=True, queued=False, pending=False)
        self.save()
        for request in removed:
            if str(request["comment"]) in info.get("steers", {}):
                self.publish_steer_card(number, request["comment"])
        if removed:
            removed_ids = "、".join(f"`{value}`" for value in sorted(ids)[:20])
            if len(ids) > 20:
                removed_ids += " 等"
            self.reply(number, f"已撤回 {len(removed)} 条排队消息（{removed_ids}），剩余 {len(self.pending_queue(number))} 条。正在执行的任务继续运行，Issue 保持原状态。")
        elif selector not in {"last", "all"} and self.state["jobs"].get(str(number), {}).get("comment") == int(selector):
            self.reply(number, "这条消息已经开始执行，无法通过 `/cancel` 撤回。当前任务继续运行；可用 `/steer 内容` 引导。")
        elif selector not in {"last", "all"}:
            self.reply(number, "这个 ID 不在当前 Issue 的等待队列中，可能已执行或撤回。用 `/queue` 查看可撤回的消息；当前任务继续运行。")
        else:
            self.reply(number, "当前 Issue 没有待处理的排队消息，无需撤回。正在执行的任务继续运行。")

    def steer(self, number: int, message: str, comment_id: int):
        info = self.state["issues"][str(number)]
        previous = info.setdefault("steers", {}).get(str(comment_id))
        if previous:
            self.publish_steer_card(number, comment_id)
            return  # Recovery before saving the comment cursor cannot inject twice.
        info["steers"][str(comment_id)] = {"sid": info.get("sid"), "text": message,
                                          "observed_at": steer_visual.now(), "pending": True}
        self.save()
        self.publish_steer_card(number, comment_id)
        try:
            self.dispatch_steer(number, message, comment_id)
        except ValueError as error:
            self.steer_receipt(number, comment_id, {"ok": False, "blocked": True, "error": str(error)})
            raise

    def dispatch_steer(self, number: int, message: str, comment_id: int):
        info = self.state["issues"][str(number)]
        sid = info.get("sid")
        job = next((job for key, job in self.state["jobs"].items()
                    if (sid and job.get("sid") == sid) or (not sid and key == str(number))), None)
        sid = sid or (job or {}).get("sid")
        if job and not sid and job.get("transport") == "appserver":
            runtime = read_json(Path(job["directory"]) / "runtime.json", {})
            sid = runtime.get("sid")
            if sid and re.fullmatch(r"[0-9a-f-]{36}", sid):
                info["sid"] = job["sid"] = sid
                self.state["rooms"].setdefault(sid, number)
                self.records = self.load_records(force=True)
                self.save()
            else:
                sid = None
        if not sid:
            if job:
                raise ValueError("任务尚在启动，原会话 ID 还未生成；请稍后重发 `/steer 内容`，或用 `/queue 内容` 排队。")
            if not info.get("new_task"):
                raise ValueError("先用 `/session 会话名或ID` 连接原会话。")
            return self.queue_idle_steer(number, message, comment_id)
        prompt = message
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://github.com/{REPO}/issues/{number}#issuecomment-{comment_id}"))
        receipt = info["steers"][str(comment_id)]
        receipt.update(sid=sid, message_id=message_id, pending=True)
        native_submission = False
        if job:
            directory = Path(job["directory"])
            result = read_json(directory / "completion.json", {}) or read_json(directory / "run-result.json", {})
            native_submission = bool(result.get("queued") or job.get("transport") == "codex-queue")
        if job and job.get("transport") == "appserver" and not native_submission:
            directory = Path(job["directory"])
            runtime = read_json(directory / "runtime.json", {})
            if not runtime.get("active"):
                if not (directory / "run-result.json").exists():
                    raise ValueError("任务正在启动或收尾，尚无可引导的执行轮次；请稍后重发 `/steer 内容`，或用 `/queue 内容`。")
                return self.queue_idle_steer(number, message, comment_id)
            receipt.update(turn_id=runtime["turn_id"], directory=str(directory))
            self.save()
            control = {"comment": comment_id, "sid": sid, "turn_id": runtime["turn_id"],
                       "text": prompt, "message_id": message_id}
            write_if_changed(directory / "control-inbox" / f"{comment_id}.json", json.dumps(control, ensure_ascii=False))
            receipt["submitted_at"] = steer_visual.now()
            self.save()
            self.publish_steer_card(number, comment_id)
            deadline = time.monotonic() + 2
            response = {}
            while time.monotonic() < deadline:
                response = read_json(directory / "control-outbox" / f"{comment_id}.json", {})
                if response and not response.get("pending"):
                    break
                time.sleep(.2)
            if not response or response.get("pending"):
                return
        else:
            current = active_turn(self.home, sid, self.records.get(sid))
            if current["status"] == "idle":
                return self.queue_idle_steer(number, message, comment_id)
            if current["status"] != "active":
                raise ValueError("无法确认原会话的活动轮次；引导未提交、未自动排队。请稍后使用 `/steer 内容`，需要下一轮处理时用 `/queue 内容`。")
            receipt.update(turn_id=current["turn_id"], submitted_at=steer_visual.now(), transport="native-steer", uncertain=True)
            # Persist before sending: a lost response or process crash cannot
            # cause the same GitHub comment to be submitted a second time.
            self.save()
            self.publish_steer_card(number, comment_id)
            response = steer_existing_thread(sid, prompt, message_id, current["turn_id"])
        self.steer_receipt(number, comment_id, response)

    def queue_idle_steer(self, number: int, message: str, comment_id: int):
        info = self.state["issues"][str(number)]
        info["queue"].append({"comment": comment_id, "text": message, "sid": info.get("sid"), "model": self.current_model(info), "effort": self.current_effort(info)})
        receipt = info.setdefault("steers", {}).setdefault(str(comment_id), {})
        receipt.update(sid=info.get("sid"), text=message, queued=True, pending=False)
        self.save()
        self.publish_steer_card(number, comment_id)

    def steer_receipt(self, number: int, comment_id: int, response: dict):
        receipt = self.state["issues"][str(number)]["steers"][str(comment_id)]
        receipt.update(response, pending=False, uncertain=bool(response.get("uncertain")))
        if response.get("ok"):
            receipt.pop("error", None)
            receipt["accepted_at"] = response.get("accepted_at") or receipt.get("accepted_at") or steer_visual.now()
        self.save()
        self.publish_steer_card(number, comment_id)

    def reconcile_steers(self):
        for number, info in self.state["issues"].items():
            for comment, receipt in info.get("steers", {}).items():
                if receipt.get("pending") and receipt.get("directory"):
                    result = read_json(Path(receipt["directory"]) / "control-outbox" / f"{comment}.json", {})
                    if result and not result.get("pending"):
                        self.steer_receipt(int(number), int(comment), result)
                if (receipt.get("pending") or receipt.get("uncertain")) and receipt.get("sid") and receipt.get("message_id"):
                    # A timed-out request may still have reached the same original
                    # thread. Confirm its exact client ID without injecting again.
                    try:
                        with contextlib.closing(db(self.home / "thread_history_1.sqlite")) as connection:
                            confirmed = connection.execute(
                                "SELECT turn_id FROM thread_items WHERE thread_id=? AND item_type='userMessage' "
                                "AND json_extract(item_json,'$.clientId')=? ORDER BY rollout_ordinal DESC LIMIT 1",
                                (receipt["sid"], receipt["message_id"]),
                            ).fetchone()
                        if confirmed:
                            self.steer_receipt(int(number), int(comment), {"ok": True, "turn_id": confirmed[0], "confirmed_from_history": True})
                    except sqlite3.Error:
                        pass  # The worker response remains authoritative.
                if receipt.get("pending") and not receipt.get("uncertain"):
                    timestamp = receipt.get("submitted_at") or receipt.get("observed_at")
                    if timestamp:
                        try:
                            elapsed = datetime.now().astimezone().timestamp() - datetime.fromisoformat(timestamp).timestamp()
                            if elapsed >= 30:
                                receipt.update(uncertain=True, error="接收回执暂未返回，监听器会继续核对；请避免重复提交")
                                self.save()
                        except ValueError:
                            pass
                if not receipt.get("visual_version") and not receipt.get("text"):
                    try:
                        original = api(f"{PREFIX}/issues/comments/{comment}") if int(comment) else {}
                        parsed = command(original.get("body", ""))
                        if parsed and parsed[0] == "steer":
                            receipt["text"] = "\n".join(part for part in parsed[1:] if part)
                        receipt["visual_version"] = steer_visual.VERSION
                        self.save()
                    except (OSError, RuntimeError, subprocess.TimeoutExpired):
                        pass
                if receipt.get("queued"):
                    request = next((request for request in info["queue"] if request["comment"] == int(comment)), None)
                    job = self.state["jobs"].get(number, {})
                    if not request and job.get("comment") == int(comment):
                        receipt.update(queued=False, started=True, sid=job.get("sid"), turn_id=job.get("turn_id"))
                        self.save()
                if receipt.get("started"):
                    job = self.state["jobs"].get(number, {})
                    if job.get("comment") == int(comment):
                        runtime = read_json(Path(job["directory"]) / "runtime.json", {}) if job.get("directory") else {}
                        receipt.update(sid=runtime.get("sid") or job.get("sid"), turn_id=runtime.get("turn_id") or job.get("turn_id"))
                        self.save()
                self.publish_steer_card(int(number), int(comment))

    def note_queued_steer_started(self, number: int, job: dict):
        receipt = self.state["issues"][str(number)].get("steers", {}).get(str(job["comment"]))
        if receipt and receipt.get("queued"):
            receipt.update(queued=False, started=True, pending=False, sid=job.get("sid"), turn_id=job.get("turn_id"))
            self.save()
            self.publish_steer_card(number, job["comment"])

    def busy(self, number: int, sid: str | None) -> bool:
        if sid and any(job.get("sid") == sid for job in self.state["jobs"].values()):
            return True
        for issue_number, entry in self.state["issues"].items():
            marker = read_json(BASE / f"running-issue-{issue_number}.json", {})
            if marker.get("pid") and process_alive(int(marker["pid"])):
                target = marker.get("sid") or self.state["jobs"].get(issue_number, {}).get("sid") or entry.get("sid")
                if int(issue_number) == number or (sid and target == sid):
                    return True
        if sid:
            with contextlib.closing(db(self.home / "thread_history_1.sqlite")) as connection:
                row = connection.execute("SELECT status,started_at FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1", (sid,)).fetchone()
            if row and row[0] == "inProgress" and row[1] and time.time() - row[1] < 3600:
                return True
        return False

    def recover_queued_results(self):
        """Recover old submission-only artifacts without sending any task again."""
        if self.state.get("queue_result_recovery") == 1:
            return
        deferred = False
        for path in (self.path.parent / "jobs").glob("*/completion.json"):
            result = read_json(path, {})
            if not result.get("queued") or result.get("execution_complete") and result.get("replied"):
                continue
            match = re.fullmatch(r"(\d+)-(\d+)", path.parent.name)
            if not match or match[1] not in self.state["issues"]:
                continue
            number, comment = match[1], int(match[2])
            if number in self.state["jobs"]:
                if self.state["jobs"][number].get("comment") != comment:
                    deferred = True
                continue
            # Legacy CLI receipts used replied=True to mean submission only.
            result.setdefault("submitted_at_ms", int(path.stat().st_mtime * 1000))
            write_if_changed(path, json.dumps(result, ensure_ascii=False))
            self.state["jobs"][number] = {
                "pid": 0, "sid": result.get("sid"), "model": result.get("model", "auto"),
                "directory": str(path.parent), "comment": comment, "transport": "codex-queue",
            }
        if not deferred:
            self.state["queue_result_recovery"] = 1
        self.save()

    def publish_queued_result(self, number: str, job: dict, result: dict):
        directory = Path(job["directory"])
        marker = f"<!-- codex2github:result {job['comment']} -->"
        effort = result.get("effort") or job.get("effort")
        detail = f" · 思考档位 `{effort}`" if effort and effort != "auto" else ""
        text = marker + f"\n{'✅' if result.get('ok') else '❌'} {result.get('model') or job['model']}{detail}\n\n" + redact(result["final"])[:50000]
        reply_id = result.get("final_reply_id")
        if not reply_id and result.get("result_post_attempted"):
            existing = next((row for row in page_list(f"{PREFIX}/issues/{number}/comments")
                             if (row.get("body") or "").startswith(BOT + "\n" + marker)), None)
            reply_id = existing["id"] if existing else None
        result["result_post_attempted"] = True
        write_if_changed(directory / "completion.json", json.dumps(result, ensure_ascii=False))
        if reply_id:
            api(f"{PREFIX}/issues/comments/{reply_id}", "PATCH", {"body": BOT + "\n" + text})
        else:
            response = self.reply(int(number), text)
            reply_id = response.get("id") if isinstance(response, dict) else None
        result.update(replied=True, reply_id=reply_id, final_reply_id=reply_id)
        write_if_changed(directory / "completion.json", json.dumps(result, ensure_ascii=False))

    def reap(self):
        for number, job in list(self.state["jobs"].items()):
            result = read_json(Path(job["directory"]) / "completion.json", {})
            if not result and process_alive(job["pid"]):
                continue
            info = self.state["issues"][number]
            if result.get("queued"):
                job["transport"] = "codex-queue"
                if "execution_complete" not in result:
                    result.update(submission_reply_id=result.get("reply_id"), replied=False, execution_complete=False)
                if not result.get("execution_complete"):
                    prompt = job.get("request", {}).get("text")
                    if prompt is None:
                        prompt = (Path(job["directory"]) / "prompt.md").read_text(encoding="utf-8-sig")
                    progress = follow_queued_result(job, result, self.records.get(job.get("sid"), {}), prompt)
                    if not progress:
                        self.save()
                        continue
                    result.update(progress)
                    write_if_changed(Path(job["directory"]) / "final.md", result["final"])
                    write_if_changed(Path(job["directory"]) / "completion.json", json.dumps(result, ensure_ascii=False))
                if not result.get("replied"):
                    self.publish_queued_result(number, job, result)
            if result.get("sid") and not info.get("sid"):
                info["sid"] = result["sid"]
                self.state["rooms"].setdefault(result["sid"], int(number))
            if result.get("retryable"):
                request = job.get("request") or next(
                    (item for item in info["queue"] if item["comment"] == job["comment"]), None)
                if request:
                    info["queue"] = [request] + [
                        item for item in info["queue"] if item["comment"] != job["comment"]
                    ]
                    notices = info.setdefault("runtime_notices", {})
                    if str(job["comment"]) not in notices:
                        self.reply(int(number), "当前本机会话正在由 Codex 桌面端执行，这条远程留言已自动等待；桌面端释放会话后将继续执行。")
                        notices[str(job["comment"])] = "thread-writer-busy"
                else:
                    self.reply(int(number), "远程任务遇到本机会话写入冲突，且原留言未能恢复到队列；请重新发送原留言。")
            elif not result:
                self.reply(int(number), "监听到执行进程退出，未找到完成记录。这条消息不会重复执行；请重发消息。")
            elif not result.get("replied"):
                final_path = Path(job["directory"]) / "final.md"
                final = final_path.read_text(encoding="utf-8-sig") if final_path.exists() else result.get("error", "未找到最终回复。")
                actual_model = result.get("model") or job["model"]
                notice = "\n\n⚠️ 请求的模型未在当前路由中可用，已重新读取并使用当前路由模型；请你自行调整 CC Switch 路由。" if result.get("model_fallback") else ""
                self.reply(int(number), f"{'✅' if result.get('ok') else '❌'} {actual_model}{notice}\n\n{redact(final)[:50000]}")
            # Recover a crash between saving a launched job and dequeuing it.
            if not result.get("retryable"):
                info["queue"] = [request for request in info["queue"] if request["comment"] != job["comment"]]
            del self.state["jobs"][number]
            self.save()

    def launch(self, number: int, request: dict):
        info = self.state["issues"][str(number)]
        sid = request["sid"] or info.get("sid")
        directory = BASE / "jobs" / f"{number}-{request['comment']}"
        directory.mkdir(parents=True, exist_ok=True)
        cwd = self.records.get(sid, {}).get("cwd") or str(WORKSPACE)
        if not Path(cwd).is_dir():
            cwd = str(WORKSPACE)
        # Forward the user's text byte-for-byte after normal file decoding.
        prompt = request["text"]
        prompt_path = directory / "prompt.md"
        write_if_changed(prompt_path, prompt)
        arguments = [sys.executable, "-m", "codex2github.worker", "--issue", str(number), "--prompt-file", str(prompt_path), "--model", request["model"], "--cwd", cwd, "--job-directory", str(directory)]
        arguments += ["--effort", request.get("effort") or self.current_effort(info)]
        if sid:
            arguments += ["--sid", sid]
        with (directory / "worker.log").open("wb") as log:
            process = subprocess.Popen(arguments, cwd=WORKSPACE, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        job = {"pid": process.pid, "sid": sid, "model": request["model"], "effort": request.get("effort") or self.current_effort(info), "directory": str(directory),
               "comment": request["comment"], "request": request, "transport": "appserver"}
        self.state["jobs"][str(number)] = job
        write_if_changed(BASE / f"running-issue-{number}.json", json.dumps(job))
        self.save()
        self.note_queued_steer_started(number, job)

    def tick(self):
        # A task repository can change visibility after setup. Check before any
        # receipt, result, or command processing writes to GitHub.
        metadata = api(PREFIX)
        if not is_private_task_repository(metadata):
            raise RuntimeError("Remote chat repository must remain private and must not be a source template")
        self.sync_route()
        self.records = self.load_records()
        self.discover_job_sessions()
        self.reconcile_steers()
        self.recover_queued_results()
        self.reap()
        actor = metadata["owner"]["login"]
        issues = page_list(f"{PREFIX}/issues?state=all")
        for issue in issues:
            if "pull_request" in issue:
                continue
            number = issue["number"]
            labels = {item["name"] for item in issue["labels"]}
            # This private repository is the user's dedicated task inbox.
            # Native GitHub New Issue doesn't apply labels automatically.
            # Accept the owner's new open issues; don't revive old unbound ones.
            if str(number) not in self.state["issues"] and (issue.get("user", {}).get("login") != actor or issue.get("state") != "open"):
                continue
            info = self.state["issues"].setdefault(str(number), {"cursor": 0, "queue": [], "count": -1})
            # Labels do not select an existing chat. Only a binding/session
            # command does; the dedicated hub keeps its existing-chat role.
            if (not info.get("sid") and number != self.state.get("hub")
                    and issue.get("user", {}).get("login") == actor and issue.get("state") == "open"
                    and not info.get("new_task")):
                info["new_task"] = True
                self.save()
            if info.get("title") != issue["title"]:
                info["title"] = issue["title"]
                self.save()
            if not info.get("body_done") and not info.get("legacy"):
                body = issue.get("body") or ""
                queued_before = len(info["queue"])
                binding = re.search(r"<!-- codex2github:bind ([0-9a-f-]{36}) -->", body)
                if binding:
                    info["sid"] = resolve(binding[1], self.records)
                    info.pop("new_task", None)
                    self.state["rooms"].setdefault(info["sid"], number)
                else:
                    try:
                        self.handle(number, body, 0)
                    except ValueError as error:
                        self.reply(number, str(error))
                info["body_done"] = True
                self.save()
                if info.get("new_task") and len(info["queue"]) > queued_before:
                    self.reply(number, "已收到任务，正在排队执行。完成后会在这里回复，任务保持打开。")
                elif info.get("new_task") and not body.strip():
                    self.reply(number, "新会话入口已准备好。请直接在评论框发送任务；`/model 完整模型ID` 可以先选择模型。无需添加标签。")
            if info.get("count") != issue["comments"]:
                comments = page_list(f"{PREFIX}/issues/{number}/comments")
                if "cursor" not in info:  # Migration: don't replay old conversation.
                    info["cursor"] = max((item["id"] for item in comments), default=0)
                    info["body_done"] = True
                for item in comments:
                    if item["id"] <= info["cursor"]:
                        continue
                    body = item.get("body") or ""
                    if item["user"]["login"] == actor and not body.startswith((BOT, "✅", "❌")):
                        try:
                            self.handle(number, body, item["id"])
                        except ValueError as error:
                            self.reply(number, str(error))
                    info["cursor"] = item["id"]
                    self.save()
                info["count"] = issue["comments"]
                self.save()
            try:
                self.ensure_history_link(issue)
            except Exception as error:
                # Presentation failures must not block task delivery or completion.
                print(f"History link #{number} will retry: {redact(str(error))[:300]}", flush=True)
        # All comments are consumed in order, including model/session commands
        # while a generation is busy. Each queued message captures its target/model.
        for number, info in self.state["issues"].items():
            if str(number) in self.state["jobs"] or not info["queue"]:
                continue
            request = info["queue"][0]
            sid = request["sid"] or info.get("sid")
            if self.busy(int(number), sid):
                continue
            self.launch(int(number), request)
            info["queue"].pop(0)
            self.save()
        hub = next((issue for issue in issues if issue["number"] == self.state.get("hub")), None)
        self.refresh_hub(hub)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--tick", action="store_true")
    parser.add_argument("--open")
    args = parser.parse_args(argv)
    SETTINGS.require_configured()
    BASE.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    with singleton(BASE / "router.lock"):
        if args.open and not is_private_task_repository(api(PREFIX)):
            raise RuntimeError("Only a private task repository, not a source template, can host local conversations")
        router = Router()
        if args.setup:
            router.setup()
            print(f"https://github.com/{REPO}/issues/{router.state['hub']}")
        if args.open:
            sid = resolve(args.open, router.records)
            number = router.ensure_room(sid)
            print(f"https://github.com/{REPO}/issues/{number}")
        if args.tick:
            router.tick()
            print(json.dumps({"ok": True, "rooms": len(router.state["rooms"]), "queued": sum(len(i["queue"]) for i in router.state["issues"].values()), "running": len(router.state["jobs"])}, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
