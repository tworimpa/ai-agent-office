"""Disjoint Studio journals must agree on the repository's unique owner label."""
import hashlib

from .github_connection import GitHubError
from .models import validate_id


def verify_owner(journal, client, repo_id, repository, *, claim=False):
    validate_id(journal)
    pinned = client.config.owner_journal
    if pinned is not None and pinned != journal:
        raise GitHubError("owner_journal_mismatch")
    identity = f"office-owner-v1\0{client.web_url.rstrip('/').lower()}\0{repo_id}\0{journal}"
    digest = hashlib.sha256(identity.encode()).hexdigest()
    client.ensure_issue_owner(repository, digest, claim=claim)
