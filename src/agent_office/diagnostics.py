"""Offline environment checks; never read credentials or call remote models."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from .engines import ENGINE_COMMANDS, resolve_executable


def doctor(required: list[str]) -> int:
    reports = []
    for name in ["git", *dict.fromkeys(required)]:
        executable = resolve_executable(ENGINE_COMMANDS.get(name, name))
        if executable is None:
            reports.append({"name": name, "installed": False, "version": None})
            continue
        try:
            completed = subprocess.run(
                [executable, "--version"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15, check=False,
            )
            version = (completed.stdout or completed.stderr).strip().splitlines()
            reports.append({
                "name": name,
                "installed": completed.returncode == 0,
                "version": version[0][:250] if version else "unknown",
            })
        except (OSError, subprocess.TimeoutExpired) as exc:
            reports.append({"name": name, "installed": False, "error": str(exc)})

    result = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "executables": reports,
        "ready": all(item["installed"] for item in reports),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 1
