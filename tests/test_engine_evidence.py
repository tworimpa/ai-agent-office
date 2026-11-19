from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent_office import engine_evidence as evidence
from agent_office import engine_status
from agent_office.adapters import AgentResult


@pytest.fixture
def installed(tmp_path, monkeypatch):
    # Provide a stat-able identity. All vendor subprocess calls are replaced.
    path = tmp_path / "fake-cli"
    path.write_bytes(b"fixture executable version one")
    version = ["1.2.3"]
    resolve = evidence.resolve_executable
    monkeypatch.setattr(evidence, "resolve_executable", lambda name: resolve("git") if name == "git" else str(path))
    monkeypatch.setattr(evidence, "probe_version", lambda engine, executable, timeout: (version[0], "ok"))

    def metadata(engine, executable):
        return dict(engine_status.metadata_entry(engine, executable), version=version[0],
                    probe_status="ok", headless_support=True)
    monkeypatch.setattr(evidence, "probe_engine", metadata)
    return path, version


def snapshot(catalog):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = catalog.snapshot()
        if all(e["probe_status"] != "pending" for e in result["engines"]):
            return {e["engine"]: e for e in result["engines"]}
        time.sleep(0.01)
    pytest.fail("Metadata worker did not finish")


def test_studio_catalog_lazily_probes_all_engines_and_caches_metadata(tmp_path, installed, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    original = evidence.probe_engine
    def blocked(engine, executable):
        calls.append(engine)
        entered.set()
        assert release.wait(5)
        return original(engine, executable)
    monkeypatch.setattr(evidence, "probe_engine", blocked)
    catalog = evidence.StudioEngineCatalog(tmp_path / "journal.db")
    assert calls == []
    try:
        first = catalog.snapshot()
        assert entered.wait(2)
        assert {e["engine"] for e in first["engines"]} == {"codex", "antigravity", "claude", "cline", "gemini"}
        assert all(e["installed"] and e["probe_status"] == "pending" for e in first["engines"])
    finally:
        release.set()
    engines = snapshot(catalog)
    assert sorted(calls) == sorted(engines)
    assert all(e["capabilities"] == {
        name: {"status": "not_checked", "checked_at": None} for name in ("read", "edit", "review")
    } for e in engines.values())
    assert snapshot(catalog) == engines and len(calls) == len(engines)


def successful_fixture(adapter, prompt, *, workspace, allow_edits):
    marker = (workspace / "reference.txt").read_text(encoding="ascii").strip()
    assert marker not in prompt
    if allow_edits:
        assert "Edit only" in prompt
        (workspace / "candidate.txt").write_text(marker + "\n", encoding="ascii")
        return AgentResult(0, "private output should never persist")
    if "Review whether" in prompt:
        wrong = (workspace / "candidate.txt").read_text(encoding="ascii").strip()
        return AgentResult(0, f"VERDICT: CHANGES_REQUESTED\nExpected: {marker}\nFound: {wrong}")
    return AgentResult(0, marker)


def test_separate_fixture_checks_require_file_evidence_and_persist_for_exact_runtime(tmp_path, installed, monkeypatch):
    db = tmp_path / "journal.sqlite3"
    monkeypatch.setattr(evidence, "execute_fixture", successful_fixture)
    catalog = evidence.StudioEngineCatalog(db)
    for capability in evidence.CAPABILITIES:
        result = evidence.verify_engine(db, "codex", capability)
        assert result["status"] == "passed" and result["checked_at"]
        assert snapshot(catalog)["codex"]["capabilities"][capability] == {
            "status": "passed", "checked_at": result["checked_at"],
        }
    assert all(x["status"] == "not_checked" for x in snapshot(catalog)["claude"]["capabilities"].values())
    assert b"private output" not in db.read_bytes()
    assert str(installed[0]) not in json.dumps(snapshot(catalog))
    restarted = evidence.StudioEngineCatalog(db)
    assert snapshot(restarted)["codex"]["capabilities"] == snapshot(catalog)["codex"]["capabilities"]
    # A copied journal on a second host, or a distinct journal on this host, has no applicable checks.
    monkeypatch.setattr(evidence.socket, "gethostname", lambda: "a-different-fixture-host")
    assert snapshot(restarted)["codex"]["capabilities"]["read"]["status"] == "not_checked"


def test_evidence_invalidated_when_file_or_version_changes(tmp_path, installed, monkeypatch):
    path, version = installed
    db = tmp_path / "journal.db"
    monkeypatch.setattr(evidence, "execute_fixture", successful_fixture)
    catalog = evidence.StudioEngineCatalog(db)
    assert evidence.verify_engine(db, "codex", "read")["status"] == "passed"
    assert snapshot(catalog)["codex"]["capabilities"]["read"]["status"] == "passed"
    path.write_bytes(b"a replacement")
    assert snapshot(catalog)["codex"]["capabilities"]["read"]["status"] == "not_checked"
    assert evidence.verify_engine(db, "codex", "read")["status"] == "passed"
    version[0] = "1.2.4"
    assert snapshot(evidence.StudioEngineCatalog(db))["codex"]["capabilities"]["read"]["status"] == "not_checked"
    assert snapshot(evidence.StudioEngineCatalog(tmp_path / "other.db"))["codex"]["capabilities"]["read"]["status"] == "not_checked"


@pytest.mark.parametrize("mode,capability", [
    ("no_evidence", "read"), ("no_evidence", "edit"), ("no_evidence", "review"),
    ("extra_file", "read"), ("extra_file", "edit"), ("extra_file", "review"),
    ("truncated", "review"), ("nonzero", "edit"), ("wrong_verdict", "review"), ("parent_file", "read"),
])
def test_false_positive_and_undeclared_edits_are_rejected(tmp_path, installed, monkeypatch, mode, capability):
    def fake(adapter, prompt, *, workspace, allow_edits):
        if mode == "no_evidence":
            return AgentResult(0, "VERDICT: CHANGES_REQUESTED")
        result = successful_fixture(adapter, prompt, workspace=workspace, allow_edits=allow_edits)
        if mode == "extra_file":
            (workspace / ".unexpected").write_bytes(b"extra")
        if mode == "parent_file":
            (workspace.parent / "unexpected").write_bytes(b"extra")
        if mode == "wrong_verdict":
            return AgentResult(0, result.message + "\nVERDICT: APPROVED")
        return AgentResult(1 if mode == "nonzero" else 0, result.message, truncated=mode == "truncated")
    monkeypatch.setattr(evidence, "execute_fixture", fake)
    db = tmp_path / "results.db"
    assert evidence.verify_engine(db, "codex", capability)["status"] == "failed"
    assert snapshot(evidence.StudioEngineCatalog(db))["codex"]["capabilities"][capability]["status"] == "failed"


def test_binary_replaced_during_run_cannot_earn_a_badge(tmp_path, installed, monkeypatch):
    def changed(adapter, prompt, *, workspace, allow_edits):
        installed[0].write_bytes(b"replaced while running")
        return successful_fixture(adapter, prompt, workspace=workspace, allow_edits=allow_edits)
    monkeypatch.setattr(evidence, "execute_fixture", changed)
    db = tmp_path / "journal"
    result = evidence.verify_engine(db, "codex", "read")
    assert (result["status"], result["reason"]) == ("failed", "runtime_changed")
    assert snapshot(evidence.StudioEngineCatalog(db))["codex"]["capabilities"]["read"]["status"] == "not_checked"


@pytest.mark.parametrize("engine", ["claude", "gemini", "cline"])
def test_extended_metadata_fixed_commands_and_redaction(monkeypatch, engine):
    version = engine_status.CLINE_ACP_VERSION if engine == "cline" else "4.3.2"
    calls = []
    def run(argv, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL and Path(kwargs["cwd"]).is_dir()
        assert kwargs["timeout"] == 5 and Path(kwargs["cwd"]) != Path.cwd()
        calls.append(argv)
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, f"v{version}\nprivate-account-secret", "")
        return subprocess.CompletedProcess(argv, 0, " ".join(engine_status.FLAGS[engine])
                                           + " stream-json plan auto_edit\nprivate-account-secret", "")
    monkeypatch.setattr(engine_status.subprocess, "run", run)
    entry = engine_status.probe_engine(engine, "/test/fake")
    assert entry["probe_status"] == "ok" and entry["version"] == version and entry["headless_support"]
    assert entry["auth_status"] == "unknown" and "private-account" not in json.dumps(entry)
    assert calls == [["/test/fake", "--version"], ["/test/fake", "--help"]]


def test_prior_badge_requires_returning_to_exact_runtime_and_fixture_revision(tmp_path, installed, monkeypatch):
    db = tmp_path / "journal"
    path, version = installed
    monkeypatch.setattr(evidence, "execute_fixture", successful_fixture)
    assert evidence.verify_engine(db, "codex", "read")["status"] == "passed"
    alternate = tmp_path / "different-launcher"
    alternate.write_bytes(path.read_bytes())
    selected = [str(path)]
    monkeypatch.setattr(evidence, "resolve_executable", lambda name: selected[0])
    catalog = evidence.StudioEngineCatalog(db)
    def read_status():
        return snapshot(catalog)["codex"]["capabilities"]["read"]["status"]
    assert read_status() == "passed"
    selected[0] = str(alternate)
    assert read_status() == "not_checked"
    selected[0] = str(path)
    assert read_status() == "passed"
    monkeypatch.setattr(evidence, "_FIXTURE_REVISION", evidence._FIXTURE_REVISION + 1)
    assert read_status() == "not_checked"


def test_cached_launcher_metadata_refresh_rechecks_changed_vendor_version(tmp_path, installed, monkeypatch):
    db = tmp_path / "journal"
    clock = [0]
    monkeypatch.setattr(evidence, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(evidence, "execute_fixture", successful_fixture)
    assert evidence.verify_engine(db, "codex", "read")["status"] == "passed"
    catalog = evidence.StudioEngineCatalog(db)
    assert snapshot(catalog)["codex"]["capabilities"]["read"]["status"] == "passed"
    installed[1][0] = "3.2.0"
    clock[0] += evidence._METADATA_TTL + 1
    entry = snapshot(catalog)["codex"]
    assert entry["version"] == "3.2.0"
    assert entry["capabilities"]["read"]["status"] == "not_checked"


def test_concurrent_independent_fixture_results_share_the_journal(tmp_path, installed, monkeypatch):
    db = tmp_path / "journal"
    catalog = evidence.StudioEngineCatalog(db)
    monkeypatch.setattr(evidence, "execute_fixture", successful_fixture)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda cap: evidence.verify_engine(db, "codex", cap), evidence.CAPABILITIES))
    assert all(result["status"] == "passed" for result in results)
    assert all(cap["status"] == "passed" for cap in snapshot(catalog)["codex"]["capabilities"].values())


def test_native_synthetic_cli_self_report_is_insufficient_then_exact_file_read_passes(tmp_path, monkeypatch):
    path = tmp_path / "test-bin"
    path.mkdir()
    program = path / "mock.py"
    program.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "if '--version' in sys.argv: print('codex-cli 1.2.3')\n"
        "elif '--help' in sys.argv: print('--json --sandbox')\n"
        "elif 'login' in sys.argv: print('Not logged in')\n"
        "else:\n"
        "    assert 'read-only' in sys.argv and 'reference.txt' in sys.stdin.read()\n"
        "    message = Path('reference.txt').read_text().strip() if os.getenv('OFFICE_TEST_READ') else 'Read succeeded'\n"
        "    print(json.dumps({'type':'turn.started'}))\n"
        "    print(json.dumps({'type':'item.completed', 'item':{'type':'agent_message','text': message}}))\n"
        "    if not os.getenv('OFFICE_INCOMPLETE_TURN'): print(json.dumps({'type':'turn.completed'}))\n",
        encoding="utf-8")
    if os.name == "nt":
        launcher = path / "codex.cmd"
        launcher.write_text(f'@"{sys.executable}" "%~dp0mock.py" %*\n')
    else:
        launcher = path / "codex"
        launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(program))} "$@"\n')
        launcher.chmod(0o700)
    git = evidence.resolve_executable("git")
    # Only the synthetic CLI and Git are on PATH.
    monkeypatch.setenv("PATH", os.pathsep.join((str(path), str(Path(git).parent))))
    monkeypatch.delenv("OFFICE_TEST_READ", raising=False)
    db = tmp_path / "journal"
    assert evidence.verify_engine(db, "codex", "read")["reason"] == "fixture_mismatch"
    monkeypatch.setenv("OFFICE_TEST_READ", "1")
    monkeypatch.setenv("OFFICE_INCOMPLETE_TURN", "1")
    assert evidence.verify_engine(db, "codex", "read")["reason"] == "provider_failed"
    monkeypatch.delenv("OFFICE_INCOMPLETE_TURN")
    assert evidence.verify_engine(db, "codex", "read")["status"] == "passed"
    resolve = evidence.resolve_executable
    monkeypatch.setattr(evidence, "resolve_executable", lambda name: resolve(name) if name == "codex" else None)
    entry = snapshot(evidence.StudioEngineCatalog(db))["codex"]
    assert entry["version"] == "1.2.3" and entry["capabilities"]["read"]["status"] == "passed"

@pytest.mark.parametrize(("engine", "stdout", "stderr", "value"), [
    ("codex", "node.js 22.11.0", "", None),
    ("codex", "Node.js 22.11.0\ncodex-cli 0.115.0", "", "0.115.0"),
    ("codex", "codex-cli 0.115.0", "codex-cli 0.114.0", None),
    ("claude", "2.1.5 (Claude Code)", "Warning: Node 21.0.0", "2.1.5"),
    ("cline", "Version: v1.2.3", "", "1.2.3"),
    ("gemini", "", "gemini 0.11.0-preview.1", "0.11.0-preview.1"),
    ("gemini", "", "cline 1.2.3", None),
])
def test_version_requires_unambiguous_cli_version_line(monkeypatch, engine, stdout, stderr, value):
    monkeypatch.setattr(engine_status, "_probe", lambda *_: (subprocess.CompletedProcess([], 0, stdout, stderr), "ok"))
    assert engine_status.probe_version(engine, "fake", 5) == (value, "ok" if value else "unknown_version")


@pytest.mark.parametrize("terminal", [None, {"type": "turn.failed"}, {"type": "error"}, {"type": "turn.completed"}])
def test_codex_fixture_requires_completed_turn(terminal):
    from agent_office.fixture_completion import parse_fixture_completion
    message = {"type": "item.completed", "item": {"type": "agent_message", "text": "correct"}}
    records = [{"type": "turn.started"}, message]
    if terminal is not None:
        records.append(terminal)
    output = "\n".join(map(json.dumps, records))
    assert (parse_fixture_completion("codex", output).returncode == 0) == (terminal == {"type": "turn.completed"})
    assert parse_fixture_completion("codex", output + '\n{"type":"turn.failed"}').returncode != 0


@pytest.mark.parametrize(("subtype", "is_error", "success"), [
    ("success", False, True), ("success", True, False), ("error_max_turns", False, False), ("success", None, False),
])
def test_claude_fixture_checks_model_result(subtype, is_error, success):
    from agent_office.fixture_completion import parse_fixture_completion
    record = {"type": "result", "subtype": subtype, "is_error": is_error, "result": "correct"}
    assert (parse_fixture_completion("claude", json.dumps(record)).returncode == 0) is success


@pytest.mark.parametrize("hang", [False, True], ids=["normal-exit", "timeout"])
def test_native_fixture_drains_cli_descendants(tmp_path, monkeypatch, hang):
    from agent_office.fixture_process import execute_fixture
    import socket
    workspace = tmp_path / "repo"
    workspace.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    leaf = bin_dir / "child.py"
    leaf.write_text(
        "import socket, time\nfrom pathlib import Path\n"
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
        "Path('child-port').write_text(str(s.getsockname()[1]))\ntime.sleep(15)\n", encoding="utf-8")
    cli = bin_dir / "synthetic.py"
    cli.write_text(
        "import os, subprocess, sys, time\nfrom pathlib import Path\n"
        "subprocess.Popen([sys.executable, str(Path(__file__).with_name('child.py'))], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name != 'nt')\n"
        "while not Path('child-port').exists(): time.sleep(.01)\n"
        "if os.getenv('OFFICE_SYNTHETIC_HANG'): time.sleep(15)\n"
        "print('{\"type\":\"turn.started\"}')\n"
        "print('{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"done\"}}')\n"
        "print('{\"type\":\"turn.completed\"}')\n", encoding="utf-8")
    if os.name == "nt":
        (bin_dir / "codex.cmd").write_text(f'@"{sys.executable}" "%~dp0synthetic.py" %*\n')
    else:
        launcher = bin_dir / "codex"
        launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(cli))} "$@"\n')
        launcher.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir))
    if hang:
        monkeypatch.setenv("OFFICE_SYNTHETIC_HANG", "1")
    else:
        monkeypatch.delenv("OFFICE_SYNTHETIC_HANG", raising=False)
    result = execute_fixture("codex", "Please read.", workspace=workspace, allow_edits=False, timeout=5 if hang else 15)
    assert (result.returncode == 0) is not hang
    port = int((workspace / "child-port").read_text())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))

def test_unvetted_cline_version_does_not_report_support_or_attempt_a_live_check(tmp_path, installed, monkeypatch):
    def probe(argv, timeout):
        text = "3.0.73" if "--version" in argv else "--acp --auto-approve"
        return subprocess.CompletedProcess(argv, 0, text, ""), "ok"
    monkeypatch.setattr(engine_status, "_probe", probe)
    entry = engine_status.probe_engine("cline", str(installed[0]))
    assert entry["version"] == "3.0.73" and entry["probe_status"] == "unsupported_version"
    assert entry["installed"] and not entry["headless_support"]
    monkeypatch.setattr(evidence, "_run_fixture", lambda *_: pytest.fail("Unexpected provider invocation"))
    db = tmp_path / "journal"
    assert evidence.verify_engine(db, "cline", "read")["reason"] == "unsupported_version"
    row = snapshot(evidence.StudioEngineCatalog(db))["cline"]
    assert row["capabilities"]["read"] == {"status": "not_checked", "checked_at": None}
