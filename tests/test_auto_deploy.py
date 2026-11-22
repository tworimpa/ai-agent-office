from dataclasses import replace
from uuid import uuid4

import pytest

from agent_office import (
    auto_deploy as auto,
    deployment,
    policy_github,
    release_store,
    runtime_ops,
    stable_history,
)
from test_deployment import Interrupted, admit, journal, machine
from test_release_store import IMAGE, release, source
from test_stable_history import passing


@pytest.fixture
def prepared(machine, journal, release, source, passing, monkeypatch):
    root, base = release
    target = machine.target
    deployment.apply_review(root, target, deployment.review(root, target, 1))
    # A normal merged commit, with genuine local objects imported into the release object store.
    git = lambda *args: release_store.run_git(source, *args).decode().strip()
    (source / "example.txt").write_text("reviewed\n")
    git("add", "example.txt")
    git("commit", "-m", "reviewed work")
    head = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    merge = git("commit-tree", tree, "-p", base, "-p", head, "-m", "Merge PR")
    git("update-ref", "refs/remotes/origin/main", merge)
    proof = policy_github.Attestation(10, head, merge, tree, (10, 20), ("fixture check",), (1, 1), merge)
    monkeypatch.setattr(auto, "_mirror", lambda root: source)
    monkeypatch.setattr(policy_github, "attest", lambda repo, sha, b, path: proof)
    monkeypatch.setattr(runtime_ops, "start_service", machine.restart)
    return root, target, merge, proof


@pytest.fixture
def enrolled(prepared):
    root, target, *_ = prepared
    assert auto.enroll(root, target, auto.binding(root, target, IMAGE))["state"] == "enabled"
    return prepared


@pytest.fixture
def split(prepared, monkeypatch):
    root, target, _, proof = prepared
    roles = {"operator_sid": "operator", "validator_sid": "validator"}
    evidence = {**roles, "session_id": uuid4().hex, "signer": "b" * 64}
    bound = auto.binding(root, target, IMAGE)
    auto.claim_session(root, target, bound, evidence["session_id"], roles)

    class Validator:
        def attest(self, *args):
            return proof

        def validate(self, sha, base, image):
            return {"passed": True, "exit_code": 0, "output": "session Docker", "image": image, "sha": sha}

        def ping(self):
            return True

    monkeypatch.setattr(policy_github, "attest", lambda *args: pytest.fail("operator tried GitHub"))
    monkeypatch.setattr(release_store.release_validation, "validate_candidate",
                        lambda *args: pytest.fail("operator tried Docker"))
    return Validator(), evidence, roles, bound


def test_split_deployment_checks_signed_tree_and_journals_the_actors(prepared, split):
    root, target, sha, _ = prepared
    validator, evidence, roles, bound = split
    with pytest.raises(ValueError, match="already started"):
        auto.claim_session(root, target, bound, evidence["session_id"], roles)
    with pytest.raises(ValueError, match="requires its authorized split"):
        auto.run_once(root, target)
    result = auto.run_once(root, target, validator=validator, session_evidence=evidence)
    assert result["state"] == "healthy" and result["sha"] == sha
    assert release_store.review(root, result["candidate"])["validation"]["output"] == "session Docker"
    attested = next(r["details"] for r in auto.status(root, target)["history"] if r["action"] == "attested")
    assert all(attested[k] == v for k, v in evidence.items())
    auto.disable(root, target)
    with pytest.raises(ValueError, match="enabled enrollment"):
        auto.claim_session(root, target, bound, uuid4().hex, roles)


def test_split_rejects_different_tree_and_old_logon(prepared, split, monkeypatch):
    root, target, _, proof = prepared
    validator, evidence, roles, bound = split
    monkeypatch.setattr(validator, "attest", lambda *args: replace(proof, tree="0" * 40))
    with pytest.raises(ValueError, match="local merge tree"):
        auto.run_once(root, target, validator=validator, session_evidence=evidence)
    auto.claim_session(root, target, bound, uuid4().hex, roles)
    with pytest.raises(ValueError, match="superseded"):
        auto.run_once(root, target, validator=validator, session_evidence=evidence)
    assert stable_history.page(root)["current"]["seq"] == 1


def test_disconnected_validator_can_retry_same_candidate(prepared, split, monkeypatch):
    root, target, _, _ = prepared
    validator, evidence, *_ = split
    with monkeypatch.context() as patch:
        def disconnect(*args):
            raise OSError("disconnected")
        patch.setattr(validator, "validate", disconnect)
        with pytest.raises(OSError, match="disconnected"):
            auto.run_once(root, target, validator=validator, session_evidence=evidence)
    assert stable_history.page(root)["current"]["seq"] == 1
    assert auto.run_once(root, target, validator=validator, session_evidence=evidence)["state"] == "healthy"


def test_eligible_release_uses_existing_validation_promotes_once_and_restarts(enrolled, machine):
    root, target, sha, proof = enrolled
    result = auto.run_once(root, target)
    assert result["state"] == "healthy" and result["seq"] == 2
    assert machine.actual["sha"] == sha
    history = auto.status(root, target)["history"]
    assert [e["action"] for e in history] == ["healthy", "attested", "authorized", "enabled"]
    assert history[2]["details"]["head"] == proof.head
    calls = machine.actions[:]
    assert auto.run_once(root, target) == {"state": "unchanged", "sha": sha}
    assert machine.actions == calls
    assert auto.disable(root, target)["state"] == "disabled"
    assert auto.run_once(root, target)["state"] == "disabled"


def test_disallowed_or_busy_never_promotes(enrolled, machine, journal, monkeypatch):
    root, target, sha, _ = enrolled
    before = machine.actions[:]

    def failed(*args):
        raise ValueError("required check failed")

    monkeypatch.setattr(policy_github, "attest", failed)
    with pytest.raises(ValueError, match="required check"):
        auto.run_once(root, target)
    admit(journal)
    assert auto.run_once(root, target)["state"] == "busy"
    assert machine.actions == before and stable_history.page(root)["current"]["seq"] == 1


def test_revocation_during_validation_prevents_promotion(enrolled, machine, monkeypatch):
    root, target, _, _ = enrolled
    original = release_store.verify_candidate

    def revoke(*args):
        result = original(*args)
        auto.disable(root, target)
        return result

    monkeypatch.setattr(release_store, "verify_candidate", revoke)
    with pytest.raises(ValueError, match="revoked"):
        auto.run_once(root, target)
    assert stable_history.page(root)["current"]["seq"] == 1
    assert machine.actual["seq"] == 1


def test_failed_start_restores_previous_without_periodic_redeploy(enrolled, machine):
    root, target, sha, _ = enrolled
    machine.failures.append("unhealthy")
    assert auto.run_once(root, target)["service_outcome"] == "deploy_failed_restored"
    assert machine.actual["seq"] == 1
    before = machine.actions[:]
    assert auto.run_once(root, target) == {"state": "needs_operator", "sha": sha}
    assert machine.actions == before


def test_interruption_after_promotion_resumes_only_same_intent(enrolled, machine, monkeypatch):
    root, target, sha, _ = enrolled
    apply = deployment.apply_review

    class PowerLoss(BaseException):
        pass

    monkeypatch.setattr(deployment, "apply_review", lambda *a, **kw: (_ for _ in ()).throw(PowerLoss()))
    with pytest.raises(PowerLoss):
        auto.run_once(root, target)
    assert stable_history.page(root)["current"]["seq"] == 2 and machine.actual["seq"] == 1
    monkeypatch.setattr(deployment, "apply_review", apply)
    assert auto.run_once(root, target)["state"] == "healthy"
    assert machine.actual["sha"] == sha and stable_history.page(root)["current"]["seq"] == 2


@pytest.mark.parametrize("interrupted_at", ["stop", "boot"])
def test_started_operation_recovers_offline(enrolled, machine, monkeypatch, interrupted_at):
    root, target, _, _ = enrolled

    def cut_power(config):
        machine.stop(config)
        raise Interrupted()

    if interrupted_at == "stop":
        monkeypatch.setattr(runtime_ops, "stop_service", cut_power)
    else:
        machine.crash_after_boot = True
    with pytest.raises(Interrupted):
        auto.run_once(root, target)
    monkeypatch.setattr(runtime_ops, "stop_service", machine.stop)
    monkeypatch.setattr(auto, "_mirror", lambda *args: pytest.fail("offline recovery contacted GitHub"))
    result = auto.run_once(root, target)
    expected = ("deploy_failed_restored", 1) if interrupted_at == "stop" else ("healthy", 2)
    assert (result["service_outcome"], machine.actual["seq"]) == expected
    assert result["operation"] == deployment.recent_operations(target)[0]["id"]


def test_manual_deployment_after_authorization_takes_precedence(enrolled, machine, monkeypatch):
    root, target, _, _ = enrolled

    def interrupt(*args, **kwargs):
        raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(deployment, "apply_review", interrupt)
        with pytest.raises(Interrupted):
            auto.run_once(root, target)
    machine.run(2)
    machine.run(1)
    actions = machine.actions[:]
    with pytest.raises(ValueError, match="predecessor or manual service decision changed"):
        auto.run_once(root, target)
    assert machine.actions == actions and machine.actual["seq"] == 1
