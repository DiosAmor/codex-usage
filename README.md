# Codex Usage

여러 ChatGPT/Codex 계정의 **잔여 사용량과 초기화권**을 로컬 컴퓨터의 터미널에서 한 번에 확인하는 개인용 도구입니다. 원하는 경우 localhost 브라우저 화면도 열 수 있습니다.

- 계정별 사용 한도, 잔여율, 다음 한도 갱신 시각
- 사용 가능한 초기화권 개수와 제공되는 만료일
- 계정별 오류 분리, 중복 계정 정리, 주기적 자동 조회
- 기존 Codex 앱의 로그인 상태를 바꾸지 않는 별도 계정 연결
- Python 표준 라이브러리만 사용: 추가 Python 패키지, 데이터베이스, 외부 호스팅 불필요

초기화권은 조회만 합니다. 모델 작업을 생성하거나 초기화권을 사용하지 않습니다.

## 실행 환경

| 항목 | 요구 사항 |
|---|---|
| 운영체제 | Linux 또는 macOS. Windows에서는 WSL의 Linux 환경 사용 |
| Python | 3.11 이상 |
| Codex CLI | `account/rateLimits/read`를 지원하는 버전. 0.159.0에서 실제 사용량·초기화권 조회 검증 |
| 시간대 데이터 | 시스템 `tzdata`. 출력 시각은 한국 시간 |
| GitHub 접근 | 이 비공개 저장소를 읽을 수 있는 계정 |

현재 자동 테스트와 설치 검증은 Linux/Python 3.12에서 수행했습니다. macOS는 지원 대상으로 작성했으며 별도 기기 검증은 아직 하지 않았습니다. Windows 네이티브 실행은 지원하지 않습니다.

먼저 확인하세요:

```bash
python3 --version
codex --version
```

Codex CLI가 없다면 [공식 설치 안내](https://learn.chatgpt.com/docs/codex/cli)를 따라 설치하세요. 이 도구의 설치 프로그램은 Codex CLI나 다른 패키지를 자동으로 다운로드하지 않습니다.

## 다른 컴퓨터에 설치하기

GitHub CLI가 있고 해당 GitHub 계정으로 로그인되어 있다면:

```bash
gh repo clone DiosAmor/codex-usage
cd codex-usage
./install.sh
codex-usage --demo
```

GitHub CLI 로그인이 필요하면 먼저 `gh auth login`을 실행하세요. SSH 인증이 준비되어 있으면 다음 방법도 가능합니다:

```bash
git clone git@github.com:DiosAmor/codex-usage.git
cd codex-usage
./install.sh
```

관리자 권한은 필요 없습니다. 프로그램은 `~/.local/share/codex-usage/`, 명령은 `~/.local/bin/codex-usage`에 설치됩니다. 로그인 정보나 Git 파일은 복사하지 않습니다.

`codex-usage: command not found`가 나오면 설치 결과에 표시된 PATH 안내를 적용하세요. 기본 설치에서는 `~/.bashrc` 또는 `~/.zshrc`에 다음 줄을 넣고 터미널을 다시 열면 됩니다:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

바로 실행하려면 `~/.local/bin/codex-usage --demo`를 사용할 수 있습니다. 다른 설치 위치를 원하면 `./install.sh --prefix /원하는/폴더`를 지정하세요. 같은 위치에 다시 설치하면 업데이트됩니다.

설치 없이 checkout에서 사용할 수도 있습니다:

```bash
./codex-usage --demo
./codex-usage
```

`./run`은 같은 기능의 호환 실행 파일입니다.

## 계정 연결하기

현재 컴퓨터에 저장된 Codex 로그인과 `codex-switch`의 `workspace-logins` 계정을 자동으로 발견합니다. 같은 계정/워크스페이스는 한 번만 표시하고, 같은 이메일의 다른 워크스페이스는 별도 항목으로 표시합니다.

다른 계정이나 만료된 저장 로그인을 연결하려면 계정마다 한 번씩 실행하세요:

```bash
codex-usage login "개인 계정 1"
codex-usage login "개인 계정 2"
codex-usage login "업무 계정"
```

1. 터미널에 나온 공식 OpenAI 로그인 주소를 브라우저에서 엽니다.
2. 표시된 일회용 코드를 입력하고 연결하려는 계정으로 로그인합니다.
3. 터미널에 등록 완료가 나올 때까지 명령을 실행해 둡니다.
4. `codex-usage`를 실행하면 등록된 계정을 함께 조회합니다.

다음 계정을 추가할 때는 브라우저에서 선택한 계정이 맞는지 확인하세요. 같은 계정/워크스페이스로 다시 로그인하면 해당 항목을 새 로그인으로 교체합니다.

**다른 컴퓨터에서는 각 계정을 새로 연결해야 합니다.** GitHub에는 프로그램만 보관하며 계정 로그인 정보를 동기화하지 않습니다. 한 번 등록한 로그인은 계속 사용할 수 있지만, 서비스에서 만료하거나 취소하면 다시 연결해야 합니다.

## 사용 명령

| 명령 | 기능 |
|---|---|
| `codex-usage` 또는 `codex-usage status` | 전체 계정의 사용량을 한 번 조회 |
| `codex-usage watch` | 기본 5분 간격으로 계속 조회 |
| `codex-usage watch --interval 120` | 2분 간격으로 조회. 최소 60초 |
| `codex-usage list` | 네트워크 조회 없이 계정 목록 확인 |
| `codex-usage login "계정 이름"` | 계정 연결 또는 재로그인 |
| `codex-usage --json` | 인증 토큰을 제외한 JSON 결과 |
| `codex-usage serve --open-browser` | localhost 브라우저 화면 실행 |
| `codex-usage --demo` | 실제 계정을 읽지 않는 예시 출력 |
| `codex-usage --version` | 프로그램 버전 확인 |
| `codex-usage --help` | 전체 옵션 확인 |

`watch`와 `serve`는 실행해 둔 동안 자동 조회합니다. `Ctrl+C`로 종료하면 조회도 멈춥니다. 터미널 화면은 새 값으로 갱신되고, 파일이나 파이프로 연결한 출력은 순서대로 기록됩니다. JSON이나 출력 파일에는 계정 이름·이메일·사용량이 포함되므로 공유 전에 내용을 확인하세요.

출력 예시 — 아래는 실제 계정 값이 아닙니다:

```text
개인 계정 1 · person@example.com · plus
  [codex]
    5시간: 73% 남음 · 갱신 10/13 13:00 KST
    주간: 54% 남음 · 갱신 10/16 05:00 KST
  초기화권: 2개
    초기화권 · 만료 10/19 11:00 KST
```

일회 조회는 모든 계정이 성공하면 종료 코드 0, 일부 계정이 실패하면 가능한 결과를 함께 표시하고 1을 반환합니다. 사용자 중단은 130입니다. `watch`는 일부 계정에서 오류가 나도 다음 주기에 다시 시도합니다.

## 브라우저 화면

```bash
codex-usage serve --open-browser
```

기본 주소는 `http://127.0.0.1:8765`입니다. 같은 저장소의 계정을 표시하며 계정 추가, 연결 해제, 수동 새로고침을 지원합니다. localhost에만 연결되고 로그인 파일은 웹으로 제공하지 않습니다. 공식 페이지 링크는 브라우저에서 현재 로그인한 계정으로 열립니다.

다른 포트 또는 예시 화면:

```bash
codex-usage serve --port 8767
codex-usage serve --demo --port 8766
```

예시 모드에서는 계정 등록과 연결 해제를 지원하지 않습니다.

## 데이터 저장 위치와 업데이트

로그인 정보는 설치 코드와 분리해서 사용자 폴더에 저장합니다:

| 환경 | 기본 위치 |
|---|---|
| Linux / WSL | `~/.local/state/codex-usage/` |
| macOS | `~/Library/Application Support/codex-usage/` |
| `XDG_STATE_HOME` 지정 | `$XDG_STATE_HOME/codex-usage/` |

우선순위는 `--data-dir`, `CODEX_USAGE_DATA_DIR`, 기존 checkout의 `.private/accounts.json` 호환 위치, 일반 기본 위치 순입니다. 초기 버전의 `.private/`에서 이미 등록한 계정은 그 checkout에서 계속 사용할 수 있습니다. 설치 명령은 이 로그인 폴더를 복사하지 않습니다. 필요한 경우 `--data-dir`로 기존 위치를 지정하세요.

```bash
codex-usage --data-dir /원하는/폴더
codex-usage login "계정" --data-dir /원하는/폴더
```

항상 같은 폴더를 쓰려면 `CODEX_USAGE_DATA_DIR`을 지정할 수 있습니다. 조회·계정 등록·자동 조회·브라우저 화면 모두 같은 저장 위치를 사용해야 합니다.

프로그램을 업데이트하려면 실행 중인 `watch`나 `serve`를 종료한 뒤, 처음 내려받았던 저장소 폴더에서 실행하세요:

```bash
git pull --ff-only
./install.sh
codex-usage --version
```

기존 명령과 프로그램 파일을 갱신하며 로그인 정보를 삭제하거나 복사하지 않습니다. 사용자 지정 prefix로 설치했다면 업데이트 때도 같은 `--prefix`를 사용하세요.

## 로그인 처리와 제공되지 않는 값

- 기존 `~/.codex/auth.json`과 `~/.codex/workspace-logins/*.json`은 읽기만 합니다. `CODEX_HOME`이 있으면 그 위치를 사용합니다. 접근 토큰을 별도 프로세스에 전달하며 기존 로그인 파일이나 갱신 토큰을 변경하지 않습니다.
- `login`으로 연결한 계정은 전용 Codex 폴더에서 인증하고 자체 토큰 갱신을 처리합니다. 폴더 권한은 `700`, 로그인 파일과 계정 목록은 `600`입니다.
- 잔여율은 서버의 `usedPercent`를 `100 - usedPercent`로 변환합니다. 제공된 모든 한도와 기간을 표시하며, 한도가 없다고 무제한으로 추정하지 않습니다.
- 초기화권 개수는 `rateLimitResetCredits.availableCount`가 기준입니다. 상세 목록이 일부만 제공되면 그 목록 길이로 개수를 계산하지 않습니다.
- 값이 없으면 `확인 불가`, 실제 개수가 0이면 `0개`입니다. 만료일이 제공되지 않을 수도 있습니다.
- 갱신 시각에 도달했다고 잔여량을 임의로 100%로 바꾸지 않습니다. 다음 서버 조회 결과를 표시합니다.
- 계속 실행하는 동안 조회가 실패하면 마지막 성공 값과 그 시각을 표시합니다. 프로그램을 새로 시작하면 다시 조회합니다.
- API 키 로그인과 keyring에만 저장된 기존 계정은 자동 발견 대상이 아닙니다. ChatGPT 계정을 `login`으로 별도 연결하세요.
- 계정 조회를 여러 프로세스에서 실행할 수 있지만, 계정 추가·해제는 한 프로그램에서 순서대로 진행하세요.

## 문제 해결

| 증상 | 확인할 내용 |
|---|---|
| 명령을 찾을 수 없음 | `~/.local/bin` PATH 설정 또는 전체 경로 실행 |
| Codex CLI를 실행할 수 없음 | `codex --version`. 필요하면 `--codex /전체/경로/codex` 사용 |
| 저장된 로그인 만료 | `codex-usage login "계정 이름"`으로 새로 연결 |
| 기기 코드 발급 실패 | 네트워크, Codex CLI 버전, 계정의 기기 코드 로그인 허용 설정 |
| 초기화권 확인 불가 | CLI가 해당 필드를 지원하는지 확인. 서버가 미제공한 값은 조회할 수 없음 |
| 저장소 권한 오류 | 현재 사용자가 쓸 수 있는 `--data-dir` 선택 |
| `Asia/Seoul` 시간대 없음 | 시스템 `tzdata` 설치. Ubuntu/Debian은 `sudo apt install tzdata` |
| 포트 사용 불가 | `serve --port 8767`처럼 다른 포트 사용 |
| Windows에서 실행 실패 | WSL에서 Python, Git, Codex CLI와 이 도구를 설치해 실행 |

## 개발과 검증

구조는 `cli.py`(명령과 출력), `backend.py`(계정과 조회), `protocol.py`(Codex 통신), `storage.py`(저장 위치), `server.py`/`static/`(선택형 웹 화면)로 나뉩니다. `install.py`는 지정된 프로그램 파일만 설치합니다. 테스트와 개인 로그인 자료는 설치 대상이 아닙니다.

```bash
python3 -m unittest discover -s tests -v
```

가짜 계정과 Codex 프로세스로 인증 보존, 중복 처리, 오류 분리, 초기화권 값 처리, 프로세스 종료, 로컬 HTTP 접근 제한과 설치·업데이트를 검사합니다. 테스트에서 실제 로그인이나 초기화권 사용을 수행하지 않습니다.

사용량 조회는 [공식 Codex App Server](https://learn.chatgpt.com/docs/app-server)의 `account/rateLimits/read`를 사용합니다. 한도와 갱신 시각은 [공식 사용량 안내](https://learn.chatgpt.com/docs/pricing)와 서비스 응답을 따릅니다. OpenAI의 공식 제품이 아닌 개인용 보조 도구입니다.
