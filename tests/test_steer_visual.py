from tests import TEST_ROOT
import unittest

from codex2github import steer_visual as visual


class CardTests(unittest.TestCase):
    def render(self, **receipt):
        return visual.render("owner/bridge", 7, 123, {"sid": "original-session", "text": "补充要求", **receipt}, "原会话", 9)

    def test_green_requires_positive_ack_and_preserves_original_message_link(self):
        for receipt in ({"pending": True}, {"pending": False, "uncertain": True}, {"ok": False}, {"queued": True}, {"canceled": True}):
            with self.subTest(receipt=receipt):
                self.assertNotIn("🟢 会话已接收", self.render(**receipt))
                self.assertIn("- [ ] 当前执行轮次已确认接收", self.render(**receipt))
        accepted = self.render(ok=True, pending=False, turn_id="active-turn", accepted_at="2026-10-13T10:30:00+08:00")
        self.assertIn("🟢 会话已接收", accepted)
        self.assertIn("- [x] 当前执行轮次已确认接收", accepted)
        self.assertIn("issues/7#issuecomment-123", accepted)
        self.assertIn("issues/9", accepted)
        self.assertIn("active-turn", accepted)

    def test_seen_submitted_and_unknown_are_distinct_and_no_time_is_invented(self):
        self.assertIn("👀 监听器已读取", self.render(pending=True))
        self.assertIn("🟡 已提交，等待接收", self.render(pending=True, submitted_at=visual.now()))
        self.assertIn("🟠 待确认", self.render(uncertain=True))
        self.assertIn("未记录", self.render(ok=True))
        self.assertIn("🔴 未接收", self.render(ok=False, pending=False))
        self.assertIn("🔴 未提交", self.render(blocked=True))

    def test_card_redacts_and_escapes_untrusted_summary_and_error(self):
        body = self.render(text="[注入](https://example.invalid)\n| " + "sk-" + "a" * 35,
                           error="password=" + "b" * 20, ok=False)
        self.assertNotIn("sk-" + "a" * 35, body)
        self.assertNotIn("b" * 20, body)
        self.assertIn("\\[注入\\]", body)
        self.assertIn("\\|", body)


if __name__ == "__main__":
    unittest.main()
