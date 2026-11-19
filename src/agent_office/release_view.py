"""Publish a journal-scoped, read-only Studio view from the operator environment."""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from uuid import uuid4

from . import release_store
from .improvement_proposal import verify_history
from .models import MAX_SQLITE_INT, OfficeError, validate_db_file, validate_id
from .ownership import RunGuard
from .store import TaskStore, now

PAGE = 24
HISTORY = 32
APP_ID = 0x414F5631  # AOV1
_SCHEMA = """
CREATE TABLE publication(slot INTEGER PRIMARY KEY CHECK(slot=1), journal TEXT NOT NULL,
                         release_id TEXT NOT NULL, stable TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE jobs(id TEXT PRIMARY KEY, digest TEXT, candidate TEXT, error TEXT, retry_after REAL);
CREATE INDEX job_candidate ON jobs(candidate);
CREATE TABLE candidates(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, last_event TEXT NOT NULL,
                        running INTEGER NOT NULL, summary TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX running_candidates ON candidates(running);
"""


def _open(path, mode):
    db = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA trusted_schema=OFF")
    return db


def _metadata(db):
    if db.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
        raise ValueError("Choose a new view filename or an existing Agent Office publication")
    row = db.execute("SELECT * FROM publication WHERE slot=1").fetchone()
    if row is None:
        raise ValueError("Incomplete Studio publication")
    return row


@contextmanager
def _reader(path, journal_id):
    if path is None:
        raise _Unavailable
    try:
        path = Path(path).expanduser().resolve()
        path.stat()
        with closing(_open(path, "ro")) as db:
            db.execute("BEGIN")
            meta = _metadata(db)
            if meta["journal"] != validate_id(journal_id):
                raise _Unavailable
            yield db, meta
    except FileNotFoundError:
        raise _Unavailable from None
    except (ValueError, sqlite3.Error, OSError):
        raise OfficeError("STORE_IO") from None


class _Unavailable(OfficeError):
    def __init__(self):
        super().__init__("STORE_IO")


def view_status(path, journal_id):
    try:
        with _reader(path, journal_id) as (_, meta):
            return {"available": True, "stable": meta["stable"], "updated_at": meta["updated_at"]}
    except _Unavailable:
        return {"available": False, "stable": None, "updated_at": None}


def view_candidates(path, journal_id, offset=0):
    if type(offset) is not int or not 0 <= offset <= MAX_SQLITE_INT:
        raise ValueError("Invalid candidate offset")
    if path is None:
        return {"items": [], "next_offset": None}
    with _reader(path, journal_id) as (db, _):
        rows = db.execute("SELECT summary FROM candidates ORDER BY rowid DESC LIMIT ? OFFSET ?",
                          (PAGE + 1, offset)).fetchall()
        return {"items": [json.loads(row[0]) for row in rows[:PAGE]],
                "next_offset": offset + PAGE if len(rows) > PAGE else None}


def view_candidate(path, journal_id, id):
    validate_id(id)
    if path is not None:
        with _reader(path, journal_id) as (db, _):
            row = db.execute("SELECT summary,detail FROM candidates WHERE id=?", (id,)).fetchone()
            if row:
                return json.loads(row["summary"]) | json.loads(row["detail"])
    raise OfficeError("NOT_FOUND")


def view_job(path, journal_id, id):
    validate_id(id)
    if path is not None:
        with _reader(path, journal_id) as (db, _):
            row = db.execute("SELECT c.id AS candidate_id,CASE WHEN c.id IS NULL THEN j.error END AS error "
                             "FROM jobs j LEFT JOIN candidates c "
                             "ON c.id=j.candidate WHERE j.id=?", (id,)).fetchone()
            if row:
                return dict(row)
    return {"candidate_id": None, "error": None}


def _view_path(name, journal, root):
    supplied = Path(name).expanduser()
    path = supplied.resolve()
    if (supplied.is_symlink() or (path.exists() and not path.is_file())
            or path.is_relative_to(root) or path == journal
            or path.name.endswith(("-journal", "-wal", "-shm"))
            or path.is_relative_to(journal.with_name(journal.name + ".locks"))
            or path == journal.with_name(journal.name + ".studio.sqlite3")
            or any((parent / ".git").exists() or parent.name.lower() == ".git"
                   or ((parent / "HEAD").is_file() and (parent / "objects").is_dir())
                   for parent in path.parents)):
        raise ValueError("Choose a separate Studio view file outside Git, the release store and journal files")
    validate_db_file(path)
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.is_symlink() or (not path.exists() and sidecar.exists()):
            raise ValueError("Unexpected publication sidecar. Choose a different view filename")
        validate_db_file(sidecar)
    return path


def _journal_id(journal):
    with TaskStore(journal, readonly=True) as source:
        try:
            row = source.connection.execute("SELECT id FROM studio_identity WHERE singleton=1").fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            row = None
        if row is None:
            raise ValueError("Start Studio with this journal once to initialize its journal identity")
        return validate_id(row[0])


def _release_identity(root):
    with release_store._db(root) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_release_identity' AND type='table'").fetchone():
            row = db.execute("SELECT i.id,r.stable FROM studio_release_identity i CROSS JOIN release r "
                             "WHERE i.slot=1 AND r.slot=1").fetchone()
            return row["id"], row["stable"]
    with release_store._db(root, write=True) as db:
        stable = db.execute("SELECT stable FROM release WHERE slot=1").fetchone()[0]
        db.execute("CREATE TABLE IF NOT EXISTS studio_release_identity "
                   "(slot INTEGER PRIMARY KEY CHECK(slot=1), id TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO studio_release_identity VALUES(1,?)", (uuid4().hex,))
        return db.execute("SELECT id FROM studio_release_identity WHERE slot=1").fetchone()[0], stable


def _initialize(path, journal_id, release_id, stable):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=path.parent, prefix=".office-view-") as scratch:
            staged = Path(scratch) / "view.db"
            with closing(sqlite3.connect(staged)) as db, db:
                db.executescript(_SCHEMA)
                db.execute(f"PRAGMA application_id={APP_ID}")
                db.execute("INSERT INTO publication VALUES(1,?,?,?,?)",
                           (journal_id, release_id, stable, now()))
            # Publish without replacing a concurrently created or unrelated file.
            try:
                if os.name == "nt":
                    staged.rename(path)
                else:
                    os.link(staged, path)
            except FileExistsError:
                pass
    with closing(_open(path, "ro")) as db:
        meta = _metadata(db)
        if (meta["journal"], meta["release_id"]) != (journal_id, release_id):
            raise ValueError("This view belongs to another release or journal. Choose a new view filename")


def _bounded(value, maximum):
    raw = value.encode("utf-8")
    if len(raw) <= maximum:
        return value, False
    marker = "\n… [truncated]"
    return raw[:maximum - len(marker.encode())].decode("utf-8", "ignore") + marker, True


def _import_jobs(root, journal, view):
    from .studio_improvements import ready_artifact, ready_job_receipts

    view.execute("CREATE TEMP TABLE ready(id TEXT PRIMARY KEY)")
    for job_id, claimed in ready_job_receipts(journal):
        digest = claimed if isinstance(claimed, str) and re.fullmatch("[0-9a-f]{64}", claimed) else None
        with view:
            view.execute("INSERT INTO ready VALUES(?)", (job_id,))
        prior = view.execute("SELECT digest,retry_after FROM jobs WHERE id=?", (job_id,)).fetchone()
        if prior and prior["digest"] == digest and (prior["retry_after"] is None or time.time() < prior["retry_after"]):
            continue
        candidate_id, error, retry = None, None, None
        try:
            proposal = ready_artifact(journal, job_id)
            candidate_id = release_store.submit(root, proposal)["id"]
        except (OSError, sqlite3.OperationalError):
            retry = time.time() + 30
            error = "Temporary journal or release storage error. Publication will retry."
        except (ValueError, OfficeError) as exc:
            reason = str(exc)
            if any(text in reason.lower() for text in (
                    "timed out", "no space left", "disk full", "permission denied", ".lock", "git is required")):
                retry = time.time() + 30
                error = "Git is temporarily unavailable. Check Git and release storage. Publication will retry."
            elif reason == "Proposal base differs from current stable source":
                error = reason
            else:
                error = "Proposal admission failed. Check the generation receipt, run evidence and patch."
        with view:
            view.execute("INSERT INTO jobs VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                         "digest=excluded.digest,candidate=excluded.candidate,error=excluded.error,"
                         "retry_after=excluded.retry_after", (job_id, digest, candidate_id, error, retry))
    with view:
        view.execute("DELETE FROM jobs WHERE NOT EXISTS(SELECT 1 FROM ready WHERE ready.id=jobs.id)")


def _projection(db, record, proposal, job_id, cached, root):
    cid = record["id"]
    latest = db.execute("SELECT event,time FROM history WHERE candidate=? ORDER BY seq DESC LIMIT ?",
                        (cid, HISTORY)).fetchall()
    report = db.execute("SELECT event,details FROM history WHERE candidate=? AND "
                        "event IN ('validation_started','validation_finished') ORDER BY seq DESC LIMIT 1",
                        (cid,)).fetchone()
    validation, output, code, output_truncated = None, None, None, False
    issue = None
    if report and report["event"] == "validation_finished":
        details = json.loads(report["details"])
        validation = "passed" if details["passed"] else "failed"
        output, output_truncated = _bounded(details["output"], 16 * 1024)
        code = details["exit_code"]
    elif report:
        if RunGuard(root / "release.sqlite3", cid).is_held():
            validation = "running"
        else:
            issue = "Validation was interrupted. Inspect the candidate and run improve verify " + cid
            output = issue
    elif job_id and record["state"] == "submitted" and record["base"] != record["stable"]:
        issue = "Candidate base differs from current stable source"
        output = issue
    if cached:
        detail = json.loads(cached["detail"])
        diff, diff_truncated = detail["diff"], detail["diff_truncated"]
    else:
        diff, diff_truncated = _bounded(
            release_store._diff(root / "objects.git", record["base"], record["sha"]), 48 * 1024)
    summary = {"id": cid, "job_id": job_id, "sha": record["sha"], "base": record["base"],
               "state": record["state"], "title": proposal["title"], "validation": validation,
               "decision": record["state"] if record["state"] in ("approved", "rejected") else None}
    detail = {"diff": diff, "diff_truncated": diff_truncated, "expected": proposal["expected"],
              "rationale": proposal["rationale"], "image": record["image"],
              "run_ids": [row["run_id"] for row in proposal["history"]],
              "history": [dict(row) for row in reversed(latest)],
              "history_omitted": record["entries"] - len(latest), "exit_code": code,
              "validation_output": output, "output_truncated": output_truncated}
    return summary, detail, issue


def _publish_one(root, journal, view, cid, *, journal_changed=True):
    with release_store._db(root) as db:
        record = db.execute(
            "SELECT c.*,r.stable,(SELECT count(*) FROM history WHERE candidate=c.id) AS entries, "
            "(SELECT time FROM history WHERE candidate=c.id ORDER BY seq DESC LIMIT 1) AS last_event "
            "FROM candidates c CROSS JOIN release r WHERE c.id=? AND r.slot=1", (cid,)).fetchone()
        if record is None:
            with view:
                view.execute("DELETE FROM candidates WHERE id=?", (cid,))
            return False
        cached = view.execute("SELECT fingerprint,last_event,running,summary FROM candidates WHERE id=?",
                              (cid,)).fetchone()
        proposal = json.loads(record["proposal"])
        try:
            if journal_changed or not cached or cached["fingerprint"] != record["fingerprint"]:
                verify_history(journal, proposal)
        except (ValueError, OfficeError) as exc:
            # A release store may also contain valid imports from other journals.
            with view:
                view.execute("DELETE FROM candidates WHERE id=?", (cid,))
                view.execute("UPDATE jobs SET error=? WHERE candidate=?", (_bounded(str(exc), 1024)[0], cid))
            return False
        job = view.execute("SELECT id FROM jobs WHERE candidate=? ORDER BY rowid LIMIT 1", (cid,)).fetchone()
        job_id = job[0] if job else None
        if cached and cached["fingerprint"] != record["fingerprint"]:
            cached = None
        if cached and cached["last_event"] == record["last_event"] and not cached["running"]:
            previous = json.loads(cached["summary"])
            if previous["job_id"] == job_id and previous["state"] == record["state"]:
                if not job_id or record["state"] != "submitted":
                    return False
        detail_cache = view.execute("SELECT detail FROM candidates WHERE id=?", (cid,)).fetchone() if cached else None
        summary, detail, issue = _projection(db, record, proposal, job_id, detail_cache, root)
    with view:
        view.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                     "fingerprint=excluded.fingerprint,last_event=excluded.last_event,running=excluded.running,"
                     "summary=excluded.summary,detail=excluded.detail",
                     (cid, record["fingerprint"], record["last_event"], summary["validation"] == "running",
                      json.dumps(summary, ensure_ascii=False), json.dumps(detail, ensure_ascii=False)))
        view.execute("UPDATE jobs SET error=? WHERE candidate=?", (issue, cid))
        view.execute("UPDATE publication SET stable=?,updated_at=? WHERE slot=1", (record["stable"], now()))
    return bool(job and record["state"] == "submitted" and record["base"] == record["stable"]
                and summary["validation"] is None and issue is None)


class PublicationWatch:
    """Compare SQLite data_version only across polls on the same open connection."""

    def __init__(self):
        self.sources = {}
        self.view_file = None
        self.snapshot_dir = None

    def changed(self, path):
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        previous = self.sources.get(path)
        if previous and previous[0] == identity:
            db = previous[1]
        else:
            if previous:
                previous[1].close()
            db = _open(path, "ro")
        version = (db.execute("PRAGMA data_version").fetchone()[0], stat.st_mtime_ns, stat.st_size)
        self.sources[path] = (identity, db, version)
        return previous is None or previous[0] != identity or previous[2] != version

    def snapshot(self, journal):
        changed = self.changed(journal)
        if self.snapshot_dir is None:
            self.snapshot_dir = tempfile.TemporaryDirectory(prefix="office-sync-")
        path = Path(self.snapshot_dir.name) / "journal.sqlite3"
        if changed or not path.exists():
            # One opened source supplies identity, jobs, receipts and run evidence.
            # The pathname can be replaced by a transferred backup during a pass.
            with closing(sqlite3.connect(path)) as target:
                self.sources[journal][1].backup(target)
        return path, changed

    def close(self):
        for _, db, _ in self.sources.values():
            db.close()
        self.sources.clear()
        self.view_file = None
        if self.snapshot_dir is not None:
            self.snapshot_dir.cleanup()
            self.snapshot_dir = None


def _refresh(root, journal, view, journal_changed):
    _import_jobs(root, journal, view)
    view.execute("CREATE TEMP TABLE present(id TEXT PRIMARY KEY)")
    cursor = 0
    with release_store._db(root) as source:
        last = source.execute("SELECT coalesce(max(rowid),0) FROM candidates").fetchone()[0]
    while cursor < last:
        with release_store._db(root) as source:
            rows = source.execute("SELECT rowid,id FROM candidates WHERE rowid>? AND rowid<=? "
                                  "ORDER BY rowid LIMIT ?", (cursor, last, PAGE)).fetchall()
        if not rows:
            break
        with view:
            view.executemany("INSERT INTO present VALUES(?)", [(row["id"],) for row in rows])
        for cursor, cid in rows:
            if _publish_one(root, journal, view, cid, journal_changed=journal_changed):
                def publish_started():
                    try:
                        _publish_one(root, journal, view, cid, journal_changed=False)
                    except sqlite3.OperationalError:
                        # The release journal durably started this attempt. A busy view
                        # must not interrupt validation between that event and its result.
                        pass
                try:
                    release_store.verify_candidate(root, cid, first_attempt_only=True,
                                                   on_started=publish_started)
                except ValueError as exc:
                    if str(exc) not in {"Another operation is active for this candidate",
                                        "Candidate is terminal or its base is stale"}:
                        raise
                    if str(exc) == "Another operation is active for this candidate":
                        with view:
                            view.execute("UPDATE jobs SET retry_after=? WHERE candidate=?", (time.time() + 5, cid))
                _publish_one(root, journal, view, cid, journal_changed=False)
    with view:
        view.execute("DELETE FROM candidates WHERE NOT EXISTS(SELECT 1 FROM present WHERE present.id=candidates.id)")
        view.execute("UPDATE jobs SET error='Candidate missing from release. Inspect the restored release store.' "
                     "WHERE candidate IS NOT NULL AND NOT EXISTS(SELECT 1 FROM present WHERE present.id=jobs.candidate)")


def sync_once(journal, root, path, *, watch=None):
    if watch is None:
        with closing(PublicationWatch()) as once:
            return sync_once(journal, root, path, watch=once)
    journal = Path(journal).expanduser().resolve(strict=True)
    root = Path(root).expanduser().resolve(strict=True)
    path = _view_path(path, journal, root)
    guard = RunGuard(root / "release.sqlite3", "0" * 32)
    guard.path = guard.path.with_name("studio-publication.lock")
    release_id, stable = _release_identity(root)
    with guard:
        journal, journal_changed = watch.snapshot(journal)
        journal_id = _journal_id(journal)
        _initialize(path, journal_id, release_id, stable)
        with closing(_open(path, "rw")) as view:
            release_changed = watch.changed(root / "release.sqlite3")
            file = path.stat()
            view_file = (file.st_dev, file.st_ino)
            if watch.view_file != view_file:
                journal_changed = True
                watch.view_file = view_file
            retry = view.execute("SELECT 1 FROM jobs WHERE retry_after<=? LIMIT 1", (time.time(),)).fetchone()
            if journal_changed or release_changed or retry:
                _refresh(root, journal, view, journal_changed)
            else:
                # The OS lock can disappear without a new release event if a validator dies.
                cursor = 0
                while rows := view.execute("SELECT rowid,id FROM candidates WHERE running=1 AND rowid>? "
                                           "ORDER BY rowid LIMIT ?", (cursor, PAGE)).fetchall():
                    for cursor, cid in rows:
                        _publish_one(root, journal, view, cid, journal_changed=False)
            stable = release_store.release_status(root)["stable"]
            updated = now()
            with view:
                view.execute("UPDATE publication SET stable=?,updated_at=? WHERE slot=1", (stable, updated))
            return {"stable": stable, "updated_at": updated,
                    "candidates": view.execute("SELECT count(*) FROM candidates").fetchone()[0],
                    "job_errors": view.execute("SELECT count(*) FROM jobs WHERE error IS NOT NULL").fetchone()[0]}
