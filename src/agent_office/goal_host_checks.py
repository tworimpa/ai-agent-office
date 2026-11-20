"""Resolve an operator-owned verification command by content digest on its execution host."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .store import encode


def definition(name, workspace=None):
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}", name):
        raise ValueError("호스트 검증 명령 이름을 확인하세요.")
    location = Path(os.environ.get("AGENT_OFFICE_GOAL_RECIPES", ""))
    if not location.is_absolute() or not location.is_file() or location.stat().st_size > 131072:
        raise ValueError("실행 호스트에 AGENT_OFFICE_GOAL_RECIPES 검증 목록을 등록하세요.")
    catalog = json.loads(location.read_text(encoding="utf-8-sig"))
    spec = catalog.get(name) if isinstance(catalog, dict) else None
    if not isinstance(spec, dict) or set(spec) != {"project", "command", "inputs", "result"}:
        raise ValueError("등록된 프로젝트, 명령, 입력 파일, 결과 형식을 확인하세요.")
    command = spec["command"]
    inputs = spec["inputs"]
    if (not isinstance(command, list) or not 1 <= len(command) <= 64 or
            not all(isinstance(x, str) and x and len(x) <= 4096 and "\0" not in x for x in command) or
            not isinstance(inputs, list) or len(inputs) > 32 or not all(isinstance(x, str) for x in inputs)
            or spec["result"] not in ("junit", "exit") or not isinstance(spec["project"], str)):
        raise ValueError("호스트 검증 명령 형식을 확인하세요.")
    if spec["result"] == "junit" and "{report}" not in command:
        raise ValueError("JUnit 명령에는 결과 파일을 받을 {report} 인수가 필요합니다.")
    project = Path(spec["project"])
    if not project.is_absolute() or not project.is_dir():
        raise ValueError("검증 명령에서 허용할 원본 프로젝트를 지정하세요.")
    project = project.resolve(strict=True)
    if workspace is not None:
        def git_directory(path):
            from .studio_catalog import _git, _local_git_environment
            return Path(_git(path, "rev-parse", "--path-format=absolute", "--git-common-dir",
                             env=_local_git_environment(), error="검증 작업 트리의 원본을 확인할 수 없습니다.")).resolve()
        if git_directory(workspace) != git_directory(project):
            raise ValueError("이 호스트 명령은 선택한 작업공간에 등록되지 않았습니다.")
    paths = [location, Path(command[0]), *(Path(path) for path in inputs)]
    checksums = []
    for path in paths:
        real = path.resolve(strict=True)
        if (not path.is_absolute() or not real.is_file() or real.is_relative_to(project) or
                workspace is not None and real.is_relative_to(workspace)):
            raise ValueError("검증 설정, 실행 파일과 호스트 입력은 프로젝트 밖의 절대 경로에 두세요.")
        if path != location:
            with real.open("rb") as stream:
                checksums.append(hashlib.file_digest(stream, "sha256").hexdigest())
    digest = hashlib.sha256(encode([name, spec, checksums]).encode()).hexdigest()
    return spec, digest


def command_for(argv, workspace, report):
    spec, digest = definition(argv[1], workspace)
    if digest != argv[2]:
        raise ValueError("등록 당시와 호스트 검증 명령 또는 입력 파일이 달라졌습니다.")
    command = [str(report) if arg == "{report}" else arg for arg in spec["command"]]
    return command, "host_" + spec["result"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Print the workspace recipe reference without executing the command.")
    parser.add_argument("name")
    args = parser.parse_args()
    try:
        _, fingerprint = definition(args.name)
        print(encode(["host-check", args.name, fingerprint]))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, str(exc) + "\n")
