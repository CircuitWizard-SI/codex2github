"""Remove local build timestamps, ZIP comments and extra fields from releases.

Entry payloads and names remain unchanged. Wheel RECORD hashes remain valid.
"""
from pathlib import Path
import sys
import zipfile


FIXED_TIME = (2000, 1, 1, 0, 0, 0)


def normalize(path: Path):
    path = Path(path).resolve()
    if path.suffix not in {".zip", ".whl"}:
        raise ValueError("Expected a source ZIP or wheel")
    temporary = path.with_name(path.name + ".normalized")
    try:
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(temporary, "w") as target:
            target.comment = b""
            names = source.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Archive contains duplicate names")
            for original in sorted(source.infolist(), key=lambda item: item.filename):
                clean = zipfile.ZipInfo(original.filename, FIXED_TIME)
                clean.create_system = 3
                clean.external_attr = ((0o40755 << 16) | 0x10) if original.is_dir() else (0o100644 << 16)
                clean.compress_type = zipfile.ZIP_DEFLATED
                clean.extra = clean.comment = b""
                target.writestr(clean, source.read(original))
        with zipfile.ZipFile(path) as before, zipfile.ZipFile(temporary) as after:
            if set(before.namelist()) != set(after.namelist()) or any(
                    before.read(name) != after.read(name) for name in before.namelist()):
                raise ValueError("Normalized archive payload differs")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python scripts/normalize_release.py PACKAGE.whl SOURCE.zip")
    for argument in sys.argv[1:]:
        normalize(Path(argument))
        print("Normalized: " + Path(argument).name)
