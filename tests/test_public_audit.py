from tests import TEST_ROOT
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("source_audit", Path(__file__).resolve().parents[1] / "scripts/audit_public.py")
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class PublicAuditTests(unittest.TestCase):
    def run_git(self, root, *arguments):
        return subprocess.run(["git", *arguments], cwd=root, capture_output=True, check=True)

    def commit(self, root, email="123+example-user@users.noreply.github.com"):
        self.run_git(root, "-c", "user.name=example-user", "-c", "user.email=" + email, "commit", "-m", "Synthetic fixture")

    def test_removed_private_file_is_detected_in_history(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            root = Path(temporary)
            self.run_git(root, "init", "-b", "main")
            (root / "config.json").write_text('{}', encoding="utf-8")
            self.run_git(root, "add", "config.json")
            self.commit(root)
            self.run_git(root, "rm", "config.json")
            self.commit(root)
            with patch.object(AUDIT, "ROOT", root), patch.object(sys, "argv", ["audit_public.py", "--history"]), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(AUDIT.main(), 1)
            self.assertIn("private/generated runtime path", output.getvalue())

    def test_commit_contact_email_is_detected_without_printing_the_email(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            root = Path(temporary)
            self.run_git(root, "init", "-b", "main")
            (root / "README.md").write_text('Generic project', encoding="utf-8")
            self.run_git(root, "add", "README.md")
            email = "private" + "@" + "example.invalid"
            self.commit(root, email)
            with patch.object(AUDIT, "ROOT", root), patch.object(sys, "argv", ["audit_public.py", "--history"]), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(AUDIT.main(), 1)
            self.assertIn("non-anonymous author or committer email", output.getvalue())
            self.assertNotIn(email, output.getvalue())

    def test_private_key_and_jwt_candidates_are_detected_without_their_values(self):
        key = "-----BEGIN " + "RSA PRIVATE KEY-----\n" + "synthetic data"
        jwt = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 25
        for value in (key, jwt):
            findings = AUDIT.inspect("fixture.txt", value.encode())
            self.assertTrue(any("private key or authentication token" in reason for _, reason in findings))
            self.assertNotIn(value, str(findings))

    def test_mismatched_package_and_module_versions_fail_the_release_audit(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            root = Path(temporary)
            self.run_git(root, "init", "-b", "main")
            (root / "pyproject.toml").write_text('[project]\nversion = "0.1.1"\n', encoding="utf-8")
            package = root / "src/codex2github"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
            self.run_git(root, "add", ".")
            with patch.object(AUDIT, "ROOT", root), patch.object(sys, "argv", ["audit_public.py"]), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(AUDIT.main(), 1)
            self.assertIn("package and module version mismatch", output.getvalue())


if __name__ == "__main__":
    unittest.main()
