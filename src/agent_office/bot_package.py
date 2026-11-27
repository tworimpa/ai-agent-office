"""Agent Plugins wrapper for the portable Office bot definition."""
from dataclasses import dataclass
from io import BytesIO
import json
import re
from zipfile import ZIP_DEFLATED, ZipFile

from . import bot_spec
from .models import OfficeError


NAMESPACE = "io.github.tworimpa.agentoffice"
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
DEFINITION_PATH = f"{NAMESPACE}/bot.json"
OFFICE_VERSION = {"packageVersion": "1.0"}
PLUGIN_BYTES = 32 * 1024
SKILL_BYTES = 8 * 1024
SKILL_COUNT = 32
_PLUGIN_TEXT = {"version", "description", "homepage", "repository", "license"}


@dataclass(frozen=True)
class Package:
    name: str
    version: str | None
    definition: dict


def _encode(document):
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def is_memory_path(path):
    return "memory.md" in path.casefold().split("/")


def without_memory(document):
    return bot_spec.validate({
        **document,
        "files": {path: text for path, text in document["files"].items() if not is_memory_path(path)},
        "folders": [path for path in document.get("folders", ()) if not is_memory_path(path)],
    })


def export_zip(document):
    portable = without_memory(document)
    plugin = {"$schema": PLUGIN_SCHEMA, "name": portable["id"], "extensions": {NAMESPACE: OFFICE_VERSION}}
    data = _encode(portable)
    bot_spec.loads(data)
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", _encode(plugin))
        archive.writestr(DEFINITION_PATH, data)
    return output.getvalue()


def _plugin(data):
    if len(data) > PLUGIN_BYTES:
        raise ValueError("plugin.json은 UTF-8 32 KiB 이내로 저장하세요.")
    try:
        manifest = json.loads(data.decode("utf-8"), object_pairs_hook=bot_spec.unique_object)
    except (ValueError, RecursionError):
        raise ValueError("plugin.json의 UTF-8 JSON 형식을 확인하세요.") from None
    if not isinstance(manifest, dict) or manifest.get("$schema") != PLUGIN_SCHEMA:
        raise ValueError("plugin.json의 Agent Plugins 1.0.0 스키마를 확인하세요.")
    name = manifest.get("name")
    if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?", name)
            or "--" in name or ".." in name):
        raise ValueError("plugin.json의 패키지 이름을 확인하세요.")
    author, keywords = manifest.get("author", {}), manifest.get("keywords", [])
    if (any(key in manifest and not isinstance(manifest[key], str) for key in _PLUGIN_TEXT)
            or not isinstance(author, dict) or author.keys() - {"name", "email", "url"}
            or any(not isinstance(value, str) for value in author.values())
            or not isinstance(keywords, list) or any(not isinstance(value, str) for value in keywords)):
        raise ValueError("plugin.json의 메타데이터 형식을 확인하세요.")
    extensions = manifest.get("extensions")
    if not isinstance(extensions, dict) or extensions.get(NAMESPACE) != OFFICE_VERSION:
        raise ValueError("plugin.json의 Agent Office 패키지 버전 1.0 확장을 확인하세요.")
    return manifest


def read(plugin_bytes, definition_bytes, skills):
    plugin = _plugin(plugin_bytes)
    try:
        definition = bot_spec.loads(definition_bytes)
    except UnicodeError:
        raise ValueError("bot.json은 UTF-8로 저장하세요.") from None
    files = definition["files"]
    if any(is_memory_path(path) for path in (*files, *skills, *definition["folders"])):
        raise ValueError("공유 패키지에서 MEMORY.md를 빼고 다시 설치하세요.")
    for path, data in skills.items():
        if path in files:
            raise ValueError("bot.json과 패키지의 스킬 경로가 중복됩니다.")
        try:
            files[path] = data.decode("utf-8")
        except UnicodeError:
            raise ValueError("SKILL.md는 UTF-8로 저장하세요.") from None
    return Package(plugin["name"], plugin.get("version"), bot_spec.validate(definition))


def installed_sources(db, workspace):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_bot_package_sources'").fetchone():
        return {}
    return {bot_id: json.loads(source) for bot_id, source in db.execute(
        "SELECT bot_id, source FROM studio_bot_package_sources WHERE workspace_id=?", (workspace,),
    )}


def replace_source(db, workspace, bot_id, source):
    if db.execute("UPDATE studio_bot_package_sources SET source=? WHERE workspace_id=? AND bot_id=?",
                  (json.dumps(source), workspace, bot_id)).rowcount != 1:
        raise OfficeError("NOT_FOUND")


def record_source(db, workspace, bot_id, source):
    """Return the original receipt on retry. The caller owns the write transaction."""
    db.execute("""CREATE TABLE IF NOT EXISTS studio_bot_package_sources(
        workspace_id TEXT NOT NULL, bot_id TEXT NOT NULL, source TEXT NOT NULL,
        PRIMARY KEY(workspace_id, bot_id))""")
    row = db.execute("SELECT source FROM studio_bot_package_sources WHERE workspace_id=? AND bot_id=?",
                     (workspace, bot_id)).fetchone()
    if row is not None:
        previous = json.loads(row[0])
        if any(previous[key] != source[key] for key in ("url", "path", "commit", "digest")):
            raise OfficeError("REQUEST_CONFLICT")
        return previous
    db.execute("INSERT INTO studio_bot_package_sources VALUES(?,?,?)", (workspace, bot_id, json.dumps(source)))
    return None
