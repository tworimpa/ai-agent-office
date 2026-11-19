"""Host-local improvement generation. Operator imports only receipted artifacts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

if __name__ == "__main__":
    # The isolated supervisor and worker import this installation, never the source checkout.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "agent_office"

from . import improvement_proposal as proposals
from .away_evidence import unique_object
from .models import OfficeError, TERMINAL_OUTCOMES, validate_id
from .ownership import RunGuard
from .store import TaskStore, encode
from .studio_process import run_until_owner_closes


PAGE_SIZE = 24
_SUMMARY = "id,base,engine,run_ids,state,title,message"
_INTERRUPTED = "생성 작업이 중단되었습니다. 다시 제안하려면 새 작업을 시작하세요."
_FAILED = "개선안을 생성하지 못했습니다. 선택한 실행 기록, 기준 커밋과 엔진 상태를 확인하세요."
_SCHEMA = """CREATE TABLE IF NOT EXISTS studio_improvements (
    id TEXT PRIMARY KEY, base TEXT NOT NULL, engine TEXT NOT NULL, run_ids TEXT NOT NULL,
    state TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
    artifact TEXT, digest TEXT
)"""


def _summary(row):
    result = dict(row)
    result["run_ids"] = json.loads(result["run_ids"])
    return result


def _job_row(db, job_id, columns=_SUMMARY):
    row = db.execute(f"SELECT {columns} FROM studio_improvements WHERE id=?", (validate_id(job_id),)).fetchone()
    if row is None:
        raise OfficeError("NOT_FOUND")
    return row


def ready_artifact(db_path, job_id):
    """Validate the entire job binding and original journal receipt on every import."""
    with TaskStore(db_path, readonly=True) as store:
        row = _job_row(store.connection, job_id, _SUMMARY + ",artifact,digest")
    if row["state"] != "ready":
        raise OfficeError("REQUEST_CONFLICT")
    try:
        artifact = proposals.validate_proposal(json.loads(row["artifact"], object_pairs_hook=unique_object))
        if (proposals._digest(artifact) != row["digest"] or artifact["base"] != row["base"]
                or artifact["engine"] != row["engine"] or artifact["title"] != row["title"]
                or [item["run_id"] for item in artifact["history"]] != json.loads(row["run_ids"])):
            raise ValueError("Generation mismatch")
        proposals.verify_history(db_path, artifact)
    except (ValueError, KeyError, TypeError, RecursionError, OfficeError) as exc:
        raise ValueError("생성 기록과 개선안 근거가 일치하지 않습니다.") from exc
    return artifact


def ready_job_receipts(db_path):
    """Scan through the newest existing row, releasing each SQLite cursor before yielding."""
    with TaskStore(db_path, readonly=True) as store:
        db = store.connection
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_improvements'").fetchone():
            return
        last = db.execute("SELECT COALESCE(MAX(rowid),0) FROM studio_improvements").fetchone()[0]
        cursor = 0
        while cursor < last:
            page = db.execute("""SELECT rowid,id,digest FROM studio_improvements WHERE rowid>? AND rowid<=?
                                 AND state='ready' ORDER BY rowid LIMIT ?""", (cursor, last, PAGE_SIZE)).fetchall()
            if not page:
                break
            cursor = page[-1]["rowid"]
            yield from ((row["id"], row["digest"]) for row in page)


def ready_job_ids(db_path):
    return (job_id for job_id, _ in ready_job_receipts(db_path))


def _slot(db_path):
    # Held by the supervisor until all worker descendants have been drained, even after owner EOF.
    guard = RunGuard(db_path, "0" * 32)
    guard.path = guard.path.with_name("studio-improvement-generation.lock")
    return guard


def _finish(db_path, job_id, state, message="", artifact=None):
    with TaskStore(db_path, existing=True) as store:
        store.connection.execute("""UPDATE studio_improvements SET state=?,message=?,title=?,artifact=?,digest=?
                                    WHERE id=? AND state='generating'""",
                                 (state, message, artifact["title"] if artifact else "",
                                  encode(artifact) if artifact else None, proposals._digest(artifact) if artifact else None,
                                  job_id))


def _generate(db_path, job_id, source):
    with TaskStore(db_path, readonly=True) as store:
        job = _summary(_job_row(store.connection, job_id))
    if job["state"] != "generating":
        return
    try:
        artifact = proposals.create_proposal(db_path, source, job["base"], job["run_ids"], job["engine"])
    except proposals.NoImprovementSuggested:
        _finish(db_path, job_id, "no_proposal", "선택한 실행 기록에서 제안할 개선 사항을 찾지 못했습니다.")
    except Exception:
        # Git and CLI exceptions may include host paths, provider data or parts of the original request.
        _finish(db_path, job_id, "failed", _FAILED)
    else:
        _finish(db_path, job_id, "ready", artifact=artifact)


def _command(mode, db_path, job_id, source):
    return [sys.executable, "-I", str(Path(__file__).resolve()), mode, str(db_path), job_id, str(source)]


def _supervise(db_path, job_id, source):
    if sys.stdin.buffer.readline() != b"start\n":
        return
    with _slot(db_path), tempfile.TemporaryDirectory(prefix="office-studio-improvement-") as scratch:
        argv = _command("generate", db_path, job_id, source)
        run_until_owner_closes(argv, json.dumps(scratch).encode("ascii"), timeout=300)


def _page(offset, rows):
    return {"items": rows[:PAGE_SIZE], "next_offset": offset + PAGE_SIZE if len(rows) > PAGE_SIZE else None}


def _offset(value):
    if type(value) is not int or not 0 <= value < 2**53 or value % PAGE_SIZE:
        raise ValueError("페이지 위치를 확인하세요.")
    return value


class StudioImprovements:
    """Created under the host's Studio ownership lock. Only start can launch a provider."""
    def __init__(self, db_path: Path, source: Path | None):
        self.path = Path(db_path).expanduser().resolve()
        self.source = Path(source).expanduser().resolve(strict=True) if source is not None else None
        if self.source is not None and not self.source.is_dir():
            raise ValueError("개선안 기준 소스 디렉터리를 확인하세요.")
        self.lock = threading.Lock()
        self.closed = False
        self.child = None
        self.monitor = None
        with TaskStore(self.path) as store, store._write():
            store.connection.execute(_SCHEMA)
            store.connection.execute("""CREATE INDEX IF NOT EXISTS studio_improvement_by_state
                                        ON studio_improvements(state)""")
            store.connection.execute("""UPDATE studio_improvements SET state='interrupted',message=?
                                        WHERE state='generating'""", (_INTERRUPTED,))

    def runs(self, offset=0):
        _offset(offset)
        states = tuple(TERMINAL_OUTCOMES)
        with TaskStore(self.path, readonly=True) as store:
            rows = store.connection.execute(f"""SELECT r.run_id AS id,t.request_snapshot AS request,r.state,r.outcome
                FROM runs r JOIN tasks t ON t.task_id=r.task_id
                WHERE r.ended_at IS NOT NULL AND r.state IN ({','.join('?' for _ in states)})
                ORDER BY r.ended_at DESC,r.run_id LIMIT ? OFFSET ?""", (*states, PAGE_SIZE + 1, offset))
            return _page(offset, [dict(row, request=proposals._request(row["request"])) for row in rows])

    def jobs(self, offset=0):
        with TaskStore(self.path, readonly=True) as store:
            rows = store.connection.execute(f"""SELECT {_SUMMARY} FROM studio_improvements
                                                ORDER BY rowid DESC LIMIT ? OFFSET ?""", (PAGE_SIZE + 1, _offset(offset)))
            return _page(offset, [_summary(row) for row in rows])

    def job(self, job_id):
        with TaskStore(self.path, readonly=True) as store:
            return _summary(_job_row(store.connection, job_id))

    def artifact(self, job_id):
        return ready_artifact(self.path, job_id)

    def start(self, job_id, base, engine, run_ids):
        validate_id(job_id)
        proposals._base_engine(base, engine)
        with self.lock, TaskStore(self.path, existing=True) as store:
            db = store.connection
            previous = db.execute(f"SELECT {_SUMMARY} FROM studio_improvements WHERE id=?", (job_id,)).fetchone()
            if previous is not None:
                job = _summary(previous)
                if (job["base"], job["engine"], job["run_ids"]) != (base, engine, run_ids):
                    raise OfficeError("REQUEST_CONFLICT")
                return job
            if self.source is None:
                raise ValueError("호스트에서 개선안 기준 소스를 지정해야 합니다.")
            if (self.closed or (self.child and self.child.poll() is None) or _slot(self.path).is_held()
                    or db.execute("SELECT 1 FROM studio_improvements WHERE state='generating' LIMIT 1").fetchone()):
                raise OfficeError("RUN_BUSY")
            proposals._history(self.path, run_ids)
            db.execute("""INSERT INTO studio_improvements (id,base,engine,run_ids,state)
                          VALUES (?,?,?,?,'generating')""", (job_id, base, engine, encode(run_ids)))
            try:
                child = subprocess.Popen(_command("supervise", self.path, job_id, self.source), stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         start_new_session=os.name != "nt",
                                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError:
                _finish(self.path, job_id, "failed", _FAILED)
            else:
                self.child = child
                self.monitor = threading.Thread(target=self._watch, args=(job_id, child), daemon=True)
                self.monitor.start()
                try:
                    child.stdin.write(b"start\n")
                    child.stdin.flush()
                except OSError:
                    child.stdin.close()
            return self.job(job_id)

    def _watch(self, job_id, child):
        child.wait()
        with self.lock:
            child.stdin.close()
            _finish(self.path, job_id, "interrupted", _INTERRUPTED)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.child:
                self.child.stdin.close()
            with TaskStore(self.path, existing=True) as store:
                store.connection.execute("""UPDATE studio_improvements SET state='interrupted',message=?
                                            WHERE state='generating'""", (_INTERRUPTED,))
        if self.monitor:
            self.monitor.join(timeout=30)


if __name__ == "__main__":
    mode, db, job_id, source = sys.argv[1:]
    if mode == "supervise":
        _supervise(Path(db), validate_id(job_id), Path(source))
    elif mode == "generate":
        tempfile.tempdir = json.load(sys.stdin.buffer)
        _generate(Path(db), validate_id(job_id), Path(source))
    else:
        sys.exit(2)
