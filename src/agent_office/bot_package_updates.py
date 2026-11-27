"""Version changes for installed Git bot packages and their local rollback slot."""
from dataclasses import dataclass
from hashlib import sha256
import json
import re

from . import bot_package, bot_package_merge, bot_spec, git_bot_source, local_bots
from .models import OfficeError
from .store import TaskStore, encode


_TABLE = "studio_bot_package_versions"
_SCHEMA = """CREATE TABLE IF NOT EXISTS studio_bot_package_versions(
    workspace_id TEXT NOT NULL, bot_id TEXT NOT NULL, version_state TEXT NOT NULL,
    PRIMARY KEY(workspace_id, bot_id))"""


def _exists(db):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,),
    ).fetchone() is not None


def record_initial(db, workspace, bot_id, definition):
    """Capture the received package, even when an older install acquires a baseline on retry."""
    db.execute(_SCHEMA)
    db.execute("""INSERT INTO studio_bot_package_versions VALUES(?,?,?)
                  ON CONFLICT(workspace_id, bot_id) DO NOTHING""",
               (workspace, bot_id, encode({"baseline": definition, "generation": 0})))


def rollback_sources(db, workspace):
    if not _exists(db):
        return {}
    return {
        bot_id: json.loads(source)
        for bot_id, source in db.execute(
            """SELECT bot_id, json_extract(version_state, '$.backup.source')
               FROM studio_bot_package_versions
               WHERE workspace_id=? AND json_type(version_state, '$.backup.source')='object'""",
            (workspace,),
        )
    }


@dataclass(frozen=True)
class Installed:
    source: dict
    working: dict
    shared: dict
    shared_digest: str
    version: dict

    @property
    def backup(self):
        return self.version.get("backup")

    def stamp(self):
        return [self.source, self.shared_digest, self.version.get("generation", 0)]

    def retry(self, action, request):
        if self.version.get("request") == [action, request]:
            return self.version["receipt"]
        return None


def _read(db, workspace, bot_id):
    source = bot_package.installed_sources(db, workspace).get(bot_id)
    if source is None:
        raise OfficeError("NOT_FOUND")
    working = local_bots.manifest(db, workspace, bot_id)
    shared = bot_package.without_memory(working)
    version = {}
    if _exists(db):
        row = db.execute("""SELECT version_state FROM studio_bot_package_versions
                            WHERE workspace_id=? AND bot_id=?""", (workspace, bot_id)).fetchone()
        if row is not None:
            version = json.loads(row[0])
    return Installed(source, working, shared, local_bots.shareable_digest(shared), version)


def _from_git(source, version):
    request = git_bot_source.Source.parse({"url": source["url"], "path": source["path"], "version": version})
    fetched = git_bot_source.fetch(request)
    definition = fetched.package.definition
    return request.receipt(fetched.commit, local_bots.shareable_digest(definition)), definition


def _baseline(installed):
    if baseline := installed.version.get("baseline"):
        if local_bots.shareable_digest(baseline) != installed.source["digest"]:
            raise OfficeError("STORE_IO")
        return baseline
    if installed.shared_digest == installed.source["digest"]:
        return installed.shared
    try:
        source, definition = _from_git(installed.source, installed.source["commit"])
    except ValueError:
        raise ValueError("기존 배포본의 고정 커밋을 확인할 수 없습니다.") from None
    if any(source[key] != installed.source[key] for key in ("commit", "digest")):
        raise ValueError("기존 배포본의 커밋과 정의 해시가 설치 기록과 일치하지 않습니다.")
    return definition


def _restore_memory(definition, working):
    files = {**definition["files"], **{
        path: text for path, text in working["files"].items() if bot_package.is_memory_path(path)
    }}
    folders = [*definition["folders"], *(
        path for path in working["folders"] if bot_package.is_memory_path(path)
    )]
    conflicts = bot_package_merge.path_conflicts(files, folders)
    if conflicts:
        return None, conflicts
    return bot_spec.validate({**definition, "files": files, "folders": folders}), []


def _preview(workspace, bot_id, action, installed, candidate, comparison, unchanged=False):
    conflicts = comparison["conflicts"]
    if comparison["definition"] is not None:
        _, conflicts = _restore_memory(comparison["definition"], installed.working)
    token = None if conflicts or unchanged else sha256(
        encode([workspace, bot_id, action, installed.stamp(), candidate]).encode("utf-8")
    ).hexdigest()
    return {"current": installed.source, "target": candidate, "changes": comparison["changes"],
            "conflicts": conflicts, "token": token, "unchanged": unchanged}


def change(db_path, workspace, bot_id, action, data, *, preview):
    update = action == "update"
    required = ({"version"} if update else set()) | (set() if preview else {"token"})
    if data.keys() != required:
        raise ValueError("버전 입력과 변경 미리보기 내용을 확인하세요.")
    token = data.get("token")
    if not preview and (not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token)):
        raise ValueError("먼저 적용할 버전과 변경 내용을 확인하세요.")

    with TaskStore(db_path, existing=True, readonly=True) as store:
        store.connection.execute("BEGIN")
        installed = _read(store.connection, workspace, bot_id)
    if not preview and (replayed := installed.retry(action, data)) is not None:
        return replayed

    if update:
        target, baseline_after = _from_git(installed.source, data["version"])
        if baseline_after["id"] != bot_id:
            raise ValueError("설치한 봇과 패키지의 봇 ID가 일치해야 합니다.")
        if target["commit"] == installed.source["commit"]:
            if target["digest"] != installed.source["digest"]:
                raise ValueError("같은 커밋의 정의 해시가 설치 기록과 일치하지 않습니다.")
            if not preview:
                raise OfficeError("REQUEST_CONFLICT")
            return _preview(workspace, bot_id, action, installed, installed.source,
                            {"definition": None, "changes": [], "conflicts": []}, unchanged=True)
        baseline_before = _baseline(installed)
        comparison = bot_package_merge.compare(baseline_before, installed.shared, baseline_after)
        backup = {
            "source": installed.source, "baseline": baseline_before,
            "working_before": installed.shared, "working_after": comparison["definition"],
        }
    else:
        backup = installed.backup
        if backup is None:
            raise OfficeError("NOT_FOUND")
        target, baseline_after = backup["source"], backup["baseline"]
        if baseline_after["id"] != bot_id or local_bots.shareable_digest(baseline_after) != target["digest"]:
            raise OfficeError("STORE_IO")
        comparison = bot_package_merge.compare(backup["working_after"], installed.shared, backup["working_before"])
        backup = None

    result = _preview(workspace, bot_id, action, installed, target, comparison)
    if preview:
        return result
    if token != result["token"]:
        raise OfficeError("REQUEST_CONFLICT")

    with TaskStore(db_path, existing=True) as store, store._write():
        db = store.connection
        current = _read(db, workspace, bot_id)
        if (replayed := current.retry(action, data)) is not None:
            return replayed
        if current.stamp() != installed.stamp():
            raise OfficeError("REQUEST_CONFLICT")
        document, conflicts = _restore_memory(comparison["definition"], current.working)
        if conflicts:
            raise OfficeError("REQUEST_CONFLICT")
        local_bots.replace_manifest(db, workspace, bot_id, document, local_bots.shareable_digest(current.working))
        bot_package.replace_source(db, workspace, bot_id, target)
        rollback = backup["source"] if backup is not None else None
        receipt = {"id": bot_id, "source": target, "rollback": rollback}
        version = {"baseline": baseline_after, "generation": current.version.get("generation", 0) + 1,
                   "backup": backup, "request": [action, data], "receipt": receipt}
        db.execute(_SCHEMA)
        db.execute("""INSERT INTO studio_bot_package_versions VALUES(?,?,?)
                      ON CONFLICT(workspace_id, bot_id) DO UPDATE SET version_state=excluded.version_state""",
                   (workspace, bot_id, encode(version)))
        return receipt
