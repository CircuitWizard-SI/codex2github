"""Constrained next-turn settings and same-turn input on an existing owner.

No global settings endpoint is available here. New task text goes through the
official Codex CLI; steering goes to the existing owner's active turn.
"""
from __future__ import annotations

import json
import os
import struct
import time
import uuid
from contextlib import contextmanager

from .reasoning import normalize
from .route_models import MODEL_ID


class SettingsUnavailable(RuntimeError):
    """The existing conversation owner cannot safely apply next-turn settings."""


class _Pipe:
    def __init__(self, timeout: float):
        if os.name != "nt":
            raise SettingsUnavailable("本机 Desktop 会话设置接口不可用")
        import ctypes
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                              wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD]
        self.kernel.PeekNamedPipe.restype = wintypes.BOOL
        import msvcrt
        self.stream = open(r"\\.\pipe\codex-ipc", "r+b", buffering=0)
        self.handle = msvcrt.get_osfhandle(self.stream.fileno())
        self.deadline = time.monotonic() + timeout

    def write(self, data: bytes):
        self.stream.write(data)

    def read(self, count: int) -> bytes:
        import ctypes
        from ctypes import wintypes
        data = bytearray()
        while len(data) < count:
            if time.monotonic() >= self.deadline:
                raise SettingsUnavailable("本机会话接口响应超时，结果暂未确认")
            available = wintypes.DWORD()
            if not self.kernel.PeekNamedPipe(self.handle, None, 0, None, ctypes.byref(available), None):
                raise SettingsUnavailable("本机会话接口连接已断开，结果暂未确认")
            if available.value:
                chunk = self.stream.read(min(count - len(data), available.value))
                if not chunk:
                    raise SettingsUnavailable("本机会话设置连接已关闭")
                data.extend(chunk)
            else:
                time.sleep(.02)
        return bytes(data)

    def close(self):
        self.stream.close()


class _ConversationSettings:
    # These are the entire interface, not a general Desktop RPC client.
    _VERSIONS = {"initialize": 0, "thread-owner-discovery": 1,
                 "thread-follower-read-model-settings": 1,
                 "thread-follower-update-thread-settings": 2,
                 "thread-follower-steer-turn": 1}

    def __init__(self, pipe, sid: str):
        uuid.UUID(sid)
        self.pipe, self.sid = pipe, sid
        self.client_id = "initializing-client"
        self.owner = ""
        self.steer_dispatched = False

    def _request(self, method: str, params: dict) -> dict:
        if method not in self._VERSIONS:
            raise ValueError("Only conversation model/effort operations are allowed")
        if method != "initialize" and params.get("conversationId") != self.sid:
            raise ValueError("Conversation id must match the authorized task")
        if method == "thread-follower-update-thread-settings":
            if set(params) != {"conversationId", "threadSettings"} or not set(params["threadSettings"]) <= {"model", "effort"}:
                raise ValueError("Only next-turn model/effort settings are allowed")
        if method == "thread-follower-steer-turn":
            if set(params) != {"conversationId", "input", "restoreMessage", "clientUserMessageId"}:
                raise ValueError("Only original same-turn text input is allowed")
        request_id = str(uuid.uuid4())
        request = {"type": "request", "requestId": request_id, "sourceClientId": self.client_id,
                   "method": method, "version": self._VERSIONS[method], "params": params}
        if self.owner:
            request["targetClientId"] = self.owner
        raw = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if method == "thread-follower-steer-turn":
            self.steer_dispatched = True
        self.pipe.write(struct.pack("<I", len(raw)) + raw)
        while True:
            size = struct.unpack("<I", self.pipe.read(4))[0]
            if size > 4 * 1024 * 1024:
                raise SettingsUnavailable("本机会话设置响应过大")
            response = json.loads(self.pipe.read(size))
            if response.get("requestId") != request_id or response.get("type") != "response":
                continue
            if response.get("resultType") != "success":
                raise SettingsUnavailable("本机目标会话无法应用模型或档位：" + str(response.get("error", "unknown")))
            if response.get("method") != method:
                raise SettingsUnavailable("本机会话设置响应不匹配")
            return response

    def connect(self):
        self.client_id = self._request("initialize", {"clientType": "cli"})["result"]["clientId"]
        response = self._request("thread-owner-discovery", {"hostId": "local", "conversationId": self.sid})
        self.owner = response["handledByClientId"]

    def read(self) -> dict:
        result = self._request("thread-follower-read-model-settings", {"conversationId": self.sid})["result"]
        return result.get("settings") or {}

    def apply(self, model: str, effort: str) -> dict:
        settings = {}
        if model and model != "auto":
            if not MODEL_ID.fullmatch(model):
                raise ValueError("Invalid conversation model id")
            settings["model"] = model
        selected = normalize(effort or "auto")
        if selected != "auto":
            settings["effort"] = selected
        if settings:
            result = self._request("thread-follower-update-thread-settings",
                                   {"conversationId": self.sid, "threadSettings": settings})["result"]
            if result.get("applied") is not True:
                raise SettingsUnavailable("本机未确认单会话模型和档位已应用；未提交任务文本")
        actual = self.read()
        if settings.get("model", actual.get("model")) != actual.get("model") or settings.get("effort", actual.get("reasoningEffort")) != actual.get("reasoningEffort"):
            raise SettingsUnavailable("本机会话模型或档位读取结果不匹配；未提交任务文本")
        return actual

    def steer(self, prompt: str, message_id: str, expected_turn: str) -> dict:
        if not prompt or not message_id or not expected_turn:
            raise ValueError("Same-turn steering requires text, message id and an active turn")
        result = self._request("thread-follower-steer-turn", {
            "conversationId": self.sid,
            "input": [{"type": "text", "text": prompt, "text_elements": []}],
            "restoreMessage": {"id": message_id, "text": prompt, "context": {}},
            "clientUserMessageId": message_id,
        })["result"].get("result", {})
        # The native owner calls turn/steer with its active turn precondition.
        # Never turn a timeout, ended turn or mismatched response into a queue.
        if result.get("turnId") != expected_turn:
            return {"ok": False, "uncertain": True, "error": "原会话引导轮次未确认，未自动排队或重发"}
        return {"ok": True, "turn_id": result["turnId"], "transport": "native-steer"}


@contextmanager
def _connection(sid: str, timeout: float = 10):
    pipe = None
    try:
        pipe = _Pipe(timeout)
        client = _ConversationSettings(pipe, sid)
        client.connect()
        yield client
    except OSError as error:
        raise SettingsUnavailable("无法连接本机目标会话设置接口；未提交任务文本") from error
    finally:
        if pipe:
            pipe.close()


def apply_existing_thread(sid: str, model: str, effort: str, timeout: float = 10) -> dict:
    with _connection(sid, timeout) as client:
        return client.apply(model, effort)


def read_existing_thread(sid: str, timeout: float = 10) -> dict:
    with _connection(sid, timeout) as client:
        return client.read()


def steer_existing_thread(sid: str, prompt: str, message_id: str, expected_turn: str, timeout: float = 10) -> dict:
    client = None
    try:
        with _connection(sid, timeout) as client:
            return client.steer(prompt, message_id, expected_turn)
    except (SettingsUnavailable, TimeoutError, OSError) as error:
        return {"ok": False, "uncertain": bool(client and client.steer_dispatched),
                "error": "原会话引导未确认，未自动排队或重发：" + str(error)}
