"""Local command-line entry point for Agent Office AO-000."""

from __future__ import annotations

import argparse
import json
import sys
import subprocess
import uuid
import platform
import sqlite3
from pathlib import Path

from .adapters import CLIAdapter, SUPPORTED_ENGINES
from .engines import INITIAL_ENGINES, DEFAULT_DEVELOPER, DEFAULT_REVIEWER
from .diagnostics import doctor
from .workflow import build_graph
from .workspace import create_worktree, plan_worktree
from .store import TaskStore
from .events import RunRecorder
from .models import TaskSpec, OwnerInfo, OfficeError, ACTIVE, validate_id, MAX_SQLITE_INT
from .ownership import RunGuard, recover_run
from .runtime_paths import resolve_db_path
from dataclasses import asdict


class OfficeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid command arguments")


def make_parser() -> argparse.ArgumentParser:
    parser = OfficeParser(prog="agent-office")
    parser.add_argument("--db", type=Path, help="Local task database; use a separate candidate path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("engines", help="Inspect the initial engine profile without model calls")

    diag = sub.add_parser("doctor", help="Inspect Git, Python and installed agent CLIs")
    diag.add_argument("--require", nargs="*", choices=SUPPORTED_ENGINES,
                      default=list(INITIAL_ENGINES))

    run = sub.add_parser("run", help="Execute a task in a separate Git worktree")
    run.add_argument("--repo", type=Path, required=True, help="Path to Git repository root")
    run.add_argument("--task", required=True, help="Development request in natural language")
    run.add_argument("--developer", choices=SUPPORTED_ENGINES, default=DEFAULT_DEVELOPER)
    run.add_argument("--reviewer", choices=SUPPORTED_ENGINES, default=DEFAULT_REVIEWER)
    run.add_argument("--allow-edits", action="store_true", help="Opt in to editing the isolated worktree")
    run.add_argument("--test-command", nargs="+", default=[sys.executable, "-m", "pytest", "-q"])
    run.add_argument("--max-attempts", type=int, default=2)
    run.add_argument("--timeout", type=int, default=900)
    tasks = sub.add_parser("tasks", help="Read persisted tasks")
    task_sub = tasks.add_subparsers(dest="task_command", required=True)
    task_list = task_sub.add_parser("list")
    task_list.add_argument("--limit", type=int, default=50)
    task_show = task_sub.add_parser("show")
    task_show.add_argument("task_id")
    events = sub.add_parser("events", help="Read ordered events")
    events.add_argument("task_id")
    events.add_argument("--run-id")
    events.add_argument("--after-seq", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    recover = sub.add_parser("recover", help="Mark an interrupted run; never replay it")
    recover.add_argument("--run-id", required=True)
    observer = sub.add_parser("serve", help="Open a loopback-only read-only observer")
    observer.add_argument("--port", type=int, default=8765)
    return parser


def execute_run(args, store, run_id):
    repo = args.repo.expanduser().resolve()
    worktree = plan_worktree(repo, run_id)
    task_id = uuid.uuid4().hex
    recorder = RunRecorder(store, run_id)
    store.create_run(TaskSpec(task_id, str(repo), args.task, args.developer, args.reviewer, args.max_attempts),
                     run_id, worktree, OwnerInfo(platform.node(), uuid.uuid4().hex))
    try:
        worktree = create_worktree(repo, planned=worktree)
        recorder.record(operation_key=f"{run_id}:workspace", event_type="WORKSPACE_READY", payload={"phase": "workspace"})
        adapters = {
            engine: CLIAdapter(engine, timeout=args.timeout)
            for engine in SUPPORTED_ENGINES
        }
        graph = build_graph(adapters, recorder=recorder)
        initial = {
            "task": args.task,
            "workspace": str(worktree.path),
            "developer": args.developer,
            "reviewer": args.reviewer,
            "test_argv": args.test_command,
            "allow_edits": args.allow_edits,
            "attempts": 0,
            "max_attempts": args.max_attempts,
            "status": "NEW",
        }
        result = graph.invoke(initial, config={"configurable": {"thread_id": run_id}})
        state, outcome = {
            "PLAN_ONLY": ("PLAN_ONLY", "PLAN_ONLY"),
            "READY_FOR_HUMAN_REVIEW": ("READY_FOR_HUMAN_REVIEW", "APPROVED"),
            "FAILED": ("FAILED", "DEVELOPER_FAILED"),
            "TEST_FAILED": ("FAILED", "TEST_FAILED"),
            "REVIEW_FAILED": ("FAILED", "REVIEW_FAILED"),
            "CHANGES_REQUESTED": ("FAILED", "CHANGES_REQUESTED"),
            "NEEDS_HUMAN_REVIEW": ("WAITING_USER", "REVIEW_UNDECIDED"),
            "RECOVERY_REQUIRED": ("RECOVERY_REQUIRED", "INTERRUPTED"),
        }[result["status"]]
        recorder.record(operation_key=f"{run_id}:final", event_type="RUN_FINISHED", state=state, outcome=outcome,
                        attempts=result["attempts"], payload={"phase": "run"})
    except KeyboardInterrupt:
        try:
            if store.get_run(run_id).state in ACTIVE:
                recorder.record(operation_key=f"{run_id}:interrupt", event_type="RECOVERY_REQUIRED",
                                state="RECOVERY_REQUIRED", outcome="INTERRUPTED", payload={"phase": "run"})
        except (sqlite3.Error, OSError, RuntimeError):
            pass  # a later explicit recover can inspect the last durable state
        raise
    return {
        "status": result["status"],
        "task_id": task_id, "run_id": run_id, "state": state, "outcome": outcome,
        "workspace": str(worktree.path),
        "branch": worktree.branch,
        "attempts": result["attempts"],
        "developer_output": result.get("developer_output"),
        "test_output": result.get("test_output"),
        "review_output": result.get("review_output"),
    }


def report_error(code):
    print(json.dumps({"error": {"code": code, "message": {
        "INVALID_ARGUMENT": "Invalid command arguments or task data.", "GIT_FAILED": "Git operation failed.",
        "STORE_IO": "Unable to persist or read task data.", "DB_BUSY": "Database is busy.",
        "DB_NOT_FOUND": "Database does not exist.", "NOT_FOUND": "Task or run does not exist.",
        "RUN_BUSY": "Run is owned by another coordinator.", "HOST_MISMATCH": "Run belongs to another host.",
        "SCHEMA_UNSUPPORTED": "Database schema is not supported.", "INTERRUPTED": "Execution interrupted; inspect recovery state.",
    }.get(code, "Operation failed.")}}), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    try:
        args = make_parser().parse_args(argv)
        if args.command == "doctor":
            return doctor(args.require)
        if args.command == "engines":
            from .engine_status import collect_engine_status
            print(json.dumps(collect_engine_status(), indent=2, ensure_ascii=False))
            return 0
        if args.command == "run" and (not 1 <= args.max_attempts <= MAX_SQLITE_INT or args.timeout < 1):
            raise ValueError("Invalid budget")
        if hasattr(args, "task_id"):
            validate_id(args.task_id)
        if hasattr(args, "run_id") and args.run_id is not None:
            validate_id(args.run_id)
        if hasattr(args, "limit") and not 1 <= args.limit <= (500 if args.command == "events" else 200):
            raise ValueError("Invalid limit")
        if args.command == "events" and not 0 <= args.after_seq <= MAX_SQLITE_INT:
            raise ValueError("Invalid cursor")
        db_path = resolve_db_path(args.db)
        if args.command == "serve":
            from .observer import serve
            serve(db_path, args.port)
            return 0
        if args.command in ("tasks", "events"):
            with TaskStore(db_path, readonly=True) as store:
                if args.command == "tasks":
                    report = {"tasks": [asdict(task) for task in store.list_tasks(limit=args.limit)], "limit": args.limit} if args.task_command == "list" else {"task": asdict(store.get_task(args.task_id))}
                else:
                    task = store.get_task(args.task_id)
                    events = store.list_events(args.task_id, run_id=args.run_id, after_seq=args.after_seq, limit=args.limit+1)
                    report = {"task_id": args.task_id, "run_id": args.run_id or task.current_run_id,
                              "events": [asdict(event) for event in events[:args.limit]],
                              "next_after_seq": events[min(len(events), args.limit)-1].seq if events else args.after_seq,
                              "has_more": len(events) > args.limit}
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0
        if args.command == "recover":
            if not db_path.is_file():
                raise OfficeError("DB_NOT_FOUND")
            with TaskStore(db_path, existing=True) as store:
                run = recover_run(store, args.run_id, host_name=platform.node())
                report = {"run_id": run.run_id, "state": run.state, "outcome": run.outcome}
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0
        run_id = uuid.uuid4().hex
        with RunGuard(db_path, run_id), TaskStore(db_path) as store:
            report = execute_run(args, store, run_id)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["status"] in ("PLAN_ONLY", "READY_FOR_HUMAN_REVIEW") else 1
    except KeyboardInterrupt:
        report_error("INTERRUPTED")
        return 130
    except OfficeError as exc:
        report_error(exc.code)
    except subprocess.CalledProcessError:
        report_error("GIT_FAILED")
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 255
        report_error("DB_BUSY" if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) else "STORE_IO")
    except (ValueError, OverflowError):
        report_error("INVALID_ARGUMENT")
    except (OSError, RuntimeError):
        report_error("STORE_IO")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
