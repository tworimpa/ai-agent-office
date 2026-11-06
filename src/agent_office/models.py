"""Persistent records and the task state contract."""
from dataclasses import dataclass
import re

ACTIVE = frozenset({"QUEUED", "RUNNING", "TESTING", "REVIEWING"})
TERMINAL = frozenset({"PLAN_ONLY", "READY_FOR_HUMAN_REVIEW", "WAITING_USER", "FAILED", "CANCELED", "RECOVERY_REQUIRED"})
STATES = ACTIVE | TERMINAL
OUTCOMES = frozenset({"PLAN_ONLY", "APPROVED", "DEVELOPER_FAILED", "TEST_FAILED", "REVIEW_FAILED", "CHANGES_REQUESTED", "REVIEW_UNDECIDED", "CANCELED", "INTERRUPTED"})
TRANSITIONS = {
    "QUEUED": {"RUNNING"}, "RUNNING": {"TESTING", "PLAN_ONLY"},
    "TESTING": {"REVIEWING", "RUNNING"},
    "REVIEWING": {"RUNNING", "READY_FOR_HUMAN_REVIEW", "WAITING_USER"},
}
EVENT_TYPES = frozenset({"RUN_CREATED", "WORKSPACE_READY", "STAGE_STARTED", "STAGE_COMPLETED", "STAGE_FAILED", "RETRY_SCHEDULED", "RUN_FINISHED", "RECOVERY_REQUIRED"})


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("Expected UUID hex ID")
    return value


class OfficeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    project: str
    request_snapshot: str
    developer_engine: str
    reviewer_engine: str
    max_attempts: int


@dataclass(frozen=True)
class OwnerInfo:
    host_name: str
    owner_token: str


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    project: str
    request_snapshot: str
    developer_engine: str
    reviewer_engine: str
    workspace: str
    branch: str
    state: str
    outcome: str | None
    attempts: int
    max_attempts: int
    current_run_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    task_id: str
    host_name: str
    owner_token: str
    state: str
    outcome: str | None
    attempts: int
    started_at: str
    ended_at: str | None
    next_seq: int


@dataclass(frozen=True)
class EventRecord:
    run_id: str
    seq: int
    operation_key: str
    event_type: str
    role: str | None
    engine: str | None
    timestamp: str
    payload: dict
