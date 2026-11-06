import json
import pytest
from test_store import seeded, TASK, RUN


def test_request_snapshot_masks_known_tokens_and_caps_utf8(tmp_path):
    secret = "sk-ant-" + "x" * 30
    request = f'API_KEY="private value" TOKEN=hidden Authorization: Bearer abcdef123 {secret} ' + "한글" * 9000
    with seeded(tmp_path / "db") as store:
        from agent_office.models import TaskSpec, OwnerInfo
        from agent_office.workspace import Worktree
        store.create_run(TaskSpec("3" * 32, "project", request, "codex", "codex", 1), "4" * 32,
                         Worktree(tmp_path, "ao/other"), OwnerInfo("host", "owner"))
        snapshot = store.get_task("3" * 32).request_snapshot
        assert len(snapshot.encode("utf-8")) <= 8192
        assert snapshot.endswith("[truncated]")
        for value in ("private value", "hidden", "abcdef123", secret):
            assert value not in snapshot


def test_payload_allows_metadata_only(tmp_path):
    with seeded(tmp_path / "db") as store:
        event = store.record_transition(RUN, operation_key="a", event_type="WORKSPACE_READY",
                                        payload={"phase": "workspace", "stdout": "SECRET", "session": {"token": "SECRET"}})
        assert "stdout" not in event.payload and "session" not in event.payload
        assert "SECRET" not in (tmp_path / "db").read_bytes().decode("utf-8", errors="ignore")


@pytest.mark.parametrize("payload", [{"phase": {"token": "SECRET"}}, {"phase": "SECRET"},
                                      {"duration_ms": True}, {"engine": "unknown"}, {"attempt": -1}])
def test_invalid_nested_or_typed_metadata_is_rejected(tmp_path, payload):
    with seeded(tmp_path / "db") as store:
        with pytest.raises(ValueError):
            store.record_transition(RUN, operation_key="bad", event_type="WORKSPACE_READY", payload=payload)
        assert len(store.list_events(TASK)) == 1


def test_payload_json_limit_and_valid_unicode():
    from agent_office.events import encode_payload, snapshot_request
    assert json.loads(encode_payload({"phase": "run"})) == {"phase": "run"}
    assert len(snapshot_request("한" * 9000).encode()) <= 8192
    with pytest.raises(ValueError):
        encode_payload({"duration_ms": 10 ** 20000})


def test_payload_cannot_disagree_with_transition(tmp_path):
    with seeded(tmp_path / "db") as store:
        with pytest.raises(ValueError):
            store.record_transition(RUN, operation_key="a", event_type="STAGE_STARTED", state="RUNNING",
                                    payload={"state": "READY_FOR_HUMAN_REVIEW"})
