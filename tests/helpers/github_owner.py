"""One provider-owned repository marker shared by competing test coordinators."""
from threading import Lock

from agent_office.github_connection import GitHubError


class OwnerClaims:
    def __init__(self):
        super().__init__()
        self.owner_markers = {}
        self.owner_lock = Lock()

    def ensure_issue_owner(self, repo, digest, *, claim=False):
        with self.owner_lock:
            if claim:
                self.owner_markers.setdefault(repo, digest)
            if repo not in self.owner_markers:
                raise GitHubError("issue_owner_missing")
            if self.owner_markers[repo] != digest:
                raise GitHubError("issue_owner_conflict")
            return True
