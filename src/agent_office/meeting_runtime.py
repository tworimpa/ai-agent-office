"""Coordinate a room meeting within the supervisor's existing run and worktree."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from xml.etree import ElementTree

from . import meeting_store
from .meeting import conduct_meeting
from .meeting_parallel import execute_parallel
from .models import ACTIVE
from .store import TaskStore
from .studio_inbox import InboxChannel


class MeetingFailure(RuntimeError):
    pass


class RoomMeeting:
    def __init__(self, path, run_id, workspace, task, developer, reviewer, folders, adapters, *, workers=None):
        self.path = path
        self.run_id = run_id
        self.workspace = workspace
        self.task = task
        self.developer = developer
        self.reviewer = reviewer
        self.folders = folders
        self.workers = workers
        self.adapters = adapters
        self.applied_work = ""

    def write(self, operation, *args, **kwargs):
        with TaskStore(self.path, existing=True) as store, store._write():
            return operation(store.connection, self.run_id, *args, **kwargs)

    def snapshot(self):
        with TaskStore(self.path, existing=True, readonly=True) as store:
            return meeting_store.describe(store.connection, self.run_id)

    def wait_for_plan(self):
        while True:
            with TaskStore(self.path, existing=True, readonly=True) as store:
                row = meeting_store.summary(store.connection, self.run_id)
                if row["status"] == "approved":
                    return
                if row["status"] != "awaiting_review" or store.get_run(self.run_id).state not in ACTIVE:
                    raise MeetingFailure("계획을 확정하기 전에 회의가 종료되었습니다.")
            time.sleep(0.25)

    def consult(self, request, record):
        try:
            return conduct_meeting(request, self.workspace, self.developer, self.reviewer,
                                   adapters=self.adapters, folders=self.folders, record=record, workers=self.workers)
        except ValueError as exc:
            raise MeetingFailure(str(exc)) from exc
        except OSError as exc:
            raise MeetingFailure("담당자의 회의 응답이나 근거를 확인할 수 없습니다.") from exc

    def begin(self):
        def record(role, engine, opinion):
            self.write(meeting_store.contribute, role, engine, opinion)

        plan = self.consult(self.task, record)
        self.write(meeting_store.propose, plan)
        self.wait_for_plan()

    def context(self):
        meeting = self.snapshot()
        return json.dumps({"revision": meeting["revision"], "plan": meeting["plan"], "answers": meeting["answers"],
                           "verification": meeting["verification"]}, ensure_ascii=False)

    def dispatch(self, role_guidance):
        current = self.snapshot()
        bound = self.workers.bind("developer","parallel_implementation",1) if self.workers else None
        engine = bound.engine if bound else self.developer
        def record(task_id, result):
            self.write(meeting_store.record_work, current["revision"], task_id, engine, result)

        def open_inbox(task_id=None):
            # Keep each child's session approvals separate. Only the primary
            # developer consumes directions addressed to the overall role.
            return InboxChannel(self.path, self.run_id, "developer", engine, goal_task_id=task_id)

        try:
            from .goal_recovery import is_goal
            with TaskStore(self.path, existing=True, readonly=True) as store:
                independent = is_goal(store.connection, self.run_id)
            self.applied_work = execute_parallel(
                current["plan"], self.workspace, {engine:bound} if bound else self.adapters, engine,
                task=self.task + "\n" + role_guidance + "\nAccepted plan and decisions:\n" + self.context(),
                record=record, open_inbox=open_inbox, independent=independent)
        except (OSError, ValueError) as exc:
            raise MeetingFailure("병렬 하위 작업을 완료하지 못했습니다. 회의의 하위 작업 기록을 확인하세요.") from exc

    def update_directions(self, directions):
        # The inbox can be consuming async provider stdio on this thread. Other
        # CLI adapters may create their own asyncio loops for the consultation.
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(self._update_directions, directions).result()

    def _update_directions(self, directions):
        for direction in directions:
            previous = self.snapshot()
            opinions = []
            request = (self.task + "\nCurrent accepted plan and decisions:\n" + self.context()
                       + "\nOperator direction for " + direction["role"] + ":\n" + direction["message"]
                       + "\nUpdate affected tasks and verification. Preserve previously decided behavior. "
                         "Identify work that needs checking again; this direction grants no additional permissions.")
            plan = self.consult(request, lambda role, engine, text: opinions.append(
                {"role": role, "engine": engine, "text": text}))
            self.write(meeting_store.revise, previous["revision"], plan, direction["message"], direction["id"],
                       contributions=opinions)
            self.wait_for_plan()
        return self.context()

    @contextmanager
    def test_command(self, argv):
        with TemporaryDirectory(prefix="office-meeting-tests-") as temp:
            report = Path(temp) / "results.xml"
            index = argv.index("--") if "--" in argv else len(argv)
            command = [*argv[:index], "-o", "junit_family=xunit1", "--junitxml", str(report), *argv[index:]]
            version = self.snapshot()
            try:
                yield command
            except Exception:
                self.note_unverified("지정 테스트를 완료하지 못했습니다. 실행의 테스트 결과를 확인하세요.")
                raise
            else:
                self.record_test_results(version, report)

    def record_test_results(self, version, report):
        files = {}
        try:
            if report.stat().st_size <= 4 * 1024 * 1024:
                for case in ElementTree.parse(report).iter("testcase"):
                    path = case.get("file", "").replace("\\", "/")
                    results = files.setdefault(path, [])
                    if case.find("failure") is not None or case.find("error") is not None:
                        results.append("failed")
                    elif case.find("skipped") is not None:
                        results.append("skipped")
                    else:
                        results.append("passed")
        except (OSError, ValueError, ElementTree.ParseError):
            pass
        self._record_files(version, files)

    def record_goal_cases(self, cases, stage):
        files = {}
        for identifier, status in cases.items():
            path = identifier.split("::", 1)[0]
            files.setdefault(path, []).append(status)
        if files:
            self._record_files(self.snapshot(), files, covered_only=True)

    def _record_files(self, version, files, *, covered_only=False):
        for criterion in version["plan"]["criteria"]:
            paths = criterion.get("test_files", [])
            if covered_only and not any(path in files for path in paths):
                continue
            statuses = [status for path in paths for status in files.get(path, [])]
            if "failed" in statuses:
                status, reason = "failed", "지정한 테스트에서 실패가 기록됐습니다."
            elif paths and all(files.get(path) for path in paths) and set(statuses) == {"passed"}:
                status, reason = "passed", f"지정한 테스트 {len(statuses)}건이 통과했습니다."
            else:
                status, reason = "not_verified", "이 조건에 연결된 테스트 전체의 통과 기록이 없습니다."
            self.write(meeting_store.verify, version["revision"], criterion["criterion"], status,
                       reason + (" " + ", ".join(paths) if paths else ""))

    def note_unverified(self, reason):
        version = self.snapshot()
        for criterion in version["plan"]["criteria"]:
            self.write(meeting_store.verify, version["revision"], criterion["criterion"], "not_verified", reason)
