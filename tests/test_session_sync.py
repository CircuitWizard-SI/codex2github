from tests import TEST_ROOT
import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex2github import session_sync as sync


class ClassificationTests(unittest.TestCase):
    def test_origin_markers_and_legacy_paths_classify_without_title_guessing(self):
        cases = [
            ({"source": json.dumps({"subagent": {"thread_spawn": {"agent_path": None}}}), "agent_path": None}, True),
            ({"source": {"subagent": {"other": "guardian"}}}, True),
            ({"thread_source": "subagent", "agent_path": "", "source": "exec"}, True),
            ({"agent_path": "/root/legacy"}, True),
            ({"source": "vscode", "thread_source": "user", "title": "讨论子代理", "agent_nickname": "Gauss"}, False),
            ({"source": "vscode", "thread_source": "agent_created_thread"}, False),
            ({"source": "{invalid", "thread_source": None, "agent_path": None}, False),
            ({}, False),
        ]
        for metadata, expected in cases:
            with self.subTest(metadata=metadata):
                self.assertEqual(sync.is_subagent(metadata), expected)


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=sync.BASE)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.output = self.root / "output"
        self.sid = "00000000-0000-0000-0000-000000000001"
        self.second = "00000000-0000-0000-0000-000000000002"
        with contextlib.closing(sqlite3.connect(self.home / "state_5.sqlite")) as c, c:
            c.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,title TEXT,name TEXT,updated_at INTEGER,updated_at_ms INTEGER,archived INTEGER,agent_path TEXT)")
            c.execute("INSERT INTO threads VALUES(?,?,?,NULL,1700000000,1700000000000,0,NULL)", (self.sid, str(self.home / "gone.jsonl"), "中文 / 会话 | [标题]"))
        with contextlib.closing(sqlite3.connect(self.home / "thread_history_1.sqlite")) as c, c:
            c.execute("CREATE TABLE thread_items(thread_id TEXT,item_id TEXT,item_json TEXT,created_at_ms INTEGER,item_type TEXT,rollout_ordinal INTEGER,updated_at_ordinal INTEGER)")
        self.mirror = sync.Mirror(self.home, self.output, self.root / "cache.json")

    def tearDown(self):
        self.temporary.cleanup()

    def item(self, number, item):
        with contextlib.closing(sqlite3.connect(self.home / "thread_history_1.sqlite")) as c, c:
            c.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)", (self.sid, str(number), json.dumps(item, ensure_ascii=False), 1700000000000 + number, item["type"], number, number))

    def test_paginated_history_unicode_archive_and_no_internal_output(self):
        self.item(1, {"type": "userMessage", "content": [{"type": "text", "text": "请继续原来的中文任务"}]})
        self.item(2, {"type": "reasoning", "text": "private-reasoning"})
        self.item(3, {"type": "commandExecution", "text": "private-tool-output"})
        self.item(4, {"type": "agentMessage", "text": "已完成，文件保留。", "phase": "final"})
        with contextlib.closing(sqlite3.connect(self.home / "state_5.sqlite")) as c, c:
            c.execute("UPDATE threads SET archived=1")
        originals = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.home.glob("*.sqlite")}
        stats = self.mirror.export()
        page = (self.output / f"sessions/{self.sid}/page-001.md").read_text(encoding="utf-8")
        self.assertEqual((stats["readable"], stats["messages"], stats["archived"]), (1, 2, 1))
        self.assertIn("请继续原来的中文任务", page)
        self.assertIn("已完成，文件保留。", page)
        self.assertNotIn("private-reasoning", page)
        self.assertNotIn("private-tool-output", page)
        self.assertEqual(originals, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in originals})
        self.assertIn(self.sid, (self.output / "sessions/archived-001.md").read_text(encoding="utf-8"))

    def test_reply_after_metadata_refreshes_cached_page(self):
        self.item(1, {"type": "userMessage", "content": [{"type": "text", "text": "测试"}]})
        self.mirror.export()
        self.item(2, {"type": "agentMessage", "text": "延迟写入的最终回复"})
        self.assertEqual(self.mirror.export()["messages"], 2)
        self.assertIn("延迟写入", (self.output / f"sessions/{self.sid}/page-001.md").read_text(encoding="utf-8"))

    def test_source_marked_subagents_are_excluded_from_main_and_recent(self):
        agents = [
            (self.second, None, "subagent"),
            ("00000000-0000-0000-0000-000000000003", json.dumps({"subagent": {"other": "guardian"}}), None),
        ]
        with contextlib.closing(sqlite3.connect(self.home / "state_5.sqlite")) as c, c:
            c.execute("ALTER TABLE threads ADD COLUMN source TEXT")
            c.execute("ALTER TABLE threads ADD COLUMN thread_source TEXT")
            c.execute("UPDATE threads SET source='vscode',thread_source='user'")
            for number, (sid, source, thread_source) in enumerate(agents):
                c.execute("INSERT INTO threads(id,rollout_path,title,updated_at,updated_at_ms,archived,agent_path,source,thread_source) VALUES(?,?,?,?,?,0,NULL,?,?)",
                          (sid, str(self.home / 'missing.jsonl'), f'无路径子代理{number}', 1800000000 + number, (1800000000 + number) * 1000, source, thread_source))
        stats = self.mirror.export()
        self.assertEqual(stats["agents"], 2)
        main = (self.output / "sessions/main-001.md").read_text(encoding="utf-8")
        recent = (self.output / "README.md").read_text(encoding="utf-8")
        agent_list = (self.output / "sessions/agents-001.md").read_text(encoding="utf-8")
        for sid, _, _ in agents:
            self.assertNotIn(sid, main)
            self.assertNotIn(sid, recent)
            self.assertIn(sid, agent_list)
        self.assertIn(self.sid, main)

    def test_rollout_file_change_refreshes_history_without_metadata_update(self):
        path = self.home / "legacy.jsonl"
        first = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "第一条消息"}]}}
        second = {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "文件里新增的回答"}]}}
        path.write_text(json.dumps(first) + "\n", encoding="utf-8")
        with contextlib.closing(sqlite3.connect(self.home / "state_5.sqlite")) as c, c:
            c.execute("UPDATE threads SET rollout_path=?", (str(path),))
        self.assertEqual(self.mirror.export()["messages"], 1)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(second) + "\n")
        self.assertEqual(self.mirror.export()["messages"], 2)
        self.assertIn("文件里新增的回答", (self.output / f"sessions/{self.sid}/page-001.md").read_text(encoding="utf-8"))

    def test_partial_history_projection_does_not_hide_rollout_progress_and_final(self):
        self.item(1, {"type": "userMessage", "content": [{"type": "text", "text": "原始问题"}]})
        path = self.home / "live.jsonl"
        records = [
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "原始问题"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "commentary", "content": [{"type": "output_text", "text": "正在处理任务"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "final", "content": [{"type": "output_text", "text": "完整最终回答"}]}},
        ]
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        with contextlib.closing(sqlite3.connect(self.home / "state_5.sqlite")) as c, c:
            c.execute("UPDATE threads SET rollout_path=?", (str(path),))
        self.assertEqual(self.mirror.export()["messages"], 3)
        page = (self.output / f"sessions/{self.sid}/page-001.md").read_text(encoding="utf-8")
        self.assertEqual(page.count("原始问题"), 1)
        self.assertIn("正在处理任务", page)
        self.assertIn("完整最终回答", page)

    def test_native_item_phases_fill_missing_response_channel_without_duplicates(self):
        path = self.home / "native.jsonl"
        records = []
        for phase, text in [("commentary", "处理进度"), ("final_answer", "最终结果")]:
            records.extend([
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}},
                {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "phase": phase, "content": [{"type": "Text", "text": text}]}}},
            ])
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        messages = sync.rollout_messages(path)
        self.assertEqual([(m["role"], m["text"]) for m in messages], [("Codex · 进度", "处理进度"), ("Codex", "最终结果")])

    def test_long_conversation_pages_and_stable_unchanged_export(self):
        self.item(1, {"type": "userMessage", "content": [{"type": "text", "text": "长对话\n" * 20000}]})
        self.mirror.export()
        pages = list((self.output / f"sessions/{self.sid}").glob("page-*.md"))
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(p.stat().st_size < sync.PAGE_BYTES for p in pages))
        timestamps = {p: p.stat().st_mtime_ns for p in self.output.rglob("*.md")}
        self.mirror.export()
        self.assertEqual(timestamps, {p: p.stat().st_mtime_ns for p in timestamps})

    def test_secret_and_infrastructure_filter(self):
        secret = "sk-" + "a" * 35
        self.item(1, {"type": "userMessage", "content": [{"type": "text", "text": "# AGENTS.md instructions\n<INSTRUCTIONS>private-rules</INSTRUCTIONS><environment_context>private-context</environment_context>真正的请求 " + secret}]})
        self.mirror.export()
        page = (self.output / f"sessions/{self.sid}/page-001.md").read_text(encoding="utf-8")
        self.assertIn("真正的请求", page)
        self.assertNotIn(secret, page)
        self.assertNotIn("private-rules", page)
        self.assertNotIn("private-context", page)

    def test_compressed_legacy_fallback_and_partial_last_line(self):
        import zstandard
        records = [
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "压缩会话中的问题"}]}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "压缩会话中的问题"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "analysis", "content": [{"type": "output_text", "text": "private-reasoning"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "final", "content": [{"type": "output_text", "text": "压缩会话的答案"}]}},
        ]
        raw = "\n".join(json.dumps(r, ensure_ascii=False) for r in records).encode() + b'\n{"unfinished"'
        (self.home / "gone.jsonl.zst").write_bytes(zstandard.ZstdCompressor().compress(raw))
        self.assertEqual(self.mirror.export()["messages"], 2)
        page = (self.output / f"sessions/{self.sid}/page-001.md").read_text(encoding="utf-8")
        self.assertIn("压缩会话的答案", page)
        self.assertNotIn("private-reasoning", page)

    def test_public_repository_is_rejected_before_git(self):
        with patch.object(sync, "api", return_value={"private": False}) as command:
            with self.assertRaisesRegex(RuntimeError, "non-private"):
                sync.publish(self.output, "owner/repo")
            self.assertEqual(command.call_count, 1)

    def test_failed_push_is_retried_with_no_new_export(self):
        self.output.mkdir()
        (self.output / "README.md").write_text("中文会话目录", encoding="utf-8")
        attempted = []
        def command(endpoint, method="GET", body=None):
            attempted.append((endpoint, method, body))
            if endpoint == "repos/owner/repo": return {"private": True, "default_branch": "main"}
            if "/git/ref/" in endpoint: return {"object": {"sha": "parent"}}
            if endpoint.endswith("/git/commits/parent"): return {"tree": {"sha": "base-tree"}}
            if "?recursive=1" in endpoint: return {"tree": [{"path": "notes/user.md", "type": "blob", "sha": "user-note"}]}
            if endpoint.endswith("/git/trees"): return {"sha": "new-tree"}
            if endpoint.endswith("/git/commits"):
                self.assertEqual(body["parents"], ["parent"])
                return {"sha": "new-commit"}
            if method == "PATCH":
                self.assertFalse(body["force"])
                if sum(x[1] == "PATCH" for x in attempted) == 1: raise RuntimeError("network failure")
                return {"object": {"sha": "new-commit"}}
            raise AssertionError(endpoint)
        with patch.object(sync, "api", side_effect=command):
            with self.assertRaises(RuntimeError): sync.publish(self.output, "owner/repo")
            self.assertTrue(sync.publish(self.output, "owner/repo"))
        self.assertEqual(sum(call[1] == "PATCH" for call in attempted), 2)
        tree_writes = [call[2] for call in attempted if call[0].endswith("/git/trees")]
        self.assertTrue(all(body["base_tree"] == "base-tree" for body in tree_writes))
        self.assertTrue(all([entry["path"] for entry in body["tree"]] == ["README.md"] for body in tree_writes))

    def test_unchanged_api_tree_never_creates_commit(self):
        self.output.mkdir()
        (self.output / "README.md").write_text("相同内容", encoding="utf-8")
        responses = [
            {"private": True, "default_branch": "main"},
            {"object": {"sha": "parent"}},
            {"tree": {"sha": "tree"}},
            {"tree": [{"path": "README.md", "type": "blob", "sha": sync.blob_sha((self.output / "README.md").read_bytes())}]},
        ]
        with patch.object(sync, "api", side_effect=responses) as command:
            self.assertFalse(sync.publish(self.output, "owner/repo"))
            self.assertEqual(command.call_count, 4)


if __name__ == "__main__":
    unittest.main()
