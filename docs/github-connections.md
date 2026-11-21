# GitHub 연결과 실행 호스트 설정

GitHub 연결은 Studio를 실행하는 호스트가 관리합니다. 운영자가 JSON에 등록한 이름을 작업실에서 고르면 해당 호스트가 HTTPS로 GitHub REST API를 호출합니다. 폴링 방식이므로 GitHub에서 사내 서버로 들어오는 웹훅 경로나 공개 수신 포트가 필요하지 않습니다. 로컬 Studio와 SSH 원격 Studio는 각자 자신의 설정과 설치 권한을 사용합니다.

## 지원 범위

| 연결 | 웹 주소 예시 | REST 루트 | 확인 범위 |
| --- | --- | --- | --- |
| GitHub.com 및 Enterprise Cloud | `https://github.com` | `https://api.github.com` | 공개 저장소 익명 조회 및 모의 설치 인증 |
| Enterprise Cloud 데이터 레지던시 | `https://tenant.ghe.com` | `https://api.tenant.ghe.com` | 주소 변환 및 설정 검증. 실제 테넌트는 별도 확인 |
| GitHub Enterprise Server 3.19 이상 | `https://git.example.org:8443` | `https://git.example.org:8443/api/v3` | 3.19 REST 계약의 모의 설치·저장소 응답. 실제 사내 설치는 별도 확인 |

프로젝트의 GHES 최소 호환 기준은 3.19입니다. 실행 호스트가 `GET /meta`의 `installed_version`을 확인하며 이전 버전이나 버전 미제공 응답은 차단합니다. 모든 연결은 REST API `2022-11-28`을 사용합니다. 데이터 레지던시 도메인은 테넌트별로 다릅니다. [GitHub API 버전](https://docs.github.com/en/rest/about-the-rest-api/api-versions) · [Enterprise Cloud 설치 API](https://docs.github.com/en/enterprise-cloud@latest/rest/apps/installations) · [GHES 메타데이터](https://docs.github.com/en/enterprise-server@3.19/rest/meta/meta)

## GitHub App 준비

1. 해당 GitHub 호스트에 조직 또는 사용자 소유 GitHub App을 등록하고 웹훅을 비활성화합니다. 설치 시 작업할 저장소만 선택하세요. App ID와 Installation ID를 확인하고 해당 앱의 RSA PEM 개인 키를 Studio 실행 호스트의 운영자 관리 경로에 둡니다.
2. 사용하는 기능에 맞춰 저장소 권한을 설정합니다. 저장소 식별과 참여자 권한 확인은 Metadata 읽기, 이슈 접수는 Issues 읽기, 상태 댓글·라벨 반영은 Issues 쓰기, PR·리뷰 조회는 Pull requests 읽기, CI 조회는 Checks 및 Commit statuses 읽기를 사용합니다. 후보 브랜치와 초안 PR을 발행하려면 Contents 쓰기 및 Pull requests 쓰기가 추가로 필요합니다. 커밋·브랜치 확인에는 Contents 읽기가 필요합니다. 설치된 서버와 저장소 요금제에서 초안 PR을 지원해야 합니다. [GitHub App 권한표](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps) · [초안 PR 지원 범위](https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request)
3. 호스트에 아래와 같은 JSON을 저장합니다. 예시는 Linux 경로입니다. Windows에서는 `C:/ProgramData/AgentOffice/github/office.pem`과 같은 절대 경로를 사용하세요. 키와 설정 파일은 개발 체크아웃 및 후보 worktree 밖에 두고 운영자와 Studio 서비스 계정으로 파일 접근을 제한하세요. 동일 운영체제 계정으로 실행되는 worker와 Studio 사이에 파일시스템 격리가 필요하면 별도 OS 접근 제어가 필요합니다.
4. Studio를 시작하는 계정의 `AGENT_OFFICE_GITHUB_CONFIG`에 JSON 파일의 절대 경로를 지정하고 Studio를 시작합니다. 작업실에서 연결 진단과 저장소 목록을 확인합니다. 설치된 앱도 해당 Studio 프로세스에 설정 경로가 전달되어야 합니다.

```json
{
  "connections": [
    {
      "id": "office-cloud",
      "name": "개발팀 GitHub",
      "kind": "cloud",
      "app_id": 12345,
      "installation_id": 67890,
      "private_key_file": "/srv/office/github/app.pem"
    },
    {
      "id": "office-ghes",
      "name": "사내 GitHub",
      "kind": "ghes",
      "web_url": "https://git.example.org",
      "app_id": 123,
      "installation_id": 456,
      "private_key_file": "/srv/office/github/enterprise.pem",
      "ca_file": "/srv/office/github/corporate-ca.pem",
      "proxy": "direct"
    },
    {
      "id": "public",
      "name": "공개 연결 점검",
      "kind": "cloud",
      "public_repositories": ["octocat/Hello-World"]
    }
  ]
}
```

데이터 레지던시에는 `kind`를 `residency`, `web_url`을 `https://<tenant>.ghe.com`으로 설정합니다. REST 루트는 웹 주소에서 계산됩니다. `api_url`을 함께 기재하면 계산된 값과 일치해야 합니다. 공개 연결은 등록된 공개 저장소 조회 및 진단에 쓸 수 있습니다. 댓글·라벨·초안 PR에는 앱 설치가 필요합니다.

`ca_file`은 시스템 신뢰 저장소와 함께 검증할 PEM CA 묶음입니다. `proxy` 기본값 `system`은 실행 호스트의 표준 프록시 설정을 따릅니다. `direct`는 직접 연결, `http://proxy.example.org:3128` 같은 값은 지정한 프록시를 사용합니다. 명시적 프록시 URL에는 사용자명·암호를 넣을 수 없습니다. 운영체제에서 사내 DNS와 VPN 경로를 먼저 구성하세요. TLS 인증서·호스트명 검증은 항상 켜집니다.

## 인증 수명 및 재시도

호스트에서 서명한 RS256 JWT로 설치 토큰을 발급합니다. 만료 시각 90초 전부터 다음 요청이 토큰을 갱신합니다. 조회가 401을 받으면 한 번 새 토큰으로 재시도하고 쓰기 요청은 재전송하지 않습니다. 설치 토큰은 프로세스 메모리에만 보관합니다. JSON에는 키 파일 경로만 넣으며 Studio 작업 저널·브라우저·worker 환경으로 설치 토큰이나 PEM을 전달하지 않습니다. [설치 토큰 발급](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)

즉시 접근을 끊으려면 해당 GitHub 호스트에서 설치를 일시 중지 또는 제거하세요. 보관된 설치 토큰은 설치 토큰 폐기 API로 무효화할 수 있습니다. 하나의 토큰만 폐기하면 앱과 설치가 활성인 동안 후속 발급은 가능합니다. 키 교체는 신규 키 파일을 호스트에 준비한 후 기존 키를 GitHub에서 폐기하며 이미 발급된 토큰의 수명도 별도로 확인하세요. [GitHub App 설치 관리](https://docs.github.com/en/rest/apps/apps#suspend-an-app-installation) · [설치 토큰 폐기](https://docs.github.com/en/rest/apps/installations#revoke-an-installation-access-token)

## 연결 진단 읽기

| 결과 코드 | 실행 호스트에서 확인할 항목 |
| --- | --- |
| `ok` | 메타데이터 또는 해당 설치의 저장소 목록 응답 성공. 개별 쓰기 권한은 실제 동작별로 확인 |
| `ca_unavailable`, `tls_verification_failed` | CA 묶음 경로·읽기 권한, 서버 인증서의 이름과 체인 |
| `dns_or_vpn_unavailable`, `connection_timeout`, `network_or_proxy_unavailable` | 서비스 계정의 DNS, VPN 경로, 프록시 및 방화벽 |
| `app_key_unavailable`, `authentication_rejected` | 지정한 PEM의 형식·권한, App ID, 설치 상태, 호스트 시계 |
| `permission_denied`, `not_found` | 설치 ID·저장소 선택·앱 권한. GitHub가 비공개 자원에 404를 반환할 수 있음 |
| `rate_limited` | GitHub의 Retry-After 또는 X-RateLimit-Reset 이후 재시도 |
| `redirect_refused`, `unsupported_ghes_version` | 리다이렉트 없는 최종 HTTPS 주소와 GHES 버전 |
| `pagination_limit`, `response_too_large` | 한 번에 읽는 목록이 20쪽 또는 단일 응답 8 MiB 한계를 초과. 이 목록의 결과는 미완료 |

API 응답이 제공한 링크에서 연속된 다음 쪽 번호만 사용하며 원래 연결·경로·필터로 요청을 재구성합니다. 교차 호스트 링크와 HTTP 리다이렉트는 자격 증명을 전달하기 전에 차단합니다. [GitHub 페이징](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api) · [GitHub 요청 제한 및 조건부 조회](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)

## 초안 PR의 Git 쓰기 범위

앱은 승인된 파일로 Git blob과 tree를 만들고 지정한 부모 커밋과 트리를 확인한 뒤 `ao/` 후보 브랜치와 초안 PR을 생성합니다. 같은 후보를 재시도하면 기존 브랜치에서 부모와 트리가 일치해야 하며 초안의 저장소·브랜치·SHA·앱 작성자도 일치해야 합니다. 후속 후보는 연결된 같은 초안의 head에서만 fast-forward로 진행합니다. GitHub REST의 ref 갱신은 `force: false`를 지원하며, 업데이트와 같은 트랜잭션에서 이전 SHA나 PR 초안 상태를 조건으로 지정하는 기능은 제공하지 않습니다. 동시 작성자가 관리 권한으로 강제 재설정할 수 있는 저장소에서는 검사와 갱신 사이에 상태가 바뀔 수 있습니다. Studio가 갱신 후 실제 원격 head를 다시 확인합니다. [Git tree 생성](https://docs.github.com/en/rest/git/trees#create-a-tree) · [Git ref 갱신](https://docs.github.com/en/rest/git/refs#update-a-reference)

## 오프라인 동안 바뀐 기준 브랜치

등록된 체크아웃에 기준 커밋이 있으면 해당 Git 객체를 그대로 사용합니다. 커밋이 없으면 호스트가 등록된 GitHub의 HTTPS 저장소 주소로 해당 브랜치만 fetch합니다. GitHub App 연결에서는 이 요청 전용으로 저장소 ID 하나와 `contents: read`만 지정한 별도 설치 토큰을 발급하고 응답 권한도 대조합니다. 이 토큰은 임시 bare 저장소의 Git 전송 프로세스 환경에만 넣으며 전송 직후 폐기합니다. GHES도 웹 주소 아래 `/OWNER/REPO.git`으로 전송하며 REST는 `/api/v3`에만 요청합니다. [GitHub 설치 토큰의 범위 지정](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)

호스트의 등록된 CA와 프록시를 적용합니다. SSH, credential helper, 리다이렉트, submodule 조회, 원래 저장소의 URL 치환·HTTP 헤더·hooks는 사용하지 않습니다. 요청한 SHA와 Git이 받은 SHA, 전송 후 REST에서 조회한 SHA와 저장소 ID가 일치하면 검증한 객체를 로컬 객체 저장소에 추가합니다. 사용자의 작업 파일, 인덱스, 브랜치, 원격 추적 ref는 보존됩니다. 전송은 120초, 팩은 256 MiB 한도입니다. [Git fetch](https://git-scm.com/docs/git-fetch) · [Git index-pack](https://git-scm.com/docs/git-index-pack)

`git_fetch_failed`이면 실행 계정에서 Git 스마트 HTTPS 접근·CA·프록시·Contents 읽기 권한을 확인하세요. `fetch_scope_unconfirmed`는 GitHub가 요청한 설치 토큰 권한·저장소 한정을 확인해 주지 않은 경우입니다. `fetch_head_changed`이면 다음 동기화가 최신 기준으로 다시 시도합니다. `git_fetch_too_large` 또는 `git_fetch_timeout`이면 운영자가 해당 체크아웃에서 정상적인 Git 클라이언트로 브랜치를 한 번 가져오면 Studio가 로컬 객체부터 재확인합니다.
