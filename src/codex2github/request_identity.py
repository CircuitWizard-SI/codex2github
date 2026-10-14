"""Project-local request identification, without changing Codex configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import struct
import subprocess

from .config import SETTINGS


def desktop_archives() -> list[Path]:
    """Find the running or installed Desktop package using read-only metadata."""
    if os.name != "nt":
        return []
    script = (
        "$ErrorActionPreference='Stop'; $paths=@(); "
        "Get-CimInstance Win32_Process -Filter \"Name='ChatGPT.exe'\" | "
        "Where-Object { $_.ExecutablePath -like '*OpenAI.Codex*' } | "
        "ForEach-Object { $paths += Join-Path (Split-Path $_.ExecutablePath) 'resources/app.asar' }; "
        "if (!$paths) { Get-AppxPackage -Name OpenAI.Codex | ForEach-Object { "
        "$paths += Join-Path $_.InstallLocation 'app/resources/app.asar' } }; "
        "ConvertTo-Json -InputObject @($paths | Select-Object -Unique) -Compress"
    )
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("Cannot read installed Codex Desktop metadata")
    return [Path(value) for value in json.loads(result.stdout)]


def desktop_version(archive: Path) -> str:
    """Read only package.json from ASAR; do not unpack or execute application code."""
    with archive.open("rb") as stream:
        _, header_size, _, json_size = struct.unpack("<4I", stream.read(16))
        if not 0 < json_size <= 16 * 1024 * 1024 or header_size < json_size + 8:
            raise ValueError("Invalid Desktop package header")
        header = json.loads(stream.read(json_size))
        item = header["files"]["package.json"]
        if item.get("unpacked") or not 0 < item["size"] <= 128 * 1024:
            raise ValueError("Invalid Desktop package metadata")
        offset = int(item["offset"])
        if offset < 0:
            raise ValueError("Invalid Desktop package offset")
        stream.seek(8 + header_size + offset)
        package = json.loads(stream.read(item["size"]))
    version = package.get("version", "")
    if package.get("name") != "openai-codex-electron" or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2,3}", version):
        raise ValueError("Unrecognized Codex Desktop package")
    return version


def client_info() -> dict:
    if SETTINGS.user_agent_profile == "default":
        return {"name": "codex2github", "version": "7"}
    for archive in desktop_archives():
        try:
            version = desktop_version(archive)
        except (OSError, ValueError, KeyError, TypeError, struct.error):
            continue
        return {"name": "Codex Desktop", "version": version}
    raise RuntimeError("Desktop User Agent profile requires a readable installed Codex Desktop package; no alternate identity was used")


def codex_environment() -> dict:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(SETTINGS.codex_home)
    if SETTINGS.user_agent_profile == "desktop":
        environment.update(CODEX_INTERNAL_ORIGINATOR_OVERRIDE="Codex Desktop",
                           TERM="unknown", TERM_PROGRAM="unknown", TERM_PROGRAM_VERSION="")
    return environment


def verify_user_agent(agent: str, info: dict):
    if SETTINGS.user_agent_profile == "desktop" and not (
            agent.startswith("Codex Desktop/")
            and agent.endswith(f" unknown (Codex Desktop; {info['version']})")):
        raise RuntimeError("Codex did not confirm the requested Desktop unknown User Agent; task was not submitted")
