# 봇 관리 클라이언트와 실행 호스트

등록된 로컬 Workspace가 있으면 Studio 시작 주소에 봇 클라이언트가 열린다. 처음 사용하는 PC에서는 작업 폴더를 등록한 뒤 **봇 회의실**을 연다. `/bots`는 봇 관리, `/workspace`는 작업실 등록·역할 배정·엔진 설정으로 이어진다. 기존 실행 링크는 해당 실행의 질문, 권한, 계획 확인과 사람 검토 화면을 연다.

## 회의실 사용

- **봇 만들기**에서 이름과 자유 역할, 업무 지침, 기본 캐릭터를 선택한다. 캐릭터는 12종이며 이미지를 올려 교체할 수 있다. Studio는 업로드 이미지를 작은 정적 WebP로 변환한다.
- 왼쪽 목록이나 방 안의 캐릭터를 누르면 봇의 대화가 가운데 열린다. 메시지를 보내면 선택한 봇에 그 작업을 접수한다. 기본 의뢰는 읽기 전용이다. 수정은 해당 의뢰에서 **파일 수정 허용**을 선택한다. 선택한 봇이 독립 리뷰를 맡고 있다면 다른 리뷰어를 먼저 배정한다.
- 오른쪽 탐색기에서 파일을 열거나 폴더·파일을 만든다. 가운데 편집 탭은 상대 경로를 사용하므로 서로 다른 폴더의 같은 파일 이름도 따로 열 수 있다. 열린 탭·미저장 초안은 같은 브라우저 탭의 작업실·봇별 상태로, 저장된 파일은 호스트의 정의로 관리한다. 저장 버전이 달라지면 충돌을 확인하고 다시 연다.
- 고정 시점의 방에서 기본 캐릭터 12종은 앞·뒤·양옆으로 방향을 바꾸며 각자의 마룻바닥 구역을 걷는다. 직접 올린 캐릭터도 원래 그림을 유지하며 이동한다. 캐릭터에 마우스를 올리거나 키보드 초점을 주면 그 자리에서 멈추며, 방 전체를 멈추려면 **이동 일시정지**를 누른다. 왕은 책상에 앉아 있다. 왕을 누르고 명령을 작성하면 클라이언트가 역할 이름과 명시한 담당자를 바탕으로 분담안을 제안한다. 운영자가 담당 업무를 확인한 뒤 보내면 **봇별로 별도의 실제 실행**이 생성된다. 명령과 답변은 해당 봇의 대화와 작업 기록에서 확인한다.
- **봇 정의 가져오기·내보내기**로 다른 클라이언트와 파일을 주고받는다. 디스크의 임의 경로, 호스트 로그인, 권한이나 과거 대화를 가져오지 않는다. 화면을 열거나 정의를 가져오기만 하면 CLI 작업은 시작되지 않는다.

## 다른 클라이언트에서 사용하기

[Bot Definition 1.0](bot-spec-v1.md)은 독립적인 JSON 문서다. 로컬 파일의 오프라인 검증에는 Studio가 필요하지 않다.

```shell
agent-office bots validate --file research.bot.json
agent-office --db office.sqlite3 bots import --workspace-id WORKSPACE_ID --file research.bot.json
agent-office --db office.sqlite3 bots export --workspace-id WORKSPACE_ID --id ux-researcher --output researcher-copy.json
```

실행이 필요한 클라이언트는 로컬 Studio의 HTTP API에 연결한다. 먼저 `GET /api/studio-identity`로 저널 ID를, `GET /api/workspaces`로 Workspace를 선택한다. 아래 경로에서 `W`는 Workspace ID, `B`는 봇 ID다. 실제 요청에는 선택한 `?office_journal=저널ID`를 붙인다. 쓰기 요청은 동일 출처의 `Origin`, `X-Agent-Office: studio`, `Content-Type: application/json`을 사용한다. 다른 저널에 연결되면 409가 반환된다. 호스트의 기존 로컬 접근·원격 경계를 적용한다.

| HTTP | 경로 | 용도 |
| --- | --- | --- |
| GET | `/api/bot-spec` | 공개 버전과 스키마·예제 URL |
| GET | `/api/rooms/W/bots` | 봇 목록, 현재 배정, 표시용 외형 |
| POST | `/api/rooms/W/bots/import` | 원본 UTF-8 JSON 정의 설치. 같은 ID와 같은 정의는 재사용 |
| GET | `/api/rooms/W/bots/B/manifest` | 폴더·파일·확장을 포함한 이식 가능한 정의 내보내기 |
| GET | `/api/rooms/W/bots/B/package` | 개인 메모를 제외한 Git 공유 패키지 ZIP 내려받기 |
| POST | `/api/rooms/W/bots/package-preview` | 공개 HTTPS URL과 버전의 패키지 정보 및 커밋·정의 해시 확인 |
| POST | `/api/rooms/W/bots/package-install` | 미리 확인한 커밋·정의 해시에 해당하는 봇 설치 |
| GET / POST | `/api/rooms/W/bots/B/assets` | 파일 트리 조회 및 버전 기반 생성·저장 |
| POST | `/api/rooms/W/bots/B/appearance` | `{digest,appearance}`로 현재 외형 확장만 변경 |
| GET | `/api/rooms/W/bots/B/portrait` | 직접 올린 정적 WebP. 원격 이미지 요청 없음 |
| GET | `/api/rooms/W/bots/B/chat` | 봇의 최근 대화, 메시지별 실행 ID와 상태 |
| POST | `/api/rooms/W/bots/B/commands` | 개별 의뢰 접수. 응답 `{run}` |
| GET / POST | `/api/rooms/W/bot-orders` | 왕명 목록 조회 또는 확정한 역할별 업무 접수 |
| GET | `/api/runs/RUN_ID` | 기존 실행 저널의 상세 상태와 결과 |

자산 변경 예시는 `{"kind":"file","create":true,"filename":"skills/summarize/SKILL.md","content":"요약 방법을 기록한다.","digest":"현재해시"}`다. 빈 폴더는 `kind:"folder"`, `create:true`로 생성한다. 기존 파일은 `kind:"file"`, `create:false`로 편집한다. 오래된 해시로 저장하면 409가 반환된다. 클라이언트가 관리하지 않는 확장은 보존한다.

개별 의뢰는 `id`(32자리 hex), `message`, `allow_edits`, `delegation`과 선택 폴더 `indices`를 기존 Studio 규칙에 맞춰 보낸다. 왕명은 `{id,message,allow_edits,assignments:[{bot_id,duty}],indices?}`를 사용한다. 접수 결과를 받기 전에 연결이 끊기면 같은 ID와 본문으로 재시도한다. 왕명은 접수된 봇을 보존하고 미접수 항목의 상태·오류를 반환한다. 보호된 요청과 사람의 최종 검토는 기존 실행 절차에서 처리한다.

채팅은 최근 30회 실행, 왕명은 최근 20건부터 제공한다. 이전 기록이 있으면 응답의 `next_cursor`를 같은 목록 경로 뒤에 `/before/{next_cursor}`로 붙인다. 후속 명령의 모델 입력에는 성공적으로 끝난 같은 봇의 최근 대화 최대 네 쌍이 포함된다. 각 발화는 UTF-8 1 KiB까지 제공한다. 진행 중이거나 실패한 실행은 상태와 실행 링크로 표시한다. 이 연속성은 독립 CLI 실행 사이에서 호스트가 제공한다.

Git 패키지 미리보기 요청은 `{url,version,path?}`를 보낸다. 확정 요청에는 미리보기의 `source.commit`과 `digest`를 `commit`, `digest`로 추가한다. 설치 기록은 봇 목록의 `package`에 저장소 URL, 선택 하위 폴더, 입력 버전, 실제 커밋 SHA와 설치 당시 정의 해시로 표시된다. [패키지 작성과 설치](local-role-bots.md#공개-git-저장소에서-공유하기)

공개 정의와 UI 파일은 별도로 버전을 관리한다. 엔진 인증, 실행 권한, 저널·검토 정책은 호스트의 책임이다. 스킬과 지침은 텍스트 자원으로 보관할 수 있으며 MCP·훅 구성 파일은 이 버전에서 자동으로 연결하거나 실행하지 않는다. 패키지 갱신 계획은 [이슈 #46](https://github.com/tworimpa/ai-agent-office/issues/46)에서 추적한다.
