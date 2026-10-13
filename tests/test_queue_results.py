from tests import TEST_ROOT
import json
from pathlib import Path
import tempfile
import unittest

from codex2github.queue_results import follow


class QueueResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.path = Path(self.temp.name) / "rollout.jsonl"
        self.path.touch()
        self.job = {"sid": "session"}
        self.receipt = {"queue_message_id": "queue-id", "rollout_path": str(self.path), "rollout_offset": 0}
        self.record = {"rollout_path": str(self.path)}

    def tearDown(self):
        self.temp.cleanup()

    def append(self, kind, turn, **extra):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": kind, "turn_id": turn, **extra}}) + "\n")

    def user(self, turn):
        self.append("item_completed", turn, item={"type": "UserMessage", "client_id": "desktop-different-id", "content": [{"type": "Text", "text": "remote task"}]})

    def finish(self, turn, text):
        self.append("item_completed", turn, item={"type": "AgentMessage", "phase": "final_answer", "content": [{"type": "Text", "text": text}]})
        self.append("task_complete", turn)

    def test_same_prompt_before_submission_and_unrelated_final_do_not_match(self):
        self.user("old-turn")
        self.finish("old-turn", "old answer")
        self.receipt["rollout_offset"] = self.path.stat().st_size
        self.user("target-turn")
        self.finish("other-turn", "wrong answer")
        self.assertIsNone(follow(self.job, self.receipt, self.record, "remote task"))
        self.finish("target-turn", "correct answer")
        result = follow(self.job, self.receipt, self.record, "remote task")
        self.assertEqual((result["turn_id"], result["final"]), ("target-turn", "correct answer"))

    def test_partial_live_line_is_retried_after_it_is_completed(self):
        self.user("target-turn")
        self.append("item_completed", "target-turn", item={"type": "AgentMessage", "phase": "final_answer", "content": [{"type": "Text", "text": "correct answer"}]})
        self.path.write_bytes(self.path.read_bytes() + b'{"type":"event_msg","payload":{"type":"task_complete"')
        self.assertIsNone(follow(self.job, self.receipt, self.record, "remote task"))
        with self.path.open("ab") as stream:
            stream.write(b',"turn_id":"target-turn"}}\n')
        self.assertEqual(follow(self.job, self.receipt, self.record, "remote task")["final"], "correct answer")

    def test_completed_log_can_be_recovered_after_archival_compression(self):
        import zstandard
        self.user("target-turn")
        self.finish("target-turn", "archived answer")
        Path(str(self.path) + ".zst").write_bytes(zstandard.ZstdCompressor().compress(self.path.read_bytes()))
        self.path.unlink()
        self.assertEqual(follow(self.job, self.receipt, self.record, "remote task")["final"], "archived answer")


if __name__ == "__main__":
    unittest.main()
