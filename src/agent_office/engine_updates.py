"""Explicit maintenance of installed CLIs, serialized against provider calls."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.request import urlopen

from .process_window import BACKGROUND_FLAGS
from .engine_status import probe_version
from .engines import ENGINE_COMMANDS, resolve_executable
from .models import ACTIVE, OfficeError
from .store import TaskStore

PACKAGES = {"claude": "@anthropic-ai/claude-code", "gemini": "@google/gemini-cli",
            "cline": "cline", "codex": "@openai/codex"}
VERSION = r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}"
REGISTRY = "https://registry.npmjs.org"
CHANGELOG = "https://antigravity.google/docs/changelog.md"


@contextmanager
def host_lease(*, update=False):
    """All this user's Office journals share the host lock across processes."""
    root = Path.home() / ".agent-office"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "cli-maintenance.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            import pywintypes
            import win32con
            import win32file
            import winerror
            flags = win32con.LOCKFILE_FAIL_IMMEDIATELY
            if update:
                flags |= win32con.LOCKFILE_EXCLUSIVE_LOCK
            try:
                win32file.LockFileEx(msvcrt.get_osfhandle(lock.fileno()), flags, 0, 1, pywintypes.OVERLAPPED())
            except pywintypes.error as exc:
                if exc.winerror == winerror.ERROR_LOCK_VIOLATION:
                    raise ValueError("호스트에서 엔진을 사용하거나 갱신하고 있습니다.") from None
                raise
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_NB | (fcntl.LOCK_EX if update else fcntl.LOCK_SH))
            except BlockingIOError:
                raise ValueError("호스트에서 엔진을 사용하거나 갱신하고 있습니다.") from None
        yield


def upstream(engine):
    url = CHANGELOG if engine == "antigravity" else f"{REGISTRY}/{PACKAGES[engine]}/latest"
    try:
        with urlopen(url, timeout=5) as response:
            if response.geturl() != url:
                return None
            data = response.read(2_000_001)
        if len(data) > 2_000_000:
            return None
        if engine == "antigravity":
            section = data.decode("utf-8").partition("\n## Antigravity CLI\n")[2]
            match = re.match(r"\s*### \[v(" + VERSION + r")\]\(/releases\?tab=cli&", section)
            return match[1] if match else None
        value = json.loads(data)
        version = value.get("version") if isinstance(value, dict) and value.get("name") == PACKAGES[engine] else None
        return version if isinstance(version, str) and re.fullmatch(VERSION, version) else None
    except (OSError, ValueError, RecursionError):
        return None


def check(engine):
    executable = resolve_executable(ENGINE_COMMANDS[engine])
    installed = probe_version(engine, executable, 3)[0] if executable else None
    published = upstream(engine)
    newer = False
    if installed and re.fullmatch(VERSION, installed) and published:
        newer = tuple(map(int, published.split("."))) > tuple(map(int, installed.split(".")))
    policy = None
    if newer:
        try:
            command = update_command(engine, executable, published)
            policy = "vendor_channel" if command[1:] == ["update"] else "pinned"
        except (OSError, TypeError, ValueError):
            pass
    return {"engine": engine, "installed": installed, "latest": published, "update_available": newer,
            "update_policy": policy}


def require_operator():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("운영자의 대화형 터미널에서 실행하세요.")
    if os.name == "nt":
        import ctypes
        session = ctypes.c_ulong()
        interactive = ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)) and session.value
        elevated = ctypes.windll.shell32.IsUserAnAdmin()
    else:
        interactive = os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp()
        elevated = os.geteuid() == 0 or os.getuid() != os.geteuid()
    if not interactive or elevated:
        raise ValueError("일반 사용자로 로그인한 터미널에서 실행하세요.")


def update_command(engine, executable, version):
    """Only known per-user vendor layouts. Never adopt a system or repo installation."""
    home = Path.home().resolve()
    path = Path(executable)
    if not path.is_absolute() or not path.resolve(strict=True).is_relative_to(home):
        raise ValueError("이 계정의 전용 CLI 설치 경로를 확인할 수 없습니다.")
    local_bin = home / ".local" / "bin"
    windows = os.name == "nt"
    if engine in ("antigravity", "claude"):
        bin_dir = home / "AppData/Local/agy/bin" if windows and engine == "antigravity" else local_bin
        name = ENGINE_COMMANDS[engine] + (".exe" if windows else "")
        resolved = path.resolve()
        version_root = home / ".local/share" / ("claude" if engine == "claude" else "agy")
        if (path == bin_dir / name and bin_dir.resolve() == bin_dir
                and (resolved == path or resolved.is_relative_to(version_root))):
            return [str(path), "update"]
    package = PACKAGES.get(engine)
    prefix = home / "AppData/Roaming/npm" if windows else home / ".local"
    command_dir = prefix if windows else prefix / "bin"
    if not windows and path.parent == home / ".npm-global/bin":
        prefix, command_dir = home / ".npm-global", path.parent
    package_dir = (prefix if windows else prefix / "lib") / "node_modules" / (package or "")
    if (not package or path.parent != command_dir or prefix != prefix.resolve()
            or package_dir.resolve() != package_dir):
        raise ValueError("이 계정의 전용 npm 설치 경로를 확인할 수 없습니다.")
    with (package_dir / "package.json").open("rb") as file:
        manifest = json.loads(file.read(256_001))
    name = ENGINE_COMMANDS[engine]
    binary = manifest.get("bin")
    target = binary.get(name) if isinstance(binary, dict) else binary
    if manifest.get("name") != package or not isinstance(target, str):
        raise ValueError("실행 파일과 npm 패키지를 확인할 수 없습니다.")
    actual = (package_dir / target).resolve(strict=True)
    if not actual.is_relative_to(package_dir):
        raise ValueError("npm 실행 파일이 패키지 외부를 가리킵니다.")
    if windows:
        with path.open("rb") as file:
            wrapper = file.read(8193).lower().replace(b"\\", b"/")
        relative = actual.relative_to(prefix).as_posix().lower().encode()
        owned = path.name.lower() == name + ".cmd" and len(wrapper) <= 8192 and relative in wrapper
        npm = Path(os.environ["ProgramFiles"]) / "nodejs/npm.cmd"
    else:
        owned = path.name == name and path.resolve() == actual
        npm = Path("/usr/bin/npm")
    if not owned or not npm.is_file():
        raise ValueError("지원되는 npm 설치와 실행 관리자를 확인할 수 없습니다.")
    return [str(npm), "install", "--global", "--prefix", str(prefix),
            "--registry", REGISTRY, package + "@" + version]


def apply(db_path, engine, installed, latest, journal, *, vendor_channel=False):
    require_operator()
    if not all(re.fullmatch(VERSION, v) for v in (installed, latest)):
        raise ValueError("정식 버전을 입력하세요.")
    with host_lease(update=True), TaskStore(db_path, existing=True, readonly=True) as store:
        identity = store.connection.execute("SELECT id FROM studio_identity WHERE singleton=1").fetchone()
        if not identity or identity[0] != journal:
            raise ValueError("Studio 저널 ID와 --db 경로를 확인하세요.")
        if store.connection.execute("SELECT 1 FROM runs WHERE state IN (?,?,?,?) LIMIT 1", tuple(ACTIVE)).fetchone():
            raise OfficeError("RUN_BUSY")
        executable = resolve_executable(ENGINE_COMMANDS[engine])
        if not executable:
            raise ValueError("설치된 CLI를 찾을 수 없습니다.")
        command = update_command(engine, executable, latest)
        if vendor_channel != (command[1:] == ["update"]):
            raise ValueError("요청한 업데이트 방식과 설치 관리자의 버전 지정 방식이 다릅니다.")
        snapshot = check(engine)
        if not snapshot["update_available"] or (installed, latest) != (snapshot["installed"], snapshot["latest"]):
            raise ValueError("버전 정보가 달라졌습니다. 공개 버전을 다시 확인하세요.")
        print(f"{executable}\n{engine} · 설치 {installed} · 공개 {latest}")
        if vendor_channel:
            prompt = "설치 시점에 공급자 채널이 선택한 최신 버전으로 갱신할까요? [y/N] "
        else:
            prompt = f"{engine} {latest} 버전을 설치할까요? [y/N] "
        if input(prompt).strip().casefold() not in {"y", "yes", "예"}:
            raise ValueError("CLI 업데이트를 취소했습니다.")
        with tempfile.TemporaryDirectory(prefix="office-cli-update-") as cwd:
            from .process_diagnostics import kill_family
            env = {k: v for k,v in os.environ.items() if k.upper() in {
                "HOME", "USER", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "SYSTEMDRIVE",
                "PROGRAMFILES", "TEMP", "TMP", "TMPDIR", "LANG"}}
            dirs = [str(Path(command[0]).parent), os.defpath]
            if os.name == "nt":
                dirs = [str(Path(command[0]).parent), str(Path(os.environ["SystemRoot"]) / "System32")]
            env["PATH"] = os.pathsep.join(dirs)
            with subprocess.Popen(command, cwd=cwd, env=env, start_new_session=os.name != "nt",
                                  creationflags=BACKGROUND_FLAGS, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
                try:
                    code = process.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    kill_family(process.pid)
                    process.kill()
                    raise ValueError("CLI 업데이트 제한 시간이 지났습니다. 설치 버전을 다시 확인하세요.") from None
        actual = resolve_executable(ENGINE_COMMANDS[engine])
        version = probe_version(engine, actual, 3)[0] if actual else None
        advanced = (version and re.fullmatch(VERSION, version) and
                    tuple(map(int, version.split("."))) > tuple(map(int, installed.split("."))))
        if code or (not advanced if vendor_channel else version != latest):
            raise ValueError("요청한 버전 설치를 확인할 수 없습니다. CLI 진단을 다시 확인하세요.")
        return {"engine": engine, "installed": version}
