"""Provider-neutral observations and a conservative code-check retry policy."""
from datetime import datetime
from hashlib import sha256
import re

from .events import redact_request
from .store import encode


def fingerprint(*parts):
    return sha256(encode(parts).encode("utf-8")).hexdigest()


def external_text(text):
    text = re.sub(r"-----BEGIN [\w ]*PRIVATE KEY-----[\s\S]*?(?:-----END [\w ]*PRIVATE KEY-----|$)",
                  "[redacted]", text)
    text = re.sub(r"\bgh[opsur]_[A-Za-z0-9_]+", "[redacted]", text)
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", text)
    return redact_request(text)


def snapshot(issue, url):
    body = issue.get("body") or ""
    title = issue["title"]
    if not isinstance(body, str) or not isinstance(title, str) or len(body) > 65536 or len(title) > 1024:
        raise ValueError("INVALID_ISSUE")
    return {"id": issue["id"], "number": issue["number"], "title": external_text(title), "body": external_text(body),
            "digest": fingerprint(title, body), "scope_digest": fingerprint(title.strip(), body.strip()),
            "body_bytes": len(body.encode("utf-8")),
            "html_url": url, "url": url, "updated_at": issue.get("updated_at")}


def label_actor(client, timeline, label):
    """A current label alone does not identify who applied it."""
    for event in reversed(timeline):
        if (event.get("label") or {}).get("name", "").casefold() != label.casefold():
            continue
        if event.get("event") == "unlabeled":
            return None
        if event.get("event") == "labeled":
            actor = event.get("actor") or {}
            if actor.get("type") == "User" and not client.is_app_actor(event):
                return actor.get("id")
            return None
    return None


def time_at_least(value, lower):
    try:
        return datetime.fromisoformat(value) >= datetime.fromisoformat(lower)
    except (ValueError, TypeError):
        return False


def answer_text(comment, goal_id, question):
    if not question or question.get("kind") not in {"technical", "product"}:
        return None
    if comment.get("updated_at") != comment.get("created_at"):
        return None
    if not time_at_least(comment.get("created_at"), question.get("created_at")):
        return None
    prefix = f"/office answer {goal_id} {question['id']} "
    body = comment.get("body") or ""
    if body.startswith(prefix):
        answer = body[len(prefix):].strip()
        if answer and len(answer.encode("utf-8")) <= 4096:
            return external_text(answer)
    return None


_INFRA = re.compile(r"runner (?:lost|offline|shutdown|stopped)|lost communication|infrastructure|"
                   r"failed to (?:download|connect|checkout)|service unavailable|billing|quota|"
                   r"connection (?:reset|refused)|rate limit|out of disk|no space left|"
                   r"network (?:error|failure)|internal server error", re.I)


_TEST_FAILURE = re.compile(r"\bAssertionError\b|\bassertion failed\b|\b[1-9]\d* tests? failed\b|"
                           r"\b[1-9]\d* failed, \d+ passed\b|\bFAIL(?:ED)? [:]?\s*tests?[/\\.]", re.I)


def check_observation(item, head, allowed, *, status=False):
    sha = item.get("sha" if status else "head_sha")
    name = item.get("context" if status else "name") or ""
    conclusion = item.get("state" if status else "conclusion") or "pending"
    output = item.get("output") or {}
    diagnostic = "\n".join(str(v or "") for v in (
        output.get("title"), output.get("summary"), output.get("text"), item.get("description")))
    classification = "observed"
    if sha != head:
        classification = "stale"
    elif conclusion in {"cancelled", "canceled", "timed_out", "startup_failure", "action_required",
                        "stale", "error"} or _INFRA.search(diagnostic):
        classification = "infrastructure_or_cancelled"
    elif (not status and name in allowed and conclusion == "failure" and _TEST_FAILURE.search(diagnostic)
          and (item.get("app") or {}).get("slug") == "github-actions"
          and ((item.get("app") or {}).get("owner") or {}).get("login") == "github"):
        classification = "code_failure"
    evidence = []
    if classification == "code_failure":
        for line in external_text(diagnostic).splitlines():
            if _TEST_FAILURE.search(line):
                evidence.append(" ".join(line.split())[:360])
            if len(evidence) == 3:
                break
    return {"id": fingerprint("status" if status else "check", item.get("id"), name, sha, conclusion),
            "evidence": "\n".join(evidence),
            "kind": "ci_result", "detail": f"{external_text(name)[:160]} · {conclusion[:40]} · {str(sha)[:64]}",
            "classification": classification, "name": name, "sha": sha, "conclusion": conclusion}
