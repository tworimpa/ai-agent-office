"""Bounded read projections for one visible workspace refresh."""
import re

from . import overnight_goals, work_profile_views, workspace_briefing
from .models import OfficeError
from .store import TaskStore


def route(method, path):
    if method == "GET":
        match = re.fullmatch(r"/rooms/([a-f0-9]{32})/bundle/(none|[a-f0-9]{32})/(live|full|full-usage)", path)
        if match:
            return match.groups()
    return None


def paths(workspace, run, mode):
    base = f"/rooms/{workspace}"
    result = {"room": base}
    if mode != "live":
        result.update(briefing=base + "/briefing", github=base + "/github")
    if run != "none":
        selected = f"/runs/{run}"
        result.update(run=selected, goal=selected + "/goal", inbox=selected + "/inbox",
                      directions=selected + "/directions")
        if mode == "full-usage":
            result["usage"] = base + "/usage/" + run
    return result


def read(server, workspace, run, mode):
    interactive = False
    with TaskStore(server.db_path, readonly=True) as store:
        result = {"room": server.rooms.snapshot(workspace, store=store)}
        if run != "none":
            detail = server.runner._detail(store, run)
            if detail["workspace_id"] != workspace:
                raise OfficeError("NOT_FOUND")
            result["run"] = {"run": detail}
            interactive = detail.get("kind") != "parallel"
            if interactive:
                inbox = server.runner.inbox.settled_history(store.connection, run)
                directions = server.directions.settled_history(store.connection, run)
    # Release the snapshot before any stale delivery or approval reconciliation.
    if interactive:
        result["goal"] = {"goal": overnight_goals.report(server.db_path, run)}
        result["inbox"] = {"requests": inbox if inbox is not None else server.runner.inbox.history(run)}
        result["directions"] = {"directions": directions if directions is not None else server.directions.history(run)}
        if mode == "full-usage":
            result["usage"] = work_profile_views.handle(server, workspace, "usage", run, None)
    if mode != "live":
        result["briefing"] = workspace_briefing.handle(server, workspace, "briefing", None, None)
        _, result["github"] = server.github.handle(workspace, None, None)
    return result
