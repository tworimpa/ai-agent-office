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
    history = actions.add_parser("history", help="Show stable versions, newest first")
    history.add_argument("--before", type=int, help="Return versions strictly before this number")
    history.add_argument("--limit", type=int, default=24, help="Page size (1–100)")
    compare = actions.add_parser("compare", help="Show the complete diff between two stable versions")
    compare.add_argument("from_seq", type=int)
    compare.add_argument("to_seq", type=int)
    rollback = actions.add_parser("rollback", help="Review and restore an earlier stable version in the operator terminal")
    rollback.add_argument("target_seq", type=int)
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
    sync.add_argument("--stable-history", action="store_true",
                      help="Publish release-wide stable transitions, changed paths and eligible text file contents")


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
    from .release_store import decide, review_with_version
    from .release_report import terminal_review
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise ValueError("Open an interactive operator terminal to record a decision")
    record, current, base_seq, release_id = review_with_version(root, args.id)
    seq = current["seq"]
    if record["sha"] != args.sha:
        raise ValueError("The supplied SHA does not match this candidate")
    if args.action == "approve" and record["state"] not in ("passed", "approved"):
        raise ValueError("Approval requires a passed validation for this candidate")
    if args.action == "approve" and record["stale_base"]:
        raise ValueError("Candidate base differs from current stable version. Submit a fresh proposal.")
    print(terminal_review(record), file=sys.stderr)
    print(f"후보 기준 버전 #{base_seq} · 현재 안정 버전 #{seq}", file=sys.stderr)
    phrase = f"{args.action} {args.sha}"
    print(f"검토한 후보에 결정을 기록하려면 다음 문구를 입력하세요.\n{phrase}", file=sys.stderr, flush=True)
    if sys.stdin.readline().rstrip("\r\n") != phrase:
        raise ValueError("Confirmation did not match. No decision was recorded")
    return decide(root, args.id, args.sha, args.action, reviewed_version=current, expected_release_id=release_id)


def _terminal_comparison(review):
    from .release_report import _text
    first, second = review["from"], review["to"]
    return (f"비교 시작 버전 #{first['seq']} {first['sha']}\n"
            f"비교 대상 버전 #{second['seq']} {second['sha']}\n"
            f"시작 버전에서 대상 버전으로의 전체 diff\n{_text(review['diff'])}")


def _human_rollback(root, target_seq):
    from . import stable_history
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise ValueError("Open an interactive operator terminal to review a rollback")
    review = stable_history.review_rollback(root, target_seq)
    current, target = review["from"], review["to"]
    print(f"승인하면 안정 소스가 현재 #{current['seq']}에서 선택한 #{target['seq']} 커밋으로 복원됩니다. "
          "다음 checkout-stable 명령은 복원한 소스를 추출합니다.\n\n"
          + _terminal_comparison(review), file=sys.stderr)
    phrase = f"rollback {current['seq']} {current['sha']} {target['seq']} {target['sha']}"
    print(f"롤백을 기록하려면 다음 문구를 입력하세요.\n{phrase}", file=sys.stderr, flush=True)
    if sys.stdin.readline().rstrip("\r\n") != phrase:
        raise ValueError("Confirmation did not match. No rollback was recorded")
    return stable_history.rollback(root, expected_release_id=review["release_id"],
                                   reviewed_versions=(current, target),
                                   expected_seq=current["seq"], expected_sha=current["sha"],
                                   target_seq=target["seq"], target_sha=target["sha"])


def run_command(args, db_path):
    from . import release_store as releases, stable_history
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
        elif action == "history":
            result = stable_history.page(root, args.limit, args.before)
        elif action == "compare":
            result = stable_history.compare(root, args.from_seq, args.to_seq)
            if sys.stdout.isatty():
                print(_terminal_comparison(result))
                return 0
        elif action == "rollback":
            result = _human_rollback(root, args.target_seq)
        elif action == "studio-sync":
            from .release_view import sync_once
            if args.watch:
                return _watch(db_path, root, args.view, stable_history=args.stable_history)
            result = sync_once(db_path, root, args.view, stable_history=args.stable_history)
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


def _watch(journal, root, view, *, stable_history=False):
    from .ownership import RunBusyError
    from .release_view import PublicationWatch, sync_once
    previous = None
    watch = PublicationWatch()
    try:
        while True:
            try:
                result = sync_once(journal, root, view, watch=watch, stable_history=stable_history)
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
