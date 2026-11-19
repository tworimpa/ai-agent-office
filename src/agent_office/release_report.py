"""Standalone, passive review report for an immutable source candidate."""
from html import escape
import json


def _text(value):
    # Preserve line layout while making embedded control and direction overrides visible.
    return "".join(c if c.isprintable() or c in "\n\t" else ascii(c)[1:-1] for c in str(value))


def _html(value):
    return escape(_text(value))


def terminal_review(record):
    proposal = record["proposal"]
    evidence = json.dumps(proposal["history"], ensure_ascii=False, indent=2)
    history = json.dumps(record["history"], ensure_ascii=False, indent=2)
    sections = (("개선안", proposal["title"]), ("이유", proposal["rationale"]),
                ("기대 효과", proposal["expected"]), ("인용한 실행", evidence),
                ("기준 커밋", record["base"]), ("현재 안정 커밋", record["stable"]),
                ("후보 커밋", record["sha"]), ("검사 이미지", record["image"]),
                ("전체 diff", record["diff"]), ("검증 및 결정 이력", history))
    return "\n\n".join(f"{label}\n{_text(value)}" for label, value in sections)


def render_review(record):
    p = record["proposal"]
    validation = record["validation"]
    states = {"submitted": "검증 대기", "passed": "검증 통과 · 승인 대기", "failed": "검증 실패",
              "verifying": "검증 중", "approved": "승격 승인", "rejected": "거절"}
    state = states.get(record["state"], record["state"])
    evidence = []
    for run in p["history"]:
        seq = run["citation"]
        event = next(e for e in run["events"] if e["seq"] == seq)
        evidence.append(f'<li><p>{_html(run["request"])}</p><code>{_html(run["run_id"])}</code>'
                        f' · {_html(run["state"])} · {_html(run["outcome"])}'
                        f'<pre>{_html(json.dumps(event, ensure_ascii=False, indent=2))}</pre></li>')
    check = (f'<p>검사 이미지 <code>{_html(record["image"])}</code></p>'
             '<p>완료된 검증 기록이 없습니다.</p>')
    if validation:
        verdict = "통과" if validation["passed"] else "실패"
        check = (f'<p><strong>{verdict}</strong> · 종료 코드 {_html(validation["exit_code"])}</p>'
                 f'<p>검사 이미지 <code>{_html(validation["image"])}</code></p>'
                 f'<p>검증한 후보 <code>{_html(validation["sha"])}</code></p>'
                 f'<pre>{_html(validation["output"])}</pre>')
    history = json.dumps(record["history"], ensure_ascii=False, indent=2)
    stale = ""
    if record.get("stale_base", record["base"] != record["stable"]) and record["state"] not in ("approved", "rejected"):
        state = "기준 버전 변경 · 재접수 필요"
        stale = "<p>후보를 접수한 뒤 안정 버전이 바뀌었습니다. 현재 순번에서 다시 접수하고 검증하세요.</p>"
    return f"""<!doctype html>
<html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{_html(p["title"])} · 개선안 검토</title>
<style>
:root {{color-scheme:light dark;font:100%/1.6 system-ui,sans-serif}}
main {{max-width:68rem;margin:auto;padding:1rem;overflow-wrap:anywhere}}
h2 {{border-bottom:1px solid GrayText;margin-top:2rem}} h1 {{line-height:1.25}}
pre {{background:Canvas;color:CanvasText;border:1px solid GrayText;border-radius:.4rem;
padding:.8rem;white-space:pre-wrap;tab-size:4}} code,pre {{font-size:.94em}}
summary {{cursor:pointer;font-weight:600}} li {{margin-block:.6rem}}
</style><main>
<header><p>AI Agent Office · 개선안 검토</p><h1>{_html(p["title"])}</h1>
<p><strong>{_html(state)}</strong> · 제안 엔진 {_html(p["engine"])}</p>{stale}</header>
<dl><dt>개선안 ID</dt><dd><code>{_html(record["id"])}</code></dd>
<dt>기준 커밋</dt><dd><code>{_html(record["base"])}</code></dd>
<dt>검토할 후보 커밋</dt><dd><code>{_html(record["sha"])}</code></dd></dl>
<h2>제안 이유</h2><p>{_html(p["rationale"])}</p>
<h2>기대 효과</h2><p>{_html(p["expected"])}</p>
<h2>근거가 된 실행</h2><ul>{''.join(evidence)}</ul>
<h2>회귀 검증</h2>{check}
<h2>변경 내용</h2><details open><summary>전체 diff</summary><pre>{_html(record["diff"])}</pre></details>
<h2>검증 및 결정 이력</h2><pre>{_html(history)}</pre></main></html>
"""
