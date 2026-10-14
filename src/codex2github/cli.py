"""One entry point shared by console scripts, launchers, and future clients."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="codex2github")
    from . import __version__
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument("--config", type=Path, help="Local JSON configuration; default: config.json")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Write local configuration without changing Codex/GitHub")
    init.add_argument("--repo", required=True)
    init.add_argument("--model", default="auto", help="Initial per-conversation model override; auto follows the current route")
    init.add_argument("--workspace", default="workspace")
    init.add_argument("--user-agent-profile", choices=["default", "desktop"], default="default")
    init.add_argument("--force", action="store_true")
    commands.add_parser("doctor", help="Read-only configuration and prerequisite checks")
    commands.add_parser("setup", help="Create a conversation hub in the configured private repository")
    commands.add_parser("tick", help="Process one remote-comment poll")
    commands.add_parser("watch", help="Run the persistent Windows listener")
    sync = commands.add_parser("sync", help="Export histories into the local runtime mirror")
    sync.add_argument("--publish", action="store_true")
    sync.add_argument("--watch", action="store_true")
    open_command = commands.add_parser("open", help="Create/reuse a room for an original session")
    open_command.add_argument("selector")
    notices = commands.add_parser("notifications", help="Enable/disable independent completion mentions")
    notices.add_argument("action", choices=["enable", "disable", "test"])
    notices.add_argument("--issue", type=int, help="Existing Issue for a labeled notification test")
    notices.add_argument("--comment", type=int, help="Existing completed task reply ID for the test")
    notices.add_argument("--update", action="store_true", help="Replace a reviewed, managed completion workflow")
    args = parser.parse_args(argv)
    if args.config:
        os.environ["CODEX2GITHUB_CONFIG"] = str(args.config.resolve())
    else:
        os.environ["CODEX2GITHUB_CONFIG"] = str(Path(os.environ.get("CODEX2GITHUB_CONFIG", "config.json")).resolve())
    try:
        from .config import config_path, load_settings, is_private_task_repository
        path = config_path()
        if args.command == "init":
            if path.exists() and not args.force:
                raise ValueError("Configuration already exists; use a different --config or --force")
            model = args.model or "auto"
            data = {"repo": args.repo, "model": model, "workspace": args.workspace, "runtime_dir": ".runtime",
                    "poll_seconds": 15, "sync_seconds": 30, "recovery_seconds": 900,
                    "user_agent_profile": args.user_agent_profile}
            # Validate before writing, including on init of a new file.
            import re
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
                raise ValueError("Use OWNER/PRIVATE_REPO")
            if args.repo.startswith("OWNER/") or (model != "auto" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,150}", model)):
                raise ValueError("Replace the example repository placeholder and use auto or a valid model ID")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Configuration written: {path}")
            return 0
        settings = load_settings(path)
        settings.require_configured()
        if args.command == "doctor":
            from .session_sync import api
            checks = {name: bool(shutil.which(name)) for name in ("gh", "codex")}
            checks["windows"] = os.name == "nt"
            checks["thread_index"] = (settings.codex_home / "state_5.sqlite").is_file()
            metadata = api(f"repos/{settings.repo}") if checks["gh"] else {}
            checks["private_task_repository"] = is_private_task_repository(metadata)
            print(json.dumps({"ok": all(checks.values()), "checks": checks}, ensure_ascii=False, indent=2))
            return 0 if all(checks.values()) else 1
        if args.command == "notifications":
            from .notifications import enable, disable, test_notice
            if args.update and args.action != "enable":
                raise ValueError("--update is only valid with notifications enable")
            if args.action == "test":
                if not args.issue or not args.comment:
                    raise ValueError("notifications test requires --issue and --comment")
                result = test_notice(settings, args.issue, args.comment)
            else:
                result = enable(settings, update=args.update) if args.action == "enable" else disable(settings)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command in {"setup", "tick", "open"}:
            from .conversation_router import main as router_main
            router_main(["--open", args.selector] if args.command == "open" else ["--" + args.command])
        elif args.command == "sync":
            from .session_sync import main as sync_main
            arguments = (["--publish"] if args.publish else []) + (["--watch"] if args.watch else [])
            arguments += ["--interval", str(settings.sync_seconds), "--recovery-seconds", str(settings.recovery_seconds)]
            return sync_main(arguments)
        elif args.command == "watch":
            from .watcher import watch
            return watch(settings)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"codex2github: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
