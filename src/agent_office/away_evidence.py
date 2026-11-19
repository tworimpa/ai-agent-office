"""Read and interpret the small set of committed facts eligible for delegation."""
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib

from .engines import resolve_executable


class DelegationDeferred(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def git(repo: Path, *args: str) -> bytes:
    # Only plumbing with literal paths and raw immutable objects. Missing objects
    # in a partial clone must never invoke transport/credential helpers.
    executable = resolve_executable("git")
    if not executable:
        raise DelegationDeferred("evidence_unavailable")
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_ALLOW_PROTOCOL="", GIT_NO_LAZY_FETCH="1", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    try:
        return subprocess.run(
            [executable, "--no-pager", "--no-replace-objects", "--literal-pathspecs",
             "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull, *args],
            cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, check=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise DelegationDeferred("evidence_unavailable") from None


def require_base(repo: Path, revision: str) -> None:
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
        raise DelegationDeferred("evidence_unavailable")
    root = os.fsdecode(git(repo, "rev-parse", "--show-toplevel").rstrip(b"\r\n"))
    if (Path(root).resolve() != repo.resolve()
            or git(repo, "rev-parse", "--verify", "HEAD^{commit}").strip() != revision.encode()):
        raise DelegationDeferred("worktree_changed")


def read_document(repo: Path, revision: str, path: str) -> str:
    require_base(repo, revision)
    entry = git(repo, "ls-tree", "-zl", revision, "--", path)
    metadata, sep, name = entry.partition(b"\t")
    match = re.fullmatch(rb"100(?:644|755) blob ([0-9a-f]{40}|[0-9a-f]{64}) +([0-9]+)", metadata)
    if not match or not sep or name != path.encode("utf-8") + b"\0" or int(match[2]) > 65536:
        raise DelegationDeferred("evidence_unavailable")
    contents = git(repo, "cat-file", "blob", match[1].decode("ascii"))
    if len(contents) != int(match[2]) or b"\0" in contents:
        raise DelegationDeferred("evidence_unavailable")
    try:
        return contents.decode("utf-8-sig")
    except UnicodeError:
        raise DelegationDeferred("evidence_unavailable") from None


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


_NUMBER = r"(?:0|[1-9][0-9]{0,3})"
_VERSION = rf"{_NUMBER}(?:\.{_NUMBER}){{0,2}}"


def _version_tuple(value: str, *, next_release=False) -> tuple[int, ...]:
    parts = [int(piece) for piece in value.split(".")]
    if next_release:
        parts[-1] += 1
    return tuple(parts + [0] * (3 - len(parts)))


def _supported_version(topic: str, value: str) -> bool:
    if not isinstance(value, str) or len(value) > 48:
        return False
    if topic == "python_version":
        single = rf"(?:(?:>=|<=|==|>|<){_VERSION}|~={_NUMBER}\.{_NUMBER}(?:\.{_NUMBER})?)"
        separator = r",[ ]*"
    else:
        single = rf"(?:>=|<=|>|<|=|~|\^)?{_VERSION}"
        separator = r"[ ]+"
    if re.fullmatch(single, value):
        return not (value.startswith("<") and not value.startswith("<=")
                    and _version_tuple(value[1:]) == (0, 0, 0))
    interval = re.fullmatch(rf"(>=?)({_VERSION}){separator}(<=?)({_VERSION})", value)
    if not interval:
        return False
    if topic == "node_version":
        # npm's >18 starts at 19.0.0; <=18.1 ends just before 18.2.0.
        # No prerelease comparator is admitted by the bounded grammar.
        lower = _version_tuple(interval[2], next_release=interval[1] == ">")
        upper = _version_tuple(interval[4], next_release=interval[3] == "<=")
        return lower < upper
    lower, upper = _version_tuple(interval[2]), _version_tuple(interval[4])
    return lower < upper or (lower == upper and interval[1] == ">=" and interval[3] == "<=")


def _version_fact(topic: str, document: str) -> tuple[str, list[dict]]:
    if topic == "python_version":
        def get_version(source):
            return tomllib.loads(source)["project"]["requires-python"]
        pattern = re.compile(r"""requires-python[ \t]*=[ \t]*(["'])(?P<value>[^"'\r\n]+)\1""")
    else:
        def get_version(source):
            return json.loads(source, object_pairs_hook=unique_object)["engines"]["node"]
        pattern = re.compile(r'"node"[ \t]*:[ \t]*"(?P<value>[^"\\\r\n]+)"')
    try:
        answer = get_version(document)
        if not _supported_version(topic, answer):
            raise ValueError
        # Mark all like-looking fields in a copy and parse once more to bind the
        # citation to the real project/engines key even in a one-line manifest.
        # Only the matched declaration can reach a provider or decision ledger.
        matches = {}

        def mark(match):
            if match["value"] != answer or len(match[0]) > 240:
                return match[0]
            marker = f"AGENT_OFFICE_CITATION_{len(matches)}"
            matches[marker] = match
            start, end = match.span("value")
            return document[match.start():start] + marker + document[end:match.end()]

        marked_document = pattern.sub(mark, document)
        actual = matches.get(get_version(marked_document))
        if actual:
            line = document.count("\n", 0, actual.start()) + 1
            return answer, [{"line": line, "quote": actual[0]}]
    except (ValueError, KeyError, TypeError, RecursionError):
        pass
    raise DelegationDeferred("evidence_unavailable")


_TEST_COMMAND = re.compile(
    r"(?:(?:(?:python3? -m )?pytest(?: -(?:q|v))?)|"
    r"(?:python3? -m unittest(?: discover)?)|"
    r"(?:(?:npm|pnpm|yarn) test)|(?:node --test)|(?:cargo test)|(?:go test \./\.\.\.))"
)
_TEST_MENTION = re.compile(r"\b(?:pytest|unittest|(?:npm|pnpm|yarn)(?: run)? test|node --test|"
                           r"cargo test|go test|make (?:test|check)|tox|jest|vitest|mvn test|gradle test)\b")
_TEST_HEADINGS = {"test", "tests", "testing", "run tests", "running tests", "test command",
                  "테스트", "테스트 실행", "테스트 명령"}
_NEUTRAL_HEADINGS = _TEST_HEADINGS | {
    "project", "readme", "development", "developer guide", "development guide", "contributing",
    "contribution guide", "getting started", "프로젝트", "개발", "개발 가이드", "기여 가이드", "시작하기",
}
_TEST_INSTRUCTIONS = {
    "run the tests:", "run the test suite:", "run the test suite with:",
    "run the tests with the following command:", "to run the tests:",
    "다음 명령으로 테스트합니다.", "다음 명령으로 테스트를 실행합니다.", "테스트 실행 명령:",
}


def _title_key(name: str) -> str:
    return re.sub(r"[ ._-]+", " ", name.casefold()).strip()


def _test_fact(document: str, project_names) -> tuple[str, list[dict]]:
    answer = None
    citations = []
    eligible = True  # A file containing just the command needs no heading.
    has_other_text = has_command = False
    headings = {}
    title_keys = {_title_key(name) for name in project_names} - {""}
    first_content_line = None
    fence = None
    in_comment = False
    for number, raw in enumerate(document.split("\n"), 1):
        line = raw.removesuffix("\r")
        command = line.strip(" \t")
        if not command:
            continue
        if first_content_line is None:
            first_content_line = number
        if not fence and (in_comment or "<!--" in line):
            if "<!--" in line or "-->" in line:
                in_comment = line.rfind("<!--") > line.rfind("-->")
            if has_command or _TEST_MENTION.search(line):
                raise DelegationDeferred("evidence_ambiguous")
            has_other_text = True
            continue
        heading = re.fullmatch(r"[ ]{0,3}(#{1,6})[ \t]+(.+?)\s*", line) if not fence else None
        project_title = (heading is not None and number == first_content_line and heading[1] == "#"
                         and _title_key(heading[2]) in title_keys)
        if heading and (project_title or not _TEST_MENTION.search(line)):
            depth = len(heading[1])
            title = heading[2].casefold()
            headings = {level: parent for level, parent in headings.items() if level < depth}
            eligible = title in _TEST_HEADINGS and all(headings.values())
            headings[depth] = project_title or title in _NEUTRAL_HEADINGS
            has_other_text = has_command = False
            continue
        marker = re.fullmatch(r"(`{3,}|~{3,})(.*)", command)
        if marker and not fence:
            fence = marker[1]
            if marker[2].strip().casefold() not in {"", "sh", "shell", "bash", "powershell", "pwsh", "cmd", "console"}:
                has_other_text = True
        elif fence and command == fence:
            fence = None
        elif _TEST_MENTION.search(line):
            if (not eligible or not _TEST_COMMAND.fullmatch(command) or len(line) > 240
                    or (answer is not None and answer != command)):
                raise DelegationDeferred("evidence_ambiguous")
            has_command = True
            answer = command
            if not citations:
                citations.append({"line": number, "quote": line})
        elif fence or command.casefold() not in _TEST_INSTRUCTIONS:
            has_other_text = True
        # A negative, conditional or historical example must not become a fact
        # merely because its shell invocation has the right spelling.
        if has_command and has_other_text:
            raise DelegationDeferred("evidence_ambiguous")
    if answer is None:
        raise DelegationDeferred("evidence_unavailable")
    if fence or in_comment:
        raise DelegationDeferred("evidence_ambiguous")
    return answer, citations


def derive_fact(topic: str, document: str, *, project_names=()) -> tuple[str, list[dict]]:
    if topic == "test_command":
        return _test_fact(document, project_names)
    return _version_fact(topic, document)
