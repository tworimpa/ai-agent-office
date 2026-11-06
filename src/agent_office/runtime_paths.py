"""Stable defaults; candidates must supply their own --db path."""
import os
from pathlib import Path
import sys


def default_db_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")) / "AI-Agent-Office/stable/tasks.sqlite3"
    return Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")) / "ai-agent-office/stable/tasks.sqlite3"


def resolve_db_path(explicit: Path | None) -> Path:
    return (explicit if explicit is not None else default_db_path()).expanduser().resolve()
