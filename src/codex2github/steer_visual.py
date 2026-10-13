"""Render GitHub-native delivery cards from actual steering receipts."""
from __future__ import annotations

import re
from datetime import datetime

from .session_sync import label, redact

VERSION = 1


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def marker(comment: int) -> str:
    return f"<!-- codex2github:steer-status {comment} -->"


def status(receipt: dict) -> tuple[str, str]:
    if receipt.get("canceled"):
        return "⚪ 已撤回", "这条等待消息已撤回，当前任务继续运行。"
    if receipt.get("started"):
        return "🚀 已开始下一轮", "这条消息此前进入等待队列，现在已开始下一轮任务。"
    if receipt.get("queued"):
        return "📥 已排队", "当前没有正在执行的任务，已将这条消息作为下一条任务排队。"
    if receipt.get("ok"):
        return "🟢 会话已接收", "已引导当前任务，继续在同一执行轮次处理。没有新增排队消息。"
    if receipt.get("blocked"):
        return "🔴 未提交", "引导尚未送入会话，请查看下方原因。"
    if receipt.get("uncertain"):
        return "🟠 待确认", "引导结果未能确认，未自动排队。请查看任务反馈，避免重复提交。"
    if receipt.get("pending"):
        if receipt.get("submitted_at"):
            return "🟡 已提交，等待接收", "监听器已提交引导，正在等待执行轮次返回接收回执。"
        return "👀 监听器已读取", "监听器已读取这条引导，正在连接目标会话。"
    return "🔴 未接收", "引导未接收，未加入排队。需要下一轮处理时请用 `/queue 内容`。"


def display_time(value: str | None) -> str:
    if not value:
        return "未记录"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M:%S")
    except ValueError:
        return "未记录"


def render(repo: str, number: int, comment: int, receipt: dict, title: str, room: int | None) -> str:
    heading, explanation = status(receipt)
    sid, turn_id = receipt.get("sid"), receipt.get("turn_id")
    target = label(title[:80]) if title else "尚未绑定会话"
    if sid:
        target = f"[{target}](https://github.com/{repo}/issues/{room or number})"
    original = f"[查看原留言](https://github.com/{repo}/issues/{number}" + (f"#issuecomment-{comment})" if comment else ")")
    submitted = bool(receipt.get("submitted_at") or receipt.get("ok") or receipt.get("uncertain"))
    accepted = bool(receipt.get("ok")) and not receipt.get("queued") and not receipt.get("canceled")
    text = (
        marker(comment) + f"\n### 引导送达状态 · {heading}\n\n"
        f"{explanation}\n\n"
        "| 项目 | 状态 |\n|---|---|\n"
        f"| 原消息 | {original} · ID `{comment}` |\n"
        f"| 目标会话 | {target} |\n"
        f"| 监听器读取 | {display_time(receipt.get('observed_at'))} |\n"
        f"| 确认接收 | {display_time(receipt.get('accepted_at')) if accepted else '尚未确认'} |\n\n"
        "- [x] 监听器已读取 GitHub 留言\n"
        f"- [{'x' if submitted else ' '}] 引导已提交到当前会话\n"
        f"- [{'x' if accepted else ' '}] 当前执行轮次已确认接收\n"
    )
    summary = re.sub(r"\s+", " ", redact(receipt.get("text", ""))).strip()
    if summary:
        text += f"\n**消息摘要：** {label(summary[:220])}\n"
    if receipt.get("error") and not accepted:
        reason = label(re.sub(r"\s+", " ", redact(receipt["error"])).strip()[:600])
        text += f"\n**原因：** {reason}\n"
    if sid:
        text += f"\n<details>\n<summary>会话标识</summary>\n\n会话 ID：`{sid}`\n"
        if turn_id:
            text += f"\n执行轮次：`{turn_id}`\n"
        text += "\n</details>\n"
    text += "\n🟢 表示执行轮次确认收到引导，将在后续处理步骤应用；不代表要求已经完成。原留言上的 👀 表示监听器已读，🚀 表示会话已接收引导。页面需要时下拉刷新。\n"
    return text
