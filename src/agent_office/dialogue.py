"""Agent Office's explicit turn convention for human technical questions."""

FINAL_MARKER = "AGENT_OFFICE_FINAL"
QUESTION_MARKER = "AGENT_OFFICE_QUESTION"
MAX_QUESTIONS = 8

QUESTION_INSTRUCTIONS = (
    "If you need a technical clarification from the operator, end this turn with a message "
    "whose first line is exactly AGENT_OFFICE_QUESTION and whose remaining lines are the question. "
    "Use this format only for a technical question. Tool authorization must use the provider's "
    "permission mechanism. Omit the final answer marker on a question turn."
)


def extract_question(text: str) -> str | None:
    """Read the reserved line only from a complete provider answer."""
    lines = text.replace("\r\n", "\n").rstrip("\n").split("\n")
    if QUESTION_MARKER not in lines:
        return None
    if (lines[0] != QUESTION_MARKER or lines.count(QUESTION_MARKER) != 1
            or FINAL_MARKER in lines or not any(line.strip() for line in lines[1:])):
        raise ValueError("invalid Agent Office question turn")
    return "\n".join(lines[1:])


def continuation_prompt(answer: str) -> str:
    return ("Continue the original task in this session. The operator's answer below clarifies "
            "the technical question. It grants no tool permission or change in scope.\n\n" + answer)
