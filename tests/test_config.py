from tests import TEST_ROOT
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex2github.config import load_settings
from codex2github import conversation_router as router


class ConfigurationTests(unittest.TestCase):
    def test_source_template_is_rejected_even_when_private(self):
        from codex2github.config import is_private_task_repository
        self.assertFalse(is_private_task_repository({"private": True, "is_template": True}))
        self.assertFalse(is_private_task_repository({"private": False, "is_template": False}))
        self.assertFalse(is_private_task_repository({"private": True, "is_template": False,
                         "full_name": "CircuitWizard-SI/codex2github"}))
        self.assertTrue(is_private_task_repository({"private": True, "is_template": False}))

    def test_default_codex_home_belongs_to_current_deployer(self):
        import os
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text(json.dumps({"repo": "another-owner/private-chat"}), encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch("codex2github.config.Path.home", return_value=root / "new-user"):
                self.assertEqual(load_settings(config).codex_home, root / "new-user" / ".codex")
            with patch.dict(os.environ, {"CODEX_HOME": str(root / "other-home")}):
                self.assertEqual(load_settings(config).codex_home, root / "other-home")

    def test_private_source_template_does_not_receive_tasks_or_history(self):
        from codex2github import session_sync, worker
        instance = object.__new__(router.Router)
        instance.state = {"rooms": {}}
        metadata = {"private": True, "is_template": True}
        with patch.object(router, "api", return_value=metadata) as remote, patch.object(instance, "room_body") as preview:
            for operation in (lambda: instance.setup(), lambda: instance.ensure_room("synthetic"), lambda: instance.tick()):
                with self.assertRaisesRegex(RuntimeError, "template"):
                    operation()
            self.assertTrue(all(call.args == (router.PREFIX,) for call in remote.call_args_list))
            preview.assert_not_called()
        with patch.object(session_sync, "api", return_value=metadata) as remote:
            with self.assertRaisesRegex(RuntimeError, "template"):
                session_sync.publish(TEST_ROOT, "another-owner/source-template")
            remote.assert_called_once()
        with patch.object(worker, "api", return_value=metadata) as remote:
            with self.assertRaisesRegex(RuntimeError, "template"):
                worker.post_result(1, "private result", "test-model", True)
            remote.assert_called_once()

    def test_relative_paths_and_route_reading_follow_config_not_original_machine(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text(json.dumps({"repo": "another-owner/private-chat", "model": "custom/model",
                                          "workspace": "work", "runtime_dir": "data", "codex_home": "home"}), encoding="utf-8")
            settings = load_settings(config)
            settings.require_configured()
            self.assertEqual(settings.workspace, root / "work")
            self.assertEqual(settings.runtime_dir, root / "data")
            self.assertEqual(settings.model, "custom/model")

    def test_missing_placeholder_and_invalid_configs_do_not_start_tasks(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            config = Path(temporary) / "config.json"
            with self.assertRaises(ValueError):
                load_settings(config).require_configured()
            config.write_text(json.dumps({"repo": "OWNER/PRIVATE_TASK_REPO", "model": "YOUR_MODEL_ID"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_settings(config).require_configured()
            config.write_text(json.dumps({"desktop_ipc": True}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_settings(config)

    def test_current_route_is_read_only_and_profiles_are_supported(self):
        from codex2github.config import current_route_fingerprint, current_route_model
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            home = Path(temporary)
            (home / "config.toml").write_text('profile = "work"\n[profiles.work]\nmodel = "route/model"\n[profiles.other]\nmodel = "other/model"\n', encoding="utf-8")
            before = (home / "config.toml").read_bytes()
            self.assertEqual(current_route_model(home), "route/model")
            self.assertTrue(current_route_fingerprint(home))
            self.assertEqual((home / "config.toml").read_bytes(), before)

    def test_cc_switch_route_change_invalidates_fingerprint_without_writing_route_files(self):
        from codex2github.config import current_route_fingerprint
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            root = Path(temporary)
            home = root / "codex"
            cc_switch = root / ".cc-switch"
            home.mkdir()
            cc_switch.mkdir()
            (home / "config.toml").write_text('model = "route/model"\n', encoding="utf-8")
            settings = cc_switch / "settings.json"
            live = cc_switch / "live-state.json"
            settings.write_text(json.dumps({"currentProviderCodex": "provider-a"}), encoding="utf-8")
            live.write_text(json.dumps({"apps": {"codex": {"mode": "direct", "proxy_route": "provider-a"}}}), encoding="utf-8")
            before = (settings.read_bytes(), live.read_bytes())
            first = current_route_fingerprint(home)
            self.assertEqual((settings.read_bytes(), live.read_bytes()), before)
            settings.write_text(json.dumps({"currentProviderCodex": "provider-b"}), encoding="utf-8")
            live.write_text(json.dumps({"apps": {"codex": {"mode": "direct", "proxy_route": "provider-b"}}}), encoding="utf-8")
            self.assertNotEqual(first, current_route_fingerprint(home))

    def test_public_task_repository_is_rejected_before_receipts_and_results(self):
        instance = object.__new__(router.Router)
        with patch.object(router, "api", return_value={"private": False}) as remote, patch.object(instance, "reconcile_steers") as receipts, patch.object(instance, "reap") as results:
            with self.assertRaisesRegex(RuntimeError, "private"):
                instance.tick()
            remote.assert_called_once()
            receipts.assert_not_called()
            results.assert_not_called()

    def test_public_destination_never_receives_new_room_preview(self):
        instance = object.__new__(router.Router)
        instance.state = {"rooms": {}}
        with patch.object(router, "api", return_value={"private": False}) as remote, patch.object(instance, "room_body") as preview:
            with self.assertRaises(RuntimeError):
                instance.ensure_room("synthetic-session")
            remote.assert_called_once()
            preview.assert_not_called()


if __name__ == "__main__":
    unittest.main()
