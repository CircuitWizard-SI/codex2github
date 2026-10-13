"""Windows filesystem notifications and a debounced session revision signal.

Only notifications are read from Codex home; signals stay in the bridge workspace.
No model requests, directory polling, or additional packages are involved.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import struct
import threading
import time
import uuid


def event_kinds(relative: str, action: int) -> set[str]:
    name = relative.replace("\\", "/").lower()
    if name in {"state_5.sqlite", "state_5.sqlite-wal", "session_index.jsonl"}:
        return {"metadata", "history"}
    if name in {"thread_history_1.sqlite", "thread_history_1.sqlite-wal"}:
        return {"history"}
    if name.startswith(("sessions/", "archived_sessions/")) and name.endswith((".jsonl", ".jsonl.zst")):
        # Added, removed, and both halves of a rename can change the list.
        return {"history"} if action == 3 else {"metadata", "history"}
    return set()


def decode_events(data: bytes):
    offset = 0
    while offset + 12 <= len(data):
        following, action, size = struct.unpack_from("<III", data, offset)
        if offset + 12 + size > len(data):
            raise ValueError("Truncated filesystem notification")
        yield data[offset + 12:offset + 12 + size].decode("utf-16-le"), action
        if not following:
            return
        offset += following


class Debouncer:
    def __init__(self, quiet: float = 3, maximum: float = 15):
        self.quiet, self.maximum = quiet, maximum
        self.pending = {}
        self.lock = threading.Lock()

    def add(self, kinds: set[str], now: float | None = None):
        now = time.monotonic() if now is None else now
        with self.lock:
            for kind in kinds:
                first = self.pending.get(kind, (now, now))[0]
                self.pending[kind] = (first, now)

    def take(self, now: float | None = None) -> set[str]:
        now = time.monotonic() if now is None else now
        with self.lock:
            ready = {kind for kind, (first, last) in self.pending.items()
                     if now - last >= self.quiet or now - first >= self.maximum}
            for kind in ready:
                del self.pending[kind]
            return ready


class DirectoryNotifications:
    """ReadDirectoryChangesW with a cancellable overlapped read on a daemon thread."""
    def __init__(self, directory: Path, callback):
        if os.name != "nt":
            raise OSError("Native session watching requires Windows")
        self.directory, self.callback = directory, callback
        self.stop_event = threading.Event()
        self.error = None
        self.ready = threading.Event()
        self.worker = threading.Thread(target=self._run, name="codex-session-events", daemon=True)

    def start(self):
        self.worker.start()
        if not self.ready.wait(5):
            raise TimeoutError("Filesystem watcher did not start")
        if self.error:
            raise self.error
        return self

    def close(self):
        self.stop_event.set()
        self.worker.join(3)

    def _run(self):
        handle = event = None
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class Overlapped(ctypes.Structure):
            _fields_ = [("internal", ctypes.c_size_t), ("internal_high", ctypes.c_size_t),
                        ("offset", wintypes.DWORD), ("offset_high", wintypes.DWORD),
                        ("event", wintypes.HANDLE)]

        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                                wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
                                                ctypes.POINTER(Overlapped), ctypes.c_void_p]
        kernel.ReadDirectoryChangesW.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped),
                                             ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
        kernel.GetOverlappedResult.restype = wintypes.BOOL
        kernel.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped)]
        kernel.CancelIoEx.restype = wintypes.BOOL
        kernel.ResetEvent.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        try:
            handle = kernel.CreateFileW(str(self.directory.resolve()), 0x0001, 0x0007, None, 3,
                                        0x02000000 | 0x40000000, None)
            if handle == ctypes.c_void_p(-1).value:
                handle = None
                raise ctypes.WinError(ctypes.get_last_error())
            event = kernel.CreateEventW(None, True, False, None)
            if not event:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(65536)
            overlapped = Overlapped(event=event)
            while not self.stop_event.is_set():
                kernel.ResetEvent(event)
                if not kernel.ReadDirectoryChangesW(handle, buffer, len(buffer), True,
                                                     0x0001 | 0x0002 | 0x0008 | 0x0010,
                                                     None, ctypes.byref(overlapped), None):
                    if ctypes.get_last_error() != 997:  # ERROR_IO_PENDING
                        raise ctypes.WinError(ctypes.get_last_error())
                self.ready.set()  # First read is armed before the caller creates files.
                while True:
                    status = kernel.WaitForSingleObject(event, 500)
                    if self.stop_event.is_set():
                        kernel.CancelIoEx(handle, ctypes.byref(overlapped))
                        kernel.WaitForSingleObject(event, 2000)
                        return
                    if status == 0:
                        break
                    if status != 258:  # WAIT_TIMEOUT
                        raise ctypes.WinError(ctypes.get_last_error())
                received = wintypes.DWORD()
                if not kernel.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(received), False):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not received.value:
                    self.callback("", 0)  # Overflow: force a full recovery.
                    continue
                for relative, action in decode_events(buffer.raw[:received.value]):
                    self.callback(relative, action)
        except Exception as error:
            self.error = error
            self.callback("", 0)
            self.ready.set()
        finally:
            if event:
                kernel.CloseHandle(event)
            if handle:
                kernel.CloseHandle(handle)


class SessionEvents:
    def __init__(self, home: Path, signal: Path, quiet: float = 3, maximum: float = 15):
        self.signal = signal
        self.debouncer = Debouncer(quiet, maximum)
        self.watcher = DirectoryNotifications(home, self._changed)
        self.revisions = {"metadata": "", "history": ""}
        self.flushes = 0

    def _changed(self, relative: str, action: int):
        kinds = event_kinds(relative, action) if action else {"metadata", "history"}
        self.debouncer.add(kinds)

    def start(self):
        self.watcher.start()
        self.commit({"metadata", "history"})  # Recover changes made while offline.
        return self

    def commit(self, kinds: set[str]):
        token = uuid.uuid4().hex
        self.revisions.update({kind: token for kind in kinds})
        self.flushes += 1
        data = {**self.revisions, "time": time.time(), "flushes": self.flushes, "mode": "filesystem-events"}
        temporary = self.signal.with_suffix(".tmp")
        temporary.write_text(json.dumps(data) + "\n", encoding="utf-8")
        temporary.replace(self.signal)

    def poll(self) -> bool:
        if self.watcher.error:
            raise RuntimeError(f"Filesystem watcher failed: {self.watcher.error}")
        kinds = self.debouncer.take()
        if kinds:
            self.commit(kinds)
        return bool(kinds)

    def close(self):
        self.watcher.close()


class SyncSchedule:
    """Keep events received during publishing, retry failures, and bound live bursts."""
    def __init__(self, interval: float = 30, recovery: float = 900):
        self.interval, self.recovery = interval, recovery
        self.dirty = True
        self.next_allowed = 0.0
        self.next_recovery = 0.0

    def changed(self):
        self.dirty = True

    def due(self, now: float) -> bool:
        return now >= self.next_allowed and (self.dirty or now >= self.next_recovery)

    def completed(self, now: float, ok: bool):
        self.dirty = not ok
        self.next_allowed = now + self.interval
        self.next_recovery = now + self.recovery
