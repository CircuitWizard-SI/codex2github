from tests import TEST_ROOT
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile


SPEC = importlib.util.spec_from_file_location("normalize_release", Path(__file__).resolve().parents[1] / "scripts/normalize_release.py")
NORMALIZER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NORMALIZER)
AUDIT_SPEC = importlib.util.spec_from_file_location("metadata_audit", Path(__file__).resolve().parents[1] / "scripts/audit_public.py")
AUDIT = importlib.util.module_from_spec(AUDIT_SPEC)
AUDIT_SPEC.loader.exec_module(AUDIT)


class ReleaseMetadataTests(unittest.TestCase):
    def test_metadata_is_removed_payloads_preserved_and_output_reproducible(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temporary:
            paths = [Path(temporary) / name for name in ("first.whl", "second.whl")]
            for index, path in enumerate(paths):
                with zipfile.ZipFile(path, "w") as archive:
                    archive.comment = f"local-commit-{index}".encode()
                    item = zipfile.ZipInfo("package/source.py", (2023 + index, 2, 3, 4, 5, 6))
                    item.comment = b"local metadata"
                    item.extra = b"\x55\x54\x05\x00\x01\x01\x02\x03\x04"
                    item.external_attr = 0o100666 << 16
                    archive.writestr(item, b"payload")
                with zipfile.ZipFile(path) as archive:
                    self.assertTrue(AUDIT.inspect_archive_metadata(path.name, archive))
                NORMALIZER.normalize(path)
                with zipfile.ZipFile(path) as archive:
                    self.assertEqual(archive.read("package/source.py"), b"payload")
                    self.assertEqual(archive.comment, b"")
                    item = archive.infolist()[0]
                    self.assertEqual(item.date_time, NORMALIZER.FIXED_TIME)
                    self.assertEqual(item.extra, b"")
                    self.assertEqual(item.comment, b"")
                    self.assertEqual(item.external_attr >> 16, 0o100644)
                    self.assertEqual(AUDIT.inspect_archive_metadata(path.name, archive), [])
            hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
            self.assertEqual(hashes[0], hashes[1])


if __name__ == "__main__":
    unittest.main()
