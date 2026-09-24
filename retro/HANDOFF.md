# retro — 진행 상황 (2026-09-24)

하루 활동 로그를 모아 노션 스타일 일간·주간 **회고 페이지**를 만드는 제품. 직장인·학생 공용 엔진(수집 → 라벨 → 숫자 계산 → LLM 요약 → HTML).

## 지금까지 (모두 `claude/eloquent-brahmagupta-2mkg25`에 커밋·푸시됨)

| 부품 | 경로 | 상태 |
|---|---|---|
| 로컬 수집기 | `retro/collect.py` | ✅ 검증 (사용자 맥 + GPU 서버). Claude Code·Codex 프롬프트, git, YouTube(Takeout), Chrome 기록. `actor`(human/agent)·`host` 라벨. `--stdout`은 JSONL, `--doctor` 진단. Python 3.6+·구 git 호환 |
| 회고 페이지 생성 | `retro/render.py` | ✅ 실데이터 검증. 숫자는 로그로 계산, 서술은 Claude 요약(구조화 출력). 백엔드 auto: API 키 → `claude -p` CLI(키 불필요) → 숫자만 |
| 주간 회고 페이지 | `retro week`, `render.py --week` | ✅ (9/24) 월–일 한 주: 합계 KPI·요일별 막대·프로젝트·날짜별 표는 로그로 계산, 한 일·결정·반복된 문제·다음 주는 주간 요약(일간과 같은 백엔드·폴백). 프롬프트는 하루 60줄·줄당 120자로 압축. 합성 데이터 테스트(`retro/tests/test_weekly.py`), **실데이터 미검증** |
| 한 줄 설치 + 명령 | `retro/retro.py`, `retro/install.sh` | ✅ uv로 Python 무관. `retro` / `add-host` / `schedule`(macOS launchd) / `doctor`. 신규 환경 설치·실행·업데이트 검증 |
| 설정·실행 화면 | `retro app`, `retro/app.py` | ✅ (9/24) 터미널 없이 쓰는 로컬 웹 화면(와이어프레임). 홈: 만든 회고 목록 + "오늘/이번 주 회고 만들기"(진행 줄 실시간 표시). 설정: 소스 켜기·끄기, ssh 서버·저장소 추가/삭제, 자동 실행(맥), 요약 상태. 127.0.0.1 전용·실행마다 토큰·Host/Origin 검사. 테스트 `retro/tests/test_app.py`, **사용자 맥 미검증**, 디자인은 추후 |
| 브라우저 확장 | `retro/extension/` | ✅ 가짜 서버 e2e. ChatGPT·Claude 오늘 대화 + 방문 기록 → `~/Downloads/retro/browser-날짜.jsonl`. **실계정 미검증** |
| MCP 서버 PoC | `retro/mcp/` | ✅ 공식 SDK 클라이언트 e2e. `log_activity`/`get_today`/`forget`, Cloudflare Worker+KV. **미배포** |

## 검증된 핵심 발견
- AI로 일하는 사람의 작업 기록은 git이 아니라 **프롬프트**에 있다.
- 원본 로그의 절반이 잡음(에이전트 간 지시, 대화 이어하기 요약, 세션 중복) → 필터 반영됨.
- 데이터가 여러 기기에 흩어짐(맥·GPU 서버·클라우드) → 여러 호스트 병합 지원.
- `claude -p`로 API 키 없이 요약 가능(9/23 데이터로 품질 확인, 약 73초).

## 방향 결정 (사용자와 합의)
- **기본은 로컬 로그 읽기**(9/24 변경, CodexBar 방식): 토큰 비용 0, 누락 없음, 서버 없음. 모바일 대화도 claude.ai 기록으로 동기화되므로 확장이 보완. **MCP는 2순위**(설치 없는 입구가 필요해질 때).
- **클라우드 Claude Code 세션(D안, 9/24):** 대화 원문은 수집하지 않는다. 대신 클라우드 세션이 GitHub에 남기는 커밋을 `git fetch`로 가져와 "무엇을 했나"를 복원한다. 대화 원문이 꼭 필요해지면 MCP 커넥터(C안). 클라우드 VM에서 로그를 자동으로 내보내는 훅(A안)은 진행하지 않기로 함.
- 확장 프로그램은 이탈 우려 → 보조로만.
- **OS 키 입력·앱 감지: 접기.** 핵심 소스가 이미 내용까지 주므로 앱 이름은 중복. 시간만 주는 활동은 가성비 낮음.
- 학생: 대학생·수험생 우선(Anki·Notion·GoodNotes 백업·LMS). 국내 중고생 앱(열품타·인강)은 D등급.

## 다음 할 일 (우선순위)
1. **로컬 수집기를 기본 경로로 다듬기.** 9/24: git은 AI 세션 폴더에서만(홈 폴더 탐색 제거), `retro sources/off/on`, AI 없이 커밋한 저장소용 `retro add-repo` 추가. 남은 것: 사용자 맥에서 실사용 확인(`retro update` 후 `retro sources`, `retro`), 확장 실계정 검증.
2. (2순위) **MCP 커넥터를 실제 Claude에 붙이기.** 사용자 맥에서 `retro/mcp/deploy.sh` 한 번 실행(로그인→KV→배포→키→스모크 테스트→URL 출력). 클라우드 세션은 `api.cloudflare.com`이 네트워크 정책에 막혀 있고 CF 토큰도 없어 배포 불가(9/24 확인). 출력 URL을 Claude "커스텀 커넥터 추가"에. 모바일 대화가 자동 기록되는지, 예약 작업(Claude Cowork)에서 과거 대화 검색+`log_activity`가 도는지 PoC.
3. 되면 OAuth·개인정보처리방침(`PRIVACY.md` 초안 있음, 끝의 '알려진 한계' 해결)·디렉터리 심사 준비(ChatGPT 앱 디렉터리 병행).

## 참고 (조사 결과는 대화 로그에)
- 학생 앱 데이터 접근성 등급표, AI 커넥터별 자동화 가능성(Claude만 조건부 완전자동) — 이 세션 대화에 정리됨.
