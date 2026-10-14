"""Mirror visible Codex conversations to an existing private GitHub repository.

Codex state/history databases are opened read-only. All generated data, Git state,
dependencies and logs stay next to this script inside the remote workspace.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
from difflib import SequenceMatcher
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from .config import SETTINGS, current_route_model, is_private_task_repository

BASE = SETTINGS.runtime_dir
PAGE_BYTES = 180_000
SCHEMA = 4
REPO = SETTINGS.repo


def chat_link(thread_id: str, title: str, rooms: dict | None = None) -> str:
    number = (rooms or {}).get(thread_id)
    if number:
        return f"https://github.com/{REPO}/issues/{number}"
    body = f"<!-- codex2github:bind {thread_id} -->\n继续本机原会话：{title}\n\n直接在下方评论框发送消息。切换模型：/model 完整模型ID [档位]；可用模型和档位：/models；调整思考档位：/effort xhigh。"
    return f"https://github.com/{REPO}/issues/new?" + urlencode({"labels": "codex-session", "title": "对话：" + title[:100], "body": body})


def redact(text: str) -> str:
    # Never export tools, internal reasoning, credentials or system instructions.
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})\b", "[凭据已隐藏]", text)
    text = re.sub(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1[凭据已隐藏]", text)
    text = re.sub(r"(?im)((?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|password|密码|密钥)\s*[:=]\s*['\"]?)[^\s'\";,]{8,}", r"\1[凭据已隐藏]", text)
    return text


def visible_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    # Rules/context can be prepended to the real request in a single message.
    text = re.sub(r"(?s)^# AGENTS\.md instructions\s*<INSTRUCTIONS>.*?</INSTRUCTIONS>\s*", "", text)
    text = re.sub(r"(?s)<environment_context>.*?</environment_context>\s*", "", text)
    if text.startswith(("# AGENTS.md", "<INSTRUCTIONS>", "<permissions instructions>", "<turn_aborted>")):
        return ""
    text = re.sub(r"(?s)<oai-mem-citation>.*?</oai-mem-citation>", "", text)
    return redact(text).strip()


def db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def write_if_changed(path: Path, text: str) -> bool:
    data = text.encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    return True


def stamp(milliseconds: int | None) -> str:
    if not milliseconds:
        return "时间未记录"
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def title_of(thread: dict, names: dict) -> str:
    title = names.get(thread["id"]) or thread.get("name") or thread.get("title") or thread["id"]
    title = re.sub(r"\s+", " ", redact(title)).strip()
    return title[:110] + ("…" if len(title) > 110 else "")


def label(text: str) -> str:
    return re.sub(r"([\\`*\[\]|<>])", r"\\\1", text).replace("\n", " ")


def is_subagent(thread: dict) -> bool:
    """Use explicit origin metadata; an optional agent path is not required."""
    if thread.get("agent_path"):
        return True  # Legacy records may have only this metadata.
    for key in ("thread_source", "source"):
        source = thread.get(key)
        if isinstance(source, str):
            if source.strip().casefold() == "subagent":
                return True
            try:
                source = json.loads(source)
            except ValueError:
                continue
        if isinstance(source, dict) and "subagent" in source:
            return True
    return False


def history_messages(connection: sqlite3.Connection | None, thread_id: str) -> list[dict]:
    if connection is None:
        return []
    rows = connection.execute(
        "SELECT item_json,created_at_ms FROM thread_items WHERE thread_id=? "
        "AND item_type IN ('userMessage','agentMessage') ORDER BY rollout_ordinal,created_at_ms,item_id",
        (thread_id,),
    )
    messages = []
    for row in rows:
        item = json.loads(row["item_json"])
        if item["type"] == "userMessage":
            parts = []
            for part in item.get("content", []):
                if part.get("type") == "text":
                    parts.append(part.get("text", ""))
                elif part.get("type") in ("image", "localImage", "input_image"):
                    parts.append("[图片附件保留在本机]")
            text, role = "\n".join(parts), "用户"
        else:
            text = item.get("text", "")
            role = "Codex · 进度" if item.get("phase") == "commentary" else "Codex"
        text = visible_text(text)
        if text:
            messages.append({"role": role, "text": text, "time": stamp(row["created_at_ms"])})
    return messages


def item_text(item: dict) -> str:
    """Read only message content across native, RPC and legacy representations."""
    if isinstance(item.get("text"), str):
        return item["text"]
    content = item.get("content", [])
    if isinstance(content, str):
        return content
    parts = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type", "")).casefold()
        if kind in {"text", "input_text", "output_text"}:
            parts.append(part.get("text", ""))
        elif kind in {"image", "localimage", "input_image"}:
            parts.append("[图片附件保留在本机]")
    return "\n".join(parts)


def merge_messages(first: list[dict], second: list[dict]) -> list[dict]:
    """Align shared visible messages while preserving missing stretches in either source."""
    # Native item_completed events carry the phase even when response_item
    # messages omit channel. Match their shared text and prefer the richer row.
    keys = lambda rows: [("用户" if row["role"] == "用户" else "Codex", row["text"]) for row in rows]
    matcher = SequenceMatcher(None, keys(first), keys(second), autojunk=False)
    merged = []
    for operation, a, b, c, d in matcher.get_opcodes():
        if operation in {"equal", "insert"}:
            merged.extend(second[c:d])
        elif operation == "delete":
            merged.extend(first[a:b])
        else:
            merged.extend(first[a:b])
            merged.extend(second[c:d])
    return merged


def rollout_messages(path: Path) -> list[dict]:
    if not path.exists() and Path(str(path) + ".zst").exists():
        path = Path(str(path) + ".zst")
    if not path.exists():
        return []
    responses, events, native = [], [], []
    with contextlib.ExitStack() as stack:
        raw = stack.enter_context(path.open("rb"))
        if path.suffix == ".zst":
            import zstandard
            raw = stack.enter_context(zstandard.ZstdDecompressor().stream_reader(raw))
        stream = stack.enter_context(io.TextIOWrapper(raw, encoding="utf-8"))
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # An active rollout can end with an unfinished line.
            item = record.get("payload", {})
            role, text, destination = None, "", responses
            if record.get("type") == "response_item" and item.get("type") == "message":
                if item.get("role") == "user":
                    role = "用户"
                elif item.get("role") == "assistant" and item.get("channel") != "analysis":
                    role = "Codex · 进度" if item.get("channel") == "commentary" else "Codex"
                text = item_text(item)
            elif record.get("type") == "event_msg" and item.get("type") == "item_completed":
                message = item.get("item", {})
                kind = str(message.get("type", "")).casefold() if isinstance(message, dict) else ""
                if kind in {"usermessage", "agentmessage"}:
                    role = "用户" if kind == "usermessage" else "Codex · 进度" if message.get("phase") == "commentary" else "Codex"
                    text, destination = item_text(message), native
            elif record.get("type") == "event_msg" and item.get("type") in ("user_message", "agent_message"):
                role = "用户" if item["type"] == "user_message" else "Codex"
                text, destination = item.get("message", ""), events
            if role and (text := visible_text(text)):
                destination.append({"role": role, "text": text, "time": record.get("timestamp", "时间未记录")})
    return merge_messages(responses or events, native) if native else responses or events


def split_utf8(text: str, budget: int) -> list[str]:
    chunks, current, size = [], [], 0
    for line in text.splitlines(keepends=True):
        # A single very long line must also stay within the GitHub preview limit.
        for start in range(0, len(line), budget // 4):
            part = line[start:start + budget // 4]
            length = len(part.encode("utf-8"))
            if current and size + length > budget:
                chunks.append("".join(current)); current, size = [], 0
            current.append(part); size += length
    if current:
        chunks.append("".join(current))
    return chunks or [""]


def conversation_pages(thread: dict, title: str, messages: list[dict], chat_url: str | None = None) -> list[str]:
    blocks = []
    for number, message in enumerate(messages, 1):
        blocks.append(f"## {number}. {message['role']}\n\n{message['time']}\n\n{message['text']}\n\n---\n\n")
    if not blocks:
        blocks = ["此会话暂未找到可显示的用户消息或 Codex 回复。\n"]
    chunks = split_utf8("".join(blocks), PAGE_BYTES - 7000)
    pages = []
    for number, chunk in enumerate(chunks, 1):
        links = [f"[目录](../../README.md)", f"[全部会话](../../sessions/README.md)"]
        links.insert(0, f"[进入对话]({chat_url or chat_link(thread['id'], title)})")
        if number > 1:
            links.append(f"[上一页](page-{number - 1:03}.md)")
        if number < len(chunks):
            links.append(f"[下一页](page-{number + 1:03}.md)")
        pages.append(
            f"# {label(title)}\n\n{' · '.join(links)}\n\n"
            f"会话 ID：`{thread['id']}`  \n"
            f"更新时间：{stamp(thread.get('updated_at_ms') or thread['updated_at'] * 1000)}  \n"
            f"状态：{'已归档' if thread['archived'] else '未归档'} · 第 {number}/{len(chunks)} 页 · 共 {len(messages)} 条可见消息\n\n"
            "点击“进入对话”即可与这条本机会话继续交流。首次点击提交已填好的连接页，之后直接在同一页评论；无需重复新建任务。`/model 完整模型ID [档位]` 切换模型，`/effort` 查看或调整思考档位。\n\n"
            f"{chunk}"
        )
    return pages


class Mirror:
    def __init__(self, codex_home: Path, output: Path, state_file: Path):
        self.codex_home, self.output, self.state_file = codex_home, output, state_file

    def export(self) -> dict:
        router_path = BASE / "conversation-state.json"
        router_state = json.loads(router_path.read_text(encoding="utf-8")) if router_path.exists() else {}
        rooms = router_state.get("rooms", {})
        names = {}
        index = self.codex_home / "session_index.jsonl"
        if index.exists():
            for line in index.open(encoding="utf-8"):
                try:
                    item = json.loads(line)
                    names[item["id"]] = item.get("thread_name", "")
                except (ValueError, KeyError):
                    continue
        with contextlib.closing(db(self.codex_home / "state_5.sqlite")) as connection:
            threads = [dict(row) for row in connection.execute("SELECT * FROM threads ORDER BY updated_at DESC,id")]
        old = json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists() else {}
        if old.get("schema") != SCHEMA:
            old = {}
        cache, records, expected = {}, [], set()
        history_path = self.codex_home / "thread_history_1.sqlite"
        with contextlib.ExitStack() as stack:
            history = stack.enter_context(contextlib.closing(db(history_path))) if history_path.exists() else None
            # A final reply can reach the history projection after thread metadata.
            # Include its ordinal/count to invalidate even an unchanged metadata row.
            revisions = {}
            if history is not None:
                revisions = {row[0]: [row[1], row[2], row[3]] for row in history.execute(
                    "SELECT thread_id,count(*),max(rollout_ordinal),max(updated_at_ordinal) "
                    "FROM thread_items WHERE item_type IN ('userMessage','agentMessage') GROUP BY thread_id"
                )}
            for thread in threads:
                thread_id = thread["id"]
                # Use UUID paths exclusively; a title can contain unsafe filename characters.
                if not re.fullmatch(r"[0-9a-fA-F-]{36}", thread_id):
                    raise ValueError("Invalid thread id in local index")
                title = title_of(thread, names)
                chat_url = chat_link(thread_id, title, rooms)
                rollout = Path(thread["rollout_path"])
                rollout_revision = []
                for source_path in (rollout, Path(str(rollout) + ".zst")):
                    try:
                        source_stat = source_path.stat()
                        rollout_revision.append([source_stat.st_mtime_ns, source_stat.st_size])
                    except FileNotFoundError:
                        rollout_revision.append(None)
                fingerprint = hashlib.sha256(json.dumps([thread, revisions.get(thread_id), rollout_revision, chat_url], sort_keys=True, ensure_ascii=False).encode() + title.encode()).hexdigest()
                previous = old.get("threads", {}).get(thread_id, {})
                entry = previous if previous.get("fingerprint") == fingerprint and all((self.output / p).exists() for p in previous.get("files", [])) and previous.get("files") else None
                if entry is None:
                    projected = history_messages(history, thread_id)
                    original = rollout_messages(rollout)
                    messages = merge_messages(projected, original)
                    source = "history+rollout" if projected and original else "history" if projected else "rollout"
                    if not messages:
                        source = "empty" if rollout.exists() or Path(str(rollout) + ".zst").exists() else "missing"
                    files = []
                    for number, text in enumerate(conversation_pages(thread, title, messages, chat_url), 1):
                        relative = f"sessions/{thread_id}/page-{number:03}.md"
                        write_if_changed(self.output / relative, text)
                        files.append(relative)
                    entry = {"fingerprint": fingerprint, "files": files, "messages": len(messages), "source": source}
                cache[thread_id] = entry
                expected.update(entry["files"])
                records.append({"id": thread_id, "title": title, "updated": stamp(thread.get("updated_at_ms") or thread["updated_at"] * 1000), "archived": bool(thread["archived"]), "agent": is_subagent(thread), "messages": entry["messages"], "source": entry["source"], "path": entry["files"][0], "chat_url": chat_url})
        for path in (self.output / "sessions").glob("*/page-*.md"):
            if path.relative_to(self.output).as_posix() not in expected:
                path.unlink()  # Only remove previously generated pages within the mirror.
        stats = {"total": len(records), "readable": sum(r["messages"] > 0 for r in records), "archived": sum(r["archived"] for r in records), "agents": sum(r["agent"] for r in records), "messages": sum(r["messages"] for r in records), "pages": len(expected)}
        groups = [("all", "全部会话", records), ("main", "主会话", [r for r in records if not r["agent"]]), ("archived", "已归档", [r for r in records if r["archived"]]), ("agents", "子代理会话", [r for r in records if r["agent"]])]
        expected_indices = set()
        for slug, heading, rows in groups:
            count = max(1, (len(rows) + 79) // 80)
            for number in range(1, count + 1):
                filename = f"sessions/{slug}-{number:03}.md"
                expected_indices.add(filename)
                nav = " · ".join(f"[第 {i} 页]({slug}-{i:03}.md)" for i in range(1, count + 1))
                text = f"# {heading}\n\n[仓库首页](../README.md) · [分类目录](README.md)\n\n点击“进入对话”与本机原会话聊天；点击会话标题查看历史。首次连接只需提交已经填好的连接页。\n\n共 {len(rows)} 个会话，按最近更新时间排序。\n\n{nav}\n\n| 会话 | 对话入口 | 更新时间 | 消息数 | 状态 |\n|---|---|---|---:|---|\n"
                for row in rows[(number - 1) * 80:number * 80]:
                    text += f"| [{label(row['title'])}]({row['id']}/page-001.md) | [进入对话]({row['chat_url']}) | {row['updated']} | {row['messages']} | {'已归档' if row['archived'] else '未归档'}{' · 子代理' if row['agent'] else ''} |\n"
                write_if_changed(self.output / filename, text)
        for path in (self.output / "sessions").glob("*-*.md"):
            if path.relative_to(self.output).as_posix() not in expected_indices:
                path.unlink()
        overview = f"# 会话目录\n\n[仓库首页](../README.md)\n\n共有 {stats['total']} 个会话，{stats['readable']} 个包含可见消息，{stats['messages']} 条消息。\n\n"
        overview += "\n".join(f"- [{heading}（{len(rows)}）]({slug}-001.md)" for slug, heading, rows in groups) + "\n\n按会话名找记录，或使用 GitHub 仓库搜索功能搜索对话文本。手机端可打开仓库的 Code 页面，再点 README 中的入口。\n"
        write_if_changed(self.output / "sessions/README.md", overview)
        write_if_changed(self.output / "sessions/index.json", json.dumps({"stats": stats, "sessions": records}, ensure_ascii=False, indent=2) + "\n")
        readme = (
            "# Codex 本机会话与远程任务\n\n"
            f"**[打开远程聊天入口](https://github.com/{REPO}/issues/{router_state['hub']})**\n\n" if router_state.get("hub") else "# Codex 本机会话与远程任务\n\n"
        )
        readme += (
            "**[打开全部会话](sessions/all-001.md)** · [主会话](sessions/main-001.md) · [已归档](sessions/archived-001.md) · [子代理会话](sessions/agents-001.md)\n\n"
            f"已收录 **{stats['total']}** 个本机会话、**{stats['messages']}** 条可见消息。点击“进入对话”继续本机原会话；首次连接提交已填好的连接页，之后直接在评论框聊天。\n\n"
            "本机会话变化由 Windows 文件通知触发，连续写入合并后自动更新；GitHub 留言每 15 秒检查。监听和同步不调用模型、不消耗模型 token。关机时保留上次快照，留言等开机后处理。\n\n"
            "## 最近主会话\n\n"
        )
        recent_main = [row for row in records if not row["agent"]][:12]
        readme += "\n".join(f"- [{label(row['title'])}]({row['path']})" for row in recent_main)
        readme += (
            "\n\n## 使用方法\n\n"
            "- 继续已有会话：点击目录或历史页的“进入对话”，在评论框直接留言。\n"
            "- 远程选择会话：在聊天入口评论 `/session 会话名或ID`，然后继续留言。\n"
            "- 远程切换模型：评论 `/model 完整模型ID [档位]`；`/models` 查看模型及档位。\n"
            "- 调整思考档位：`/effort` 查看当前模型档位，`/effort xhigh` 切换，`/effort auto` 恢复本机默认；仅对当前对话生效。\n"
            "- 引导正在执行的任务：评论 `/steer 补充要求`，继续同一轮；升级前已运行的 CLI 任务会说明限制。\n"
            "- 确认引导送达：查看同一 Issue 的状态卡，👀 已读 → 🟡 已提交 → 🟢 会话已接收；原留言上的 🚀 也表示会话已接收。\n"
            "- 等当前回复结束后执行：评论 `/queue 下一条任务`；普通留言默认排队。\n"
            "- 查看或撤回排队消息：`/queue` 查看列表；`/cancel` 撤回最近一条，`/cancel 消息ID` 撤回指定消息，`/cancel all` 清空当前 Issue 的等待队列。\n"
            "- 查找内容：在仓库搜索框中输入关键词，选择代码搜索；也可在会话目录中按名称查找。\n"
            "- 执行新任务：直接新建 Issue，无需添加标签，填写任务或在评论框留言。\n"
            "- 如果正文只有 `/model 模型ID`，会先确认模型，再在同一 Issue 下发送实际任务。\n"
            "- 新任务回复后保持打开；结束时由你手动关闭，或单独留言 `/close`。\n"
            "- 继续 Issue 任务：在原 Issue 下追加评论，沿用原会话。\n\n"
            f"当前路由默认模型 `{current_route_model(SETTINGS.codex_home) or 'auto'}`。路由由本机 Codex/CC Switch 管理；本桥接器只读取它。`/model` 只对当前 Issue 的会话生效，路由变化后该覆盖自动失效。\n\n"
            "## 同步范围\n\n"
            "包含本机索引中的主会话、归档会话和子代理会话，以及用户消息、Codex 答复和进度文字。图片附件留在本机。内置规则、内部推理、工具原始输出不进入阅读页面；常见凭据会隐藏。\n\n"
            "同步器只读取 Codex 数据，不修改原会话。只有目标仓库仍为私有时才允许推送。\n\n"
            "[项目源码与安装说明](https://github.com/CircuitWizard-SI/codex2github) · [机器可读会话目录](sessions/index.json)\n"
        )
        write_if_changed(self.output / "README.md", readme)
        write_if_changed(self.state_file, json.dumps({"schema": SCHEMA, "threads": cache, "stats": stats}, ensure_ascii=False))
        return stats


def run(args: list[str], cwd: Path | None = None, input_text: str | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, input=input_text, capture_output=True, encoding="utf-8", errors="replace", timeout=120,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        # Avoid logging remote content or credential-bearing subprocess output.
        raise RuntimeError(f"{args[0]} {args[1]} failed (exit {result.returncode})")
    return result.stdout.strip()


def api(endpoint: str, method: str = "GET", body: dict | None = None) -> dict:
    args = ["gh", "api", endpoint, "--method", method]
    if body is not None:
        args += ["--input", "-"]
    output = run(args, input_text=json.dumps(body, ensure_ascii=False) if body is not None else None)
    return json.loads(output) if output else {}


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def publish(output: Path, repo: str) -> bool:
    """Publish one atomic commit over api.github.com, without a Git transport.

    Re-read the actual remote tree on every tick, so failed ref updates are
    retried even when the local export is unchanged. Unrelated files survive.
    """
    prefix = f"repos/{repo}"
    metadata = api(prefix)
    if not is_private_task_repository(metadata):
        raise RuntimeError("Refusing to publish conversations to a non-private repository or source template")
    branch = metadata.get("default_branch") or "main"
    try:
        reference = api(f"{prefix}/git/ref/heads/{branch}")
    except RuntimeError:
        if metadata.get("size") != 0:
            raise
        # Git Trees requires a first commit. Bootstrap an actually empty repo.
        api(f"{prefix}/contents/README.md", "PUT", {"message": "建立 Codex 会话目录", "branch": branch, "content": base64.b64encode((output / "README.md").read_bytes()).decode("ascii")})
        reference = api(f"{prefix}/git/ref/heads/{branch}")
    parent = reference["object"]["sha"]
    commit = api(f"{prefix}/git/commits/{parent}")
    base_tree = commit["tree"]["sha"]
    tree = api(f"{prefix}/git/trees/{base_tree}?recursive=1")
    if tree.get("truncated"):
        raise RuntimeError("Remote tree truncated; refusing an incomplete comparison")
    remote = {item["path"]: item["sha"] for item in tree["tree"] if item["type"] == "blob"}
    files = [output / "README.md"]
    for folder in ("sessions",):
        files.extend(p for p in (output / folder).rglob("*") if p.is_file())
    local, changes = set(), []
    for path in sorted(files):
        relative = path.relative_to(output).as_posix()
        local.add(relative)
        content = path.read_bytes()
        if remote.get(relative) != blob_sha(content):
            changes.append({"path": relative, "mode": "100644", "type": "blob", "content": content.decode("utf-8")})
    # Only delete obsolete generated conversation/index pages, never user files.
    for path in remote:
        generated = bool(re.fullmatch(r"sessions/[0-9a-fA-F-]{36}/page-\d{3}\.md", path) or re.fullmatch(r"sessions/(all|main|archived|agents)-\d{3}\.md", path))
        if generated and path not in local:
            changes.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
    if not changes:
        return False
    new_tree = api(f"{prefix}/git/trees", "POST", {"base_tree": base_tree, "tree": changes})
    new_commit = api(f"{prefix}/git/commits", "POST", {"message": "更新本机 Codex 会话目录与对话", "tree": new_tree["sha"], "parents": [parent]})
    # force=false rejects a concurrent remote write; next tick uses its new base.
    api(f"{prefix}/git/refs/heads/{branch}", "PATCH", {"sha": new_commit["sha"], "force": False})
    return True


def bridge_signature() -> tuple:
    # A public source repository never receives private runtime/history files.
    # The history mirror contains only generated conversation pages and indexes.
    state_path = BASE / "conversation-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        state = {}
    return json.dumps([state.get("hub"), state.get("rooms", {})], sort_keys=True),


@contextlib.contextmanager
def singleton(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if handle.tell() == 0:
                handle.write(b"0"); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=SETTINGS.codex_home)
    parser.add_argument("--output", type=Path, default=BASE / "repository")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=30, help="Minimum seconds between changed-history exports")
    parser.add_argument("--recovery-seconds", type=int, default=900, help="Low-frequency recovery after missed events")
    args = parser.parse_args(argv)
    SETTINGS.require_configured()
    if args.repo != REPO:
        parser.error("Use a separate configuration to change the task repository")
    args.output = args.output.resolve()
    args.output.relative_to(BASE)  # A caller cannot write outside this workspace.
    mirror = Mirror(args.codex_home, args.output, BASE / "export-state.json")
    with singleton(BASE / "sync.lock"), contextlib.ExitStack() as stack:
        events = None
        schedule = None
        if args.watch:
            from .session_events import SessionEvents, SyncSchedule
            events = SessionEvents(args.codex_home, BASE / "session-events.json").start()
            stack.callback(events.close)
            schedule = SyncSchedule(max(10, args.interval), max(60, args.recovery_seconds))
        signature = bridge_signature()
        while True:
            if events:
                if events.poll():
                    schedule.changed()
                current_signature = bridge_signature()
                if current_signature != signature:
                    signature = current_signature
                    schedule.changed()
                now = time.monotonic()
                if not schedule.due(now):
                    time.sleep(1)
                    continue
                if not schedule.dirty:  # Repair an overflow or a lost notification.
                    events.commit({"metadata", "history"})
            ok = False
            try:
                stats = mirror.export()
                pushed = publish(args.output, args.repo) if args.publish else False
                result = {"time": datetime.now().astimezone().isoformat(), "ok": True, "pushed": pushed,
                          "mode": "filesystem-events" if events else "once",
                          "recovery_seconds": args.recovery_seconds, **stats}
                write_if_changed(BASE / "sync-status.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps(result, ensure_ascii=False), flush=True)
                ok = True
            except Exception as error:
                result = {"time": datetime.now().astimezone().isoformat(), "ok": False, "error": str(error)}
                write_if_changed(BASE / "sync-status.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if not args.watch:
                    return 1
            if not args.watch:
                return 0
            schedule.completed(time.monotonic(), ok)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
