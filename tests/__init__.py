"""Every test uses synthetic data and a runtime beneath this checkout."""
import json
import os
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parents[1] / ".test-runtime"
TEST_ROOT.mkdir(exist_ok=True)
TEST_CONFIG = TEST_ROOT / "config.json"
TEST_CONFIG.write_text(json.dumps({"repo": "example-user/private-tasks", "model": "test-model",
                                  "codex_home": "home", "workspace": "workspace", "runtime_dir": ".",
                                  }), encoding="utf-8")
os.environ["CODEX2GITHUB_CONFIG"] = str(TEST_CONFIG)
