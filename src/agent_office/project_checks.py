"""Fixed project test invocations, executed only in an unprivileged verification session."""
import json
import os
from pathlib import Path, PureWindowsPath
import re

from .engines import resolve_executable


def project_recipe(argv):
    if argv == ["cargo", "test"] or argv == ["go", "test", "./..."]:
        return argv[0]
    if argv == ["npm", "test"] or (len(argv) == 3 and argv[:2] == ["npm", "run"]
                                    and re.fullmatch(r"test:[a-zA-Z0-9][a-zA-Z0-9:_-]{0,63}", argv[2])):
        return "npm"
    return None


def _tool(name, root):
    executable = resolve_executable(name + (".exe" if os.name == "nt" else ""))
    if not executable or Path(executable).resolve().is_relative_to(root):
        raise ValueError(f"검증 호스트에 설치된 {name} 실행 파일이 필요합니다.")
    return Path(executable)


def command(root, argv, kind):
    manifest = root / {"npm": "package.json", "go": "go.mod", "cargo": "Cargo.toml"}[kind]
    if not manifest.is_file() or manifest.resolve(strict=True) != manifest:
        raise ValueError(f"검증할 프로젝트의 {manifest.name} 파일이 필요합니다.")
    if kind == "go":
        return [str(_tool(kind, root)), "test", "-mod=readonly", "./..."]
    if kind == "cargo":
        return [str(_tool(kind, root)), "test", "--locked", "--offline"]
    if manifest.stat().st_size > 1024 * 1024:
        raise ValueError("package.json 크기 제한을 초과했습니다.")
    package = json.loads(manifest.read_text(encoding="utf-8-sig"))
    scripts = package.get("scripts") if isinstance(package, dict) else None
    if not isinstance(scripts, dict) or not isinstance(scripts.get(argv[-1]), str) or not scripts[argv[-1]].strip():
        raise ValueError("package.json에 선택한 테스트 스크립트가 없습니다.")
    node = _tool("node", root)
    if os.name == "nt":
        cli = node.parent / "node_modules/npm/bin/npm-cli.js"
    else:
        installed = resolve_executable("npm")
        cli = Path(installed).resolve() if installed else None
    if not cli or cli.name != "npm-cli.js" or not cli.is_file() or cli.resolve().is_relative_to(root):
        raise ValueError("호스트의 npm CLI를 확인할 수 없습니다.")
    return [str(node), str(cli), "--ignore-scripts", "--offline", *argv[1:]]


def invocation_matches(kind, argv, planned):
    exe = PureWindowsPath(argv[0]).name.lower()
    if kind == "npm":
        return (exe in ("node", "node.exe") and len(argv) >= 5 and PureWindowsPath(argv[1]).name == "npm-cli.js"
                and argv[2:] == ["--ignore-scripts", "--offline", *planned[1:]])
    tail = ["test", "-mod=readonly", "./..."] if kind == "go" else ["test", "--locked", "--offline"]
    return exe in (kind, kind + ".exe") and argv[1:] == tail


def dependency_inputs(root):
    """Enumerate local npm dependencies so a changed ignored module invalidates a checkpoint."""
    modules = root / "node_modules"
    if not modules.exists():
        return
    if modules.resolve(strict=True) != modules:
        raise ValueError("외부 node_modules의 내용은 복구 근거로 사용할 수 없습니다.")
    count = total = 0
    def fail(error):
        raise error
    for folder, directories, files in os.walk(modules, followlinks=False, onerror=fail):
        for name in sorted(directories + files):
            entry = Path(folder) / name
            resolved = entry.resolve(strict=True)
            if not resolved.is_relative_to(modules):
                raise ValueError("동일한 프로젝트 안의 Node.js 의존성만 재사용할 수 있습니다.")
            if resolved.is_dir():
                if resolved != entry:
                    raise ValueError("연결된 패키지 디렉터리가 있는 검증은 다시 확인해야 합니다.")
                continue
            total += resolved.stat().st_size
            count += 1
            if count > 50000 or total > 512 * 1024 * 1024:
                raise ValueError("Node.js 의존성이 검증 환경 지문 크기 제한을 초과했습니다.")
            yield entry
