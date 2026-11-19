"""Chronological versions of the operator's selected stable source."""

from datetime import datetime, timezone
import json
from uuid import uuid4

from .models import MAX_SQLITE_INT, validate_id
from .release_validation import COMMIT_ID


def _create_tables(db):
    db.execute("""CREATE TABLE stable_versions (
        seq INTEGER PRIMARY KEY CHECK(seq >= 1), kind TEXT NOT NULL,
        previous TEXT, sha TEXT NOT NULL, time TEXT,
        candidate_id TEXT UNIQUE REFERENCES candidates(id),
        target_seq INTEGER REFERENCES stable_versions(seq),
        CHECK((kind='initial' AND seq=1 AND previous IS NULL AND candidate_id IS NULL AND target_seq IS NULL)
           OR (kind='promotion' AND seq>1 AND previous IS NOT NULL AND time IS NOT NULL
               AND candidate_id IS NOT NULL AND target_seq IS NULL)
           OR (kind='rollback' AND seq>1 AND previous IS NOT NULL AND time IS NOT NULL
               AND candidate_id IS NULL AND target_seq IS NOT NULL AND target_seq<seq)))""")
    db.execute("CREATE INDEX stable_sha ON stable_versions(sha,seq)")
    db.execute("ALTER TABLE candidates ADD COLUMN base_seq INTEGER REFERENCES stable_versions(seq)")


def _seal(db):
    # Reuse the published identity if a legacy store was already bound to Studio.
    db.execute("CREATE TABLE IF NOT EXISTS studio_release_identity "
               "(slot INTEGER PRIMARY KEY CHECK(slot=1), id TEXT NOT NULL)")
    db.execute("INSERT OR IGNORE INTO studio_release_identity VALUES(1,?)", (uuid4().hex,))
    identity(db)
    # An older executable only knows how to update release.stable. Reject that
    # write unless this transaction has already recorded the matching version.
    db.execute("""CREATE TRIGGER require_stable_version BEFORE UPDATE OF stable ON release
        WHEN NEW.stable IS NOT (SELECT sha FROM stable_versions ORDER BY seq DESC LIMIT 1)
        BEGIN SELECT RAISE(ABORT,'Stable source requires a version record'); END""")
    for operation in ("UPDATE", "DELETE"):
        db.execute(f"CREATE TRIGGER immutable_version_{operation} BEFORE {operation} ON stable_versions "
                   "BEGIN SELECT RAISE(ABORT,'Stable versions are append-only'); END")
    db.execute("PRAGMA user_version=1")


def initialize(db, sha):
    _create_tables(db)
    db.execute("INSERT INTO stable_versions(seq,kind,sha,time) VALUES(1,'initial',?,?)",
               (sha, datetime.now(timezone.utc).isoformat()))
    _seal(db)


def _invalid():
    return ValueError("Legacy release history is inconsistent. Inspect the operator release store.")


def migrate(db, git):
    """Replay only the decisions committed by the pre-versioned release store."""
    from .release_store import run_git

    initial = run_git(git, "rev-parse", "--verify", "refs/releases/initial^{commit}").strip().decode("ascii")
    _create_tables(db)
    db.execute("INSERT INTO stable_versions(seq,kind,sha) VALUES(1,'initial',?)", (initial,))
    current, seq = initial, 1
    rows = db.execute("""SELECT h.event,h.time,h.details,h.candidate,c.id,c.base,c.sha,c.image,c.state
        FROM history h LEFT JOIN candidates c ON c.id=h.candidate ORDER BY h.seq""")
    states, validating = {}, set()
    for row in rows:
        cid = validate_id(row["candidate"])
        event = row["event"]
        state = states.get(cid)
        if row["id"] is None or state in ("approved", "rejected") or (event != "submitted" and state is None):
            raise _invalid()
        details = json.loads(row["details"])
        if not isinstance(details, dict):
            raise _invalid()
        sha, base = row["sha"], row["base"]
        if event == "submitted":
            if (state is not None or base != current or not COMMIT_ID.fullmatch(sha)
                    or (details.get("base"), details.get("sha"), details.get("image")) != (base, sha, row["image"])):
                raise _invalid()
            db.execute("UPDATE candidates SET base_seq=? WHERE id=?", (seq, cid))
            states[cid] = "submitted"
        elif event == "validation_started":
            if state == "passed" or (details.get("sha"), details.get("image")) != (sha, row["image"]):
                raise _invalid()
            validating.add(cid)
        elif event == "validation_finished":
            if (cid not in validating or type(details.get("passed")) is not bool
                    or (details.get("sha"), details.get("image")) != (sha, row["image"])
                    or (details["passed"] and (type(details.get("exit_code")) is not int or details["exit_code"] != 0))):
                raise _invalid()
            validating.remove(cid)
            states[cid] = "passed" if details["passed"] else "failed"
        elif event in ("approved", "rejected"):
            if row["state"] != event or (details.get("base"), details.get("sha")) != (base, sha):
                raise _invalid()
            states[cid] = event
            if event == "approved":
                if state != "passed" or current != base or sha == current:
                    raise _invalid()
                ref = "refs/candidates/" + cid
                pinned = run_git(git, "rev-list", "--parents", "-n", "1", ref).decode("ascii").split()
                if pinned != [sha, base] or datetime.fromisoformat(row["time"]).tzinfo is None:
                    raise _invalid()
                seq += 1
                db.execute("INSERT INTO stable_versions VALUES(?,'promotion',?,?,?,?,NULL)",
                           (seq, current, sha, row["time"], cid))
                current = sha
        else:
            raise _invalid()
    pointer = db.execute("SELECT stable FROM release WHERE slot=1").fetchone()
    if pointer is None or pointer[0] != current or states != dict(db.execute("SELECT id,state FROM candidates")):
        raise _invalid()
    _seal(db)


def latest(db):
    return db.execute("SELECT * FROM stable_versions ORDER BY seq DESC LIMIT 1").fetchone()


def identity(db, expected=None):
    row = db.execute("SELECT id FROM studio_release_identity WHERE slot=1").fetchone()
    if row is None or (expected is not None and expected != row[0]):
        raise ValueError("Release identity changed. Review the selected release store again.")
    return validate_id(row[0])


def advance(db, kind, sha, *, when, candidate_id=None, target_seq=None):
    current = latest(db)
    seq = current["seq"] + 1
    db.execute("INSERT INTO stable_versions VALUES(?,?,?,?,?,?,?)",
               (seq, kind, current["sha"], sha, when, candidate_id, target_seq))
    changed = db.execute("UPDATE release SET stable=? WHERE slot=1 AND stable=?", (sha, current["sha"]))
    if changed.rowcount != 1:
        raise ValueError("Stable pointer disagrees with version history")
    return dict(latest(db))


def snapshot(root, *, release_id=None):
    from .release_store import _db
    with _db(root) as db:
        if release_id is not None:
            identity(db, release_id)
        versions = [dict(row) for row in db.execute("SELECT * FROM stable_versions ORDER BY seq")]
        receipts = db.execute("""SELECT h.candidate,h.details,h.time,c.base,c.base_seq,c.sha,c.state
            FROM history h LEFT JOIN candidates c ON c.id=h.candidate WHERE h.event='approved' ORDER BY h.seq""")
        previous = None
        for seq, version in enumerate(versions, 1):
            sha = version["sha"]
            if (version["seq"] != seq or version["previous"] != previous or not COMMIT_ID.fullmatch(sha)
                    or (version["kind"] == "initial") != (seq == 1)):
                raise ValueError("Broken stable version chain")
            if version["kind"] == "promotion":
                row = receipts.fetchone()
                if (row is None or row["state"] != "approved" or row["candidate"] != version["candidate_id"]
                        or row["base_seq"] != seq - 1 or previous == sha
                        or (row["base"], row["sha"], row["time"]) != (previous, sha, version["time"])
                        or json.loads(row["details"]) != {"base": previous, "sha": sha}):
                    raise ValueError("Stable promotion disagrees with its approval receipt")
            if version["kind"] == "rollback":
                target = version["target_seq"]
                if not 1 <= target < seq or versions[target - 1]["sha"] != sha or sha == previous:
                    raise ValueError("Invalid rollback target in stable history")
            previous = sha
        unrecorded = db.execute("SELECT 1 FROM candidates c WHERE state='approved' AND NOT EXISTS("
                                "SELECT 1 FROM stable_versions v WHERE v.candidate_id=c.id) LIMIT 1").fetchone()
        if not versions or receipts.fetchone() or unrecorded:
            raise ValueError("Approval history disagrees with recorded versions")
        return versions


def _number(seq):
    if type(seq) is not int or not 1 <= seq <= MAX_SQLITE_INT:
        raise ValueError("Expected a positive stable version number")
    return seq


def page(root, limit=24, before=None):
    from .release_store import _db
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("History page limit must be between 1 and 100")
    with _db(root) as db:
        if before is None:
            rows = db.execute("SELECT * FROM stable_versions ORDER BY seq DESC LIMIT ?", (limit + 1,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM stable_versions WHERE seq<? ORDER BY seq DESC LIMIT ?",
                              (_number(before), limit + 1)).fetchall()
        return {"current": dict(latest(db)), "items": [dict(row) for row in rows[:limit]],
                "next_before": rows[limit - 1]["seq"] if len(rows) > limit else None}


def version(db, seq):
    row = db.execute("SELECT * FROM stable_versions WHERE seq=?", (_number(seq),)).fetchone()
    if row is None:
        raise ValueError("Unknown stable version")
    return dict(row)


def _check_ref(db, git, version):
    from .release_store import run_git
    original = db.execute("SELECT kind,candidate_id FROM stable_versions WHERE sha=? "
                          "AND kind IN ('initial','promotion') ORDER BY seq LIMIT 1", (version["sha"],)).fetchone()
    if original is None:
        raise ValueError("Stable commit has no recorded origin")
    ref = ("refs/releases/initial" if original["kind"] == "initial"
           else "refs/candidates/" + validate_id(original["candidate_id"]))
    if run_git(git, "rev-parse", "--verify", ref + "^{commit}").strip().decode("ascii") != version["sha"]:
        raise ValueError("Pinned Git ref disagrees with stable version")


def _comparison(root, first, second):
    from .release_store import _diff, _paths
    return {"from": first, "to": second, "diff": _diff(_paths(root)[1], first["sha"], second["sha"], allow_binary=True)}


def compare(root, from_seq, to_seq):
    from .release_store import _db, _paths
    with _db(root) as db:
        first, second = version(db, from_seq), version(db, to_seq)
        for entry in (first, second):
            _check_ref(db, _paths(root)[1], entry)
    return _comparison(root, first, second)


def _check_target(current, target):
    if target["seq"] >= current["seq"] or target["sha"] == current["sha"]:
        raise ValueError("Rollback requires an earlier stable version with a different commit")


def review_rollback(root, target_seq):
    from .release_store import _db, _paths
    with _db(root) as db:
        current, target = dict(latest(db)), version(db, target_seq)
        release_id = identity(db)
        _check_target(current, target)
        for entry in (current, target):
            _check_ref(db, _paths(root)[1], entry)
    return _comparison(root, current, target) | {"release_id": release_id}


def rollback(root, *, expected_seq, expected_sha, target_seq, target_sha, expected_release_id=None,
             reviewed_versions=None):
    from .release_store import _db, _paths
    from .release_validation import committed_files
    with _db(root, write=True) as db:
        identity(db, expected_release_id)
        expected, target = version(db, expected_seq), version(db, target_seq)
        if reviewed_versions is not None and reviewed_versions != (expected, target):
            raise ValueError("Stable version changed during review")
        if (expected["sha"], target["sha"]) != (expected_sha, target_sha):
            raise ValueError("Reviewed SHA differs from the selected stable version")
        _check_target(expected, target)
        current = latest(db)
        if (current["kind"] == "rollback" and current["seq"] == expected_seq + 1
                and current["target_seq"] == target_seq and current["previous"] == expected_sha):
            return dict(current)
        if current["seq"] != expected_seq:
            raise ValueError("Stable version changed during review")
        git = _paths(root)[1]
        for entry in (expected, target):
            _check_ref(db, git, entry)
        committed_files(git, target_sha)
        return advance(db, "rollback", target_sha, when=datetime.now(timezone.utc).isoformat(), target_seq=target_seq)
