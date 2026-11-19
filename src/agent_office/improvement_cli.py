"""Host-local operator commands for improvements to the stable Office source."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from .engines import DEFAULT_DEVELOPER, SUPPORTED_ENGINES


def add_commands(sub):
    improve = sub.add_parser("improve", help="Propose, verify and promote Office improvements")
    improve.add_argument("--release", type=Path, help="Operator-owned release directory")
    actions = improve.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init", help="Create a release store from a trusted baseline")
    init.add_argument("--repo", type=Path, required=True)
    init.add_argument("--revision", required=True, help="Initial stable commit")
    init.add_argument("--image", required=True, help="Trusted local validator image ID (sha256:...)")

    propose = actions.add_parser("propose", help="Ask an agent for a patch citing selected completed runs")
    propose.add_argument("--repo", type=Path, required=True)
    propose.add_argument("--base", required=True, help="Full stable source commit ID")
    propose.add_argument("--run", dest="runs", action="append", required=True)
    propose.add_argument("--engine", choices=SUPPORTED_ENGINES, default=DEFAULT_DEVELOPER)
    propose.add_argument("--out", type=Path, required=True, help="New proposal JSON file")
    submit = actions.add_parser("submit", help="Import a proposal as an immutable candidate")
    submit.add_argument("file", type=Path)
    submit.add_argument("--image", help="Pin a trusted validator with change-specific regression tests")
    actions.add_parser("list", help="List candidates and decisions")
    actions.add_parser("status", help="Show the current stable commit and default validation image")
    verify = actions.add_parser("verify", help="Run isolated regressions for this candidate")
    verify.add_argument("id")
    review = actions.add_parser("review", help="Read the proposal, complete diff, validation and decision history")
    review.add_argument("id")
    review.add_argument("--html", type=Path, help="Save a standalone Korean review report to a new file")
    for verb in ("approve", "reject"):
        decision = actions.add_parser(verb, help=f"Review and {verb} an exact candidate in the operator terminal")
        decision.add_argument("id")
        decision.add_argument("--sha", required=True, help="Full reviewed candidate commit ID")
    export = actions.add_parser("checkout-stable", help="Materialize the approved stable source in a new directory")
    export.add_argument("dest", type=Path)
    sync = actions.add_parser("studio-sync", help="Import generated Studio jobs, validate and publish a read-only view")
    sync.add_argument("--view", type=Path, required=True, help="Operator-owned SQLite publication outside Git and the release store")
    sync.add_argument("--watch", action="store_true", help="Keep the view up to date until Ctrl+C")


def _write_new(path, content):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def _read_proposal(path):
    from .away_evidence import unique_object
    with path.open("rb") as stream:
        raw = stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError("Proposal file exceeds 256 KiB")
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object)


def _human_decision(root, args):
    from .release_store import decide, review
    from .release_report import terminal_review
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise ValueError("Open an interactive operator terminal to record a decision")
    record = review(root, args.id)
    if record["sha"] != args.sha:
        raise ValueError("The supplied SHA does not match this candidate")
    if args.action == "approve" and record["state"] not in ("passed", "approved"):
        raise ValueError("Approval requires a passed validation for this candidate")
    print(terminal_review(record), file=sys.stderr)
    phrase = f"{args.action} {args.sha}"
    print(f"검토한 후보에 결정을 기록하려면 다음 문구를 입력하세요.\n{phrase}", file=sys.stderr, flush=True)
    if sys.stdin.readline().rstrip("\r\n") != phrase:
        raise ValueError("Confirmation did not match. No decision was recorded")
    return decide(root, args.id, args.sha, args.action)


def run_command(args, db_path):
    from . import release_store as releases
    from .improvement_proposal import NoImprovementSuggested, create_proposal, validate_proposal, verify_history
    from .release_report import render_review
    root = args.release
    action = args.action
    try:
        if action == "propose":
            if root is not None:
                raise ValueError("Run propose in the worker environment without --release")
            if args.out.exists():
                raise FileExistsError("Choose a new file for the proposal")
            proposal = create_proposal(db_path, args.repo, args.base, args.runs, args.engine)
            _write_new(args.out, json.dumps(proposal, ensure_ascii=False, indent=2) + "\n")
            result = {"file": str(args.out), "base": proposal["base"], "title": proposal["title"]}
        elif root is None:
            raise ValueError("Specify the operator directory with improve --release PATH")
        elif action == "init":
            result = releases.init_release(root, args.repo, args.revision, args.image)
        elif action == "submit":
            proposal = validate_proposal(_read_proposal(args.file))
            verify_history(db_path, proposal)
            result = releases.submit(root, proposal, image=args.image)
        elif action == "list":
            result = {"candidates": releases.list_candidates(root)}
        elif action == "status":
            result = releases.release_status(root)
        elif action == "studio-sync":
            from .release_view import sync_once
            if args.watch:
                return _watch(db_path, root, args.view)
            result = sync_once(db_path, root, args.view)
        elif action == "verify":
            result = releases.verify_candidate(root, args.id)
        elif action == "review":
            result = releases.review(root, args.id)
            if args.html:
                _write_new(args.html, render_review(result))
                result = {key: result[key] for key in ("id", "sha", "state")} | {"file": str(args.html)}
        elif action in ("approve", "reject"):
            result = _human_decision(root, args)
        else:
            result = releases.checkout_stable(root, args.dest)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if action == "verify" and result["state"] != "passed" else 0
    except NoImprovementSuggested:
        print(json.dumps({"status": "no_proposal", "runs": args.runs}))
        return 0
    except (ValueError, OSError, RuntimeError, EOFError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2


def _watch(journal, root, view):
    from .ownership import RunBusyError
    from .release_view import PublicationWatch, sync_once
    previous = None
    watch = PublicationWatch()
    try:
        while True:
            try:
                result = sync_once(journal, root, view, watch=watch)
                key = {k: v for k, v in result.items() if k != "updated_at"}
                if key != previous:
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                previous = key
            except (RunBusyError, sqlite3.OperationalError, OSError) as exc:
                watch.close()
                message = f"Studio publication delayed: {exc}. Retrying in 5 seconds."
                if previous != message:
                    print(message, file=sys.stderr, flush=True)
                previous = message
            time.sleep(5)
    except KeyboardInterrupt:
        return 0
    finally:
        watch.close()
