"""Local command-line entry point for Agent Office AO-000."""

from __future__ import annotations

import argparse
import json
import sys
import subprocess
import uuid
import os
import platform
import sqlite3
from pathlib import Path

from .adapters import CLIAdapter, SUPPORTED_ENGINES
from .diagnostics import doctor
from .workflow import build_graph
from .workspace import create_worktree, plan_worktree
from .store import TaskStore
from .events import RunRecorder
from .models import TaskSpec, OwnerInfo, OfficeError, ACTIVE
from .ownership import RunGuard


class OfficeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid command arguments")


def make_parser() -> argparse.ArgumentParser:
    parser = OfficeParser(prog="agent-office")
    parser.add_argument("--db", type=Path, help="Local task database; use a separate candidate path")
    sub = parser.add_subparsers(dest="command", required=True)

    diag = sub.add_parser("doctor", help="Inspect Git, Python and installed agent CLIs")
    diag.add_argument("--require", nargs="*", choices=SUPPORTED_ENGINES,
                      default=list(SUPPORTED_ENGINES))

    run = sub.add_parser("run", help="Execute a task in a separate Git worktree")
    run.add_argument("--repo", type=Path, required=True, help="Path to Git repository root")
    run.add_argument("--task", required=True, help="Development request in natural language")
    run.add_argument("--developer", choices=SUPPORTED_ENGINES, default="claude")
    run.add_argument("--reviewer", choices=SUPPORTED_ENGINES, default="codex")
    run.add_argument("--allow-edits", action="store_true", help="Opt in to editing the isolated worktree")
    run.add_argument("--test-command", nargs="+", default=[sys.executable, "-m", "pytest", "-q"])
    run.add_argument("--max-attempts", type=int, default=2)
    run.add_argument("--timeout", type=int, default=900)
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
        if args.max_attempts < 1 or args.timeout < 1:
            raise ValueError("Invalid budget")
        db_path = args.db or (Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "AI-Agent-Office/stable/tasks.sqlite3" if os.name == "nt" else Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "ai-agent-office/stable/tasks.sqlite3")
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
    except ValueError:
        report_error("INVALID_ARGUMENT")
    except (OSError, RuntimeError):
        report_error("STORE_IO")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
