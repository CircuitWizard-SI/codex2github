from tests import TEST_ROOT
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex2github import worker


class ResultTests(unittest.TestCase):
    def test_result_survives_worker_shutdown_failure_without_rerunning_task(self):
        with tempfile.TemporaryDirectory(dir=worker.BASE) as temporary:
            directory = Path(temporary)
            prompt = directory / "prompt.md"
            prompt.write_text("remote task", encoding="utf-8")
            result = {"sid": "session", "turn_id": "turn", "ok": True,
                      "final": "finished", "model": "model", "transport": "appserver"}
            client = Mock()
            def close():
                self.assertTrue((directory / "completion.json").exists(), "Completion must be durable before shutdown")
                saved = json.loads((directory / "run-result.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["final"], "finished")
                self.assertTrue(saved["ok"])
                raise TimeoutError("shutdown did not finish")
            client.close.side_effect = close
            with patch.object(worker, "api", return_value={"private": True}), patch.object(worker, "AppServer", return_value=client), patch.object(worker, "run_turn", return_value=result) as execute, patch.object(worker, "post_result", return_value={"id": 7}) as publish:
                code = worker.main(["--issue", "5", "--prompt-file", str(prompt), "--model", "model",
                                    "--cwd", str(directory), "--job-directory", str(directory)])
            self.assertEqual(code, 0)
            execute.assert_called_once()
            publish.assert_called_once()
            saved = json.loads((directory / "completion.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["ok"])
            self.assertTrue(saved["replied"])
            self.assertIn("close_error", json.loads((directory / "cleanup.json").read_text(encoding="utf-8")))

    def test_route_reset_resolves_auto_before_existing_thread_settings(self):
        with tempfile.TemporaryDirectory(dir=worker.BASE) as temporary:
            directory = Path(temporary)
            prompt = directory / "prompt.md"
            prompt.write_text("remote task", encoding="utf-8")
            receipt = {"sid": "session", "queued": True, "ok": True, "final": "Queued", "model": "new-route", "effort": "high"}
            with patch.object(worker, "api", return_value={"private": True, "id": 7}), patch.object(worker, "current_route_model", return_value="new-route"), patch.object(worker, "default_effort", return_value="high"), patch.object(worker, "queue_existing_thread", return_value=receipt) as submit:
                worker.main(["--issue", "5", "--prompt-file", str(prompt), "--model", "auto", "--effort", "auto", "--cwd", str(directory), "--job-directory", str(directory), "--sid", "session"])
            self.assertEqual(submit.call_args.args[1], "new-route")
            self.assertEqual(submit.call_args.kwargs["effort"], "high")

    def test_native_queue_submission_is_not_a_finished_task_result(self):
        with tempfile.TemporaryDirectory(dir=worker.BASE) as temporary:
            directory = Path(temporary)
            prompt = directory / "prompt.md"
            prompt.write_text("remote task", encoding="utf-8")
            receipt = {"sid": "session", "queued": True, "ok": True, "final": "Queued message native-id", "transport": "codex-queue"}
            with patch.object(worker, "api", side_effect=lambda endpoint, method="GET", body=None: {"private": True, "id": 7}), patch.object(worker, "queue_existing_thread", return_value=receipt), patch.object(worker, "post_result") as final:
                code = worker.main(["--issue", "5", "--prompt-file", str(prompt), "--model", "model", "--cwd", str(directory), "--job-directory", str(directory), "--sid", "session"])
            result = json.loads((directory / "completion.json").read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertFalse(result["replied"])
            self.assertFalse(result["execution_complete"])
            self.assertEqual(result["submission_reply_id"], 7)
            self.assertFalse((directory / "final.md").exists())
            final.assert_not_called()

    def test_public_destination_receives_no_output(self):
        with patch.object(worker, "api", return_value={"private": False}) as remote:
            with self.assertRaises(RuntimeError):
                worker.post_result(5, "private output", "model", True)
            remote.assert_called_once()

    def test_result_is_redacted_and_never_closes_task(self):
        calls = []
        def api(endpoint, method="GET", body=None):
            calls.append((endpoint, method, body))
            return {"private": True} if method == "GET" else {"id": 7}
        with patch.object(worker, "api", side_effect=api):
            worker.post_result(5, "token sk-" + "a" * 35, "model", True)
        self.assertEqual([item[1] for item in calls], ["GET", "POST"])
        self.assertNotIn("sk-" + "a" * 35, calls[1][2]["body"])
        self.assertNotIn("state", calls[1][2])


if __name__ == "__main__":
    unittest.main()
