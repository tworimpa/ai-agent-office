"""Versioned, published discovery metadata for pinned public Git bot packages."""
from hashlib import sha256
from http.client import HTTPException
import json
import re
from threading import Lock
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import bot_spec, git_bot_source, local_bots
from .models import OfficeError


INDEX_URL = "https://raw.githubusercontent.com/tworimpa/ai-agent-office/main/catalog/bots.json"
MAX_INDEX_BYTES = 256 * 1024
LINK_ROUTE = re.compile(r"/bot-catalog/links/(?=[a-z0-9-]{1,64}/)([a-z0-9]+(?:-[a-z0-9]+)*)/([0-9a-f]{32})")
_ENTRY_KEYS = {"id", "botId", "publisher", "name", "role", "description", "tags", "package"}
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}")


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - set(optional):
        raise ValueError("공유 봇 카탈로그 항목의 필드를 확인하세요.")


def _text(value, length):
    if (not isinstance(value, str) or not 0 < len(value) <= length or value.strip() != value
            or not value.isprintable()):
        raise ValueError("공유 봇 카탈로그의 한 줄 표시 문구를 확인하세요.")
    return value


def _detail_url(value):
    _text(value, 1000)
    if not value.isascii() or any(char in value for char in ('\\', ' ', '"', '<', '>')):
        raise ValueError("CODE-Skills의 HTTPS 상세 주소를 확인하세요.")
    if re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f|5c)|%(?![0-9a-f]{2})", value, re.I):
        raise ValueError("CODE-Skills의 HTTPS 상세 주소를 확인하세요.")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise ValueError("CODE-Skills의 HTTPS 상세 주소를 확인하세요.") from None
    if (parsed.scheme != "https" or not host or len(host) > 253 or not _DOMAIN.fullmatch(host)
            or parsed.netloc.casefold() not in {host, f"{host}:{port}"} or port == 0):
        raise ValueError("CODE-Skills의 HTTPS 상세 주소를 확인하세요.")
    return value


def package_source(package):
    _fields(package, {"url", "commit"}, {"path", "version"})
    commit = package["commit"]
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise ValueError("카탈로그 패키지는 전체 커밋 SHA로 지정하세요.")
    return git_bot_source.Source.parse({"url": package["url"], "path": package.get("path", ""), "version": commit})


def _entry(value):
    _fields(value, _ENTRY_KEYS, {"skills"})
    if not bot_spec.valid_id(value["id"]) or not bot_spec.valid_id(value["botId"]):
        raise ValueError("카탈로그 ID와 봇 ID의 형식을 확인하세요.")
    source = package_source(value["package"])
    package = {"url": source.url, "path": source.path, "commit": source.ref}
    if "version" in value["package"]:
        package["version"] = _text(value["package"]["version"], 80)
    tags = value["tags"]
    if not isinstance(tags, list) or len(tags) > 16:
        raise ValueError("카탈로그 항목의 태그는 최대 16개입니다.")
    tags = [_text(tag, 32) for tag in tags]
    if len(set(tag.casefold() for tag in tags)) != len(tags):
        raise ValueError("카탈로그 태그가 중복됩니다.")
    skill_records = value.get("skills", [])
    if not isinstance(skill_records, list) or len(skill_records) > 32:
        raise ValueError("공유 봇의 스킬 목록을 확인하세요.")
    skills = []
    paths = set()
    for record in skill_records:
        _fields(record, {"name", "path"}, {"url"})
        path = record["path"]
        parts = bot_spec.check_path(path)
        if len(parts) < 3 or parts[0] != "skills" or parts[-1] != "SKILL.md" or path.casefold() in paths:
            raise ValueError("공유 봇의 중복 또는 잘못된 SKILL.md 경로를 확인하세요.")
        paths.add(path.casefold())
        skill = {"name": _text(record["name"], 80), "path": path}
        if "url" in record:
            skill["url"] = _detail_url(record["url"])
        skills.append(skill)
    return {"id": value["id"], "botId": value["botId"], "publisher": _text(value["publisher"], 80),
            "name": _text(value["name"], 80), "role": _text(value["role"], 80),
            "description": _text(value["description"], 500), "tags": tags, "package": package, "skills": skills}


def loads(data):
    if len(data) > MAX_INDEX_BYTES:
        raise ValueError("공유 봇 카탈로그는 UTF-8 256 KiB 이내여야 합니다.")
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=bot_spec.unique_object)
    except (ValueError, RecursionError):
        raise ValueError("공유 봇 카탈로그의 UTF-8 JSON을 확인하세요.") from None
    _fields(document, {"schemaVersion", "entries"})
    entries = document["entries"]
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1:
        raise ValueError("지원하는 공유 봇 카탈로그 버전은 1입니다.")
    if not isinstance(entries, list) or len(entries) > 256:
        raise ValueError("공유 봇 카탈로그 항목은 최대 256개입니다.")
    entries = [_entry(value) for value in entries]
    if any(len({entry[key] for entry in entries}) != len(entries) for key in ("id", "botId")):
        raise ValueError("카탈로그 ID 또는 봇 ID가 중복됩니다.")
    return {"schemaVersion": 1, "entries": entries}


def verify_packages(document):
    for entry in document["entries"]:
        source = package_source(entry["package"])
        try:
            fetched = git_bot_source.fetch(source)
            definition = fetched.package.definition
            local_bots.shareable_digest(definition)
            if fetched.commit != source.ref or definition["id"] != entry["botId"]:
                raise ValueError("실제 커밋 또는 봇 ID가 카탈로그와 일치하지 않습니다.")
            for skill in entry["skills"]:
                if skill["path"] not in definition["files"]:
                    raise ValueError(f"패키지에 {skill['path']} 파일이 없습니다.")
        except ValueError as error:
            raise ValueError(f"{entry['id']} — {error}") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _download():
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(INDEX_URL, headers={"Cache-Control": "no-cache", "Accept": "application/json, text/plain"})
    try:
        with opener.open(request, timeout=8) as response:
            return response.read(MAX_INDEX_BYTES + 1)
    except (OSError, HTTPException):
        raise ValueError("공유 봇 카탈로그를 읽지 못했습니다. 다시 시도하세요.") from None


class Catalog:
    def __init__(self, loader=None):
        self._loader = loader or _download
        self._refresh = Lock()
        self._links = {}

    def refresh(self):
        if not self._refresh.acquire(blocking=False):
            raise ValueError("공유 봇 카탈로그를 읽는 중입니다. 잠시 후 다시 시도하세요.")
        try:
            snapshot = loads(self._loader())
            links = {}
            for entry in snapshot["entries"]:
                for skill in entry["skills"]:
                    target = skill.pop("url", None)
                    if target is not None:
                        link_id = sha256((skill["path"] + "\n" + target).encode("utf-8")).hexdigest()[:32]
                        links[(entry["id"], link_id)] = target
                        skill["href"] = f"/bot-catalog/links/{entry['id']}/{link_id}"
            self._links = links
            return snapshot
        finally:
            self._refresh.release()

    def link(self, entry_id, link_id):
        target = self._links.get((entry_id, link_id))
        if target is None:
            raise OfficeError("NOT_FOUND")
        return target
