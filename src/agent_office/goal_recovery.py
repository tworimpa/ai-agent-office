"""Execution-host goal checkpoints, wait intervals and guarded continuation receipts."""
from datetime import datetime
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import sysconfig

from .events import snapshot_request
from .goal_candidates import private_name
from .meeting_parallel import _contents
from .models import ACTIVE, OfficeError, validate_id
from .ownership import RunGuard, RunBusyError
from .release_store import run_git
from .store import TaskStore, encode, now
from .workspace import plan_worktree


def ensure_schema(db):
    from . import goal_stage_wait
    goal_stage_wait.ensure_schema(db)
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_checkpoints(
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id), stage TEXT NOT NULL, state_json TEXT NOT NULL,
        resume_phase TEXT NOT NULL, seq INTEGER NOT NULL, digest TEXT, created_at TEXT NOT NULL,
        claimed INTEGER NOT NULL DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_recoveries(
        run_id TEXT NOT NULL REFERENCES runs(run_id), id TEXT NOT NULL, disposition TEXT NOT NULL,
        reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id,id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_workers(holder TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id), task_id TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_waits(request_id TEXT PRIMARY KEY
        REFERENCES studio_requests(id), started_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_drains(run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
        drained_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_owners(run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
        seen_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_gaps(run_id TEXT NOT NULL REFERENCES runs(run_id),
        started_at TEXT NOT NULL, ended_at TEXT NOT NULL, PRIMARY KEY(run_id,started_at))""")


def is_goal(db, run_id):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_goals'").fetchone()
                and db.execute("SELECT 1 FROM studio_goals WHERE id=?", (run_id,)).fetchone())


def note_wait(db, request_id):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_goals'").fetchone():
        db.execute("""INSERT OR IGNORE INTO studio_goal_waits
            SELECT q.id,? FROM studio_requests q JOIN studio_goals g ON g.id=q.run_id WHERE q.id=?""",
                   (now(), request_id))


def note_owner(db, run_id):
    try:
        db.execute("INSERT OR REPLACE INTO studio_goal_owners VALUES(?,?)", (run_id, now()))
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", 0) & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise


def mark_owner_gap(db, run_id):
    if not is_goal(db,run_id):
        return
    ensure_schema(db)
    last = db.execute("""SELECT max(stamp) FROM (
        SELECT seen_at AS stamp FROM studio_goal_owners WHERE run_id=?
        UNION ALL SELECT timestamp FROM events WHERE run_id=?)""", (run_id,run_id)).fetchone()[0]
    if last:
        db.execute("INSERT OR IGNORE INTO studio_goal_gaps VALUES(?,?,?)",(run_id,last,now()))


def timing(db, run_id, *, at=None):
    """Only freeze the goal clock while every running independent worker needs a human."""
    run = db.execute("SELECT started_at,ended_at FROM runs WHERE run_id=?", (run_id,)).fetchone()
    end = run["ended_at"] or at or now()
    start = run["started_at"]
    intervals = []
    def add(kind, key, lower, upper):
        lower, upper = max(start, lower), min(end, upper or end)
        if lower <= upper:
            intervals.append((kind, key, lower, upper))

    for row in db.execute("""SELECT w.started_at,q.decided_at,q.expired_at,q.holder FROM studio_goal_waits w
        JOIN studio_requests q ON q.id=w.request_id WHERE q.run_id=?""", (run_id,)):
        add("wait", row["holder"], row["started_at"], row["decided_at"] or row["expired_at"])
    for row in db.execute("SELECT * FROM studio_goal_workers WHERE run_id=?", (run_id,)):
        add("worker", row["holder"], row["started_at"], row["ended_at"])
    stage_wait = False
    for lower, upper in db.execute("SELECT started_at,ended_at FROM studio_goal_stage_waits WHERE run_id=?", (run_id,)):
        add("dependency", "", lower, upper)
        stage_wait |= upper is None
    revisions = list(db.execute("SELECT created_at,approved_at,approved_by FROM studio_meeting_revisions "
                                "WHERE run_id=? ORDER BY revision", (run_id,)))
    for index, row in enumerate(revisions):
        if row["approved_by"] != "coordinator":
            next_revision = revisions[index+1]["created_at"] if index+1 < len(revisions) else end
            add("plan", "", row["created_at"], row["approved_at"] or next_revision)
    paused = None
    for event, stamp in db.execute("SELECT event_type,timestamp FROM events WHERE run_id=? AND event_type IN "
                                   "('RECOVERY_REQUIRED','RECOVERY_RESUMED') ORDER BY seq", (run_id,)):
        if event == "RECOVERY_REQUIRED":
            paused = stamp
        elif paused:
            add("offline", "", paused, stamp)
            paused = None
    if paused:
        add("offline", "", paused, end)
    for lower, upper in db.execute("SELECT started_at,ended_at FROM studio_goal_gaps WHERE run_id=?", (run_id,)):
        add("unknown", "", lower, upper)
    def mode(t):
        groups = {}
        for kind, key, lower, upper in intervals:
            if lower <= t < upper:
                groups.setdefault(kind, set()).add(key)
        if "unknown" in groups:
            return "unknown"
        if "offline" in groups:
            return "offline"
        if "plan" in groups or ("wait" in groups and groups.get("worker", set()) <= groups["wait"]):
            return "wait"
        if "dependency" in groups:
            return "dependency"
        return "active"

    edges = sorted({start, end, *(t for _, _, a, b in intervals for t in (a, b))})
    elapsed = {"active": 0., "wait": 0., "offline": 0., "unknown": 0., "dependency": 0.}
    for left, right in zip(edges, edges[1:]):
        elapsed[mode(left)] += (datetime.fromisoformat(right) - datetime.fromisoformat(left)).total_seconds()
    # The final interval may end at the time of this sample itself. Check the current gate separately.
    workers = {r[0] for r in db.execute("SELECT holder FROM studio_goal_workers WHERE run_id=? AND ended_at IS NULL",
                                        (run_id,))}
    waits = {r[0] for r in db.execute("""SELECT q.holder FROM studio_requests q JOIN studio_goal_waits w
        ON q.id=w.request_id WHERE q.run_id=? AND q.status='pending'""", (run_id,))}
    plan = db.execute("SELECT 1 FROM studio_meetings WHERE run_id=? AND status='awaiting_review'", (run_id,)).fetchone()
    return {"active_seconds": round(elapsed["active"], 3), "user_wait_seconds": round(elapsed["wait"], 3),
            "dependency_wait_seconds": round(elapsed["dependency"], 3),
            "waiting_for_dependency": bool(not run["ended_at"] and stage_wait),
            "unobserved_seconds": round(elapsed["unknown"], 3),
            "waiting_for_user": bool(not run["ended_at"] and (plan or waits and workers <= waits))}


def _environment(root, checks, update):
    from .goal_checks import _command, recipe

    def file(path):
        path = Path(path).resolve(strict=True)
        if not path.is_file() or path.stat().st_size > 512 * 1024 * 1024:
            raise ValueError("검증 환경의 파일을 확인할 수 없습니다.")
        with path.open("rb") as stream:
            update(encode([str(path), hashlib.file_digest(stream, "sha256").hexdigest()]).encode())

    update(encode([platform.node(), platform.system(), platform.version(), platform.machine(),
                   sys.implementation.name, sys.version, sys.prefix]).encode())
    file(sys.executable)
    # Only installed distribution names and versions. No executable probes or environment dumps.
    sites = sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
    packages = sorted((d.metadata.get("Name", "").casefold(), d.version or "")
                      for d in metadata.distributions(path=sites))
    update(encode(packages).encode())
    host_module = Path(__file__).parent
    for name in ("goal_checks.py", "goal_candidates.py", "goal_verification.py", "goal_host_checks.py",
                 "goal_peer.py", "goal_pytest.py", "goal_process.py", "goal_artifact.py",
                 "goal_reproduction.py", "studio_process.py", "workflow.py", "goal_stage_wait.py",
                 "workspace_coordination.py"):
        file(host_module / name)
    files = set()
    for check in checks:
        if check["host"].casefold() not in ("local", platform.node().casefold()):
            # The origin cannot attest that the remote environment still matches after a crash.
            raise ValueError("원격 호스트 검증이 포함된 목표의 복구에는 실행 환경을 다시 확인해야 합니다.")
        if recipe(check["argv"]) == "pytest":
            # The --lf policy resolves against the saved baseline cases below.
            continue
        argv, kind = _command(root, check, Path("report.xml"))
        update(encode(argv).encode())
        files.add(Path(argv[0]))
        if kind == "web":
            project = root
            if not (root / "node_modules/jsdom/package.json").is_file():
                project = Path(os.environ.get("AGENT_OFFICE_GOAL_WEB_PROJECT", Path(__file__).resolve().parents[2]))
            files.update((project / "package-lock.json", project / "node_modules/.package-lock.json",
                          project / "node_modules/jsdom/package.json"))
        elif kind == "installed":
            harness, installed = map(Path, argv[1:])
            runtime = installed.parent / "resources"
            files.update((harness, harness.with_name("studio.mjs"), installed, runtime / "app.asar",
                          harness.parent / "package-lock.json", harness.parent / "node_modules/.package-lock.json",
                          harness.parent / "node_modules/playwright/package.json",
                          harness.parent / "fixtures/codex.py", harness.parent / "fixtures/agy.py",
                          harness.parent / "fixtures/meeting.py"))
    for path in sorted(files):
        file(path)


def _fingerprint(db, run_id):
    goal = db.execute("SELECT * FROM studio_goals WHERE id=?", (run_id,)).fetchone()
    task = db.execute("SELECT t.* FROM runs r JOIN tasks t ON t.task_id=r.task_id WHERE run_id=?",
                      (run_id,)).fetchone()
    project, root = Path(task["project"]), Path(task["workspace"])
    expected = plan_worktree(project, run_id)
    if (root.resolve(strict=True) != expected.path or task["branch"] != expected.branch or
            run_git(root, "symbolic-ref", "--short", "HEAD").decode().strip() != expected.branch or
            Path(run_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()).resolve()
            != Path(run_git(project, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()).resolve()):
        raise ValueError("작업 트리 소유 정보를 확인할 수 없습니다.")
    base = goal["base_commit"]
    external = (db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_external_origins'").fetchone()
                and db.execute("SELECT 1 FROM studio_external_origins WHERE run_id=?", (run_id,)).fetchone())
    if (run_git(root, "rev-parse", "--verify", "HEAD").decode().strip() != base or
            not external and run_git(project, "rev-parse", "--verify", "HEAD").decode().strip() != base):
        raise ValueError("기준 커밋이 변경되었습니다.")
    digest = hashlib.sha256()
    def update(value):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    update(encode({k: goal[k] for k in ("id","request","scope_json","criteria_json","policy_json","base_commit")}).encode())
    update(encode([str(project), str(root), task["developer_engine"], task["reviewer_engine"], task["max_attempts"]]).encode())
    _environment(root, json.loads(goal["policy_json"]).get("checks", []), update)
    update(run_git(root, "ls-files", "--stage", "-z"))
    def check_name(name):
        if private_name(name):
            raise ValueError("비공개 파일이 포함된 작업 트리에는 복구 체크포인트를 만들 수 없습니다.")
    for name, (mode, data) in sorted(_contents(root, check_name=check_name).items()):
        update(encode([name, mode]).encode())
        update(data)
    for table in ("studio_room_assignments", "studio_meeting_revisions", "studio_directions", "studio_requests",
                  "studio_run_skills", "studio_away_policies", "studio_away_choice_policies"):
        rows = db.execute(f"SELECT * FROM {table} WHERE run_id=? ORDER BY rowid", (run_id,))
        update(encode([tuple(row) for row in rows]).encode())
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_goal_checks'").fetchone():
        rows = db.execute("SELECT attempt,position,receipt,cases FROM studio_goal_checks WHERE run_id=? "
                          "ORDER BY attempt,position", (run_id,))
        for row in rows:
            update(encode(tuple(row)).encode())
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_goal_reproducers'").fetchone():
        rows = db.execute("SELECT files_json,check_json,created_at FROM studio_goal_reproducers WHERE run_id=?",
                          (run_id,))
        for row in rows:
            update(encode(tuple(row)).encode())
    return digest.hexdigest()


def checkpoint(db, run_id, next_stage, state):
    """Caller has finished and recorded all outputs for the preceding operation."""
    if next_stage not in ("start_goal", "baseline", "develop", "test", "review"):
        raise ValueError("Unknown goal checkpoint stage")
    if not is_goal(db, run_id):
        return
    row = db.execute("SELECT next_seq,state FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row["state"] not in ACTIVE:
        return
    try:
        digest = _fingerprint(db, run_id)
    except (ValueError, OSError, UnicodeError):
        digest = None
    saved = {key: state[key] for key in ("attempts", "status", "check_index", "feedback", "test_output",
                                          "developer_output") if key in state}
    for key in ("feedback", "test_output", "developer_output"):
        if key in saved:
            saved[key] = snapshot_request(saved[key])
    db.execute("INSERT OR REPLACE INTO studio_goal_checkpoints VALUES (?,?,?,?,?,?,?,0)",
               (run_id, next_stage, encode(saved), row["state"], row["next_seq"]-1, digest, now()))


def invalidate_checkpoint(db, run_id):
    db.execute("UPDATE studio_goal_checkpoints SET digest=NULL,claimed=0 WHERE run_id=?", (run_id,))


def resume_state(db, run_id):
    row = db.execute("SELECT * FROM studio_goal_checkpoints WHERE run_id=? AND claimed=1", (run_id,)).fetchone()
    if row is None:
        return None
    # Verify again inside the worker after supervisor ownership is acquired.
    if _fingerprint(db, run_id) != row["digest"]:
        raise ValueError("복구 시점의 작업 파일이나 입력이 달라졌습니다.")
    return dict(json.loads(row["state_json"]), next_stage=row["stage"])


def detail(store, run_id):
    db = store.connection
    if not is_goal(db, validate_id(run_id)):
        return None
    run = store.get_run(run_id)
    if run.host_name != platform.node():
        raise OfficeError("HOST_MISMATCH")
    supervisor_alive = RunGuard(store.path, run_id).is_held()
    worker_alive = RunGuard.worker(store.path, run_id).is_held()
    alive = supervisor_alive or worker_alive
    cp = db.execute("SELECT * FROM studio_goal_checkpoints WHERE run_id=?", (run_id,)).fetchone()
    directions = [dict(row) for row in db.execute("SELECT id,role,delivery FROM studio_directions WHERE run_id=? "
                                                  "AND delivery!='delivered' ORDER BY rowid", (run_id,))]
    decisions = [dict(row) for row in db.execute("""SELECT q.id,q.role,q.kind,q.decision_type,q.status,w.task_id
        FROM studio_requests q LEFT JOIN studio_goal_workers w ON w.holder=q.holder WHERE q.run_id=? AND
        (q.status IN ('pending','decided') OR (?='RECOVERY_REQUIRED' AND q.status='expired' AND q.expired_at>=?))
        ORDER BY q.rowid""", (run_id, run.state, cp["created_at"] if cp else run.started_at))]
    receipts = [dict(row) for row in db.execute("SELECT id,disposition,reason,created_at FROM studio_goal_recoveries "
                                                "WHERE run_id=? ORDER BY rowid DESC LIMIT 20", (run_id,))]
    public = {"stage": cp["stage"], "created_at": cp["created_at"], "digest": cp["digest"]} if cp else None
    clock = timing(db, run_id)
    stopped = db.execute("SELECT stop_requested FROM studio_runs WHERE id=?", (run_id,)).fetchone()[0]
    from .goal_stage_wait import waiting
    stage_wait = waiting(db, run_id)
    if stage_wait and run.state in ACTIVE:
        action, reason = "waiting_dependency", "\n".join(json.loads(stage_wait["reasons"]))
    elif alive:
        action = "working"
        reason = ("작업을 계속하려면 운영자 응답이 필요합니다." if clock["waiting_for_user"] else
                  "일부 작업은 운영자 응답을 기다리고 다른 담당자는 실행 중입니다." if decisions else
                  "현재 실행 소유자가 작업을 관리하고 있습니다.")
    elif run.state in ACTIVE:
        action, reason = "inspect", "실행 저널이 열려 있습니다. 호스트에서 작업자의 소유 상태를 확인하고 있습니다."
    elif run.state != "RECOVERY_REQUIRED" or stopped:
        action, reason = "closed", "실행이 종료되었습니다."
    elif db.execute("SELECT 1 FROM studio_goal_redacted_requests WHERE goal_id=?", (run_id,)).fetchone():
        action, reason = "inspect", "저장할 때 원래 요청의 일부를 가렸습니다. 계속할 요청 내용을 확인하세요."
    elif directions or decisions:
        action, reason = "inspect", "미전달 지시나 미결 요청을 확인하세요."
    elif not db.execute("SELECT 1 FROM studio_goal_drains WHERE run_id=?", (run_id,)).fetchone():
        action, reason = "inspect", "감독 프로세스가 하위 프로세스의 종료를 확인하기 전에 연결이 끊겼습니다."
    elif not cp or not cp["digest"] or db.execute("SELECT 1 FROM events WHERE run_id=? AND seq>? "
                                "AND event_type='STAGE_STARTED'", (run_id, cp["seq"])).fetchone():
        action, reason = "inspect", "외부 작업의 종료 및 다음 실행 경계를 확인할 수 없습니다."
    else:
        try:
            matches = _fingerprint(db, run_id) == cp["digest"]
        except (ValueError, OSError, UnicodeError):
            matches = False
        if matches:
            action, reason = "resume_available", "저장한 입력과 작업 파일을 확인했습니다."
        else:
            action, reason = "inspect", "기준 커밋, 작업 파일, 검증 설정 또는 운영자 입력이 달라졌습니다."
    return dict(disposition=action, reason=reason, worker_alive=worker_alive, supervisor_alive=supervisor_alive,
                checkpoint=public,
                pending_directions=directions, pending_decisions=decisions, receipts=receipts, **clock)


def reconcile(runner, run_id, request_id):
    validate_id(request_id)
    with runner.lock, TaskStore(runner.path, existing=True) as store:
        runner._refresh(store)
        db = store.connection
        status = detail(store, run_id)
        if status is None:
            raise OfficeError("NOT_FOUND")
        previous = db.execute("SELECT * FROM studio_goal_recoveries WHERE run_id=? AND id=?", (run_id, request_id)).fetchone()
        if previous:
            return dict(status, receipt=dict(previous))
        if runner.closed:
            raise OfficeError("RUN_BUSY")
        if status["disposition"] == "resume_available" and run_id not in runner.children:
            try:
                with RunGuard(runner.path, run_id), RunGuard.worker(runner.path, run_id), store._write():
                    cp = db.execute("SELECT * FROM studio_goal_checkpoints WHERE run_id=?", (run_id,)).fetchone()
                    # The worker rechecks the live tree before its first operation. Keep
                    # filesystem reads outside the SQLite writer transaction.
                    if (store.get_run(run_id).state != "RECOVERY_REQUIRED" or not cp or
                            status["checkpoint"]["digest"] != cp["digest"] or
                            db.execute("SELECT stop_requested FROM studio_runs WHERE id=?", (run_id,)).fetchone()[0]):
                        raise OfficeError("REQUEST_CONFLICT")
                    prompt = db.execute("SELECT request FROM studio_goals WHERE id=?", (run_id,)).fetchone()[0]
                    stamp = now()
                    db.execute("UPDATE runs SET state=?,outcome=NULL,ended_at=NULL WHERE run_id=?",
                               (cp["resume_phase"], run_id))
                    db.execute("UPDATE tasks SET state=?,outcome=NULL,updated_at=? WHERE current_run_id=?",
                               (cp["resume_phase"], stamp, run_id))
                    db.execute("UPDATE studio_runs SET diagnostic='',proposed_state=NULL,proposed_outcome=NULL "
                               "WHERE id=?", (run_id,))
                    db.execute("UPDATE studio_goal_checkpoints SET claimed=1 WHERE run_id=?", (run_id,))
                    db.execute("DELETE FROM studio_goal_drains WHERE run_id=?", (run_id,))
                    store._record(run_id, operation_key=f"{run_id}:resume:{request_id}", event_type="RECOVERY_RESUMED",
                                  payload={"phase":"run"})
                    db.execute("INSERT INTO studio_goal_recoveries VALUES (?,?, 'resumed',?,?)",
                               (run_id, request_id, status["reason"], stamp))
            except RunBusyError:
                raise OfficeError("RUN_BUSY") from None
            runner._start_monitor()
            runner._launch(store, run_id, prompt)
        else:
            db.execute("INSERT INTO studio_goal_recoveries VALUES (?,?,?,?,?)",
                       (run_id, request_id, status["disposition"], status["reason"], now()))
        receipt = db.execute("SELECT * FROM studio_goal_recoveries WHERE run_id=? AND id=?", (run_id, request_id)).fetchone()
        return dict(detail(store, run_id), receipt=dict(receipt))
