"""Agent Office's explicit turn convention for technical and product questions."""
import asyncio
from contextlib import contextmanager


@contextmanager
def operator_wait(inbox, timer):
    """A goal worker keeps its remaining provider time while a decision is outstanding."""
    if getattr(inbox, "goal_task_id", None) is None:
        yield
        return
    loop = asyncio.get_running_loop()
    remaining = timer.when() - loop.time()
    if remaining <= 0:
        raise TimeoutError("Worker's active time has expired")
    timer.reschedule(None)
    try:
        yield
    finally:
        if not timer.expired():
            timer.reschedule(loop.time() + remaining)

FINAL_MARKER = "AGENT_OFFICE_FINAL"
QUESTION_MARKER = "AGENT_OFFICE_QUESTION"
PRODUCT_DECISION_MARKER = "AGENT_OFFICE_PRODUCT_DECISION"
PRODUCT_DECISION = "product_decision"
QUESTION_MARKERS = {QUESTION_MARKER: "question", PRODUCT_DECISION_MARKER: PRODUCT_DECISION}
REQUEST_KINDS = ("question", PRODUCT_DECISION, "permission")
MAX_QUESTIONS = 8

QUESTION_INSTRUCTIONS = (
    "If you need a technical clarification from the operator, end this turn with a message "
    "whose first line is exactly AGENT_OFFICE_QUESTION and whose remaining lines are the question. "
    "Use AGENT_OFFICE_PRODUCT_DECISION instead when the operator must choose product behavior, "
    "requirements, or scope. Tool authorization must use the provider's permission mechanism. "
    "Omit the final answer marker on either kind of question turn."
)


def extract_request(text: str) -> tuple[str, str] | None:
    """Read the reserved line only from a complete provider answer."""
    lines = text.replace("\r\n", "\n").rstrip("\n").split("\n")
    markers = [line for line in lines if line in QUESTION_MARKERS]
    if not markers:
        return None
    if (len(markers) != 1 or lines[0] != markers[0]
            or FINAL_MARKER in lines or not any(line.strip() for line in lines[1:])):
        raise ValueError("invalid Agent Office question turn")
    return QUESTION_MARKERS[markers[0]], "\n".join(lines[1:])


def extract_question(text: str) -> str | None:
    request = extract_request(text)
    return request[1] if request and request[0] == "question" else None


class DelegationWithdrawn(Exception):
    """The proposed reply lost its handoff guard before any bytes were written."""


def continuation_prompt(answer: str, *, source=None, kind="question") -> str:
    if kind == PRODUCT_DECISION:
        return ("Continue the original task in this session using the operator's decision below about "
                "the requested product behavior. It grants no tool permission.\n\n" + answer)
    author = "The verified delegated specialist's" if source == "delegate" else "The operator's"
    return (f"Continue the original task in this session. {author} answer below clarifies "
            "the technical question. It grants no tool permission or change in scope.\n\n" + answer)
