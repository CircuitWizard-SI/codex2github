from tests import TEST_ROOT
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest

from codex2github.session_events import Debouncer, DirectoryNotifications, SessionEvents, SyncSchedule, decode_events, event_kinds

BASE = TEST_ROOT


class EventTests(unittest.TestCase):
    def test_filters_creation_rename_history_and_unrelated_writes(self):
        self.assertEqual(event_kinds(r"sessions\2026\10\rollout.jsonl", 1), {"metadata", "history"})
        self.assertEqual(event_kinds("archived_sessions/rollout.jsonl.zst", 5), {"metadata", "history"})
        self.assertEqual(event_kinds("sessions/rollout.jsonl", 3), {"history"})
        self.assertEqual(event_kinds("state_5.sqlite-wal", 3), {"metadata", "history"})
        self.assertEqual(event_kinds("thread_history_1.sqlite-wal", 3), {"history"})
        self.assertEqual(event_kinds("session_index.jsonl", 3), {"metadata", "history"})
        self.assertEqual(event_kinds("state_5.sqlite-shm", 3), set())
        self.assertEqual(event_kinds("logs/debug.jsonl", 3), set())

    def test_quiet_debounce_and_maximum_delay_during_streaming(self):
        events = Debouncer(quiet=3, maximum=15)
        events.add({"metadata", "history"}, 0)
        events.add({"history"}, 2)
        self.assertEqual(events.take(3), {"metadata"})
        self.assertEqual(events.take(4), set())
        self.assertEqual(events.take(5), {"history"})
        events.add({"history"}, 10)
        for now in range(11, 26):
            events.add({"history"}, now)
        self.assertEqual(events.take(25), {"history"})
        self.assertEqual(events.take(30), set())

    def test_no_idle_exports_and_failed_publish_retries_without_new_event(self):
        schedule = SyncSchedule(interval=30, recovery=900)
        self.assertTrue(schedule.due(0))
        schedule.completed(0, True)
        self.assertFalse(schedule.due(60))
        self.assertFalse(schedule.due(899))
        self.assertTrue(schedule.due(900))
        schedule.changed()
        schedule.completed(900, False)
        self.assertFalse(schedule.due(929))
        self.assertTrue(schedule.due(930))
        schedule.completed(930, True)
        schedule.changed()  # An event received after/during the preceding publish.
        self.assertFalse(schedule.due(940))
        self.assertTrue(schedule.due(960))

    def test_decodes_unicode_notification_chain(self):
        first, second = "sessions/中文.jsonl".encode("utf-16-le"), "session_index.jsonl".encode("utf-16-le")
        size = (12 + len(first) + 3) // 4 * 4
        data = struct.pack("<III", size, 1, len(first)) + first
        data += bytes(size - len(data)) + struct.pack("<III", 0, 3, len(second)) + second
        self.assertEqual(list(decode_events(data)), [("sessions/中文.jsonl", 1), ("session_index.jsonl", 3)])

    @unittest.skipUnless(os.name == "nt", "Windows native notifications")
    def test_native_nested_create_rename_and_delete_without_directory_scan(self):
        with tempfile.TemporaryDirectory(dir=BASE) as temporary:
            root = Path(temporary)
            folder = root / "sessions" / "2026" / "10"
            folder.mkdir(parents=True)
            received = []
            wake = threading.Event()
            def changed(name, action):
                received.append((name.replace("\\", "/"), action))
                wake.set()
            watcher = DirectoryNotifications(root, changed).start()
            try:
                source = folder / "测试.jsonl"
                source.write_text("first", encoding="utf-8")
                renamed = source.with_name("renamed.jsonl")
                source.rename(renamed)
                renamed.unlink()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not any(name.endswith("renamed.jsonl") and action == 2 for name, action in received):
                    wake.wait(.1)
                    wake.clear()
                self.assertIsNone(watcher.error)
                self.assertTrue(any(name.endswith("测试.jsonl") and action == 1 for name, action in received), received)
                self.assertTrue(any(name.endswith("renamed.jsonl") and action == 5 for name, action in received), received)
                self.assertTrue(any(name.endswith("renamed.jsonl") and action == 2 for name, action in received), received)
            finally:
                watcher.close()
            self.assertFalse(watcher.worker.is_alive())

    @unittest.skipUnless(os.name == "nt", "Windows native notifications")
    def test_event_signal_merges_writes_and_keeps_history_separate(self):
        with tempfile.TemporaryDirectory(dir=BASE) as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            events = SessionEvents(home, root / "signal.json", quiet=.15, maximum=.5).start()
            try:
                baseline = dict(events.revisions)
                self.assertFalse(events.poll())
                (home / "unrelated.txt").write_text("ignored")
                (home / "thread_history_1.sqlite-wal").write_text("a")
                (home / "thread_history_1.sqlite-wal").write_text("ab")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not events.poll():
                    time.sleep(.05)
                self.assertEqual(events.revisions["metadata"], baseline["metadata"])
                self.assertNotEqual(events.revisions["history"], baseline["history"])
                self.assertEqual(events.flushes, 2)  # Startup plus one merged notification.
            finally:
                events.close()


if __name__ == "__main__":
    unittest.main()
