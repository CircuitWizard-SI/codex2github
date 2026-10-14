"""Bridge configuration; Codex and CC Switch configuration stays read-only."""
from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
import os
from pathlib import Path
import re
import tomllib


SOURCE_REPOSITORY = "CircuitWizard-SI/codex2github"


def is_private_task_repository(metadata: dict) -> bool:
    """A source template must never receive conversations or task output."""
    name = str(metadata.get("full_name", "")).casefold()
    return (metadata.get("private") is True and metadata.get("is_template") is not True
            and name != SOURCE_REPOSITORY.casefold())


def resolve_path(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def config_path() -> Path:
    return Path(os.environ.get("CODEX2GITHUB_CONFIG", "config.json")).expanduser().resolve()


def codex_config(home: Path) -> dict:
    """Read Codex configuration and apply its selected profile, without writes."""
    path = home / "config.toml"
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    profile = data.get("profile")
    if isinstance(profile, str):
        selected = data.get("profiles", {}).get(profile, {})
        if isinstance(selected, dict):
            data = {**data, **selected}
    return data


def codex_preferences(home: Path) -> dict[str, str]:
    """Read the active Codex route without exposing or writing secrets."""
    data = codex_config(home)
    result = {}
    for key in ("model", "model_provider", "model_reasoning_effort", "service_tier"):
        value = data.get(key)
        if isinstance(value, str) and value:
            result[key] = value
    return result


def current_route_model(home: Path) -> str:
    """Return only the current route's model ID, or an empty string."""
    return codex_preferences(home).get("model", "")


def cc_switch_route_identity(home: Path) -> dict:
    """Read the active CC Switch route selectors without exposing credentials."""
    root = home.parent / ".cc-switch"
    result = {}
    for name in ("settings.json", "live-state.json"):
        path = root / name
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError, TypeError):
            data = {}
        if name == "settings.json" and isinstance(data, dict):
            result["currentProviderCodex"] = data.get("currentProviderCodex")
            if "enableStackMode" in data:
                result["enableStackMode"] = data.get("enableStackMode")
        elif name == "live-state.json" and isinstance(data, dict):
            apps = data.get("apps", {})
            codex = apps.get("codex", {}) if isinstance(apps, dict) else {}
            if isinstance(codex, dict):
                stack = codex.get("stack")
                result["liveCodex"] = {
                    "mode": codex.get("mode"),
                    "proxy_route": codex.get("proxy_route"),
                    "stack": ({
                        "enabled": stack.get("enabled"),
                        "members": stack.get("members", []),
                    } if isinstance(stack, dict) else None),
                }
    return result


def current_route_fingerprint(home: Path) -> str:
    """Identify the current local route without changing any Codex file.

    The complete config file is included because a CC Switch route can change
    provider or endpoint while keeping the same model ID.  Such a change must
    invalidate conversation-local model overrides as well.
    """
    path = home / "config.toml"
    try:
        raw = path.read_bytes()
    except OSError:
        raw = b"<missing>"
    payload = {"config_sha256": hashlib.sha256(raw).hexdigest(),
               "preferences": codex_preferences(home),
               "cc_switch_route": cc_switch_route_identity(home)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Settings:
    path: Path
    repo: str
    model: str
    codex_home: Path
    workspace: Path
    runtime_dir: Path
    poll_seconds: int = 15
    sync_seconds: int = 30
    recovery_seconds: int = 900
    user_agent_profile: str = "default"

    def require_configured(self):
        if not self.path.is_file():
            raise ValueError("Run 'codex2github init --repo OWNER/PRIVATE_REPO' first")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo) or self.repo.startswith("OWNER/"):
            raise ValueError("repo must name your dedicated private task repository")
        if self.model != "auto" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,150}", self.model):
            raise ValueError("model must be auto or a valid per-conversation model ID")
        if min(self.poll_seconds, self.sync_seconds, self.recovery_seconds) < 1:
            raise ValueError("Polling and recovery intervals must be positive")
        if self.user_agent_profile not in {"default", "desktop"}:
            raise ValueError("user_agent_profile must be default or desktop")


def load_settings(path: Path | None = None) -> Settings:
    path = (path or config_path()).resolve()
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a JSON object")
    if data.get("desktop_ipc", False):
        raise ValueError("desktop_ipc has been removed; this bridge never updates Desktop settings")
    root = path.parent
    codex_home = resolve_path(data.get("codex_home") or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"), root)
    return Settings(path=path, repo=data.get("repo", ""), model=data.get("model") or "auto",
                    codex_home=codex_home, workspace=resolve_path(data.get("workspace", "workspace"), root),
                    runtime_dir=resolve_path(data.get("runtime_dir", ".runtime"), root),
                    poll_seconds=int(data.get("poll_seconds", 15)),
                    sync_seconds=int(data.get("sync_seconds", 30)),
                    recovery_seconds=int(data.get("recovery_seconds", 900)),
                    user_agent_profile=data.get("user_agent_profile", "default"))


SETTINGS = load_settings()
