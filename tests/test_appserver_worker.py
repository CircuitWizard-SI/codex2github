from tests import TEST_ROOT
import json
import queue
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from codex2github import appserver_worker as worker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=worker.BASE)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_active_writer_conflict_is_retryable(self):
        self.assertTrue(worker.is_active_writer_error(
            "thread-store conflict: thread abc already has an active writer"))
        self.assertFalse(worker.is_active_writer_error("model is unavailable"))

    def test_desktop_unknown_profile_sends_desktop_identity_and_terminal(self):
        from dataclasses import replace
        from codex2github import request_identity
        settings = replace(worker.SETTINGS, user_agent_profile="desktop")
        with patch.object(worker, "SETTINGS", settings), \
             patch.object(request_identity, "SETTINGS", settings), \
             patch.object(worker, "client_info", return_value={"name": "Codex Desktop", "version": "26.1007.21434"}), \
             patch.object(worker, "verify_user_agent"):
            with patch.object(worker.subprocess, "Popen") as popen:
                process = popen.return_value
                process.stdin = Mock()
                process.stdout = MagicMock()
                process.poll.return_value = None
                client = worker.AppServer(self.root)
            self.assertEqual(worker.codex_environment()["TERM"], "unknown")
            self.assertEqual(worker.codex_environment()["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"], "Codex Desktop")
            self.assertEqual(client.client_info, {"name": "Codex Desktop", "version": "26.1007.21434"})
            client.close()

    def test_queue_confirms_conversation_settings_then_uses_official_cli(self):
        completed = Mock(returncode=0, stdout="Queued message abc-123 for thread session.\n", stderr="")
        with patch.object(worker, "apply_existing_thread", return_value={"model": "gpt-6.1-sol", "reasoningEffort": "high"}) as apply, patch.object(worker, "codex_command", return_value=["codex.exe"]), patch.object(worker.subprocess, "run", return_value=completed) as run:
            result = worker.queue_existing_thread("任务", "gpt-6.1-sol", str(self.root), "session")
        args = run.call_args.args[0]
        apply.assert_called_once_with("session", "gpt-6.1-sol", "auto", timeout=10)
        self.assertEqual(args[:4], ["codex.exe", "queue", "--thread", "session"])
        self.assertNotIn("--model", args)
        self.assertEqual(result["queue_message_id"], "abc-123")
        self.assertTrue(result["queued"])
        self.assertTrue(result["settings_confirmed"])

    def test_queue_applies_reasoning_effort_before_submitting_unchanged_prompt(self):
        completed = Mock(returncode=0, stdout="Queued message abc-123 for thread session.\n", stderr="")
        applied = []
        def apply(*args, **kwargs):
            applied.append(args)
            return {"model": "gpt-6.1-sol", "reasoningEffort": "ultra"}
        def submit(*args, **kwargs):
            self.assertEqual(applied, [("session", "gpt-6.1-sol", "ultra")])
            return completed
        with patch.object(worker, "apply_existing_thread", side_effect=apply), patch.object(worker, "codex_command", return_value=["codex.exe"]), patch.object(worker.subprocess, "run", side_effect=submit) as run:
            worker.queue_existing_thread("原始任务", "gpt-6.1-sol", str(self.root), "session", effort="ultra")
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--message") + 1], "原始任务")
        self.assertNotIn("-c", args)

    def test_unconfirmed_settings_do_not_submit_or_repeat_task(self):
        with patch.object(worker, "apply_existing_thread", side_effect=worker.SettingsUnavailable("not applied")), patch.object(worker.subprocess, "run") as run:
            with self.assertRaises(worker.SettingsUnavailable):
                worker.queue_existing_thread("原始任务", "gpt-6.1-sol", str(self.root), "session", effort="ultra")
        run.assert_not_called()

    def test_model_fallback_resets_effort_to_new_route_default(self):
        rpc = Mock()
        rpc.notifications = queue.Queue()
        rpc.notifications.put({"method": "turn/completed", "params": {"threadId": "session", "turn": {
            "id": "turn", "status": "completed", "items": [{"type": "agentMessage", "id": "answer", "phase": "final", "text": "done"}]}}})
        rpc.call.side_effect = [worker.RpcError("model unavailable"), {"config": {"model": "route-default"}},
                                {"thread": {"id": "session"}}, {"turn": {"id": "turn"}}]
        with patch.object(worker, "default_effort", return_value="medium"):
            result = worker.run_turn(rpc, self.root, "原始任务", "old-model", str(self.root), "session", "ultra")
        self.assertEqual(rpc.call.call_args.args, ("turn/start", {"threadId": "session",
                         "input": [{"type": "text", "text": "原始任务", "text_elements": []}], "effort": "medium"}))
        self.assertEqual((result["model"], result["effort"]), ("route-default", "medium"))

    def test_long_running_turn_waits_for_actual_terminal_event_without_interrupting(self):
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                clock = [0]
                rpc = Mock()
                rpc.process.poll.return_value = None
                rpc.call.side_effect = lambda method, params: (
                    {"thread": {"id": "session"}} if method == "thread/resume"
                    else {"turn": {"id": "turn"}})
                def next_event(timeout):
                    if clock[0] == 0:
                        clock[0] = 7200  # Two hours pass while the server stays alive.
                        raise queue.Empty
                    return {"method": "turn/completed", "params": {"threadId": "session", "turn": {
                        "id": "turn", "status": status,
                        "error": {"message": "actual server failure"} if status == "failed" else None,
                        "items": [{"type": "agentMessage", "id": "answer", "phase": "final", "text": "long task finished"}]
                                 if status == "completed" else []}}}
                rpc.notifications.get.side_effect = next_event
                with patch("time.monotonic", side_effect=lambda: clock[0]):
                    result = worker.run_turn(rpc, self.root, "unchanged task", "configured-model", str(self.root), "session")
                self.assertEqual(result["ok"], status == "completed")
                self.assertEqual(result["final"], "long task finished" if status == "completed"
                                 else "Codex 执行未完成：actual server failure")
                self.assertNotIn("turn/interrupt", [call.args[0] for call in rpc.call.call_args_list])

    def control(self, sid="session", turn="turn", comment=1):
        data = {"sid": sid, "turn_id": turn, "comment": comment, "message_id": f"message-{comment}", "text": "补充引导"}
        worker.json_file(self.root / "control-inbox" / f"{comment}.json", data)

    def test_same_turn_delivery_is_guarded_and_deduplicated(self):
        self.control()
        rpc = Mock()
        rpc.call.return_value = {"turnId": "turn"}
        worker.process_controls(rpc, self.root, "session", "turn")
        worker.process_controls(rpc, self.root, "session", "turn")
        rpc.call.assert_called_once()
        method, params = rpc.call.call_args.args
        self.assertEqual(method, "turn/steer")
        self.assertEqual(params["expectedTurnId"], "turn")
        self.assertEqual(params["threadId"], "session")
        self.assertTrue(json.loads((self.root / "control-outbox/1.json").read_text(encoding="utf-8"))["ok"])

    def test_wrong_or_completed_turn_never_receives_input(self):
        self.control(turn="older")
        self.control(comment=2)
        rpc = Mock()
        worker.process_controls(rpc, self.root, "session", "newer", active=False)
        rpc.call.assert_not_called()
        self.assertFalse(json.loads((self.root / "control-outbox/1.json").read_text(encoding="utf-8"))["ok"])

    def test_unknown_delivery_is_not_retried_or_queued(self):
        self.control()
        rpc = Mock()
        rpc.call.side_effect = TimeoutError("response unknown")
        worker.process_controls(rpc, self.root, "session", "turn")
        worker.process_controls(rpc, self.root, "session", "turn")
        rpc.call.assert_called_once()
        self.assertTrue(json.loads((self.root / "control-outbox/1.json").read_text(encoding="utf-8"))["uncertain"])

    def test_full_stdio_worker_steers_and_preserves_session_without_model_request(self):
        fixture = self.root / "fake_server.py"
        fixture.write_text('''import json,sys
def send(data):
 print(json.dumps(data),flush=True)
for line in sys.stdin:
 request=json.loads(line)
 method=request['method']
 if method=='initialized': continue
 if method=='initialize': result={}
 elif method=='thread/resume': result={'thread':{'id':request['params']['threadId']}}
 elif method=='turn/start':
  assert request['params']['input']==[{'type':'text','text':'task','text_elements':[]}]
  assert request['params']['effort']=='xhigh'
  result={'turn':{'id':'turn','status':'inProgress','items':[]}}
 elif method=='turn/steer':
  assert request['params']['expectedTurnId']=='turn'
  result={'turnId':'turn'}
 else: raise RuntimeError(method)
 send({'id':request['id'],'result':result})
 if method=='turn/steer':
  send({'method':'item/completed','params':{'threadId':'session','turnId':'turn','item':{'type':'agentMessage','id':'answer','text':'steered answer','phase':'final'}}})
  send({'method':'turn/completed','params':{'threadId':'session','turn':{'id':'turn','status':'completed','items':[]}}})
''', encoding="utf-8")
        self.control()
        rpc = worker.AppServer(self.root, [sys.executable, "-u", str(fixture)])
        try:
            result = worker.run_turn(rpc, self.root, "task", "configured-model", str(self.root), sid="session", effort="xhigh")
        finally:
            rpc.close()
        self.assertTrue(result["ok"])
        self.assertEqual(result["sid"], "session")
        self.assertEqual(result["turn_id"], "turn")
        self.assertEqual(result["final"], "steered answer")
        self.assertFalse(json.loads((self.root / "runtime.json").read_text(encoding="utf-8"))["active"])


if __name__ == "__main__":
    unittest.main()
