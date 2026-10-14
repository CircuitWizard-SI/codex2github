"""Run one original Codex thread with a workspace-local same-turn steering inbox."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import re

from .session_sync import BASE, write_if_changed
from .config import SETTINGS
from .queue_results import submission_snapshot
from .reasoning import normalize as normalize_effort, default_effort
from .thread_settings import apply_existing_thread, SettingsUnavailable
from .request_identity import client_info, codex_environment, verify_user_agent


class RpcError(RuntimeError):
    pass


def is_active_writer_error(error: BaseException | str) -> bool:
    """Identify a thread-store collision that is safe to retry later."""
    message = str(error).casefold()
    return "active writer" in message and "thread" in message


def is_queue_transport_error(error: BaseException | str) -> bool:
    """Return whether the official queue CLI could not reach its server."""
    if isinstance(error, SettingsUnavailable):
        return True
    message = str(error).casefold()
    return any(marker in message for marker in (
        "failed to connect", "connection refused", "connection reset",
        "app-server is unavailable", "app server is unavailable",
        "could not connect", "no such file or directory",
    ))


def codex_command() -> list[str]:
    executable = shutil.which("codex.exe")
    if executable:
        return [executable]
    # Launch the npm entry point directly; never interpolate user text into a shell.
    shim = shutil.which("codex.cmd") or shutil.which("codex")
    if shim:
        entry = Path(shim).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if entry.exists():
            # Prefer the native binary so cancellation cannot orphan a Node child.
            root = entry.parent.parent / "node_modules" / "@openai"
            for package in root.glob("codex-win32-*"):
                binaries = list((package / "vendor").glob("*/bin/codex.exe"))
                if binaries:
                    return [str(binaries[0])]
            return [shutil.which("node") or "node", str(entry)]
        return [shim]
    raise FileNotFoundError("Codex CLI is unavailable")


class AppServer:
    def __init__(self, directory: Path, command: list[str] | None = None):
        self.client_info = client_info()
        environment = codex_environment()
        self.errors = (directory / "appserver-error.log").open("w", encoding="utf-8")
        self.process = subprocess.Popen(command or codex_command() + ["app-server", "--stdio"],
                                        env=environment,
                                        cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.errors, text=True, encoding="utf-8", errors="replace",
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.pending = {}
        self.lock = threading.Lock()
        self.serial = 0
        self.notifications = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def send(self, message: dict):
        with self.lock:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if "id" in message and "method" not in message:
                    with self.lock:
                        response = self.pending.get(message["id"])
                    if response:
                        response.put(message)
                elif "id" in message:
                    # Approval is explicitly 'never'. Fail unexpected interactive
                    # requests instead of silently accepting a different permission.
                    self.send({"id": message["id"], "error": {"code": -32601,
                               "message": "Interactive request unavailable in GitHub task worker"}})
                else:
                    self.notifications.put(message)
        finally:
            with self.lock:
                waiting = list(self.pending.values())
            for response in waiting:
                response.put({"error": {"message": "Codex app-server exited"}})

    def call(self, method: str, params: dict, timeout: float = 30) -> dict:
        with self.lock:
            self.serial += 1
            request_id = self.serial
            response = self.pending[request_id] = queue.Queue()
        try:
            self.send({"id": request_id, "method": method, "params": params})
            try:
                message = response.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(f"{method}: response unknown after timeout") from None
            if "error" in message:
                raise RpcError(message["error"].get("message", str(message["error"])))
            return message["result"]
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def initialize(self):
        result = self.call("initialize", {"clientInfo": self.client_info,
                                          "capabilities": {"experimentalApi": True}})
        self.user_agent = result.get("userAgent", "")
        verify_user_agent(self.user_agent, self.client_info)
        self.send({"method": "initialized", "params": {}})

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.reader.join(2)
        self.process.stdout.close()
        self.errors.close()


def json_file(path: Path, data: dict):
    write_if_changed(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def process_controls(client: AppServer, directory: Path, sid: str, turn_id: str, active: bool = True):
    inbox, outbox = directory / "control-inbox", directory / "control-outbox"
    for path in sorted(inbox.glob("*.json")):
        response_path = outbox / path.name
        if response_path.exists():
            continue  # A restart/poll never injects an acknowledged message again.
        request = json.loads(path.read_text(encoding="utf-8"))
        response = {"ok": False, "sid": sid, "turn_id": turn_id, "comment": request["comment"]}
        # Persist uncertainty before sending. A crash cannot duplicate steering.
        json_file(response_path, {**response, "pending": True, "error": "引导结果暂未确认"})
        if not active or request.get("sid") != sid or request.get("turn_id") != turn_id:
            response["error"] = "目标执行轮次已结束或不匹配，未注入新轮次"
        else:
            try:
                result = client.call("turn/steer", {"threadId": sid, "expectedTurnId": turn_id,
                                                   "clientUserMessageId": request["message_id"],
                                                   "input": [{"type": "text", "text": request["text"], "text_elements": []}]}, timeout=15)
                if result.get("turnId") != turn_id:
                    raise RpcError("Unexpected steering turn id")
                response["ok"] = True
            except (RpcError, TimeoutError, OSError) as error:
                response["error"] = str(error)
                response["uncertain"] = isinstance(error, (TimeoutError, OSError))
        json_file(response_path, response)


def queue_existing_thread(prompt: str, model: str, cwd: str, sid: str, timeout: float = 30, *, effort: str = "") -> dict:
    """Queue one message through Codex's existing local app-server session."""
    if not sid:
        raise RpcError("无法将没有会话 ID 的任务排入现有会话")
    # CLI queue ignores --model and -c model_reasoning_effort for an existing
    # thread. Confirm settings on its owner before submitting any task text.
    selected_effort = normalize_effort(effort) if effort else "auto"
    actual_settings = apply_existing_thread(sid, model, selected_effort, timeout=min(timeout, 10))
    arguments = codex_command() + ["queue", "--thread", sid, "--message", prompt]
    if cwd and Path(cwd).is_dir():
        arguments += ["--cd", cwd]
    environment = codex_environment()
    boundary = submission_snapshot(SETTINGS.codex_home, sid)
    try:
        completed = subprocess.run(arguments, cwd=cwd if cwd and Path(cwd).is_dir() else None,
                                   env=environment, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RpcError(str(error)) from error
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "Codex queue failed").strip()
        raise RpcError(detail)
    receipt = (completed.stdout or "").strip()
    message_id = ""
    match = re.search(r"Queued message ([0-9a-f-]+)", receipt, re.I)
    if match:
        message_id = match.group(1)
    actual = actual_settings.get("model") or model or "auto"
    return {**boundary, "sid": sid, "turn_id": "", "ok": True, "final": receipt or "已排入本机 Codex 会话。",
            "model": actual, "requested_model": model, "effort": actual_settings.get("reasoningEffort") or selected_effort, "queued": True,
            "settings_confirmed": True,
            "queue_message_id": message_id, "transport": "codex-queue"}


def read_route(client: AppServer, cwd: str) -> dict:
    """Read the effective route through Codex; this call never writes settings."""
    try:
        result = client.call("config/read", {"cwd": cwd, "includeLayers": False})
    except (RpcError, TimeoutError, OSError):
        return {}
    config = result.get("config", {}) if isinstance(result, dict) else {}
    return config if isinstance(config, dict) else {}


def run_turn(client: AppServer, directory: Path, prompt: str, model: str, cwd: str,
             sid: str = "", effort: str = "", ephemeral: bool = False) -> dict:
    client.initialize()
    actual_effort = normalize_effort(effort) if effort else "auto"
    requested = model if model and model != "auto" else ""
    route = read_route(client, cwd) if not requested else {}
    actual = requested or route.get("model", "") or "auto"
    fallback = ""

    def thread_start(selected: str):
        settings = {"cwd": cwd}
        if selected:
            settings["model"] = selected
        if sid:
            started = client.call("thread/resume", {"threadId": sid, **settings})
            if started["thread"]["id"] != sid:
                raise RpcError("Codex returned a different session id")
        else:
            started = client.call("thread/start", {**settings, "ephemeral": ephemeral})
        return started

    try:
        started = thread_start(requested)
    except RpcError:
        if not requested:
            raise
        route = read_route(client, cwd)
        fallback = route.get("model", "")
        if not fallback:
            raise RpcError("无法读取当前 Codex 路由模型；请先在本机调整路由后重试")
        actual = fallback
        actual_effort = default_effort(SETTINGS.codex_home, actual)
        started = thread_start("")
    if not sid:
        sid = started["thread"]["id"]
    json_file(directory / "runtime.json", {"sid": sid, "active": False, "transport": "appserver"})
    write_if_changed(directory / "result.md", f"OpenAI Codex\nmodel: {actual}\nsession id: {sid}\n")
    request = {"threadId": sid, "input": [{"type": "text", "text": prompt, "text_elements": []}]}
    if actual_effort != "auto":
        request["effort"] = actual_effort
    if requested and not fallback:
        request["model"] = requested
    try:
        turn = client.call("turn/start", request)["turn"]
    except RpcError:
        if not requested or fallback:
            raise
        route = read_route(client, cwd)
        fallback = route.get("model", "")
        if not fallback:
            raise RpcError("无法读取当前 Codex 路由模型；请先在本机调整路由后重试")
        actual = fallback
        actual_effort = default_effort(SETTINGS.codex_home, actual)
        client.call("thread/resume", {"threadId": sid, "cwd": cwd})
        retry_request = {"threadId": sid, "input": [{"type": "text", "text": prompt, "text_elements": []}]}
        if actual_effort != "auto":
            retry_request["effort"] = actual_effort
        turn = client.call("turn/start", retry_request)["turn"]
    turn_id = turn["id"]
    json_file(directory / "runtime.json", {"sid": sid, "turn_id": turn_id, "active": True, "transport": "appserver"})
    messages, status, error = {}, "inProgress", ""
    # Task duration is controlled by Codex and the user, never a bridge deadline.
    # Short transport waits below still detect process exit and handle steering.
    while status == "inProgress":
        process_controls(client, directory, sid, turn_id)
        try:
            event = client.notifications.get(timeout=.25)
        except queue.Empty:
            if client.process.poll() is not None:
                raise RpcError("Codex app-server exited before completion")
            continue
        params = event.get("params", {})
        if params.get("threadId") != sid or params.get("turnId", turn_id) != turn_id:
            continue
        if event.get("method") == "item/completed":
            item = params["item"]
            if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                messages[item["id"]] = item.get("text", "")
        elif event.get("method") == "turn/completed" and params["turn"]["id"] == turn_id:
            completed = params["turn"]
            status = completed["status"]
            error = (completed.get("error") or {}).get("message", "")
            for item in completed.get("items", []):
                if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                    messages[item["id"]] = item.get("text", "")
    json_file(directory / "runtime.json", {"sid": sid, "turn_id": turn_id, "active": False, "transport": "appserver"})
    process_controls(client, directory, sid, turn_id, active=False)
    final = "\n\n".join(text for text in messages.values() if text).strip()
    ok = status == "completed" and bool(final)
    if not ok and not final:
        final = "Codex 执行未完成：" + (error or status)
    write_if_changed(directory / "final.md", final)
    return {"sid": sid, "turn_id": turn_id, "ok": ok, "final": final,
            "model": actual, "requested_model": model, "model_fallback": fallback, "effort": actual_effort,
            "transport": "appserver"}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-directory", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--sid", default="")
    args = parser.parse_args(argv)
    directory = args.job_directory.resolve()
    directory.relative_to(BASE)
    directory.mkdir(parents=True, exist_ok=True)
    client = None
    result = {"sid": args.sid, "ok": False, "transport": "appserver"}
    try:
        client = AppServer(directory)
        result = run_turn(client, directory, args.prompt_file.read_text(encoding="utf-8-sig"),
                          args.model, args.cwd, args.sid)
    except Exception as error:
        runtime = directory / "runtime.json"
        if runtime.exists():
            result["sid"] = json.loads(runtime.read_text(encoding="utf-8")).get("sid", args.sid)
        result.update(error=str(error), final="执行失败：" + str(error))
        write_if_changed(directory / "final.md", result["final"])
    finally:
        try:
            runtime_path = directory / "runtime.json"
            if runtime_path.exists():
                runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
                runtime["active"] = False
                json_file(runtime_path, runtime)
                if client and runtime.get("turn_id"):
                    process_controls(client, directory, runtime["sid"], runtime["turn_id"], active=False)
            if client:
                client.close()
        finally:
            json_file(directory / "run-result.json", result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
