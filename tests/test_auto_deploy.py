import pytest

from agent_office import auto_deploy as auto, deployment, policy_github, release_store, runtime_ops, stable_history
from test_deployment import admit, journal, machine
from test_release_store import IMAGE, release, source
from test_stable_history import passing


@pytest.fixture
def enrolled(machine, journal, release, source, passing, monkeypatch):
    root,base=release
    target=machine.target
    deployment.apply_review(root,target,deployment.review(root,target,1))
    bound=auto.binding(root,target,IMAGE)
    assert auto.enroll(root,target,bound)["state"] == "enabled"
    # A normal merged commit, with genuine local objects imported into the release object store.
    git=lambda *args: release_store.run_git(source,*args).decode().strip()
    (source/"example.txt").write_text("reviewed\n")
    git("add","example.txt")
    git("commit","-m","reviewed work")
    head=git("rev-parse","HEAD")
    tree=git("rev-parse","HEAD^{tree}")
    merge=git("commit-tree",tree,"-p",base,"-p",head,"-m","Merge PR")
    git("update-ref","refs/remotes/origin/main",merge)
    proof=policy_github.Attestation(10,head,merge,tree,(10,20),("fixture check",),(1,1),merge)
    monkeypatch.setattr(auto,"_mirror",lambda root: source)
    monkeypatch.setattr(policy_github,"attest",lambda repo,sha,b,path: proof)
    monkeypatch.setattr(runtime_ops,"start_service",machine.restart)
    return root,target,merge,proof


def test_eligible_release_uses_existing_validation_promotes_once_and_restarts(enrolled, machine):
    root,target,sha,proof=enrolled
    result=auto.run_once(root,target)
    assert result["state"]=="healthy" and result["seq"]==2
    assert machine.actual["sha"]==sha
    history=auto.status(root,target)["history"]
    assert [e["action"] for e in history]==["healthy","attested","authorized","enabled"]
    assert history[2]["details"]["head"]==proof.head
    calls=machine.actions[:]
    assert auto.run_once(root,target)=={"state":"unchanged","sha":sha}
    assert machine.actions==calls
    assert auto.disable(root,target)["state"]=="disabled"
    assert auto.run_once(root,target)["state"]=="disabled"


def test_disallowed_or_busy_never_promotes(enrolled, machine, journal, monkeypatch):
    root,target,sha,_=enrolled
    before=machine.actions[:]
    def failed(*args):
        raise ValueError("required check failed")
    monkeypatch.setattr(policy_github,"attest",failed)
    with pytest.raises(ValueError,match="required check"):
        auto.run_once(root,target)
    admit(journal)
    assert auto.run_once(root,target)["state"]=="busy"
    assert machine.actions==before and stable_history.page(root)["current"]["seq"]==1


def test_revocation_during_validation_prevents_promotion(enrolled, machine, monkeypatch):
    root,target,_,_=enrolled
    original=release_store.verify_candidate
    def revoke(*args):
        result=original(*args)
        auto.disable(root,target)
        return result
    monkeypatch.setattr(release_store,"verify_candidate",revoke)
    with pytest.raises(ValueError,match="revoked"):
        auto.run_once(root,target)
    assert stable_history.page(root)["current"]["seq"]==1
    assert machine.actual["seq"]==1


def test_failed_start_restores_previous_without_periodic_redeploy(enrolled,machine):
    root,target,sha,_=enrolled
    machine.failures.append("unhealthy")
    assert auto.run_once(root,target)["service_outcome"]=="deploy_failed_restored"
    assert machine.actual["seq"]==1
    before=machine.actions[:]
    assert auto.run_once(root,target)=={"state":"needs_operator","sha":sha}
    assert machine.actions==before


def test_interruption_after_promotion_resumes_only_same_intent(enrolled, machine, monkeypatch):
    root,target,sha,_=enrolled
    apply=deployment.apply_review
    class PowerLoss(BaseException):
        pass
    monkeypatch.setattr(deployment,"apply_review",lambda *a,**kw: (_ for _ in ()).throw(PowerLoss()))
    with pytest.raises(PowerLoss):
        auto.run_once(root,target)
    assert stable_history.page(root)["current"]["seq"]==2 and machine.actual["seq"]==1
    monkeypatch.setattr(deployment,"apply_review",apply)
    assert auto.run_once(root,target)["state"]=="healthy"
    assert machine.actual["sha"]==sha and stable_history.page(root)["current"]["seq"]==2
