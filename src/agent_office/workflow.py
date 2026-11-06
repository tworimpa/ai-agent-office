"""LangGraph workflow. External CLI execution and user interrupts are separate concerns."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from .adapters import AgentResult
from .workspace import git_summary


class Adapter(Protocol):
    def execute(self, prompt: str, *, workspace: Path, allow_edits: bool) -> AgentResult: ...


class TaskState(TypedDict, total=False):
    task: str
    workspace: str
    developer: str
    reviewer: str
    test_argv: list[str]
    allow_edits: bool
    attempts: int
    max_attempts: int
    status: str
    feedback: str
    developer_output: str
    test_output: str
    review_output: str


def parse_review_verdict(output: str) -> str:
    lines = re.findall(r"(?im)^[ \t]*VERDICT:[ \t]*([^\r\n]*)", output)
    if len(lines) == 1 and lines[0].strip().upper() in ("APPROVED", "CHANGES_REQUESTED"):
        return lines[0].strip().upper()
    return "UNKNOWN"


def build_graph(adapters: dict[str, Adapter], *, checkpointer=None, recorder=None):
    def emit(event, phase, count, *, state=None, engine=None, returncode=None):
        if recorder is not None:
            payload = {"phase": phase, "role": {"developer": "developer", "test": "tester", "review": "reviewer"}[phase], "engine": engine}
            if returncode is not None:
                payload["returncode"] = returncode
            recorder.record(operation_key=f"{recorder.run_id}:{count}:{phase}:{event}", event_type=event,
                            state=state, attempts=count, payload=payload)

    def develop(state: TaskState) -> dict:
        count = state.get("attempts", 0) + 1
        emit("STAGE_STARTED", "developer", count, state="RUNNING", engine=state["developer"])
        prompt = (
            "You are the development worker for this repository. "
            "Stay within the Git worktree. Never commit, push, merge, deploy, "
            "or change security/approval policy. "
            "If requirements are ambiguous or approval is needed, explicitly "
            "explain the blocker; do not silently assume permission.\n\n"
            f"Task:\n{state['task']}\n\n"
            f"Feedback from last attempt:\n{state.get('feedback', '(none)')}"
        )
        result = adapters[state["developer"]].execute(
            prompt, workspace=Path(state["workspace"]),
            allow_edits=state["allow_edits"],
        )
        emit("STAGE_COMPLETED" if result.returncode == 0 else "STAGE_FAILED", "developer", count,
             engine=state["developer"], returncode=result.returncode)
        return {
            "attempts": count,
            "developer_output": result.message,
            "status": "DEVELOPED" if result.returncode == 0 else "RECOVERY_REQUIRED" if result.returncode == 124 else "FAILED",
            "feedback": "" if result.returncode == 0 else result.message[:2000],
        }

    def after_develop(state: TaskState) -> str:
        if state["status"] != "DEVELOPED":
            return "end"
        return "test" if state["allow_edits"] else "plan_only"

    def plan_only(_state: TaskState) -> dict:
        return {"status": "PLAN_ONLY"}

    def test(state: TaskState) -> dict:
        emit("STAGE_STARTED", "test", state["attempts"], state="TESTING")
        timed_out = False
        returncode = None
        try:
            completed = subprocess.run(
                state["test_argv"], cwd=state["workspace"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=300, check=False,
            )
            output = (completed.stdout + "\n" + completed.stderr)[-6000:]
            passed = completed.returncode == 0
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            passed = False
            timed_out = True
            output = str(exc)
            returncode = 124
        except OSError as exc:
            passed = False
            output = str(exc)
        emit("STAGE_COMPLETED" if passed else "STAGE_FAILED", "test", state["attempts"], returncode=returncode)
        if not passed and not timed_out and state["attempts"] < state["max_attempts"]:
            emit("RETRY_SCHEDULED", "test", state["attempts"], state="RUNNING")
        return {
            "status": "TEST_PASSED" if passed else "RECOVERY_REQUIRED" if timed_out else "TEST_FAILED",
            "test_output": output,
            "feedback": "" if passed else f"Tests failed:\n{output}",
        }

    def after_test(state: TaskState) -> str:
        if state["status"] == "RECOVERY_REQUIRED":
            return "end"
        if state["status"] == "TEST_PASSED":
            return "review"
        return "develop" if state["attempts"] < state["max_attempts"] else "end"

    def review(state: TaskState) -> dict:
        emit("STAGE_STARTED", "review", state["attempts"], state="REVIEWING", engine=state["reviewer"])
        prompt = (
            "Review the requested changes without modifying files. "
            "Respond using a standalone line exactly 'VERDICT: APPROVED' "
            "or 'VERDICT: CHANGES_REQUESTED', followed by reasons. "
            "Do not approve absent evidence.\n\n"
            f"Original task:\n{state['task']}\n\n"
            f"Working-tree status:\n{git_summary(Path(state['workspace']))}\n\n"
            f"Test results:\n{state.get('test_output', '')}"
        )
        result = adapters[state["reviewer"]].execute(
            prompt, workspace=Path(state["workspace"]), allow_edits=False,
        )
        output = result.message
        emit("STAGE_COMPLETED" if result.returncode == 0 else "STAGE_FAILED", "review", state["attempts"], engine=state["reviewer"], returncode=result.returncode)
        if result.returncode == 124:
            status = "RECOVERY_REQUIRED"
        elif result.returncode != 0:
            status = "REVIEW_FAILED"
        elif result.truncated:
            status = "NEEDS_HUMAN_REVIEW"
        elif parse_review_verdict(output) == "APPROVED":
            status = "READY_FOR_HUMAN_REVIEW"
        elif parse_review_verdict(output) == "CHANGES_REQUESTED":
            status = "CHANGES_REQUESTED"
        else:
            status = "NEEDS_HUMAN_REVIEW"
        if status == "CHANGES_REQUESTED" and state["attempts"] < state["max_attempts"]:
            emit("RETRY_SCHEDULED", "review", state["attempts"], state="RUNNING", engine=state["reviewer"])
        return {
            "status": status,
            "review_output": output,
            "feedback": f"Review feedback:\n{output[:3000]}" if status == "CHANGES_REQUESTED" else "",
        }

    def after_review(state: TaskState) -> str:
        if state["status"] == "CHANGES_REQUESTED" and state["attempts"] < state["max_attempts"]:
            return "develop"
        return "end"

    graph = StateGraph(TaskState)
    graph.add_node("develop", develop)
    graph.add_node("plan_only", plan_only)
    graph.add_node("test", test)
    graph.add_node("review", review)
    graph.add_edge(START, "develop")
    graph.add_conditional_edges("develop", after_develop, {
        "test": "test", "plan_only": "plan_only", "end": END,
    })
    graph.add_edge("plan_only", END)
    graph.add_conditional_edges("test", after_test, {"review": "review", "develop": "develop", "end": END})
    graph.add_conditional_edges("review", after_review, {"develop": "develop", "end": END})
    return graph.compile(checkpointer=checkpointer)
