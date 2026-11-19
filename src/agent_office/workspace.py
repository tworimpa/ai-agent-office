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


def plan_worktree(repo: Path, run_id: str) -> Worktree:
    from .models import validate_id
    validate_id(run_id)
    repo = repo.expanduser().resolve()
    short_name = re.sub(r"[^a-zA-Z0-9_-]", "-", repo.name)
    return Worktree(repo.parent / f"{short_name}-agent-worktrees" / f"task-{run_id}", f"ao/task-{run_id}")


def create_worktree(repo: Path, *, planned: Worktree | None = None) -> Worktree:
    repo = repo.expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"Repository directory not found: {repo}")
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    root = Path(result.stdout.removesuffix("\n")).resolve()
    if root != repo:
        raise ValueError(f"Use repository root {root}, not {repo}")

    short_name = re.sub(r"[^a-zA-Z0-9_-]", "-", repo.name)
    token = secrets.token_hex(5)
    branch = planned.branch if planned else f"ao/task-{token}"
    path = planned.path if planned else repo.parent / f"{short_name}-agent-worktrees" / f"task-{token}"
    path.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(path), "HEAD"],
        cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return Worktree(path=path, branch=branch)


def git_summary(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--short"], cwd=workspace,
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    return result.stdout[:4000] if result.returncode == 0 else "(git status unavailable)"

def map_folders(root: Path, folders: list[str]) -> list[Path]:
    """Resolve each selection inside this checkout, rejecting links and junctions."""
    import stat

    if not folders:
        raise ValueError("Select at least one folder")
    mapped = []
    for folder in folders:
        relative = Path(folder)
        if relative.anchor or ".." in relative.parts:
            raise ValueError("Invalid selected folder")
        path = root
        for part in relative.parts:
            path = path / part
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError("Selected folder must be a directory inside the worktree")
        if not path.is_dir() or not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
            raise ValueError("Selected folder is missing from this checkout")
        mapped.append(path)
    return mapped


def read_git_prefix(root, *args):
    """Read up to 16 KiB of output from a Git command in a working directory."""
    with subprocess.Popen(
        ["git", "-c", "core.fsmonitor=false", "-c", "core.quotepath=false", "--no-pager", *args],
        cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as proc:
        data = proc.stdout.read(16385)
        if len(data) > 16384:
            proc.kill()
        code = proc.wait()
    if len(data) > 16384:
        return data[:16384], True
    if code:
        raise OSError("Git output unavailable")
    return data, False
