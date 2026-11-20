import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from agent_office.dialogue import operator_wait
from agent_office.goal_checks import _command, _no_linux_capabilities, _unprivileged, run_check
from agent_office.goal_host_checks import definition
from test_studio_http import project


def test_host_reviewed_recipe_pins_program_and_expected_returncode(tmp_path, monkeypatch):
    web, _ = project(tmp_path)
    repo = web.parent
    script = tmp_path / "check.py"
    script.write_text("raise SystemExit(7)\n")
    spec = {"project":str(repo), "command":[sys.executable, str(script)], "inputs":[str(script)], "result":"exit"}
    catalog = tmp_path / "host-checks.json"
    catalog.write_text(json.dumps({"compat":spec}))
    monkeypatch.setenv("AGENT_OFFICE_GOAL_RECIPES", str(catalog))
    _, digest = definition("compat")
    check = {"stage":"full","host":"local","argv":["host-check","compat",digest],"pass_exit_code":7}
    assert _command(repo, check, tmp_path/"report.xml") == (spec["command"], "host_exit")
    if _unprivileged():
        receipt, cases = run_check(repo, check, timeout=10)
        assert (receipt["exit_code"], receipt["status"], cases) == (7, "passed", {"host-check::compat":"passed"})
    script.write_text("raise SystemExit(0)\n")
    with pytest.raises(ValueError, match="달라졌습니다"):
        _command(repo, check, tmp_path/"report.xml")


def test_goal_decision_wait_preserves_only_remaining_provider_time():
    answered = []
    async def conversation(channel):
        async with asyncio.timeout(0.2) as timer:
            deadline = timer.when()
            with operator_wait(channel, timer):
                await asyncio.sleep(0.3)
            answered.append(channel.goal_task_id)
            assert timer.when() > deadline
            await asyncio.sleep(0.3)
    with pytest.raises(TimeoutError):
        asyncio.run(conversation(SimpleNamespace(goal_task_id="developer")))
    with pytest.raises(TimeoutError):
        asyncio.run(conversation(SimpleNamespace()))
    assert answered == ["developer"]


def test_nonroot_process_requires_empty_linux_capability_sets():
    empty = "CapPrm: 0\nCapEff: 0\nCapAmb: 0\nCapInh: 0\nCapBnd: ffffffff\n"
    assert _no_linux_capabilities(empty)
    for field in ("CapPrm", "CapEff", "CapAmb", "CapInh"):
        assert not _no_linux_capabilities(empty.replace(field + ": 0", field + ": 400"))
    assert not _no_linux_capabilities("CapEff: 0")
