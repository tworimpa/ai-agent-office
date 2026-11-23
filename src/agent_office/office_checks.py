"""Bundled verification recipes for the Agent Office frontend and installed application."""
import json
import os
from pathlib import Path
from urllib.parse import quote

from .engines import resolve_executable
from .studio_catalog import verify_worktree_test_files


def command(workspace, kind, report):
    node = resolve_executable("node.exe" if os.name == "nt" else "node")
    if not node or Path(node).resolve().is_relative_to(workspace):
        raise ValueError("호스트에 Node.js가 필요합니다.")
    if kind == "web":
        # Fixed test:web recipe skips package lifecycle hooks, npm's shell
        # and executable selection from a candidate's node_modules/.bin.
        files = sorted(path.relative_to(workspace).as_posix() for path in (workspace / "tests/web").glob("*.test.mjs"))
        if not files or len(files) > 1024:
            raise ValueError("웹 테스트 파일을 확인할 수 없습니다.")
        verify_worktree_test_files(workspace, files)
        options = []
        if not (workspace / "node_modules/jsdom/package.json").is_file():
            source = Path(os.environ.get("AGENT_OFFICE_GOAL_WEB_PROJECT", Path(__file__).resolve().parents[2]))
            locked = workspace / "package-lock.json"
            host_lock = source / "package-lock.json"
            package = source / "node_modules/jsdom/package.json"
            if not (package.is_file() and locked.is_file() and host_lock.is_file() and
                    locked.read_bytes().splitlines() == host_lock.read_bytes().splitlines()):
                raise ValueError("검증할 저장소와 같은 package-lock.json으로 설치한 호스트의 jsdom이 필요합니다.")
            # Load only the predefined web suite dependency from a known
            # checkout. Relative imports still resolve against the candidate.
            hook = (
                "import {createRequire,registerHooks} from 'node:module';\n"
                "import {pathToFileURL} from 'node:url';\n"
                "const require=createRequire(" + json.dumps(str(host_lock.resolve())) + ");\n"
                "const url=pathToFileURL(require.resolve('jsdom')).href;\n"
                "registerHooks({resolve(name,context,next){return name==='jsdom'?"
                "{url,shortCircuit:true}:next(name,context)}});\n")
            options = ["--import", "data:text/javascript," + quote(hook, safe="")]
        return [node, *options, "--test", "--test-reporter=junit", "--test-reporter-destination=" + str(report), *files], kind
    harness = Path(os.environ.get("AGENT_OFFICE_GOAL_INSTALL_HARNESS",
                                  Path(__file__).resolve().parents[2] / "desktop/verify-install.mjs"))
    installed = (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/ai-agent-office-desktop/AI Agent Office.exe"
                 if os.name == "nt" else Path("/opt/AI Agent Office/ai-agent-office-desktop"))
    if (not harness.is_absolute() or not harness.is_file() or not installed.is_absolute() or not installed.is_file()
            or harness.resolve().is_relative_to(workspace) or installed.resolve().is_relative_to(workspace)):
        raise ValueError("이 호스트의 신뢰된 설치판 검증 도구 또는 설치 실행 파일이 없습니다.")
    return [node, str(harness), str(installed)], kind
