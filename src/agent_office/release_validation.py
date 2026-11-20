"""Validate an exact Git tree with an operator-selected local Linux container."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import threading
import unicodedata

from .engines import resolve_executable


MAX_FILES = 4096
MAX_SOURCE = 32 * 1024 * 1024
MAX_OUTPUT = 16 * 1024
RUN_TIMEOUT = 120
OWNER_LABEL = "org.agent-office.validation-owner"
COMMIT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
RESERVED_WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³"}


def _execute(argv, *, limit, timeout, env, source=None, stderr=subprocess.STDOUT):
    """Bound the pipe while the child runs, including on Windows."""
    if argv[0] in {"git", "docker"}:
        command = argv[0] + (".exe" if os.name == "nt" else "")
        executable = resolve_executable(command)
        if executable is None:
            raise FileNotFoundError(f"Missing required executable: {command}")
        argv = [executable, *argv[1:]]
    with subprocess.Popen(argv, stdin=source if source is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=stderr, env=env) as process:
        chunks = []

        def read():
            data = process.stdout.read(limit + 1)
            chunks.append(data)
            if len(data) > limit:
                process.kill()

        reader = threading.Thread(target=read)
        reader.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"{Path(argv[0]).name} timed out after {timeout}s") from None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            reader.join()
        if not chunks or len(chunks[0]) > limit:
            raise ValueError(f"{Path(argv[0]).name} output exceeded {limit} bytes")
        return process.returncode, chunks[0]


def _tool_env():
    return {key: os.environ[key] for key in
            ("PATH", "PATHEXT", "SystemRoot", "WINDIR", "TMPDIR", "TMP", "TEMP")
            if key in os.environ}


def _check_path(name, seen):
    parts = name.split("/")
    if len(parts) > 64 or len(name.encode()) > 1024:
        raise ValueError("Candidate path exceeds limit")
    prefix = []
    for part in parts:
        if (not part or part.endswith((" ", ".")) or part.casefold() == ".git"
                or any(character in '<>:"\\|?*~' or ord(character) < 32 for character in part)
                or part.partition(".")[0].rstrip(" ").upper() in RESERVED_WINDOWS_NAMES):
            raise ValueError("Unsupported candidate path")
        prefix.append(part)
        raw = "/".join(prefix)
        key = unicodedata.normalize("NFC", raw).casefold()
        if seen.setdefault(key, raw) != raw:
            raise ValueError("Candidate paths collide on a case-insensitive filesystem")


def committed_files(git_dir: Path, sha: str) -> list[tuple[str, int, bytes]]:
    """Read the raw blobs, preserving executable bits regardless of checkout attributes."""
    if not isinstance(sha, str) or not COMMIT_ID.fullmatch(sha):
        raise ValueError("Expected a full commit ID")
    git = ["git", "--no-replace-objects", f"--git-dir={git_dir.resolve()}"]
    env = {**_tool_env(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}

    def run(args, limit, source=None):
        status, data = _execute([*git, *args], env=env, timeout=30, limit=limit, source=source)
        if status:
            raise ValueError("Git could not read the complete candidate commit")
        return data

    if run(["cat-file", "-t", sha], 1024).strip() != b"commit":
        raise ValueError("Candidate object must be a commit")
    tree = run(["ls-tree", "--full-tree", "-rtlz", sha], 4 * 1024 * 1024)
    files, paths, directories, parents = [], {}, set(), set()
    total = 0
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        header, raw_name = entry.split(b"\t", 1)
        mode, kind, oid, size = header.split()
        name = raw_name.decode("utf-8")
        _check_path(name, paths)
        parents.add(name.rpartition("/")[0])
        if mode == b"040000" and kind == b"tree":
            directories.add(name)
            continue
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise ValueError("Candidate contains a symlink, submodule or unsupported file mode")
        nbytes = int(size)
        total += nbytes
        if len(files) >= MAX_FILES or total > MAX_SOURCE:
            raise ValueError("Candidate file count or size exceeds limit")
        files.append((name, int(mode, 8) & 0o777, oid, nbytes))
    if not files or directories - parents:
        raise ValueError("Empty candidate trees are unsupported")
    with tempfile.TemporaryFile() as request:
        request.writelines(oid + b"\n" for _, _, oid, _ in files)
        request.seek(0)
        data = io.BytesIO(run(["cat-file", "--batch"], total + 128 * len(files), request))
    result = []
    for name, mode, oid, size in files:
        if data.readline().split() != [oid, b"blob", str(size).encode()]:
            raise ValueError("Git returned an unexpected blob")
        content = data.read(size)
        if len(content) != size or data.read(1) != b"\n":
            raise ValueError("Git returned an incomplete blob")
        result.append((name, mode, content))
    if data.read(1):
        raise ValueError("Unexpected bytes after Git snapshot")
    return result


def _local_daemon():
    # Ignore named/remote contexts. Honor only an explicit local socket override.
    if os.name == "nt":
        endpoint = os.environ.get("DOCKER_HOST", "npipe:////./pipe/docker_engine")
        if re.fullmatch(r"npipe:////\./pipe/[\w.-]+", endpoint):
            return endpoint
    else:
        endpoint = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
        if endpoint.startswith("unix:///") and not endpoint.startswith("unix:////"):
            return endpoint
    raise ValueError("Validation requires a local Docker daemon socket")


def _container_identity(git_dir, sha, image):
    location = os.fsencode(os.path.normcase(git_dir.resolve()))
    owner = hashlib.sha256(b"\0".join((location, sha.encode(), image.encode()))).hexdigest()
    return f"agent-office-validator-{owner}", owner


def _remove_owned(docker, env, name, owner):
    # Only dereference the name during inspection. Remove by full ID to avoid
    # a concurrent deletion/name reuse targeting another container.
    template = '{{.Id}} {{.Name}} {{index .Config.Labels "' + OWNER_LABEL + '"}}'
    code, data = _execute([*docker, "container", "inspect", "--format", template, name],
                          limit=4096, timeout=15, env=env)
    if code and b"no such container:" in data.lower():
        return
    fields = data.decode("utf-8", "replace").strip().split()
    if (code or len(fields) != 3 or not re.fullmatch(r"[0-9a-f]{64}", fields[0])
            or fields[1:] != ["/" + name, owner]):
        raise ValueError("Could not verify validator container ownership")
    code, data = _execute([*docker, "rm", "--force", "--volumes", fields[0]],
                          limit=4096, timeout=15, env=env)
    if code and b"no such container:" not in data.lower():
        raise ValueError("Could not verify validator container cleanup")


def _validate(git_dir, sha, image, result):
    if not isinstance(image, str) or not IMAGE_ID.fullmatch(image):
        raise ValueError("Expected a local immutable image ID (sha256: followed by 64 hex digits)")
    files = committed_files(git_dir, sha)
    with tempfile.TemporaryDirectory() as config, tempfile.TemporaryFile() as source:
        with tarfile.open(fileobj=source, mode="w") as archive:
            for name, mode, content in files:
                member = tarfile.TarInfo(name)
                member.mode, member.size = mode, len(content)
                archive.addfile(member, io.BytesIO(content))
        source.seek(0)
        # Empty config excludes client proxy injection and registry credentials.
        docker = ["docker", "--config", config, "--host", _local_daemon()]
        env = _tool_env()
        status, info = _execute([*docker, "image", "inspect", image],
                                limit=128 * 1024, timeout=15, env=env)
        metadata = json.loads(info) if not status else []
        if (not isinstance(metadata, list) or len(metadata) != 1 or not isinstance(metadata[0], dict)
                or metadata[0].get("Id") != image or metadata[0].get("Os") != "linux"
                or not isinstance(metadata[0].get("Config"), dict)
                or metadata[0]["Config"].get("Volumes")):
            raise ValueError("A local Linux validator image without declared volumes is required")

        image_config = metadata[0]["Config"]
        entrypoint, cmd = image_config.get("Entrypoint") or [], image_config.get("Cmd") or []
        if not isinstance(entrypoint, list) or not isinstance(cmd, list):
            raise ValueError("Invalid validator image command")
        command = entrypoint + cmd
        if not command or not all(isinstance(arg, str) for arg in command):
            raise ValueError("Validator image requires an entrypoint or command")
        name, owner = _container_identity(git_dir, sha, image)
        _remove_owned(docker, env, name, owner)
        args = [*docker, "run", "--rm", "--name", name, "--label", f"{OWNER_LABEL}={owner}",
                "--pull=never", "--interactive",
                "--network=none", "--read-only", "--user=65534:65534", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=256m",
                "--memory-swap=256m", "--cpus=1", "--log-driver=none",
                "--ulimit=nofile=128:128", "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=96m,mode=1777",
                "--entrypoint=/usr/bin/timeout", image, "--signal=KILL", "--",
                f"{RUN_TIMEOUT}s", *command]
        try:
            code, output = _execute(args, limit=MAX_OUTPUT, timeout=RUN_TIMEOUT + 15, env=env,
                                    source=source)
            result.update(exit_code=code, output=output.decode("utf-8", "replace"))
        finally:
            # --rm handles normal exits. A disconnected/killed client may leave a job running.
            _remove_owned(docker, env, name, owner)
        result["passed"] = result["exit_code"] == 0


def validate_candidate(git_dir: Path, sha: str, image: str) -> dict:
    result = dict(sha=sha, image=image, passed=False, exit_code=None, output="")
    try:
        _validate(git_dir, sha, image, result)
    except (OSError, ValueError) as error:
        result["output"] = str(error)[:MAX_OUTPUT]
    return result
