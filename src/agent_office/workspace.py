"""Create isolated Git worktrees for tasks; never edit the source checkout in place."""

from __future__ import annotations

import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str


def create_worktree(repo: Path) -> Worktree:
    repo = repo.expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"Repository directory not found: {repo}")
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    root = Path(result.stdout.strip()).resolve()
    if root != repo:
        raise ValueError(f"Use repository root {root}, not {repo}")

    short_name = re.sub(r"[^a-zA-Z0-9_-]", "-", repo.name)
    token = secrets.token_hex(5)
    branch = f"ao/task-{token}"
    path = repo.parent / f"{short_name}-agent-worktrees" / f"task-{token}"
    path.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(path), "HEAD"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    return Worktree(path=path, branch=branch)


def git_summary(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--short"], cwd=workspace,
        capture_output=True, text=True, check=False,
    )
    return result.stdout[:4000] if result.returncode == 0 else "(git status unavailable)"
