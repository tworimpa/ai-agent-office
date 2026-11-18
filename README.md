# AI Agent Office

Local-first orchestration of coding agent CLIs with a browser-based local studio, persistent task history and a separate read-only observer.

The local studio adds browser-based workspace registration, run controls and results on top of the CLI workflow. Remote runners, automatic human-in-the-loop Q&A, unattended delegation and self-updating are planned features.

## 로컬 작업실 시작

아래 명령을 설치된 환경에서 실행한 뒤 터미널에 표시되는 주소를 브라우저에서 여세요. Windows와 Linux에서 같은 명령을 사용합니다.

```shell
agent-office --db ../office-data/tasks.sqlite3 studio
```

1. **작업공간 등록**에서 이름과 폴더의 절대 경로를 한 줄에 하나씩 입력합니다.
2. 주 폴더와 함께 쓸 폴더를 선택합니다. 한 번의 실행에서는 같은 Git 체크아웃의 현재 커밋에 포함된 폴더들을 선택할 수 있습니다. 이 목록은 작업자에게 전달하는 작업 범위입니다. CLI와 테스트는 별도 worktree의 저장소 루트에서 실행되며 실제 파일 접근 권한은 CLI와 OS 설정에 따릅니다.
3. 요청을 입력하고 **작업 시작**을 누릅니다. 기본값은 계획 작성입니다. 파일 수정과 테스트·리뷰까지 맡길 때에는 **개발·검증 맡기기**를 선택합니다.
4. 실행 상세에서 진행 상태를 확인하고 **실행 중지**를 요청할 수 있습니다. 작업실 명령이 실행 중이면 브라우저를 닫아도 작업이 계속됩니다. 같은 주소로 돌아오면 변경 파일, diff, 테스트 결과와 리뷰 의견을 확인할 수 있습니다.

기본 주소는 `http://127.0.0.1:8766/`입니다. `studio --port 0`을 사용하면 빈 포트를 고릅니다. 실제 작업은 선택한 PC에 설치한 Codex와 Antigravity 계정을 사용하므로 신뢰하는 저장소에서 시작하세요. [제품 구상](docs/product-vision.md) · [구현 계획](docs/superpowers/plans/2026-11-19-local-studio.md)

## Architecture

```text
User task -> Agent Office CLI
            -> Git worktree (isolated working directory)
            -> LangGraph
               -> Developer CLI (Codex / Antigravity; optional Claude)
               -> Local test command (allow-edits mode only)
               -> Reviewer CLI (read-only)
               -> [rework on failure, with a maximum attempt limit]
            -> Report for human review
```

The orchestrator does not replace the internal agentic loops inside Claude Code or Codex. It treats each CLI as a worker.

## Requirements

- Python 3.11+ and Git installed locally.
- Codex and Antigravity CLI (`agy`) installed/authenticated for real runs with the initial engine profile. Legacy Claude remains available through explicit role overrides.
- Windows or Linux, with a local Git repository containing an initial commit.
- The CLI you select must be on `PATH`.

## Install

```bash
git clone https://github.com/tworimpa/ai-agent-office.git
cd ai-agent-office
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## Cross-platform verification (PR #1)

The CI workflow runs **Ubuntu + Windows**, Python **3.11 + 3.13**, without provider credentials. It verifies dependencies, unit tests, Git worktrees and native subprocess adapter communication.

- Windows: `powershell -ExecutionPolicy Bypass -File .\\scripts\\verify-windows.ps1`
- Linux: `bash scripts/verify-linux.sh`
- To include authenticated headless CLI checks: add `-Live` (Windows) or `--live` (Linux).
- Inspect available CLIs: `agent-office doctor`

Task and event queries, explicit interruption marking and candidate DB paths are described in [storage guide](docs/storage.md). Use `--db` before the subcommand to keep development state separate from stable state. The studio controls its own runs. Provider conversation resume is future work.

For read-only access to the journal, use `agent-office --db PATH serve --port 8765`. See the [observer guide](docs/observer.md) for selection, timeline updates and actual CLI acceptance limits.

See [verification guide](docs/verification.md). Real Codex/Antigravity checks require those CLIs installed and authenticated on the target PC; CI mocks do not validate live provider behavior.

## Try it

Default is **planning-only**, with edit permissions disabled.

```bash
agent-office run --repo . --task "Suggest the next milestone for Agent Office"
```

Opt in to edit the isolated Git worktree and run tests:

```bash
agent-office run --repo . --allow-edits \
  --task "Add one focused regression test and explain what changed"
```

By default development uses `codex` and review uses `antigravity` (`agy`). Roles remain independent: override with `--developer antigravity --reviewer codex`, or explicitly select legacy `claude`. Product workers do not merge/push/deploy their work. Codex's repository maintenance follows the owner's separate CI-gated workflow in AGENTS.md.

Use `agent-office engines` for fixed metadata-only diagnostics. The observer shows a startup snapshot for the two initial engines. Installation, authentication, advertised headless flags and actual task acceptance are distinct; metadata never marks a live task verified. See [engine guide](docs/engines.md).

**Important:** CLI sandbox modes are provider-specific; Claude's `acceptEdits` permission mode is **not a full OS/filesystem sandbox**. Prompts are not a security boundary. Only run trusted tasks on an appropriately isolated local account/environment, and do not expose sensitive credentials or production repositories. The default test command is `python -m pytest -q` (using the Agent Office Python interpreter). Changes to files outside the Git worktree can still be possible depending on CLI permissions and installed tools.

## Security / platform note

Tasks are transferred to CLI subprocesses through standard input rather than shell-interpolated command arguments. This helps with long prompts and Windows npm `.cmd` wrappers, but cannot eliminate every risk from third-party tools. On Windows, validate Codex's effective native sandbox before using `--allow-edits`. The worktree is not an operating-system sandbox.

## Roadmap

제품의 목표, 사용자 흐름과 단계별 완료 기준은 [AI Agent Office 제품 구상](docs/product-vision.md)에 정리했습니다.

- AO-000: LangGraph + Claude/Codex adapters + separate worktree + tests + human-reviewed result.
- AO-001: persistent task/run/event database, read-only queries and explicit recovery marking.
- AO-002: office observation UI, Gemini/Cline/Antigravity adapters and skills registry.
- AO-003: questions, approval inbox, session continuation, and delegated decisions within policy.
- AO-004: supervised self-improvement, regression benchmarks, candidate builds and safe promotion.

## Project rules

- Codex maintenance follows the owner's standing PR → latest-head CI → merge authorization in [AGENTS.md](AGENTS.md). Product workers still do not publish/merge their own work; deployment and security changes require separate authorization.
- Isolate development to a worktree; never edit the running Agent Office installation in place.
- Store per-task evidence including test results, git diffs and decision trail before promotion.
- Keep agent roles separate from the selected CLI engines.
- Fail closed on unrecognized reviewer verdicts or provider errors.

See [docs/architecture.md](docs/architecture.md) for the planned system and AO-000 scope.
