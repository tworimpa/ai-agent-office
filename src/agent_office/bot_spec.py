"""Bot Definition 1.0 interchange format. All package paths are virtual."""

import base64
import json
import math
import re
import unicodedata


VERSION = "1.0"
SCHEMA_ID = "https://raw.githubusercontent.com/tworimpa/ai-agent-office/main/src/agent_office/schemas/bot-1.0.schema.json"
APPEARANCE = "io.github.tworimpa.agentoffice.appearance"
MAX_DOCUMENT_BYTES = 512 * 1024
_IMAGE_PREFIX = "data:image/webp;base64,"
_NAMESPACE = re.compile(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+")
_DEVICES = re.compile(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³]", re.IGNORECASE)
_REQUIRED = {"specVersion", "id", "name", "role", "files"}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 객체의 키가 중복되었습니다.")
        result[key] = value
    return result


def loads(data: bytes):
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("봇 정의는 UTF-8 512 KiB 이내로 저장하세요.")
    try:
        return validate(json.loads(data.decode("utf-8"), object_pairs_hook=unique_object))
    except RecursionError:
        raise ValueError("봇 정의의 JSON 중첩이 너무 깊습니다.") from None


def valid_id(value):
    return isinstance(value, str) and len(value) <= 64 and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) is not None


def check_path(path):
    if not isinstance(path, str) or len(path.encode("utf-8")) > 240 or unicodedata.normalize("NFC", path) != path:
        raise ValueError("파일 경로는 NFC 형식의 UTF-8 240바이트 이내로 입력하세요.")
    parts = path.split("/")
    if len(parts) > 8:
        raise ValueError("파일 경로는 8단계 이내로 입력하세요.")
    for part in parts:
        stem = part.split(".", 1)[0].rstrip(" ")
        if (not part or part.endswith((".", " ")) or part.casefold() == ".git"
                or _DEVICES.fullmatch(stem) or len(part.encode("utf-8")) > 128
                or any(c in '<>:"\\|?*' or unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for c in part)):
            raise ValueError("Windows와 Linux에서 사용할 수 있는 상대 파일 경로를 입력하세요.")
    return parts


def folders_for(files, folders):
    """Return all directories, including implicit parents, and detect path aliases."""
    entries = {}
    for path, is_dir in [(p, False) for p in files] + [(p, True) for p in folders]:
        parts = check_path(path)
        for end in range(1, len(parts) + 1):
            prefix = "/".join(parts[:end])
            key = prefix.casefold()
            entry = (prefix, end < len(parts) or is_dir)
            if key in entries and entries[key] != entry:
                raise ValueError("대소문자가 다르거나 파일과 폴더가 겹치는 경로입니다.")
            entries[key] = entry
    if entries.get("memory.md", ("MEMORY.md", False)) != ("MEMORY.md", False):
        raise ValueError("루트 메모 파일의 이름은 MEMORY.md입니다.")
    return sorted(path for path, is_dir in entries.values() if is_dir)


def _content(text):
    if (not isinstance(text, str) or len(text.encode("utf-8")) > 8192
            or any(unicodedata.category(c) in {"Cc", "Cs"} and c not in "\r\n\t" for c in text)):
        raise ValueError("각 파일은 UTF-8 8 KiB 이내의 텍스트여야 합니다.")


def _image_size(blob):
    """Read static VP8/VP8L dimensions; reject animation, metadata and trailing bytes."""
    def uint(data):
        return int.from_bytes(data, "little")

    if blob[:4] != b"RIFF" or blob[8:12] != b"WEBP" or uint(blob[4:8]) != len(blob) - 8:
        return None
    chunks = {}
    offset = 12
    while offset < len(blob):
        tag = blob[offset:offset + 4]
        size = uint(blob[offset + 4:offset + 8])
        end = offset + 8 + size
        if (tag not in (b"VP8 ", b"VP8L", b"VP8X", b"ALPH") or tag in chunks or not size
                or end > len(blob) or size % 2 and blob[end:end + 1] != b"\0"):
            return None
        chunks[tag] = blob[offset + 8:end]
        offset = end + size % 2
    tags = list(chunks)
    if tags not in ([b"VP8 "], [b"VP8L"], [b"VP8X", b"VP8L"], [b"VP8X", b"ALPH", b"VP8 "]):
        return None
    if b"VP8L" in chunks:
        data = chunks[b"VP8L"]
        if len(data) <= 5 or data[0] != 0x2F or data[4] >> 5:
            return None
        bits = uint(data[1:5])
        size = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    else:
        data = chunks[b"VP8 "]
        if len(data) <= 10 or data[0] & 1 or data[3:6] != b"\x9d\x01\x2a":
            return None
        size = (uint(data[6:8]) & 0x3FFF, uint(data[8:10]) & 0x3FFF)
    if b"VP8X" in chunks:
        header = chunks[b"VP8X"]
        if (len(header) != 10 or header[:4] != b"\x10\0\0\0"
                or (1 + uint(header[4:7]), 1 + uint(header[7:10])) != size):
            return None
    return size


def _appearance(value):
    if (not isinstance(value, dict) or set(value) - {"image", "preset"}
            or type(value.get("preset")) is not int or not 0 <= value["preset"] <= 11):
        raise ValueError("외형의 preset 번호는 0부터 11까지입니다.")
    if "image" in value:
        image = value["image"]
        if not isinstance(image, str) or len(image) > len(_IMAGE_PREFIX) + 131072 or not image.startswith(_IMAGE_PREFIX):
            raise ValueError("외형 이미지에는 96 KiB 이내의 WebP 데이터 URI를 사용하세요.")
        encoded = image[len(_IMAGE_PREFIX):]
        blob = base64.b64decode(encoded, validate=True)
        size = _image_size(blob)
        if base64.b64encode(blob).decode("ascii") != encoded or not size or not all(1 <= side <= 512 for side in size):
            raise ValueError("외형 이미지의 WebP 형식과 최대 크기(512×512)를 확인하세요.")


def _json_value(value, depth=0):
    if depth > 16:
        raise ValueError("확장 JSON 중첩은 16단계까지 허용합니다.")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("확장 객체의 키는 문자열이어야 합니다.")
            _json_value(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _json_value(child, depth + 1)
    elif value is not None and type(value) not in (str, int, bool) and not (type(value) is float and math.isfinite(value)):
        raise ValueError("확장 데이터에는 JSON 값만 사용할 수 있습니다.")


def validate(document):
    """Validate wire and semantic rules, supply optional defaults and sort explicit folders."""
    if (not isinstance(document, dict) or not _REQUIRED.issubset(document)
            or set(document) - _REQUIRED - {"$schema", "folders", "extensions"}):
        raise ValueError("봇 정의의 필수 필드와 추가 필드를 확인하세요.")
    if document["specVersion"] != VERSION or document.get("$schema", SCHEMA_ID) != SCHEMA_ID:
        raise ValueError("지원하는 봇 규격 버전은 1.0입니다.")
    if not valid_id(document["id"]):
        raise ValueError("봇 ID는 64자 이내의 영문 소문자, 숫자와 단어 사이 하이픈을 사용하세요.")
    for key, limit in (("name", 40), ("role", 60)):
        label = document[key]
        if (not isinstance(label, str) or not label or label.strip() != label or len(label) > limit
                or any(unicodedata.category(c) in {"Cc", "Cs", "Zl", "Zp"} for c in label)):
            raise ValueError(f"{key} 필드는 {limit}자 이내 한 줄로 입력하세요.")
    files = document["files"]
    if not isinstance(files, dict) or len(files) > 32 or "SOUL.md" not in files:
        raise ValueError("SOUL.md를 포함해 최대 32개 파일을 지정하세요.")
    for content in files.values():
        _content(content)
    if sum(len(v.encode("utf-8")) for k, v in files.items() if k != "SOUL.md") > 24576:
        raise ValueError("SOUL.md 외 파일의 합계는 UTF-8 24 KiB 이내입니다.")
    folders = document.get("folders", [])
    if (not isinstance(folders, list) or len(folders) > 32 or not all(isinstance(p, str) for p in folders)
            or len(set(folders)) != len(folders)):
        raise ValueError("명시적 폴더는 중복 없이 최대 32개까지 지정하세요.")
    folders_for(files, folders)
    extensions = document.get("extensions", {})
    if (not isinstance(extensions, dict) or len(extensions) > 16 or any(
            not isinstance(k, str) or len(k) > 128 or not _NAMESPACE.fullmatch(k) for k in extensions)):
        raise ValueError("최대 16개 확장을 점으로 구분한 역도메인 이름으로 지정하세요.")
    _json_value(extensions)
    if len(json.dumps(extensions, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 160 * 1024:
        raise ValueError("확장 데이터 합계는 UTF-8 160 KiB 이내입니다.")
    if APPEARANCE in extensions:
        _appearance(extensions[APPEARANCE])
    return {"$schema": SCHEMA_ID, "specVersion": VERSION, "id": document["id"], "name": document["name"],
            "role": document["role"], "files": dict(files), "folders": sorted(folders), "extensions": extensions}
