"""Read model directories for the active Codex route; never write local settings."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tomllib
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import codex_config, current_route_fingerprint


MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,150}")
OTHER_TASK = re.compile(
    r"(?:^|[/._-])(?:image|imagen|audio|realtime|embedding|embed|tts|whisper|"
    r"speech|transcription|transcribe|video|sora)(?:$|[/._-])", re.I
)


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}


def _models(catalog) -> set[str]:
    if isinstance(catalog, dict):
        catalog = catalog.get("data", catalog.get("models", []))
    if not isinstance(catalog, list):
        return set()
    result = set()
    for item in catalog:
        name = item
        if isinstance(item, dict):
            name = item.get("slug") or item.get("id") or item.get("model")
            outputs = item.get("output_modalities")
            if isinstance(outputs, list) and outputs and "text" not in outputs:
                continue
        if isinstance(name, str) and MODEL_ID.fullmatch(name) and not OTHER_TASK.search(name):
            result.add(name)
    return result


def _provider(config: dict) -> dict:
    providers = config.get("model_providers", {})
    value = providers.get(config.get("model_provider"), {}) if isinstance(providers, dict) else {}
    return value if isinstance(value, dict) else {}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward a route's credentials to a redirected endpoint.
        return None


def _endpoint_models(home: Path, provider: dict) -> set[str] | None:
    base = provider.get("base_url")
    if not isinstance(base, str):
        return None
    try:
        parts = urlsplit(base)
    except ValueError:
        return None
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        return None
    endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/models", parts.query, ""))
    headers = {"Accept": "application/json"}
    static = provider.get("http_headers", {})
    if isinstance(static, dict):
        headers.update({key: value for key, value in static.items()
                        if isinstance(key, str) and isinstance(value, str)})
    env_headers = provider.get("env_http_headers", {})
    if isinstance(env_headers, dict):
        headers.update({key: os.environ[var] for key, var in env_headers.items()
                        if isinstance(key, str) and isinstance(var, str) and os.environ.get(var)})
    key = provider.get("experimental_bearer_token")
    env_key = provider.get("env_key")
    if isinstance(env_key, str):
        key = os.environ.get(env_key)
    if not key:
        auth = _json(home / "auth.json")
        key = auth.get("OPENAI_API_KEY") if isinstance(auth, dict) else None
    if isinstance(key, str) and key:
        headers["Authorization"] = "Bearer " + key
    try:
        request = Request(endpoint, headers=headers, method="GET")
        with build_opener(_NoRedirect()).open(request, timeout=8) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            return None
        data = json.loads(raw)
        entries = data.get("data", data.get("models")) if isinstance(data, dict) else data
        return _models(entries) if isinstance(entries, list) else None
    except (OSError, ValueError, TypeError):
        # Never copy response bodies, credentials or request URLs into replies.
        return None


def _cc_switch_catalog(home: Path, provider: dict):
    """Use only a selected provider whose endpoint matches the actual Codex route."""
    root = home.parent / ".cc-switch"
    settings = _json(root / "settings.json")
    selected = settings.get("currentProviderCodex") if isinstance(settings, dict) else None
    path = root / "cc-switch.db"
    if not isinstance(selected, str) or not path.is_file():
        return {}
    try:
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)) as connection:
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                "SELECT settings_config FROM providers WHERE app_type = ? AND id = ?", ("codex", selected)
            ).fetchone()
        stored = json.loads(row[0]) if row else {}
        config = tomllib.loads(stored.get("config", ""))
        expected, actual = _provider(config).get("base_url"), provider.get("base_url")
        if actual and (not isinstance(expected, str) or expected.rstrip("/") != actual.rstrip("/")):
            return {}
        return stored.get("modelCatalog", {})
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        return {}


def model_catalog(home: Path, *, refresh: bool = False) -> list[str]:
    """Query the active route on explicit model commands, using local fallback only.

    Idle watcher ticks and Router construction perform no network requests.
    Remote results replace an incomplete snapshot instead of mixing other routes.
    """
    config = codex_config(home)
    provider = _provider(config)
    before = current_route_fingerprint(home)
    names = _endpoint_models(home, provider) if refresh else None
    # A local route switch during the lookup must not expose the old result.
    if refresh and before != current_route_fingerprint(home):
        return model_catalog(home)
    if names is None:
        configured = config.get("model_catalog_json")
        path = Path(configured).expanduser() if isinstance(configured, str) and configured else home / "cc-switch-model-catalog.json"
        if not path.is_absolute():
            path = home / path
        names = _models(_json(path))
        # An implicit snapshot lacking this route's default belongs to another
        # route.  An explicitly configured catalog is owned by the active route.
        if not configured and provider.get("base_url") and config.get("model") not in names:
            names = set()
        names |= _models(_cc_switch_catalog(home, provider))
    default = config.get("model")
    if isinstance(default, str) and MODEL_ID.fullmatch(default):
        names.add(default)
    # ccs-* IDs only make sense with CC Switch's local proxy, never a direct API.
    base = provider.get("base_url")
    try:
        host = urlsplit(base).hostname if isinstance(base, str) else None
    except ValueError:
        host = None
    if host not in ("127.0.0.1", "localhost", "::1"):
        names = {name for name in names if not name.startswith("ccs-")}
    return sorted(names)
