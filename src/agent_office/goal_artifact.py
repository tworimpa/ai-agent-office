"""Bind a desktop journey to the installed renderer, runtime and Electron shell."""
import hashlib
import json
import os
from pathlib import Path
import struct

from .studio_catalog import verify_worktree_test_files


def _sources(root):
    if not root.is_dir() or root != root.resolve(strict=True):
        raise ValueError("설치판과 비교할 런타임 소스 디렉터리를 찾을 수 없습니다.")
    names = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if
                   p.is_file() and p.suffix in {".py", ".html", ".css", ".js", ".webp"})
    if not names or len(names) > 4096:
        raise ValueError("런타임 소스 목록을 확인할 수 없습니다.")
    verify_worktree_test_files(root, names)
    return names


def _hash_stream(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def installed_evidence(workspace, executable):
    resources = executable.parent / "resources"
    runtime = resources / "runtime"
    installed = runtime / ("Lib/site-packages/agent_office" if os.name == "nt" else
                           "lib/python3.13/site-packages/agent_office")
    source = Path(workspace) / "src/agent_office"
    names = _sources(source)
    if names != _sources(installed):
        raise ValueError("설치판 런타임의 파일 목록이 후보와 다릅니다.")
    matched = hashlib.sha256()
    def match(name, actual, expected):
        if not name.endswith(".webp"):
            actual = actual.replace(b"\r\n", b"\n")
            expected = expected.replace(b"\r\n", b"\n")
        if actual != expected:
            raise ValueError("설치판에 후보 변경이 반영되지 않았습니다: " + name)
        matched.update(name.encode() + b"\0" + hashlib.sha256(actual).digest())
    for name in names:
        left, right = source / name, installed / name
        if max(left.stat().st_size, right.stat().st_size) > 16 * 1024 * 1024:
            raise ValueError("설치판 파일 크기를 확인할 수 없습니다.")
        match(name, left.read_bytes(), right.read_bytes())
    archive = resources / "app.asar"
    with archive.open("rb") as stream:
        prefix = stream.read(16)
        if len(prefix) != 16:
            raise ValueError("설치판 앱 아카이브가 불완전합니다.")
        marker, header_size, payload_size, count = struct.unpack("<4I", prefix)
        if marker != 4 or payload_size + 4 != header_size or not 0 < count < header_size <= 1024 * 1024:
            raise ValueError("설치판 앱 아카이브 머리글을 확인할 수 없습니다.")
        files = json.loads(stream.read(count))["files"]
        package = json.loads((Path(workspace) / "desktop/package.json").read_text(encoding="utf-8"))
        expected = sorted({"package.json", *package["build"]["files"]})
        if sorted(files) != expected:
            raise ValueError("설치판 앱의 파일 목록이 후보와 다릅니다.")
        desktop = Path(workspace) / "desktop"
        verify_worktree_test_files(desktop, expected)
        for name in expected:
            entry = files[name]
            size = int(entry["size"])
            if "/" in name or not 0 <= size < 16 * 1024 * 1024 or (desktop / name).stat().st_size >= 16 * 1024 * 1024:
                raise ValueError("설치판 앱 파일을 확인할 수 없습니다.")
            stream.seek(8 + header_size + int(entry["offset"]))
            content = stream.read(size)
            if name == "package.json":
                packaged = json.loads(content)
                fields = ("name", "version", "main", "type", "dependencies")
                if any(package.get(key) != packaged.get(key) for key in fields):
                    raise ValueError("설치판 앱의 버전 또는 진입점이 후보와 다릅니다.")
                matched.update(content)
            else:
                match(name, content, (desktop / name).read_bytes())
    return dict(executable=str(executable), executable_sha256=_hash_stream(executable),
                archive_sha256=_hash_stream(archive), source_sha256=matched.hexdigest(),
                matched_runtime_files=len(names), matched_shell_files=len(files))
