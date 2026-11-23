import json
import shutil
import subprocess

import pytest

from agent_office.goal_checks import _unprivileged, recipe, run_check
from agent_office.goal_peer import _invocation_matches
from agent_office.goal_verification import validate_policy


@pytest.mark.parametrize(("tool", "argv"), [("npm", ["npm", "test"]), ("cargo", ["cargo", "test"]),
                                           ("go", ["go", "test", "./..."])])
def test_project_recipe_runs_the_fixture_and_reports_command_scope(tmp_path, tool, argv):
    check = dict(stage="quick", host="local", argv=argv, pass_exit_code=0, timeout_seconds=120)
    policy = validate_policy(dict(active_seconds=300, max_attempts=2, checks=[check, dict(check, stage="full")]))
    assert recipe(policy["checks"][0]["argv"]) == tool
    if not _unprivileged() or not shutil.which(tool):
        pytest.skip("needs the named host tool and an ordinary verification session")
    if tool == "npm":
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {
            "pretest": "node -e \"process.exit(1)\"", "test": "node -e \"process.exit(0)\""
        }}))
    elif tool == "cargo":
        (tmp_path / "src").mkdir()
        (tmp_path / "src/lib.rs").write_text("#[test] fn works() { assert_eq!(2+2,4); }")
        (tmp_path / "Cargo.toml").write_text('[package]\nname="fixture"\nversion="0.1.0"\nedition="2021"\n')
        subprocess.run([shutil.which(tool), "generate-lockfile", "--offline"], cwd=tmp_path, check=True)
    else:
        (tmp_path / "go.mod").write_text("module example.test/check\n\ngo 1.20\n")
        (tmp_path / "sum_test.go").write_text('package check\nimport "testing"\nfunc TestSum(t *testing.T) {}\n')
    receipt, cases = run_check(tmp_path, check, timeout=120)
    assert receipt["status"] == "passed", receipt
    assert receipt["result_format"] == "exit" and cases == {"command::" + " ".join(argv): "passed"}
    assert _invocation_matches(dict(receipt, recipe_argv=argv))
    assert not _invocation_matches(dict(receipt, argv=receipt["argv"] + ["extra"], recipe_argv=argv))


@pytest.mark.parametrize("argv", [["npm", "install"], ["npm", "run", "test:x;other"], ["go", "run", "."],
                                  ["cargo", "publish"], ["cargo", "test", "--manifest-path", "../other"]])
def test_unregistered_argv_rejected(argv):
    with pytest.raises(ValueError):
        recipe(argv)


def test_external_web_project_opts_into_its_script_without_changing_saved_office_recipes():
    assert recipe(["npm", "--ignore-scripts", "run", "test:web"]) == "npm"
    assert recipe(["npm", "run", "test:web"]) == "web"


def test_node_dependency_content_is_part_of_the_environment(tmp_path):
    import hashlib
    from agent_office.goal_recovery import _environment

    if not shutil.which("node") or not shutil.which("npm"):
        pytest.skip("needs host Node.js and npm")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node test.js"}}')
    module = tmp_path / "node_modules" / "lib" / "helper.js"
    module.parent.mkdir(parents=True)
    def fingerprint(source):
        module.write_text(source)
        digest = hashlib.sha256()
        _environment(tmp_path, [dict(argv=["npm", "test"], host="local")], digest.update)
        return digest.hexdigest()
    assert fingerprint("a") != fingerprint("b")
