# retro — 진행 상황 (2026-09-24)

하루 활동 로그를 모아 노션 스타일 일간·주간 **회고 페이지**를 만드는 제품. 직장인·학생 공용 엔진(수집 → 라벨 → 숫자 계산 → LLM 요약 → HTML).

## 지금까지 (모두 `claude/eloquent-brahmagupta-2mkg25`에 커밋·푸시됨)

| 부품 | 경로 | 상태 |
|---|---|---|
| 로컬 수집기 | `retro/collect.py` | ✅ 검증 (사용자 맥 + GPU 서버). Claude Code·Codex 프롬프트, git, YouTube(Takeout), Chrome 기록. `actor`(human/agent)·`host` 라벨. `--stdout`은 JSONL, `--doctor` 진단. Python 3.6+·구 git 호환 |
| 회고 페이지 생성 | `retro/render.py` | ✅ 실데이터 검증. 숫자는 로그로 계산, 서술은 Claude 요약(구조화 출력). 백엔드 auto: API 키 → `claude -p` CLI(키 불필요) → 숫자만 |
| 주간 회고 페이지 | `retro week`, `render.py --week` | ✅ (9/24) 월–일 한 주: 합계 KPI·요일별 막대·프로젝트·날짜별 표는 로그로 계산, 한 일·결정·반복된 문제·다음 주는 주간 요약(일간과 같은 백엔드·폴백). 프롬프트는 하루 60줄·줄당 120자로 압축. 합성 데이터 테스트(`retro/tests/test_weekly.py`), **실데이터 미검증** |
| 요약 저장 · 페이지 이동 · 목록 | `render.py`, `retro --refresh`, `~/Retro/index.html` | ✅ (9/24) 요약을 `~/Retro/summary-*.json`에 저장, 로그가 그대로면 LLM 호출 없이 재사용(`--refresh`로 강제). 새 요약 실패 시 이전 요약 + 이유 표시. 주간 프롬프트는 저장된 일간 요약 + 하루 원문 20줄. 페이지 위 `← 이전 날 · 주간 보기 · 목록 · 다음 날 →`(있는 페이지만, 이후 생긴 페이지는 다음 실행 때 링크 갱신), 날짜별 표에서 일간으로 링크, `index.html` 최신 주부터. 합성 데이터 테스트(`retro/tests/test_cache_nav.py`), **실데이터 미검증** |
| 템플릿 v2 (탭·분석) | `retro/render.py`, `retro/analyze.py` | ✅ (9/24) 일간 **전체/오전/오후 탭**(CSS만, `daily-날짜.html#am`), 관측 범위, 연속 활동 구간·기록상 프로젝트 변경·내 지시 1건당 자동 실행, 흐름(연속 활동 띠·프로젝트 타임라인), 프롬프트 문구 신호(자동 분류·추정, 유사 표현 후보), AI 사용 위임 사슬(나 → 도구 → 자동 실행 도구·기기), 작업 직전 탐색, 학습 후보·내일 첫 할 일·KPT, 무거운 섹션은 펼쳐보기. 주간: 지난주 대비 ▲▼, 요일×시간대 히트맵, 오전/오후 링크. 요약 스키마 PLAN §5 + 근거(시각·도구)·완료/요청함, 호출 1번 유지. 외부 리뷰(중립 이름·관측 범위·가벼운 첫 화면·오래된 요약 표시) 반영. 테스트 `retro/tests/test_template_v2.py`, 임시 HOME e2e + 390/1200px 스크린샷, **실데이터·실제 LLM 요약 미검증**, 시각 디자인은 추후 |
| 한 줄 설치 + 명령 | `retro/retro.py`, `retro/install.sh` | ✅ uv로 Python 무관. `retro` / `add-host` / `schedule`(macOS launchd) / `doctor`. 신규 환경 설치·실행·업데이트 검증 |
| 설정·실행 화면 | `retro app`, `retro/app.py` | ✅ (9/24) 터미널 없이 쓰는 로컬 웹 화면(와이어프레임). 홈: 만든 회고 목록 + "오늘/이번 주 회고 만들기"(진행 줄 실시간 표시). 설정: 소스 켜기·끄기, ssh 서버·저장소 추가/삭제, 자동 실행(맥), 요약 상태. 127.0.0.1 전용·실행마다 토큰·Host/Origin 검사. 테스트 `retro/tests/test_app.py`, **사용자 맥 미검증**, 디자인은 추후 |
| 프로젝트 묶기 · 기록 삭제 · 전송 미리보기 | `retro alias`/`aliases`, `retro forget`, `retro --preview`, `retro app` 설정 | ✅ (9/24, PLAN §8 F차수) **묶기:** 설정 `aliases` {폴더·레포 이름: 프로젝트 이름}을 수집 직후 AI 지시·커밋에만 적용(웹사이트·YouTube 채널은 안 묶음, 원래 이름은 `project_raw`), 페이지에서 LLM 라벨보다 우선. 앱: 최근 기록에서 이름 고르기 + 풀기. **삭제:** `forget --date`는 그날 일간 페이지·요약·메모만, `--all`은 `~/Retro`의 retro 파일 전부(원본 기록·설정은 그대로), CLI y/N(`--yes`), 앱은 2단계 확인, 지운 뒤 목록·이동 링크·주간 페이지 링크 정리. **미리보기:** 보낼 기록 전부·고정 지시문·글자 수·보낼 곳·캐시 재사용 여부를 출력, LLM 호출·페이지 쓰기 없음(`~/Retro`도 안 건드림). 앱은 회고마다 "요약에 보내는 내용 보기"(마지막 events.jsonl 기준). 테스트 `retro/tests/test_alias_forget_preview.py`, **사용자 맥 미검증** |
| 회고 쓰기 · 다음 날 확인 | `retro/notes.py`, `retro app` → `/notes`, `render.py` | ✅ (9/24) PLAN §8 F의 첫 항목. 앱에서 KPT·오늘 가장 의미 있었던 일·내일 첫 할 일(기본값 AI 초안)을 `~/Retro/notes-날짜.json`에 저장(원자적 쓰기) → 페이지의 표시 부분만 제자리에서 다시 씀(LLM·로그 불필요, 예전 페이지는 `--llm cached`로 다시 만듦). 다음 날 "어제 정한 첫 할 일 / 어제의 Try"를 완료·이어가기·취소로 확인, 이어가기는 오늘 기본값으로. 주간 페이지에 이번 주 첫 할 일과 상태 개수. 파일로 열어도 보임("수정은 retro app에서"). 메모는 LLM에 보내지 않음. 테스트 `retro/tests/test_notes.py`, 임시 HOME + 헤드리스 Chromium 390px 확인, **사용자 맥 미검증** |
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
0. **템플릿 v2 실데이터 확인.** 사용자 맥에서 `retro update` 후 `retro --refresh`·`retro week --refresh` 한 번(요약 스키마가 바뀌어 새로 요약). 탭(`#am`/`#pm`), 펼쳐보기, 주간 ▲▼, 요약의 근거·"요청함" 표시가 맞는지 확인. 그다음 시각 디자인(사용자). 알려진 문제: 추가만 있는 커밋은 수집 단계에서 줄 수가 빠져 `+0 / −0줄`로 보임(`collect.py` shortstat 파싱).
1. **로컬 수집기를 기본 경로로 다듬기.** 9/24: git은 AI 세션 폴더에서만(홈 폴더 탐색 제거), `retro sources/off/on`, AI 없이 커밋한 저장소용 `retro add-repo` 추가. 남은 것: 사용자 맥에서 실사용 확인(`retro update` 후 `retro sources`, `retro`), 확장 실계정 검증.
2. (2순위) **MCP 커넥터를 실제 Claude에 붙이기.** 사용자 맥에서 `retro/mcp/deploy.sh` 한 번 실행(로그인→KV→배포→키→스모크 테스트→URL 출력). 클라우드 세션은 `api.cloudflare.com`이 네트워크 정책에 막혀 있고 CF 토큰도 없어 배포 불가(9/24 확인). 출력 URL을 Claude "커스텀 커넥터 추가"에. 모바일 대화가 자동 기록되는지, 예약 작업(Claude Cowork)에서 과거 대화 검색+`log_activity`가 도는지 PoC.
3. 되면 OAuth·개인정보처리방침(`PRIVACY.md` 초안 있음, 끝의 '알려진 한계' 해결)·디렉터리 심사 준비(ChatGPT 앱 디렉터리 병행).

## 참고 (조사 결과는 대화 로그에)
- 학생 앱 데이터 접근성 등급표, AI 커넥터별 자동화 가능성(Claude만 조건부 완전자동) — 이 세션 대화에 정리됨.
