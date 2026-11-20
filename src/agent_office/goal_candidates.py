"""Capture reviewable source changes against the original revision."""
import difflib
import ast
from collections import Counter
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tomllib

from .away_evidence import require_base, DelegationDeferred
from .events import snapshot_request
from .meeting_parallel import _check_destination
from .workspace import read_git_prefix


def capture(root, revision, folders):
    from .release_store import run_git

    root = Path(root)
    try:
        require_base(root, revision)
    except DelegationDeferred as exc:
        raise ValueError("작업 트리의 기준 커밋이 변경되었습니다.") from exc
    names, clipped = read_git_prefix(root, "diff", "--name-only", "--no-renames", "-z", revision)
    additions, omitted = read_git_prefix(root, "ls-files", "--others", "--exclude-standard", "-z")
    if clipped or omitted:
        raise ValueError("변경 파일 목록이 제한을 초과했습니다.")
    tracked = names.decode().split("\0")[:-1]
    untracked = additions.decode().split("\0")[:-1]
    paths = sorted(set(tracked + untracked))
    if len(paths) > 128:
        raise ValueError("검토할 변경 파일이 128개를 넘습니다.")
    _check_destination(root, paths)
    for name in paths:
        if not any(PurePosixPath(name).is_relative_to(folder) for folder in folders):
            raise ValueError("허용한 작업 범위를 벗어난 파일: " + name)
        if re.search(r"(?:^|/)(?:\.env(?:\.|$)|\.ssh/|\.aws/)|\.(?:pem|key|pfx)$", name, re.I):
            raise ValueError("비공개 설정 파일은 후보에 담을 수 없습니다.")
    if not paths:
        return [], "", ""
    data = run_git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--full-index",
                   "--binary", revision, "--", *tracked) if tracked else b""
    if len(data) > 1024 * 1024:
        raise ValueError("검토할 후보 변경이 1MiB 제한을 초과했습니다.")
    patch = data.decode("utf-8")
    for name in untracked:
        file = root / name
        info = file.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
            raise ValueError("새 파일의 크기 또는 형식을 검토할 수 없습니다.")
        with file.open("rb") as stream:
            if not os.path.samestat(os.fstat(stream.fileno()), info):
                raise ValueError("후보 수집 중 파일이 변경되었습니다.")
            content = stream.read(1024 * 1024 + 1)
        text = content.decode("utf-8")
        if len(content) != info.st_size or "\0" in text:
            raise ValueError("새 텍스트 파일을 검토할 수 없습니다.")
        mode = "100755" if os.name != "nt" and info.st_mode & stat.S_IXUSR else "100644"
        patch += f"diff --git a/{name} b/{name}\nnew file mode {mode}\n"
        lines = difflib.unified_diff([], text.splitlines(keepends=True), "/dev/null", "b/" + name)
        for line in lines:
            patch += line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        if len(patch.encode()) > 1024 * 1024:
            raise ValueError("검토할 후보 변경이 1MiB 제한을 초과했습니다.")
    digest = hashlib.sha256((revision + "\0" + patch).encode()).hexdigest()
    if any(snapshot_request(line) != line for line in patch.splitlines()):
        raise ValueError("민감한 값 또는 너무 긴 줄이 포함되어 후보를 안전하게 보관할 수 없습니다.")
    return paths, patch, digest


def scope_changes(root, revision, files):
    """Existing test obligations must survive a successful test process."""
    from .away_evidence import git

    risks = []
    for name in files:
        filename = Path(name).name
        settings = filename in {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"}
        if not (settings or
                filename.startswith("test_") or filename.endswith(("_test.py", ".test.mjs"))):
            continue
        try:
            old = git(root, "cat-file", "blob", revision + ":" + name)
        except DelegationDeferred:
            # ls-tree distinguishes a newly added test from an unreadable blob.
            if not git(root, "ls-tree", revision, "--", name).strip():
                if settings:
                    risks.append("추가된 검증 설정: " + name)
                continue
            raise ValueError("기준 테스트 파일을 읽을 수 없습니다.")
        if not (root / name).is_file():
            risks.append("삭제된 검증 파일: " + name)
            continue
        new = (root / name).read_bytes()
        if old.splitlines() == new.splitlines():
            continue
        if filename == "pyproject.toml":
            try:
                def options(data):
                    table = tomllib.loads(data.decode())
                    return table.get("tool", {}).get("pytest"), table.get("pytest")
                if options(old) == options(new):
                    continue
            except (UnicodeError, ValueError):
                pass
        if filename.endswith(".py") and filename != "conftest.py":
            try:
                def obligations(data):
                    # Adding independent tests can extend coverage. A rewrite
                    # of an existing test or its helper needs human inspection
                    # even if collection reports exactly the same node IDs.
                    return Counter(ast.dump(node, include_attributes=False) for node in ast.parse(data).body)
                if not (obligations(old) - obligations(new)):
                    continue
            except (SyntaxError, ValueError):
                pass
        risks.append("기존 검증 내용 변경: " + name)
    return risks
