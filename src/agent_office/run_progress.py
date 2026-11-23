"""Small, replayable journal of confirmed provider activity. No provider text is retained."""
import json
import threading
import time

from .models import ACTIVE, OfficeError
from .store import TaskStore, now


LABELS = {
    "started": "CLI 프로세스 시작",
    "session": "공급자 세션 확인",
    "plan": "작업 계획 갱신",
    "response": "응답 작성 중",
    "command": "명령 실행 중",
    "file": "파일 작업 중",
    "tool": "도구 실행 중",
    "tool_finished": "도구 결과 수신",
    "output": "공급자 출력 수신",
}


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_provider_progress(
        id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL REFERENCES studio_runs(id),
        engine TEXT NOT NULL, activity TEXT NOT NULL,at TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS studio_progress_run ON studio_provider_progress(run_id,id)")


class Reporter:
    def __init__(self, path, run_id, engine):
        self.path, self.run_id, self.engine = path, run_id, engine
        self.last = None
        self.written = 0
        self.lock = threading.Lock()

    def __call__(self, activity):
        if activity not in LABELS:
            return
        with self.lock:
            current = time.monotonic()
            if self.last is not None and current - self.written < (3 if activity == self.last else 0.25):
                return
            with TaskStore(self.path, existing=True) as store, store._write():
                db = store.connection
                db.execute("INSERT INTO studio_provider_progress(run_id,engine,activity,at) VALUES(?,?,?,?)",
                           (self.run_id, self.engine, activity, now()))
                db.execute("""DELETE FROM studio_provider_progress WHERE run_id=? AND id <=
                    (SELECT id FROM studio_provider_progress WHERE run_id=? ORDER BY id DESC LIMIT 1 OFFSET 128)""",
                           (self.run_id, self.run_id))
            self.last, self.written = activity, current


def public(row):
    return {"id": row["id"], "engine": row["engine"], "label": LABELS[row["activity"]], "at": row["at"]}


def latest(db, run_id):
    row = db.execute("SELECT * FROM studio_provider_progress WHERE run_id=? ORDER BY id DESC LIMIT 1",
                     (run_id,)).fetchone()
    return public(row) if row else None


def stream(handler, run_id, cursor):
    server = handler.server
    if not server.progress_slots.acquire(blocking=False):
        raise OfficeError("RUN_BUSY")
    try:
        with TaskStore(server.db_path, existing=True, readonly=True) as store:
            if not store.connection.execute("SELECT 1 FROM studio_runs WHERE id=?", (run_id,)).fetchone():
                raise OfficeError("NOT_FOUND")
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.connection.settimeout(3)
            handler.wfile.write(b"retry: 2000\n\n")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                rows = store.connection.execute("""SELECT * FROM studio_provider_progress
                    WHERE run_id=? AND id>? ORDER BY id LIMIT 128""", (run_id, cursor)).fetchall()
                for row in rows:
                    cursor = row["id"]
                    data = json.dumps(public(row), ensure_ascii=False)
                    handler.wfile.write(f"id: {cursor}\ndata: {data}\n\n".encode())
                if store.get_run(run_id).state not in ACTIVE:
                    handler.wfile.write(b"event: finished\ndata: {}\n\n")
                    break
                handler.wfile.flush()
                if server.runner.shutdown.wait(0.8):
                    break
            handler.wfile.flush()
    finally:
        server.progress_slots.release()
