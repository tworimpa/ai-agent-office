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


def build_graph(adapters: dict[str, Adapter], *, checkpointer=None):
    def develop(state: TaskState) -> dict:
        count = state.get("attempts", 0) + 1
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
        return {
            "attempts": count,
            "developer_output": result.message,
            "status": "DEVELOPED" if result.returncode == 0 else "FAILED",
            "feedback": "" if result.returncode == 0 else result.message[:2000],
        }

    def after_develop(state: TaskState) -> str:
        if state["status"] != "DEVELOPED":
            return "end"
        return "test" if state["allow_edits"] else "plan_only"

    def plan_only(_state: TaskState) -> dict:
        return {"status": "PLAN_ONLY"}

    def test(state: TaskState) -> dict:
        try:
            completed = subprocess.run(
                state["test_argv"], cwd=state["workspace"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=300, check=False,
            )
            output = (completed.stdout + "\n" + completed.stderr)[-6000:]
            passed = completed.returncode == 0
        except (subprocess.TimeoutExpired, OSError) as exc:
            passed = False
            output = str(exc)
        return {
            "status": "TEST_PASSED" if passed else "TEST_FAILED",
            "test_output": output,
            "feedback": "" if passed else f"Tests failed:\n{output}",
        }

    def after_test(state: TaskState) -> str:
        if state["status"] == "TEST_PASSED":
            return "review"
        return "develop" if state["attempts"] < state["max_attempts"] else "end"

    def review(state: TaskState) -> dict:
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
        if result.returncode != 0:
            status = "REVIEW_FAILED"
        elif parse_review_verdict(output) == "APPROVED":
            status = "READY_FOR_HUMAN_REVIEW"
        elif parse_review_verdict(output) == "CHANGES_REQUESTED":
            status = "CHANGES_REQUESTED"
        else:
            status = "NEEDS_HUMAN_REVIEW"
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
