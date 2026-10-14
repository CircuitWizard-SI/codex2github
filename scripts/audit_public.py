"""Check the tracked source tree before pushing; never scan external credentials."""
import ast
from pathlib import Path
import re
import subprocess
import zipfile
import tarfile
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
BLOCKED = {".runtime", ".test-runtime", ".venv", "workspace", "vendor", "desktop-source", "protocol", "repository", "jobs", "sessions", "archived_sessions"}
SECRET = re.compile(r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{20,})")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}\b")
PRIVATE_PATH = re.compile(r"(?:[A-Za-z]:[/\\]Users[/\\][^\s'\"<>]+|[A-Za-z]:[/\\]Wechat[/\\]xwechat_files[/\\]|\bwxid_[A-Za-z0-9_]+)", re.I)
EMAIL = re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def anonymous_email(value: str) -> bool:
    return value.strip().lower().endswith("@users.noreply.github.com")


def inspect(name: str, raw: bytes) -> list[tuple[str, str]]:
    path = Path(name)
    findings = []
    if any(part in BLOCKED for part in path.parts) or path.name in {"config.json", "auth.json", "conversation-state.json", "session-events.json"} or path.name.startswith("config.local") or path.name.startswith(".env") or path.suffix in {".sqlite", ".db", ".jsonl", ".log", ".lock"} or path.name.endswith(".jsonl.zst"):
        findings.append((name, "private/generated runtime path"))
    data = raw.decode("utf-8", errors="replace")
    if SECRET.search(data):
        findings.append((name, "credential-like literal"))
    if PRIVATE_KEY.search(data) or JWT.search(data):
        findings.append((name, "private key or authentication token candidate"))
    if PRIVATE_PATH.search(data):
        findings.append((name, "personal machine path or identifier"))
    if any(not anonymous_email(email) and email.rpartition("@")[2].lower() not in {"example.com", "example.invalid"}
           for email in EMAIL.findall(data)):
        findings.append((name, "personal contact email candidate"))
    if name.endswith(".py"):
        try:
            ast.parse(data, feature_version=(3, 11))
        except SyntaxError:
            findings.append((name, "not Python 3.11 compatible"))
    return findings


def inspect_history() -> tuple[list[tuple[str, str]], int, int]:
    findings, blobs, paths = [], {}, set()
    commits = subprocess.check_output(["git", "rev-list", "--all"], cwd=ROOT).decode().splitlines()
    for commit in commits:
        fields = subprocess.check_output(["git", "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B", commit], cwd=ROOT).decode("utf-8").split("\0", 4)
        if not anonymous_email(fields[1]) or not anonymous_email(fields[3]):
            findings.append(("commit:" + commit[:12], "non-anonymous author or committer email"))
        findings.extend(inspect("commit:" + commit[:12], (fields[0] + "\n" + fields[2] + "\n" + fields[4]).encode()))
        tree = subprocess.check_output(["git", "ls-tree", "-r", "-z", commit], cwd=ROOT)
        for row in tree.split(b"\0"):
            if not row:
                continue
            fields, raw_name = row.split(b"\t", 1)
            _, kind, sha = fields.decode().split()
            if kind != "blob":
                continue
            name = raw_name.decode("utf-8")
            if name not in paths:
                findings.extend(inspect(name, b""))
                paths.add(name)
            blobs.setdefault(sha, name)
    for sha, name in blobs.items():
        content = subprocess.check_output(["git", "cat-file", "blob", sha], cwd=ROOT)
        findings.extend(inspect("history:" + name, content))
    return findings, len(commits), len(blobs)


def main():
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")[:-1]
    findings = []
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8")) if (ROOT / "pyproject.toml").is_file() else {}
    if metadata:
        namespace = ast.parse((ROOT / "src/codex2github/__init__.py").read_text(encoding="utf-8"))
        version = next((ast.literal_eval(node.value) for node in namespace.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)), None)
        if version != metadata.get("project", {}).get("version"):
            findings.append(("pyproject.toml", "package and module version mismatch"))
    for name in files:
        if not (ROOT / name).is_file():
            continue
        findings.extend(inspect(name, (ROOT / name).read_bytes()))
    artifact_entries = 0
    history_counts = None
    if "--history" in sys.argv[1:]:
        history_findings, commits, blobs = inspect_history()
        findings.extend(history_findings)
        history_counts = commits, blobs
    for argument in sys.argv[1:]:
        if argument == "--history":
            continue
        artifact = Path(argument)
        if artifact.suffix in {".whl", ".zip"}:
            with zipfile.ZipFile(artifact) as archive:
                for name in archive.namelist():
                    if name.endswith("/"):
                        continue
                    findings.extend(inspect(name, archive.read(name)))
                    artifact_entries += 1
        else:
            with tarfile.open(artifact) as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    findings.extend(inspect(member.name, archive.extractfile(member).read()))
                    artifact_entries += 1
    for name, reason in findings:
        print(f"FAIL: {name}: {reason}")
    print(f"{'PASS' if not findings else 'FAIL'}: {len(files)} tracked files checked")
    if artifact_entries:
        print(f"{'PASS' if not findings else 'FAIL'}: {artifact_entries} package entries checked")
    if history_counts:
        print(f"{'PASS' if not findings else 'FAIL'}: {history_counts[0]} commits and {history_counts[1]} historical blobs checked")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
