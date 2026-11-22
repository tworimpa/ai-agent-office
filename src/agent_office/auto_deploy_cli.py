"""Local operator enrollment, revocation and unattended policy consumption."""

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from . import auto_deploy, policy_github, release_store, stable_history
from .ownership import RunBusyError


def add_commands(actions):
    for suffix, help_text in (
        ("enable", "Enroll the displayed existing healthy service in the fixed GitHub policy"),
        ("status", "Show the service policy and its latest audit records"),
        ("disable", "Revoke the operator enrollment for this service"),
        ("run", "Check the next main merge and apply it only under the enrolled policy"),
    ):
        command = actions.add_parser("deployment-auto-" + suffix, help=help_text)
        command.add_argument("--target", type=Path, required=True)
        if suffix == "enable":
            command.add_argument(
                "--image", help="Validator image ID (defaults to the current stable candidate's image)"
            )
        if suffix == "run":
            command.add_argument(
                "--watch",
                action="store_true",
                help="Recheck every five minutes until disabled or interrupted",
            )


def run(root, args):
    if args.action == "deployment-auto-status":
        return auto_deploy.status(root, args.target)
    if args.action == "deployment-auto-disable":
        return auto_deploy.disable(root, args.target)
    if args.action == "deployment-auto-enable":
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            raise ValueError("Enable standing authorization in a local operator terminal")
        image = args.image
        if image is None:
            current = stable_history.page(root, limit=1)["current"]
            image = (
                release_store.review(root, current["candidate_id"])["image"]
                if current["candidate_id"]
                else release_store.release_status(root)["image"]
            )
        reviewed = auto_deploy.binding(root, args.target, image)
        print(json.dumps(reviewed, ensure_ascii=False, indent=2), file=sys.stderr)
        print(
            f"{policy_github.REPOSITORY}의 main 병합 PR을 순서대로 확인합니다. 최신 PR 커밋의 필수 검사 8개, "
            "동일한 검증 트리, 고정 이미지 검증과 저널 유휴 상태가 확인되면 이 서비스의 안정 승격·재시작을 위임합니다. "
            "배포 도구 변경도 포함합니다. 이 정책을 등록할까요? [y/N]",
            file=sys.stderr,
            flush=True,
        )
        if sys.stdin.readline().strip().casefold() not in {"y", "yes", "예"}:
            return {"state": "not_enrolled"}
        return auto_deploy.enroll(root, args.target, reviewed)
    if not args.watch:
        return auto_deploy.run_once(root, args.target)
    while True:
        try:
            result = auto_deploy.run_once(root, args.target)
        except (
            OSError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
            subprocess.SubprocessError,
            RunBusyError,
        ) as exc:
            result = {"state": "refused", "reason": str(exc)[:600]}
        if result["state"] in {"disabled", "needs_operator"}:
            return result
        print(json.dumps(result, ensure_ascii=False), flush=True)
        time.sleep(300)
