from tests import TEST_ROOT
import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex2github import conversation_router as router
from codex2github.session_sync import chat_link


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=router.BASE)
        self.root = Path(self.temp.name)
        self.sid = "00000000-0000-0000-0000-000000000001"
        self.other = "00000000-0000-0000-0000-000000000002"
        (self.root / "config.toml").write_text('model = "test-model"\nmodel_provider = "test-route"\n', encoding="utf-8")
        (self.root / "cc-switch-model-catalog.json").write_text(
            json.dumps({"models": [{"slug": "test-model"}, {"slug": "glm-5.3-flashx"}]}), encoding="utf-8")
        self.records = {self.sid: {"display_title": "原中文会话", "model": router.MODEL}, self.other: {"display_title": "另一个会话"}}
        with patch.object(router, "threads", return_value=self.records), patch.object(router, "model_catalog", return_value=[router.MODEL, "glm-5.3-flashx"]):
            self.router = router.Router(self.root, self.root / "state.json")
        self.router.state["issues"]["8"] = {"sid": self.sid, "cursor": 0, "queue": [], "count": 0}
        self.router.state["rooms"][self.sid] = 8
        self.replies = []
        self.router.reply = lambda number, text: self.replies.append((number, text))
        # Unit routes capture card content without writing to the real repository.
        self.router.publish_steer_card = lambda number, comment: self.replies.append((number, self.router.steer_card_body(number, comment)))

    def tearDown(self):
        self.temp.cleanup()

    def test_session_name_and_unique_prefix_select_exact_existing_id(self):
        self.assertEqual(router.resolve("原中文", self.records), self.sid)
        self.assertEqual(router.resolve(self.other, self.records), self.other)
        with self.assertRaisesRegex(ValueError, "多个"):
            router.resolve("00000000", self.records)

    def test_model_commands_persist_and_capture_queue_in_order(self):
        self.router.handle(8, "先回答第一条", 1)
        self.router.handle(8, "/model glm-5.3-flashx", 2)
        self.router.handle(8, "再回答第二条", 3)
        queue = self.router.state["issues"]["8"]["queue"]
        self.assertEqual([row["model"] for row in queue], [router.MODEL, "glm-5.3-flashx"])
        self.assertEqual([row["sid"] for row in queue], [self.sid, self.sid])
        self.assertIn("下一条", self.replies[0][1])
        self.assertEqual(json.loads((self.root / "state.json").read_text(encoding="utf-8"))["models"][self.sid], "glm-5.3-flashx")
        with self.assertRaises(ValueError): self.router.handle(8, "/model unknown-model", 4)
        self.assertEqual(len(queue), 2)

    def test_hub_selection_then_plain_comment_routes_same_thread(self):
        self.router.state["issues"]["9"] = {"cursor": 0, "queue": [], "count": 0}
        self.router.handle(9, "/session 原中文会话", 1)
        self.router.handle(9, "沿用原来的上下文", 2)
        request = self.router.state["issues"]["9"]["queue"][0]
        self.assertEqual(request["sid"], self.sid)
        self.assertIn("issues/8", self.replies[0][1])

    def test_chinese_model_command_is_scoped_to_one_conversation(self):
        self.router.handle(8, "切换模型为glm-5.3-flashx", 1)
        self.assertEqual(self.router.current_model(self.router.state["issues"]["8"]), "glm-5.3-flashx")
        self.assertNotIn("default_model", self.router.state)

    def test_reasoning_effort_is_scoped_to_conversation_and_captured_in_queue(self):
        self.router.state["issues"]["9"] = {"sid": self.other, "queue": []}
        before = (self.root / "config.toml").read_bytes()
        self.router.handle(8, "/effort xhigh", 4101)
        self.router.handle(8, "任务文字保持原样", 4102)
        self.assertEqual(self.router.state["efforts"][self.sid], "xhigh")
        self.assertEqual(self.router.state["issues"]["8"]["queue"][0]["effort"], "xhigh")
        self.assertEqual(self.router.state["issues"]["8"]["queue"][0]["text"], "任务文字保持原样")
        self.assertNotIn(self.other, self.router.state["efforts"])
        self.assertEqual((self.root / "config.toml").read_bytes(), before)

    def test_native_queued_worker_with_running_desktop_turn_does_not_queue_steer(self):
        directory = self.root / "native-job"
        directory.mkdir()
        (directory / "run-result.json").write_text(json.dumps({"queued": True, "transport": "codex-queue", "sid": self.sid}), encoding="utf-8")
        self.router.state["jobs"]["8"] = {"sid": self.sid, "directory": str(directory), "transport": "appserver", "comment": 99}
        with patch.object(self.router, "busy", return_value=True), patch.object(router, "active_turn", create=True, return_value={"status": "active", "turn_id": "native-turn"}), patch.object(router, "steer_existing_thread", create=True, return_value={"ok": True, "turn_id": "native-turn"}) as deliver:
            self.router.handle(8, "/steer 立即补充正在执行的任务", 4141)
        self.assertEqual(self.router.state["issues"]["8"]["queue"], [])
        deliver.assert_called_once()
        self.assertEqual(deliver.call_args.args[:2], (self.sid, "立即补充正在执行的任务"))
        self.assertEqual(deliver.call_args.args[3], "native-turn")

    def test_desktop_owned_turn_steers_once_without_a_bridge_worker(self):
        with patch.object(router, "active_turn", return_value={"status": "active", "turn_id": "native-turn"}), patch.object(router, "steer_existing_thread", return_value={"ok": True, "turn_id": "native-turn"}) as deliver:
            self.router.handle(8, "/steer 原始引导文字", 4142)
            self.router.handle(8, "/steer 原始引导文字", 4142)
        deliver.assert_called_once()
        self.assertEqual(self.router.state["issues"]["8"]["queue"], [])
        self.assertTrue(self.router.state["issues"]["8"]["steers"]["4142"]["ok"])

    def test_native_steer_timeout_does_not_queue_or_retry(self):
        with patch.object(router, "active_turn", return_value={"status": "active", "turn_id": "native-turn"}), patch.object(router, "steer_existing_thread", return_value={"ok": False, "uncertain": True, "error": "response lost"}) as deliver:
            self.router.handle(8, "/steer 原始引导文字", 4143)
            self.router.handle(8, "/steer 原始引导文字", 4143)
        deliver.assert_called_once()
        self.assertEqual(self.router.state["issues"]["8"]["queue"], [])
        self.assertTrue(self.router.state["issues"]["8"]["steers"]["4143"]["uncertain"])

    def test_model_and_effort_can_be_selected_together(self):
        self.router.handle(8, "/model glm-5.3-flashx high", 4111)
        self.assertEqual(self.router.current_model(self.router.state["issues"]["8"]), "glm-5.3-flashx")
        self.assertEqual(self.router.state["efforts"][self.sid], "high")

    def test_effort_auto_and_model_change_clear_only_target_override(self):
        self.router.state["efforts"][self.other] = "medium"
        self.router.handle(8, "/effort xhigh", 4121)
        self.router.handle(8, "/effort auto", 4122)
        self.assertNotIn(self.sid, self.router.state["efforts"])
        self.router.handle(8, "/effort high", 4123)
        self.router.handle(8, "/model glm-5.3-flashx", 4124)
        self.assertNotIn(self.sid, self.router.state["efforts"])
        self.assertEqual(self.router.state["efforts"][self.other], "medium")

    def test_model_directory_and_status_show_supported_reasoning_levels(self):
        (self.root / "cc-switch-model-catalog.json").write_text(json.dumps({"models": [
            {"slug": "test-model", "default_reasoning_level": "high", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}, {"effort": "ultra"}]}
        ]}), encoding="utf-8")
        self.router.handle(8, "/models", 4131)
        self.assertIn("思考档位", self.replies[-1][1])
        self.assertIn("low / high / ultra", self.replies[-1][1])
        with self.assertRaisesRegex(ValueError, "不支持"):
            self.router.handle(8, "/effort medium", 4132)
        self.router.handle(8, "/effort ultra", 4133)
        self.router.handle(8, "/status", 4134)
        self.assertIn("思考档位：`ultra`", self.replies[-1][1])

    def test_route_change_invalidates_every_conversation_override_and_queued_model(self):
        self.router.state["issues"]["9"] = {"sid": self.other, "queue": []}
        self.router.handle(8, "/model glm-5.3-flashx", 1)
        self.router.handle(8, "旧路由排队消息", 2)
        self.router.handle(9, "/model glm-5.3-flashx", 3)
        self.router.handle(9, "另一个会话消息", 4)
        self.router.handle(8, "/effort ultra", 5)
        self.router.handle(8, "旧档位排队消息", 6)
        self.router.handle(9, "/effort medium", 7)
        (self.root / "config.toml").write_text('model = "new-route-model"\nmodel_provider = "new-route"\n', encoding="utf-8")
        self.assertEqual(self.router.current_model(self.router.state["issues"]["8"]), "new-route-model")
        self.assertEqual(self.router.current_model(self.router.state["issues"]["9"]), "new-route-model")
        self.assertEqual(self.router.state["models"], {})
        self.assertEqual(self.router.state["efforts"], {})
        self.assertTrue(all(item["model"] == "auto" for item in self.router.state["issues"]["8"]["queue"]))
        self.assertTrue(all(item["effort"] == "auto" for item in self.router.state["issues"]["8"]["queue"]))

    def test_route_change_keeps_running_job_model_for_result_without_restoring_override(self):
        self.router.handle(8, "/model glm-5.3-flashx", 1)
        self.router.state["jobs"]["8"] = {"pid": 0, "sid": self.sid, "model": "glm-5.3-flashx",
                                             "directory": str(self.root), "comment": 1}
        (self.root / "config.toml").write_text('model = "new-route-model"\n', encoding="utf-8")
        self.router.sync_route()
        self.assertEqual(self.router.current_model(self.router.state["issues"]["8"]), "new-route-model")
        self.assertEqual(self.router.state["models"], {})

    def test_model_list_uses_current_route_snapshot_only(self):
        catalog = self.root / "cc-switch-model-catalog.json"
        catalog.write_text(json.dumps({"models": [{"slug": "new-route-model"}, {"slug": "route-only"}]}), encoding="utf-8")
        (self.root / "models_cache.json").write_text(json.dumps({"models": [{"slug": "stale-model"}]}), encoding="utf-8")
        self.assertEqual(router.model_catalog(self.root), ["new-route-model", "route-only", "test-model"])

    def test_model_list_reads_active_cc_switch_provider_catalog_without_using_inactive_providers(self):
        codex_home = self.root / "codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text('model = "test-model"\n', encoding="utf-8")
        (codex_home / "cc-switch-model-catalog.json").write_text(json.dumps({"models": []}), encoding="utf-8")
        cc_switch = codex_home.parent / ".cc-switch"
        cc_switch.mkdir()
        (cc_switch / "settings.json").write_text(json.dumps({"currentProviderCodex": "active-provider"}), encoding="utf-8")
        (cc_switch / "live-state.json").write_text(json.dumps({"apps": {"codex": {"mode": "direct"}}}), encoding="utf-8")
        with contextlib.closing(sqlite3.connect(cc_switch / "cc-switch.db")) as connection:
            connection.execute("CREATE TABLE providers (id TEXT, app_type TEXT, settings_config TEXT)")
            connection.executemany("INSERT INTO providers VALUES (?, ?, ?)", [
                ("active-provider", "codex", json.dumps({"config": 'model = "test-model"', "modelCatalog": {
                    "models": [{"model": "route-model-a"}, {"model": "route-model-b"}]}})),
                ("inactive-provider", "codex", json.dumps({"config": 'model = "stale-model"', "modelCatalog": {
                    "models": [{"model": "stale-model"}]}})),
            ])
            connection.commit()
        self.assertEqual(
            router.model_catalog(codex_home),
            ["route-model-a", "route-model-b", "test-model"],
        )

    def test_model_list_does_not_merge_saved_stack_members_into_a_direct_route(self):
        codex_home = self.root / "codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text('model = "stack-default"\n', encoding="utf-8")
        (codex_home / "cc-switch-model-catalog.json").write_text(json.dumps({
            "models": [{"slug": "ccs-active/route-a"}]
        }), encoding="utf-8")
        cc_switch = codex_home.parent / ".cc-switch"
        cc_switch.mkdir()
        (cc_switch / "settings.json").write_text(json.dumps({
            "currentProviderCodex": "stack-a", "enableStackMode": False
        }), encoding="utf-8")
        (cc_switch / "live-state.json").write_text(json.dumps({"apps": {"codex": {
            "mode": "direct", "stack": {"enabled": True, "members": ["stack-a", "stack-b"]}
        }}}), encoding="utf-8")
        with contextlib.closing(sqlite3.connect(cc_switch / "cc-switch.db")) as connection:
            connection.execute("CREATE TABLE providers (id TEXT, app_type TEXT, settings_config TEXT)")
            connection.executemany("INSERT INTO providers VALUES (?, ?, ?)", [
                ("stack-a", "codex", json.dumps({"config": 'model = "stack-default"', "modelCatalog": {
                    "models": [{"model": "route-a"}]}})),
                ("stack-b", "codex", json.dumps({"config": 'model = "stack-b-default"', "modelCatalog": {
                    "models": [{"model": "route-b"}]}})),
                ("inactive", "codex", json.dumps({"config": 'model = "inactive"', "modelCatalog": {
                    "models": [{"model": "inactive"}]}})),
            ])
            connection.commit()
        self.assertEqual(
            router.model_catalog(codex_home),
            ["route-a", "stack-default"],
        )

    def test_models_refreshes_route_directory_without_changing_conversation_overrides(self):
        self.router.state["issues"]["9"] = {"sid": self.other, "queue": []}
        self.router.state["models"][self.sid] = "glm-5.3-flashx"
        with patch.object(router, "model_catalog", return_value=["route-model-a", "route-model-b", "test-model"]) as catalog:
            self.router.handle(8, "/models", 5001)
        catalog.assert_called_once_with(self.root, refresh=True)
        self.assertIn("route-model-a", self.replies[-1][1])
        self.assertIn("route-model-b", self.replies[-1][1])
        self.assertEqual(self.router.current_model(self.router.state["issues"]["8"]), "glm-5.3-flashx")
        self.assertEqual(self.router.current_model(self.router.state["issues"]["9"]), "test-model")
        self.assertEqual(self.router.state["issues"]["8"]["queue"], [])

    def test_chat_link_reuses_existing_room_and_prefills_exact_id(self):
        self.assertEqual(chat_link(self.sid, "原会话", {self.sid: 8}), "https://github.com/example-user/private-tasks/issues/8")
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(chat_link(self.sid, "原会话")).query)
        self.assertIn(self.sid, query["body"][0])
        self.assertEqual(query["labels"], ["codex-session"])

    def test_busy_local_turn_waits_without_interrupting(self):
        with contextlib.closing(sqlite3.connect(self.root / "thread_history_1.sqlite")) as c, c:
            c.execute("CREATE TABLE thread_turns(thread_id TEXT,status TEXT,started_at INTEGER,rollout_ordinal INTEGER)")
            import time
            c.execute("INSERT INTO thread_turns VALUES(?,?,?,1)", (self.sid, "inProgress", int(time.time())))
        self.assertTrue(self.router.busy(8, self.sid))
        with contextlib.closing(sqlite3.connect(self.root / "thread_history_1.sqlite")) as c, c:
            c.execute("UPDATE thread_turns SET status='completed'")
        self.assertFalse(self.router.busy(8, self.sid))

    def test_idle_thread_without_an_active_turn_is_not_busy(self):
        with contextlib.closing(sqlite3.connect(self.root / "thread_history_1.sqlite")) as c, c:
            c.execute("CREATE TABLE thread_turns(thread_id TEXT,status TEXT,started_at INTEGER,rollout_ordinal INTEGER)")
            c.execute("INSERT INTO thread_turns VALUES(?,?,?,1)", (self.sid, "completed", 0))
        self.assertFalse(self.router.busy(8, self.sid))

    def test_queued_receipt_does_not_remove_job_before_native_turn_finishes(self):
        directory = self.root / "queued-job"
        directory.mkdir()
        (directory / "prompt.md").write_text("远程任务", encoding="utf-8")
        (directory / "completion.json").write_text(json.dumps({
            "sid": self.sid, "queued": True, "replied": True, "ok": True,
            "queue_message_id": "native-message", "transport": "codex-queue"
        }), encoding="utf-8")
        path = self.root / "live.jsonl"
        path.write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "remote-turn", "item": {
                "type": "UserMessage", "id": "user-id", "client_id": "native-message",
                "content": [{"type": "Text", "text": "远程任务"}]}}}) + "\n", encoding="utf-8")
        self.records[self.sid]["rollout_path"] = str(path)
        self.router.state["jobs"]["8"] = {"pid": 0, "sid": self.sid, "directory": str(directory),
                                          "comment": 5002, "model": "test-model", "request": {"text": "远程任务"}}
        self.router.reap()
        self.assertIn("8", self.router.state["jobs"])
        self.assertEqual(self.replies, [])
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {
                "type": "item_completed", "turn_id": "remote-turn", "item": {
                    "type": "AgentMessage", "id": "answer-id", "phase": "final_answer",
                    "content": [{"type": "Text", "text": "真实最终回复"}]}}}) + "\n")
            stream.write(json.dumps({"type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": "remote-turn"}}) + "\n")
        self.router.reap()
        self.assertNotIn("8", self.router.state["jobs"])
        self.assertIn("真实最终回复", self.replies[-1][1])
        self.router.reap()
        self.assertEqual(len(self.replies), 1)

    def test_completed_queue_result_survives_a_lost_post_response_without_duplicate(self):
        directory = self.root / "result-job"
        directory.mkdir()
        result = {"sid": self.sid, "queued": True, "execution_complete": True, "replied": False,
                  "ok": True, "model": "test-model", "final": "已完成"}
        (directory / "completion.json").write_text(json.dumps(result), encoding="utf-8")
        self.router.state["jobs"]["8"] = {"pid": 0, "sid": self.sid, "directory": str(directory), "comment": 5010, "model": "test-model"}
        with patch.object(self.router, "reply", side_effect=RuntimeError("POST response lost")) as post:
            with self.assertRaises(RuntimeError):
                self.router.reap()
        self.assertIn("8", self.router.state["jobs"])
        marker = router.BOT + "\n<!-- codex2github:result 5010 -->"
        with patch.object(router, "page_list", return_value=[{"id": 901, "body": marker + "\n已完成"}]), patch.object(router, "api", return_value={"id": 901}) as remote, patch.object(self.router, "reply") as duplicate:
            self.router.reap()
        duplicate.assert_not_called()
        self.assertEqual(remote.call_args.args[1], "PATCH")
        self.assertNotIn("8", self.router.state["jobs"])
        self.assertEqual(json.loads((directory / "completion.json").read_text(encoding="utf-8"))["reply_id"], 901)

    def test_legacy_submitted_artifact_is_recovered_without_launching_task(self):
        directory = self.root / "jobs" / "8-5011"
        directory.mkdir(parents=True)
        (directory / "completion.json").write_text(json.dumps({"sid": self.sid, "queued": True, "replied": True, "reply_id": 902}), encoding="utf-8")
        with patch.object(self.router, "launch") as launch:
            self.router.recover_queued_results()
        launch.assert_not_called()
        self.assertEqual(self.router.state["jobs"]["8"]["comment"], 5011)
        self.assertEqual(self.router.state["jobs"]["8"]["pid"], 0)

    def test_retryable_active_writer_result_returns_request_to_front_of_queue(self):
        directory = self.root / "retryable-job"
        directory.mkdir()
        request = {"comment": 8801, "text": "远程任务", "sid": self.sid, "model": router.MODEL}
        (directory / "completion.json").write_text(json.dumps({
            "sid": self.sid, "model": router.MODEL, "ok": False,
            "replied": False, "retryable": True,
            "error": "thread abc already has an active writer",
        }), encoding="utf-8")
        self.router.state["jobs"]["8"] = {
            "pid": 0, "sid": self.sid, "model": router.MODEL,
            "directory": str(directory), "comment": 8801,
            "request": request,
        }
        self.router.reap()
        self.assertEqual(self.router.state["issues"]["8"]["queue"], [request])
        self.assertNotIn("8", self.router.state["jobs"])
        self.assertIn("自动等待", self.replies[-1][1])

    def test_desktop_wait_keeps_exact_request_and_sends_one_notice(self):
        directory = self.root / "desktop-wait"
        directory.mkdir()
        request = {"comment": 8802, "text": "unchanged task", "sid": self.sid, "model": router.MODEL}
        (directory / "completion.json").write_text(json.dumps({"sid": self.sid, "ok": False,
            "retryable": True, "retry_reason": "desktop-unavailable"}), encoding="utf-8")
        job = {"pid": 0, "sid": self.sid, "model": router.MODEL, "directory": str(directory),
               "comment": 8802, "request": request}
        for _ in range(2):
            self.router.state["jobs"]["8"] = job.copy()
            with patch.object(router.time, "time", return_value=100):
                self.router.reap()
        pending = self.router.state["issues"]["8"]["queue"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["text"], "unchanged task")
        self.assertGreaterEqual(pending[0]["retry_after"], 130)
        self.assertEqual(len(self.replies), 1)
        self.assertIn("等待重连", self.replies[0][1])

    def test_native_submission_crash_follows_durable_attempt_without_replay(self):
        directory = self.root / "uncertain-send"
        directory.mkdir()
        attempt = {"sid": self.sid, "model": router.MODEL, "queued": True, "ok": False,
                   "submission_uncertain": True, "execution_complete": False, "rollout_offset": 123}
        (directory / "submission-attempt.json").write_text(json.dumps(attempt), encoding="utf-8")
        self.router.state["jobs"]["8"] = {"pid": 0, "sid": self.sid, "model": router.MODEL,
            "directory": str(directory), "comment": 8803, "request": {"text": "original"}}
        with patch.object(router, "follow_queued_result", return_value=None) as follow, \
             patch.object(self.router, "launch") as launch:
            self.router.reap()
        follow.assert_called_once()
        self.assertEqual(follow.call_args.args[1]["rollout_offset"], 123)
        self.assertIn("8", self.router.state["jobs"])
        launch.assert_not_called()
        self.assertEqual(self.replies, [])

    def test_retry_launch_removes_stale_wait_record_before_starting_worker(self):
        request = {"comment": 8804, "text": "original", "sid": self.sid, "model": router.MODEL}
        directory = self.root / "jobs" / "8-8804"
        directory.mkdir(parents=True)
        for name in ("completion.json", "run-result.json"):
            (directory / name).write_text(json.dumps({"retryable": True}), encoding="utf-8")
        def start(*args, **kwargs):
            self.assertFalse((directory / "completion.json").exists())
            self.assertFalse((directory / "run-result.json").exists())
            from unittest.mock import Mock
            return Mock(pid=123)
        with patch.object(router, "BASE", self.root), patch.object(router, "WORKSPACE", self.root), \
             patch.object(router.subprocess, "Popen", side_effect=start):
            self.router.launch(8, request)
        self.assertEqual(self.router.state["jobs"]["8"]["transport"], "codex-queue")

    def test_close_requires_a_standalone_explicit_command(self):
        with patch.object(router, "api") as remote:
            self.router.handle(8, "普通留言，请继续回复", 1)
            remote.assert_not_called()
            self.assertEqual(len(self.router.state["issues"]["8"]["queue"]), 1)
            with self.assertRaises(ValueError): self.router.handle(8, "/close\n顺便继续任务", 2)
            remote.assert_not_called()
            self.router.handle(8, "/close", 3)
            remote.assert_called_once_with("repos/example-user/private-tasks/issues/8", "PATCH", {"state": "closed"})
        self.assertIn("按你的指令", self.replies[-1][1])
        self.assertEqual(router.command("关闭这个任务"), ("close", "", ""))
        self.assertIsNone(router.command("任务完成后不要关闭这个任务"))

    def test_autostart_can_be_controlled_from_issue_without_creating_a_task(self):
        with patch.object(router, "manage_autostart", return_value={"enabled": True, "running": True}) as startup:
            self.router.handle(8, "/autostart on", 801)
            startup.assert_called_with(router.SETTINGS, "enable", start=True)
            self.assertIn("已开启", self.replies[-1][1])
            self.router.handle(8, "/autostart off", 802)
            startup.assert_called_with(router.SETTINGS, "disable", start=False)
            self.router.handle(8, "/autostart", 803)
            startup.assert_called_with(router.SETTINGS, "status", start=False)
            before = startup.call_count
            for text in ("/autostart on\n顺便执行任务", "/autostart stop"):
                with self.assertRaises(ValueError):
                    self.router.handle(8, text, 804)
            self.assertEqual(startup.call_count, before)
        self.assertEqual(self.router.state["issues"]["8"]["queue"], [])

    def test_non_owner_issue_comment_cannot_enable_local_autostart(self):
        self.router.state["issues"]["8"].update(body_done=True)
        issue = {"number": 8, "title": "已有会话", "body": "", "labels": [], "comments": 2}
        comments = [{"id": 801, "body": "/autostart on", "user": {"login": "someone-else"}},
                    {"id": 802, "body": "/autostart", "user": {"login": "example-user"}}]
        with patch.object(router, "api", return_value={"private": True, "owner": {"login": "example-user"}}), patch.object(router, "page_list", side_effect=lambda endpoint: comments if endpoint.endswith("/comments") else [issue]), patch.object(self.router, "reap"), patch.object(router, "manage_autostart", return_value={}) as startup:
            self.router.tick()
        startup.assert_called_once_with(router.SETTINGS, "status", start=False)

    def test_cli_completion_does_not_change_issue_state(self):
        output = self.root / "job"
        output.mkdir()
        (output / "completion.json").write_text(json.dumps({"sid": self.sid, "ok": True, "replied": True}), encoding="utf-8")
        self.router.state["jobs"]["8"] = {"pid": 0, "sid": self.sid, "model": router.MODEL, "directory": str(output), "comment": 99}
        with patch.object(router, "api") as remote:
            self.router.reap()
            remote.assert_not_called()
        self.assertNotIn("8", self.router.state["jobs"])

    def test_dead_worker_with_durable_result_is_recovered_before_next_queued_task(self):
        directory = self.root / "shutdown-job"
        directory.mkdir()
        (directory / "run-result.json").write_text(json.dumps({"sid": self.sid, "turn_id": "turn",
            "ok": True, "final": "已完成的真实结果", "model": router.MODEL, "transport": "appserver"}), encoding="utf-8")
        self.router.state["jobs"]["8"] = {"pid": 12, "sid": self.sid, "model": router.MODEL,
                                         "directory": str(directory), "comment": 123}
        self.router.state["issues"]["8"]["queue"] = [{"comment": 124, "text": "后续任务"}]
        with patch.object(router, "process_alive", return_value=False):
            self.router.reap()
            self.router.reap()
        self.assertEqual(len(self.replies), 1)
        self.assertIn("已完成的真实结果", self.replies[0][1])
        self.assertNotIn("未找到完成记录", self.replies[0][1])
        self.assertNotIn("8", self.router.state["jobs"])
        self.assertEqual([q["comment"] for q in self.router.state["issues"]["8"]["queue"]], [124])

    def test_owner_new_issue_without_label_accepts_model_and_first_message(self):
        issue = {"number": 10, "title": "创建新会话", "body": "/model " + router.MODEL, "labels": [], "state": "open", "comments": 0, "user": {"login": "example-user"}}
        self.router.records = self.records
        comments = []
        def pages(endpoint):
            return comments if endpoint.endswith("/comments") else [issue]
        with patch.object(router, "threads", return_value=self.records), patch.object(router, "api", return_value={"private": True, "owner": {"login": "example-user"}}), patch.object(router, "page_list", side_effect=pages), patch.object(self.router, "reap"), patch.object(self.router, "busy", return_value=True):
            self.router.tick()
            self.assertIn("10", self.router.state["issues"])
            info = self.router.state["issues"]["10"]
            self.assertTrue(info["new_task"])
            self.assertEqual(info["model"], router.MODEL)
            self.assertEqual(info["queue"], [])  # A model command isn't a model request.
            self.assertTrue(self.replies)
            issue["comments"] = 1
            comments.append({"id": 123, "user": {"login": "example-user"}, "body": "你好，开始这条新会话"})
            self.router.tick()
            self.router.tick()
            self.assertEqual(len(info["queue"]), 1)
            self.assertEqual(info["queue"][0]["text"], "你好，开始这条新会话")
            self.assertIsNone(info["queue"][0]["sid"])
            self.assertEqual(info["queue"][0]["model"], router.MODEL)

    def test_owner_unlabeled_task_body_queues_once(self):
        issue = {"number": 10, "title": "新任务", "body": "请处理这条任务", "labels": [], "state": "open", "comments": 0, "user": {"login": "example-user"}}
        def pages(endpoint): return [] if endpoint.endswith("/comments") else [issue]
        with patch.object(router, "threads", return_value=self.records), patch.object(router, "api", return_value={"private": True, "owner": {"login": "example-user"}}), patch.object(router, "page_list", side_effect=pages), patch.object(self.router, "reap"), patch.object(self.router, "busy", return_value=True):
            self.router.tick()
            self.router.tick()
        self.assertEqual(len(self.router.state["issues"]["10"]["queue"]), 1)
        self.assertEqual(self.router.state["issues"]["10"]["queue"][0]["text"], "请处理这条任务")

    def test_labeled_new_issue_accepts_effort_then_first_task_once(self):
        issue = {"number": 10, "title": "新会话", "body": None, "labels": [{"name": "codex-session"}],
                 "state": "open", "comments": 0, "user": {"login": "example-user"}}
        comments = []
        def pages(endpoint): return comments if endpoint.endswith("/comments") else [issue]
        with patch.object(router, "threads", return_value=self.records), patch.object(router, "api", return_value={"private": True, "owner": {"login": "example-user"}}), patch.object(router, "page_list", side_effect=pages), patch.object(self.router, "reap"), patch.object(self.router, "busy", return_value=True):
            self.router.tick()
            comments.extend([{"id": 121, "body": "/effort xhigh", "user": {"login": "example-user"}},
                             {"id": 122, "body": "执行这条新任务，保持原文", "user": {"login": "example-user"}}])
            issue["comments"] = 2
            self.router.tick()
            self.router.tick()
        queue = self.router.state["issues"]["10"]["queue"]
        self.assertEqual(len(queue), 1)
        self.assertIsNone(queue[0]["sid"])
        self.assertEqual(queue[0]["text"], "执行这条新任务，保持原文")
        self.assertEqual(queue[0]["effort"], "xhigh")

    def test_existing_unbound_labeled_issue_is_migrated_without_replaying_old_comments(self):
        self.router.state["issues"]["10"] = {"cursor": 121, "queue": [], "count": 1,
                                              "body_done": True, "model": router.MODEL, "effort": "xhigh"}
        issue = {"number": 10, "title": "新会话", "body": None, "labels": [{"name": "codex-session"}],
                 "state": "open", "comments": 2, "user": {"login": "example-user"}}
        comments = [{"id": 121, "body": "此前已处理的消息", "user": {"login": "example-user"}},
                    {"id": 122, "body": "当前新任务", "user": {"login": "example-user"}}]
        def pages(endpoint): return comments if endpoint.endswith("/comments") else [issue]
        with patch.object(router, "threads", return_value=self.records), patch.object(router, "api", return_value={"private": True, "owner": {"login": "example-user"}}), patch.object(router, "page_list", side_effect=pages), patch.object(self.router, "reap"), patch.object(self.router, "busy", return_value=True):
            self.router.tick()
            self.router.tick()
        queue = self.router.state["issues"]["10"]["queue"]
        self.assertEqual([row["comment"] for row in queue], [122])
        self.assertEqual(queue[0]["effort"], "xhigh")
        self.assertEqual(self.router.state["issues"]["8"]["sid"], self.sid)

    def test_unbound_hub_still_requires_an_explicit_existing_session(self):
        self.router.state["hub"] = 10
        self.router.state["issues"]["10"] = {"cursor": 0, "queue": [], "count": 0, "body_done": True}
        issue = {"number": 10, "title": "会话入口", "body": None, "labels": [{"name": "codex-session"}],
                 "state": "open", "comments": 1, "user": {"login": "example-user"}}
        def pages(endpoint):
            return [{"id": 122, "body": "未选择已有会话", "user": {"login": "example-user"}}] if endpoint.endswith("/comments") else [issue]
        with patch.object(router, "threads", return_value=self.records), patch.object(router, "api", return_value={"private": True, "owner": {"login": "example-user"}}), patch.object(router, "page_list", side_effect=pages), patch.object(self.router, "reap"), patch.object(self.router, "refresh_hub"), patch.object(self.router, "busy", return_value=True):
            self.router.tick()
        self.assertFalse(self.router.state["issues"]["10"].get("new_task"))
        self.assertEqual(self.router.state["issues"]["10"]["queue"], [])
        self.assertIn("/session", self.replies[-1][1])

    def test_unregistered_foreign_closed_and_pull_request_are_not_started(self):
        base = {"title": "非任务", "body": "不能执行", "labels": [], "state": "open", "comments": 0, "user": {"login": "example-user"}}
        issues = [{**base, "number": 10, "user": {"login": "someone-else"}, "labels": [{"name": "codex-task"}]}, {**base, "number": 11, "state": "closed"}, {**base, "number": 12, "pull_request": {}}]
        def pages(endpoint): return [] if endpoint.endswith("/comments") else issues
        with patch.object(router, "threads", return_value=self.records), patch.object(router, "api", return_value={"private": True, "owner": {"login": "example-user"}}), patch.object(router, "page_list", side_effect=pages), patch.object(self.router, "reap"), patch.object(self.router, "busy", return_value=True):
            self.router.tick()
        self.assertTrue(all(str(number) not in self.router.state["issues"] for number in (10, 11, 12)))

    def test_invalid_initial_model_does_not_block_other_new_issues(self):
        base = {"title": "新任务", "labels": [], "state": "open", "comments": 0, "user": {"login": "example-user"}}
        issues = [{**base, "number": 10, "body": "/model unknown-model"}, {**base, "number": 11, "body": "继续其他任务"}]
        def pages(endpoint): return [] if endpoint.endswith("/comments") else issues
        with patch.object(router, "threads", return_value=self.records), patch.object(router, "api", return_value={"private": True, "owner": {"login": "example-user"}}), patch.object(router, "page_list", side_effect=pages), patch.object(self.router, "reap"), patch.object(self.router, "busy", return_value=True):
            self.router.tick()
            self.router.tick()
        self.assertTrue(self.router.state["issues"]["10"]["body_done"])
        self.assertEqual(self.router.state["issues"]["10"]["queue"], [])
        self.assertEqual(len(self.router.state["issues"]["11"]["queue"]), 1)
        self.assertEqual(sum(number == 10 for number, text in self.replies), 1)

    def test_live_hub_lists_new_sessions_links_and_changes_only_when_needed(self):
        self.router.state["hub"] = 8
        self.router.state["issues"]["8"]["title"] = "入口"
        self.records[self.other].update(updated_at=1700000010, archived=False)
        hub = {"body": "创建时的旧列表", "state": "open"}
        with patch.object(router, "api", return_value={}) as remote:
            self.router.refresh_hub(hub)
            self.assertEqual(remote.call_count, 1)
            endpoint, method, body = remote.call_args.args
            self.assertEqual(endpoint, "repos/example-user/private-tasks/issues/8")
            self.assertEqual(method, "PATCH")
            self.assertEqual(set(body), {"body"})  # Never close/reopen the hub.
            self.assertIn(self.other, body["body"])
            self.assertIn("另一个会话", body["body"])
            self.assertIn("issues/8", body["body"])
            self.assertIn("/session", body["body"])
            hub["body"] = body["body"]
            remote.reset_mock()
            self.router.refresh_hub(hub)
            remote.assert_not_called()
            self.router.records[self.other]["display_title"] = "更新后的名字"
            self.router.refresh_hub(hub)
            self.assertEqual(remote.call_count, 1)
            self.assertIn("更新后的名字", remote.call_args.args[2]["body"])

    def test_active_new_task_session_is_registered_before_completion(self):
        directory = self.root / "live-job"
        directory.mkdir()
        (directory / "result.md").write_text("OpenAI Codex\nmodel: " + router.MODEL + "\nsession id: " + self.other + "\n", encoding="utf-8-sig")
        self.router.state["issues"]["10"] = {"queue": [], "new_task": True, "model": router.MODEL}
        self.router.state["jobs"]["10"] = {"pid": 123, "sid": None, "model": router.MODEL, "directory": str(directory)}
        self.router.discover_job_sessions()
        self.assertEqual(self.router.state["issues"]["10"]["sid"], self.other)
        self.assertEqual(self.router.state["jobs"]["10"]["sid"], self.other)
        self.assertEqual(self.router.state["rooms"][self.other], 10)
        self.assertFalse((directory / "completion.json").exists())
        self.assertIn("issues/10", self.router.render_hub())

    def test_active_new_chat_gets_history_link_on_tick_before_completion(self):
        directory = self.root / "new-history-job"
        directory.mkdir()
        (directory / "result.md").write_text("session id: " + self.other, encoding="utf-8")
        self.router.state["issues"]["10"] = {"queue": [], "new_task": True, "body_done": True,
                                              "count": 0, "cursor": 42}
        self.router.state["jobs"]["10"] = {"sid": None, "directory": str(directory)}
        original = "原始任务\n保留正文和空格  "
        issue = {"number": 10, "title": "新任务", "body": original, "labels": [],
                 "state": "open", "comments": 0, "user": {"login": "example-user"}}
        def api(endpoint, method="GET", body=None):
            if endpoint == router.PREFIX:
                return {"private": True, "owner": {"login": "example-user"}}
            self.assertEqual(endpoint, router.PREFIX + "/issues/10")
            if method == "GET":
                # A user's edit after the issue list was fetched must survive.
                return {**issue, "body": original + "\n最新补充"}
            self.assertEqual(method, "PATCH")
            self.assertEqual(set(body), {"body"})
            issue.update(body)
            return issue
        with patch.object(router, "api", side_effect=api) as remote, patch.object(router, "page_list", return_value=[issue]), patch.object(self.router, "reap"):
            self.router.tick()
            calls = remote.call_count
            self.router.tick()
            self.assertEqual(remote.call_count, calls + 1)  # Metadata only on idle tick.
        self.assertTrue(issue["body"].startswith(original + "\n最新补充\n\n"))
        self.assertIn("查看完整会话记录", issue["body"])
        self.assertIn(f"/sessions/{self.other}/page-001.md", issue["body"])
        self.assertIn("首次同步完成后", issue["body"])
        self.assertFalse((directory / "completion.json").exists())
        self.assertEqual(self.router.state["issues"]["10"]["queue"], [])
        self.assertEqual(self.replies, [])

    def test_completed_new_chat_history_is_repaired_and_rebinding_updates_only_panel(self):
        self.router.state["issues"]["10"] = {"sid": self.sid, "new_task": True, "body_done": True}
        issue = {"number": 10, "body": "任务原文  \n"}
        with patch.object(router, "api", side_effect=lambda *args: dict(issue)) as remote:
            self.router.ensure_history_link(issue)
            self.assertEqual(remote.call_count, 2)
            first = issue["body"]
            remote.reset_mock()
            self.router.ensure_history_link(issue)
            remote.assert_not_called()
            self.router.state["issues"]["10"]["sid"] = self.other
            self.router.ensure_history_link(issue)
        self.assertTrue(issue["body"].startswith("任务原文  \n\n\n"))
        self.assertEqual(issue["body"].count("<!-- codex2github:history:start -->"), 1)
        self.assertIn(self.other, issue["body"])
        self.assertNotIn(self.sid, issue["body"])
        self.assertNotEqual(first, issue["body"])

    def test_history_decoration_never_becomes_a_task_during_binding_recovery(self):
        self.router.state["issues"]["10"] = {"sid": self.sid, "new_task": True, "body_done": True}
        issue = {"number": 10, "title": "新任务", "body": "已经执行过的任务", "labels": [],
                 "state": "open", "comments": 0, "user": {"login": "example-user"}}
        with patch.object(router, "api", return_value=dict(issue)):
            self.router.ensure_history_link(issue)
        del self.router.state["issues"]["10"]
        with patch.object(router, "api", return_value={"private": True, "owner": {"login": "example-user"}}), patch.object(router, "page_list", side_effect=lambda endpoint: [] if endpoint.endswith("/comments") else [issue]), patch.object(self.router, "reap"), patch.object(self.router, "handle") as handle:
            self.router.tick()
        handle.assert_not_called()
        self.assertEqual(self.router.state["issues"]["10"]["sid"], self.sid)
        self.assertEqual(self.router.state["issues"]["10"]["queue"], [])

    def test_history_attachment_skips_hub_old_rooms_and_unconsumed_tasks(self):
        with patch.object(router, "api") as remote:
            self.router.ensure_history_link({"number": 8})
            self.router.state["issues"]["10"] = {"new_task": True, "body_done": True}
            self.router.ensure_history_link({"number": 10})
            self.router.state["issues"]["10"].update(sid=self.other, body_done=False)
            self.router.ensure_history_link({"number": 10})
            self.router.state["issues"]["10"]["body_done"] = True
            self.router.state["hub"] = 10
            self.router.ensure_history_link({"number": 10})
            remote.assert_not_called()

    def test_history_api_failure_does_not_block_following_task_launch(self):
        self.router.state["issues"]["10"] = {"sid": self.other, "new_task": True,
                                              "body_done": True, "count": 0, "queue": []}
        self.router.state["issues"]["8"]["queue"] = [{"sid": self.sid, "text": "继续工作"}]
        issue = {"number": 10, "title": "新任务", "body": None, "labels": [], "comments": 0}
        def api(endpoint, *args):
            if endpoint == router.PREFIX:
                return {"private": True, "owner": {"login": "example-user"}}
            raise RuntimeError("Temporary GitHub failure")
        with patch.object(router, "api", side_effect=api), patch.object(router, "page_list", return_value=[issue]), patch.object(self.router, "reap"), patch.object(self.router, "busy", return_value=False), patch.object(self.router, "launch") as launch:
            self.router.tick()
        launch.assert_called_once_with(8, {"sid": self.sid, "text": "继续工作"})

    def test_metadata_revision_refreshes_index_but_idle_and_history_do_not(self):
        with patch.object(router, "threads", return_value=self.records) as query:
            self.router.load_records()
            query.assert_not_called()
            signal = self.root / "session-events.json"
            signal.write_text(json.dumps({"metadata": "", "history": "new-reply"}), encoding="utf-8")
            self.router.load_records()
            query.assert_not_called()
            signal.write_text(json.dumps({"metadata": "new-session", "history": "new-reply"}), encoding="utf-8")
            self.records[self.other]["display_title"] = "新建的会话"
            self.assertEqual(self.router.load_records()[self.other]["display_title"], "新建的会话")
            query.assert_called_once()
            self.router.load_records()
            query.assert_called_once()

    def test_explicit_queue_keeps_multiline_content_and_captures_current_model(self):
        self.router.handle(8, "/queue 下一轮处理\n保留这一行", 301)
        request = self.router.state["issues"]["8"]["queue"][0]
        self.assertEqual(request["text"], "下一轮处理\n保留这一行")
        self.assertEqual(request["model"], router.MODEL)
        self.assertIn("已排队", self.replies[-1][1])
        self.router.handle(8, "/queue 下一轮处理\n保留这一行", 301)
        self.assertEqual(len(self.router.state["issues"]["8"]["queue"]), 1)
        with self.assertRaises(ValueError):
            self.router.handle(8, "/steer", 302)
        self.router.handle(8, "/queue", 303)
        self.assertIn("301", self.replies[-1][1])
        self.assertEqual(len(self.router.state["issues"]["8"]["queue"]), 1)

    def test_idle_steer_becomes_next_message_and_unknown_active_turn_is_explicit(self):
        with patch.object(router, "active_turn", return_value={"status": "idle"}):
            self.router.handle(8, "/steer 无运行任务时开始处理", 501)
            self.router.handle(8, "/steer 无运行任务时开始处理", 501)
        self.assertEqual(self.router.state["issues"]["8"]["queue"][0]["text"], "无运行任务时开始处理")
        with patch.object(router, "active_turn", return_value={"status": "unknown"}):
            with self.assertRaisesRegex(ValueError, "无法确认"):
                self.router.handle(8, "/steer 修改旧任务", 502)
        self.assertEqual(len(self.router.state["issues"]["8"]["queue"]), 1)

    def test_appserver_steer_reuses_same_thread_job_even_from_another_issue(self):
        directory = self.root / "active-job"
        directory.mkdir()
        (directory / "runtime.json").write_text(json.dumps({"sid": self.sid, "turn_id": "active-turn", "active": True}), encoding="utf-8")
        (directory / "control-outbox").mkdir()
        (directory / "control-outbox/601.json").write_text(json.dumps({"ok": True, "turn_id": "active-turn"}), encoding="utf-8")
        self.router.state["jobs"]["99"] = {"transport": "appserver", "sid": self.sid, "directory": str(directory)}
        self.router.handle(8, "/steer 当前轮次增加要求", 601)
        self.assertEqual(self.router.state["issues"]["8"]["queue"], [])
        control = json.loads((directory / "control-inbox/601.json").read_text(encoding="utf-8"))
        self.assertEqual(control["sid"], self.sid)
        self.assertEqual(control["turn_id"], "active-turn")
        self.assertIn("已引导", self.replies[-1][1])
        self.assertIn("🟢 会话已接收", self.replies[-1][1])

    def test_idle_hub_skips_render_and_patch_but_repairs_external_body_edit(self):
        self.router.state["hub"] = 8
        hub = {"body": "旧列表", "state": "open"}
        with patch.object(router, "api", return_value={}) as remote:
            self.router.refresh_hub(hub)
            hub["body"] = remote.call_args.args[2]["body"]
            remote.reset_mock()
            with patch.object(self.router, "render_hub", wraps=self.router.render_hub) as render:
                self.router.refresh_hub(hub)
                render.assert_not_called()
                remote.assert_not_called()
                hub["body"] = "被修改的正文"
                self.router.refresh_hub(hub)
                render.assert_called_once()
                self.assertEqual(set(remote.call_args.args[2]), {"body"})

    def test_subagents_without_agent_path_appear_only_in_agent_section(self):
        self.records[self.other]["display_title"] = "用户讨论子代理的普通会话"
        cases = [
            {"source": json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": self.sid, "agent_path": None}}})},
            {"source": "exec", "thread_source": "subagent"},
            {"source": json.dumps({"subagent": {"other": "guardian"}})},
        ]
        agent_ids = []
        for index, metadata in enumerate(cases, 1):
            sid = f"10000000-0000-0000-0000-{index:012d}"
            agent_ids.append(sid)
            self.router.records[sid] = {"display_title": f"无路径子代理{index}", "agent_path": None,
                                        "updated_at": 1800000000 + index, **metadata}
        body = self.router.render_hub()
        self.assertIn("<summary>子代理会话", body)
        main, agents = body.split("<summary>子代理会话", 1)
        for sid in agent_ids:
            self.assertNotIn(sid, main)
            self.assertNotIn(sid[:12], main)
            self.assertIn(sid[:12], agents)
        self.assertIn(self.other, main)
        self.assertIn("主会话 2，子代理 3", body)


    def test_queue_list_has_stable_ids_links_and_never_starts_work(self):
        self.router.handle(8, "等待的第一条", 7101)
        self.router.handle(8, "/queue 等待的第二条", 7102)
        with patch.object(self.router, "launch") as launch, patch.object(router, "api") as remote:
            self.router.handle(8, "/queue", 7103)
            launch.assert_not_called()
            remote.assert_not_called()
        body = self.replies[-1][1]
        self.assertIn("#issuecomment-7101", body)
        self.assertIn("/cancel 7102", body)
        self.assertEqual([request["comment"] for request in self.router.pending_queue(8)], [7101, 7102])

    def test_cancel_last_persists_and_replay_cannot_withdraw_a_newer_message(self):
        self.router.handle(8, "第一条", 7201)
        self.router.handle(8, "最近一条", 7202)
        self.router.handle(8, "/cancel", 7203)
        self.assertEqual([request["comment"] for request in self.router.pending_queue(8)], [7201])
        saved = json.loads(self.router.path.read_text(encoding="utf-8"))
        self.assertIn("7202", saved["issues"]["8"]["canceled_messages"])
        self.router.state = saved  # Resume from the persisted state.
        self.router.handle(8, "撤回前的原留言被重放", 7202)
        self.router.handle(8, "后来加入的消息", 7204)
        self.router.handle(8, "/cancel", 7203)
        self.assertEqual([request["comment"] for request in self.router.pending_queue(8)], [7201, 7204])

    def test_cancel_by_id_preserves_other_order_models_and_other_issue(self):
        self.router.handle(8, "第一条", 7301)
        self.router.handle(8, "/model glm-5.3-flashx", 7302)
        self.router.handle(8, "/queue 第二条", 7303)
        self.router.handle(8, "第三条", 7304)
        self.router.state["issues"]["9"] = {"queue": [{"comment": 7305, "text": "其他 Issue", "sid": self.sid, "model": router.MODEL}]}
        first, third = self.router.state["issues"]["8"]["queue"][0::2]
        self.router.handle(8, "/cancel 7303", 7306)
        self.assertEqual(self.router.state["issues"]["8"]["queue"], [first, third])
        self.assertEqual(self.router.state["issues"]["9"]["queue"][0]["comment"], 7305)
        self.router.handle(8, "/queue 第二条重放", 7303)
        self.assertEqual(len(self.router.state["issues"]["8"]["queue"]), 2)

    def test_cancel_all_keeps_running_job_and_its_shadow_queue_entry(self):
        for comment in (7401, 7402, 7403):
            self.router.handle(8, f"消息 {comment}", comment)
        job = {"comment": 7402, "sid": self.sid, "transport": "appserver", "pid": 1}
        self.router.state["jobs"]["8"] = job
        self.assertNotIn("/cancel 7402", self.router.queue_listing(8))
        with patch.object(router, "api") as remote:
            self.router.handle(8, "/cancel 7402", 7404)
            self.assertIn("已经开始执行", self.replies[-1][1])
            self.router.handle(8, "/cancel all", 7405)
            remote.assert_not_called()
        self.assertEqual(self.router.state["issues"]["8"]["queue"][0]["comment"], 7402)
        self.assertEqual(self.router.pending_queue(8), [])
        self.assertIs(self.router.state["jobs"]["8"], job)
        self.assertNotIn("7402", self.router.state["issues"]["8"]["canceled_messages"])

    def test_invalid_empty_and_replayed_empty_cancellation_are_safe(self):
        self.router.handle(8, "/cancel", 7501)
        self.router.handle(8, "以后加入的消息", 7502)
        self.router.handle(8, "/cancel", 7501)
        for text in ("/cancel unknown", "/cancel -1", "/cancel all\n顺便关闭任务"):
            with self.assertRaises(ValueError):
                self.router.handle(8, text, 7503)
        self.router.handle(8, "/cancel 999999", 7504)
        self.assertEqual(self.router.pending_queue(8)[0]["comment"], 7502)
        self.assertEqual(len(self.router.pending_queue(8)), 1)

    def test_idle_steer_withdrawal_updates_receipt_and_handles_body_id_zero(self):
        with patch.object(router, "active_turn", return_value={"status": "idle"}):
            self.router.handle(8, "/steer 下一条处理", 7601)
        self.router.handle(8, "/cancel 7601", 7602)
        self.assertTrue(self.router.state["issues"]["8"]["steers"]["7601"]["canceled"])
        self.router.handle(8, "尚未启动的 Issue 正文", 0)
        self.router.handle(8, "/cancel 0", 7603)
        self.assertEqual(self.router.pending_queue(8), [])

    def test_same_poll_withdrawal_prevents_dispatch_and_keeps_remaining_message(self):
        info = self.router.state["issues"]["8"]
        info.update(body_done=True, title="原 Issue")
        issue = {"number": 8, "title": "原 Issue", "body": "", "labels": [], "state": "open", "comments": 3, "user": {"login": "example-user"}}
        comments = [{"id": comment, "body": text, "user": issue["user"]} for comment, text in ((7701, "保留这一条"), (7702, "/queue 要撤回的消息"), (7703, "/cancel 7702"))]
        def pages(endpoint):
            return comments if endpoint.endswith("/comments") else [issue]
        with patch.object(router, "api", return_value={"private": True, "owner": {"login": "example-user"}}), patch.object(router, "page_list", side_effect=pages), patch.object(self.router, "busy", return_value=False), patch.object(self.router, "launch") as launch:
            self.router.tick()
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[1]["comment"], 7701)
        self.assertEqual(self.router.pending_queue(8), [])
        self.assertIn("7702", info["canceled_messages"])


    def test_delivery_card_updates_same_comment_and_skips_unchanged_writes(self):
        del self.router.publish_steer_card
        receipt = {"sid": self.sid, "text": "补充要求", "pending": True, "observed_at": "2026-10-13T10:30:00+08:00"}
        self.router.state["issues"]["8"]["steers"] = {"801": receipt}
        with patch.object(router, "api", return_value={"id": 9001}) as remote:
            self.router.publish_steer_card(8, 801)
            self.assertEqual(remote.call_args_list[0].args[1], "POST")
            self.assertIn("👀 监听器已读取", remote.call_args_list[0].args[2]["body"])
            self.assertEqual(receipt["visual_comment"], 9001)
            remote.reset_mock()
            receipt["submitted_at"] = "2026-10-13T10:30:01+08:00"
            self.router.publish_steer_card(8, 801)
            remote.assert_called_once()
            self.assertEqual(remote.call_args.args[:2], (router.PREFIX + "/issues/comments/9001", "PATCH"))
            self.router.steer_receipt(8, 801, {"ok": True, "turn_id": "same-turn"})
            self.assertIn("rocket", receipt["visual_reactions"])
            self.assertTrue(receipt["accepted_at"])
            self.assertEqual(receipt["visual_comment"], 9001)
            remote.reset_mock()
            self.router.publish_steer_card(8, 801)
            remote.assert_not_called()

    def test_lost_card_post_response_recovers_existing_card_without_duplicate(self):
        del self.router.publish_steer_card
        receipt = {"sid": self.sid, "text": "补充要求", "pending": True}
        self.router.state["issues"]["8"]["steers"] = {"802": receipt}
        with patch.object(router, "api", side_effect=RuntimeError("response lost")), patch("builtins.print"):
            self.router.publish_steer_card(8, 802)
        self.assertTrue(receipt["visual_post_attempted"])
        with patch.object(router, "page_list", return_value=[{"id": 9002, "body": self.router.steer_card_body(8, 802)}]), patch.object(router, "api", return_value={}) as remote:
            self.router.publish_steer_card(8, 802)
            self.assertEqual(remote.call_args_list[0].args[:2], (router.PREFIX + "/issues/comments/9002", "PATCH"))
            self.assertFalse(any(call.args[0].endswith("/issues/8/comments") for call in remote.call_args_list))
        self.assertEqual(receipt["visual_comment"], 9002)

    def test_timeout_card_is_confirmed_from_exact_same_session_message_history(self):
        receipt = {"sid": self.sid, "message_id": "original-message", "text": "补充要求", "uncertain": True, "pending": False}
        self.router.state["issues"]["8"]["steers"] = {"804": receipt}
        with contextlib.closing(sqlite3.connect(self.root / "thread_history_1.sqlite")) as c, c:
            c.execute("CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_type TEXT,item_json TEXT,rollout_ordinal INTEGER)")
            c.execute("INSERT INTO thread_items VALUES(?,?,'userMessage',?,1)", (self.other, "wrong-turn", json.dumps({"clientId": "original-message"})))
            c.execute("INSERT INTO thread_items VALUES(?,?,'userMessage',?,2)", (self.sid, "different-message", json.dumps({"clientId": "someone-else"})))
        self.router.reconcile_steers()
        self.assertFalse(receipt.get("ok"))
        with contextlib.closing(sqlite3.connect(self.root / "thread_history_1.sqlite")) as c, c:
            c.execute("INSERT INTO thread_items VALUES(?,?,'userMessage',?,3)", (self.sid, "correct-turn", json.dumps({"clientId": "original-message"})))
        self.router.reconcile_steers()
        self.assertTrue(receipt["ok"])
        self.assertFalse(receipt["uncertain"])
        self.assertEqual(receipt["turn_id"], "correct-turn")
        self.assertIn("🟢 会话已接收", self.replies[-1][1])

    def test_late_worker_ack_updates_pending_card_and_queue_withdrawal_stays_visible(self):
        directory = self.root / "pending-steer"
        directory.mkdir()
        receipt = {"sid": self.sid, "text": "补充要求", "directory": str(directory), "pending": True,
                   "observed_at": "2026-10-13T00:00:00+08:00", "submitted_at": "2026-10-13T00:00:01+08:00"}
        self.router.state["issues"]["8"]["steers"] = {"805": receipt}
        self.router.reconcile_steers()
        self.assertIn("🟠 待确认", self.replies[-1][1])
        (directory / "control-outbox").mkdir()
        (directory / "control-outbox/805.json").write_text(json.dumps({"ok": True, "turn_id": "same-turn"}), encoding="utf-8")
        self.router.reconcile_steers()
        self.assertIn("🟢 会话已接收", self.replies[-1][1])
        with patch.object(router, "active_turn", return_value={"status": "idle"}):
            self.router.handle(8, "/steer 下一轮任务", 806)
        self.router.handle(8, "/cancel 806", 807)
        self.assertIn("⚪ 已撤回", self.router.steer_card_body(8, 806))


if __name__ == "__main__":
    unittest.main()
