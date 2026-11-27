# 데스크톱 설치판 배포

Windows x64와 Ubuntu amd64용 Electron 앱을 GitHub Releases에서 배포한다. 사용자는 [최신 릴리스](https://github.com/tworimpa/ai-agent-office/releases/latest)의 **Assets**에서 운영체제에 맞는 설치 파일을 선택한다.

| 파일 | 용도 |
| --- | --- |
| `AI-Agent-Office-Setup-<버전>.exe` | Windows 사용자별 설치. 설치 후 시작 메뉴에서 AI Agent Office를 연다. |
| `ai-agent-office-desktop_<버전>_amd64.deb` | Ubuntu x64 설치. 다운로드 폴더에서 `sudo apt install ./ai-agent-office-desktop_<버전>_amd64.deb`를 실행한다. |
| `SHA256SUMS.txt` | 다운로드한 두 설치 파일의 SHA-256 목록. |

Windows에서는 PowerShell의 `Get-FileHash .\AI-Agent-Office-Setup-<버전>.exe -Algorithm SHA256` 결과를 같은 파일명 옆의 값과 대조한다. Ubuntu에서는 합계 파일과 패키지를 같은 폴더에 두고 `sha256sum --ignore-missing --check SHA256SUMS.txt`를 실행해 내려받은 파일명이 `OK`로 표시되는지 확인한다. Windows 설치 파일의 코드 서명 상태는 해당 버전의 릴리스 노트에서 확인한다.

앱에는 로컬 Studio와 Python 런타임이 포함된다. 코드 작업을 실행할 호스트에는 Git과 사용할 에이전트 CLI가 필요하다. 기존 개인용 앱을 업그레이드할 때는 진행 중인 작업이 끝난 뒤 앱 메뉴의 **작업실 → 로컬 작업실 종료**를 선택하고 앱을 닫은 상태에서 새 버전을 설치한다. [첫 실행·저널 위치·제거 안내](desktop.md)

## 운영자 절차

1. 버전 변경 PR에서 `desktop/package.json`과 잠금 파일의 앱 버전을 맞춘다. PR을 병합한 후 해당 **main push의 전체 커밋 SHA**를 확인한다.
2. 그 커밋의 **CI (Windows + Linux)** 네 작업과 **Desktop installers** 세 작업(두 OS의 설치 검사와 `tested-merge / ubuntu-latest / Tested merge`)의 성공을 기다린다. 설치 검사를 완료한 각 OS 작업이 남긴 영수증에는 커밋, 실행 ID, 시도, 설치 파일명 및 해시, Actions 아티팩트 ID와 digest가 들어간다.
3. Actions의 **Electron desktop release**를 기본 브랜치에서 수동 실행한다. `version`에는 예를 들어 `0.9.0`, `commit`에는 40자리 전체 SHA를 입력한다. `dry-run`은 패키지와 출처를 검증하고, `draft`는 태그·초안 생성과 설치 검사를, `publish`는 설치 검증 후 공개까지 수행한다. 동일한 버전의 공개 릴리스에는 새 파일을 덮어쓸 수 없다.
4. 초안에 등록된 두 설치 파일을 다시 받아 해시를 확인하고, 일회용 Windows와 Ubuntu에서 새 설치와 기존 설치판 업그레이드를 검사한다. 새 설치 후 Workspace 등록, 업그레이드 후 기존 저널·Workspace·작업 이력 재열기가 완료 기준이다. 0.10.0부터는 공개 0.9.0을 기준으로 두 OS의 업그레이드도 검사한다.
5. 검증 후 공개 모드에서는 두 OS 검사가 모두 통과한 다음 공개한다. 초안 모드는 검증 결과를 확인할 수 있도록 릴리스를 Draft 상태로 둔다.

0.8.1 업그레이드 기준은 이슈 #40 변경 전에 0.8.1을 도입한 PR #37의 [기존 설치 CI](https://github.com/tworimpa/ai-agent-office/actions/runs/45030107146)의 성공 작업이 사용한 Windows 아티팩트 14757755238와 Linux 아티팩트 14757447261다. 이 기준 파일이 만료되면 고정 소스 `bbf4e06`에서 0.8.1을 새로 빌드하고, 원본과 구분해 실제 설치한 파일의 해시를 기록한다.

0.9.0 업그레이드 검사는 [공개 릴리스](https://github.com/tworimpa/ai-agent-office/releases/tag/v0.9.0)의 태그·릴리스 ID와 설치 파일·체크섬의 자산 ID 및 해시를 확인한다. 설치한 원본 자산과 업그레이드 후 다시 연 저널·작업 기록을 CI 증거에 남긴다.

CI 아티팩트는 보관 기간이 있다. 검증된 아티팩트가 만료되었거나 실행·버전·해시가 일치하지 않으면 해당 SHA에 대해 빌드와 설치 검사를 다시 수행한다. 부분 업로드 이력이 있으면 태그가 가리키는 커밋과 기존 자산의 SHA-256부터 확인한다. 업로드 도중 502가 발생해 GitHub에 크기 0, digest 없음, 상태 `starter`인 항목만 남으면 자동 재시도는 중단된다. 운영자가 같은 태그·커밋의 초안인지, 다른 업로드가 종료됐는지, 자산 ID·이름·상태가 모두 해당 빈 항목인지 확인한 뒤 그 항목만 삭제하고 다시 실행한다. 이미 업로드된 파일이 하나만 빠진 경우에는 기존 일치 파일을 유지하고 누락분만 올린다. 다른 내용이 있는 공개 버전을 수정하려면 새 버전으로 배포한다.

이 절차는 개인용 설치판을 배포한다. 운영 중인 AgentOfficeStudio 서비스의 안정 승격과 재시작은 [서비스 배포 절차](studio-deployment.md)를 따른다. Rust 비교판의 설치 대상과 Studio 연결 방식은 [비교판 안내](rust-comparison.md)에 있다.
