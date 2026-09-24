# retro — 로컬 활동 로그 수집기 + 일간 회고 페이지

## 설치 (한 줄, Python 설정 불필요)

```bash
curl -fsSL https://raw.githubusercontent.com/ffogtive/ai-market/claude/eloquent-brahmagupta-2mkg25/retro/install.sh | sh
retro                                              # 오늘의 회고 → ~/Retro/daily-날짜.html 자동으로 열림
retro add-host "ssh -p 10024 user@서버"            # 서버도 매번 함께 수집 (서버엔 아무것도 설치 안 함)
retro --date 2026-09-23                            # 지난 날짜
retro week                                         # 이번 주(월–일) 회고 → ~/Retro/weekly-월요일날짜.html (--date로 지난 주)
retro --refresh                                    # 요약 새로 받기 (기본은 로그가 그대로면 저장된 요약 재사용). retro week --refresh도 같음
open ~/Retro/index.html                            # 모든 회고 목록(최신 주부터). 실행할 때마다 새로 만듦
retro doctor                                       # 출처별 진단
retro sources                                      # 무엇을 읽는지, 꺼진 소스
retro off chrome / retro on chrome                 # 소스 끄기·켜기 (claude codex git chrome extension)
retro add-repo ~/code                              # AI 세션 없이 커밋한 저장소도 수집 (저장소 또는 상위 폴더). 목록: retro repos, 빼기: retro remove-repo
retro schedule --at 22:00                          # 매일 자동 실행 (맥), 끄기: retro unschedule
retro app                                          # 명령어 없이 브라우저 화면에서: 회고 만들기 버튼 + 회고 쓰기(KPT·내일 첫 할 일) + 설정(소스·서버·저장소·자동 실행). 끄기: Ctrl+C
retro update                                       # 최신 버전으로
```

- Python: uv가 3.12를 따로 받아 씀 → 맥의 Python 3.9, 서버의 3.6 무관
- 요약: `ANTHROPIC_API_KEY`가 있으면 API, 없으면 설치된 **Claude Code(`claude -p`)**로 → API 키 불필요. 둘 다 없으면 숫자만
- `--llm none`: 로그 원문을 외부로 보내지 않음
- 요약 저장: `~/Retro/summary-daily-날짜.json`·`summary-weekly-월요일날짜.json`. 로그가 그대로면 다시 요약하지 않음(즉시, 사용량 0). 새 요약이 실패하면 이전 요약을 보여주고 페이지 맨 아래에 이유 표시
- 주간 요약은 그 주에 저장된 일간 요약을 먼저 읽고 원문은 하루 20줄만 봄 → 짧고 정확
- 페이지 맨 위 `← 이전 날 · 주간 보기 · 목록 · 다음 날 →` (주간: `← 지난주 · 목록 · 다음 주 →`), 있는 페이지만 링크. 예전에 만든 페이지는 다시 만들 때 생김
- 일간 페이지 탭 **전체 · 오전(00:00–11:59) · 오후(12:00–23:59)**. `daily-날짜.html#am`·`#pm`으로 바로 열림(스크립트 없이 CSS만, 파일로 열어도 동작). 숫자·흐름·문구 신호는 구간마다 따로 계산하고, 요약은 하루 1번 — "한 일"만 시각으로 나눠 보여줌(정오를 걸친 일은 양쪽, 시각 없는 일은 전체에만)
- 첫 화면: 관측 범위(소스·기기별 건수, 꺼진 소스) → 한 줄 요약·키워드 → 숫자(연속 활동 구간, 기록상 프로젝트 변경, 내 지시 1건당 자동 실행) → 한 일(완료/요청함 + 근거 시각) → 결정·막힘(재시도 신호) → 내일 첫 할 일 → KPT(AI 초안 + 직접 작성 칸). **펼쳐보기**: 학습 후보, 흐름(시간대 막대·연속 활동 띠·프로젝트 타임라인), 프롬프트 문구 신호(자동 분류·추정, 유사 표현 후보, 자동화·코칭 검토 후보), AI 사용(`나 → Codex n건 → (자동 실행) Claude Code m건`, 도구·기기별), 탐색(작업 직전 10분)
- 주간 페이지: 지난주 대비 ▲▼(`retro week`가 지난주 기록도 함께 모음, 관측 소스가 다르면 경고), 요일×시간대 히트맵(요일 → 일간 페이지), 요일별 프로젝트 막대, 날짜별 표의 오전/오후 지시 수 → 그 탭
- **회고 쓰기** (`retro app` → 홈의 "✍️ 오늘 회고 쓰기" 또는 목록의 각 페이지 옆 "회고 쓰기"): KPT 세 칸(AI 초안이 흐린 글씨로 보임), 오늘 가장 의미 있었던 일, 내일 첫 할 일(기본값 = AI 초안)을 쓰고 저장 → 페이지의 "(직접 작성)" 자리에 바로 들어감. 요약은 다시 하지 않음(AI 호출 없음). 저장 위치 `~/Retro/notes-날짜.json`(주간은 `notes-weekly-월요일날짜.json`), 이 기기에만 있고 AI 요약에 보내지 않음. 페이지를 파일로 열어도 쓴 내용이 보이고, 고치는 곳은 app("수정은 retro app에서")
- **다음 날 확인**: 다음 날 회고 쓰기 화면과 페이지에 "어제 정한 첫 할 일 / 어제의 Try"가 나오고 **완료 · 이어가기 · 취소**를 누름(페이지에는 고른 상태 또는 "아직 확인 안 함"). 이어가기는 그 항목을 오늘의 "내일 첫 할 일"(Try는 Try 칸) 기본값으로 가져옴. 어제 쓴 게 없으면 7일 안의 마지막 회고를 날짜와 함께 보여줌. 주간 페이지 "이번 주 첫 할 일"에 날마다 정한 첫 할 일과 상태, 완료·이어가기·취소·아직 확인 안 함 개수(점수 없음)
- `render.py --llm cached`: 저장된 요약만 쓰고 새로 요약하지 않음(네트워크 없음). 저장된 요약이 없으면 숫자만
- 숫자·분석은 모두 이 기기에서 계산(`retro/analyze.py`). 점수·평가가 아니라 기록에서 보이는 것만 중립적으로 적음. `--llm none`이어도 목록(`index.html`)에 지시·커밋 수가 나옴

---


가설: Claude Code 대화, Codex 대화, git 커밋, YouTube 시청 기록을 합치면 "무엇을 만들었나"까지 하루가 복원된다.

## 실행 (맥)

```bash
python3 retro/collect.py --days 7
# YouTube 포함 (Takeout 파일 경로)
python3 retro/collect.py --days 7 --youtube ~/Downloads/Takeout/YouTube*/history/watch-history.json
```

0건이 나오면 먼저 진단:

```bash
python3 retro/collect.py --doctor   # 각 출처를 어디서 찾는지, 파일이 몇 개인지 출력
```

결과: `retro_out/timeline.md` (날짜별 타임라인), `retro_out/events.jsonl`

| 출처 | 읽는 위치 | 비고 |
|---|---|---|
| Claude Code | `~/.claude/projects/*/*.jsonl` | 내가 입력한 프롬프트만 (시스템 메시지 제외) |
| Codex | `~/.codex/sessions/**`, `~/.codex/archived_sessions/`, `~/.codex/history.jsonl` | 중복 제거 |
| git | 최근 30일 Claude Code·Codex 세션의 작업 폴더(`cwd`)가 속한 레포 | 읽기 전에 `git fetch`로 클라우드 세션·다른 기기에서 푸시한 커밋도 가져옴(`--no-fetch`로 끔). 홈 폴더를 훑지 않음. AI 없이 커밋한 저장소는 `retro add-repo DIR`로 추가(저장소 또는 상위 폴더, 깊이 4). 봇 커밋 제외, 작성자 제한은 `--git-author` |
| Chrome | `~/Library/Application Support/Google/Chrome/*/History` | 로컬 방문 기록 (Takeout 불필요). 끄려면 `--no-chrome` |
| YouTube | Takeout `watch-history.json`(권장) 또는 `.html` | 광고 시청 기록 제외 |

## YouTube 기록 받기

1. https://takeout.google.com → "모두 선택 해제" → **YouTube 및 YouTube Music**만 선택
2. "모든 YouTube 데이터 포함됨" → **기록(history)** 만 체크
3. "여러 형식" → 기록 형식을 **JSON** 으로 변경 → 내보내기 (메일로 링크 옴)

## 개인정보

- 모든 처리는 로컬에서만. 네트워크 전송 없음.
- 각 항목은 200자로 잘림. `retro_out/`은 `.gitignore` 처리됨 — 공유 전 직접 확인할 것.

## 일간 회고 페이지 만들기

```bash
pip install anthropic            # 요약에 Claude API 사용 (ANTHROPIC_API_KEY 또는 `ant auth login`)
python3 retro/collect.py --days 7                       # 맥 → retro_out/events.jsonl
ssh -p <포트> <user>@<서버> 'python3 - --days 7 --no-chrome --stdout' < retro/collect.py > retro_out/gpu.jsonl
python3 retro/render.py --date 2026-09-23 retro_out/events.jsonl retro_out/gpu.jsonl
open retro_out/daily-2026-09-23.html
```

- 숫자(시간대별 활동, 지시 수, 커밋, 사이트)는 라벨로 계산하고, 요약·한 일·결정·막힌 것·내일·활동 유형은 Claude가 작성.
- `--no-llm`: API 없이 숫자만. 요약 실패(인증, 한도, 거절) 시에도 숫자 페이지는 생성됨.
- 요약은 `--out` 옆(또는 `--cache-dir`)에 JSON으로 저장·재사용, `--refresh`로 다시 요약. 같은 폴더에 `index.html`도 만듦.
- 라벨: `actor`(human / agent: `claude -p` 같은 헤드리스 실행), `source`, `project`, `host`.
- 해당 날짜의 **내가 입력한 로그 원문이 Anthropic API로 전송됨**. 민감하면 `--no-llm`.

## 브라우저 확장 (ChatGPT·Claude 대화 + 방문 기록)

앱(데스크톱·모바일)에서 한 대화도 계정에 동기화되므로, PC 브라우저에 로그인만 되어 있으면 함께 수집됩니다.

1. `chrome://extensions` → 우측 상단 **개발자 모드** 켜기
2. **압축해제된 확장 프로그램 로드** → `~/.local/share/retro/retro/extension` 선택 (Finder에서 ⌘⇧. 로 숨김 폴더 표시)
3. 툴바의 retro 아이콘 → **지금 수집** (이후 매시간 자동)

- 저장 위치: `~/Downloads/retro/browser-날짜.jsonl` → `retro`가 자동으로 합침 (이 파일이 있으면 Chrome DB는 읽지 않아 권한 문제도 사라짐)
- 수집 범위: 팝업에서 "제목 + 내 질문" / "제목만" 선택. 외부 서버 전송 없음
- ChatGPT·Claude는 공식 기록 API가 없어 웹앱 내부 엔드포인트를 사용 → 서비스 변경 시 해당 항목만 "실패"로 표시되고 나머지는 계속 수집
