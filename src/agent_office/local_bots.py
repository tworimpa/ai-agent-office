"""Workspace-local role definitions and the immutable versions used by a run.

The hosting service validates workspace membership using its StudioCatalog.
Call mutating functions under the journal's write transaction.
"""
from hashlib import sha256
import json
import re

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
    return (isinstance(bot_id, str) and 1 <= len(bot_id) <= 64
            and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", bot_id) is not None)


def _definition(bot_id, name, role, instructions, *, builtin=False):
    if not _valid_id(bot_id):
        raise ValueError("봇 ID는 영문 소문자와 숫자를 사용하고 단어 사이에 하이픈을 넣어 64자 이내로 입력하세요.")
    if role not in ROLES:
        raise ValueError("봇 역할은 coordinator, developer, reviewer 중 하나를 선택하세요.")
    if not _single_line(name) or len(name.strip()) > 40:
        raise ValueError("봇 이름은 40자 이내의 한 줄로 입력하세요.")
    if builtin:
        if _BUILTINS.get(bot_id) != role or name != _NAMES[role] or instructions != "":
            raise ValueError("기본 봇 정의를 확인하세요.")
    else:
        if bot_id in _BUILTINS:
            raise ValueError("기본 봇에 예약된 ID입니다.")
        instruction_digest(instructions)
    document = {"schema_version": 1, "id": bot_id, "name": name.strip(), "role": role,
                "instructions": instructions, "builtin": builtin}
    return {**document, "digest": sha256(encode(document).encode("utf-8")).hexdigest()}


def _builtin(role):
    return _definition(f"office-{role}", _NAMES[role], role, "", builtin=True)


def _checked(document, *, bot_id=None):
    """Verify stored content, including the expected ID when it comes from a catalog row."""
    try:
        rebuilt = _definition(document["id"], document["name"], document["role"], document["instructions"],
                              builtin=document["builtin"])
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


def assign(db, workspace_id, role, bot_id):
    definition = get(db, workspace_id, bot_id)
    if role != definition["role"]:
        raise ValueError("선택한 봇이 담당하는 역할을 확인하세요.")
    if definition["builtin"]:
        db.execute("DELETE FROM studio_role_bot_bindings WHERE workspace_id=? AND role=?", (workspace_id, role))
    else:
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
        if definition["role"] != role:
            raise OfficeError("STORE_IO")
        selected[role] = definition
    return selected


def public_receipt(bots):
    return {role: {key: bot[key] for key in _PUBLIC} for role, bot in bots.items()}


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
