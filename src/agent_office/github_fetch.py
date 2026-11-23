"""Bring an authenticated remote branch tip into the local object database without updating user refs."""
import base64
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

from .engines import resolve_executable
from .process_window import BACKGROUND_FLAGS
from .github_api import _branch, _sha
from .github_connection import GitHubError, _origin, repository_name

MAX_PACK = 256 * 1024 * 1024


def _environment():
    keep = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR"}
    return {**{k: v for k, v in os.environ.items() if k.upper() in keep},
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_ATTR_NOSYSTEM": "1", "GIT_TEMPLATE_DIR": "", "LC_ALL": "C"}


def _local(git, cwd, *args, env, data=None, stdin=None, stdout=subprocess.PIPE, optional=False):
    source = None if data is not None else stdin if stdin is not None else subprocess.DEVNULL
    try:
        result = subprocess.run([git, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args],
                                cwd=cwd, env=env, stdin=source, creationflags=BACKGROUND_FLAGS,
                                input=data, stdout=stdout, stderr=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise GitHubError("git_objects_unavailable") from None
    if result.returncode and not optional:
        raise GitHubError("git_objects_unavailable")
    return result


def _fetch_environment(base, private, cfg, url, token):
    if cfg.proxy == "system":
        parsed = urlsplit(url)
        proxy = "" if proxy_bypass(parsed.netloc) else getproxies().get("https", "")
        if proxy:
            proxy = _origin(proxy, proxy=True)
    else:
        proxy = "" if cfg.proxy == "direct" else cfg.proxy
    settings = {"credential.helper": "", "http.proxy": proxy, "http.followRedirects": "false",
                "http.sslVerify": "true", "http.lowSpeedLimit": "1024", "http.lowSpeedTime": "30",
                "core.hooksPath": "/dev/null", "gc.auto": "0", "fetch.writeCommitGraph": "false",
                "fetch.unpackLimit": "1", "fetch.fsckObjects": "true", "submodule.recurse": "false",
                "fetch.recurseSubmodules": "false", "maintenance.auto": "false"}
    if cfg.ca_file:
        settings["http.sslCAInfo"] = str(cfg.ca_file)
        settings["http.schannelUseSSLCAInfo"] = "true"
    if token:
        auth = base64.b64encode(("x-access-token:" + token).encode("ascii")).decode("ascii")
        settings["http." + url + ".extraHeader"] = "Authorization: Basic " + auth
    env = {**base, "GIT_ALLOW_PROTOCOL": "https", "HOME": str(private), "USERPROFILE": str(private),
           "CURL_HOME": str(private), "XDG_CONFIG_HOME": str(private), "GIT_CONFIG_COUNT": str(len(settings))}
    for index, (key, value) in enumerate(settings.items()):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _fetch(git, private, url, branch, env):
    argv = [git, "fetch", "--no-tags", "--no-progress", "--no-write-fetch-head", "--no-recurse-submodules",
            url, "refs/heads/" + branch + ":refs/heads/verified"]
    job = process = None
    try:
        if os.name == "nt":
            from .studio_process import WindowsJob
            job = WindowsJob()
            process = job.spawn(argv, cwd=private, env=env)
            process.stdin.close()
        else:
            process = subprocess.Popen(argv, cwd=private, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 120
        while True:
            try:
                size = sum(p.stat().st_size for p in (private / "objects" / "pack").iterdir())
            except FileNotFoundError:
                size = 0
            if size > MAX_PACK:
                raise GitHubError("git_fetch_too_large")
            if process.poll() is not None:
                break
            if time.monotonic() >= deadline:
                raise GitHubError("git_fetch_timeout")
            time.sleep(0.1)
        if process.poll() != 0:
            raise GitHubError("git_fetch_failed")
    except OSError:
        raise GitHubError("git_fetch_failed") from None
    finally:
        if job:
            job.close()
        elif process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def ensure_base(root, slug, branch, sha, client, *, expected_repo_id=None):
    """Fetch only on a missing commit; preserve checkout HEAD, local and remote-tracking refs, and index."""
    _sha(sha)
    _branch(branch)
    repository_name(slug)
    git = resolve_executable("git.exe" if os.name == "nt" else "git")
    if not git:
        raise GitHubError("git_unavailable")
    env = _environment()
    # Local metadata reads must honor the service account's already configured
    # safe.directory entries. The network fetch runs only in a private repository
    # with the isolated config above, never with the source checkout's settings.
    source_env = {key: value for key, value in env.items() if key not in {"GIT_CONFIG_GLOBAL","GIT_CONFIG_NOSYSTEM"}}
    for key in ("HOME", "USERPROFILE", "PROGRAMDATA", "XDG_CONFIG_HOME", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL"):
        if key in os.environ:
            source_env[key] = os.environ[key]
    if _local(git, root, "rev-parse", "--verify", sha + "^{commit}", env=source_env, optional=True).stdout.strip() == sha.encode():
        return sha
    objects = Path(os.fsdecode(_local(git, root, "rev-parse", "--path-format=absolute",
                                      "--git-path", "objects", env=source_env).stdout.rstrip(b"\r\n")))
    if not objects.is_absolute() or not objects.is_dir() or any(c in str(objects) for c in "\r\n"):
        raise GitHubError("git_objects_unavailable")
    url = client.web_url + "/" + slug + ".git"
    with tempfile.TemporaryDirectory(prefix="office-github-fetch-") as temp:
        private = Path(temp) / "remote.git"
        _local(git, temp, "init", "--bare", "--template=", str(private), env=env)
        # Advertise an existing commit for negotiation. A shallow checkout may
        # lack its ancestors; fetch a complete history in that case.
        shallow = _local(git, root, "rev-parse", "--is-shallow-repository", env=source_env).stdout.strip()
        local_tip = _local(git, root, "rev-parse", "--verify", "HEAD^{commit}", env=source_env, optional=True)
        if shallow == b"false" and local_tip.returncode == 0:
            (private / "objects" / "info" / "alternates").write_bytes(os.fsencode(objects.as_posix()) + b"\n")
            _local(git, private, "update-ref", "refs/heads/known", _sha(local_tip.stdout.decode().strip()), env=env)
        with client.git_read_token(slug, expected_repo_id) as token:
            _fetch(git, private, url, branch, _fetch_environment(env, private, client.config, url, token))
        pinned = _local(git, private, "rev-parse", "--verify", "refs/heads/verified^{commit}", env=env).stdout.strip()
        if pinned != sha.encode() or client.branch_sha(slug, branch) != sha:
            raise GitHubError("fetch_head_changed")
        if expected_repo_id is not None and client.repository(slug).get("id") != expected_repo_id:
            raise GitHubError("repository_identity_changed")
        with tempfile.TemporaryFile(dir=temp) as pack:
            _local(git, private, "pack-objects", "--quiet", "--local", "--stdout", "--revs", env=env,
                   data=(sha + "\n").encode(), stdout=pack)
            if pack.tell() > MAX_PACK:
                raise GitHubError("git_fetch_too_large")
            pack.seek(0)
            # index-pack verifies checksums and installs immutable objects. It
            # runs in our bare repo with only the object's destination overridden.
            _local(git, private, "index-pack", "--strict", "--stdin", "--fix-thin",
                   env={**env, "GIT_OBJECT_DIRECTORY": str(objects)}, stdin=pack)
    if _local(git, root, "rev-parse", "--verify", sha + "^{commit}", env=source_env).stdout.strip() != sha.encode():
        raise GitHubError("git_objects_unavailable")
    return sha
