"""Supervisor-owned verification recipes and measured subprocess results."""
from collections import Counter
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.parse import quote
from xml.etree import ElementTree

from .events import snapshot_request
from . import project_checks
from .engines import resolve_executable
from .goal_artifact import installed_evidence
from .goal_process import execute
from .studio_catalog import normalize_test_files, verify_worktree_test_files
from .studio_process import windows_service_session


def _pytest_files(argv):
    if argv[:4] == ["python", "-m", "pytest", "-q"]:
        paths = argv[4:]
    elif argv[:2] == ["pytest", "-q"]:
        paths = argv[2:]
    else:
        raise ValueError("지원되는 pytest 검증 명령을 선택하세요.")
    if paths == ["--lf"]:
        return []
    if paths[:1] == ["-x"]:
        paths = paths[1:]
    if paths[:1] == ["--"]:
        paths = paths[1:]
    return normalize_test_files(paths) if paths else []


def recipe(argv):
    if not isinstance(argv, list) or not all(type(arg) is str for arg in argv):
        raise ValueError("검증 명령은 인자 배열로 입력하세요.")
    if argv == ["npm", "run", "test:web"]:
        return "web"
    if argv == ["node", "desktop/verify-install.mjs", "<installed-app>"]:
        return "installed"
    project = project_checks.project_recipe(argv)
    if project:
        return project
    if len(argv) == 3 and argv[0] == "host-check":
        import re
        if (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", argv[1])
                and re.fullmatch(r"[a-f0-9]{64}", argv[2])):
            return "host"
    _pytest_files(argv)
    return "pytest"


def _unprivileged():
    if os.name == "nt":
        return not windows_service_session() and not ctypes.windll.shell32.IsUserAnAdmin()
    if os.geteuid() == 0 or os.environ.get("INVOCATION_ID"):
        return False
    try:
        return _no_linux_capabilities(Path("/proc/self/status").read_text(encoding="ascii", errors="replace"))
    except OSError:
        return False


def _no_linux_capabilities(status):
    required = {"CapEff", "CapPrm", "CapAmb", "CapInh"}
    values = {}
    for line in status.splitlines():
        name, _, raw = line.partition(":")
        if name in required:
            try:
                values[name] = int(raw.strip(), 16)
            except ValueError:
                return False
    return values.keys() == required and not any(values.values())


def _command(workspace, check, report):
    argv = check["argv"]
    kind = recipe(argv)
    if kind in {"npm", "go", "cargo"}:
        return project_checks.command(workspace, argv, kind), kind
    if kind == "host":
        from .goal_host_checks import command_for
        return command_for(argv, workspace, report)
    if kind == "pytest":
        if "--lf" in argv:
            raise ValueError("빠른 검증을 실행하려면 기준 커밋에서 확인한 테스트 경로가 필요합니다.")
        paths = _pytest_files(argv)
        verify_worktree_test_files(workspace, paths)
        # Isolated startup imports the coordinator's installed pytest before
        # the bundled launcher adds the worktree for project imports.
        command = [sys.executable, "-I", "-B", str(Path(__file__).resolve().with_name("goal_pytest.py")), "-q",
                   "-o", "junit_family=xunit1", "-o", "cache_dir=" + str(report.parent / "cache"), "--junitxml", str(report)]
        if "-x" in argv:
            command.append("-x")
        if paths:
            command.extend(["--", *paths])
        return command, kind
    node = resolve_executable("node.exe" if os.name == "nt" else "node")
    if not node or Path(node).resolve().is_relative_to(workspace):
        raise ValueError("호스트에 Node.js가 필요합니다.")
    if kind == "web":
        manifest = workspace / "package.json"
        if manifest.is_file():
            if manifest.resolve(strict=True) != manifest or manifest.stat().st_size > 1024 * 1024:
                raise ValueError("검증할 프로젝트의 package.json을 확인할 수 없습니다.")
            package = json.loads(manifest.read_text(encoding="utf-8-sig"))
            scripts = package.get("scripts") if isinstance(package, dict) else None
            if not isinstance(scripts, dict) or scripts.get("test:web") != "node --test tests/web/*.test.mjs":
                return project_checks.command(workspace, argv, "npm"), "npm"
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


def portable_case_id(key):
    filename, separator, remainder = key.partition("::")
    return filename.replace("\\", "/") + separator + remainder


def portable_cases(cases):
    normalized = {portable_case_id(key): value for key, value in cases.items()}
    if len(normalized) != len(cases):
        raise ValueError("같은 파일 경로로 해석되는 중복 테스트가 있습니다.")
    return normalized


def _pytest_results(path):
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("테스트 결과 파일이 제한을 초과했습니다.")
    cases = {}
    for case in ElementTree.parse(path).iter("testcase"):
        key = portable_case_id("::".join(case.get(attr, "") for attr in ("file", "classname", "name")))
        if key in cases or len(cases) >= 20000:
            raise ValueError("테스트 식별자가 중복되었거나 제한을 초과했습니다.")
        cases[key] = ("failed" if case.find("failure") is not None or case.find("error") is not None
                      else "skipped" if case.find("skipped") is not None else "passed")
    return cases


def run_check(workspace, check, *, timeout, on_start=None):
    workspace = Path(workspace).resolve(strict=True)
    argv = check["argv"]
    started = time.monotonic()
    receipt = dict(stage=check["stage"], host=platform.node(), cwd=str(workspace), argv=list(argv),
                   started_at=datetime.now(timezone.utc).isoformat(), elapsed_seconds=0,
                   exit_code=None, status="running", summary="", counts={}, scope_changes=[])
    cases = {}
    with TemporaryDirectory(prefix="office-goal-") as temp:
        report = Path(temp) / "report.xml"
        try:
            if not _unprivileged():
                raise ValueError("검증에는 일반 사용자 세션이 필요합니다.")
            if check.get("host", "local").casefold() not in {"local", platform.node().casefold()}:
                raise ValueError("지정한 검증 호스트가 이 작업공간을 소유한 호스트와 다릅니다.")
            command, kind = _command(workspace, check, report)
            if kind in {"host_exit", "host_junit"}:
                receipt["host_recipe_digest"] = argv[2]
            if kind == "installed":
                receipt["artifact"] = installed_evidence(workspace, Path(command[-1]))
            receipt["argv"] = command
            if on_start:
                on_start(receipt.copy())
            env = {key: os.environ[key] for key in (
                "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "HOME", "USERPROFILE",
                "APPDATA", "LOCALAPPDATA", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "DISPLAY", "WAYLAND_DISPLAY",
                "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS") if key in os.environ}
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            if kind == "cargo":
                env["CARGO_TARGET_DIR"] = str(report.parent / "target")
            elif kind == "go":
                env.update(GOPROXY="off", GOTOOLCHAIN="local", GOWORK="off")
            code, output = execute(command, workspace, env, timeout)
            receipt.update(exit_code=code, summary=snapshot_request(output))
            if kind in {"host_exit", "host_junit"}:
                from .goal_host_checks import definition
                if definition(argv[1], workspace)[1] != argv[2]:
                    raise ValueError("검증 중 호스트 명령 또는 고정한 입력이 변경되었습니다.")
            if kind == "installed":
                if installed_evidence(workspace, Path(command[-1])) != receipt["artifact"]:
                    raise ValueError("검증 도중 설치판 또는 후보가 변경되었습니다.")
                cases = {"installed-app::verify-install": "passed" if code == 0 else "failed"}
            elif kind == "host_exit":
                cases = {"host-check::" + argv[1]: "passed" if code == check["pass_exit_code"] else "failed"}
            elif kind in {"npm", "go", "cargo"}:
                receipt["result_format"] = "exit"
                cases = {"command::" + " ".join(argv): "passed" if code == 0 else "failed"}
            else:
                cases = _pytest_results(report)
            receipt["counts"] = dict(Counter(cases.values()))
            receipt["status"] = ("passed" if "passed" in receipt["counts"] and code == check.get("pass_exit_code", 0)
                                 and "failed" not in cases.values() else "failed")
        except subprocess.TimeoutExpired as exc:
            receipt.update(status="timeout", summary="검증 제한 시간이 지났습니다.\n" + snapshot_request(exc.output or ""))
        except (ValueError, OSError, KeyError, ElementTree.ParseError) as exc:
            receipt.update(status="unverified", summary=snapshot_request(str(exc)))
    receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return receipt, cases
