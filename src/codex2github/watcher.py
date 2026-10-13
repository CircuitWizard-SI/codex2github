"""Supervise only the listener and history mirror, keeping task workers alive."""
from datetime import datetime
import os
import subprocess
import sys
import time

from .session_sync import singleton


def watch(settings) -> int:
    if os.name != "nt":
        raise RuntimeError("Filesystem notifications currently require Windows")
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    settings.workspace.mkdir(parents=True, exist_ok=True)
    os.environ["CODEX2GITHUB_CONFIG"] = str(settings.path)
    child = None
    sync_log = sync_errors = None
    with singleton(settings.runtime_dir / "watcher.lock"):
        def log(message):
            with (settings.runtime_dir / "watch.log").open("a", encoding="utf-8") as stream:
                stream.write(f"{datetime.now().isoformat(timespec='seconds')}  {message}\n")
        def command(*args):
            return [sys.executable, "-m", "codex2github", "--config", str(settings.path), *args]
        def start_sync():
            return subprocess.Popen(command("sync", "--watch", "--publish"), cwd=settings.workspace,
                                    stdout=sync_log, stderr=sync_errors, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            setup = subprocess.run(command("setup"), cwd=settings.workspace, capture_output=True, text=True, encoding="utf-8")
            if setup.returncode:
                raise RuntimeError(setup.stderr.strip() or "Conversation router setup failed")
            log(setup.stdout.strip())
            sync_log = (settings.runtime_dir / "sync.log").open("a", encoding="utf-8")
            sync_errors = (settings.runtime_dir / "sync-error.log").open("a", encoding="utf-8")
            child = start_sync()
            log(f"watcher started; history mirror pid={child.pid}")
            while True:
                if child.poll() is not None:
                    log("History mirror exited; restarting")
                    child = start_sync()
                tick = subprocess.run(command("tick"), cwd=settings.workspace, capture_output=True, text=True, encoding="utf-8")
                log(tick.stdout.strip() if tick.returncode == 0 else tick.stderr.strip())
                time.sleep(settings.poll_seconds)
        except KeyboardInterrupt:
            log("listener stopped by user; task workers preserved")
            return 0
        finally:
            if child and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            for stream in (sync_log, sync_errors):
                if stream:
                    stream.close()
