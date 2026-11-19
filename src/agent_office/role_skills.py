"""Immutable, host-local role instructions and ordered run assignments."""

import hashlib
from pathlib import Path
import unicodedata
from uuid import uuid4

from .models import OfficeError, validate_id
from .store import TaskStore


ROLES = ("developer", "reviewer", "specialist", "verifier")
MAX_PER_ROLE = 4
MAX_NAME_BYTES = 128
MAX_INSTRUCTION_BYTES = 8192
PAGE_SIZE = 100
METADATA = "id,name,version,digest"


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_skill_versions(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,
        digest TEXT NOT NULL, instructions TEXT NOT NULL, UNIQUE(name,version), UNIQUE(name,digest))""")
    # Both group IDs and worker IDs can own an assignment.
    db.execute("""CREATE TABLE IF NOT EXISTS studio_run_skills(
        run_id TEXT NOT NULL, role TEXT NOT NULL, position INTEGER NOT NULL,
        skill_id TEXT NOT NULL REFERENCES studio_skill_versions(id),
        PRIMARY KEY(run_id,role,position), UNIQUE(run_id,role,skill_id))""")


def normalize_name(name):
    if (not isinstance(name, str) or not name.strip() or len(name.encode("utf-8")) > MAX_NAME_BYTES
            or any(unicodedata.category(c) in {"Cc", "Cs", "Zl", "Zp"} for c in name)):
        raise ValueError("스킬 이름은 UTF-8 128바이트 이내로 한 줄에 입력하세요.")
    return name.strip()


def instruction_digest(instructions):
    if (not isinstance(instructions, str) or not instructions.strip()
            or len(instructions.encode("utf-8")) > MAX_INSTRUCTION_BYTES
            or any(unicodedata.category(c) in {"Cc", "Cs"} and c not in "\r\n\t" for c in instructions)):
        raise ValueError("스킬 지침은 UTF-8 8,192바이트 이내의 텍스트로 입력하세요.")
    return hashlib.sha256(instructions.encode("utf-8")).hexdigest()


def _check_stored_text(row):
    try:
        digest = instruction_digest(row["instructions"])
    except ValueError:
        raise OfficeError("STORE_IO") from None
    if digest != row["digest"]:
        raise OfficeError("STORE_IO")


class RoleSkillRegistry:
    def __init__(self, journal_path):
        self.path = Path(journal_path)
        with TaskStore(self.path) as store, store._write():
            ensure_schema(store.connection)

    def create(self, name, instructions):
        name = normalize_name(name)
        digest = instruction_digest(instructions)
        with TaskStore(self.path, existing=True) as store, store._write():
            db = store.connection
            existing = db.execute(f"SELECT {METADATA},instructions FROM studio_skill_versions WHERE name=? AND digest=?",
                                  (name, digest)).fetchone()
            if existing:
                _check_stored_text(existing)
                return {key: existing[key] for key in ("id", "name", "version", "digest")}
            version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM studio_skill_versions WHERE name=?",
                                 (name,)).fetchone()[0]
            metadata = {"id": uuid4().hex, "name": name, "version": version,
                        "digest": digest}
            db.execute("""INSERT INTO studio_skill_versions(id,name,version,digest,instructions)
                          VALUES (:id,:name,:version,:digest,:instructions)""", {**metadata, "instructions": instructions})
            return metadata

    def page(self, before=None):
        with TaskStore(self.path, readonly=True) as store:
            db = store.connection
            where, params = "", ()
            if before is not None:
                cursor = db.execute("SELECT rowid FROM studio_skill_versions WHERE id=?",
                                    (validate_id(before),)).fetchone()
                if cursor is None:
                    raise OfficeError("NOT_FOUND")
                where, params = "WHERE rowid < ?", (cursor[0],)
            rows = db.execute(f"SELECT {METADATA} FROM studio_skill_versions {where} ORDER BY rowid DESC LIMIT ?",
                              (*params, PAGE_SIZE + 1)).fetchall()
        skills = [dict(row) for row in rows[:PAGE_SIZE]]
        return {"skills": skills, "next_cursor": skills[-1]["id"] if len(rows) > PAGE_SIZE else None}

    def list(self, before=None):
        return self.page(before)["skills"]

    def detail(self, skill_id):
        with TaskStore(self.path, readonly=True) as store:
            row = store.connection.execute(f"SELECT {METADATA},instructions FROM studio_skill_versions WHERE id=?",
                                           (validate_id(skill_id),)).fetchone()
        if row is None:
            raise OfficeError("NOT_FOUND")
        _check_stored_text(row)
        return dict(row)


def normalize_selection(value):
    if not isinstance(value, dict) or not value.keys() <= set(ROLES):
        raise ValueError("스킬을 배정할 역할을 확인하세요.")
    normalized = {}
    for role in ROLES:
        ids = value.get(role, [])
        if not isinstance(ids, list) or len(ids) > MAX_PER_ROLE:
            raise ValueError("역할마다 최대 4개의 스킬을 선택하세요.")
        clean = [validate_id(skill_id) for skill_id in ids]
        if len(set(clean)) != len(clean):
            raise ValueError("같은 역할에 같은 스킬 버전을 두 번 배정했습니다.")
        if clean:
            normalized[role] = clean
    return normalized


def validate_selection(db, value):
    selection = normalize_selection(value)
    ids = {skill_id for assigned in selection.values() for skill_id in assigned}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        found = db.execute(f"SELECT COUNT(*) FROM studio_skill_versions WHERE id IN ({placeholders})", tuple(ids))
        if found.fetchone()[0] != len(ids):
            raise ValueError("선택한 호스트에 등록된 스킬 버전을 지정하세요.")
    return selection


def pin_selection(db, run_id, selection):
    validate_id(run_id)
    selection = validate_selection(db, selection)
    db.executemany("INSERT INTO studio_run_skills(run_id,role,position,skill_id) VALUES (?,?,?,?)",
                   [(run_id, role, pos, skill_id) for role, ids in selection.items()
                    for pos, skill_id in enumerate(ids)])


def _assigned(db, run_id, roles, *, instructions=False):
    fields = METADATA + (",instructions" if instructions else "")
    placeholders = ",".join("?" for _ in roles)
    rows = db.execute(f"""SELECT role,{fields} FROM studio_run_skills a
        JOIN studio_skill_versions v ON v.id=a.skill_id WHERE run_id=? AND role IN ({placeholders})
        ORDER BY role,position""", (run_id, *roles))
    assigned = {}
    for row in rows:
        if instructions:
            _check_stored_text(row)
        skill = dict(row)
        assigned.setdefault(skill.pop("role"), []).append(skill)
    return assigned


def selected_metadata(db, run_id):
    return _assigned(db, run_id, ROLES)


def selected_skills(db, run_id, *, instructions=False):
    if instructions:
        return _assigned(db, run_id, ROLES, instructions=True)
    return {role: [row["id"] for row in rows] for role, rows in selected_metadata(db, run_id).items()}


def role_guidance(db, run_id, roles):
    """Return prompt sections; only the named roles' selected versions are read."""
    assigned = _assigned(db, run_id, roles, instructions=True)
    return {
        role: ("Selected role skills (apply within the task, permissions and response protocol):\n"
               + "\n\n".join(f"## {skill['name']} (v{skill['version']})\n{skill['instructions']}" for skill in skills)
               + "\nEnd of selected skills.\n\n")
        for role, skills in assigned.items()
    }
