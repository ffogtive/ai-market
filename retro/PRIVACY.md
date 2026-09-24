# retro 개인정보 처리 (초안)

> 초안입니다. 코드가 실제로 하는 일을 그대로 적었고, 디렉터리 심사용 개인정보처리방침의 뼈대로 씁니다. 코드가 바뀌면 이 표도 같이 바꿉니다.

## 한눈에
- **로컬 수집기·브라우저 확장은 retro 서버로 아무것도 보내지 않습니다.** 결과는 내 컴퓨터에만 남습니다.
- **MCP 커넥터만 서버(Cloudflare)에 저장**하며, 저장되는 건 대화 원문이 아니라 AI가 쓴 한 줄 요약입니다.
- 비밀번호·API 키를 만들거나 저장하지 않습니다. 이미 로그인된 CLI·브라우저 세션을 그대로 씁니다.
- 텔레메트리·분석·광고 추적이 없습니다.

## 1. MCP 커넥터 (`mcp/`, Cloudflare Worker + KV)

| 항목 | 내용 |
|---|---|
| 받는 데이터 | AI가 `log_activity`로 보낸 `app`, `topic`(80자), `did`(300자), `kind`, `decisions`(최대 5개×160자). 시각은 서버가 기록 |
| 받지 않는 것 | 대화 원문, 파일, 계정 정보, IP 기반 프로필 |
| 저장 위치 | Cloudflare KV, 키 `log:<사용자키>:<날짜>` |
| 보관 기간 | 마지막 기록 후 400일 뒤 자동 만료 |
| 삭제 | AI에게 "retro 기록 지워줘"라고 말하면 `forget` 도구 실행: 방금 기록(`last`), 그날 전체(`day`), 전부(`all`). 즉시 삭제 |
| 접근 | URL 속 사용자 키(무작위 24바이트)를 아는 사람만. 회고 페이지 `/p/<키>/<날짜>`도 같은 키 |
| AI에 주는 지시 | "잡담은 기록하지 말 것, 비밀번호·비밀·타인의 개인정보를 넣지 말 것" |

## 2. 로컬 수집기 (`collect.py`, `retro.py`)

| 소스 | 읽는 경로 | 비고 |
|---|---|---|
| Claude Code | `~/.claude/projects/*/*.jsonl` | 읽기 전용 |
| Codex CLI | `~/.codex/sessions/**/*.jsonl`, `~/.codex/archived_sessions/*.jsonl`, `~/.codex/history.jsonl` | 읽기 전용. `auth.json`은 읽지 않음 |
| git | 최근 30일 Claude Code·Codex 세션에 기록된 작업 폴더가 속한 레포에서 `git fetch`(그 레포의 원격에서 읽기만, 비밀번호 입력 없이) 후 `git log` | 홈 폴더를 훑지 않음. 세션 파일은 첫 `cwd`까지만 읽음. 사용자가 `retro add-repo`로 지정한 폴더는 그 안만 탐색 |
| Chrome 방문 기록 | Chrome 프로필의 `History` 파일 **복사본** | 원본을 잠그지 않도록 임시 폴더에 복사해 읽고 지움 |
| YouTube | 사용자가 지정한 Google Takeout 파일(`--youtube`) | 지정할 때만 |
| 다른 기기 | `retro add-host`로 등록한 ssh 호스트에서 같은 수집기 실행 | 사용자의 ssh 키 사용, 비밀번호 저장 안 함 |
| 설정 화면 (`retro app`) | 새로 읽는 것 없음. 위 명령들과 같은 설정 파일·`~/Retro/daily-*.html`·`weekly-*.html`만 사용 | 이 컴퓨터(127.0.0.1)에서만 열리는 임시 서버. 실행할 때마다 새 무작위 토큰이 있어야 열리고, 다른 사이트의 요청은 거부. 외부로 보내는 것 없음, Ctrl+C로 종료 |

- **쓰는 곳:** `~/Retro/`(events.jsonl, daily-날짜.html, retro.log), `~/.config/retro/config.json`(등록한 호스트), macOS 예약 실행 시 `~/Library/LaunchAgents/`.
- **요약(`render.py`):** 서술 요약이 필요할 때만 하루 타임라인을 **사용자 자신의 Anthropic 계정**으로 보냅니다(`ANTHROPIC_API_KEY` 또는 설치된 `claude -p`). retro 서버를 거치지 않습니다. `--llm none`이면 외부 전송 없이 숫자만 계산합니다.
- **소스 끄기:** `retro sources`로 무엇을 읽는지 보고, `retro off <소스>`로 끕니다(`~/.config/retro/config.json`에 저장, 등록한 서버에도 적용).
- **하지 않는 것:** Keychain·브라우저 쿠키 읽기, 키 입력·화면 기록.
- **권한:** Chrome 기록을 읽을 때 macOS가 터미널에 전체 디스크 접근 권한을 요구할 수 있습니다. 싫으면 `retro off chrome`(확장이 방문 기록을 대신 제공).

## 3. 브라우저 확장 (`extension/`)

| 항목 | 내용 |
|---|---|
| 권한 | `history`, `storage`, `alarms`, `downloads`, 호스트 `chatgpt.com`, `claude.ai` |
| 읽는 것 | 오늘 업데이트된 ChatGPT·Claude 대화(메시지당 최대 600자, 소스당 최대 40개), 브라우저 방문 기록 |
| 방법 | 이미 로그인된 브라우저 세션으로 각 서비스의 웹앱이 쓰는 JSON 엔드포인트를 호출(공식 API 아님) |
| 보내는 곳 | 없음. `~/Downloads/retro/browser-날짜.jsonl`로 저장 |

## 알려진 한계 (공개 전 해결)
- MCP 인증이 URL 속 키뿐입니다. URL이 유출되면 기록을 읽고 지울 수 있습니다 → OAuth로 교체 예정.
- 확장이 비공식 엔드포인트를 씁니다. 서비스 약관 검토가 필요합니다.
- 전체 일시정지 스위치는 없습니다(자동 실행은 `retro unschedule`로 끔).
