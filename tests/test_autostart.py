from tests import TEST_ROOT
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codex2github import autostart, watcher
from codex2github.config import SETTINGS


class AutostartTests(unittest.TestCase):
    def test_explicit_startup_action_is_scoped_to_deployment_and_preserves_environment(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            settings = replace(SETTINGS, path=Path(temporary) / "config with spaces.json")
            original = {"KEEP": "unchanged"}
            remote = Mock(returncode=0, stdout='{"enabled":true,"running":true}')
            with patch.object(autostart, "os", SimpleNamespace(name="nt", environ=original)), patch.object(autostart.subprocess, "run", return_value=remote) as run:
                result = autostart.manage(settings, "enable", start=True)
                request = json.loads(run.call_args.kwargs["env"]["CODEX2GITHUB_STARTUP_REQUEST"])
                self.assertEqual(request["action"], "enable")
                self.assertTrue(request["start"])
                self.assertIn('"' + str(settings.path) + '"', request["arguments"])
                self.assertTrue(request["arguments"].endswith("watch --supervise"))
                self.assertEqual(request["cwd"], str(settings.path.parent))
                self.assertTrue(result["running"])
            self.assertEqual(original, {"KEEP": "unchanged"})
            self.assertNotEqual(autostart.task_name(settings), autostart.task_name(replace(settings, path=settings.path.with_name("other.json"))))

    def test_invalid_and_unsupported_requests_do_not_touch_scheduler(self):
        with patch.object(autostart.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                autostart.manage(SETTINGS, "stop")
            with self.assertRaises(ValueError):
                autostart.manage(SETTINGS, "disable", start=True)
            with patch.object(autostart, "os", SimpleNamespace(name="posix")):
                with self.assertRaisesRegex(RuntimeError, "Windows"):
                    autostart.manage(SETTINGS)
            run.assert_not_called()

    def test_scheduler_failure_does_not_expose_raw_stderr(self):
        with patch.object(autostart, "os", SimpleNamespace(name="nt", environ={})), patch.object(autostart.subprocess, "run", return_value=Mock(returncode=1, stderr="private machine details")):
            with self.assertRaisesRegex(RuntimeError, "Task Scheduler") as raised:
                autostart.manage(SETTINGS, "enable")
            self.assertNotIn("private machine", str(raised.exception))

    def test_supervisor_waits_through_startup_failures_without_stopping_workers(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            settings = replace(SETTINGS, runtime_dir=Path(temporary))
            with patch.object(watcher, "watch", side_effect=[OSError("existing listener"), RuntimeError("network unavailable"), 0]) as watch, patch.object(watcher.time, "sleep") as delay:
                self.assertEqual(watcher.supervise(settings), 0)
            self.assertEqual(watch.call_count, 3)
            self.assertEqual(delay.call_count, 2)


if __name__ == "__main__":
    unittest.main()
