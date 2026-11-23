"""Child-only Git transport and GitHub CLI restrictions for candidate code."""
from contextlib import contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from .github_connection import worker_env


@contextmanager
def coding_environment(source):
    """Leave local Git inspection available and isolate conventional Git/gh invocations."""
    with TemporaryDirectory(prefix="agent-office-worker-") as temp:
        root = Path(temp)
        config = root / "gh-config"
        config.mkdir()
        commands = root / "bin"
        commands.mkdir()
        script = "#!/bin/sh\necho 'GitHub CLI is disabled for coding workers' >&2\nexit 1\n"
        shim = commands / "gh"
        shim.write_bytes(script.encode("ascii"))
        shim.chmod(0o700)
        if os.name == "nt":
            # Native executable lookup can ignore batch files. An invalid image
            # stops CreateProcess at the first gh.exe in the child search path.
            (commands / "gh.exe").write_bytes(script.encode("ascii"))
            (commands / "gh.cmd").write_bytes(b"@echo GitHub CLI is disabled for coding workers 1>&2\r\n@exit /b 1\r\n")
        env = {key: value for key, value in worker_env(source).items()
               if not key.upper().startswith(("GIT_CONFIG_", "GH_")) and key.upper() not in
               {"GIT_ASKPASS", "SSH_ASKPASS", "SSH_AUTH_SOCK", "GIT_SSH", "GIT_SSH_COMMAND", "GITHUB_PAT"}}
        env.update(PATH=str(commands) + os.pathsep + env.get("PATH", os.defpath),
                   GH_CONFIG_DIR=str(config), GH_PROMPT_DISABLED="1", GIT_ALLOW_PROTOCOL="",
                   GIT_SSH_COMMAND="false", GIT_ASKPASS="false", GIT_TERMINAL_PROMPT="0",
                   GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="credential.helper", GIT_CONFIG_VALUE_0="",
                   GIT_CONFIG_KEY_1="remote.origin.pushurl", GIT_CONFIG_VALUE_1="no-push://blocked")
        yield env
