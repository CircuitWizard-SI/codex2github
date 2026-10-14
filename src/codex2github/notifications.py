"""Install an independent completion notifier in the private task repository."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess
from urllib.parse import quote

from . import notification_action
from .config import is_private_task_repository
from .session_sync import api


WORKFLOW = "codex-completion-notify.yml"
WORKFLOW_PATH = ".github/workflows/" + WORKFLOW


def workflow_body():
    script = Path(notification_action.__file__).read_text(encoding="utf-8")
    embedded = "\n".join("          " + line if line else "" for line in script.splitlines())
    return """# Managed by codex2github notifications enable. No checkout or task execution.
name: Codex completion notice
on:
  issue_comment:
    types: [created]
  workflow_dispatch:
    inputs:
      issue_number:
        description: Issue containing an existing completed task (test only)
        required: true
        type: string
      comment_id:
        description: Existing final reply ID (test only)
        required: true
        type: string
permissions: {}
concurrency:
  group: codex-notice-${{ github.event.comment.id || inputs.comment_id }}
  cancel-in-progress: false
jobs:
  notify:
    if: >-
      github.event.repository.private == true &&
      github.event.repository.is_template != true &&
      github.event.sender.login == github.repository_owner &&
      (github.event_name == 'workflow_dispatch' ||
      startsWith(github.event.comment.body, '<!-- codex2github:reply -->'))
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions:
      issues: write
    steps:
      - name: Append one completion mention
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        shell: bash
        run: |
          python3 - <<'PY'
""" + embedded + "\n          PY\n"


def metadata_for(settings):
    settings.require_configured()
    metadata = api("repos/" + settings.repo)
    if not is_private_task_repository(metadata) or metadata.get("owner", {}).get("type") != "User":
        raise ValueError("Notifications require your personal private task repository")
    profile = api("user")
    if profile.get("login") != metadata["owner"]["login"]:
        raise ValueError("Authenticate as the task repository owner")
    return metadata, profile


def enable(settings):
    metadata, profile = metadata_for(settings)
    prefix = "repos/" + settings.repo
    branch = metadata.get("default_branch") or "main"
    endpoint = f"{prefix}/contents/{WORKFLOW_PATH}"
    probe = subprocess.run(["gh", "api", endpoint + "?ref=" + quote(branch, safe="")],
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
    existing = None
    if probe.returncode == 0:
        existing = json.loads(probe.stdout)
    elif "(HTTP 404)" not in probe.stderr:
        raise RuntimeError("Unable to inspect the notification workflow")
    content = workflow_body().encode("utf-8")
    if existing:
        original = base64.b64decode(existing.get("content", ""))
        if original != content:
            raise ValueError("Existing notification workflow differs; review it before replacing")
    else:
        identity = {"name": profile["login"],
                    "email": f"{profile['id']}+{profile['login']}@users.noreply.github.com"}
        api(endpoint, "PUT", {"message": "Add append-only Codex completion notifications", "branch": branch,
            "content": base64.b64encode(content).decode("ascii"), "author": identity, "committer": identity})
    # Explicitly enable only this workflow, never repository-wide permissions.
    api(f"{prefix}/actions/workflows/{WORKFLOW}/enable", "PUT")
    return {"enabled": True, "repo": settings.repo, "workflow": WORKFLOW_PATH}


def disable(settings):
    metadata_for(settings)
    api(f"repos/{settings.repo}/actions/workflows/{WORKFLOW}/disable", "PUT")
    return {"enabled": False, "repo": settings.repo}


def test_notice(settings, issue, comment):
    metadata, _ = metadata_for(settings)
    issue, comment = notification_action.positive_id(issue), notification_action.positive_id(comment)
    api(f"repos/{settings.repo}/actions/workflows/{WORKFLOW}/dispatches", "POST",
        {"ref": metadata.get("default_branch") or "main",
         "inputs": {"issue_number": str(issue), "comment_id": str(comment)}})
    return {"test_requested": True, "repo": settings.repo, "issue": issue, "comment": comment}
