"""Workspace UI boundary for Issue intake and polling, including journal-scoped SSH views."""
import re

from .github_api import GitHubRegistry
from .github_connection import GitHubError
from .github_office import GitHubOffice, resolve_binding
from .github_sync import IssueSync
from .models import validate_id


def route(method, path):
    if method not in ("GET","POST"):
        return None
    match = re.fullmatch(r"/rooms/([a-f0-9]{32})/github(?:/(repos/[A-Za-z0-9_-]{1,64}|page/[1-9][0-9]{0,18}|"
                         r"issues/[1-9][0-9]{0,18}(?:/page/[1-9][0-9]{0,18})?|test|sync|decision|restart|pull|retry-post))?", path)
    if not match:
        return None
    workspace, action = match.groups()
    if action is None or method == ("GET" if action.startswith(("repos/","issues/","page/")) else "POST"):
        return workspace, action
    return None


def check_reply(status, result, method, path, payload=None):
    from .studio_ssh import RemoteUnavailable

    workspace, action = route(method, path)
    if status != (202 if action == "sync" else 200) or not isinstance(result, dict):
        raise RemoteUnavailable()
    if action is None or action.startswith("page/") or action in {"decision", "restart", "pull", "retry-post"}:
        if (result.get("workspace_id") != workspace or not isinstance(result.get("status"), dict)
                or not isinstance(result.get("issues"), list) or len(result["issues"]) > 25
                or not isinstance(result.get("connections"), list) or len(result["connections"]) > 64):
            raise RemoteUnavailable()
        for connection in result["connections"]:
            if not isinstance(connection, dict) or set(connection) != {"id","name","kind","web_url","api_url","auth"}:
                raise RemoteUnavailable()
        if method == "POST" and payload is not None:
            if action is None:
                # Repository slug comes from the provider; the numeric identity and policy are authoritative.
                config = result.get("config")
                if not isinstance(config, dict) or any(config.get(k) != v for k,v in payload.items() if k != "repository"):
                    raise RemoteUnavailable()
            elif result.get("receipt") != dict(payload, operation=action):
                raise RemoteUnavailable()
        for item in result["issues"]:
            if not isinstance(item, dict) or type(item.get("issue_id")) is not int or not isinstance(item.get("status"), str):
                raise RemoteUnavailable()
            if item.get("run_id"):
                validate_id(item["run_id"])
    elif action.startswith("issues/"):
        item = result.get("issue")
        if (result.get("workspace_id") != workspace or not isinstance(item, dict)
                or item.get("issue_id") != int(action.split("/")[1]) or not isinstance(item.get("events"), list)
                or len(item["events"]) > 50):
            raise RemoteUnavailable()
    elif action.startswith("repos/"):
        if (not isinstance(result.get("repositories"), list) or result.get("connection_id") != action[6:]
                or any(not isinstance(repo, dict) or type(repo.get("id")) is not int or
                       not isinstance(repo.get("full_name"), str) for repo in result["repositories"])):
            raise RemoteUnavailable()
    elif action == "test":
        if not isinstance(result.get("diagnostic"), dict) or type(result["diagnostic"].get("ok")) is not bool:
            raise RemoteUnavailable()
    elif result.get("queued") is not True:
        raise RemoteUnavailable()


class WorkspaceGitHub:
    def __init__(self, server, registry=None):
        self.catalog = server.catalog
        self.registry_error = None
        if registry is None:
            try:
                registry = GitHubRegistry.load()
            except GitHubError as error:
                self.registry_error = error.code
                registry = GitHubRegistry()
        self.registry = registry
        office = GitHubOffice(server, registry)
        self.sync = IssueSync(server.db_path, registry, office)
        self.sync.start()

    def close(self):
        self.sync.close()

    def state(self, workspace, cursor=None):
        result = self.sync.state(workspace) if cursor is None else self.sync.state(workspace, cursor)
        if self.registry_error:
            result["status"]["last_error"] = self.registry_error
        return dict(result, workspace_id=workspace, connections=self.registry.connections())

    def handle(self, workspace, action, data=None):
        self.catalog.get(workspace)
        if action is None and data is None:
            return 200, self.state(workspace)
        if action and action.startswith("page/"):
            return 200, self.state(workspace, action.split("/")[1])
        if action and action.startswith("issues/"):
            fields = action.split("/")
            detail = self.sync.issue_detail(workspace, int(fields[1]), fields[3] if len(fields)==4 else None)
            return 200, dict(detail, workspace_id=workspace)
        if action and action.startswith("repos/"):
            connection = action[6:]
            return 200, {"repositories": self.registry.client(connection).repositories(), "connection_id":connection}
        if action == "test" or action is None:
            required = {"connection_id", "repository_id", "branch", "folder_index"}
            if not isinstance(data, dict) or not required <= data.keys() or action == "test" and data.keys() != required:
                raise ValueError("GitHub 연결·저장소·브랜치와 작업 폴더를 지정하세요.")
            if action is None and data.get("enabled") is False:
                old = self.sync.state(workspace)["config"]
                if old and all(old[k] == data[k] for k in required):
                    self.sync.configure(workspace, dict(data, repository=old["repository"]))
                    return 200, self.state(workspace)
            client = self.registry.client(data["connection_id"])
            diagnostic = client.diagnose()
            if diagnostic["ok"]:
                try:
                    _, repo, sha = resolve_binding(self.catalog, workspace, client, data["repository_id"],
                                                   data["folder_index"], data["branch"])
                    diagnostic.update(repository=repo, base_sha=sha)
                except (ValueError, GitHubError) as exc:
                    diagnostic.update(ok=False, code=exc.code if isinstance(exc, GitHubError) else "checkout_mismatch",
                                      message=exc.code if isinstance(exc, GitHubError) else str(exc))
            if action == "test":
                return 200, {"diagnostic": diagnostic}
            if not diagnostic["ok"]:
                raise ValueError(diagnostic.get("message") or diagnostic["code"])
            self.sync.configure(workspace, dict(data, repository=diagnostic["repository"]))
        elif action == "sync" and data == {}:
            return 202, self.sync.sync(workspace)
        elif action == "decision" and data.keys() == {"issue_id","event_id","action"}:
            self.sync.decide(workspace, data["issue_id"], data["event_id"], data["action"])
        elif action in {"restart","retry-post"} and data.keys() == {"issue_id","id"}:
            method = self.sync.restart if action == "restart" else self.sync.retry_post
            method(workspace, data["issue_id"], validate_id(data["id"]))
        elif action == "pull" and data.keys() in ({"issue_id","number"},{"issue_id","number","candidate_sha"}):
            self.sync.link_pull(workspace, data["issue_id"], data["number"], data.get("candidate_sha"))
        else:
            raise ValueError("GitHub 연동 요청 형식을 확인하세요.")
        result = self.state(workspace)
        if action:
            result["receipt"] = dict(data, operation=action)
        return 200, result
