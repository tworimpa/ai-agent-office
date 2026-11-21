"""Publish one immutable candidate as a new branch and a verified draft pull request."""

from __future__ import annotations

import base64
import hashlib

from .github_api import GitHubError, _branch, _dict, _repo, _sha, positive_id


def _entries(files):
    if not isinstance(files, dict) or not 1 <= len(files) <= 300:
        raise GitHubError("invalid_candidate_files")
    entries, size = [], 0
    for path, value in files.items():
        if (not isinstance(path, str) or not path.isprintable() or len(path) > 1024 or "\\" in path
                or any(p.casefold() in {"", ".", "..", ".git"} for p in path.split("/"))):
            raise GitHubError("invalid_candidate_path")
        mode, data = value if isinstance(value, tuple) and len(value) == 2 else ("100644", value)
        if mode not in ("100644", "100755") or (data is not None and not isinstance(data, bytes)):
            raise GitHubError("invalid_candidate_mode")
        if data is not None:
            size += len(data)
            if size > 8 * 1024**2:
                raise GitHubError("candidate_too_large")
        entries.append((path, mode, data))
    return entries


def _existing_head(client, repo, branch):
    try:
        return client.branch_sha(repo, branch)
    except GitHubError as error:
        if error.status == 404:
            return None
        raise


def _verify_pull(client, repo, base_branch, head_branch, head_sha, pr):
    number = positive_id(pr.get("number"))
    pr = client.pull(repo, number)
    def same(side, branch):
        ref = _dict(pr.get(side))
        destination = _dict(ref.get("repo"))
        return ref.get("ref") == branch and str(destination.get("full_name", "")).lower() == repo.lower()
    if (pr.get("number") != number or pr.get("state") != "open" or pr.get("draft") is not True
            or not same("head", head_branch) or not same("base", base_branch)
            or pr["head"].get("sha") != head_sha or not client.is_app_actor(pr)):
        raise GitHubError("draft_mismatch")
    return {"number": number, "html_url": f"{client.web_url}/{repo}/pull/{number}", "head_sha": head_sha}


def _candidate_tree(client, repo, parent_sha, files, expected_tree):
    rows = _entries(files)
    base_tree = client.commit_tree(repo, parent_sha)
    git = _repo(repo) + "/git/"
    entries = []
    for path, mode, data in rows:
        sha = None
        if data is not None:
            blob = {"encoding": "base64", "content": base64.b64encode(data).decode("ascii")}
            sha = _sha(_dict(client._request("POST", git + "blobs", blob).data).get("sha"))
            if sha != hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest():
                raise GitHubError("candidate_blob_mismatch")
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": sha})
    tree = _dict(client._request("POST", git + "trees", {"base_tree": base_tree, "tree": entries}).data)
    tree_sha = _sha(tree.get("sha"))
    if expected_tree is not None and tree_sha != _sha(expected_tree):
        raise GitHubError("candidate_tree_mismatch")
    if tree_sha == base_tree:
        raise GitHubError("empty_candidate")
    return tree_sha


def _verify_parent(client, repo, head, tree, parent):
    commit = client.read_commit(repo, head)
    if (_dict(commit.get("tree")).get("sha") != tree
            or [_dict(p).get("sha") for p in commit.get("parents", [])] != [parent]):
        raise GitHubError("candidate_branch_conflict")


def _candidate_branch(head, base):
    if not _branch(head).startswith("ao/") or head == _branch(base):
        raise GitHubError("invalid_candidate_branch")


def publish_candidate(client, repo, base_branch, base_sha, head_branch, files, title, body, expected_tree=None):
    _sha(base_sha)
    _candidate_branch(head_branch, base_branch)
    if client.branch_sha(repo, base_branch) != base_sha:
        raise GitHubError("base_advanced")
    tree_sha = _candidate_tree(client, repo, base_sha, files, expected_tree)
    git = _repo(repo) + "/git/"
    head = _existing_head(client, repo, head_branch)
    if not head:
        new = {"message": title, "tree": tree_sha, "parents": [base_sha]}
        created = _dict(client._request("POST", git + "commits", new).data)
        head = _sha(created.get("sha"))
        try:
            client._request("POST", git + "refs", {"ref": "refs/heads/" + head_branch, "sha": head})
        except GitHubError as exc:
            if exc.status not in (409, 422):
                raise
        head = client.branch_sha(repo, head_branch)
    _verify_parent(client, repo, head, tree_sha, base_sha)
    if client.branch_sha(repo, base_branch) != base_sha:
        raise GitHubError("base_advanced")
    prs = client.pulls(repo, head_branch, base_branch)
    if len(prs) > 1:
        raise GitHubError("draft_mismatch")
    pr = prs[0] if prs else client.create_draft(repo, head_branch, base_branch, title, body)
    return {**_verify_pull(client, repo, base_branch, head_branch, head, pr), "tree_sha": tree_sha}


def advance_candidate(client, repo, base_branch, head_branch, expected_head, pr_number, files, expected_tree, title):
    from urllib.parse import quote
    _candidate_branch(head_branch, base_branch)
    _sha(expected_head)
    _sha(expected_tree)
    head = client.branch_sha(repo, head_branch)
    pr = {"number": positive_id(pr_number)}
    details = _verify_pull(client, repo, base_branch, head_branch, head, pr)
    if head != expected_head:
        _verify_parent(client, repo, head, expected_tree, expected_head)
        return {**details, "tree_sha": expected_tree}
    tree_sha = _candidate_tree(client, repo, head, files, expected_tree)
    git = _repo(repo) + "/git/"
    # A sibling commit racing this update is not an ancestor: GitHub rejects
    # the non-fast-forward update. The only parent is the approved PR head.
    created = _dict(client._request("POST", git + "commits",
                                    {"message": title, "tree": tree_sha, "parents": [head]}).data)
    new_sha = _sha(created.get("sha"))
    _verify_parent(client, repo, new_sha, tree_sha, expected_head)
    _verify_pull(client, repo, base_branch, head_branch, expected_head, pr)
    ref = git + "refs/heads/" + quote(head_branch, safe="/")
    client._request("PATCH", ref, {"sha": new_sha, "force": False})
    if client.branch_sha(repo, head_branch) != new_sha:
        raise GitHubError("candidate_branch_conflict")
    return {**_verify_pull(client, repo, base_branch, head_branch, new_sha, pr), "tree_sha": tree_sha}
