from tests import TEST_ROOT
import json
from pathlib import Path
import tempfile
import unittest

from codex2github.reasoning import default_effort, model_efforts, normalize, validate


class ReasoningTests(unittest.TestCase):
    def test_read_only_catalog_and_model_compatible_default(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            home = Path(temporary)
            config = home / "config.toml"
            config.write_text('model = "fixture"\nmodel_reasoning_effort = "high"\n', encoding="utf-8")
            catalog = home / "cc-switch-model-catalog.json"
            catalog.write_text(json.dumps({"models": [
                {"slug": "fixture", "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "xhigh"}], "defaultReasoningEffort": "low"},
                {"slug": "no-reasoning", "supported_reasoning_levels": []}
            ]}), encoding="utf-8")
            before = config.read_bytes(), catalog.read_bytes()
            self.assertEqual(model_efforts(home, "fixture")["levels"], ["low", "xhigh"])
            self.assertEqual(default_effort(home, "fixture"), "low")
            self.assertFalse(model_efforts(home, "unknown")["verified"])
            self.assertEqual(validate(home, "fixture", "自动"), "auto")
            self.assertEqual(normalize("极限"), "ultra")
            with self.assertRaisesRegex(ValueError, "不支持"):
                validate(home, "no-reasoning", "high")
            self.assertEqual(before, (config.read_bytes(), catalog.read_bytes()))


if __name__ == "__main__":
    unittest.main()
