# 공급자별 실행 프로필과 사용량

개발자와 검토자는 같은 엔진에서도 서로 다른 모델과 추론 강도를 요청할 수 있다. 선택값은 해당 CLI 호출에만 전달한다. 권한 모드, 샌드박스, 작업 디렉터리와 사람의 승인 절차는 기존 어댑터가 적용한다.

| 엔진 | 호출별 모델과 추론 강도 | 확인할 수 있는 실행 근거 |
| --- | --- | --- |
| Codex | `exec --model`과 `-c model_reasoning_effort=…`. 대화형에서는 `thread/start`와 각 `turn/start`에 선택값을 보낸다. | 앱 서버가 응답한 모델·강도, 재라우팅된 모델, 스레드 토큰, 컨텍스트, 계정 한도. `exec --json`에서는 완료된 턴의 토큰만 수집한다. |
| Antigravity | `agy --model`, `--effort`를 첫 호출과 같은 대화의 재개 호출에 전달한다. | 최종 응답의 토큰 합계와 입력·출력·캐시·추론 내역. 계정 한도는 미확인이다. |
| Claude Code | `claude --model`, `--effort`를 첫 호출과 같은 대화의 재개 호출에 전달한다. | 최종 응답의 입력·출력·캐시 토큰과 달러 비용. Studio 스트림에서 해당 세션의 계정 한도 이벤트를 수집한다. |
| Gemini CLI | ACP 프로세스 시작 시 `--model`을 전달한다. | ACP가 보고한 컨텍스트 점유와 창 크기. 세션 누적 토큰을 제공하는 버전에서는 완료 응답의 합계를 기록한다. 계정 한도는 미확인이다. |
| Cline | 검증된 ACP 버전의 기본 모델과 추론 설정을 사용한다. | ACP가 보고한 컨텍스트 점유와 창 크기. 세션 누적 토큰을 제공하는 버전에서는 완료 응답의 합계를 기록한다. 계정 한도는 미확인이다. |

### 실행 호스트의 Codex 모델 목록

`ModelCatalog.refresh()`는 빈 임시 디렉터리에서 호스트의 Codex 앱 서버에 연결한다. 고정된 초기화와 `model/list` 요청으로 보이는 모델을 최대 8페이지 읽는다. 전체 기한은 10초, 응답 총량은 4 MiB다. 모델 ID, 표시 이름, 사용 가능한 강도와 기본값만 메모리에 남긴다. 목록 읽기와 선택 검증은 마지막 스냅샷을 사용한다. 계정이나 실행 환경을 바꾼 뒤에는 목록을 새로 고쳐야 한다. 실패한 새로 고침은 해당 호스트의 목록을 확인 불가로 표시한다.

### 사용량과 계정 한도

Codex 대화형 스레드의 누적 토큰은 같은 호출의 최신 관측값으로 갱신한다. 마지막 응답의 토큰 합계(`last.totalTokens`)와 모델 창 크기(`modelContextWindow`)로 컨텍스트 여유를 추정한다. 이 값의 출처는 `last_response_total`이며 컨텍스트 압축이나 다음 입력 때문에 실제 여유가 달라질 수 있다. `exec`에서는 입력과 출력을 더해 합계를 구한다. 캐시 입력과 추론 출력은 각각의 부분합이다. 수치가 없으면 미확인으로 남긴다. 재라우팅 알림은 원래 요청과 별도로 관측된 모델을 갱신한다. [Codex 앱 서버](https://developers.openai.com/codex/app-server/) · [Codex 비대화형 실행](https://developers.openai.com/codex/noninteractive/)

`probe_quota("codex")`는 고정된 `account/rateLimits/read`로 계정 한도를 읽는다. 이 요청에는 프롬프트와 모델 턴이 없다. 일반 사용 허용 여부, 한도 버킷, 기본·보조 시간 창의 사용률과 갱신 시각을 정규화한다. 계정 ID가 있으면 공급자 이름을 포함한 SHA-256 지문만 반환한다. 대화형 작업에서도 공급자가 OpenAI로 확인되면 같은 세션에서 한도를 읽고, 이후 계정 변경 알림을 받으면 기존 계정 연결을 해제한다. 첫 턴 전에 해당 한도가 소진된 경우 작업을 시작하기 전에 중단한 사실을 기록한다. 일반 사용 허용 응답과 특별 모델 버킷의 잔여 한도는 별도로 판단해야 한다.

Windows에서 Codex 0.170.0의 생성된 JSON 스키마와 메타데이터 조회, Antigravity 1.5.12·Claude Code 2.1.311·Gemini CLI 0.64.0의 도움말로 인터페이스를 확인했다. 프로토콜 회귀 테스트는 모의 CLI를 사용한다. 인증된 실제 작업의 모델 선택, 과금, Linux 공급자 동작은 별도 검증 대상이다.

Claude와 Antigravity의 재개 호출은 이전 대화의 집계 범위가 불명확하여 복수 호출의 합계를 미확인으로 남긴다. ACP의 usage_update.used는 현재 컨텍스트를 뜻한다. 토큰 예산 계산에는 종료 응답의 명시적인 usage.totalTokens를 사용한다. [Claude 비용·사용량](https://platform.claude.com/cookbook/claude-agent-sdk-04-migrating-from-openai-agents-sdk) · [Antigravity 헤드리스 출력](https://antigravity.google/docs/cli/headless/) · [ACP v1 스키마](https://github.com/agentclientprotocol/agent-client-protocol/blob/main/schema/v1/schema.json) · [ACP v1 확장 스키마](https://github.com/agentclientprotocol/agent-client-protocol/blob/main/schema/v1/schema.unstable.json)
