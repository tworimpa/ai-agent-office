"""Local command-line entry point for Agent Office AO-000."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from .adapters import CLIAdapter, SUPPORTED_ENGINES
from .diagnostics import doctor
from .workflow import build_graph
from .workspace import create_worktree


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-office")
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


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    if args.command == "doctor":
        return doctor(args.require)
    if args.max_attempts < 1 or args.timeout < 1:
        print("max-attempts and timeout must be positive", file=sys.stderr)
        return 2

    repo = args.repo.expanduser().resolve()
    try:
        worktree = create_worktree(repo)
        adapters = {
            engine: CLIAdapter(engine, timeout=args.timeout)
            for engine in SUPPORTED_ENGINES
        }
        graph = build_graph(adapters)
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
        result = graph.invoke(initial, config={"configurable": {"thread_id": str(uuid.uuid4())}})
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Agent Office error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps({
        "status": result["status"],
        "workspace": str(worktree.path),
        "branch": worktree.branch,
        "attempts": result["attempts"],
        "developer_output": result.get("developer_output"),
        "test_output": result.get("test_output"),
        "review_output": result.get("review_output"),
    }, indent=2, ensure_ascii=False))
    return 0 if result["status"] in ("PLAN_ONLY", "READY_FOR_HUMAN_REVIEW") else 1


if __name__ == "__main__":
    raise SystemExit(main())
