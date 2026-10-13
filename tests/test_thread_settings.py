from tests import TEST_ROOT
import json
import struct
import unittest

from codex2github.thread_settings import _ConversationSettings, SettingsUnavailable


SID = "00000000-0000-0000-0000-000000000001"


class FakePipe:
    def __init__(self, applied=True, confirm=True):
        self.requests, self.buffer = [], bytearray()
        self.settings = {"model": "old-model", "reasoningEffort": "high"}
        self.applied, self.confirm = applied, confirm

    def write(self, frame):
        size = struct.unpack("<I", frame[:4])[0]
        self.assert_size = size == len(frame[4:])
        request = json.loads(frame[4:])
        self.requests.append(request)
        method = request["method"]
        if method == "initialize":
            result = {"clientId": "bridge"}
        elif method == "thread-owner-discovery":
            result = {}
        elif method == "thread-follower-update-thread-settings":
            settings = request["params"]["threadSettings"]
            if self.applied and self.confirm:
                self.settings.update(model=settings.get("model", self.settings["model"]),
                                     reasoningEffort=settings.get("effort", self.settings["reasoningEffort"]))
            result = {"applied": self.applied}
        elif method == "thread-follower-read-model-settings":
            result = {"settings": self.settings}
        elif method == "thread-follower-steer-turn":
            result = {"result": {"turnId": "active-turn"}}
        else:
            raise AssertionError(method)
        response = {"type": "response", "requestId": request["requestId"], "method": method,
                    "resultType": "success", "handledByClientId": "target-owner", "result": result}
        raw = json.dumps(response).encode()
        self.buffer.extend(struct.pack("<I", len(raw)) + raw)

    def read(self, size):
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data


class ThreadSettingsTests(unittest.TestCase):
    def test_native_steering_wire_preserves_text_and_uses_only_same_turn_endpoint(self):
        pipe = FakePipe()
        client = _ConversationSettings(pipe, SID)
        client.connect()
        result = client.steer("原始补充要求", "client-id", "active-turn")
        request = pipe.requests[-1]
        self.assertEqual(request["method"], "thread-follower-steer-turn")
        self.assertEqual(request["params"]["input"], [{"type": "text", "text": "原始补充要求", "text_elements": []}])
        self.assertEqual(request["params"]["clientUserMessageId"], "client-id")
        self.assertEqual(result["turn_id"], "active-turn")
        self.assertTrue(result["ok"])
        self.assertNotIn("thread-follower-update-thread-settings", [r["method"] for r in pipe.requests])
        uncertain = client.steer("下一条补充要求", "another-id", "ended-turn")
        self.assertTrue(uncertain["uncertain"])
        self.assertFalse(uncertain["ok"])

    def test_actual_framed_requests_change_only_target_next_turn_model_and_effort(self):
        pipe = FakePipe()
        client = _ConversationSettings(pipe, SID)
        client.connect()
        actual = client.apply("new-model", "ultra")
        self.assertEqual(actual, {"model": "new-model", "reasoningEffort": "ultra"})
        update = pipe.requests[2]
        self.assertEqual(update["version"], 2)
        self.assertEqual(update["targetClientId"], "target-owner")
        self.assertEqual(update["params"], {"conversationId": SID,
                         "threadSettings": {"model": "new-model", "effort": "ultra"}})
        self.assertEqual([r["method"] for r in pipe.requests], ["initialize", "thread-owner-discovery",
                         "thread-follower-update-thread-settings", "thread-follower-read-model-settings"])
        self.assertTrue(pipe.assert_size)

    def test_no_global_permission_or_active_turn_endpoint_can_be_sent(self):
        client = _ConversationSettings(FakePipe(), SID)
        for method, params in [("config/batchWrite", {}), ("turn/start", {}),
                               ("thread-follower-update-thread-settings", {"conversationId": SID, "threadSettings": {"sandboxPolicy": "danger-full-access"}}),
                               ("thread-follower-update-thread-settings", {"conversationId": SID, "threadSettings": {"model": "new"}, "activeTurnId": "current"}),
                               ("thread-follower-update-thread-settings", {"conversationId": "other", "threadSettings": {"effort": "low"}})]:
            with self.subTest(method=method, params=params), self.assertRaises(ValueError):
                client._request(method, params)
        self.assertEqual(client.pipe.requests, [])

    def test_unapplied_or_mismatched_settings_are_not_reported_as_success(self):
        for pipe in (FakePipe(applied=False), FakePipe(confirm=False)):
            client = _ConversationSettings(pipe, SID)
            client.connect()
            with self.assertRaises(SettingsUnavailable):
                client.apply("new-model", "xhigh")


if __name__ == "__main__":
    unittest.main()
