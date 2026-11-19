import io
import json
import sqlite3
import sys
from uuid import uuid4

import pytest

from agent_office import cli, deploy_cli, deployment as deploy, runtime_ops, stable_history
from agent_office.ownership import RunBusyError, RunGuard
from test_improvement_cli import OperatorTerminal
from test_release_store import proposal, release, source
from test_stable_history import approve, passed, passing


class Interrupted(BaseException):
    pass


class NativeFixture:
    def __init__(self, root, target, monkeypatch):
        from agent_office import service_binding
        self.root, self.target = root, target
        self.state, self.actual = "stopped", None
        self.health = "refused"
        self.actions = []
        self.failures = []
        self.crash_after_boot = False
        self.signature = "1" * 64
        self.owns = True
        monkeypatch.setattr(service_binding, "inspect_service", lambda config, target, **kwargs: {
            "manager": config["manager"], "service": config["service"], "pid": 111 if self.state == "running" else 0,
            "signature": self.signature})
        monkeypatch.setattr(service_binding, "owns_listener", lambda binding, receipt, port: self.owns)
        monkeypatch.setattr(runtime_ops, "manager_state", lambda config: self.state)
        monkeypatch.setattr(runtime_ops, "probe", self.probe)
        monkeypatch.setattr(runtime_ops, "restart_service", self.restart)
        monkeypatch.setattr(runtime_ops, "stop_service", self.stop)
        monkeypatch.setattr(deploy, "HEALTH_TIMEOUT", 0)

    def probe(self, config):
        return {"state": self.health, "identity": dict(self.actual) if self.actual else None, "detail": ""}

    def restart(self, config):
        stored, selection = deploy.read_selection(self.target)
        assert config == stored
        deploy.verify_slot(self.target, selection["sha"])
        self.actions.append(("restart", selection))
        fault = self.failures.pop(0) if self.failures else None
        if fault == "stop":
            self.state, self.health, self.actual = "stopped", "refused", None
            raise RuntimeError("Failed to start")
        if fault == "interrupt":
            raise Interrupted()
        prior = self.actual
        self.actual = {key: config[key] for key in ("target_id", "release_id", "journal_id", "port")}
        self.actual.update(selection, boot_id=uuid4().hex, pid=111)
        self.state, self.health = "running", "healthy"
        if fault == "stale_boot":
            self.actual["boot_id"] = prior["boot_id"]
        if fault == "unhealthy":
            self.health = "unhealthy"
        if self.crash_after_boot:
            self.crash_after_boot = False
            raise Interrupted()

    def stop(self, config):
        self.actions.append(("stop", None))
        self.state, self.health, self.actual = "stopped", "refused", None

    def run(self, seq=None, *, recover=False):
        reviewed = deploy.review(self.root, self.target, seq, recover=recover)
        return deploy.apply_review(self.root, self.target, reviewed)


@pytest.fixture
def machine(release, tmp_path, monkeypatch):
    root, _ = release
    target = tmp_path / "target"
    deploy.init_target(root, target, manager="systemd", service="office.service", port=8766,
                       journal_id="a" * 32)
    return NativeFixture(root, target, monkeypatch)


def test_historical_deploy_and_verified_noop(machine, release, passing):
    root, base = release
    second = approve(root, passed(root, proposal(base)))
    assert deploy.public_status(machine.target)["health"] == "offline"
    assert machine.run(1)["state"] == "healthy"
    before = dict(machine.actual)
    assert (machine.target / "slots" / base / "example.txt").read_bytes() == b"old\n"
    assert machine.run(1)["state"] == "unchanged"
    assert len(machine.actions) == 1 and machine.actual == before
    assert machine.run(2)["state"] == "healthy"
    assert machine.actual["seq"] == 2 and machine.actual["sha"] == second["sha"]
    assert before["boot_id"] != machine.actual["boot_id"]
    machine.run(1)
    assert deploy.public_status(machine.target)["actual"] == machine.actual
    assert stable_history.page(root)["current"]["seq"] == 2


@pytest.mark.parametrize("fault", ["stop", "unhealthy", "stale_boot"])
def test_failed_restart_restores_actual_previous_with_new_boot(machine, release, passing, fault):
    root, base = release
    approve(root, passed(root, proposal(base)))
    machine.run(1)
    prior = dict(machine.actual)
    machine.failures = [fault]
    result = machine.run(2)
    assert result["state"] == "deploy_failed_restored"
    assert result["previous"] == {"seq": 1, "sha": base}
    assert machine.actual["seq"] == 1
    assert len({action[1]["activation_id"] for action in machine.actions}) == 3
    assert machine.actual["boot_id"] != prior["boot_id"]
    assert deploy.public_status(machine.target)["health"] == "healthy"


def test_first_boot_failure_stops_and_leaves_a_retryable_target(machine):
    machine.failures = ["unhealthy"]
    assert machine.run(1)["state"] == "deploy_failed_stopped"
    assert machine.actions[-1] == ("stop", None)
    status = deploy.public_status(machine.target)
    assert status["selected"] is None and status["actual"] is None and status["last"]["previous"] is None
    assert machine.run(1)["state"] == "healthy"


@pytest.mark.parametrize("previous", [False, True])
def test_throttled_candidate_is_stopped_before_restoration(machine, release, passing, monkeypatch, previous):
    root, base = release
    approve(root, passed(root, proposal(base)))
    if previous:
        machine.run(1)
    prior_restart = machine.restart
    def restart(config):
        _, selection = deploy.read_selection(machine.target)
        if selection["seq"] == 2:
            machine.state, machine.health, machine.actual = "paused", "refused", None
            raise RuntimeError("Service did not reach running state")
        prior_restart(config)
    monkeypatch.setattr(runtime_ops, "restart_service", restart)
    result = machine.run(2)
    assert result["state"] == ("deploy_failed_restored" if previous else "deploy_failed_stopped")
    assert ("stop", None) in machine.actions
    assert (machine.actual["sha"] if previous else machine.actual) == (base if previous else None)


def test_first_deployment_rejects_already_running_or_foreign_port(machine):
    machine.state = "running"
    with pytest.raises(ValueError, match="stopped"):
        machine.run(1)
    machine.state, machine.health = "stopped", "wrong_identity"
    with pytest.raises(ValueError, match="unused port"):
        machine.run(1)
    assert not machine.actions


def test_slot_corruption_rejected_without_following_or_restarting(machine):
    machine.run(1)
    slot = machine.target / "slots" / machine.actual["sha"]
    (slot / "injected.py").write_text("raise Exception()")
    with pytest.raises(ValueError, match="Unexpected file"):
        machine.run(1)
    (slot / "injected.py").unlink()
    (slot / "example.txt").write_bytes(b"BAD\n")
    with pytest.raises(ValueError, match="differs"):
        machine.run(1)
    assert len(machine.actions) == 1


def test_phase_recovery_after_boot_commits_without_another_restart(machine):
    machine.crash_after_boot = True
    with pytest.raises(Interrupted):
        machine.run(1)
    assert deploy.public_status(machine.target)["last"]["state"] == "deploying"
    with pytest.raises(ValueError, match="Recover"):
        machine.run(1)
    assert machine.run(recover=True)["state"] == "healthy"
    assert len(machine.actions) == 1


def test_unfinished_switch_shows_previous_live_version_and_recovery(machine, release, passing):
    root, base = release
    approve(root, passed(root, proposal(base)))
    machine.run(1)
    before = machine.actual
    machine.failures = ["interrupt", "stop"]
    with pytest.raises(Interrupted):
        machine.run(2)
    status = deploy.public_status(machine.target)
    assert status["selected"]["seq"] == 2 and status["actual"] == before and status["health"] == "healthy"
    assert machine.run(recover=True)["state"] == "restore_failed"
    assert deploy.public_status(machine.target)["health"] == "offline"
    assert machine.run(recover=True)["state"] == "deploy_failed_restored"
    assert machine.actual["sha"] == base
    assert len(set(a[1]["activation_id"] for a in machine.actions)) == 3


def test_foreign_identity_prevents_automatic_recovery_or_takeover(machine):
    machine.failures = ["interrupt"]
    with pytest.raises(Interrupted):
        machine.run(1)
    machine.health, machine.state = "wrong_identity", "running"
    assert machine.run(recover=True)["state"] == "restore_failed"
    assert len(machine.actions) == 1
    assert deploy.public_status(machine.target)["actual"] is None


def test_recheck_actual_boot_release_identity_and_target_guard(machine, release, passing):
    root, base = release
    approve(root, passed(root, proposal(base)))
    machine.run(1)
    reviewed = deploy.review(root, machine.target, 2)
    machine.actual["boot_id"] = uuid4().hex
    with pytest.raises(ValueError, match="changed during review"):
        deploy.apply_review(root, machine.target, reviewed)
    with RunGuard(machine.target / deploy.DB_NAME, reviewed["config"]["target_id"]):
        with pytest.raises(RunBusyError):
            machine.run(2)
    with sqlite3.connect(root / "release.sqlite3") as db:
        db.execute("UPDATE studio_release_identity SET id=?", (uuid4().hex,))
    with pytest.raises(ValueError, match="identity"):
        deploy.apply_review(root, machine.target, reviewed)
    assert len(machine.actions) == 1


def test_cli_tty_requires_full_phrase_and_reports_service_failure(machine, monkeypatch, capsys):
    command = ["improve", "--release", str(machine.root), "deploy", "1", "--target", str(machine.target)]
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(command) == 2
    assert "interactive" in capsys.readouterr().err
    reviewed = deploy.review(machine.root, machine.target, 1)
    phrase = deploy_cli._phrase(reviewed)
    for answer, expected in ((phrase[:-1], 2), (phrase, 1)):
        screen = OperatorTerminal()
        monkeypatch.setattr(sys, "stderr", screen)
        monkeypatch.setattr(sys, "stdin", OperatorTerminal(answer + "\n"))
        machine.failures = ["stop"]
        assert cli.main(command) == expected
        assert phrase in screen.getvalue() and "진행 중인 작업" in screen.getvalue()
    assert json.loads(capsys.readouterr().out)["state"] == "deploy_failed_stopped"
    assert len(machine.actions) == 1


def test_reject_target_overlap_and_unreleased_revision(machine, source, tmp_path):
    args = dict(manager="systemd", service="office.service", port=8766, journal_id="b" * 32)
    for dest in (source / "nested", machine.root / "nested", machine.target / "nested"):
        with pytest.raises(ValueError):
            deploy.init_target(machine.root, dest, **args)
    with pytest.raises(ValueError, match="Unknown stable version"):
        machine.run(999)

@pytest.mark.parametrize("linger", [False, True])
def test_unresponsive_tcp_listener_must_disappear_before_restore(release, tmp_path, passing, monkeypatch, linger):
    import socket

    root, base = release
    approve(root, passed(root, proposal(base)))
    target = tmp_path / "hung-target"
    real_probe = runtime_ops.probe
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        deploy.init_target(root, target, manager="systemd", service="test.service", port=port, journal_id="b" * 32)
        native = NativeFixture(root, target, monkeypatch)
        native.run(1)
        native_restart, native_stop = native.restart, native.stop
        use_http = False

        def restart(config):
            nonlocal use_http
            _, chosen = deploy.read_selection(target)
            if chosen["seq"] == 2:
                native.actions.append(("restart", chosen))
                listener.listen(16)
                use_http = True
            else:
                native_restart(config)
                use_http = False

        def stop(config):
            native_stop(config)
            if not linger:
                listener.close()

        def probe(config):
            if use_http:
                return real_probe(config, timeout=0.12) if listener.fileno() >= 0 else real_probe(config)
            return native.probe(config)

        monkeypatch.setattr(runtime_ops, "probe", probe)
        monkeypatch.setattr(runtime_ops, "restart_service", restart)
        monkeypatch.setattr(runtime_ops, "stop_service", stop)
        outcome = native.run(2)
        if linger:
            assert outcome["state"] == "restore_failed" and "포트" in outcome["message"]
            assert [action for action, _ in native.actions] == ["restart", "restart", "stop"]
            listener.close()
            outcome = native.run(recover=True)
        assert outcome["state"] == "deploy_failed_restored"
        assert [action for action, _ in native.actions] == ["restart", "restart", "stop", "restart"]
        assert native.actual["sha"] == base


def test_native_registration_and_listener_ownership_must_survive_review(machine):
    reviewed = deploy.review(machine.root, machine.target, 1)
    machine.signature = "f" * 64
    with pytest.raises(ValueError, match="during review"):
        deploy.apply_review(machine.root, machine.target, reviewed)
    assert not machine.actions
    machine.run(1)
    machine.owns = False
    with pytest.raises(ValueError, match="actual running predecessor"):
        machine.run(1)
    assert len(machine.actions) == 1


def test_recovery_refuses_changed_native_service_command(machine):
    machine.failures = ["interrupt"]
    with pytest.raises(Interrupted):
        machine.run(1)
    machine.signature = "c" * 64
    with pytest.raises(ValueError, match="service command changed"):
        machine.run(recover=True)
    assert len(machine.actions) == 1


def test_replaced_identical_ledger_invalidates_review(machine):
    import shutil

    reviewed = deploy.review(machine.root, machine.target, 1)
    ledger = machine.target / deploy.DB_NAME
    replaced = ledger.with_suffix(".old")
    ledger.rename(replaced)
    shutil.copy2(replaced, ledger)
    with pytest.raises(ValueError, match="ledger file was replaced"):
        deploy.apply_review(machine.root, machine.target, reviewed)
    assert not machine.actions
    assert deploy.public_status(machine.target)["last"] is None


def test_reverted_ledger_after_restart_never_records_success_or_restarts_again(machine, monkeypatch):
    def restart(config):
        machine.restart(config)
        # An independent backup restore removes this operation after native start.
        with sqlite3.connect(machine.target / deploy.DB_NAME) as db:
            db.execute("DELETE FROM operations")
            db.execute("UPDATE selection SET data='null'")

    monkeypatch.setattr(runtime_ops, "restart_service", restart)
    with pytest.raises(ValueError, match="ledger was changed or restored"):
        machine.run(1)
    assert len(machine.actions) == 1
    assert deploy.public_status(machine.target)["last"] is None


def test_web_reader_only_reports_a_running_registered_listener(machine, monkeypatch):
    from agent_office import service_binding

    machine.run(1)
    monkeypatch.setattr(runtime_ops, "restart_service", lambda *args: pytest.fail("native restart"))
    monkeypatch.setattr(runtime_ops, "stop_service", lambda *args: pytest.fail("native stop"))
    assert deploy.public_status(machine.target)["actual"] == machine.actual
    machine.state = "stopped"
    status = deploy.public_status(machine.target)
    assert status["actual"] is None and status["health"] == "unverified"
    machine.state, machine.owns = "running", False
    assert deploy.public_status(machine.target)["health"] == "unverified"
    machine.owns, machine.signature = True, "e" * 64
    assert deploy.public_status(machine.target)["health"] == "unverified"

    def inaccessible(config, target, *, for_control):
        assert for_control is False
        raise PermissionError()

    monkeypatch.setattr(service_binding, "inspect_service", inaccessible)
    assert deploy.public_status(machine.target)["health"] == "unverified"


def test_recovery_confirms_restored_boot_without_repeating_restart(machine, release, passing, monkeypatch):
    root, base = release
    approve(root, passed(root, proposal(base)))
    machine.run(1)
    first_boot = machine.actual["boot_id"]
    machine.failures = ["stop"]

    def interrupt_after_restore(config):
        _, selected = deploy.read_selection(machine.target)
        if selected["seq"] == 1:
            machine.crash_after_boot = True
        machine.restart(config)

    monkeypatch.setattr(runtime_ops, "restart_service", interrupt_after_restore)
    with pytest.raises(Interrupted):
        machine.run(2)
    assert deploy.public_status(machine.target)["last"]["state"] == "restoring"
    assert machine.actual["boot_id"] != first_boot
    assert machine.run(recover=True)["state"] == "deploy_failed_restored"
    assert len(machine.actions) == 3
    audit = deploy.recent_operations(machine.target)
    assert len(audit) == 2
    assert audit[0]["confirmed"] == machine.actual
    assert audit[0]["previous"]["boot_id"] == first_boot
    assert [step["state"] for step in audit[0]["transitions"]] == ["deploying", "restoring", "deploy_failed_restored"]


def test_inconsistent_recovery_selection_never_mutates_service(machine):
    machine.failures = ["interrupt"]
    with pytest.raises(Interrupted):
        machine.run(1)
    with sqlite3.connect(machine.target / deploy.DB_NAME) as db:
        db.execute("UPDATE selection SET data='null' WHERE id=1")
    with pytest.raises(ValueError, match="recorded deployment phase"):
        machine.run(recover=True)
    assert len(machine.actions) == 1
