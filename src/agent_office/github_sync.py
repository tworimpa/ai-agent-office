"""Durable reconciliation of external issues with the local Office goal coordinator."""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import re
import threading
import time
from uuid import uuid4

from .github_journal import IssueJournal, SUMMARY_KEYS
from .issue_events import answer_text, check_observation, external_text, fingerprint, label_actor, snapshot
from .models import ACTIVE, OfficeError, validate_id
from .ownership import RunGuard, RunBusyError
from .store import encode, now

PAGES_PER_POLL = 2
ISSUES_PER_POLL = 25

def _code(error):
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z_0-9]{0,63}", code) else "SYNC_FAILED"


def _positive(value):
    return type(value) is int and 0 < value < 2**63


def _cursor(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch("[1-9][0-9]{0,18}", value) or not _positive(int(value)):
        raise ValueError("GitHub 기록의 조회 위치를 확인하세요.")
    return int(value)


def _config(data):
    fields = {"connection_id", "repository_id", "repository", "branch", "folder_index", "label",
              "allowed_actors", "answer_actors", "product_actors", "enabled", "draft_pr", "ci_checks", "ci_retries"}
    cfg = dict(folder_index=0, label="office:ready", allowed_actors=[], answer_actors=[], product_actors=[],
               enabled=False, draft_pr=False, ci_checks=[], ci_retries=0)
    if not isinstance(data, dict) or data.keys() - fields:
        raise ValueError("GitHub 연결 설정을 확인하세요.")
    cfg.update(data)
    if (not all(isinstance(cfg.get(k), str) and cfg[k].strip() and len(cfg[k]) <= 255
                for k in ("connection_id", "repository", "branch", "label"))
        or not _positive(cfg.get("repository_id"))
        or type(cfg["folder_index"]) is not int or cfg["folder_index"] < 0
        or any(type(cfg[k]) is not bool for k in ("enabled", "draft_pr"))
        or type(cfg["ci_retries"]) is not int or not 0 <= cfg["ci_retries"] <= 3
        or not isinstance(cfg["ci_checks"], list) or len(cfg["ci_checks"]) > 32
        or any(not isinstance(v, str) or not v.strip() or len(v) > 160 for v in cfg["ci_checks"])
        or any(not isinstance(cfg[k], list) or len(cfg[k]) > 100 or any(not _positive(v) for v in cfg[k])
               for k in ("allowed_actors", "answer_actors", "product_actors"))
        or cfg["enabled"] and not cfg["allowed_actors"]):
        raise ValueError("GitHub 연결 설정을 확인하세요.")
    return cfg


def _key(binding, issue_id):
    return (binding["instance"], binding["repo_id"], issue_id)


def _fresh(binding, value, url):
    snap = snapshot(value, url)
    return dict(workspace_id=binding["workspace"], issue_id=value["id"], number=value["number"], url=url,
                title=snap["title"], state=value["state"], status="waiting", run_id=None, goal_id=None,
                admission=None, observed=snap, accepted=None, base_commit=None, initial_base=None, restart_required=False,
                closed_event=None, scope_pending=None, scope_delivery=None, restart_key=None,
                pr_number=None, pr_url=None, pr_head=None, candidate_sha=None, comment_id=None,
                publish_state="idle", ci_state=None, retries=0, pending_run=None, last_error=None)


class IssueSync:
    def __init__(self, db_path, registry, office, *, interval=90):
        self.journal = IssueJournal(db_path)
        self.registry = registry
        self.office = office
        self.interval = interval
        self.wake = threading.Condition()
        self.queued = set()
        self.running = set()
        self.closed = False
        self.thread = None

    @contextmanager
    def _guard(self, binding):
        # Distinct connections, workspaces and server processes share this repo lock.
        guard = RunGuard(self.journal.path, fingerprint("external-issues", binding["instance"], binding["repo_id"])[:32])
        with guard:
            if "workspace" in binding:
                current = self.journal.bindings(binding["workspace"])
                if not current or any(current[0][k] != binding[k] for k in ("identity", "instance", "repo_id", "config")):
                    raise OfficeError("BINDING_CHANGED")
            yield

    def configure(self, workspace, data):
        cfg = _config(data)
        client = self.registry.client(cfg["connection_id"]).for_repository(cfg["repository_id"])
        if (cfg["enabled"] or cfg["draft_pr"]) and not client.app_id:
            raise ValueError("작업 실행에는 GitHub App 연결이 필요합니다.")
        instance = client.web_url.rstrip("/").lower()
        with ExitStack() as locks:
            locks.enter_context(RunGuard(self.journal.path, fingerprint("issue-binding", workspace)[:32]))
            old = next(iter(self.journal.bindings(workspace)), None)
            targets = {(instance, cfg["repository_id"])}
            if old:
                targets.add((old["instance"], old["repo_id"]))
            for url, repo_id in sorted(targets):
                locks.enter_context(self._guard(dict(instance=url, repo_id=repo_id)))
            def preflight(db):
                current = db.execute("SELECT config,identity,instance,repo_id FROM github_bindings WHERE workspace=?",
                                     (workspace,)).fetchone()
                expected = (encode(old["config"]), old["identity"], old["instance"], old["repo_id"]) if old else None
                if (tuple(current) if current else None) != expected:
                    raise OfficeError("BINDING_CHANGED")
                if old and (instance, cfg["repository_id"], client.identity, cfg["branch"], cfg["folder_index"]) != (
                        old["instance"], old["repo_id"], old["identity"], old["config"]["branch"], old["config"]["folder_index"]):
                    if db.execute("SELECT 1 FROM github_issues WHERE workspace=? LIMIT 1", (workspace,)).fetchone():
                        raise ValueError("이슈 이력이 있는 작업실의 저장소·공급자·브랜치·폴더 연결은 고정됩니다.")
                if cfg["enabled"] and db.execute("""SELECT 1 FROM github_bindings
                        WHERE enabled=1 AND instance=? AND repo_id=? AND workspace<>?""",
                        (instance, cfg["repository_id"], workspace)).fetchone():
                    raise ValueError("이 저장소의 자동 실행은 다른 작업실에서 사용 중입니다.")

            with self.journal.db() as db:
                preflight(db)
            if cfg["enabled"]:
                # The remote marker names this journal. A lost response is recoverable
                # by the same journal even when no local binding has yet committed.
                self.office.claim_ownership(workspace, cfg, client)
            with self.journal.db(write=True) as db:
                preflight(db)
                db.execute("""INSERT INTO github_bindings(workspace,instance,repo_id,identity,enabled,config)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(workspace) DO UPDATE SET instance=excluded.instance,
                    repo_id=excluded.repo_id,identity=excluded.identity,enabled=excluded.enabled,
                    config=excluded.config,cursor=NULL,last_full=0,retry_at=0""",
                           (workspace, instance, cfg["repository_id"], client.identity, cfg["enabled"], encode(cfg)))
                db.execute("DELETE FROM github_scans WHERE workspace=?", (workspace,))
        return self.state(workspace)

    def state(self, workspace, cursor=None):
        cursor = _cursor(cursor)
        bindings = self.journal.bindings(workspace)
        with self.wake:
            queued = workspace in self.queued or workspace in self.running
        status = dict(last_sync=None, last_error=None, retry_at=None, queued=queued)
        if not bindings:
            return dict(config=None, status=status, issues=[], next_cursor=None)
        binding = bindings[0]
        retry = binding["retry_at"]
        status.update(last_sync=binding["last_sync"], last_error=binding["last_error"],
                      retry_at=datetime.fromtimestamp(retry, timezone.utc).isoformat() if retry else None)
        issues, next_cursor = self.journal.issue_page(binding, cursor)
        return dict(config=binding["config"], status=status, issues=issues, next_cursor=next_cursor)

    def issue_detail(self, workspace, issue_id, cursor=None):
        binding, key = self._target(workspace, issue_id)
        r = self._record(binding, issue_id)
        events, next_cursor = self.journal.events(key, _cursor(cursor))
        public = {k: r[k] for k in SUMMARY_KEYS}
        public.update(admission=r["admission"], initial_base=r.get("initial_base"), events=events)
        if r["scope_pending"]:
            public.update(previous_scope=r["accepted"], proposed_scope=r["observed"])
        return dict(issue=public, next_cursor=next_cursor)

    def sync(self, workspace):
        if not self.journal.bindings(workspace):
            raise ValueError("GitHub 저장소 연결을 먼저 설정하세요.")
        with self.wake:
            self.queued.add(workspace)
            self.wake.notify()
        return {"queued": True}

    def start(self):
        with self.wake:
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, name="github-issue-sync", daemon=True)
                self.thread.start()

    def close(self):
        with self.wake:
            self.closed = True
            self.wake.notify_all()
        if self.thread:
            self.thread.join()

    def _loop(self):
        deadline = 0
        while True:
            with self.wake:
                self.wake.wait_for(lambda: self.closed or self.queued, max(0, deadline - time.monotonic()))
                if self.closed:
                    return
                all_due = time.monotonic() >= deadline
                targets = list(self.queued)
                self.queued.clear()
            if all_due:
                self.run_once()
                deadline = time.monotonic() + self.interval
            else:
                for workspace in targets:
                    self.run_once(workspace)

    def run_once(self, workspace=None):
        for binding in self.journal.bindings(workspace):
            if self.closed or binding["retry_at"] > time.time():
                continue
            try:
                with self._guard(binding):
                    current = self.journal.bindings(binding["workspace"])[0]
                    if current["retry_at"] <= time.time():
                        with self.wake:
                            self.running.add(binding["workspace"])
                        try:
                            self._poll(current)
                        finally:
                            with self.wake:
                                self.running.discard(binding["workspace"])
            except RunBusyError:
                continue
            except Exception as exc:
                self._failure(binding, exc)

    def _failure(self, binding, exc):
        delay = max(0, min(int(getattr(exc, "retry_after", None) or 0), 86400))
        with self.journal.db(write=True) as db:
            if delay:
                db.execute("UPDATE github_bindings SET retry_at=?,last_error=? WHERE identity=?",
                           (time.time() + delay, _code(exc), binding["identity"]))
            else:
                db.execute("UPDATE github_bindings SET last_error=? WHERE workspace=?", (_code(exc), binding["workspace"]))

    def _client(self, binding):
        cfg = binding["config"]
        client = self.registry.client(cfg["connection_id"]).for_repository(cfg["repository_id"])
        if client.identity != binding["identity"] or client.web_url.rstrip("/").lower() != binding["instance"]:
            raise OfficeError("CONNECTION_IDENTITY_CHANGED")
        if cfg["enabled"]:
            self.office.verify_ownership(binding["workspace"], cfg, client)
        repo = client.repository(cfg["repository"])
        if repo.get("id") != binding["repo_id"] or repo.get("full_name", "").lower() != cfg["repository"].lower():
            raise OfficeError("REPOSITORY_IDENTITY_CHANGED")
        return client

    def _poll(self, binding):
        cfg = binding["config"]
        client = self._client(binding)
        workspace = binding["workspace"]
        with self.journal.db() as db:
            scan = db.execute("SELECT * FROM github_scans WHERE workspace=?", (workspace,)).fetchone()
        full = bool(scan["full"]) if scan else time.time() - binding["last_full"] >= 3600
        since = scan["since"] if scan else None if full else binding["cursor"]
        cutoff = scan["cutoff"] if scan else (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        page = scan["page"] if scan else 1
        for _ in range(PAGES_PER_POLL):
            if self.closed:
                return
            result = client.issue_page(cfg["repository"], labels=[cfg["label"]], state="all", page=page,
                                       since=since)
            with self.journal.db(write=True) as db:
                for row in result.items:
                    if "pull_request" in row or not _positive(row.get("id")) or not _positive(row.get("number")):
                        continue
                    self._check_issue(row, row["id"], row["number"])
                    key = _key(binding, row["id"])
                    record = self.journal.get(db, key)
                    if record is None:
                        url = f"{client.web_url.rstrip('/')}/{cfg['repository']}/issues/{row['number']}"
                        record = _fresh(binding, row, url)
                    elif record["workspace_id"] != workspace:
                        if record["goal_id"] or not cfg["enabled"]:
                            continue
                        record["workspace_id"] = workspace
                    elif (record["observed"].get("updated_at") == row.get("updated_at") and
                          record["state"] == row["state"]):
                        continue
                    record["needs_check"] = True
                    self.journal.save_issue(db, key, record)
            if result.next_page is not None and result.next_page <= page:
                raise OfficeError("INVALID_PAGINATION")
            page = result.next_page
            if page is None:
                break
        # Every discovered ID is durable before the page cursor advances. Poll any
        # associated PR and locally running goal even if GitHub filtered out the issue.
        with self.journal.db(write=True) as db:
            if page is not None:
                db.execute("INSERT OR REPLACE INTO github_scans VALUES(?,?,?,?,?)",
                           (workspace, page, since, cutoff, full))
            else:
                db.execute("DELETE FROM github_scans WHERE workspace=?", (workspace,))
                db.execute("UPDATE github_bindings SET cursor=?,last_full=? WHERE workspace=?",
                           (cutoff, time.time() if full else binding["last_full"], workspace))
        scheduled = self.journal.scheduled(binding, time.time(), ISSUES_PER_POLL)
        error = None
        for record in scheduled:
            if self.closed:
                return
            issue_id, number = record["issue_id"], record["number"]
            try:
                issue = client.issue(cfg["repository"], number)
                self._check_issue(issue, issue_id, number)
                self._reconcile(binding, client, issue, record)
                record["last_error"] = None
                record["needs_check"] = False
            except Exception as exc:
                error = _code(exc)
                record["last_error"] = error
                if getattr(exc, "retry_after", None):
                    self.journal.save(_key(binding, issue_id), record)
                    raise
            record["last_checked"] = time.time()
            self.journal.save(_key(binding, issue_id), record)
        with self.journal.db(write=True) as db:
            db.execute("UPDATE github_bindings SET last_sync=?,last_error=?,retry_at=0 WHERE workspace=?",
                       (now(), error, workspace))

    @staticmethod
    def _check_issue(issue, identity, number):
        if (issue.get("id") != identity or issue.get("number") != number or "pull_request" in issue
                or issue.get("state") not in {"open", "closed"}):
            raise OfficeError("ISSUE_IDENTITY_CHANGED")

    @staticmethod
    def _write_actor(client, repo, actor):
        return (actor.get("type") == "User" and bool(actor.get("login")) and
                _positive(actor.get("id")) and client.permission(repo, actor["login"], actor["id"]) in
                {"admin", "maintain", "write"})

    @classmethod
    def _admissible(cls, client, cfg, issue, timeline):
        label = cfg["label"]
        if not any(v.get("name", "").casefold() == label.casefold() for v in issue.get("labels", [])):
            return "label_missing"
        actor_id = label_actor(client, timeline, label)
        if actor_id not in cfg["allowed_actors"]:
            return "label_actor_denied"
        actor = next(e["actor"] for e in reversed(timeline) if (e.get("actor") or {}).get("id") == actor_id)
        if not cls._write_actor(client, cfg["repository"], actor):
            return "label_actor_denied"
        return None

    def _reconcile(self, binding, client, issue, r):
        cfg = binding["config"]
        key = _key(binding, r["issue_id"])
        repo = cfg["repository"]
        timeline = client.timeline(repo, r["number"])
        old_state = r["state"]
        r.update(state=issue["state"], title=external_text(issue["title"]))
        current = snapshot(issue, r["url"])
        previous = r["observed"]
        r["observed"] = current
        if previous["digest"] != current["digest"]:
            change = fingerprint("scope", previous, current)
            material = previous["scope_digest"] != current["scope_digest"]
            self.journal.event(key, change, "goal_changed", "이슈 제목 또는 본문이 변경되었습니다.",
                               "needs_decision" if material and r["goal_id"] else "observed")
            if r["goal_id"] and material:
                r["scope_pending"] = change
                r["scope_delivery"] = None
        closes = [str(e["id"]) for e in timeline if e.get("event") == "closed" and e.get("id")]
        new_close = bool(closes and r["closed_event"] != closes[-1])
        needs_stop = (issue["state"] == "closed" or new_close or
                      old_state == "closed" and issue["state"] == "open" or r.get("lifecycle_pending"))
        if not cfg["enabled"]:
            r.update(status="observing", lifecycle_pending=bool(needs_stop))
            if r["goal_id"]:
                self._discussions(key, cfg, client, r)
            return
        if needs_stop:
            r["restart_required"] = True
            r["restart_key"] = None
            r["closed_event"] = closes[-1] if closes else r["closed_event"]
            self._halt(key, r, "closed" if issue["state"] == "closed" else "restart_required")
            r["lifecycle_pending"] = False
        if issue["state"] == "closed" or r["restart_required"] and not r["restart_key"]:
            if issue["state"] == "closed":
                r["status"] = "closed"
            elif r["status"] not in {"merged", "pull_closed", "scope_accepted_restart_required"}:
                r["status"] = "restart_required"
            if cfg["enabled"] and r["goal_id"]:
                self._discussions(key, cfg, client, r)
                self._comment(key, cfg, client, r, {})
            return

        # A process may have died after a successful local dispatch but before
        # recording the receipt. Reconcile the existing run before considering a new one.
        if r["pending_run"]:
            try:
                self.office.status(r["pending_run"]["id"])
            except OfficeError as exc:
                if exc.code != "NOT_FOUND":
                    raise
            else:
                self._launch(binding, key, r)
        if not r["run_id"] or r["restart_key"] or r["pending_run"] and r["pending_run"]["kind"] == "start":
            denial = self._admissible(client, cfg, issue, timeline)
            if denial:
                r["status"] = denial
                return
            if not r["pending_run"]:
                run_id = uuid4().hex
                r.update(goal_id=r["goal_id"] or run_id, accepted=current, admission=r["admission"] or current,
                         generation_id=run_id,
                         pending_run={"kind": "start", "id": run_id, "snapshot": current},
                         scope_pending=None, scope_delivery=None, status="starting")
            # Reconfirm after a failed callback. A changed request cannot replace a pinned request silently.
            if r["scope_pending"]:
                r["status"] = "waiting_for_user"
                return
            self._launch(binding, key, r)

        pull = None
        if r["pr_number"]:
            pull = client.pull(repo, r["pr_number"])
            self._check_pull(cfg, r, pull)
            if pull.get("merged_at") or pull.get("merged") or pull.get("state") == "closed":
                self._halt(key, r, "merged" if pull.get("merged_at") or pull.get("merged") else "pull_closed")
                self._discussions(key, cfg, client, r)
                self._comment(key, cfg, client, r, {})
                return
        run = self.office.status(r["run_id"])
        state = run.get("state", "unknown")
        r["status"] = ("waiting_for_user" if run.get("question") else
                       "running" if state in ACTIVE else state.lower())
        if r["scope_pending"]:
            r["status"] = "waiting_for_user"
        elif r["scope_delivery"]:
            update = r["scope_delivery"]
            if state in ACTIVE:
                self.office.direction(r["run_id"], self._scope_text(update["snapshot"]), update["id"][:32])
            else:
                r.update(status="scope_accepted_restart_required", restart_required=True)
            r["scope_delivery"] = None
            self.journal.save(key, r)
            if r["restart_required"]:
                self._discussions(key, cfg, client, r)
                self._comment(key, cfg, client, r, run)
                return
        self._discussions(key, cfg, client, r, None if r["scope_pending"] else run.get("question"))

        if pull:
            self._pull_feedback(binding, key, client, r, pull, run)
        if (not r["scope_pending"] and not r["pending_run"] and cfg["draft_pr"] and
                run.get("validated") is True and state == "READY_FOR_HUMAN_REVIEW" and
                (not r["pr_number"] or r.get("office_draft")) and
                r.get("published_run") != r["run_id"] and run.get("id", r["run_id"]) == r["run_id"]):
            # Publish is itself idempotent. A lost ack retries publication of this completed run.
            r["publish_state"] = "draft_pending"
            self.journal.save(key, r)
            live = client.issue(repo, r["number"])
            self._check_issue(live, r["issue_id"], r["number"])
            if (live["state"] != "open" or snapshot(live, r["url"])["digest"] != current["digest"] or
                    self._admissible(client, cfg, live, client.timeline(repo, r["number"]))):
                r["status"] = "draft_authorization_pending"
                return
            receipt = self.office.publish(binding["workspace"], r["run_id"], cfg, client, self._payload(r))
            if not _positive(receipt.get("number")) or not re.fullmatch("[0-9a-f]{40,64}", receipt.get("head_sha") or ""):
                raise OfficeError("INVALID_DRAFT_RECEIPT")
            r["published_run"] = r["run_id"]
            r["office_draft"] = True
            self._link(key, cfg, r, receipt["number"], receipt["head"], receipt["head_sha"])
        self._comment(key, cfg, client, r, run)

    @staticmethod
    def _scope_text(scope):
        return scope["title"] + "\n\n" + scope["body"]

    @staticmethod
    def _payload(r):
        return dict(r["accepted"] or r["observed"], goal_id=r["goal_id"], issue_id=r["issue_id"],
                    observed_scope_digest=r["observed"]["scope_digest"],
                    pr_number=r["pr_number"], pr_head=r["pr_head"], candidate_sha=r["candidate_sha"],
                    generation_id=r.get("generation_id") or r["goal_id"], base_commit=r["base_commit"])

    def _halt(self, key, r, status):
        # Persist the intention before stop. stop() is idempotent for terminal workers.
        ids = [r["run_id"], (r["pending_run"] or {}).get("id")]
        r.update(status=status, restart_required=True)
        self.journal.save(key, r)
        for run_id in filter(None, ids):
            try:
                if self.office.status(run_id).get("state") in ACTIVE:
                    self.office.stop(run_id)
            except OfficeError as exc:
                if exc.code != "NOT_FOUND":
                    raise
        r.update(pending_run=None, scope_delivery=None, scope_pending=None, restart_key=None)
        self.journal.save(key, r)

    def _launch(self, binding, key, r):
        pending = r["pending_run"]
        events = []
        self.journal.save(key, r)
        if pending["kind"] == "start":
            receipt = self.office.start(binding["workspace"], pending["id"], self._payload(r), binding["config"])
            if receipt.get("base_commit"):
                r["base_commit"] = receipt["base_commit"]
                r["initial_base"] = r.get("initial_base") or r["base_commit"]
            if r["restart_key"]:
                events.append((r["restart_key"], "goal_restarted", pending["id"], "accepted"))
            r["restart_key"] = None
            r["restart_required"] = False
        else:
            receipt = self.office.rework(binding["workspace"], r["run_id"], pending["id"], pending["feedback"],
                                         binding["config"], self._payload(r), pending["sha"])
            if receipt and receipt.get("base_commit"):
                r["base_commit"] = receipt["base_commit"]
            events.append((pending["source"], "feedback_applied", pending["sha"], "accepted"))
            if pending["kind"] == "ci":
                r["retries"] += 1
        r.update(run_id=pending["id"], pending_run=None, status="running")
        events.append(("run:" + r["run_id"], "goal_run", r["run_id"], "started"))
        self.journal.save(key, r, events)

    def _discussions(self, key, cfg, client, r, question=None):
        # Only the initial import walks old comments. Hold the same since value
        # across pages and save the next position after any answer is acknowledged.
        page = r.get("discussion_page", 1)
        cutoff = r.get("discussion_cutoff") or (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        result = client.comment_page(cfg["repository"], r["number"], since=r.get("discussion_since"), page=page)
        events = []
        people = []
        for comment in result.items:
            actor = comment.get("user") or {}
            cid = comment.get("id")
            body = comment.get("body")
            if (actor.get("type") != "User" or not _positive(actor.get("id")) or not _positive(cid)
                    or not isinstance(body, str) or client.is_app_actor(comment)):
                continue
            people.append(comment)
            if body.startswith("/office answer "):
                continue
            updated = comment.get("updated_at")
            edited = updated and updated != comment.get("created_at")
            identity = fingerprint("discussion", cid, updated, body)
            preview = " ".join(external_text(body).split())
            preview = "".join(c for c in preview if c.isprintable())
            if len(preview) > 300:
                preview = preview[:299] + "…"
            detail = f"#{cid} · 작성자 {actor['id']} · {preview}"
            events.append((identity, "issue_comment_edited" if edited else "issue_comment", detail, "observed"))
        self._answers(key, cfg, client, r, people, question)
        if result.next_page is None:
            r.update(discussion_since=cutoff, discussion_cutoff=None, discussion_page=1)
        elif result.next_page == page + 1:
            r.update(discussion_cutoff=cutoff, discussion_page=result.next_page)
        else:
            raise OfficeError("INVALID_PAGINATION")
        self.journal.save(key, r, events)

    def _answers(self, key, cfg, client, r, comments, question):
        if not question or question.get("kind") not in {"technical", "product"}:
            return
        actors = cfg["answer_actors"] if question.get("kind") == "technical" else cfg["product_actors"]
        for comment in comments:
            actor = comment.get("user") or {}
            if actor.get("id") not in actors or not actor.get("login"):
                continue
            answer = answer_text(comment, r["goal_id"], question)
            event = fingerprint("answer", comment["id"], question["id"])
            if answer and not self.journal.has(key, event):
                if client.permission(cfg["repository"], actor["login"], actor["id"]) not in {
                        "read", "triage", "write", "maintain", "admin"}:
                    continue
                self.office.answer(r["run_id"], question["id"], answer, event[:32], question["kind"])
                self.journal.event(key, event, "question_answered", str(comment["id"]), "accepted")
                break

    def _check_pull(self, cfg, r, pull):
        base = pull.get("base") or {}
        head = pull.get("head") or {}
        if (pull.get("number") != r["pr_number"] or base.get("ref") != cfg["branch"]
                or (base.get("repo") or {}).get("id") != cfg["repository_id"]
                or (head.get("repo") or {}).get("id") != cfg["repository_id"] or head.get("ref") != r["pr_head"]):
            raise OfficeError("PULL_IDENTITY_CHANGED")

    def _pull_feedback(self, binding, key, client, r, pull, run):
        cfg = binding["config"]
        repo = cfg["repository"]
        head = pull["head"]["sha"]
        if not re.fullmatch(r"[0-9a-f]{40,64}", head):
            raise OfficeError("INVALID_PULL_HEAD")
        trusted = head == r["candidate_sha"]
        rows = [(v, False) for v in client.checks(repo, head)] + [(v, True) for v in client.statuses(repo, head)]
        previous = [sha for sha in dict.fromkeys(
            [r.get("observed_head"), r.get("candidate_sha"), *r.get("previous_heads", [])])
            if sha and sha != head][:2]
        r.update(observed_head=head, previous_heads=previous)
        for sha in previous:
            rows.extend((v, False) for v in client.checks(repo, sha))
            rows.extend((v, True) for v in client.statuses(repo, sha))
        actionable = {}
        seen = set()
        # A rerun can supersede a failure on the same SHA. Only the most recent result per check can trigger.
        rows.sort(key=lambda pair: pair[0].get("id") or 0, reverse=True)
        states = {}
        for value, legacy in rows:
            obs = check_observation(value, head, cfg["ci_checks"], status=legacy)
            lane = (legacy, (value.get("app") or {}).get("id"), obs["name"], obs["sha"])
            classification = obs["classification"]
            if lane in seen:
                classification = "superseded"
            elif not trusted and classification != "stale":
                classification = "unverified_head"
            seen.add(lane)
            self.journal.event(key, obs["id"], obs["kind"], obs["detail"], classification)
            if (trusted and obs["name"] in cfg["ci_checks"] and
                    classification not in {"superseded", "stale"}):
                states.setdefault(obs["name"], set()).add(obs["conclusion"])
            action = fingerprint("repair", r["pr_number"], head)
            if classification == "code_failure" and r["retries"] < cfg["ci_retries"] and not self.journal.has(key, action):
                actionable[action] = {"kind": "ci", "source": action, "sha": head,
                                      "feedback": "검증 실패: " + obs["detail"]}
        conclusions = {v for name in cfg["ci_checks"] for v in states.get(name, {"pending"})}
        if not trusted:
            r["ci_state"] = "unverified_head"
        elif not conclusions:
            r["ci_state"] = None
        elif "failure" in conclusions:
            r["ci_state"] = "failure"
        elif len(conclusions) == 1:
            r["ci_state"] = next(iter(conclusions))
        else:
            r["ci_state"] = "pending" if "pending" in conclusions else ",".join(sorted(conclusions))[:200]
        reviews = sorted(client.reviews(repo, r["pr_number"]), key=lambda v: v.get("id") or 0, reverse=True)
        reviewers = set()
        for review in reviews:
            actor = review.get("user") or {}
            if (actor.get("type") != "User" or client.is_app_actor(review) or not _positive(review.get("id"))
                    or review.get("state") not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}):
                continue
            superseded = actor.get("id") in reviewers
            reviewers.add(actor.get("id"))
            if review.get("state") != "CHANGES_REQUESTED":
                continue
            action = fingerprint("review", r["pr_number"], review["id"], review.get("commit_id"))
            current = trusted and review.get("commit_id") == head and not superseded
            authorized = current and actor.get("id") in cfg["allowed_actors"] and self._write_actor(client, repo, actor)
            self.journal.event(key, "seen:" + action, "review_observed", str(review["id"]),
                               "current" if authorized else "superseded" if superseded else "untrusted_or_stale")
            if authorized and not self.journal.has(key, action) and str(review.get("body") or "").strip():
                actionable[action] = {"kind": "review", "source": action, "sha": head,
                                      "feedback": "PR 변경 요청 (검토 자료)\n" + external_text(review["body"])[:4096]}
        pending = r["pending_run"]
        if pending and (pending["sha"] != head or not trusted or pending["source"] not in actionable):
            r["pending_run"] = None
            self.journal.event(key, "stale:" + pending["source"], "feedback_obsolete", pending["sha"])
        if (r["scope_pending"] or not trusted or run.get("state") != "READY_FOR_HUMAN_REVIEW"
                or run.get("validated") is not True or r.get("published_run") != r["run_id"]):
            return
        if r["pending_run"] is None and actionable:
            retry = next(iter(actionable.values()))
            r["pending_run"] = dict(retry, id=uuid4().hex)
            self.journal.save(key, r)
        if r["pending_run"]:
            # Refetch PR and Issue immediately before any new worker.
            live = client.issue(repo, r["number"])
            self._check_issue(live, r["issue_id"], r["number"])
            latest = client.pull(repo, r["pr_number"])
            self._check_pull(cfg, r, latest)
            if (latest["head"]["sha"] != head or latest["state"] != "open" or live["state"] != "open"
                    or snapshot(live, r["url"]) != r["observed"]
                    or self._admissible(client, cfg, live, client.timeline(repo, r["number"]))):
                r["status"] = "feedback_waiting"
                return
            self._launch(binding, key, r)
            # The new run has its own validation and completion gate.
            run.clear()
            run["state"] = "QUEUED"

    def _link(self, key, cfg, r, number, branch, sha):
        url = f"{key[0]}/{cfg['repository']}/pull/{number}"
        if r["candidate_sha"] and r["candidate_sha"] != sha:
            r["previous_heads"] = list(dict.fromkeys([r["candidate_sha"], *r.get("previous_heads", [])]))[:2]
        r.update(pr_number=number, pr_url=url, pr_head=branch, candidate_sha=sha)
        self.journal.save(key, r, [(fingerprint("pull", number, sha), "pull_linked", f"#{number} · {sha}", "observed")])

    def _comment(self, key, cfg, client, r, run):
        if not client.app_id:
            return
        marker = "<!-- agent-office:" + fingerprint(*key) + " -->"
        # Only identifiers, enum states and verified link numbers may leave the host.
        lines = [marker, "Agent Office", f"목표 `{r['goal_id']}`", "상태 `" + r["status"] + "`"]
        if run.get("role") in {"developer", "reviewer", "coordinator", "tester"}:
            lines.append("담당 `" + run["role"] + "`")
        if r["pr_number"]:
            lines.append(f"연결된 PR #{r['pr_number']}")
        if type(run.get("attempts")) is int:
            lines.append(f"실행 시도 {run['attempts']}회")
        checks = run.get("checks") or []
        if checks:
            passed = sum(check.get("status") == "passed" for check in checks)
            lines.append(f"로컬 검증 {len(checks)}건 중 통과 {passed}건")
            for stage in ("quick", "full"):
                results = [check.get("status") for check in checks if check.get("stage") == stage]
                if results:
                    lines.append(f"{stage} 검증 통과 {results.count('passed')}/{len(results)}")
        if type(run.get("manual_count")) is int and run["manual_count"]:
            lines.append(f"수동 확인 {run['manual_count']}건")
        if r.get("ci_state") in {"failure", "success", "pending"}:
            lines.append("CI `" + r["ci_state"] + "`")
        question = run.get("question")
        if type(run.get("permission_count")) is int and run["permission_count"] > 0:
            lines.append(f"권한 승인 요청 {run['permission_count']}건 · 작업실 요청함에서만 결정할 수 있습니다.")
        if question and question.get("kind") == "plan":
            lines.append("계획 확인 대기 · 작업실 요청함에서 계획과 제품 선택을 확인하세요.")
        if question and question.get("kind") in {"technical", "product"} and re.fullmatch(
                "[a-f0-9]{32}", str(question.get("id", ""))):
            prompt = question.get("prompt")
            if isinstance(prompt, str) and prompt and external_text(prompt) == prompt and not re.search(
                    r"[/\\`@<>]|[\x00-\x08]", prompt):
                lines.append("질문 · " + prompt[:600])
            lines.append(f"답변 형식: `/office answer {r['goal_id']} {question['id']} 답변`")
        elif question:
            lines.append("작업실 요청함에서 결정을 기다립니다.")
        body = "\n\n".join(lines)
        r["publish_state"] = "comment_pending"
        self.journal.save(key, r)
        if r["comment_id"]:
            try:
                recorded = client.comment(cfg["repository"], r["comment_id"])
            except Exception as exc:
                if getattr(exc, "status", None) != 404:
                    raise
                comments = client.comments(cfg["repository"], r["number"])
            else:
                if recorded.get("id") != r["comment_id"] or not self._owned(client, [recorded], marker):
                    raise OfficeError("COMMENT_IDENTITY_CHANGED")
                comments = [recorded]
        else:
            comments = client.comments(cfg["repository"], r["number"])
        owned = self._owned(client, comments, marker)
        if len(owned) > 1:
            raise OfficeError("MULTIPLE_STATUS_COMMENTS")
        if owned:
            comment = owned[0]
            if comment["body"] != body:
                comment = client.update_comment(cfg["repository"], comment["id"], body)
        else:
            # Visibility can lag after a successful but unacknowledged POST. Keep the intent while uncertain.
            if r.get("comment_uncertain"):
                r["publish_state"] = "comment_ack_pending"
                return
            r["comment_uncertain"] = True
            self.journal.save(key, r)
            try:
                comment = client.add_comment(cfg["repository"], r["number"], body)
            except Exception as exc:
                if getattr(exc, "status", None) in {400, 401, 403, 404, 409, 410, 422, 429}:
                    r["comment_uncertain"] = False
                raise
        if not client.is_app_actor(comment) or not _positive(comment.get("id")) or comment.get("body") != body:
            raise OfficeError("INVALID_COMMENT_RECEIPT")
        r.update(comment_id=comment["id"], comment_uncertain=False, publish_state="published")
        self.journal.save(key, r)

    @staticmethod
    def _owned(client, comments, marker):
        return [c for c in comments if (c.get("user") or {}).get("type") == "Bot" and
                client.is_app_actor(c) and _positive(c.get("id")) and
                (c.get("body") or "").splitlines()[:1] == [marker]]

    def retry_post(self, workspace, issue_id, id):
        validate_id(id)
        binding, key = self._target(workspace, issue_id)
        event = "retry-post:" + id
        with self._guard(binding):
            if self.journal.has(key, event):
                return self.state(workspace)
            r = self._record(binding, issue_id)
            if (not binding["config"]["enabled"] or not r.get("comment_uncertain") or
                    r["publish_state"] != "comment_ack_pending"):
                raise OfficeError("POST_RETRY_UNAVAILABLE")
            client = self._client(binding)
            repo = binding["config"]["repository"]
            self._check_issue(client.issue(repo, r["number"]), issue_id, r["number"])
            owned = self._owned(client, client.comments(repo, r["number"]),
                                "<!-- agent-office:" + fingerprint(*key) + " -->")
            if len(owned) > 1:
                raise OfficeError("MULTIPLE_STATUS_COMMENTS")
            if owned:
                r["comment_id"] = owned[0]["id"]
            r.update(comment_uncertain=False, publish_state="comment_pending")
            self.journal.save(key, r, [(event, "comment_retry", str(r["number"]), "accepted")])
        self.sync(workspace)
        return self.state(workspace)

    def _target(self, workspace, issue_id):
        if not _positive(issue_id):
            raise ValueError("이슈 ID를 확인하세요.")
        bindings = self.journal.bindings(workspace)
        if not bindings:
            raise OfficeError("NOT_FOUND")
        return bindings[0], _key(bindings[0], issue_id)

    def _record(self, binding, issue_id):
        with self.journal.db() as db:
            record = self.journal.get(db, _key(binding, issue_id))
        if record and record["workspace_id"] == binding["workspace"]:
            return record
        raise OfficeError("NOT_FOUND")

    def decide(self, workspace, issue_id, event_id, action):
        if action not in {"accept", "reject"} or not isinstance(event_id, str) or not re.fullmatch("[0-9a-f]{64}", event_id):
            raise ValueError("목표 변경 결정과 이벤트 ID를 확인하세요.")
        binding, key = self._target(workspace, issue_id)
        with self._guard(binding):
            r = self._record(binding, issue_id)
            decision = fingerprint("decision", event_id, action)
            if self.journal.has(key, decision):
                return self.state(workspace)
            if event_id != r["scope_pending"]:
                raise OfficeError("SCOPE_DECISION_STALE")
            if action == "accept":
                r["accepted"] = r["observed"]
                if r["run_id"]:
                    r["scope_delivery"] = {"id": decision, "snapshot": r["accepted"]}
                if r["pending_run"] and r["pending_run"]["kind"] == "start":
                    r["pending_run"]["snapshot"] = r["accepted"]
            r["scope_pending"] = None
            self.journal.save(key, r, [(decision, "scope_decision", event_id, action)])
        self.sync(workspace)
        return self.state(workspace)

    def restart(self, workspace, issue_id, id):
        validate_id(id)
        binding, key = self._target(workspace, issue_id)
        with self._guard(binding):
            r = self._record(binding, issue_id)
            if self.journal.has(key, id) or r["restart_key"] == id:
                return self.state(workspace)
            terminal = {"failed", "canceled", "waiting_user", "recovery_required", "plan_only"}
            if not binding["config"]["enabled"] or (not r["restart_required"] and r["status"] not in terminal):
                raise OfficeError("RESTART_UNAVAILABLE")
            client = self._client(binding)
            cfg = binding["config"]
            issue = client.issue(cfg["repository"], r["number"])
            self._check_issue(issue, issue_id, r["number"])
            if issue["state"] != "open" or self._admissible(client, cfg, issue, client.timeline(cfg["repository"], r["number"])):
                raise OfficeError("RESTART_UNAVAILABLE")
            if r["run_id"] and self.office.status(r["run_id"]).get("state") in ACTIVE:
                raise OfficeError("RUN_BUSY")
            # Keep the old admission/run history and close marker while opening a new generation.
            r.update(restart_key=id, pending_run=None, pr_number=None, pr_url=None, pr_head=None,
                     candidate_sha=None, observed_head=None, previous_heads=[], published_run=None,
                     office_draft=False, retries=0, ci_state=None, state="open")
            timeline = client.timeline(cfg["repository"], r["number"])
            closes = [str(e["id"]) for e in timeline if e.get("event") == "closed" and e.get("id")]
            r["closed_event"] = closes[-1] if closes else None
            self.journal.save(key, r)
        self.sync(workspace)
        return self.state(workspace)

    def link_pull(self, workspace, issue_id, number, candidate_sha=None):
        if not _positive(number):
            raise ValueError("PR 번호를 확인하세요.")
        binding, key = self._target(workspace, issue_id)
        with self._guard(binding):
            r = self._record(binding, issue_id)
            cfg = binding["config"]
            if not cfg["enabled"] or not r["run_id"] or r["scope_pending"] or r["restart_required"]:
                raise OfficeError("PULL_LINK_UNAVAILABLE")
            if r["pr_number"] is not None and r["pr_number"] != number:
                raise OfficeError("PULL_ALREADY_LINKED")
            client = self._client(binding)
            pull = client.pull(cfg["repository"], number)
            head = pull["head"]
            self._check_pull(cfg, dict(r, pr_number=number, pr_head=head["ref"]), pull)
            if pull.get("state") != "open" or head["ref"] == cfg["branch"]:
                raise OfficeError("PULL_LINK_UNAVAILABLE")
            sha = self.office.verify_pull(workspace, r["run_id"], cfg, client, self._payload(r), pull)
            if sha != head.get("sha") or candidate_sha is not None and sha != candidate_sha:
                raise OfficeError("CANDIDATE_MISMATCH")
            r["published_run"] = r["run_id"]
            self._link(key, cfg, r, number, head["ref"], sha)
        self.sync(workspace)
        return self.state(workspace)
