"""Local journal for external goals. Network requests never hold a database transaction."""
from contextlib import contextmanager
import json

from .store import TaskStore, encode, now


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS github_bindings(
        workspace TEXT PRIMARY KEY, instance TEXT NOT NULL, repo_id INTEGER NOT NULL,
        identity TEXT NOT NULL, enabled INTEGER NOT NULL, config TEXT NOT NULL,
        cursor TEXT, last_full REAL DEFAULT 0, last_sync TEXT, last_error TEXT, retry_at REAL DEFAULT 0)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS github_active_binding ON github_bindings(instance,repo_id)
        WHERE enabled=1""",
    """CREATE TABLE IF NOT EXISTS github_scans(
        workspace TEXT PRIMARY KEY, page INTEGER NOT NULL, since TEXT, cutoff TEXT NOT NULL, full INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS github_issues(
        instance TEXT NOT NULL, repo_id INTEGER NOT NULL, issue_id INTEGER NOT NULL,
        workspace TEXT NOT NULL, number INTEGER NOT NULL, record TEXT NOT NULL,
        PRIMARY KEY(instance,repo_id,issue_id))""",
    """CREATE TABLE IF NOT EXISTS github_domain_events(
        instance TEXT NOT NULL, repo_id INTEGER NOT NULL, issue_id INTEGER NOT NULL,
        id TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL, status TEXT NOT NULL,
        PRIMARY KEY(instance,repo_id,issue_id,id))""",
)


class IssueJournal:
    def __init__(self, path):
        self.path = path
        with self.db(write=True) as db:
            for sql in SCHEMA:
                db.execute(sql)

    @contextmanager
    def db(self, *, write=False):
        with TaskStore(self.path, existing=True) as store:
            if write:
                with store._write():
                    yield store.connection
            else:
                yield store.connection

    def bindings(self, workspace=None):
        with self.db() as db:
            sql = "SELECT * FROM github_bindings"
            rows = db.execute(sql if workspace is None else sql + " WHERE workspace=?",
                              () if workspace is None else (workspace,))
            return [dict(dict(row), config=json.loads(row["config"])) for row in rows]

    def records(self, binding):
        with self.db() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT record FROM github_issues WHERE instance=? AND repo_id=? ORDER BY number DESC",
                (binding["instance"], binding["repo_id"]))]

    def save(self, key, record, events=()):
        with self.db(write=True) as db:
            db.execute("""INSERT INTO github_issues VALUES(?,?,?,?,?,?)
                ON CONFLICT(instance,repo_id,issue_id) DO UPDATE SET workspace=excluded.workspace,
                number=excluded.number,record=excluded.record""",
                       (*key, record["workspace_id"], record["number"], encode(record)))
            for identity, kind, detail, status in events:
                db.execute("INSERT OR IGNORE INTO github_domain_events VALUES(?,?,?,?,?,?,?,?)",
                           (*key, identity, kind, detail, now(), status))

    def event(self, key, identity, kind, detail, status="observed"):
        with self.db(write=True) as db:
            return bool(db.execute("INSERT OR IGNORE INTO github_domain_events VALUES(?,?,?,?,?,?,?,?)",
                                   (*key, identity, kind, detail, now(), status)).rowcount)

    def events(self, key, limit=100):
        with self.db() as db:
            return [dict(row) for row in db.execute("""SELECT id,kind,detail,at,status FROM github_domain_events
                WHERE instance=? AND repo_id=? AND issue_id=? ORDER BY rowid DESC LIMIT ?""", (*key, limit))]

    def has(self, key, identity):
        with self.db() as db:
            return db.execute("""SELECT 1 FROM github_domain_events WHERE instance=? AND repo_id=?
                AND issue_id=? AND id=?""", (*key, identity)).fetchone() is not None
