"""Operator-published stable source history and read-only comparisons in the Studio view."""

from __future__ import annotations

import difflib
import io

from . import release_store
from .models import MAX_SQLITE_INT, OfficeError
from .release_view import PAGE, _bounded, _reader, _Unavailable

MAX_FILES = 100
MAX_DIFF = 64 * 1024
MAX_BLOB = 64 * 1024
# SequenceMatcher can take quadratic time on repetitive text. Budget all pairs
# in a response together. Pure insertions and deletions need only linear work.
MAX_LINE_PAIRS = 2_000_000
_FIELDS = "seq,kind,previous,sha,time,target_seq"
_SCHEMA = (
    "CREATE TABLE stable_publication(slot INTEGER PRIMARY KEY CHECK(slot=1), latest INTEGER NOT NULL, "
    "updated_at TEXT NOT NULL)",
    "CREATE TABLE stable_events(seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, previous TEXT, "
    "sha TEXT NOT NULL, time TEXT, target_seq INTEGER)",
    "CREATE TABLE stable_revisions(path TEXT NOT NULL, seq INTEGER NOT NULL, mode TEXT, oid TEXT, "
    "PRIMARY KEY(path,seq)) WITHOUT ROWID",
    "CREATE INDEX stable_changes ON stable_revisions(seq,path)",
    "CREATE TABLE stable_blobs(oid TEXT PRIMARY KEY, content TEXT, omission TEXT) WITHOUT ROWID",
)


def _has_schema(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stable_publication'").fetchone()


def revoke(db):
    if _has_schema(db) and db.execute("SELECT 1 FROM stable_publication").fetchone():
        db.execute("PRAGMA secure_delete=ON")
        with db:
            for table in ("stable_publication", "stable_events", "stable_revisions", "stable_blobs"):
                db.execute(f"DELETE FROM {table}")


def _store_blobs(db, git, oids):
    missing = [oid.encode("ascii") for oid in sorted(oids) if not db.execute(
        "SELECT 1 FROM stable_blobs WHERE oid=?", (oid,)).fetchone()]
    # Limit each Git response to 32 bounded blobs. Oversized source is never read.
    for offset in range(0, len(missing), 32):
        batch = missing[offset:offset + 32]
        info = release_store.run_git(git, "cat-file", "--batch-check", data=b"\n".join(batch) + b"\n")
        small = []
        for oid, line in zip(batch, info.splitlines(), strict=True):
            name, kind, size = line.split()
            if name != oid or kind != b"blob":
                raise ValueError("Unexpected stable source object")
            if int(size) <= MAX_BLOB:
                small.append((oid, int(size)))
            else:
                db.execute("INSERT INTO stable_blobs VALUES(?,NULL,'oversized')", (oid.decode("ascii"),))
        if not small:
            continue
        request = b"".join(oid + b"\n" for oid, _ in small)
        data = io.BytesIO(release_store.run_git(git, "cat-file", "--batch", data=request))
        for oid, size in small:
            if data.readline().split() != [oid, b"blob", str(size).encode()]:
                raise ValueError("Unexpected stable source blob")
            raw = data.read(size)
            if len(raw) != size or data.read(1) != b"\n":
                raise ValueError("Incomplete stable source blob")
            try:
                content = raw.decode("utf-8") if b"\0" not in raw else None
            except UnicodeDecodeError:
                content = None
            omission = "binary or non-UTF-8" if content is None else None
            db.execute("INSERT INTO stable_blobs VALUES(?,?,?)", (oid.decode("ascii"), content, omission))


def _index_transition(db, git, event):
    raw = release_store.run_git(git, "diff-tree", "-r", "--raw", "-z", "--no-commit-id", "--no-abbrev",
                                "--no-renames", "--no-ext-diff", "--no-textconv",
                                event["previous"], event["sha"], "--")
    tokens = raw.split(b"\0")
    if tokens.pop() or len(tokens) % 2:
        raise ValueError("Incomplete stable tree changes")
    blobs = set()
    for header, raw_path in zip(tokens[::2], tokens[1::2]):
        old_mode, new_mode, old_oid, new_oid, status = header.decode("ascii").removeprefix(":").split()
        if status not in {"A", "D", "M"} or any(
                mode not in {"000000", "100644", "100755"} for mode in (old_mode, new_mode)):
            raise ValueError("Unsupported stable tree change")
        path = raw_path.decode("utf-8")
        old = (old_mode, old_oid) if old_mode != "000000" else (None, None)
        new = (new_mode, new_oid) if new_mode != "000000" else (None, None)
        prior = db.execute("SELECT mode,oid FROM stable_revisions WHERE path=? ORDER BY seq DESC LIMIT 1",
                           (path,)).fetchone()
        if prior is None and old[0] is not None:
            # This path had this baseline state up to its first stable change.
            db.execute("INSERT INTO stable_revisions VALUES(?,1,?,?)", (path, *old))
        if prior is not None and tuple(prior) != old:
            raise ValueError("Stable file history disagrees with Git")
        db.execute("INSERT INTO stable_revisions VALUES(?,?,?,?)", (path, event["seq"], *new))
        if old[1] != new[1]:
            blobs.update(oid for _, oid in (old, new) if oid is not None)
    _store_blobs(db, git, blobs)


def publish(db, root, updated, *, rebuild=False):
    """Append projections and switch both stable pointers in one SQLite transaction."""
    from .stable_history import snapshot

    release_id = db.execute("SELECT release_id FROM publication WHERE slot=1").fetchone()[0]
    events = [{field: entry[field] for field in _FIELDS.split(",")}
              for entry in snapshot(root, release_id=release_id)]
    with db:
        db.execute("PRAGMA secure_delete=ON")
        db.execute("BEGIN IMMEDIATE")
        if not _has_schema(db):
            for statement in _SCHEMA:
                db.execute(statement)
        stored = [dict(row) for row in db.execute(f"SELECT {_FIELDS} FROM stable_events ORDER BY seq")]
        if rebuild or stored != events[:len(stored)]:
            # An operator may have restored an older release backup. Rebuild exactly its history.
            for table in ("stable_events", "stable_revisions", "stable_blobs"):
                db.execute(f"DELETE FROM {table}")
            stored = []
        for event in events[len(stored):]:
            if event["seq"] != 1:
                _index_transition(db, root / "objects.git", event)
            db.execute(f"INSERT INTO stable_events({_FIELDS}) VALUES(:seq,:kind,:previous,:sha,:time,:target_seq)",
                       event)
        latest = events[-1]
        db.execute("INSERT INTO stable_publication VALUES(1,?,?) ON CONFLICT(slot) DO UPDATE SET "
                   "latest=excluded.latest,updated_at=excluded.updated_at", (latest["seq"], updated))
        db.execute("UPDATE publication SET stable=?,updated_at=? WHERE slot=1", (latest["sha"], updated))
    return latest


def _head(db, meta):
    if not _has_schema(db):
        return None, None
    pub = db.execute("SELECT latest,updated_at FROM stable_publication WHERE slot=1").fetchone()
    row = db.execute(f"SELECT {_FIELDS} FROM stable_events ORDER BY seq DESC LIMIT 1").fetchone()
    if pub is None and row is None:
        return None, None
    if pub is None or row is None or pub["latest"] != row["seq"] or row["sha"] != meta["stable"]:
        raise OfficeError("STORE_IO")
    return dict(row), pub["updated_at"]


def versions(path, journal_id, offset=0):
    if type(offset) is not int or not 0 <= offset <= MAX_SQLITE_INT:
        raise ValueError("Invalid version offset")
    empty = {"available": False, "current": None, "updated_at": None, "items": [], "next_offset": None}
    try:
        with _reader(path, journal_id) as (db, meta):
            head, updated = _head(db, meta)
            if head is None:
                return empty
            rows = db.execute(f"SELECT {_FIELDS} FROM stable_events ORDER BY seq DESC LIMIT ? OFFSET ?",
                              (PAGE + 1, offset)).fetchall()
            return {"available": True, "current": head, "updated_at": updated,
                    "items": [dict(row) for row in rows[:PAGE]],
                    "next_offset": offset + PAGE if len(rows) > PAGE else None}
    except _Unavailable:
        return empty


# Only a path touched between the endpoints can differ. Resolve each endpoint with
# the (path,seq) index, including a missing row for a path added after the baseline.
_CHANGES = """
WITH touched AS (SELECT DISTINCT path FROM stable_revisions WHERE seq>? AND seq<=?)
SELECT p.path,b.mode AS before_mode,a.mode AS after_mode,b.oid AS before_oid,a.oid AS after_oid,
       count(*) OVER() AS total
FROM touched p
LEFT JOIN stable_revisions b ON b.path=p.path AND b.seq=(
    SELECT max(seq) FROM stable_revisions WHERE path=p.path AND seq<=?)
LEFT JOIN stable_revisions a ON a.path=p.path AND a.seq=(
    SELECT max(seq) FROM stable_revisions WHERE path=p.path AND seq<=?)
WHERE b.oid IS NOT a.oid OR b.mode IS NOT a.mode
ORDER BY p.path LIMIT ?
"""


def _file_diff(db, row, remaining_pairs):
    path, old, new = row["path"], row["before_mode"], row["after_mode"]
    output = [f"diff --git a/{path} b/{path}\n"]
    if old is None:
        output.append(f"new file mode {new}\n")
    elif new is None:
        output.append(f"deleted file mode {old}\n")
    elif old != new:
        output.append(f"old mode {old}\nnew mode {new}\n")
    if row["before_oid"] == row["after_oid"]:
        return "".join(output), False, remaining_pairs
    sides = []
    for side in ("before_oid", "after_oid"):
        oid = row[side]
        if oid is None:
            sides.append([])
            continue
        blob = db.execute("SELECT content,omission FROM stable_blobs WHERE oid=?", (oid,)).fetchone()
        if blob is None or (blob["content"] is None) == (blob["omission"] is None):
            raise OfficeError("STORE_IO")
        if blob["omission"]:
            output.append(f"[content unavailable: {blob['omission']}]\n")
            return "".join(output), True, remaining_pairs
        # Git lines are split on LF only, preserving CR and an unterminated final line.
        lines = blob["content"].split("\n")
        sides.append([line + "\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else []))
    pairs = len(sides[0]) * len(sides[1])
    if pairs > remaining_pairs:
        output.append("[content unavailable: line comparison budget exceeded]\n")
        return "".join(output), True, remaining_pairs
    for line in difflib.unified_diff(*sides, fromfile=f"a/{path}" if old else "/dev/null",
                                    tofile=f"b/{path}" if new else "/dev/null"):
        output.append(line)
        if not line.endswith("\n"):
            output.append("\n\\ No newline at end of file\n")
    return "".join(output), False, remaining_pairs - pairs


def compare(path, journal_id, from_seq, to_seq):
    if any(type(n) is not int or not 1 <= n <= MAX_SQLITE_INT for n in (from_seq, to_seq)):
        raise ValueError("Invalid stable version")
    if path is None:
        raise OfficeError("NOT_FOUND")
    with _reader(path, journal_id) as (db, meta):
        head, _ = _head(db, meta)
        if head is None:
            raise OfficeError("NOT_FOUND")
        shas = dict(db.execute("SELECT seq,sha FROM stable_events WHERE seq IN (?,?)", (from_seq, to_seq)))
        if from_seq not in shas or to_seq not in shas:
            raise OfficeError("NOT_FOUND")
        rows = []
        if shas[from_seq] != shas[to_seq]:
            bounds = sorted((from_seq, to_seq))
            rows = db.execute(_CHANGES, (*bounds, from_seq, to_seq, MAX_FILES)).fetchall()
        total = rows[0]["total"] if rows else 0
        files, preview, truncated, remaining = [], [], total > MAX_FILES, MAX_DIFF
        remaining_pairs = MAX_LINE_PAIRS
        for row in rows:
            old, new = row["before_mode"], row["after_mode"]
            status = "added" if old is None else "deleted" if new is None else "modified"
            files.append({"path": row["path"], "status": status, "before_mode": old, "after_mode": new})
            if remaining > 0:
                patch, omitted, remaining_pairs = _file_diff(db, row, remaining_pairs)
                preview.append(patch)
                remaining -= len(patch.encode("utf-8"))
                truncated |= omitted
            else:
                truncated = True
        if total > MAX_FILES:
            preview.append(f"[{total - MAX_FILES} additional changed files omitted]\n")
        diff, clipped = _bounded("".join(preview), MAX_DIFF)
        return {"from_seq": from_seq, "to_seq": to_seq, "from_sha": shas[from_seq], "to_sha": shas[to_seq],
                "files": files, "total_files": total, "diff": diff, "truncated": truncated or clipped}
