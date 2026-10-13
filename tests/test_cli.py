from tests import TEST_ROOT
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CliTests(unittest.TestCase):
    def test_init_then_help_from_unrelated_workdir_preserves_config_and_no_runtime(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            root = Path(temporary)
            config = root / "config.json"
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            result = subprocess.run([sys.executable, "-m", "codex2github", "--config", str(config), "init",
                                     "--repo", "example-user/tasks", "--model", "custom/model"],
                                    cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(config.is_file())
            self.assertFalse((root / ".runtime").exists())
            rejected = subprocess.run([sys.executable, "-m", "codex2github", "--config", str(config), "init",
                                       "--repo", "example-user/other", "--model", "custom/model"],
                                      cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(rejected.returncode, 1)


if __name__ == "__main__":
    unittest.main()
