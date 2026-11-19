"""Ordered local workspace folders, stored separately from the task journal."""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import unicodedata
from uuid import uuid4

from .models import OfficeError, validate_db_file, validate_id


def _redirected(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _single_line(value: str) -> bool:
    return (isinstance(value, str) and bool(value.strip())
            and all(unicodedata.category(char) not in {"Cc", "Cs", "Zl", "Zp"} for char in value))


def _local_directory(value: str) -> Path:
    if not _single_line(value):
        raise ValueError("폴더의 로컬 절대 경로를 입력하세요.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.drive.startswith("\\\\"):
        raise ValueError("폴더의 로컬 절대 경로를 입력하세요.")
    try:
        for component in (*reversed(path.parents), path):
            if _redirected(component.lstat()):
                raise ValueError(f"링크를 거치는 폴더입니다. 실제 경로를 입력하세요: {component}")
        if not path.is_dir():
            raise ValueError(f"디렉터리가 아닙니다: {path}")
        return path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"로컬 폴더를 열 수 없습니다: {path}") from exc


def _git(folder: Path, *args: str, error: str, env=None) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=folder, stdin=subprocess.DEVNULL, check=True, capture_output=True,
                                encoding="utf-8", env=env, timeout=10)
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise ValueError(error) from exc
    return result.stdout.removesuffix("\n")


def read_workspace_revision(repo: Path) -> str:
    """Pin HEAD for a checkout already checked by the workspace catalog."""
    error = "작업에 사용할 기준 커밋을 확인할 수 없습니다."
    # This fixed Git read uses the same configured ownership trust as select().
    # An incomplete checkout must not attempt network access to resolve HEAD.
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_TERMINAL_PROMPT": "0"}
    revision = _git(repo, "--no-replace-objects", "-c", "core.fsmonitor=false", "rev-parse",
                    "--verify", "HEAD^{commit}", error=error, env=env)
    if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
        raise ValueError(error)
    return revision


class StudioCatalog:
    def __init__(self, db_path: Path):
        journal = Path(db_path).expanduser().resolve()
        self.path = journal.with_name(journal.name + ".studio.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("CREATE TABLE IF NOT EXISTS workspaces "
                       "(id TEXT PRIMARY KEY, name TEXT NOT NULL, folders TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            pass
        else:
            if _redirected(info) or not stat.S_ISREG(info.st_mode):
                raise ValueError("작업공간 목록을 저장할 일반 파일이 필요합니다.")
            validate_db_file(self.path)
        return sqlite3.connect(self.path)

    def create(self, name: str, paths: list[str]) -> dict:
        if not _single_line(name):
            raise ValueError("작업공간 이름을 입력하세요.")
        if not isinstance(paths, list) or not paths:
            raise ValueError("등록할 폴더를 하나 이상 입력하세요.")
        directories = [_local_directory(value) for value in paths]
        if len(set(directories)) != len(directories):
            raise ValueError("중복된 폴더를 제거하세요.")
        folders = [str(path) for path in directories]
        workspace_id = uuid4().hex
        name = name.strip()
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO workspaces(id, name, folders) VALUES (?, ?, ?)",
                       (workspace_id, name, json.dumps(folders, ensure_ascii=False)))
        return {"id": workspace_id, "name": name, "folders": folders}

    def list(self) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT id, name, folders FROM workspaces ORDER BY rowid")
            return [{"id": key, "name": name, "folders": json.loads(folders)}
                    for key, name, folders in rows]

    def get(self, workspace_id: str) -> dict:
        validate_id(workspace_id)
        with closing(self._connect()) as db:
            row = db.execute("SELECT name, folders FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        if row is None:
            raise OfficeError("NOT_FOUND")
        return {"id": workspace_id, "name": row[0], "folders": json.loads(row[1])}

    def select(self, workspace_id: str, indices: list[int]) -> tuple[Path, list[str]]:
        validate_id(workspace_id)
        with closing(self._connect()) as db:
            row = db.execute("SELECT folders FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if row is None:
            raise ValueError("등록된 작업공간을 찾을 수 없습니다.")
        folders = json.loads(row[0])
        if not isinstance(indices, list) or not indices:
            raise ValueError("작업 폴더를 하나 이상 선택하세요.")
        if any(type(index) is not int or not 0 <= index < len(folders) for index in indices):
            raise ValueError("등록된 폴더 목록에서 작업 폴더를 선택하세요.")
        if len(set(indices)) != len(indices):
            raise ValueError("같은 작업 폴더를 두 번 선택했습니다.")

        roots = set()
        relative_paths = []
        for index in indices:
            folder = _local_directory(folders[index])
            root = Path(_git(folder, "rev-parse", "--show-toplevel",
                             error=f"Git 작업 트리에 있는 폴더를 선택하세요: {folder}")).resolve()
            roots.add(root)
            if len(roots) > 1 or not folder.is_relative_to(root):
                raise ValueError("한 작업에서는 같은 Git 저장소의 같은 체크아웃에 있는 폴더를 선택하세요.")
            relative = folder.relative_to(root).as_posix()
            spec = "HEAD^{tree}" if relative == "." else f"HEAD:{relative}"
            error = f"현재 Git 커밋에 들어 있는 폴더를 선택하세요: {folder}"
            if _git(root, "cat-file", "-t", spec, error=error) != "tree":
                raise ValueError(error)
            relative_paths.append(relative)
        return roots.pop(), relative_paths
