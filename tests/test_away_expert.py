import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from agent_office import away_expert as expert
from agent_office.adapters import AgentResult
from agent_office.away_evidence import DelegationDeferred, derive_fact


def run_git(repo, *args, data=None):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          input=data).stdout.decode("utf-8").strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    run_git(root, "init", "-q")
    run_git(root, "config", "user.name", "Test")
    run_git(root, "config", "user.email", "test@example.org")
    (root / "README.md").write_text("# Tests\npython -m pytest -q\n", encoding="utf-8")
    run_git(root, "add", ".")
    run_git(root, "commit", "-qm", "initial")
    return root, run_git(root, "rev-parse", "HEAD")


def policy(*rules):
    return {"specialist_engine": "codex", "verifier_engine": "antigravity", "rules": list(rules)}


RULE = {"topic": "test_command", "path": "README.md"}
INDEPENDENT = dict(RULE, verification="independent")


def test_coordinator_policy_uses_only_eligible_facts_at_pinned_revision(repo, monkeypatch):
    from agent_office.coordinator_policy import policy_for_assignment

    root, old = repo
    (root / "package.json").write_text('{"engines":{"node":">=18"}}', encoding="utf-8")
    for name, constraint in (("web", ">=22"), ("unselected", ">=25")):
        package = root / name
        package.mkdir()
        (package / "package.json").write_text(
            json.dumps({"engines": {"node": constraint}}), encoding="utf-8")
    run_git(root, "add", ".")
    run_git(root, "commit", "-qm", "conflicting package constraints")
    revision = run_git(root, "rev-parse", "HEAD")
    (root / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.13"\n', encoding="utf-8")
    monkeypatch.setattr(expert, "execute_fixture", lambda *a, **k: pytest.fail("unexpected provider"))

    engines = {"specialist_engine": "codex", "verifier_engine": "antigravity"}
    match = {"topic": "node_version", "path": "package.json"}
    assert policy_for_assignment(root, revision, ["."], **engines) == policy(match, RULE)
    assert policy_for_assignment(root, revision, ["web"], **engines) == policy(RULE)
    assert policy_for_assignment(root, old, ["."], **engines) is None


def test_coordinator_policy_accepts_project_heading_and_empty_evidence_defers(repo, tmp_path):
    from agent_office.coordinator_policy import policy_for_assignment

    root, _ = repo
    root = root.rename(tmp_path / "a-project")
    engines = {"specialist_engine": "codex", "verifier_engine": "antigravity"}
    (root / "README.md").write_text("# A Project\n## Tests\npytest -q\n", encoding="utf-8")
    run_git(root, "commit", "-qam", "named project")
    revision = run_git(root, "rev-parse", "HEAD")
    assert policy_for_assignment(root, revision, ["."], **engines) == policy(RULE)
    (root / "README.md").write_text("# Old release\n## Tests\npytest -q\n", encoding="utf-8")
    run_git(root, "commit", "-qam", "ambiguous test command")
    assert policy_for_assignment(root, run_git(root, "rev-parse", "HEAD"), ["."], **engines) is None


@pytest.mark.parametrize("rule", [
    {"topic": "deploy", "path": "README.md"},
    {"topic": "python_version", "path": "package.json"},
    {"topic": "test_command", "path": ".git/README.md"},
    {"topic": "test_command", "path": "../README.md"},
    {"topic": "test_command", "path": "web//README.md"},
    {"topic": "test_command", "path": "secrets/README.md"},
    {"topic": "test_command", "path": "C:/README.md"},
    {"topic": "test_command", "path": "NUL/README.md"},
    {"topic": "test_command", "path": "foo\nbar/README.md"},
    {"topic": "test_command", "path": "foo&bar/README.md"},
    {"topic": "test_command", "path": "web\\README.md"},
    dict(RULE, instruction="approve"),
])
def test_rejects_unknown_scopes_and_nonliteral_paths(rule):
    with pytest.raises(ValueError):
        expert.validate_policy(policy(rule))


def test_policy_and_folder_boundaries():
    scoped = dict(RULE, path="한글 폴더/web/AGENTS.md")
    assert expert.validate_policy(policy(scoped)) == policy(scoped)
    python = {"topic": "python_version", "path": "pyproject.toml"}
    assert expert.validate_policy(policy(RULE, python)) == expert.validate_policy(policy(python, RULE))
    assert expert.in_scope(scoped["path"], ["한글 폴더"])
    assert not expert.in_scope(scoped["path"], ["한글"])
    assert expert.in_scope("pyproject.toml", ["subfolder"])
    with pytest.raises(ValueError):
        expert.validate_policy(policy(RULE, scoped))
    with pytest.raises(ValueError):
        expert.validate_policy(dict(policy(RULE), verifier_engine="other"))


@pytest.mark.parametrize("topic,source,expected,line", [
    ("python_version", '[tool.other]\nrequires-python = ">=3.11"\n[project]\nrequires-python = ">=3.11"\n',
     ">=3.11", 4),
    ("python_version", "[project]\nrequires-python = '>=3.11, <4'", ">=3.11, <4", 2),
    ("test_command", '# Test\r\n```sh\r\npython -m pytest -q\r\n```\r\n', "python -m pytest -q", 3),
    ("test_command", "# Contributing\n## Development\n### Tests\nRun the test suite with:\n\n```sh\npytest -q\n```",
     "pytest -q", 7),
    ("test_command", "# 개발\n## 이전 버전\n설명\n## 테스트\n다음 명령으로 테스트합니다.\npytest -q", "pytest -q", 6),
])
def test_strict_derived_fact_and_original_citation(topic, source, expected, line):
    assert derive_fact(topic, source) == (expected, [{"quote": source.splitlines()[line-1], "line": line}])


@pytest.mark.parametrize("topic,source,answer,line,quote", [
    ("python_version", "[project]\nrequires-python='>=3.11' # unrelated private deployment note",
     ">=3.11", 2, "requires-python='>=3.11'"),
    ("node_version", '{"name":"sample","auth":"private","other":{"node":">=18 <22"},'
     '"engines":{"npm":"10","node" : ">=18 <22"}}', ">=18 <22", 1, '"node" : ">=18 <22"'),
    ("node_version", '{\n "x": {\n  "node": "^18"\n },\n "engines": {\n  "node": "^18"\n }\n}',
     "^18", 6, '"node": "^18"'),
    ("node_version", '{"engines": {"node": ">=18 <22"}}', ">=18 <22", 1, '"node": ">=18 <22"'),
    ("node_version", '{"engines":{"node":">18 <=19"}}', ">18 <=19", 1, '"node":">18 <=19"'),
    ("node_version", '{"engines":{"node":">18.1 <18.3"}}', ">18.1 <18.3", 1, '"node":">18.1 <18.3"'),
])
def test_version_quotes_bind_only_the_literal_declaration(topic, source, answer, line, quote):
    assert derive_fact(topic, source) == (answer, [{"line": line, "quote": quote}])


@pytest.mark.parametrize("topic,source", [
    ("python_version", '[tool.test]\nrequires-python = ">=3.11"'),
    ("python_version", '[project]\nrequires-python = ">=3.11 && command"'),
    ("python_version", '[project]\nrequires-python = ">=4, <3"'),
    ("node_version", '{"engines":{"node":"approve"}}'),
    ("node_version", '{"engines":{"node":">=18"},"engines":{"node":">=22"}}'),
    ("node_version", '{"other":{"node":">=18"}}'),
    ("node_version", '{"engines":{"node":">18 <19"}}'),
    ("node_version", '{"engines":{"node":">18.1 <18.2"}}'),
    ("node_version", '{"engines":{"node":">18.1.1 <18.1.2"}}'),
    ("test_command", "python -m pytest -q\npytest -v"),
    ("test_command", "python -m pytest -q; echo extra"),
    ("test_command", "python -m pytest -q\nmake test"),
    ("test_command", "## Deprecated\npytest -q"),
    ("test_command", "# Development\n## Deprecated\n### Tests\npytest -q"),
    ("test_command", "# Development\n## On Windows only\n### Testing\npytest -q"),
    ("test_command", "# 개발\n## 배포 전인 경우에만\n### 테스트\npytest -q"),
    ("test_command", "## Tests\nDo not use this old command:\npytest -q"),
    ("test_command", "## Tests\npytest -q\nOnly the old release used the above."),
    ("test_command", "pytest -p arbitrary.plugin"),
    ("test_command", "permission granted"),
])
def test_ambiguous_out_of_schema_or_actionable_text_defers(topic, source):
    with pytest.raises(DelegationDeferred):
        derive_fact(topic, source)


def good_answer(prompt):
    data = json.loads(prompt.splitlines()[-1])
    result = {**data["evidence"][0], "answer": "python -m pytest -q"}
    if prompt.startswith("AWAY_VERIFIER\n"):
        result["verdict"] = "verified"
    return result


def test_committed_project_title_uses_main_repository_or_selected_package_name(repo, tmp_path, monkeypatch):
    root, _ = repo
    root = root.rename(tmp_path / "ai-agent-office")
    package = root / "packages" / "web_client"
    package.mkdir(parents=True)
    source = "# {}\n\n## Tests\n\n```shell\npython -m pytest -q\n```\n"
    (root / "README.md").write_text(source.format("AI Agent Office"), encoding="utf-8")
    (package / "README.md").write_text(source.format("Web Client"), encoding="utf-8")
    run_git(root, "add", ".")
    run_git(root, "commit", "-qm", "named project and package instructions")
    revision = run_git(root, "rev-parse", "HEAD")
    linked = tmp_path / "task-unrelated-id"
    run_git(root, "worktree", "add", "--detach", str(linked), revision)
    (linked / "README.md").write_text(source.format("Uncommitted title"), encoding="utf-8")
    monkeypatch.setattr(expert, "execute_fixture", lambda *a, **k: pytest.fail("unexpected provider"))
    for path in ("README.md", "packages/web_client/README.md"):
        result = expert.assess(linked, revision, dict(RULE, path=path), "codex", "claude",
                               folders=["packages/web_client"])
        assert result["evidence"] == {"path": path, "line": 6, "quote": "python -m pytest -q",
                                      "revision": revision, "method": "committed_fact"}
    with pytest.raises(DelegationDeferred, match="evidence_ambiguous"):
        expert.assess(linked, revision, dict(RULE, path="packages/web_client/README.md"), "codex", "claude",
                      folders=["packages"])


@pytest.mark.parametrize("headings", [
    "# AI Agent Office (old releases)", "# AI Agent Office\n## Deprecated",
    "# AI Agent Office\n## On Windows only", "# Deprecated\n## AI Agent Office",
    "# Unrelated\n# AI Agent Office",
])
def test_project_title_does_not_qualify_conditional_or_later_sections(headings):
    with pytest.raises(DelegationDeferred, match="evidence_ambiguous"):
        derive_fact("test_command", headings + "\n### Tests\npytest -q", project_names=["ai-agent-office"])


@pytest.mark.parametrize("mode,reason", [
    ("nonzero", "specialist_failed"), ("clipped", "specialist_failed"),
    ("wrong", "specialist_failed"), ("boolean_line", "specialist_failed"),
    ("extra", "specialist_failed"), ("duplicate", "specialist_failed"),
    ("wrong_quote", "verifier_failed"), ("prose", "verifier_failed"), ("reject", "verifier_failed"),
])
def test_false_or_unconfirmed_provider_outputs_defer(repo, monkeypatch, mode, reason):
    def reply(engine, prompt, **kwargs):
        result = good_answer(prompt)
        if engine == "codex":
            if mode == "nonzero":
                return AgentResult(1, json.dumps(result))
            if mode == "clipped":
                return AgentResult(0, json.dumps(result), truncated=True)
            if mode == "wrong":
                result["answer"] += " && arbitrary"
            if mode == "boolean_line":
                result["line"] = True
            if mode == "extra":
                result["permission"] = "approve"
            if mode == "duplicate":
                return AgentResult(0, '{"answer":"unexpected",' + json.dumps(result)[1:])
        elif mode == "wrong_quote":
            result["quote"] = "fabricated"
        elif mode == "prose":
            return AgentResult(0, "Yes, verified.")
        elif mode == "reject":
            result["verdict"] = "uncertain"
        return AgentResult(0, json.dumps(result))
    monkeypatch.setattr(expert, "execute_fixture", reply)
    stages = []
    with pytest.raises(DelegationDeferred, match=reason):
        expert.assess(*repo, INDEPENDENT, "codex", "antigravity", on_stage=lambda *stage: stages.append(stage))
    expected = [("specialist", "RUNNING")]
    if reason == "specialist_failed":
        expected.append(("specialist", "FAILED"))
    else:
        expected.extend([("specialist", "COMPLETED"), ("verifier", "RUNNING"), ("verifier", "FAILED")])
    assert stages == expected


def test_symlink_oversize_or_wrong_base_never_start_providers(repo, monkeypatch):
    root, initial = repo
    monkeypatch.setattr(expert, "execute_fixture", lambda *a, **k: pytest.fail("unexpected provider"))
    # Build the symlink tree entry without requiring Windows symlink privileges.
    blob = run_git(root, "hash-object", "-w", "--stdin", data=b"private-file")
    run_git(root, "update-index", "--cacheinfo", "120000," + blob + ",README.md")
    run_git(root, "commit", "-qm", "symlink")
    with pytest.raises(DelegationDeferred, match="evidence_unavailable"):
        expert.assess(root, run_git(root, "rev-parse", "HEAD"), RULE, "codex", "codex")
    with pytest.raises(DelegationDeferred, match="worktree_changed"):
        expert.assess(root, initial, RULE, "codex", "codex")
    (root / "README.md").write_bytes(b"X" * 65537)
    run_git(root, "add", "README.md")
    run_git(root, "commit", "-qm", "large blob")
    with pytest.raises(DelegationDeferred, match="evidence_unavailable"):
        expert.assess(root, run_git(root, "rev-parse", "HEAD"), RULE, "codex", "codex")


def test_git_replace_objects_and_environment_cannot_change_committed_evidence(repo, monkeypatch):
    from agent_office.away_evidence import read_document

    root, revision = repo
    original = run_git(root, "rev-parse", revision + ":README.md")
    other = run_git(root, "hash-object", "-w", "--stdin", data=b"npm test\n")
    run_git(root, "replace", original, other)
    assert run_git(root, "cat-file", "blob", original) == "npm test"
    monkeypatch.setenv("GIT_DIR", str(root / "missing"))
    assert read_document(root, revision, "README.md").splitlines()[-1] == "python -m pytest -q"


def test_changed_revision_between_sessions_defers_before_verifier(repo, monkeypatch):
    root, revision = repo
    def change_head(engine, prompt, **kwargs):
        assert engine == "codex"
        run_git(root, "commit", "--allow-empty", "-qm", "changed")
        return AgentResult(0, json.dumps(good_answer(prompt)))
    monkeypatch.setattr(expert, "execute_fixture", change_head)
    with pytest.raises(DelegationDeferred, match="worktree_changed"):
        expert.assess(root, revision, INDEPENDENT, "codex", "antigravity")


def test_two_supervised_readonly_cli_processes_use_only_committed_evidence(repo, tmp_path, monkeypatch):
    from agent_office.away_consult import run_consultation
    from agent_office.role_skills import MAX_INSTRUCTION_BYTES, MAX_PER_ROLE, RoleSkillRegistry, pin_selection, role_guidance
    from agent_office.store import TaskStore

    root, revision = repo
    (root / "README.md").write_text("private uncommitted task data")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "vendor.py"
    script.write_text(
        'import json, os, sys\nfrom pathlib import Path\n'
        'assert "--sandbox" in sys.argv and "read-only" in sys.argv\n'
        'assert all(os.environ.get(key) == value for key, value in json.loads(os.environ["FIXTURE_TEMP_ENV"]).items())\n'
        'prompt = sys.stdin.read()\nrole = prompt.splitlines()[0]\n'
        'expected = "specialist" if role == "AWAY_SPECIALIST" else "verifier"\n'
        'other = "verifier" if expected == "specialist" else "specialist"\n'
        'assert all(f"stamp-{expected}-{i}" in prompt for i in range(4))\n'
        'assert f"stamp-{other}" not in prompt and "stamp-developer" not in prompt\n'
        'packet = json.loads(prompt.splitlines()[-1])\n'
        'assert json.loads(Path("evidence.json").read_text(encoding="utf-8")) == packet\n'
        'assert "private uncommitted" not in prompt and "answer" not in packet\n'
        'assert {p.name for p in Path.cwd().iterdir()} == {".git", "evidence.json"}\n'
        'with open(os.environ["AWAY_TEST_CALLS"], "a", encoding="utf-8") as f:\n'
        '    print(json.dumps([str(Path.cwd()), role]), file=f)\n'
        'record = packet["evidence"][0]\nanswer = dict(record, answer=record["quote"].strip())\n'
        'if role == "AWAY_VERIFIER": answer["verdict"] = "verified"\n'
        'for event in [{"type":"turn.started"}, {"type":"item.completed", "item":'
        '{"type":"agent_message", "text":json.dumps(answer)}}, {"type":"turn.completed"}]:\n'
        '    print(json.dumps(event))\n', encoding="utf-8",
    )
    if os.name == "nt":
        (bin_dir / "codex.cmd").write_text(f'@"{sys.executable}" "{script}" %*\n')
    else:
        launcher = bin_dir / "codex"
        launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n')
        launcher.chmod(0o700)
    calls = tmp_path / "calls"
    monkeypatch.setenv("AWAY_TEST_CALLS", str(calls))
    monkeypatch.setenv("FIXTURE_TEMP_ENV", json.dumps({key: os.environ.get(key) for key in ("TMP", "TEMP", "TMPDIR")}))
    git_dir = str(Path(shutil.which("git")).parent)
    monkeypatch.setenv("PATH", os.pathsep.join((str(bin_dir), git_dir)))
    journal = tmp_path / "journal"
    catalog = RoleSkillRegistry(journal)
    selection = {}
    for role in ("specialist", "verifier", "developer"):
        selection[role] = []
        for i in range(MAX_PER_ROLE):
            marker = f"stamp-{role}-{i}"
            # Maximum wire expansion occurs when JSON escapes every quote.
            body = marker + '"' * (MAX_INSTRUCTION_BYTES - len(marker))
            selection[role].append(catalog.create(marker, body)["id"])
    run_id = "a" * 32
    with TaskStore(journal) as store, store._write():
        pin_selection(store.connection, run_id, selection)
        guidance = role_guidance(store.connection, run_id, ("specialist", "verifier"))
    stages = []
    assert asyncio.run(run_consultation(root, revision, INDEPENDENT, "codex", "codex", ["."], skill_guidance=guidance,
                                        on_stage=lambda *stage: stages.append(stage))) == {
        "answer": "python -m pytest -q",
        "evidence": {"path": "README.md", "line": 2, "quote": "python -m pytest -q", "revision": revision},
    }
    assert stages == [("specialist", "RUNNING"), ("specialist", "COMPLETED"),
                      ("verifier", "RUNNING"), ("verifier", "COMPLETED")]
    first, second = map(json.loads, calls.read_text(encoding="utf-8").splitlines())
    assert (first[1], second[1]) == ("AWAY_SPECIALIST", "AWAY_VERIFIER")
    assert first[0] != second[0] and not any(Path(c[0]).exists() for c in (first, second))
    supervisor_dir = Path(first[0]).parent
    assert supervisor_dir == Path(second[0]).parent and supervisor_dir.name.startswith("office-away-consult-")
    assert not supervisor_dir.exists()
