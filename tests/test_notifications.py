from tests import TEST_ROOT
import base64
import json
from pathlib import Path
import os
import tempfile
import io
import urllib.error
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex2github import notification_action as action, notifications, session_sync


class FakeAPI:
    def __init__(self, body="<!-- codex2github:reply -->\n✅ model\n\nprivate result"):
        self.calls = []
        self.metadata = {"private": True, "full_name": "example-user/tasks",
                         "owner": {"login": "example-user", "type": "User"}}
        self.issue = {"user": {"login": "example-user"}}
        self.comment = {"body": body, "issue_url": "https://api.github.com/repos/example-user/tasks/issues/7",
                        "user": {"login": "example-user", "type": "User"}}
        self.pages = [[]]

    def request(self, endpoint, method="GET", body=None):
        self.calls.append((endpoint, method, body))
        if method == "POST":
            return {"id": 42}
        if endpoint.endswith("/issues/comments/19"):
            return self.comment
        if endpoint.endswith("/issues/7"):
            return self.issue
        if "?per_page=" in endpoint:
            return self.pages[int(endpoint.rsplit("=", 1)[1]) - 1]
        return self.metadata


class CompletionNoticeTests(unittest.TestCase):
    def test_transient_comment_read_failure_retries_before_posting_one_notice(self):
        fixtures = FakeAPI()
        calls = []
        def remote(request, timeout=30):
            path = request.full_url.removeprefix("https://api.github.com/")
            calls.append((path, request.method))
            if path.endswith("/issues/comments/19") and sum(p == path for p, _ in calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 500, "synthetic service failure", {}, None)
            body = json.loads(request.data) if request.data is not None else None
            return io.BytesIO(json.dumps(fixtures.request(path, request.method, body)).encode())
        with patch.object(action.urllib.request, "urlopen", side_effect=remote), patch("time.sleep") as wait:
            self.assertEqual(action.notify(action.NoticeAPI("example-user/tasks", "synthetic-token"),
                                           "example-user/tasks", 7, 19), "sent")
        self.assertEqual(sum(path.endswith("/issues/comments/19") for path, _ in calls), 2)
        self.assertEqual(sum(method == "POST" for _, method in calls), 1)
        wait.assert_called_once()

    def test_permanent_read_failures_are_bounded_and_never_post(self):
        error = urllib.error.HTTPError("https://api.github.com/", 500, "synthetic", {}, None)
        with patch.object(action.urllib.request, "urlopen", side_effect=error) as remote, patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                action.notify(action.NoticeAPI("example-user/tasks", "synthetic-token"), "example-user/tasks", 7, 19)
        self.assertEqual(remote.call_count, 3)
        self.assertTrue(all(call.args[0].method == "GET" for call in remote.call_args_list))

    def test_permission_denial_is_not_retried_and_never_exposes_response_text(self):
        error = urllib.error.HTTPError("https://api.github.com/", 403, "private response", {}, None)
        with patch.object(action.urllib.request, "urlopen", side_effect=error) as remote, patch("time.sleep") as wait:
            with self.assertRaisesRegex(RuntimeError, "HTTP 403") as failure:
                action.NoticeAPI("example-user/tasks", "synthetic-token").request("repos/example-user/tasks")
        remote.assert_called_once()
        wait.assert_not_called()
        self.assertNotIn("private response", str(failure.exception))

    def test_uncertain_post_is_reconciled_without_posting_again(self):
        api = FakeAPI()
        original = api.request
        def request(endpoint, method="GET", body=None):
            response = original(endpoint, method, body)
            if method == "POST":
                api.pages = [[{"body": body["body"], "user": {"login": "github-actions[bot]"}}]]
                raise action.NoticeRequestError("Response lost", uncertain=True)
            return response
        api.request = request
        with patch("time.sleep"):
            self.assertEqual(action.notify(api, "example-user/tasks", 7, 19), "sent (reconciled)")
        self.assertEqual(sum(method == "POST" for _, method, _ in api.calls), 1)

    def test_unconfirmed_post_is_not_blindly_retried(self):
        api = action.NoticeAPI("example-user/tasks", "synthetic-token")
        error = urllib.error.HTTPError("https://api.github.com/", 500, "synthetic", {}, None)
        with patch.object(action.urllib.request, "urlopen", side_effect=error) as remote, patch("time.sleep") as wait:
            with self.assertRaises(action.NoticeRequestError) as failure:
                api.request("repos/example-user/tasks/issues/7/comments", "POST", {"body": "notice"})
        self.assertTrue(failure.exception.uncertain)
        remote.assert_called_once()
        wait.assert_not_called()

    def test_completed_result_gets_only_a_link_and_owner_mention(self):
        api = FakeAPI()
        self.assertEqual(action.notify(api, "example-user/tasks", 7, 19), "sent")
        body = api.calls[-1][2]["body"]
        self.assertIn("@example-user", body)
        self.assertIn("#issuecomment-19", body)
        self.assertNotIn("private result", body)
        self.assertEqual([c[1] for c in api.calls], ["GET"] * 4 + ["POST"])

    def test_failure_and_native_queue_completion_are_eligible(self):
        for status in ("✅", "❌"):
            api = FakeAPI(f"<!-- codex2github:reply -->\n<!-- codex2github:result 123 -->\n{status} model\n\nresult")
            action.notify(api, "example-user/tasks", 7, 19)
            self.assertIn("任务已完成" if status == "✅" else "任务执行失败", api.calls[-1][2]["body"])

    def test_submission_steer_commands_and_user_text_never_notify(self):
        for body in ("<!-- codex2github:reply -->\n⏳ Submitted", "<!-- codex2github:reply -->\n<div>✅ steer</div>",
                     "<!-- codex2github:reply -->\nModels\n✅", "user text\n✅ complete"):
            api = FakeAPI(body)
            with self.assertRaises(ValueError):
                action.notify(api, "example-user/tasks", 7, 19)
            self.assertFalse(any(c[1] != "GET" for c in api.calls))

    def test_public_template_organization_and_wrong_repository_are_rejected(self):
        for change in ({"private": False}, {"is_template": True}, {"full_name": "example-user/other"},
                       {"owner": {"login": "example-org", "type": "Organization"}}):
            api = FakeAPI()
            api.metadata.update(change)
            with self.assertRaises(ValueError):
                action.notify(api, "example-user/tasks", 7, 19)
            self.assertEqual(len(api.calls), 1)

    def test_other_authors_cross_issue_comments_and_pull_requests_are_rejected(self):
        for scenario in ("author", "issue_author", "cross_issue", "pr", "bot"):
            api = FakeAPI()
            if scenario == "author":
                api.comment["user"]["login"] = "someone-else"
            elif scenario == "issue_author":
                api.issue["user"]["login"] = "someone-else"
            elif scenario == "cross_issue":
                api.comment["issue_url"] += "0"
            elif scenario == "pr":
                api.issue["pull_request"] = {}
            else:
                api.comment["user"]["type"] = "Bot"
            with self.assertRaises(ValueError):
                action.notify(api, "example-user/tasks", 7, 19)
            self.assertFalse(any(c[1] != "GET" for c in api.calls))

    def test_rerun_reconciles_even_a_lost_post_response_without_duplicate(self):
        api = FakeAPI()
        api.pages = [[{}] * 100, [{"body": "<!-- codex2github:notice 19 -->\nnotice",
                                  "user": {"login": "github-actions[bot]"}}]]
        self.assertEqual(action.notify(api, "example-user/tasks", 7, 19), "already sent")
        self.assertTrue(api.calls[-1][0].endswith("page=2"))
        self.assertFalse(any(c[1] != "GET" for c in api.calls))

    def test_labeled_test_is_separate_from_real_completion_notice(self):
        api = FakeAPI()
        action.notify(api, "example-user/tasks", 7, 19, test=True)
        self.assertIn("notice-test 19", api.calls[-1][2]["body"])
        self.assertIn("通知测试：", api.calls[-1][2]["body"])

    def test_api_boundary_forbids_edits_deletion_arbitrary_endpoints_and_invalid_ids(self):
        api = action.NoticeAPI("example-user/tasks", "synthetic-token")
        for endpoint, verb in (("repos/example-user/tasks/issues/comments/19", "DELETE"),
                               ("repos/example-user/tasks/issues/comments/19", "PATCH"),
                               ("repos/example-user/other/issues/7/comments", "POST"),
                               ("repos/example-user/tasks/issues/7", "POST")):
            with patch.object(action.urllib.request, "urlopen") as remote, self.assertRaises(ValueError):
                api.request(endpoint, verb, {"body": "notice"})
            remote.assert_not_called()
        for value in (0, -1, "7/../8", True):
            with self.assertRaises(ValueError):
                action.positive_id(value)

    def test_generated_workflow_embeds_the_tested_script_without_checkout_or_interpolation(self):
        body = notifications.workflow_body()
        embedded = body.split("          python3 - <<'PY'\n", 1)[1].rsplit("          PY\n", 1)[0]
        script = "\n".join(line[10:] if line else "" for line in embedded.splitlines())
        compile(script, "completion-notice", "exec")
        self.assertEqual(script, Path(action.__file__).read_text(encoding="utf-8").rstrip("\n"))
        self.assertNotIn("actions/checkout", body)
        self.assertNotIn("${{ github.event.comment.body }}", body)
        self.assertIn("permissions: {}", body)
        self.assertIn("issues: write", body)
        self.assertNotIn("contents: write", body)
        self.assertIn("types: [created]", body)

    def test_successful_empty_204_response_is_not_a_failed_dispatch(self):
        with patch.object(session_sync, "run", return_value=""):
            self.assertEqual(session_sync.api("synthetic/dispatches", "POST", {}), {})

    def test_actual_comment_event_forwards_final_ids_and_bot_event_cannot_loop(self):
        event = {"repository": {"owner": {"login": "example-user"}}, "sender": {"login": "example-user"},
                 "action": "created", "issue": {"number": 7},
                 "comment": {"id": 19, "body": "<!-- codex2github:reply -->\n✅ model\n\nresult"}}
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            path = Path(temporary) / "event.json"
            with patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(path), "GITHUB_REPOSITORY": "example-user/tasks",
                                       "GITHUB_EVENT_NAME": "issue_comment", "GITHUB_TOKEN": "synthetic-token"}), \
                    patch.object(action, "notify", return_value="sent") as send, patch("builtins.print"):
                path.write_text(json.dumps(event), encoding="utf-8")
                action.main()
                self.assertEqual(send.call_args.args[1:], ("example-user/tasks", 7, 19))
                send.assert_called_once()
                event["sender"]["login"] = "github-actions[bot]"
                path.write_text(json.dumps(event), encoding="utf-8")
                action.main()
                send.assert_called_once()

    def test_dispatch_sends_only_ids_and_never_result_text(self):
        settings = SimpleNamespace(repo="example-user/tasks")
        with patch.object(notifications, "metadata_for", return_value=({"default_branch": "main"}, {})), \
                patch.object(notifications, "api", return_value={}) as remote:
            notifications.test_notice(settings, 7, 19)
        self.assertEqual(remote.call_args.args[2], {"ref": "main", "inputs": {"issue_number": "7", "comment_id": "19"}})

    def test_install_uses_noreply_identity_and_does_not_overwrite_custom_workflow(self):
        settings = SimpleNamespace(repo="example-user/tasks")
        profile = {"login": "example-user", "id": 123}
        metadata = {"default_branch": "main"}
        with patch.object(notifications, "metadata_for", return_value=(metadata, profile)), \
                patch.object(notifications.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr="(HTTP 404)")), \
                patch.object(notifications, "api", return_value={}) as remote:
            notifications.enable(settings)
        payload = remote.call_args_list[0].args[2]
        self.assertEqual(payload["author"], payload["committer"])
        self.assertTrue(payload["author"]["email"].endswith("@users.noreply.github.com"))
        self.assertIn("issues: write", base64.b64decode(payload["content"]).decode())
        with patch.object(notifications, "metadata_for", return_value=(metadata, profile)), \
                patch.object(notifications.subprocess, "run", return_value=SimpleNamespace(returncode=0,
                    stdout=json.dumps({"content": base64.b64encode(b"custom workflow").decode()}))), \
                patch.object(notifications, "api") as remote, self.assertRaises(ValueError):
            notifications.enable(settings)
        remote.assert_not_called()

    def test_reviewed_managed_update_uses_the_original_file_sha(self):
        settings = SimpleNamespace(repo="example-user/tasks")
        old = b"# Managed by codex2github notifications enable.\nold script"
        probe = SimpleNamespace(returncode=0, stdout=json.dumps({"sha": "original-file-sha",
                                "content": base64.b64encode(old).decode()}))
        with patch.object(notifications, "metadata_for", return_value=({"default_branch": "main"}, {"login": "example-user", "id": 123})), \
                patch.object(notifications.subprocess, "run", return_value=probe), patch.object(notifications, "api", return_value={}) as remote:
            notifications.enable(settings, update=True)
        self.assertEqual(remote.call_args_list[0].args[2]["sha"], "original-file-sha")
        self.assertEqual(remote.call_args_list[0].args[2]["author"], remote.call_args_list[0].args[2]["committer"])


if __name__ == "__main__":
    unittest.main()
