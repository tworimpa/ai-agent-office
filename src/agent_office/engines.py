"""Shared engine IDs, executable aliases and the owner's initial profile."""
import os
import shutil
ENGINE_COMMANDS = {"codex": "codex", "antigravity": "agy", "claude": "claude"}
SUPPORTED_ENGINES = tuple(ENGINE_COMMANDS)
INITIAL_ENGINES = ("codex", "antigravity")
DEFAULT_DEVELOPER = "codex"
DEFAULT_REVIEWER = "antigravity"


def resolve_executable(command: str) -> str | None:
    """Do not implicitly search a repository for a vendor CLI on Windows."""
    if os.path.isabs(command):
        return shutil.which(command)
    if os.path.dirname(command):
        return None
    cwd = os.path.normcase(os.path.realpath(os.getcwd()))
    for entry in os.get_exec_path():
        if not entry or not os.path.isabs(entry):
            continue
        if os.name == "nt" and not os.path.splitdrive(entry)[0]:
            continue
        if os.path.normcase(os.path.realpath(entry)) == cwd:
            continue
        suffixes = [""]
        if os.name == "nt":
            suffixes = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
            if os.path.splitext(command)[1]:
                suffixes = [""] + suffixes
        for suffix in suffixes:
            executable = shutil.which(os.path.join(entry, command + suffix))
            if executable is not None:
                return executable
    return None
