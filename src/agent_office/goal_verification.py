"""Run-scoped verification policies, baseline measurements and ranked attempts."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .events import snapshot_request
from .goal_checks import recipe, run_check
from .goal_candidates import capture, scope_changes
from . import goal_recovery
from .store import encode, now


def default_policy(test_files=None):
    return validate_policy({
        "active_seconds": 28800, "max_attempts": 6,
        "checks": [{"stage": stage, "host": "local", "timeout_seconds": timeout, "pass_exit_code": 0,
                    "argv": ["python", "-m", "pytest", "-q", *args]}
                   for stage, timeout, args in (("quick", 1800, test_files or ["--lf"]), ("full", 7200, []))],
    })


def validate_policy(value):
    if not isinstance(value, dict) or set(value) != {"active_seconds", "max_attempts", "checks"}:
        raise ValueError("검증 설정의 실행 시간, 시도 횟수, 검증 단계를 확인하세요.")
    def integer(item, lower, upper):
        return type(item) is int and lower <= item <= upper
    if not integer(value["active_seconds"], 1, 604800) or not integer(value["max_attempts"], 1, 32):
        raise ValueError("검증 시간은 1초~7일, 시도 횟수는 1~32회입니다.")
    checks = value["checks"]
    if not isinstance(checks, list) or not 2 <= len(checks) <= 16:
        raise ValueError("빠른 검증과 전체 검증을 포함해 최대 16개 단계를 지정하세요.")
    stages = []
    for check in checks:
        if (not isinstance(check, dict) or set(check) != {"stage", "host", "argv", "timeout_seconds", "pass_exit_code"}
                or check["stage"] not in ("quick", "full", "journey")
                or not isinstance(check["host"], str) or not re.fullmatch(r"[\w.-]{1,253}", check["host"])
                or not integer(check["timeout_seconds"], 1, 86400) or not integer(check["pass_exit_code"], 0, 123)):
            raise ValueError("검증 종류, 호스트, 제한 시간과 성공 종료 코드를 확인하세요.")
        if recipe(check["argv"]) != "host" and check["pass_exit_code"] != 0:
            raise ValueError("기본 검증 명령의 성공 종료 코드는 0입니다.")
        if "--lf" in check["argv"] and check["stage"] != "quick":
            raise ValueError("기준 실패 파일 선택은 빠른 검증에서만 사용할 수 있습니다.")
        stages.append(("quick", "full", "journey").index(check["stage"]))
    if stages != sorted(stages) or not {0, 1}.issubset(stages):
        raise ValueError("빠른 검증, 전체 검증, 사용자 여정 순서로 지정하세요.")
    if any("--lf" in c["argv"] for c in checks) and not any(
            c["stage"] == "full" and recipe(c["argv"]) == "pytest" for c in checks):
        raise ValueError("실패 파일 자동 선택에는 전체 pytest 검증 단계가 필요합니다.")
    return json.loads(json.dumps(value))


def init_schema(db):
    from . import goal_reproduction
    goal_reproduction.schema(db)
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_checks (
        run_id TEXT NOT NULL REFERENCES runs(run_id), attempt INTEGER NOT NULL, position INTEGER NOT NULL,
        receipt TEXT NOT NULL, cases TEXT NOT NULL, PRIMARY KEY(run_id,attempt,position))""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_candidates (
        run_id TEXT NOT NULL REFERENCES runs(run_id), attempt INTEGER NOT NULL, record TEXT NOT NULL,
        patch TEXT NOT NULL, PRIMARY KEY(run_id,attempt))""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_stops (
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id), reason TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_reviews (
        run_id TEXT NOT NULL REFERENCES runs(run_id), attempt INTEGER NOT NULL, kind TEXT NOT NULL, digest TEXT NOT NULL,
        status TEXT NOT NULL, output TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(run_id,attempt,kind))""")
    for action in ("UPDATE", "DELETE"):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_goal_candidate_{action}
            BEFORE {action} ON studio_goal_candidates BEGIN SELECT RAISE(ABORT,'Immutable candidate'); END""")
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_goal_review_{action}
            BEFORE {action} ON studio_goal_reviews BEGIN SELECT RAISE(ABORT,'Immutable review'); END""")


def describe(db, run_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_goal_checks'").fetchone():
        return None
    checks = [json.loads(row[0]) for row in db.execute(
        "SELECT receipt FROM studio_goal_checks WHERE run_id=? ORDER BY attempt,position", (run_id,))]
    candidates = [json.loads(row[0]) for row in db.execute(
        "SELECT record FROM studio_goal_candidates WHERE run_id=? ORDER BY attempt", (run_id,))]
    stop = db.execute("SELECT reason FROM studio_goal_stops WHERE run_id=?", (run_id,)).fetchone()
    if not (checks or candidates or stop):
        return None
    for candidate in candidates:
        candidate.update(review_status="pending", review_output="")
        for kind, status, output, at in db.execute(
                "SELECT kind,status,output,at FROM studio_goal_reviews WHERE run_id=? AND attempt=? AND digest=?",
                (run_id, candidate["attempt"], candidate["digest"])):
            candidate.update({kind + "_status": status, kind + "_output": output, kind + "_at": at})
    eligible = [candidate for candidate in candidates if candidate["eligible"]]
    best = max(eligible, key=lambda c: (c["review_status"] == "approved", c["status"] == "passed", c["score"],
                                        c["review_status"] != "changes_requested"), default=None)
    return {"checks": checks, "candidates": candidates, "best_attempt": best["attempt"] if best else None,
            "stop_reason": stop[0] if stop else ""}


def candidate_diff(db, run_id, attempt):
    present = db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_goal_candidates'").fetchone()
    row = (db.execute("SELECT patch FROM studio_goal_candidates WHERE run_id=? AND attempt=?",
                      (run_id, attempt)).fetchone() if present else None)
    if row is None:
        from .models import OfficeError
        raise OfficeError("NOT_FOUND")
    return row[0]


class GoalVerification:
    def __init__(self, store, run_id, worktree, revision, folders, assignment):
        self.store = store
        self.db = store.connection
        self.id = run_id
        self.root = Path(worktree)
        self.revision = revision
        self.folders = folders
        self.policy = validate_policy(assignment["goal_policy"])
        self.reproduce_required = assignment.get("reproduce_required", False)
        self.needs_reproducer = False
        init_schema(self.db)

    def stop(self, reason):
        self.db.execute("INSERT OR REPLACE INTO studio_goal_stops VALUES(?,?)", (self.id, reason))

    def remaining(self):
        used = goal_recovery.timing(self.db, self.id)["active_seconds"]
        return self.policy["active_seconds"] - used

    def checkpoint(self, stage, state):
        goal_recovery.checkpoint(self.db, self.id, stage, state)

    def invalidate(self):
        goal_recovery.invalidate_checkpoint(self.db, self.id)

    def review(self, attempt, kind, status, output):
        row = self.db.execute("SELECT record FROM studio_goal_candidates WHERE run_id=? AND attempt=?",
                              (self.id, attempt)).fetchone()
        if not row:
            return False
        digest = json.loads(row[0])["digest"]
        try:
            valid = bool(digest and capture(self.root, self.revision, self.folders)[2] == digest)
        except (OSError, ValueError):
            valid = False
        self.db.execute("INSERT INTO studio_goal_reviews VALUES(?,?,?,?,?,?,?)",
                        (self.id, attempt, kind, digest, status if valid else "workspace_changed",
                         snapshot_request(output), now()))
        return valid

    def _rows(self, attempt):
        return [(json.loads(receipt), json.loads(cases)) for receipt, cases in self.db.execute(
            "SELECT receipt,cases FROM studio_goal_checks WHERE run_id=? AND attempt=? ORDER BY position",
            (self.id, attempt))]

    def _run(self, attempt, position, check, *, workspace=None, baseline=None):
        self.invalidate()
        key = (self.id, attempt, position)
        def save(receipt, cases):
            receipt.update(attempt=attempt, position=position, target_host=check["host"],
                           recipe_argv=check["argv"],
                           timeout_seconds=check["timeout_seconds"], pass_exit_code=check["pass_exit_code"])
            self.db.execute("""INSERT INTO studio_goal_checks VALUES(?,?,?,?,?)
                ON CONFLICT(run_id,attempt,position) DO UPDATE SET receipt=excluded.receipt,cases=excluded.cases""",
                (*key, encode(receipt), encode(cases)))
        timeout = min(check["timeout_seconds"], self.remaining())
        if timeout <= 0:
            raise TimeoutError("목표의 작업 시간 예산을 소진했습니다.")
        import platform
        if check["host"].casefold() in {"local", platform.node().casefold()}:
            receipt, cases = run_check(workspace or self.root, check, timeout=timeout,
                                       on_start=lambda item: save(item, {}))
        else:
            from .goal_peer import run_remote
            receipt, cases = run_remote(self.store, self.id, attempt, position, workspace or self.root,
                                       self.revision, check, timeout=timeout, on_start=lambda item: save(item, {}))
        base = baseline or {}
        previous = None
        if attempt > 1:
            row = self.db.execute("SELECT receipt,cases FROM studio_goal_checks WHERE run_id=? AND attempt=? AND position=?",
                                  (self.id, attempt - 1, position)).fetchone()
            if row and json.loads(row[0])["status"] not in {"timeout", "unverified", "running"}:
                previous = json.loads(row[1])
        reference = previous if previous is not None else {}
        removed = sorted(base.keys() - cases.keys())
        skipped = sorted(k for k in base.keys() & cases.keys() if cases[k] == "skipped" and base[k] != "skipped")
        if previous is not None:
            removed = sorted(set(removed) | (previous.keys() - cases.keys()))
            skipped = sorted(set(skipped) | {k for k in previous.keys() & cases.keys()
                                             if cases[k] == "skipped" and previous[k] != "skipped"})
        fixed = sorted(k for k in base.keys() & cases.keys() if base[k] == "failed" and cases[k] == "passed")
        new = sorted(k for k, v in cases.items() if v == "failed" and base.get(k) != "failed")
        repaired = sorted(k for k in reference.keys() & cases.keys() if reference[k] == "failed" and cases[k] == "passed")
        regressions = sorted(k for k, status in cases.items()
                             if previous is not None and reference.get(k) != "failed" and status == "failed")
        remaining = sorted(k for k in reference.keys() & cases.keys() if reference[k] == cases[k] == "failed")
        receipt.update(
            scope_changes=[*("누락: " + k for k in removed[:50]), *("건너뜀: " + k for k in skipped[:50])],
            removed_count=len(removed), newly_skipped_count=len(skipped),
            failed_ids=[k for k, v in cases.items() if v == "failed"][:50],
            fixed_ids=fixed[:50], fixed_count=len(fixed), new_failures=new[:50], new_failure_count=len(new),
            previous_attempt=attempt-1 if previous is not None else None,
            fixed_since_previous=repaired[:50], fixed_since_previous_count=len(repaired),
            failures_since_previous=regressions[:50], failures_since_previous_count=len(regressions),
            still_failing=remaining[:50], still_failing_count=len(remaining),
        )
        if (removed or skipped) and receipt["status"] in {"passed", "failed"}:
            receipt["status"] = "scope_reduced"
        save(receipt, cases)
        return receipt, cases

    def _focused(self, check):
        # Keep the repro scope pinned across attempts. pytest's mutable cache
        # is not authoritative evidence about the previous baseline.
        rows = self.db.execute("SELECT receipt,cases FROM studio_goal_checks WHERE run_id=? AND attempt=0 "
                               "ORDER BY position", (self.id,))
        for receipt, raw in rows:
            result = json.loads(receipt)
            if result["stage"] != "full" or recipe(result["recipe_argv"]) != "pytest":
                continue
            cases = json.loads(raw)
            failed = sorted(k.split("::", 1)[0] for k, status in cases.items() if status == "failed")
            files = list(dict.fromkeys(failed or sorted(k.split("::", 1)[0] for k in cases)[:1]))[:16]
            if files:
                return dict(check, argv=[*check["argv"][:-1], "--", *files])
        raise ValueError("기준 전체 검증에서 관련 pytest 파일을 확인할 수 없습니다.")

    def baseline(self):
        records = []
        if capture(self.root, self.revision, self.folders)[0]:
            reason = "기준 측정 전에 작업 트리에 변경이 있습니다."
            self.stop(reason)
            return False, reason
        checks = list(enumerate(self.policy["checks"]))
        if any("--lf" in c["argv"] for _, c in checks):
            checks.sort(key=lambda item: (0 if item[1]["stage"] == "full" else 1 if item[1]["stage"] == "quick" else 2))
        for position, check in checks:
            if "--lf" in check["argv"]:
                check = self._focused(check)
            receipt, _ = self._run(0, position, check)
            records.append(receipt)
            if receipt["status"] in {"timeout", "unverified"} or not receipt["counts"]:
                self.stop("기준 검증을 완료할 수 없습니다. " + receipt["summary"][-500:])
                return False, self.format(records)
        if capture(self.root, self.revision, self.folders)[0]:
            reason = "기준 검증이 소스 파일을 수정했습니다. 변경을 확인하세요."
            self.stop(reason)
            return False, self.format(records) + "\n" + reason
        configured_files = {path for c in self.policy["checks"] if c["stage"] == "quick" and recipe(c["argv"]) == "pytest"
                            for path in c["argv"] if path.endswith(".py")}
        failures = {case for _, cases in self._rows(0) for case, status in cases.items() if status == "failed"}
        reproduction = (any(k.split("::", 1)[0] in configured_files for k in failures)
                        if configured_files else bool(failures))
        if self.reproduce_required and not reproduction:
            self.needs_reproducer = any(
                c["stage"] == "quick" and recipe(c["argv"]) == "pytest" for c in self.policy["checks"])
            self.stop("기준 커밋에서 문제가 재현되지 않았습니다. 실패하는 회귀 테스트 또는 재현 조건이 필요합니다.")
            return False, self.format(records)
        return True, self.format(records)

    @staticmethod
    def format(records):
        return snapshot_request("\n".join(
            f"{r['stage']} · {r['status']} · {r['elapsed_seconds']}s · exit {r['exit_code']} · {r['counts']}"
            + ("\n" + "\n".join(r["failed_ids"][:8] + r["scope_changes"][:8]) if r.get("failed_ids") or r.get("scope_changes") else "")
            + (f"\n직전 시도 대비 해결 {r['fixed_since_previous_count']}건, 새 실패 {r['failures_since_previous_count']}건, "
               f"계속 실패 {r['still_failing_count']}건." if r.get("previous_attempt") is not None else "")
            + ("\n" + r["summary"][-1200:] if r["status"] != "passed" else "")
            for r in records))

    def verify(self, state, meeting):
        from . import goal_reproduction
        attempt = state["attempts"]
        baseline = self._rows(0)
        start = state.get("check_index", 0)
        records = [row[0] for row in self._rows(attempt)]
        if len(records) != start:
            raise ValueError("이전 검증 실행의 종료 상태를 확인해야 합니다.")
        try:
            files, patch, digest = capture(self.root, self.revision, self.folders)
            scope = scope_changes(self.root, self.revision, files)
            added = goal_reproduction.read(self.db, self.id)
            if added and not goal_reproduction.intact(self.root, added[0]):
                scope.append("최초 실패를 증명한 회귀 테스트가 변경되었습니다.")
        except (OSError, ValueError) as exc:
            reason = snapshot_request(str(exc))
            record = dict(attempt=attempt, created_at=now(), digest="", score=0, eligible=False, status="unverified",
                          files=[], hypothesis=snapshot_request(state.get("developer_output", "")),
                          change="", scope_changes=[reason], next_action=reason, fixed_count=0, new_failure_count=0)
            self.db.execute("INSERT INTO studio_goal_candidates VALUES(?,?,?,?)", (self.id, attempt, encode(record), ""))
            self.stop(reason)
            return False, False, False, reason
        ordered = list(enumerate(self.policy["checks"]))
        if added:
            ordered.insert(0, (len(ordered), added[1]))
        for index, (pos, check) in enumerate(ordered[start:], start):
            if "--lf" in check["argv"]:
                check = self._focused(check)
            receipt, cases = self._run(attempt, pos, check, baseline=baseline[pos][1])
            records.append(receipt)
            if meeting:
                meeting.record_goal_cases(cases, check["stage"])
            if receipt["status"] != "passed":
                break
            if index + 1 < len(ordered):
                self.checkpoint("test", dict(state, check_index=index + 1))
        passed = len(records) == len(ordered) and all(r["status"] == "passed" for r in records)
        interrupted = any(r["status"] in ("timeout", "unverified") for r in records)
        scope_reduced = bool(scope) or any(r["scope_changes"] for r in records)
        explanation = ""
        try:
            if capture(self.root, self.revision, self.folders)[2] != digest:
                raise ValueError("검증 도중 소스가 변경되어 검증 결과를 후보에 연결할 수 없습니다.")
            if not files:
                explanation = "기준 커밋에 대해 검토할 변경이 없습니다."
        except (OSError, ValueError) as exc:
            files, patch, digest = [], "", ""
            explanation = str(exc)
        eligible = bool(digest and not scope_reduced and not interrupted)
        passed = passed and eligible
        fixed = sum(r["fixed_count"] for r in records)
        regressions = sum(r["new_failure_count"] for r in records)
        stages_passed = sum(r["status"] == "passed" for r in records)
        score = int(passed) * 1000000 + stages_passed * 10000 + fixed * 10 - regressions * 10
        last = describe(self.db, self.id) or {}
        prior = last.get("candidates", [])
        rejected = passed and any(c["digest"] == digest and c["review_status"] == "changes_requested" for c in prior)
        if rejected:
            passed = False
            explanation = "독립 검토가 수정을 요청한 후보와 변경 내용이 같습니다."
        no_progress = bool(prior and (rejected or any(c["digest"] == digest and c["score"] >= score for c in prior)
                                     or len(prior) >= 2 and score <= max(c["score"] for c in prior)))
        retry = not passed and not interrupted and not no_progress and attempt < self.policy["max_attempts"]
        reason = ("독립 검토가 필요합니다." if passed else
                  "검증 종료 상태를 확인하세요." if interrupted else
                  "동일하거나 더 나은 검증 결과가 있는 후보를 보존했습니다." if no_progress else
                  "작업 시도 예산을 소진했습니다." if not retry else "실패 원인을 수정하고 다시 검증합니다.")
        if explanation:
            reason = explanation
        if scope:
            reason += " " + ", ".join(scope)
        if not retry:
            self.stop(reason)
        record = dict(attempt=attempt, created_at=now(), digest=digest, score=score, eligible=eligible,
                      status="passed" if passed else "scope_reduced" if scope_reduced else "changes_requested" if rejected else "failed",
                      files=files, hypothesis=snapshot_request(state.get("developer_output", "")),
                      change=", ".join(files), scope_changes=scope, next_action=reason,
                      fixed_count=fixed, new_failure_count=regressions)
        self.db.execute("INSERT INTO studio_goal_candidates VALUES(?,?,?,?)", (self.id, attempt, encode(record), patch))
        return passed, interrupted, retry, self.format(records) + "\n" + reason
