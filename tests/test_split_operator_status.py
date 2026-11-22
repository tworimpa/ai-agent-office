import json

from agent_office import split_windows_session as session


def test_validator_can_read_latest_outcome_without_receiving_session_credentials(tmp_path, monkeypatch):
    status = tmp_path / "status"
    status.mkdir()
    binding = {"session_group_sid": "group", "operator_sid": "operator", "validator_sid": "validator"}
    def acl(path, *, writers, readers):
        assert path == status and writers == {"group", "operator"} and readers == {"validator"}
    monkeypatch.setattr(session, "_acl", acl)
    session._publish_status(tmp_path, binding, {"state": "healthy", "seq": 10, "operation": "first",
                                               "capability": "never publish"})
    session._publish_status(tmp_path, binding, {"state": "unchanged"})
    latest = json.loads((status / "last.json").read_text(encoding="utf8"))
    deployed = json.loads((status / "last-deployment.json").read_text(encoding="utf8"))
    assert latest["state"] == "unchanged" and deployed["seq"] == 10
    assert deployed["operator_sid"] == "operator" and "capability" not in deployed
