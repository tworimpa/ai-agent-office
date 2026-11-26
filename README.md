# AI Agent Office

Local-first orchestration of coding agent CLIs with a browser-based local studio, persistent task history and a separate read-only observer.

The selected local or SSH-connected host owns its folders, processes, requests and decision journal. The collaboration inbox supports operator decisions and opt-in, evidence-checked answers to selected technical questions.

## 설치형 앱

**[GitHub Releases에서 설치판 다운로드](https://github.com/tworimpa/ai-agent-office/releases)** · Windows x64는 `Setup.exe`, Ubuntu amd64는 `.deb`를 선택하세요. [파일 확인과 업그레이드 방법](docs/desktop-release.md)

조정자에게 일을 맡기면 작업 방식과 역할별 배정이 실행에 저장됩니다. 사용한 모델·effort, 토큰과 계정 한도, 지난 방문 이후의 결정과 캐릭터별 근거를 같은 작업에서 확인할 수 있습니다. [작업 방식·한도·육각형 사용법](docs/work-profiles.md)

기존 Studio에 연결하는 **AI Agent Office (Rust 비교판)**도 Windows와 Ubuntu x64에 별도로 설치할 수 있습니다. 같은 Workspace와 실행을 두 앱에서 이어 보는 방법은 [Rust 비교판 사용 및 검증 안내](docs/rust-comparison.md)에 있습니다.

Windows와 Ubuntu·Debian 계열 Linux에서 **AI Agent Office** 앱을 열어 실행 중인 Studio에 연결하거나 새 로컬 작업실을 시작할 수 있습니다. 앱 창을 닫고 다시 열어도 실행 중인 작업은 같은 화면에서 확인할 수 있습니다. [빌드·설치·제거 안내](docs/desktop.md)

## 조정자에게 작업 맡기기

조정자·개발자·리뷰어에 내 봇을 배정할 수 있습니다. 예를 들어 Windows·Linux 호환성 리뷰어를 만들고, 해당 Workspace의 다음 의뢰부터 경로 처리 회귀를 확인하도록 지정할 수 있습니다. [로컬 역할 봇 만들기와 배정](docs/local-role-bots.md)

사무실의 **조정자 브리핑**에서 응답을 기다리는 질문과 영향을 받는 목표, 검토할 후보, 다음 작업을 확인할 수 있습니다. 제품 결정 하나를 여러 목표가 참조할 때 원래 질문에 한 번 답하면 됩니다. 권한 요청에는 각 실행에서 따로 응답합니다. [Workspace 브리핑과 단계별 선행 조건](docs/workspace-briefing.md)

GitHub Issue에서 작업을 맡기려면 실행 호스트에 GitHub 연결을 등록한 뒤 **작업실 설정 → GitHub**에서 저장소·브랜치·폴더와 허용할 사용자를 선택하세요. `office:ready` 라벨을 붙인 열린 Issue가 목표로 연결됩니다. 작업실에서 **지금 가져오기**로 확인하거나 Studio가 다음 동기화 때 처리하도록 둘 수 있습니다. 결과는 같은 Issue의 상태 댓글에서 확인합니다. [GitHub App·Enterprise 연결](docs/github-connections.md) · [이벤트 처리와 게시 복구](docs/github-issue-sync.md)

반복 수정과 장시간 회귀 검증이 필요한 작업은 조정자 대화의 **장시간 개발 목표** 옵션으로 맡길 수 있습니다. 작업실 설정에서 검증 단계와 예산을 저장하면 다음 파일 수정 의뢰의 기본값으로 사용합니다. 같은 목표에서 최초 실패, 시도별 테스트 변화, 후보 간 변경, 미검증 기준과 채택 기록을 확인합니다. [장시간 목표 설정과 복구](docs/overnight-goals.md)

초상화를 누르면 기본 **간단히** 보기에서 요청, 담당자의 결론, 현재 상태와 응답할 항목을 확인할 수 있습니다. **변경 보기**, **테스트 보기**, **회의 보기**를 누르면 해당 실행의 근거를 열고 대화로 돌아올 수 있습니다. **협업 과정**은 계획과 의견을, **기술 상세**는 Markdown 답변, 파일별 diff와 원문을 표시합니다. 기획 회의에서 **작업 흐름 보기**를 누르면 선택한 계획 버전의 담당자와 선행 관계가 오프라인 도식으로 열립니다.

Workspace에서 조정자 초상화를 누르고 원하는 결과를 입력하세요. **빠른 확인**과 기본 읽기 전용 의뢰는 담당자에게 바로 전달됩니다. **계획을 보고 시작**을 선택하면 배정된 조정자 봇이 원 요청과 기준 커밋을 읽고 담당 역할·작업 순서·완료 기준을 제안합니다. 계획을 확인하거나 수정해 승인하면 담당자가 작업을 시작합니다. 수정 작업에는 **파일 수정 허용**을 함께 선택하세요. 조정자 계획에는 별도 CLI 사용량이 발생합니다.

배정이 끝나면 **담당자와 대화**를 눌러 방금 맡긴 작업을 따라가세요. 여기서 보낸 추가 지시와 답변은 같은 실행에 기록됩니다. 별개의 일을 의뢰할 때는 **새 작업 맡기기**를 선택하세요. 사무실로 돌아오면 작업공간 상단에서 선택한 작업의 현재 단계와 응답할 항목을 확인할 수 있습니다.

제품 선택이 남으면 사무실에서 한 번에 답할 수 있습니다. 먼저 계획을 확인하려면 **계획을 보고 시작**을 선택하세요. 담당자 초상화로 추가 지시를 보내면 같은 실행에 변경 이유, 계획 버전과 검증할 항목이 남습니다. 서로 다른 폴더에 배정된 독립적인 개발 작업은 호스트에 설정한 동시 슬롯 수(기본 2개) 안에서 병렬로 진행하고, 결과를 원래 실행의 작업 트리에 모아 테스트·리뷰합니다.

작업 기록에서 담당자의 의견, 계획, 테스트 방법과 실제 결과를 확인하세요. 자동 검증은 지정된 pytest 파일의 실행 보고서를 기준으로 표시하고, 화면 확인 등 별도 근거가 필요한 항목은 미검증으로 남습니다. 같은 실행을 다시 열면 계획 결정과 변경 이력도 복원됩니다.

## 로컬 작업실 시작

아래 명령을 설치된 환경에서 실행한 뒤 터미널에 표시되는 주소를 브라우저에서 여세요. Windows와 Linux에서 같은 명령을 사용합니다.

```shell
agent-office --db ../office-data/tasks.sqlite3 studio
```

1. **작업공간 등록**에서 이름과 폴더의 절대 경로를 한 줄에 하나씩 입력합니다.
2. Workspace에 들어가 **조정자** 초상화를 누르고 하고 싶은 일을 적습니다. 파일 수정이 필요한 작업은 **파일 수정 허용**을 선택합니다. 작업 옵션에서 범위와 기술 판단 위임 여부를 바꿀 수 있습니다. 등록된 폴더가 여러 체크아웃에 걸치면 같은 Git 체크아웃에서 작업할 폴더를 선택하세요. 실행과 테스트는 별도 worktree에서 이루어집니다.
3. 담당자 초상화 옆에서 진행 상황을 봅니다. 초상화를 눌러 질문에 답하고, 진행 중인 같은 실행에 추가 지시를 보낼 수 있습니다. 단계가 끝나면 새 지시를 받지 않으며 이전에 접수한 지시의 전달 여부는 대화에 남습니다.
4. 작업 기록에서 결과와 변경 사항, 테스트 출력, 검토 의견을 확인합니다. 실행 중인 작업은 화면에서 중지할 수 있으며, 작업실 프로세스가 살아 있으면 브라우저를 닫아도 작업이 계속됩니다.

파일 수정 작업에는 실행 호스트의 작업실 Python에 pytest가 필요합니다. 일부 테스트를 실행하려면 **작업별 옵션 → 테스트 범위**에 기준 커밋의 파일 경로를 한 줄에 하나씩 입력하세요. 예를 들어 `tests/test_cli.py`를 입력하면 해당 파일을 실행하며, 지정한 범위가 실행 기록에 남습니다. 입력을 비우면 전체 테스트를 실행합니다.

기본 주소는 `http://127.0.0.1:8766/`입니다. `studio --port 0`은 빈 포트를 고릅니다. 기본값과 담당자별 엔진·스킬은 **작업실 설정**에서 관리합니다. 기존 실행 설정, 엔진 진단, 요청함, 개선안과 안정 버전 관리는 **운영 화면**(`/studio`)에서 열 수 있습니다. [제품 구상](docs/product-vision.md) · [작업실 구현 계획](docs/superpowers/plans/2026-11-20-workspace-office.md)

기술 위임을 켜면 조정자가 기준 커밋에서 기존 구현, 진단 시작 위치, 검증에 참고할 테스트를 확인합니다. 전문가와 검증자가 같은 선택과 코드 근거를 제시한 경우에만 결정과 근거를 대화에 남깁니다. 제품 동작을 바꿀지 묻는 질문, 근거로 확정하기 어려운 질문과 권한 요청은 사용자가 처리합니다.

## 원격 호스트에서 작업하기

두 컴퓨터에 이 버전의 Agent Office를 설치합니다. 로컬에서는 Windows 기본 OpenSSH 클라이언트 또는 Linux의 `/usr/bin/ssh`를 사용합니다. 사용할 SSH 별칭(예: `build-box`)은 이미 호스트 키를 확인했고, 해당 계정에 비대화형으로 접속할 수 있어야 합니다. SSH 연결에서 원격 루프백으로의 TCP 전달도 허용되어 있어야 합니다. Git과 작업에 사용할 CLI·테스트 환경은 원격 호스트에 준비합니다.

**1. 원격 호스트**에서 작업실을 시작하고 계속 실행해 둡니다. 출력되는 포트와 32자리 `Journal ID`를 확인합니다.

```shell
agent-office --db ../remote-office/tasks.sqlite3 studio --port 8766 --accept-remote
```

**2. 로컬 컴퓨터**에서 다음 예시의 SSH 별칭과 저널 ID를 실제 값으로 바꿔 실행합니다. `--remote`는 호스트마다 한 번씩 지정할 수 있습니다. 다시 시작할 때도 같은 원격 저널을 지정하면 기존 실행 주소가 유지됩니다.

```shell
agent-office --db ../local-office/tasks.sqlite3 studio --remote build-box 8766 0123456789abcdef0123456789abcdef
```

**3. 브라우저**에서 터미널에 표시된 로컬 주소를 열고 실행 호스트로 `build-box`를 선택합니다. 처음이면 **작업공간 등록**에 원격 컴퓨터의 절대 경로를 한 줄씩 입력합니다. Workspace에 들어가 조정자에게 작업을 맡기면 worktree, 담당자 프로세스, 테스트와 대화 기록은 원격 호스트에 남습니다.

네트워크가 끊기거나 로컬 작업실을 종료해도 **원격 작업실 프로세스가 살아 있으면 실행은 계속됩니다.** 연결이 복구되면 기존 상세 주소를 열어 같은 호스트·실행 ID의 상태와 결과를 확인합니다. 작업 접수 응답이 유실됐을 때에는 같은 ID로 접수 여부를 확인할 수 있습니다. 원격 작업실 자체를 종료한 경우에는 해당 호스트의 중지·복구 규칙이 적용됩니다.

양쪽 HTTP 서버는 루프백에서 동작합니다. 로컬 작업실은 OpenSSH의 표준입출력 전달을 이용해 지정한 호스트의 작업실에만 요청합니다. 원격 포트에 다른 저널이 열려 있으면 요청이 거부됩니다. 저널 ID는 저장소 식별자이며 접속 인증은 SSH가 담당합니다. [OpenSSH 매뉴얼](https://man.openbsd.org/ssh.1) · [원격 실행 구현 계획](docs/superpowers/plans/2026-11-19-remote-studio.md)

## 역할별 엔진 선택과 호스트별 검증

실행 호스트를 고른 뒤 **개발자 엔진**과 **리뷰어 엔진**을 선택합니다. Codex, Antigravity, Claude Code, Gemini CLI, Cline CLI를 각 역할에 배정할 수 있습니다. 기본값은 개발자 Codex, 리뷰어 Antigravity입니다. 계획 작성은 개발자만 실행하고, 개발·검증에서는 선택한 개발자가 작업한 뒤 저장소 테스트와 선택한 리뷰어의 검토가 이어집니다. 실행 상세에는 접수 당시의 배정이 남습니다.

**호스트별 엔진 진단**에서 설치 여부, 버전, CLI가 안내하는 비대화형 옵션과 인증 상태를 확인합니다. 읽기·수정·리뷰 검증에는 해당 호스트에서 별도 예제로 확인한 결과와 시각을 표시합니다. 새 엔진을 설치했거나 실행 환경이 달라지면 현재 환경의 근거를 확인하세요.

Gemini와 Cline은 ACP로 연결합니다. Cline의 작업공간 파일 읽기·수정 허가는 도구 규약을 확인한 **CLI 3.0.74**에 적용합니다. 설치한 버전이 다르면 해당 호스트의 진단에 표시됩니다. [연결 방식과 지원 범위](docs/engine-protocols.md)

운영자가 실검증을 승인한 경우에만 **실행할 호스트**에서 그 작업실과 같은 DB 경로로 아래처럼 실행합니다. 이 명령은 임시 Git 저장소에서 지정한 CLI를 호출하므로 계정 사용량이 발생할 수 있습니다. `gemini` 자리에 원하는 엔진 ID를, 마지막 인자에 확인할 기능(`read`, `edit`, `review`)을 넣습니다.

```shell
agent-office --db ../remote-office/tasks.sqlite3 verify-engine gemini read
```

원격 작업실에서는 두 컴퓨터에 이 버전을 설치합니다. 원격 호스트의 진단과 검증 내역은 해당 호스트의 저널에서 가져옵니다. [엔진 가이드](docs/engines.md) · [역할별 엔진 구현 계획](docs/superpowers/plans/2026-11-19-engine-expansion.md)

## 협업 요청함

새 작업에서 **협업 요청함**을 켜고 시작합니다. 작업자가 판단을 기다리면 실행 중인 호스트의 요청함에 작업·역할·엔진과 질문 또는 권한 요청의 원문이 표시됩니다. 원격 호스트는 브라우저 상단에서 해당 SSH 별칭을 선택합니다.

| 요청 | 사용자 동작 | 작업자가 받는 내용 |
| --- | --- | --- |
| 기술 질문 | 예를 들어 “기존 주문에도 적용할까요?”에 답변을 입력하고 전송 | 원래 대화에 이어지는 기술적 답변 |
| 권한 요청 | 제안한 파일 작업의 경로와 내용을 확인한 뒤 이번 한 건을 승인하거나 거절 | 제공자 프로토콜의 일회성 결정 |

실행 상세의 요청 이력에서 **결정 접수**와 **작업자에게 전달됨**을 확인할 수 있습니다. 연결이 끊기면 원래 호스트·실행 주소로 돌아옵니다. 이미 저장된 결정은 유지되고 같은 요청에 다른 결정을 보내면 충돌로 표시됩니다. 중지·시간 초과·작업자 종료 후에는 남은 요청이 만료됩니다. 결정 시각이 있고 전달 시각이 없는 만료 기록은 작업자가 결정을 받았는지 확인할 수 없는 경우입니다.

질문을 받은 동안에도 실행 제한 시간은 흐릅니다. 기본 작업자 호출은 900초이며 기술 질문은 호출당 최대 8회입니다. 요청·답변은 각각 UTF-8 8,192바이트까지 원문으로 기록합니다.

| 엔진 | 기술 질문 처리 | 사용자에게 전달할 수 있는 권한 요청 |
| --- | --- | --- |
| Codex | 작업실 요청함 모드에서 app-server의 같은 스레드로 후속 턴 전달 | 제공자가 요청한 작업 폴더 안의 파일 변경 한 건 |
| Cline CLI 3.0.74 | 실행 중인 같은 ACP 세션에서 후속 턴 진행 | 역할이 허용하는 `read_files` 및 `editor` 호출 한 건 |
| Gemini CLI | 실행 중인 같은 ACP 세션에서 후속 턴 진행 | 기존 호스트 정책 적용. 추가 대화형 권한 요청은 거절 |
| Claude Code · Antigravity | 완료된 질문 턴의 대화 UUID를 지정해 같은 작업 폴더에서 재개 | 현재 배치 연결은 기술 답변을 전달 |

요청함에는 제공자가 실제로 승인을 요구한 작업이 나타납니다. 예를 들어 Codex가 호스트 정책에 따라 작업 폴더 안의 편집을 이미 허용하면 그 편집은 바로 진행됩니다. 요청함은 신뢰하는 개인용 호스트와 기존 SSH 연결을 사용합니다. 운영체제·제공자의 접근 제한과 작업자의 배포·푸시·머지 제한은 계속 적용됩니다. [구현 계획](docs/superpowers/plans/2026-11-19-studio-inbox.md)

## 부재중 기술 답변과 복귀 보고

작업을 시작하기 전에 **이번 실행의 부재중 기술 답변 위임**을 켜고, 대신 답해도 되는 질문과 그 근거 파일을 선택합니다. 기본값은 꺼짐입니다. 위임은 선택한 호스트의 그 실행에만 적용됩니다.

| 위임할 질문 | 지정할 수 있는 근거 |
| --- | --- |
| 이 프로젝트의 테스트 명령은 무엇인가요? | `AGENTS.md`, `README.md`, `TESTING.md`에 명시된 단일 테스트 명령. 예를 들어 ‘테스트’ 절의 `python -m pytest -q` |
| 이 프로젝트가 요구하는 Python 버전은 무엇인가요? | `pyproject.toml`의 `[project]` 아래 `requires-python` |
| 이 프로젝트가 요구하는 Node.js 버전은 무엇인가요? | `package.json`의 `engines.node` |

근거 경로는 저장소 루트 기준입니다. 루트의 공통 지침·설정 파일 또는 선택한 폴더 안의 파일을 지정할 수 있습니다. 실행 접수 시점의 커밋을 고정하므로, 검증 결과에서 당시 파일과 인용 위치를 다시 찾을 수 있습니다.

테스트 명령은 `## Tests`나 `## 테스트` 절에 한 줄로 적을 수 있습니다. 문서의 첫 제목에 저장소 이름을 넣어도 됩니다. 예를 들어 `ai-agent-office` 저장소는 `# AI Agent Office`를 인식합니다. 선택한 하위 폴더에 둔 문서는 그 폴더 이름도 첫 제목으로 사용할 수 있습니다. 예전 버전이나 특정 환경에서만 쓰는 명령은 사람이 확인합니다.

등록한 질문과 일치하고 기준 커밋의 근거에서 답이 하나로 확정되면 모델 호출 없이 답과 인용 줄을 기록합니다. 독립 재확인 옵션을 켜면 전문가와 검증자가 별도 세션에서 같은 자료를 확인합니다. 이 경우 합의한 답과 인용만 원래 작업자에게 전달합니다. 다른 질문, 모호한 근거, 검증 실패는 사람이 처리할 질문으로 남습니다. **권한 요청은 항상 기존 사용자 요청함에서 처리합니다.**

돌아오면 **부재중 복귀 보고**에서 대리 답변과 남은 질문·권한 요청 수를 확인합니다. 결정마다 질문, 답, 전문가·검증자, 근거 파일·행·커밋, 전달 상태와 원래 실행을 볼 수 있습니다. 보고에는 최근 40건을 표시하고, 이전 결정의 근거는 각 실행 상세에 보관합니다. **이 실행의 위임 종료**를 누르면 원래 작업은 계속되며 아직 전송되지 않은 대리 답변은 같은 요청에서 사람의 답을 기다립니다. [구현 계획과 검증 기준](docs/superpowers/plans/2026-11-19-away-delegation.md)

## 역할별 스킬과 병렬 하위 작업

1. 실행 호스트를 선택하고 **호스트 스킬 관리**에서 이름과 Markdown 지침을 등록합니다. 기존 `SKILL.md`의 본문도 가져올 수 있습니다. 새 본문에는 새 버전이 생기고, 같은 이름·본문을 다시 등록하면 기존 버전을 반환합니다.
2. **역할별 스킬 선택**에서 개발자·리뷰어·부재중 전문가·독립 검증자에게 각각 최대 4개를 지정합니다. 실행에 고정된 버전의 지침이 해당 역할의 프롬프트에만 전달됩니다. 전문가와 독립 검증자의 스킬은 실제 위임 질문을 처리할 때 쓰입니다. 스킬이 제공하는 것은 Markdown 지침이며, 외부 도구·스크립트 설치와 실행 권한은 호스트가 관리합니다.
3. 여러 에이전트에 나눌 때는 상위 요청에 전체 목표와 공통 제약을 쓰고, **독립 하위 작업으로 나눠 실행**을 켭니다. 서로의 결과를 기다릴 필요가 없는 목표를 2~4개 입력하고 각각 담당 폴더와 개발자·리뷰어 엔진을 선택합니다. 예를 들어 UI 수정은 `web` 폴더의 Claude Code에, API 검증은 `api` 폴더의 Codex에 맡길 수 있습니다.
4. 부모 실행을 열어 각 하위 작업의 상태, 스킬 버전, 변경 파일과 diff, 테스트 출력, 리뷰 의견을 확인합니다. 하위 작업의 실행 ID를 열면 해당 작업의 요청함과 전체 상세로 이동합니다. 부모 주소를 다시 열어도 같은 하위 작업이 연결됩니다.

한 부모의 하위 작업은 **선택한 한 호스트**에서 같은 Git 커밋을 기준으로 각자의 worktree를 만듭니다. 같은 작업실 저널의 병렬 작업 슬롯은 기본 **2개**입니다. 호스트에서 Studio를 시작하기 전에 환경 변수 `AGENT_OFFICE_TEAM_SLOTS`를 1~6으로 지정할 수 있으며, 먼저 맡긴 작업부터 빈 슬롯을 사용합니다. 자식 하나가 실패해도 형제 작업의 결과는 남습니다. 부모의 검토 대기 상태는 모든 자식이 개별 검증을 마쳤다는 뜻입니다. 여러 worktree의 변경을 함께 반영할 때에는 사람이 검토하고 통합 결과를 검증합니다.

부모를 중지하면 대기 중인 자식은 시작하지 않고, 실행 중인 자식은 기존 중지 절차를 따릅니다. 작업실이 비정상 종료되면 소유권을 확인해 복구 필요 상태로 기록합니다. [구현 계획과 검증 기준](docs/superpowers/plans/2026-11-19-role-skills-parallel.md)

## 오피스 보기

Studio 상단의 **오피스 보기**를 열면 캐릭터마다 역할·CLI 엔진, 맡은 작업과 진행 상태가 표시됩니다. **전체 호스트**에서 이 컴퓨터와 등록한 SSH 호스트를 함께 보거나 원하는 호스트만 선택하세요. 같은 엔진이 병렬 작업 두 개를 맡으면 각 실행의 자리가 따로 생깁니다.

- **응답이 필요한 자리**에는 기술 질문과 권한 요청이 서로 다른 말풍선으로 표시됩니다. 캐릭터나 말풍선을 클릭하면 해당 호스트의 실행 상세가 열립니다. 병렬 작업에서는 질문을 보낸 자식 실행으로 이동합니다.
- **업무 자리**에서는 개발·테스트·리뷰 단계와 차례 대기, 위임된 질문 확인 상태를 볼 수 있습니다. 부재중 전문가와 독립 검증자는 해당 역할의 기록이 생겼을 때 표시됩니다.
- **최근 실행 기록**은 호스트별로 가장 최근 종료된 실행 8개를 보여줍니다. 한 페이지는 실행 24개이며, 한 자리에서 요청 미리보기는 최대 3건입니다. 전체 요청은 실행 상세에서 확인할 수 있습니다.

화면이 열려 있는 동안 상태를 주기적으로 조회합니다. 연결이 끊긴 호스트에는 마지막 기록과 확인 시각이 남고, 상단 합계에는 연결된 호스트의 값만 반영됩니다. 두 컴퓨터 모두 오피스 조회를 지원하는 버전의 Studio를 사용하세요. [구현 계획과 검증 기준](docs/superpowers/plans/2026-11-19-office-view.md)

## 작업 이력에서 개선안을 제안하고 안정 소스로 승격하기

**안정 버전 이력** 화면에서 선택한 호스트의 현재 안정 소스와 이전 승격·롤백을 보고 두 시점의 변경 파일과 diff를 비교할 수 있습니다. 운영자가 조회 파일에 이력을 게시할 때 `studio-sync --stable-history`를 지정합니다. 롤백은 운영자 터미널에서 변경 방향을 검토하고 확인 문구를 입력하면 기록됩니다. [이력 게시와 롤백 예시](docs/improvement-promotion.md#안정-버전-이력과-롤백)

같은 화면의 **서비스 배포 상태**에서 선택한 안정 버전과 실제 실행 버전을 비교할 수 있습니다. 운영자 터미널에서 등록된 서비스의 재시작을 승인하면 새 프로세스의 부팅 ID와 Studio 응답을 확인합니다. 기동 검증에 실패하면 배포 직전에 정상 응답했던 버전으로 복구를 시도하고 결과를 기록합니다. [서비스 등록·배포·복구](docs/studio-deployment.md)

**작업실 → 개선안**에서 로컬 또는 등록한 원격 호스트를 선택하고, 종료된 실행 최대 8건과 제안 엔진을 고릅니다. 생성 상세에서 제안 이유와 패치를 읽고 연결된 후보를 열면 실제 diff, 검증 출력, 승인·거절 시각이 표시됩니다. 후보 데이터는 운영자가 마지막으로 게시한 시각을 기준으로 합니다. [작업실 화면 연결과 운영자 설정](docs/improvement-promotion.md#작업실-화면-연결)

`agent-office improve`로 Office 자체의 소스 변경을 검토할 수 있습니다. 작업 에이전트는 사용자가 고른 실행의 이벤트를 인용해 패치를 제안합니다. 운영자는 별도의 릴리스 디렉터리에 후보를 접수하고 회귀 검사를 실행합니다. HTML 보고서에서 근거·전체 diff·검사 출력을 확인한 뒤 운영자 터미널에서 해당 커밋을 승인하면 안정 소스가 바뀝니다.

Windows와 Linux에서 같은 CLI를 사용합니다. 후보 검증에는 로컬 Linux 컨테이너를 실행할 Docker 엔진이 필요합니다. [설정과 사용 예](docs/improvement-promotion.md) · [구현 계획](docs/superpowers/plans/2026-11-19-improvement-promotion.md)

## Architecture

```text
User task -> Agent Office CLI
            -> Git worktree (isolated working directory)
            -> LangGraph
               -> Developer CLI (Codex / Antigravity / Claude Code / Gemini CLI / Cline CLI)
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

Task and event queries, explicit interruption marking and candidate DB paths are described in the [storage guide](docs/storage.md). Use `--db` before the subcommand to keep development state separate from stable state. Inbox continuations belong to the original Studio run. A run marked for recovery requires inspection.

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

By default development uses `codex` and review uses `antigravity` (`agy`). Override either role, for example with `--developer gemini --reviewer cline`. The other selectable engine is `claude`. Product workers do not merge/push/deploy their work. Codex's repository maintenance follows the owner's separate CI-gated workflow in AGENTS.md.

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

현재 구성과 책임 경계는 [아키텍처](docs/architecture.md)를 참조하세요. 프로젝트 라이선스는 [Apache-2.0](LICENSE)입니다.
