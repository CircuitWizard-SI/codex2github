from tests import TEST_ROOT
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex2github import route_models


class RouteModelsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.root = Path(self.temp.name)
        self.home = self.root / "codex"
        self.home.mkdir()
        self.config = self.home / "config.toml"
        self.write_route("https://route-a.example/v1")
        (self.home / "cc-switch-model-catalog.json").write_text(
            json.dumps({"models": [{"slug": "default-model"}]}), encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def write_route(self, endpoint, model="default-model"):
        self.config.write_text(
            f'model = "{model}"\nmodel_provider = "custom"\n[model_providers.custom]\n'
            f'base_url = "{endpoint}"\nexperimental_bearer_token = "fixture-key"\n', encoding="utf-8"
        )

    def response(self, models):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"data": models}).encode()
        return response

    def test_live_models_command_path_queries_endpoint_and_expands_single_model_snapshot(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                requests.append((handler.path, handler.headers.get("Authorization")))
                handler.send_response(200)
                handler.end_headers()
                handler.wfile.write(json.dumps({"data": [
                    {"id": "route-text-a"}, {"id": "route-text-b"}, {"id": "gpt-image-2"},
                    {"id": "gpt-4o-realtime-preview"}, {"id": "gpt-4o-audio-preview"},
                    {"id": "another-image-model", "output_modalities": ["image"]},
                ]}).encode())

            def log_message(handler, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.write_route(f"http://127.0.0.1:{server.server_port}/v1")
            before = {p: p.read_bytes() for p in self.home.iterdir() if p.is_file()}
            self.assertEqual(route_models.model_catalog(self.home), ["default-model"])
            self.assertEqual(requests, [])  # Idle watcher is network-free.
            self.assertEqual(route_models.model_catalog(self.home, refresh=True),
                             ["default-model", "route-text-a", "route-text-b"])
            self.assertEqual(requests, [("/v1/models", "Bearer fixture-key")])
            self.assertEqual({p: p.read_bytes() for p in self.home.iterdir() if p.is_file()}, before)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_route_switch_requeries_new_endpoint_and_excludes_old_models(self):
        with patch.object(route_models, "build_opener") as opener:
            opener.return_value.open.side_effect = [
                self.response([{"id": "route-a-model"}]), self.response([{"id": "route-b-model"}])
            ]
            self.assertEqual(route_models.model_catalog(self.home, refresh=True), ["default-model", "route-a-model"])
            self.write_route("https://route-b.example/v1", "route-b-default")
            self.assertEqual(route_models.model_catalog(self.home, refresh=True), ["route-b-default", "route-b-model"])
            urls = [call.args[0].full_url for call in opener.return_value.open.call_args_list]
            self.assertEqual(urls, ["https://route-a.example/v1/models", "https://route-b.example/v1/models"])

    def test_lookup_failure_only_uses_matching_active_provider_and_leaves_db_unchanged(self):
        cc_switch = self.root / ".cc-switch"
        cc_switch.mkdir()
        (cc_switch / "settings.json").write_text(json.dumps({"currentProviderCodex": "active"}), encoding="utf-8")
        database = cc_switch / "cc-switch.db"
        with contextlib.closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE providers(id TEXT, app_type TEXT, settings_config TEXT)")
            route = self.config.read_text(encoding="utf-8")
            connection.executemany("INSERT INTO providers VALUES(?,?,?)", [
                ("active", "codex", json.dumps({"config": route, "modelCatalog": {"models": [{"model": "active-model"}]}})),
                ("inactive", "codex", json.dumps({"config": route, "modelCatalog": {"models": [{"model": "inactive-model"}]}})),
            ])
            connection.commit()
        before = hashlib.sha256(database.read_bytes()).digest()
        with patch.object(route_models, "build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError("untrusted response must stay private")
            self.assertEqual(route_models.model_catalog(self.home, refresh=True), ["active-model", "default-model"])
            self.write_route("https://route-b.example/v1")
            self.assertEqual(route_models.model_catalog(self.home, refresh=True), ["default-model"])
        self.assertEqual(hashlib.sha256(database.read_bytes()).digest(), before)

    def test_selected_profile_and_relative_catalog_are_read(self):
        self.config.write_text(
            'model = "inactive-model"\nprofile = "work"\n[profiles.work]\n'
            'model = "profile-model"\nmodel_catalog_json = "catalogs/work.json"\n', encoding="utf-8"
        )
        catalog = self.home / "catalogs" / "work.json"
        catalog.parent.mkdir()
        catalog.write_text(json.dumps({"models": [{"slug": "profile-other-model"}]}), encoding="utf-8")
        self.assertEqual(route_models.model_catalog(self.home), ["profile-model", "profile-other-model"])

    def test_route_change_during_lookup_does_not_return_old_route_models(self):
        def switch(*args, **kwargs):
            self.write_route("https://route-b.example/v1", "route-b-default")
            return self.response([{"id": "route-a-model"}])
        with patch.object(route_models, "build_opener") as opener:
            opener.return_value.open.side_effect = switch
            models = route_models.model_catalog(self.home, refresh=True)
        self.assertNotIn("route-a-model", models)
        self.assertIn("route-b-default", models)

    def test_credentialed_directory_lookup_does_not_follow_redirect(self):
        self.assertIsNone(route_models._NoRedirect().redirect_request(
            None, None, 302, "moved", {}, "https://other.example/models"
        ))


if __name__ == "__main__":
    unittest.main()
