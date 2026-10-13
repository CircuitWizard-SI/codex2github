"""Conversation-only reasoning effort values and read-only model capabilities."""
from pathlib import Path

from .config import codex_config, codex_preferences
from .route_models import _json, _provider, _cc_switch_catalog


EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
ALIASES = {"关闭": "none", "最少": "minimal", "低": "low", "中": "medium", "高": "high",
           "很高": "xhigh", "极高": "xhigh", "最大": "max", "极限": "ultra",
           "自动": "auto", "默认": "auto"}


def normalize(value: str) -> str:
    selected = ALIASES.get(value.strip().lower(), value.strip().lower())
    if selected not in (*EFFORTS, "auto"):
        raise ValueError("无效思考档位。使用 `/effort` 查看当前模型的档位，`/effort auto` 恢复默认。")
    return selected


def route_effort(home: Path) -> str:
    value = codex_preferences(home).get("model_reasoning_effort", "")
    return value if value in EFFORTS else "auto"


def _catalogs(home: Path) -> list:
    config = codex_config(home)
    configured = config.get("model_catalog_json")
    path = Path(configured).expanduser() if isinstance(configured, str) and configured else home / "cc-switch-model-catalog.json"
    if not path.is_absolute():
        path = home / path
    # Provider-specific metadata takes precedence over the native snapshot.
    snapshot = _json(path)
    rows = snapshot.get("models", snapshot.get("data", [])) if isinstance(snapshot, dict) else snapshot
    if not configured and _provider(config).get("base_url") and not any(
        isinstance(item, dict) and (item.get("slug") or item.get("id") or item.get("model")) == config.get("model")
        for item in (rows if isinstance(rows, list) else [])
    ):
        snapshot = {}
    return [snapshot, _cc_switch_catalog(home, _provider(config))]


def _model_efforts(catalogs: list, model: str) -> dict:
    result = {"levels": list(EFFORTS), "default": "auto", "verified": False}
    for catalog in catalogs:
        rows = catalog.get("models", catalog.get("data", [])) if isinstance(catalog, dict) else catalog
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict) or (item.get("slug") or item.get("id") or item.get("model")) != model:
                continue
            raw = next((item[key] for key in ("supported_reasoning_levels", "supported_reasoning_efforts", "supportedReasoningEfforts", "reasoningLevels", "reasoning_levels") if key in item), None)
            if not isinstance(raw, list):
                continue
            names = set()
            for level in raw:
                value = level.get("effort", level.get("reasoningEffort")) if isinstance(level, dict) else level
                if isinstance(value, str) and value in EFFORTS:
                    names.add(value)
            default = item.get("default_reasoning_level") or item.get("defaultReasoningEffort") or item.get("default_reasoning_effort")
            result = {"levels": [level for level in EFFORTS if level in names],
                      "default": default if isinstance(default, str) and default in names else "auto", "verified": True}
    return result


def model_efforts(home: Path, model: str) -> dict:
    return _model_efforts(_catalogs(home), model)


def model_efforts_many(home: Path, models: list[str]) -> dict[str, dict]:
    catalogs = _catalogs(home)
    return {model: _model_efforts(catalogs, model) for model in models}


def default_effort(home: Path, model: str) -> str:
    default = route_effort(home)
    options = model_efforts(home, model)
    if options["verified"] and default not in options["levels"]:
        return options["default"]
    return default


def validate(home: Path, model: str, effort: str) -> str:
    selected = normalize(effort)
    info = model_efforts(home, model)
    if selected != "auto" and info["verified"] and selected not in info["levels"]:
        raise ValueError(f"模型 `{model}` 的本机目录不支持思考档位 `{selected}`。可选：" + " / ".join(info["levels"]) + "。")
    return selected
