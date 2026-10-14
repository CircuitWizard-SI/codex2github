"""Run a task and publish its result; it never closes a GitHub Issue."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .appserver_worker import (AppServer, run_turn, process_controls, queue_existing_thread,
                               is_active_writer_error, SettingsUnavailable, QueueSubmissionUncertain)
from .config import SETTINGS, current_route_model, is_private_task_repository
from .reasoning import normalize as normalize_effort, default_effort
from .session_sync import BASE, api, redact, write_if_changed


def post_result(issue: int, final: str, model: str, ok: bool, notice: str = "", effort: str = "", request_id: int | None = None) -> dict:
    prefix = f"repos/{SETTINGS.repo}"
    if not is_private_task_repository(api(prefix)):
        raise RuntimeError("Refusing to publish task output to a non-private repository or source template")
    detail = f" · 思考档位 `{effort}`" if effort and effort != "auto" else ""
    marker = f"<!-- codex2github:result {request_id} -->\n" if request_id is not None else ""
    body = "<!-- codex2github:reply -->\n" + marker + ("✅" if ok else "❌") + f" {model}{detail}{notice}\n\n" + redact(final)[:50000]
    return api(f"{prefix}/issues/{issue}/comments", "POST", {"body": body})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--job-directory", type=Path, required=True)
    parser.add_argument("--sid", default="")
    parser.add_argument("--effort", default="")
    args = parser.parse_args(argv)
    SETTINGS.require_configured()
    directory = args.job_directory.resolve()
    directory.relative_to(BASE)
    args.prompt_file.resolve().relative_to(directory)
    directory.mkdir(parents=True, exist_ok=True)
    client = None
    execution_model = (current_route_model(SETTINGS.codex_home) or "auto") if args.model == "auto" else args.model
    selected_effort = normalize_effort(args.effort) if args.effort else "auto"
    if selected_effort == "auto":
        selected_effort = default_effort(SETTINGS.codex_home, execution_model)
    effort = "" if selected_effort == "auto" else selected_effort
    result = {"sid": args.sid, "model": args.model, "effort": selected_effort, "ok": False, "replied": False,
              "transport": "codex-queue" if args.sid else "appserver"}
    try:
        # Check before starting work, not only before posting its final response.
        if not is_private_task_repository(api(f"repos/{SETTINGS.repo}")):
            raise RuntimeError("Task repository must remain private and must not be a source template")
        prompt = args.prompt_file.read_text(encoding="utf-8-sig")
        if args.sid:
            result.update(queue_existing_thread(prompt, execution_model, args.cwd, args.sid,
                                                effort=effort, directory=directory))
        else:
            client = AppServer(directory)
            result.update(run_turn(client, directory, prompt, execution_model, args.cwd, args.sid, effort))
    except Exception as error:
        runtime = directory / "runtime.json"
        if runtime.exists():
            result["sid"] = json.loads(runtime.read_text(encoding="utf-8")).get("sid", args.sid)
        if isinstance(error, QueueSubmissionUncertain):
            result.update(error.receipt, error=str(error))
        else:
            desktop_wait = bool(args.sid and isinstance(error, SettingsUnavailable))
            result.update(error=str(error), final="执行等待：" + str(error),
                          retryable=desktop_wait or is_active_writer_error(error))
            if desktop_wait:
                result.update(retry_reason="desktop-unavailable", execution_complete=False)
    queued = bool(result.get("queued"))
    if queued:
        result["execution_complete"] = False
    write_if_changed(directory / ("queue-receipt.md" if queued else "final.md"), result["final"])
    write_if_changed(directory / "run-result.json", json.dumps(result, ensure_ascii=False))
    if not result.get("retryable"):
        try:
            model = result.get("model") or args.model
            notice = " · 已回退到当前路由，请你自行调整 CC Switch 路由" if result.get("model_fallback") else ""
            if queued:
                if not is_private_task_repository(api(f"repos/{SETTINGS.repo}")):
                    raise RuntimeError("Task repository must remain private and must not be a source template")
                receipt_text = (result["final"] if result.get("submission_uncertain") else
                                f"已提交到本机原会话，模型 `{model}` · 思考档位 `{result.get('effort') or 'auto'}`。等待对应任务完成，完成后会在本 Issue 自动回复。")
                response = api(f"repos/{SETTINGS.repo}/issues/{args.issue}/comments", "POST", {"body":
                    "<!-- codex2github:reply -->\n⏳ " + receipt_text})
                result.update(submission_replied=True, submission_reply_id=response.get("id"))
            else:
                match = re.fullmatch(rf"{args.issue}-(\d+)", directory.name)
                request_id = int(match[1]) if match else None
                if request_id is not None:
                    result["result_post_attempted"] = True
                    write_if_changed(directory / "run-result.json", json.dumps(result, ensure_ascii=False))
                response = post_result(args.issue, result["final"], model, result["ok"], notice,
                                       effort=result.get("effort", ""), request_id=request_id)
                result.update(replied=True, reply_id=response.get("id"))
        except Exception as error:
            result["reply_error"] = str(error)
    write_if_changed(directory / "completion.json", json.dumps(result, ensure_ascii=False))
    marker = BASE / f"running-issue-{args.issue}.json"
    if marker.exists():
        marker.unlink()
    # Completion and its delivery receipt must survive slow/broken shutdown.
    # The router can now advance the queue even while cleanup is finishing.
    try:
        runtime_path = directory / "runtime.json"
        if runtime_path.exists():
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime["active"] = False
            write_if_changed(runtime_path, json.dumps(runtime, ensure_ascii=False))
            if client and runtime.get("turn_id"):
                process_controls(client, directory, runtime["sid"], runtime["turn_id"], active=False)
    except Exception as error:
        result["cleanup_error"] = str(error)
    if client:
        try:
            client.close()
        except Exception as error:
            result["close_error"] = str(error)
    if result.get("close_error") or result.get("cleanup_error"):
        # Update diagnostics separately; the router may have delivered the result.
        write_if_changed(directory / "cleanup.json", json.dumps({key: result[key] for key in
                         ("close_error", "cleanup_error") if key in result}, ensure_ascii=False))
    return 0 if result["ok"] and (result["replied"] or result.get("submission_replied")) else (75 if result.get("retryable") else 1)


if __name__ == "__main__":
    raise SystemExit(main())
