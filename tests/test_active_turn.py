from tests import TEST_ROOT
import json
from pathlib import Path
import tempfile
import unittest

from codex2github.active_turn import active_turn


class ActiveTurnTests(unittest.TestCase):
    def test_original_lifecycle_overrides_missing_or_stale_history_projection(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            home = Path(temporary)
            log = home / "rollout.jsonl"
            rows = [{"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "older"}},
                    {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "active"}},
                    {"type": "response_item", "payload": {"text": "x" * 300000}}]
            log.write_text("\n".join(json.dumps(row) for row in rows) + '\n{"partial":', encoding="utf-8")
            self.assertEqual(active_turn(home, "fixture", {"rollout_path": str(log)}), {"status": "active", "turn_id": "active"})
            with log.open("a", encoding="utf-8") as stream:
                stream.write('\n' + json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "active"}}) + '\n')
            self.assertEqual(active_turn(home, "fixture", {"rollout_path": str(log)})["status"], "idle")

    def test_unreadable_or_absent_thread_is_unknown(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            home = Path(temporary)
            self.assertEqual(active_turn(home, "fixture"), {"status": "unknown"})
            self.assertEqual(active_turn(home, "fixture", {"rollout_path": str(home / "missing.jsonl")}), {"status": "unknown"})


if __name__ == "__main__":
    unittest.main()
