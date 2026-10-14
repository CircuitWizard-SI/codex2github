"""Standalone, append-only GitHub completion notice (embedded in a workflow).

No checkout, model execution, comment editing, deletion, or third-party calls.
The workflow token is used only with the fixed GET/POST endpoints below.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request


FINAL = re.compile(r"^<!-- codex2github:reply -->\r?\n"
                   r"(?:<!-- codex2github:result [1-9][0-9]* -->\r?\n)?([✅❌])(?: |\r?\n)")


def positive_id(value):
    text = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", text):
        raise ValueError("Invalid issue or comment ID")
    return int(text)


class NoticeAPI:
    def __init__(self, repo, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("Invalid repository")
        self.prefix = "repos/" + repo
        self.token = token

    def request(self, path, method="GET", body=None):
        # Even an accidental new call cannot edit or delete a comment.
        read = (path == self.prefix or re.fullmatch(
            re.escape(self.prefix) + r"/issues/(?:comments/[1-9][0-9]*|[1-9][0-9]*(?:/comments\?per_page=100&page=[1-9][0-9]*)?)", path))
        append = re.fullmatch(re.escape(self.prefix) + r"/issues/[1-9][0-9]*/comments", path)
        if not ((method == "GET" and read and body is None) or
                (method == "POST" and append and isinstance(body, dict) and set(body) == {"body"})):
            raise ValueError("Notification API only permits reading and appending a notice")
        request = urllib.request.Request("https://api.github.com/" + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"GitHub notification API failed (HTTP {error.code})") from None
        except (OSError, ValueError):
            # A POST timeout is uncertain. Do not repeat it automatically; a
            # workflow rerun first reconciles the stable marker with GitHub.
            raise RuntimeError("GitHub notification API response unavailable; rerun to reconcile") from None


def notify(api, repo, issue_id, comment_id, *, test=False):
    issue_id, comment_id = positive_id(issue_id), positive_id(comment_id)
    prefix = "repos/" + repo
    metadata = api.request(prefix)
    if (metadata.get("private") is not True or metadata.get("is_template") is True
            or metadata.get("full_name", "").casefold() != repo.casefold()
            or metadata.get("owner", {}).get("type") != "User"):
        raise ValueError("Not a personal private task repository")
    owner = metadata["owner"]["login"]
    issue = api.request(f"{prefix}/issues/{issue_id}")
    if "pull_request" in issue or issue.get("user", {}).get("login") != owner:
        raise ValueError("Notice must target the owner's task Issue")
    comment = api.request(f"{prefix}/issues/comments/{comment_id}")
    match = FINAL.match(comment.get("body") or "")
    expected_issue = f"https://api.github.com/{prefix}/issues/{issue_id}"
    if (not match or comment.get("issue_url") != expected_issue
            or comment.get("user", {}).get("login") != owner
            or comment.get("user", {}).get("type") != "User"):
        raise ValueError("Not a completed task reply in this Issue")
    marker = f"<!-- codex2github:notice{'-test' if test else ''} {comment_id} -->"
    page = 1
    while True:
        comments = api.request(f"{prefix}/issues/{issue_id}/comments?per_page=100&page={page}")
        if any((row.get("body") or "").startswith(marker + "\n") and
               row.get("user", {}).get("login") == "github-actions[bot]" for row in comments):
            return "already sent"
        if len(comments) < 100:
            break
        page += 1
    status = "任务已完成" if match[1] == "✅" else "任务执行失败"
    label = "通知测试：" if test else ""
    # Construct the URL, never interpolate task output or user-controlled URLs.
    url = f"https://github.com/{repo}/issues/{issue_id}#issuecomment-{comment_id}"
    body = f"{marker}\n@{owner} {label}{status}。[查看结果]({url})"
    api.request(f"{prefix}/issues/{issue_id}/comments", "POST", {"body": body})
    return "sent"


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    repo = os.environ["GITHUB_REPOSITORY"]
    owner = event.get("repository", {}).get("owner", {}).get("login")
    if event.get("sender", {}).get("login") != owner:
        print("Skipped: only owner-authored task completions are eligible")
        return
    test = os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch"
    if test:
        inputs = event["inputs"]
        issue_id, comment_id = inputs["issue_number"], inputs["comment_id"]
    else:
        if (event.get("action") != "created" or "pull_request" in event.get("issue", {})
                or not FINAL.match(event.get("comment", {}).get("body") or "")):
            print("Skipped: no completed task reply")
            return
        issue_id, comment_id = event["issue"]["number"], event["comment"]["id"]
    api = NoticeAPI(repo, os.environ["GITHUB_TOKEN"])
    print("Completion notice: " + notify(api, repo, issue_id, comment_id, test=test))


if __name__ == "__main__":
    main()
