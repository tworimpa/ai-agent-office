"""Fixed entry point for the operator's least-privilege Windows scheduled task."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(r"C:\ProgramData\AI-Agent-Office")
CONTROLLER = ROOT / "operator"
RELEASE = ROOT / "release"
TARGET = ROOT / "deploy"
TOOLS = {
    "git": Path(r"C:\Program Files\Git\cmd\git.exe"),
    "gh": Path(r"C:\Program Files\GitHub CLI\gh.exe"),
    "docker": Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"),
}


def _environment(system, profile, roaming, local):
    return {
        "PATH": os.pathsep.join(map(str, [*(tool.parent for tool in TOOLS.values()), system])),
        "PATHEXT": ".EXE", "SystemRoot": str(system.parent), "WINDIR": str(system.parent),
        "COMSPEC": str(system / "cmd.exe"), "USERPROFILE": str(profile), "HOME": str(profile),
        "APPDATA": str(roaming), "LOCALAPPDATA": str(local),
        "TEMP": str(local / "Temp"), "TMP": str(local / "Temp"),
        "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1",
        "NoDefaultCurrentDirectoryInExePath": "1",
    }


def _prepare():
    import ctypes

    if not sys.flags.isolated or ctypes.windll.shell32.IsUserAnAdmin():
        raise ValueError("Use an unelevated operator session and isolated Python (-I)")
    package = CONTROLLER / "Lib" / "site-packages" / "agent_office"
    if (Path(sys.executable) != CONTROLLER / "Scripts" / "python.exe"
            or Path(__file__).resolve(strict=True).parent != package):
        raise ValueError("Install the trusted controller wheel in the fixed operator environment")
    os.chdir(CONTROLLER)

    def folder(kind):
        value = ctypes.create_unicode_buffer(260)  # SHGetFolderPathW returns a MAX_PATH buffer
        if ctypes.windll.shell32.SHGetFolderPathW(None, kind, None, 0, value):
            raise ValueError("Cannot identify the signed-in operator profile")
        return Path(value.value)

    system = ctypes.create_unicode_buffer(32768)
    if not 0 < ctypes.windll.kernel32.GetSystemDirectoryW(system, len(system)) < len(system):
        raise ValueError("Cannot identify the Windows system directory")
    # CSIDL_PROFILE, CSIDL_APPDATA, CSIDL_LOCAL_APPDATA from the current Windows token.
    clean = _environment(Path(system.value), folder(0x28), folder(0x1A), folder(0x1C))
    os.environ.clear()
    os.environ.update(clean)
    from .engines import resolve_executable
    for name, path in TOOLS.items():
        found = resolve_executable(name + ".exe")
        if found is None or Path(found).resolve(strict=True) != path:
            raise ValueError(f"Missing trusted executable at {path}")


def _service_rights():
    import pywintypes
    import win32service as scm

    manager = scm.OpenSCManager(None, None, scm.SC_MANAGER_CONNECT)
    try:
        rights = scm.SERVICE_QUERY_CONFIG | scm.SERVICE_QUERY_STATUS | scm.SERVICE_START | scm.SERVICE_STOP
        try:
            handle = scm.OpenService(manager, "AgentOfficeStudio", rights)
        except pywintypes.error as exc:
            raise ValueError("Operator requires native query, start and stop rights on AgentOfficeStudio") from exc
        scm.CloseServiceHandle(handle)
    finally:
        scm.CloseServiceHandle(manager)


def _preflight():
    from . import auto_deploy, deployment, runtime_ops, service_binding

    config, _ = deployment.read_selection(TARGET)
    if (config["manager"], config["service"]) != ("windows", "AgentOfficeStudio"):
        raise ValueError("Expected the enrolled AgentOfficeStudio Windows target")
    policy = auto_deploy.status(RELEASE, TARGET)
    if not policy["enabled"] or auto_deploy.binding(RELEASE, TARGET, policy["binding"]["image"]) != policy["binding"]:
        raise ValueError("The operator must enroll this exact target before starting the watcher")
    image = policy["binding"]["image"]
    service_binding.inspect_service(config, TARGET)
    if deployment.public_status(TARGET)["health"] != "healthy":
        raise ValueError("Operator must be able to verify the running Studio process, journal and listener")
    with runtime_ops.idle_journal(Path(policy["binding"]["journal"]),config["journal_id"]):
        pass
    # Check authentication and the local engine without displaying account data or reading credential files.
    for suffix in ("user", "repos/tworimpa/ai-agent-office/git/ref/heads/main"):
        _probe([TOOLS["gh"], "api", "--hostname", "github.com", suffix], "Operator GitHub read access")
    with tempfile.TemporaryDirectory() as empty:
        _probe([TOOLS["docker"], "--config", empty, "--host", "npipe:////./pipe/docker_engine",
                "image", "inspect", image], "Local Docker engine and pinned validator image")


def _probe(command, label):
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=30)
    if result.returncode:
        raise ValueError(f"{label} check failed")


def main(args=None):
    args = sys.argv[1:] if args is None else args
    if os.name != "nt" or args not in ([], ["--preflight"]):
        raise ValueError("Expected the Windows scheduled task or its --preflight check")
    _prepare()
    _service_rights()
    if args:
        _preflight()
        return 0
    from .cli import main as office
    return office(["improve", "--release", str(RELEASE), "deployment-auto-run", "--target", str(TARGET)])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        sys.exit(str(error))
