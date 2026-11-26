"""Workspace-local role definitions and the immutable versions used by a run.

The hosting service validates workspace membership using its StudioCatalog.
Call mutating functions under the journal's write transaction.
"""
from hashlib import sha256
import json
import re

from . import bot_spec
from .models import OfficeError, validate_id
from .role_skills import instruction_digest
from .store import encode
from .studio_catalog import _single_line


ROLES = ("coordinator", "developer", "reviewer")
_NAMES = {"coordinator": "조정자", "developer": "개발 담당", "reviewer": "검토 담당"}
_BUILTINS = {f"office-{role}": role for role in ROLES}
_PUBLIC = ("id", "name", "role", "digest", "builtin")


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_local_bots(
        workspace_id TEXT NOT NULL, bot_id TEXT NOT NULL, definition TEXT NOT NULL,
        PRIMARY KEY(workspace_id,bot_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_role_bot_bindings(
        workspace_id TEXT NOT NULL, role TEXT NOT NULL, bot_id TEXT NOT NULL,
        PRIMARY KEY(workspace_id,role),
        FOREIGN KEY(workspace_id,bot_id) REFERENCES studio_local_bots(workspace_id,bot_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_run_role_bots(
        run_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)""")


def _valid_id(bot_id):
    return bot_spec.valid_id(bot_id)


def _asset_name(name):
    if (not isinstance(name, str) or len(name) > 72 or not re.fullmatch(r"[\w-][\w .-]*\.(?:md|txt)", name)
            or ".." in name):
        raise ValueError("에셋은 경로 없이 .md 또는 .txt 파일 이름을 입력하세요.")
    return name


def _definition(bot_id, name, role, instructions, *, builtin=False, assets=None):
    if not _valid_id(bot_id):
        raise ValueError("봇 ID는 영문 소문자와 숫자를 사용하고 단어 사이에 하이픈을 넣어 64자 이내로 입력하세요.")
    if not _single_line(role) or len(role.strip()) > 60:
        raise ValueError("역할은 60자 이내의 한 줄로 입력하세요.")
    role = role.strip()
    if not _single_line(name) or len(name.strip()) > 40:
        raise ValueError("봇 이름은 40자 이내의 한 줄로 입력하세요.")
    if builtin:
        if _BUILTINS.get(bot_id) != role or name != _NAMES[role] or instructions != "" or assets:
            raise ValueError("기본 봇 정의를 확인하세요.")
    else:
        if bot_id in _BUILTINS:
            raise ValueError("기본 봇에 예약된 ID입니다.")
        instruction_digest(instructions)
    document = {"schema_version": 1, "id": bot_id, "name": name.strip(), "role": role,
                "instructions": instructions, "builtin": builtin}
    if assets is not None:
        if not isinstance(assets, dict) or len(assets) > 15:
            raise ValueError("봇에는 지침 외에 최대 15개 파일을 저장할 수 있습니다.")
        names = {"soul.md"}
        for filename, content in assets.items():
            key = _asset_name(filename).casefold()
            if key in names or (key == "memory.md" and filename != "MEMORY.md"):
                raise ValueError("같은 이름의 에셋이 있습니다.")
            names.add(key)
            if content != "":
                instruction_digest(content)
        if sum(len(value.encode("utf-8")) for value in assets.values()) > 24576:
            raise ValueError("추가 에셋의 합계는 UTF-8 24 KiB 이내로 저장하세요.")
    if assets or role not in ROLES:
        document.update(schema_version=2, assets=assets or {})
    return {**document, "digest": sha256(encode(document).encode("utf-8")).hexdigest()}


def _builtin(role):
    return _definition(f"office-{role}", _NAMES[role], role, "", builtin=True)


def _portable(bot):
    return bot_spec.validate({
        "specVersion": bot_spec.VERSION, "id": bot["id"], "name": bot["name"], "role": bot["role"],
        "files": {"SOUL.md": bot["instructions"], **bot.get("assets", {})},
        "folders": bot.get("folders", []), "extensions": bot.get("extensions", {}),
    })


def _from_portable(document):
    package = bot_spec.validate(document)
    bot_id, name, role = (package[k] for k in ("id", "name", "role"))
    assets = package["files"].copy()
    instructions = assets.pop("SOUL.md")
    if bot_id in _BUILTINS:
        builtin = _builtin(_BUILTINS[bot_id])
        if _portable(builtin) != package:
            raise ValueError("기본 봇에 예약된 ID입니다.")
        return builtin
    instruction_digest(instructions)
    if not package["folders"] and not package["extensions"]:
        # Use the historical representation whenever possible, preserving old receipts.
        try:
            return _definition(bot_id, name, role, instructions, assets=assets)
        except ValueError:
            pass
    definition = {"schema_version": 3, "id": bot_id, "name": name, "role": role, "builtin": False,
                  "instructions": instructions, "assets": assets, "folders": package["folders"],
                  "extensions": package["extensions"]}
    return {**definition, "digest": sha256(encode(definition).encode("utf-8")).hexdigest()}


def _checked(document, *, bot_id=None):
    """Verify stored content, including the expected ID when it comes from a catalog row."""
    try:
        if document["schema_version"] == 3:
            rebuilt = _from_portable(_portable(document))
        else:
            rebuilt = _definition(document["id"], document["name"], document["role"], document["instructions"],
                                  builtin=document["builtin"], assets=document.get("assets"))
        if (type(document["builtin"]) is not bool or type(document["schema_version"]) is not int
                or document != rebuilt or bot_id is not None and (document["id"] != bot_id or document["builtin"])):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise OfficeError("STORE_IO") from None
    return rebuilt


def _load(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        raise OfficeError("STORE_IO") from None


def _has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def create(db, workspace_id, bot_id, name, role, instructions):
    validate_id(workspace_id)
    definition = _definition(bot_id, name, role, instructions)
    db.execute("INSERT INTO studio_local_bots VALUES(?,?,?) ON CONFLICT(workspace_id,bot_id) DO NOTHING",
               (workspace_id, bot_id, encode(definition)))
    if get(db, workspace_id, bot_id) != definition:
        raise ValueError("이 작업공간에서 이미 사용하는 봇 ID입니다. 변경한 봇은 새 ID로 등록하세요.")
    return definition


def get(db, workspace_id, bot_id):
    validate_id(workspace_id)
    if not _valid_id(bot_id):
        raise ValueError("유효한 봇 ID를 입력하세요.")
    if bot_id in _BUILTINS:
        return _builtin(_BUILTINS[bot_id])
    if not _has_table(db, "studio_local_bots"):
        raise OfficeError("NOT_FOUND")
    row = db.execute("SELECT definition FROM studio_local_bots WHERE workspace_id=? AND bot_id=?",
                     (workspace_id, bot_id)).fetchone()
    if row is None:
        raise OfficeError("NOT_FOUND")
    return _checked(_load(row[0]), bot_id=bot_id)


def list_bots(db, workspace_id):
    validate_id(workspace_id)
    builtins = [_builtin(role) for role in ROLES]
    if not _has_table(db, "studio_local_bots"):
        return builtins
    rows = db.execute("SELECT bot_id,definition FROM studio_local_bots WHERE workspace_id=? ORDER BY bot_id",
                      (workspace_id,))
    return builtins + [_checked(_load(raw), bot_id=bot_id) for bot_id, raw in rows]


def asset_files(bot):
    return {"SOUL.md": bot["instructions"], "MEMORY.md": "", **bot.get("assets", {})}


def manifest(db, workspace_id, bot_id):
    return _portable(get(db, workspace_id, bot_id))


def manifest_digest(document):
    return _from_portable(document)["digest"]


def install(db, workspace_id, manifest):
    validate_id(workspace_id)
    bot = _from_portable(manifest)
    if not bot["builtin"]:
        db.execute("INSERT INTO studio_local_bots VALUES(?,?,?) ON CONFLICT(workspace_id,bot_id) DO NOTHING",
                   (workspace_id, bot["id"], encode(bot)))
        if get(db, workspace_id, bot["id"]) != bot:
            raise OfficeError("REQUEST_CONFLICT")
    return bot


def tree(bot):
    files = asset_files(bot)
    # v1/v2 were flat, and accepted names that predate portable path restrictions.
    folders = bot_spec.folders_for(files, bot["folders"]) if bot["schema_version"] == 3 else []
    return {"id": bot["id"], "digest": bot["digest"], "files": files, "folders": folders,
            "extensions": bot.get("extensions", {})}


def _editable(db, workspace_id, bot_id, digest):
    bot = get(db, workspace_id, bot_id)
    if bot["builtin"]:
        raise ValueError("직접 만든 봇의 에셋을 선택하세요.")
    if bot["digest"] != digest:
        raise OfficeError("REQUEST_CONFLICT")
    return bot


def _update(db, workspace_id, bot_id, updated):
    db.execute("UPDATE studio_local_bots SET definition=? WHERE workspace_id=? AND bot_id=?",
               (encode(updated), workspace_id, bot_id))
    return updated


def save_entry(db, workspace_id, bot_id, path, content, digest, *, kind="file", create=False):
    bot = _editable(db, workspace_id, bot_id, digest)
    bot_spec.check_path(path)
    package = _portable(bot)
    listing = tree(bot)
    if create and (path in listing["files"] or path in listing["folders"]):
        raise ValueError("이미 존재하는 파일 또는 폴더입니다.")
    if kind == "file":
        if not create and path not in listing["files"]:
            raise ValueError("수정할 파일이 존재하지 않습니다.")
        package["files"][path] = content
    elif kind == "folder" and create and content in ("", None):
        package["folders"].append(path)
    else:
        raise ValueError("파일 또는 빈 폴더를 선택하세요.")
    return _update(db, workspace_id, bot_id, _from_portable(package))


def set_extensions(db, workspace_id, bot_id, extensions, digest):
    bot = _editable(db, workspace_id, bot_id, digest)
    package = _portable(bot)
    package["extensions"] = extensions
    return _update(db, workspace_id, bot_id, _from_portable(package))


def save_asset(db, workspace_id, bot_id, filename, content, expected_digest):
    """Preserve the legacy flat Markdown/text upsert API, including legacy path names."""
    bot = _editable(db, workspace_id, bot_id, expected_digest)
    if bot["schema_version"] == 3:
        return save_entry(db, workspace_id, bot_id, filename, content, expected_digest,
                          create=filename not in asset_files(bot))
    _asset_name(filename)
    files = dict(bot.get("assets", {}))
    if filename != "SOUL.md":
        files[filename] = content
    updated = _definition(bot_id, bot["name"], bot["role"], content if filename == "SOUL.md"
                          else bot["instructions"], assets=files)
    return _update(db, workspace_id, bot_id, updated)


def assign(db, workspace_id, role, bot_id):
    definition = get(db, workspace_id, bot_id)
    if role not in ROLES or (definition["builtin"] and role != definition["role"]):
        raise ValueError("선택한 봇이 담당하는 역할을 확인하세요.")
    if definition["builtin"]:
        db.execute("DELETE FROM studio_role_bot_bindings WHERE workspace_id=? AND role=?", (workspace_id, role))
    else:
        if db.execute("SELECT 1 FROM studio_role_bot_bindings WHERE workspace_id=? AND role<>? AND bot_id=?",
                      (workspace_id, role, bot_id)).fetchone():
            raise ValueError("이 봇은 다른 실행 자리에 배정되어 있습니다. 먼저 기존 배정을 변경하세요.")
        db.execute("""INSERT INTO studio_role_bot_bindings VALUES(?,?,?) ON CONFLICT(workspace_id,role)
            DO UPDATE SET bot_id=excluded.bot_id""", (workspace_id, role, bot_id))
    return definition


def resolve(db, workspace_id):
    validate_id(workspace_id)
    selected = {role: _builtin(role) for role in ROLES}
    if not _has_table(db, "studio_role_bot_bindings"):
        return selected
    rows = db.execute("""SELECT b.role,b.bot_id,c.definition FROM studio_role_bot_bindings b
        LEFT JOIN studio_local_bots c USING(workspace_id,bot_id) WHERE b.workspace_id=?""", (workspace_id,))
    for role, bot_id, raw in rows:
        definition = _checked(_load(raw), bot_id=bot_id)
        if role not in ROLES:
            raise OfficeError("STORE_IO")
        selected[role] = definition
    return selected


def public_receipt(bots):
    # Peers identify the workflow slot here. The pinned definition retains the author's role.
    return {role: {**{key: bot[key] for key in _PUBLIC}, "role": role} for role, bot in bots.items()}


def validate_receipt(receipt):
    if not isinstance(receipt, dict) or set(receipt) != set(ROLES):
        return False
    for role, bot in receipt.items():
        if (not isinstance(bot, dict) or set(bot) != set(_PUBLIC) or bot["role"] != role
                or not _valid_id(bot["id"]) or type(bot["builtin"]) is not bool
                or bot["builtin"] != (bot["id"] in _BUILTINS)
                or not _single_line(bot["name"]) or bot["name"] != bot["name"].strip() or len(bot["name"]) > 40
                or not isinstance(bot["digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", bot["digest"])):
            return False
        if bot["builtin"] and bot != public_receipt({role: _builtin(role)})[role]:
            return False
    return len({bot["id"] for bot in receipt.values()}) == len(ROLES)


def _checked_snapshot(bots):
    if not isinstance(bots, dict) or set(bots) != set(ROLES):
        raise OfficeError("STORE_IO")
    checked = {role: _checked(bot) for role, bot in bots.items()}
    if not validate_receipt(public_receipt(checked)):
        raise OfficeError("STORE_IO")
    return checked


def pin(db, run_id, bots):
    validate_id(run_id)
    snapshot = encode(_checked_snapshot(bots))
    inserted = db.execute("INSERT INTO studio_run_role_bots VALUES(?,?) ON CONFLICT(run_id) DO NOTHING",
                          (run_id, snapshot)).rowcount
    if not inserted and for_run(db, run_id) != bots:
        raise ValueError("이미 시작한 실행의 봇 배정을 바꿀 수 없습니다.")


def for_run(db, run_id):
    validate_id(run_id)
    if not _has_table(db, "studio_run_role_bots"):
        return {}
    row = db.execute("SELECT snapshot FROM studio_run_role_bots WHERE run_id=?", (run_id,)).fetchone()
    return _checked_snapshot(_load(row[0])) if row else {}
