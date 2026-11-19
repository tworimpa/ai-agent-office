"""Browser actions on host-local proposal jobs and an operator-published read model."""
from . import release_view
from .improvement_wire import check_start
from .models import OfficeError
from .store import TaskStore


def _studio_runs(db_path, ids):
    with TaskStore(db_path, readonly=True) as store:
        rows = store.connection.execute(f"SELECT id FROM studio_runs WHERE id IN ({','.join('?' for _ in ids)})",
                                        ids)
        return [row["id"] for row in rows]


def dispatch(server, target, payload=None):
    kind, value = target
    service = server.improvements
    view = (server.release_view, server.journal_id)
    if kind == "status":
        try:
            publication = release_view.view_status(*view)
        except OfficeError as exc:
            if exc.code != "STORE_IO":
                raise
            publication = {"available": False, "stable": None, "updated_at": None}
        return 200, {"source_enabled": server.improvement_source is not None,
                     "view": publication}
    if kind == "runs":
        return 200, service.runs(value)
    if kind == "jobs":
        return 200, service.jobs(value)
    if kind == "candidates":
        return 200, release_view.view_candidates(*view, value)
    if kind == "candidate":
        record = release_view.view_candidate(*view, value)
        return 200, {"candidate": record, "studio_run_ids": _studio_runs(server.db_path, record["run_ids"])}
    if kind == "job":
        job = service.job(value)
        proposal = None
        if job["state"] == "ready":
            artifact = service.artifact(value)
            proposal = {key: artifact[key] for key in ("rationale", "expected", "patch")}
        try:
            link = release_view.view_job(*view, value)
            available = server.release_view is not None
        except OfficeError as exc:
            if exc.code != "STORE_IO":
                raise
            link = {"candidate_id": None, "error": None}
            available = False
        return 200, {"job": job, "proposal": proposal, "studio_run_ids": _studio_runs(server.db_path, job["run_ids"]),
                     "publication_available": available, **link}

    check_start(payload)
    # A retry may arrive after promotion or after publication was disconnected.
    # Return the original receipt without authorizing another model call.
    try:
        existing = service.job(payload["id"])
    except OfficeError as exc:
        if exc.code != "NOT_FOUND":
            raise
    else:
        if any(existing[key] != val for key, val in payload.items()):
            raise OfficeError("REQUEST_CONFLICT")
        return 202, {"job": existing}
    if server.improvement_source is None:
        raise ValueError("작업 호스트에서 개선안 기준 소스를 설정하세요.")
    published = release_view.view_status(*view)
    if not published["available"] or payload["base"] != published["stable"]:
        raise ValueError("운영자가 게시한 안정 커밋을 새로 확인한 뒤 제안하세요.")
    return 202, {"job": service.start(payload["id"], payload["base"], payload["engine"], payload["run_ids"])}
