"""Host-local, interactive review of a managed Studio restart."""

import json
import os
from pathlib import Path
import sys

from . import deployment, release_store, stable_history
from .release_report import _text


def add_commands(actions):
    init = actions.add_parser("deployment-init", help="Register a new operator-owned Studio deployment target")
    init.add_argument("--service", required=True)
    init.add_argument("--port", type=int, required=True)
    init.add_argument("--journal-id", required=True)
    init.add_argument("--runtime-python", type=Path, help="Absolute interpreter path in the trusted runtime environment")
    init.add_argument("--manager", choices=("systemd-user", "systemd", "windows"),
                      default="windows" if os.name == "nt" else "systemd")
    deploy = actions.add_parser("deploy", help="Review and deploy a recorded stable version")
    deploy.add_argument("seq", type=int)
    status = actions.add_parser("deployment-status", help="Read target state and actual Studio health")
    recover = actions.add_parser("deployment-recover", help="Review and reconcile an interrupted deployment")
    for command in (init, deploy, status, recover):
        command.add_argument("--target", type=Path, required=True)


def _phrase(reviewed):
    config = reviewed["config"]
    event = reviewed["versions"]["requested"]
    probe = reviewed["observed"]["probe"]
    prior = probe["identity"]
    origin = f"{prior['seq']} {prior['sha']} {prior['boot_id']}" if prior else probe["state"]
    binding = f"{config['target_id']} {config['manager']} {config['service']}"
    requested = f"{event['seq']} {event['sha']}"
    if reviewed["recover"]:
        previous = reviewed["versions"]["previous"]
        fallback = f"{previous['seq']} {previous['sha']}" if previous else "stop"
        return (f"recover {binding} {reviewed['last']['id']} observed {origin} "
                f"requested {requested} fallback {fallback}")
    return f"deploy {binding} {origin} to {requested}"


def run(root, args):
    target = args.target
    if args.action == "deployment-init":
        return deployment.init_target(root, target, manager=args.manager, service=args.service,
                                      port=args.port, journal_id=args.journal_id, runtime_python=args.runtime_python)
    if args.action == "deployment-status":
        config, _ = deployment.read_selection(target)
        with release_store._db(root) as db:
            stable_history.identity(db, config["release_id"])
        return deployment.public_status(target) | {"recent": deployment.recent_operations(target)}
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise ValueError("Open an interactive operator terminal to confirm the service restart")
    reviewed = deployment.review(root, target, getattr(args, "seq", None),
                                 recover=args.action == "deployment-recover")
    print("재시작으로 진행 중인 작업과 원격 연결이 끊길 수 있습니다. 배포 실패 시 직전 정상 버전을 "
          "다시 시작하며, 첫 배포라면 서비스를 중지합니다.", file=sys.stderr)
    if reviewed["recover"]:
        print("복구 시 현재 부팅과 중단 기록을 검사하고 직전 정상 버전 복원 또는 중지를 확인합니다.", file=sys.stderr)
    details = {"대상 서비스": reviewed["config"], "현재 안정 버전": reviewed["versions"]["current"],
               "배포 요청 버전": reviewed["versions"]["requested"], "복원할 버전": reviewed["versions"]["previous"],
               "서비스 관리자와 실제 응답": reviewed["observed"], "선택된 소스": reviewed["selected"],
               "직전 배포 기록": deployment._view_op(reviewed["last"])}
    print(_text(json.dumps(details, ensure_ascii=False, indent=2)), file=sys.stderr)
    phrase = _phrase(reviewed)
    print("검토한 대상에 적용하려면 아래 문구를 입력하세요.\n" + phrase, file=sys.stderr, flush=True)
    if sys.stdin.readline().rstrip("\r\n") != phrase:
        raise ValueError("Confirmation did not match. Service restart was not requested")
    return deployment.apply_review(root, target, reviewed)
