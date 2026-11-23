"""Resolve the enabled assignment's rules from its repository's pinned evidence."""
from pathlib import Path, PurePosixPath

from .away_evidence import DelegationDeferred, git, read_document, require_base
from .away_expert import EVIDENCE_FILES, document_fact, validate_policy, validate_rule


def _paths(topic, filenames, folders):
    # Only the named files at the repository root or in a selected directory
    # are considered. Folder entries are already relative to the pinned repo.
    paths = []
    for folder in dict.fromkeys([*folders, "."]):
        for filename in filenames:
            path = str(PurePosixPath(folder, filename))
            try:
                validate_rule({"topic": topic, "path": path})
            except ValueError:
                continue
            paths.append(path)
    if topic == "test_command" and "docs/TESTING.md" not in paths:
        paths.append("docs/TESTING.md")
    return paths


def _tracked_paths(repo, revision, paths):
    tracked = set()
    # Keep invocations within Windows argv limits for large workspace selections.
    for start in range(0, len(paths), 32):
        entries = git(repo, "ls-tree", "-z", revision, "--", *paths[start:start + 32])
        tracked.update(entry.partition(b"\t")[2] for entry in entries.split(b"\0") if entry)
    return tracked


def _topic_rule(repo, revision, topic, paths, folders):
    found = None
    for path in paths:
        try:
            document = read_document(repo, revision, path)
        except DelegationDeferred:
            return None
        try:
            answer, _ = document_fact(repo, topic, path, document, folders)
        except DelegationDeferred as exc:
            if exc.reason == "evidence_unavailable" and topic == "test_command":
                # A general README or AGENTS file may contain no test command.
                continue
            return None
        if found is None:
            found = (path, answer)
        elif found[1] != answer:
            return None
    if found:
        return {"topic": topic, "path": found[0]}
    return None


def policy_for_assignment(repo: Path, revision: str, folders: list[str], *,
                          specialist_engine: str, verifier_engine: str) -> dict | None:
    """Pin only unambiguous supported facts. Discovery never calls providers."""
    candidates = {topic: _paths(topic, filenames, folders) for topic, filenames in EVIDENCE_FILES.items()}
    try:
        require_base(repo, revision)
        tracked = _tracked_paths(repo, revision, [path for paths in candidates.values() for path in paths])
        rules = []
        for topic, paths in candidates.items():
            rule = _topic_rule(repo, revision, topic,
                               [path for path in paths if path.encode("utf-8") in tracked], folders)
            if rule:
                rules.append(rule)
        require_base(repo, revision)
    except (DelegationDeferred, OSError, UnicodeError):
        return None
    if rules:
        return validate_policy({"rules": rules, "specialist_engine": specialist_engine,
                                "verifier_engine": verifier_engine})
    return None
