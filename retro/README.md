# retro — 로컬 활동 로그 수집기 + 일간 회고 페이지

## 설치 (한 줄, Python 설정 불필요)

```bash
curl -fsSL https://raw.githubusercontent.com/ffogtive/ai-market/claude/eloquent-brahmagupta-2mkg25/retro/install.sh | sh
retro                                              # 오늘의 회고 → ~/Retro/daily-날짜.html 자동으로 열림
retro add-host "ssh -p 10024 user@서버"            # 서버도 매번 함께 수집 (서버엔 아무것도 설치 안 함)
retro --date 2026-09-23                            # 지난 날짜
retro doctor                                       # 출처별 진단
retro sources                                      # 무엇을 읽는지, 꺼진 소스
retro off chrome / retro on chrome                 # 소스 끄기·켜기 (claude codex git chrome extension)
retro schedule --at 22:00                          # 매일 자동 실행 (맥), 끄기: retro unschedule
retro update                                       # 최신 버전으로
```

- Python: uv가 3.12를 따로 받아 씀 → 맥의 Python 3.9, 서버의 3.6 무관
- 요약: `ANTHROPIC_API_KEY`가 있으면 API, 없으면 설치된 **Claude Code(`claude -p`)**로 → API 키 불필요. 둘 다 없으면 숫자만
- `--llm none`: 로그 원문을 외부로 보내지 않음

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
| Codex | `~/.codex/sessions/**`, `~/.codex/history.jsonl` | 중복 제거 |
| git | 최근 30일 Claude Code·Codex 세션의 작업 폴더(`cwd`)가 속한 레포 | 홈 폴더를 훑지 않음. 추가 폴더 스캔은 `--git-root DIR`(깊이 `--git-depth`). 봇 커밋 제외, 작성자 제한은 `--git-author` |
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
