"""Independent, read-only consultations for exact preauthorized technical facts."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

from .away_evidence import DelegationDeferred, derive_fact, git, read_document, require_base, unique_object
from .engines import SUPPORTED_ENGINES
from .fixture_process import execute_fixture


RULES = {
    "test_command": "이 프로젝트의 테스트 명령은 무엇인가요?",
    "python_version": "이 프로젝트가 요구하는 Python 버전은 무엇인가요?",
    "node_version": "이 프로젝트가 요구하는 Node.js 버전은 무엇인가요?",
}
EVIDENCE_FILES = {
    "test_command": ("README.md", "AGENTS.md", "TESTING.md"),
    "python_version": ("pyproject.toml",),
    "node_version": ("package.json",),
}


def validate_rule(value) -> dict:
    if (not isinstance(value, dict) or set(value) - {"topic", "path", "verification"}
            or not {"topic", "path"} <= value.keys() or value.get("verification", "fact") not in ("fact", "independent")):
        raise ValueError("위임 규칙에는 질문 종류와 근거 파일을 지정하세요.")
    topic, path = value["topic"], value["path"]
    if not isinstance(topic, str) or topic not in RULES:
        raise ValueError("지원되는 기술 질문을 선택하세요.")
    if not isinstance(path, str) or len(path) > 240:
        raise ValueError("근거 파일은 저장소 안의 문서여야 합니다.")
    parts = path.split("/")
    if (parts[-1] not in EVIDENCE_FILES[topic]
            or any(not re.fullmatch(r"[^\W_][\w. -]{0,62}", part) or part.endswith((".", " "))
                   or re.fullmatch(r"(?i:con|prn|aux|nul|(?:com|lpt)[1-9])(?:\..*)?", part)
                   for part in parts[:-1])
            or any(word in path.casefold() for word in ("secret", "credential", "password", "private_key", "token"))):
        raise ValueError("질문에 맞는 저장소 상대 근거 파일을 지정하세요.")
    rule = {"topic": topic, "path": path}
    if value.get("verification") == "independent":
        rule["verification"] = "independent"
    return rule


def validate_policy(value) -> dict:
    if not isinstance(value, dict) or set(value) != {"rules", "specialist_engine", "verifier_engine"}:
        raise ValueError("위임 규칙과 두 역할의 엔진을 지정하세요.")
    specialist, verifier = value["specialist_engine"], value["verifier_engine"]
    if specialist not in SUPPORTED_ENGINES or verifier not in SUPPORTED_ENGINES:
        raise ValueError("지원되는 전문가·검증자 엔진을 선택하세요.")
    if not isinstance(value["rules"], list) or not 1 <= len(value["rules"]) <= len(RULES):
        raise ValueError("위임할 기술 질문을 선택하세요.")
    rules = [validate_rule(rule) for rule in value["rules"]]
    if len({rule["topic"] for rule in rules}) != len(rules):
        raise ValueError("한 질문에는 한 개의 근거 파일만 지정하세요.")
    return {"rules": sorted(rules, key=lambda rule: rule["topic"]),
            "specialist_engine": specialist, "verifier_engine": verifier}


def in_scope(path: str, folders: list[str]) -> bool:
    relative = PurePosixPath(path)
    return (path == "docs/TESTING.md" or any(path in names for names in EVIDENCE_FILES.values())
            or any(relative.is_relative_to(folder) for folder in folders))


def consult_role(role, engine, material, instructions, validate, guidance, scratch_root, on_stage, db_path=None):
    failure = role + "_failed"
    payload = json.dumps(material, ensure_ascii=False)
    prompt = (f"AWAY_{role.upper()}\n{guidance}Read evidence.json in this directory. "
              f"{instructions} If uncertain return {{}}. Do not run commands or change files.\n" + payload)
    entered = False
    try:
        with tempfile.TemporaryDirectory(prefix=f"office-away-{role}-", dir=scratch_root) as directory:
            scratch = Path(directory)
            git(scratch, "init", "--quiet", "--template=")
            (scratch / "evidence.json").write_text(payload, encoding="utf-8")
            if on_stage:
                on_stage(role, "RUNNING")
                entered = True
            result = execute_fixture(engine, prompt, workspace=scratch, allow_edits=False, timeout=45,
                                     **({"db_path": db_path} if db_path is not None and engine == "cline" else {}))
        if result.returncode or result.truncated or len(result.message) > 2048:
            raise ValueError
        citation = validate(json.loads(result.message, object_pairs_hook=unique_object))
    except (OSError, ValueError, TypeError, RecursionError):
        if entered:
            on_stage(role, "FAILED")
        raise DelegationDeferred(failure) from None
    if entered:
        on_stage(role, "COMPLETED")
    return citation


def _consult(role, engine, material, answer, guidance, scratch_root, on_stage, db_path=None):
    verifying = role == "verifier"
    instructions = ("Independently answer the technical question using only its numbered evidence. "
                    "Return just one JSON object with answer, line and quote"
                    + (', and verdict ("verified" only when the evidence supports your answer)' if verifying else "")
                    + ". The answer is the exact command or version constraint alone. Copy line and quote exactly.")

    def validate(response):
        keys = {"answer", "line", "quote", "verdict"} if verifying else {"answer", "line", "quote"}
        if (not isinstance(response, dict) or set(response) != keys
                or response["answer"] != answer or type(response["line"]) is not int
                or (verifying and response["verdict"] != "verified")):
            raise ValueError
        citation = {"line": response["line"], "quote": response["quote"]}
        if citation not in material["evidence"]:
            raise ValueError
        return citation

    return consult_role(role, engine, material, instructions, validate, guidance, scratch_root, on_stage, db_path)


def document_fact(repo: Path, topic: str, path: str, document: str,
                  folders: list[str] | None) -> tuple[str, list[dict]]:
    project_names = []
    if topic == "test_command":
        # A run's linked worktree has a generated name. The ordinary checkout
        # containing the common .git directory supplies the project title.
        common_dir = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        common = Path(os.fsdecode(common_dir.rstrip(b"\r\n")))
        if common.name == ".git":
            project_names.append(common.parent.name)
        directory = PurePosixPath(path).parent
        if folders and directory.name and str(directory) in folders:
            project_names.append(directory.name)
    return derive_fact(topic, document, project_names=project_names)


def assess(repo: Path, revision: str, rule: dict, specialist_engine: str, verifier_engine: str,
           *, folders: list[str] | None = None, skill_guidance=None, scratch_root: Path | None = None,
           on_stage=None, db_path=None) -> dict:
    try:
        rule = validate_policy({"rules": [rule], "specialist_engine": specialist_engine,
                                "verifier_engine": verifier_engine})["rules"][0]
    except ValueError:
        raise DelegationDeferred("out_of_scope") from None
    topic, path = rule["topic"], rule["path"]
    if folders is not None and not in_scope(path, folders):
        raise DelegationDeferred("out_of_scope")
    document = read_document(repo, revision, path)
    answer, citations = document_fact(repo, topic, path, document, folders)
    if rule.get("verification") != "independent":
        require_base(repo, revision)
        return {"answer": answer, "evidence": {"path": path, **citations[0], "revision": revision,
                                                "method": "committed_fact"}}
    material = {"topic": topic, "question": RULES[topic], "path": path, "revision": revision, "evidence": citations}
    guidance = skill_guidance or {}
    proposal = _consult("specialist", specialist_engine, material, answer, guidance.get("specialist", ""),
                        scratch_root, on_stage, db_path)
    require_base(repo, revision)
    verified = _consult("verifier", verifier_engine, material, answer, guidance.get("verifier", ""),
                        scratch_root, on_stage, db_path)
    if proposal != verified:
        raise DelegationDeferred("verifier_failed")
    require_base(repo, revision)
    return {"answer": answer, "evidence": {"path": path, **verified, "revision": revision}}
