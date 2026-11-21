"""Local journal for external goals. Network requests never hold a database transaction."""
from contextlib import contextmanager
import json

from .store import TaskStore, encode, now

SUMMARY_KEYS = ("issue_id", "number", "title", "url", "state", "status", "run_id", "goal_id", "pr_number",
                "pr_url", "comment_id", "publish_state", "ci_state", "scope_pending", "last_error",
                "base_commit", "restart_required", "candidate_sha", "retries")
ISSUE_PAGE = 25
EVENT_PAGE = 50


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
            columns = {r["name"] for r in db.execute("PRAGMA table_info(github_issues)")}
            if "checked" not in columns:
                db.execute("ALTER TABLE github_issues ADD COLUMN checked REAL NOT NULL DEFAULT 0")
                db.execute("ALTER TABLE github_issues ADD COLUMN due REAL NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS github_issue_list ON github_issues(workspace,instance,repo_id,number)")
            db.execute("CREATE INDEX IF NOT EXISTS github_poll_queue ON github_issues(workspace,instance,repo_id,checked,number)")

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

    @staticmethod
    def get(db, key):
        row = db.execute("SELECT record FROM github_issues WHERE instance=? AND repo_id=? AND issue_id=?", key).fetchone()
        return json.loads(row[0]) if row else None

    def issue_page(self, binding, cursor):
        columns = ",".join(f"json_extract(record,'$.{k}') AS {k}" for k in SUMMARY_KEYS)
        query = f"SELECT {columns} FROM github_issues WHERE workspace=? AND instance=? AND repo_id=?"
        params = [binding["workspace"], binding["instance"], binding["repo_id"]]
        if cursor is not None:
            query += " AND number<?"
            params.append(cursor)
        with self.db() as db:
            rows = [dict(row, restart_required=bool(row["restart_required"])) for row in
                    db.execute(query + " ORDER BY number DESC LIMIT ?", (*params, ISSUE_PAGE + 1))]
        return rows[:ISSUE_PAGE], str(rows[ISSUE_PAGE-1]["number"]) if len(rows) > ISSUE_PAGE else None

    def scheduled(self, binding, at, limit):
        with self.db() as db:
            return [json.loads(row[0]) for row in db.execute("""SELECT record FROM github_issues
                WHERE workspace=? AND instance=? AND repo_id=? AND due<=?
                ORDER BY checked,number LIMIT ?""",
                (binding["workspace"], binding["instance"], binding["repo_id"], at, limit))]

    @staticmethod
    def save_issue(db, key, record):
        checked = record.get("last_checked", 0)
        settled = (record["state"] == "closed" and checked and not record.get("last_error") and
                   not record.get("needs_check") and record["publish_state"] not in {"comment_pending", "comment_ack_pending"})
        db.execute("""INSERT INTO github_issues(instance,repo_id,issue_id,workspace,number,record,checked,due)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(instance,repo_id,issue_id) DO UPDATE SET workspace=excluded.workspace,
            number=excluded.number,record=excluded.record,checked=excluded.checked,due=excluded.due""",
            (*key, record["workspace_id"], record["number"], encode(record), checked, checked + 3600 if settled else 0))

    def save(self, key, record, events=()):
        with self.db(write=True) as db:
            self.save_issue(db, key, record)
            for identity, kind, detail, status in events:
                db.execute("INSERT OR IGNORE INTO github_domain_events VALUES(?,?,?,?,?,?,?,?)",
                           (*key, identity, kind, detail, now(), status))

    def event(self, key, identity, kind, detail, status="observed"):
        with self.db(write=True) as db:
            return bool(db.execute("INSERT OR IGNORE INTO github_domain_events VALUES(?,?,?,?,?,?,?,?)",
                                   (*key, identity, kind, detail, now(), status)).rowcount)

    def events(self, key, cursor):
        query = "SELECT rowid AS pos,id,kind,detail,at,status FROM github_domain_events WHERE instance=? AND repo_id=? AND issue_id=?"
        params = key
        if cursor is not None:
            query += " AND rowid<?"
            params = (*key, cursor)
        with self.db() as db:
            rows = [dict(row) for row in db.execute(query + " ORDER BY rowid DESC LIMIT ?", (*params, EVENT_PAGE+1))]
        next_cursor = str(rows[EVENT_PAGE-1]["pos"]) if len(rows) > EVENT_PAGE else None
        for row in rows:
            del row["pos"]
        return rows[:EVENT_PAGE], next_cursor

    def has(self, key, identity):
        with self.db() as db:
            return db.execute("""SELECT 1 FROM github_domain_events WHERE instance=? AND repo_id=?
                AND issue_id=? AND id=?""", (*key, identity)).fetchone() is not None
