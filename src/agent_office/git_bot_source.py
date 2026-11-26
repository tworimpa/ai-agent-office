"""Read bot packages from a pinned public HTTPS Git commit without a checkout."""
from contextlib import ExitStack
from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
from tempfile import TemporaryDirectory, TemporaryFile
from threading import BoundedSemaphore
import time
from urllib.parse import urlsplit

from . import bot_package, bot_spec
from .engines import resolve_executable
from .studio_process import WindowsJob


_OBJECT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.IGNORECASE | re.ASCII)
_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}", re.ASCII)
_REPO_PATH = re.compile(r"(?:/[a-zA-Z0-9._~-]+)+")
_TAG = re.compile(r"[^\x00-\x20\x7f~^:?*\[\\]{1,180}")
_FETCH_LIMIT = 64 * 1024 * 1024
_FETCH_SLOTS = BoundedSemaphore(2)
_TIME_LIMIT = 30


@dataclass(frozen=True)
class Source:
    url: str
    version: str
    path: str
    host: str
    ref: str

    @classmethod
    def parse(cls, data):
        if set(data) not in ({"url", "version"}, {"url", "version", "path"}):
            raise ValueError("저장소 URL, 버전과 선택 하위 폴더를 확인하세요.")
        url, version, path = data["url"], data["version"], data.get("path", "")
        if (not isinstance(url, str) or len(url) > 500
                or any(ord(char) <= 32 or ord(char) >= 127 for char in url)):
            raise ValueError("공개 HTTPS Git 저장소 URL을 입력하세요.")
        parsed = urlsplit(url)
        host = parsed.hostname
        remote_path = parsed.path.removesuffix("/")
        if (parsed.scheme != "https" or not host or not _HOST.fullmatch(host)
                or parsed.netloc.lower() not in (host, f"{host}:443") or not _REPO_PATH.fullmatch(remote_path)
                or any(component in (".", "..") for component in remote_path.split("/"))
                or "?" in url or "#" in url):
            raise ValueError("공개 HTTPS Git 저장소 URL을 입력하세요.")
        if not isinstance(version, str):
            raise ValueError("Git 태그나 전체 커밋 SHA를 입력하세요.")
        if _OBJECT.fullmatch(version):
            ref = version.lower()
        elif (_TAG.fullmatch(version) and not version.startswith("refs/")
              and ".." not in version and "@{" not in version and not version.endswith(".")
              and all(part and not part.startswith(".") and not part.endswith(".lock")
                      for part in version.split("/"))):
            ref = "refs/tags/" + version
        else:
            raise ValueError("Git 태그나 전체 커밋 SHA를 입력하세요.")
        if not isinstance(path, str):
            raise ValueError("패키지 하위 폴더를 확인하세요.")
        if path:
            bot_spec.check_path(path)
        return cls(f"https://{host}{remote_path}", version, path, host, ref)

    def receipt(self, commit, digest):
        return {"url": self.url, "version": self.version, "path": self.path, "commit": commit, "digest": digest}


@dataclass(frozen=True)
class Fetched:
    commit: str
    package: bot_package.Package


def _network_options(host):
    try:
        answers = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)
        addresses = {ipaddress.ip_address(answer[4][0]) for answer in answers}
    except (OSError, ValueError):
        raise ValueError("Git 저장소 주소를 확인할 수 없습니다.") from None
    if not addresses or any(
        not address.is_global or address.is_multicast or address.is_reserved
        or isinstance(address, ipaddress.IPv6Address) and (
            address.ipv4_mapped or address.sixtofour or address.teredo or address.scope_id
        ) for address in addresses
    ):
        raise ValueError("공개 인터넷의 Git 저장소 주소만 사용할 수 있습니다.")
    pinned = ",".join(
        str(address) if address.version == 4 else f"[{address}]"
        for address in sorted(addresses, key=lambda address: (address.version, int(address)))
    )
    return ["-c", f"http.curloptResolve={host}:443:{pinned}"]


class _Repository:
    def __init__(self, root):
        self.root = root
        self.git = resolve_executable("git.exe" if os.name == "nt" else "git")
        if not self.git:
            raise ValueError("공유 패키지를 설치하려면 Git이 필요합니다.")
        self.deadline = time.monotonic() + _TIME_LIMIT
        # Use only the host's executable lookup and OS bootstrap environment.
        self.env = {key: os.environ[key] for key in (
            "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
        ) if key in os.environ}
        self.env.update(
            HOME=str(root), USERPROFILE=str(root), LC_ALL="C",
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="https",
            GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1",
        )

    def run(self, *args, message, limit=128 * 1024, transfer=False):
        argv = [self.git, "-C", str(self.root), "-c", "core.hooksPath=" + os.devnull, *args]
        with ExitStack() as stack:
            output = stack.enter_context(TemporaryFile())
            if os.name == "nt":
                job = WindowsJob()
                stack.callback(job.close)
                child = job.spawn(argv, output, cwd=self.root, env=self.env)
                child.stdin.close()
                stack.callback(child.handle.Close)
            else:
                child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output,
                                         stderr=subprocess.DEVNULL, cwd=self.root, env=self.env,
                                         start_new_session=True)
            try:
                while True:
                    if os.fstat(output.fileno()).st_size > limit or transfer and self.pack_size() > _FETCH_LIMIT:
                        raise ValueError("Git 패키지 확인 중 다운로드 또는 파일 크기 한도를 초과했습니다.")
                    status = child.poll()
                    if status is not None:
                        break
                    if time.monotonic() >= self.deadline:
                        raise ValueError("Git 저장소 확인 시간이 초과되었습니다.")
                    time.sleep(0.02)
            finally:
                if child.poll() is None:
                    if os.name == "nt":
                        job.drain(child)
                    else:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        child.wait()
            if status:
                raise ValueError(message)
            if os.fstat(output.fileno()).st_size > limit or transfer and self.pack_size() > _FETCH_LIMIT:
                raise ValueError("Git 패키지 확인 중 다운로드 또는 파일 크기 한도를 초과했습니다.")
            output.seek(0)
            return output.read()

    def pack_size(self):
        size = 0
        for file in (self.root / "objects" / "pack").iterdir():
            try:
                size += file.stat().st_size
            except FileNotFoundError:
                # Git renames the temporary pack after indexing it.
                continue
        return size

    def size_checked_object(self, kind, oid, limit):
        error = "Git 패키지의 객체를 읽을 수 없습니다."
        size = int(self.run("cat-file", "-s", oid, message=error))
        if size > limit:
            raise ValueError("Git 패키지의 파일 크기 한도를 초과했습니다.")
        return self.run("cat-file", kind, oid, limit=limit, message=error)

    def entries(self, tree):
        data = self.run("ls-tree", "-z", tree, message="Git 패키지 폴더를 읽을 수 없습니다.")
        entries = {}
        try:
            for entry in data.rstrip(b"\0").split(b"\0") if data else ():
                metadata, name = entry.split(b"\t", 1)
                mode, _, oid = metadata.decode("ascii").split()
                name = name.decode("utf-8")
                if name in entries:
                    raise ValueError("duplicate tree entry")
                entries[name] = (mode, oid)
        except (UnicodeError, ValueError):
            raise ValueError("Git 패키지 폴더의 파일 이름을 확인하세요.") from None
        return entries

    @staticmethod
    def entry(entries, name, modes, *, optional=False):
        matches = [key for key in entries if key.casefold() == name.casefold()]
        if not matches and optional:
            return None
        if matches != [name]:
            raise ValueError(f"Git 패키지의 {name} 경로를 확인하세요.")
        mode, oid = entries[name]
        if mode not in modes:
            raise ValueError(f"Git 패키지의 {name}에는 일반 파일과 폴더만 사용할 수 있습니다.")
        return oid

    def read(self, commit, path):
        dirs, files = {"040000"}, {"100644", "100755"}
        entries = self.entries(commit)
        if path:
            for part in path.split("/"):
                entries = self.entries(self.entry(entries, part, dirs))
        plugin = self.entry(entries, "plugin.json", files)
        office = self.entries(self.entry(entries, bot_package.NAMESPACE, dirs))
        definition = self.entry(office, "bot.json", files)
        skills = {}
        if skill_tree := self.entry(entries, "skills", dirs, optional=True):
            for name, (mode, oid) in self.entries(skill_tree).items():
                if mode not in dirs:
                    if mode not in files:
                        raise ValueError("공유 스킬 폴더에는 일반 파일과 폴더만 사용할 수 있습니다.")
                    continue
                if leaf := self.entry(self.entries(oid), "SKILL.md", files, optional=True):
                    if len(skills) >= bot_package.SKILL_COUNT:
                        raise ValueError("공유 스킬 파일은 최대 32개입니다.")
                    key = f"skills/{name}/SKILL.md"
                    bot_spec.check_path(key)
                    skills[key] = self.size_checked_object("blob", leaf, bot_package.SKILL_BYTES)
        return bot_package.read(
            self.size_checked_object("blob", plugin, bot_package.PLUGIN_BYTES),
            self.size_checked_object("blob", definition, bot_spec.MAX_DOCUMENT_BYTES),
            skills,
        )


def _fetch(source, root):
    repo = _Repository(root)
    version = repo.run("--version", message="Git 버전을 확인할 수 없습니다.")
    numbers = re.match(rb"git version (\d+)\.(\d+)", version)
    if not numbers or tuple(map(int, numbers.groups())) < (2, 39):
        raise ValueError("공개 Git 패키지 설치에는 Git 2.39 이상이 필요합니다.")
    options = [
        *_network_options(source.host), "-c", "http.followRedirects=false",
        "-c", "http.sslVerify=true", "-c", "http.proxy=", "-c", "credential.helper=",
        "-c", "fetch.unpackLimit=1", "-c", "fetch.fsckObjects=true",
        "-c", "http.lowSpeedLimit=1024", "-c", "http.lowSpeedTime=8",
    ]
    missing = "공개 Git 저장소에 연결할 수 없거나 지정한 태그·커밋이 없습니다."
    if source.ref.startswith("refs/tags/"):
        listing = repo.run(*options, "ls-remote", "--exit-code", "--tags", "--", source.url,
                           source.ref, source.ref + "^{}", message=missing)
        refs = {}
        for line in listing.splitlines():
            value, separator, name = line.partition(b"\t")
            if separator:
                refs[name] = value
        key = source.ref.encode("utf-8")
        ref = refs.get(key + b"^{}", refs.get(key, b"")).decode("ascii", errors="replace")
    else:
        ref = source.ref
    if not _OBJECT.fullmatch(ref):
        raise ValueError(missing)
    object_format = "sha1" if len(ref) == 40 else "sha256"
    repo.run("init", "--bare", "--quiet", "--template=", f"--object-format={object_format}",
             message="임시 Git 저장소를 만들 수 없습니다.")
    repo.run(*options, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules",
             "--no-auto-maintenance", "--depth=1", "--", source.url, source.ref,
             message=missing, transfer=True)
    commit = repo.run("rev-parse", "--verify", "FETCH_HEAD^{commit}",
                      message="버전이 Git 커밋을 가리키는지 확인하세요.").decode("ascii").strip()
    if commit != ref.lower():
        raise ValueError("Git 태그가 변경되었습니다. 패키지를 다시 확인하세요.")
    return Fetched(commit, repo.read(commit, source.path))


def fetch(source):
    if not _FETCH_SLOTS.acquire(blocking=False):
        raise ValueError("다른 Git 패키지를 확인 중입니다. 잠시 후 다시 시도하세요.")
    try:
        with TemporaryDirectory(prefix="office-bot-git-") as directory:
            return _fetch(source, Path(directory))
    except OSError:
        raise ValueError("Git 패키지 확인 중 호스트에서 파일을 읽거나 실행하지 못했습니다.") from None
    finally:
        _FETCH_SLOTS.release()
