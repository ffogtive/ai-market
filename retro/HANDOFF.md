# retro — 진행 상황 (2026-09-24)

하루 활동 로그를 모아 노션 스타일 일간·주간 **회고 페이지**를 만드는 제품. 직장인·학생 공용 엔진(수집 → 라벨 → 숫자 계산 → LLM 요약 → HTML).

## 지금까지 (모두 `claude/eloquent-brahmagupta-2mkg25`에 커밋·푸시됨)

| 부품 | 경로 | 상태 |
|---|---|---|
| 로컬 수집기 | `retro/collect.py` | ✅ 검증 (사용자 맥 + GPU 서버). Claude Code·Codex 프롬프트, git, YouTube(Takeout), Chrome 기록. `actor`(human/agent)·`host` 라벨. `--stdout`은 JSONL, `--doctor` 진단. Python 3.6+·구 git 호환 |
| 회고 페이지 생성 | `retro/render.py` | ✅ 실데이터 검증. 숫자는 로그로 계산, 서술은 Claude 요약(구조화 출력). 백엔드 auto: API 키 → `claude -p` CLI(키 불필요) → 숫자만 |
| 한 줄 설치 + 명령 | `retro/retro.py`, `retro/install.sh` | ✅ uv로 Python 무관. `retro` / `add-host` / `schedule`(macOS launchd) / `doctor`. 신규 환경 설치·실행·업데이트 검증 |
| 브라우저 확장 | `retro/extension/` | ✅ 가짜 서버 e2e. ChatGPT·Claude 오늘 대화 + 방문 기록 → `~/Downloads/retro/browser-날짜.jsonl`. **실계정 미검증** |
| MCP 서버 PoC | `retro/mcp/` | ✅ 공식 SDK 클라이언트 e2e. `log_activity`/`get_today`, Cloudflare Worker+KV. **미배포** |

## 검증된 핵심 발견
- AI로 일하는 사람의 작업 기록은 git이 아니라 **프롬프트**에 있다.
- 원본 로그의 절반이 잡음(에이전트 간 지시, 대화 이어하기 요약, 세션 중복) → 필터 반영됨.
- 데이터가 여러 기기에 흩어짐(맥·GPU 서버·클라우드) → 여러 호스트 병합 지원.
- `claude -p`로 API 키 없이 요약 가능(9/23 데이터로 품질 확인, 약 73초).

## 방향 결정 (사용자와 합의)
- **입구는 MCP 커넥터**(설치 없이 연결 버튼, 모바일 포함, 대화 내용까지). 파워유저는 로컬 수집기.
- 확장 프로그램은 이탈 우려 → 보조로만.
- **OS 키 입력·앱 감지: 접기.** 핵심 소스가 이미 내용까지 주므로 앱 이름은 중복. 시간만 주는 활동은 가성비 낮음.
- 학생: 대학생·수험생 우선(Anki·Notion·GoodNotes 백업·LMS). 국내 중고생 앱(열품타·인강)은 D등급.

## 다음 할 일 (우선순위)
1. **MCP 커넥터를 실제 Claude에 붙이기.** 사용자 맥에서 `retro/mcp/deploy.sh` 한 번 실행(로그인→KV→배포→키→스모크 테스트→URL 출력). 클라우드 세션은 `api.cloudflare.com`이 네트워크 정책에 막혀 있고 CF 토큰도 없어 배포 불가(9/24 확인). 출력 URL을 Claude "커스텀 커넥터 추가"에. 모바일 대화가 자동 기록되는지, 예약 작업(Claude Cowork)에서 과거 대화 검색+`log_activity`가 도는지 PoC.
2. 되면 OAuth·개인정보처리방침·디렉터리 심사 준비(ChatGPT 앱 디렉터리 병행).
3. 브라우저 확장 실계정 검증.

## 참고 (조사 결과는 대화 로그에)
- 학생 앱 데이터 접근성 등급표, AI 커넥터별 자동화 가능성(Claude만 조건부 완전자동) — 이 세션 대화에 정리됨.
