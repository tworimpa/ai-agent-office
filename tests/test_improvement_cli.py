import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from agent_office import cli, release_validation
from test_store import RUN, seeded


class OperatorTerminal(io.StringIO):
    def isatty(self):
        return True


def test_improvement_cli_from_selected_history_through_review_and_checkout(tmp_path, monkeypatch, capsys, request):
    db = tmp_path / "history.db"
    with seeded(db) as store:
        store.record_transition(RUN, operation_key="failed", event_type="RUN_FINISHED",
                                state="FAILED", outcome="TEST_FAILED", payload={"phase": "test"})
    repo = tmp_path / "source"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo).decode().strip()
    git("init", "-q")
    (repo / "message.txt").write_bytes(b"before\n")
    docker = os.environ.get("OFFICE_DOCKER_ACCEPTANCE") == "1"
    if docker:
        package = Path("src/agent_office")
        (repo / package).mkdir(parents=True)
        for module in ("__init__.py", "models.py", "engines.py", "events.py"):
            shutil.copyfile(Path(__file__).resolve().parents[1] / package / module, repo / package / module)
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    # Install a fake provider only in the test PATH. The real adapter and supervisor handle its output.
    tools_dir = tmp_path / "mock tools"
    tools_dir.mkdir()
    worker = tools_dir / "worker.py"
    worker.write_text(
        "import json,sys\n"
        "row=json.loads(sys.stdin.read().splitlines()[-1])[0]\n"
        "proposal=dict(title='Improve output <tag>', rationale='Regression in example run',"
        "expected='Updated output',citations=[dict(run_id=row['run_id'],event=row['events'][-1])],"
        "patch='--- a/message.txt\\n+++ b/message.txt\\n@@ -1 +1 @@\\n-before\\n+after\\n')\n"
        "print(json.dumps(dict(type='result',subtype='success',is_error=False,result=json.dumps(proposal))))\n",
        encoding="utf-8")
    if os.name == "nt":
        (tools_dir / "claude.cmd").write_text(f'@"{sys.executable}" "{worker}"\r\n', encoding="utf-8")
    else:
        shim = tools_dir / "claude"
        shim.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(worker))}\n")
        shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools_dir) + os.pathsep + os.environ["PATH"])

    root = tmp_path / "release"
    image = request.getfixturevalue("trusted_validator_image") if docker else "sha256:" + "b" * 64
    command = ["--db", str(db), "improve"]
    operator = [*command, "--release", str(root)]
    def run(args, code=0):
        actual = cli.main(args)
        output = capsys.readouterr()
        assert actual == code, output
        return json.loads(output.out or output.err)

    artifact = tmp_path / "proposal.json"
    run([*command, "propose", "--repo", str(repo), "--base", base, "--run", RUN,
         "--engine", "claude", "--out", str(artifact)])
    run([*operator, "init", "--repo", str(repo), "--revision", base, "--image", image])
    forged = json.loads(artifact.read_text(encoding="utf-8"))
    forged["history"][0]["request"] = "A different original task"
    bad = tmp_path / "edited.json"
    bad.write_text(json.dumps(forged))
    assert "error" in run([*operator, "submit", str(bad)], 2)
    entry = run([*operator, "submit", str(artifact)])
    cid, sha = entry["id"], entry["sha"]
    assert run([*operator, "list"])["candidates"][0]["id"] == cid
    unapproved = run([*operator, "checkout-stable", str(tmp_path / "first")])
    assert unapproved["stable"] == base
    def validate(git_dir, candidate, pinned_image):
        assert candidate == sha and pinned_image == image
        return dict(sha=candidate, image=pinned_image, passed=True, exit_code=0, output="regression ok")
    if not docker:
        monkeypatch.setattr(release_validation, "validate_candidate", validate)
    assert run([*operator, "verify", cid])["state"] == "passed"
    page = tmp_path / "review.html"
    run([*operator, "review", cid, "--html", str(page)])
    html = page.read_text(encoding="utf-8")
    evidence = "Passed: request redaction" if docker else "regression ok"
    assert all(s in html for s in (sha, base, RUN, evidence, "+after", "&lt;tag&gt;"))
    assert "<tag>" not in html
    flags = [*operator, "approve", cid, "--sha", sha]
    monkeypatch.setattr(sys, "stdin", io.StringIO("approve " + sha))
    assert "error" in run(flags, 2)
    def interactive(answer):
        with monkeypatch.context() as m:
            m.setattr(sys, "stdin", OperatorTerminal(answer))
            screen = OperatorTerminal()
            m.setattr(sys, "stderr", screen)
            code = cli.main(flags)
            assert sha in screen.getvalue()
            return code
    assert interactive("wrong\n") == 2
    assert run([*operator, "review", cid])["state"] == "passed"
    assert interactive("approve " + sha + "\n") == 0
    assert json.loads(capsys.readouterr().out)["state"] == "approved"
    dest = tmp_path / "approved checkout"
    assert run([*operator, "checkout-stable", str(dest)])["stable"] == sha
    assert (dest / "message.txt").read_bytes() == b"after\n"
    assert (repo / "message.txt").read_bytes() == b"before\n"
