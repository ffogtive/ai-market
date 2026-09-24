# retro — 로컬 활동 로그 수집기

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
| git | `--git-root`(기본 `~`) 아래 깊이 4까지의 레포 | 기본은 global `user.email` 커밋만. 전체는 `--git-author ''` |
| Chrome | `~/Library/Application Support/Google/Chrome/*/History` | 로컬 방문 기록 (Takeout 불필요). 끄려면 `--no-chrome` |
| YouTube | Takeout `watch-history.json`(권장) 또는 `.html` | 광고 시청 기록 제외 |

## YouTube 기록 받기

1. https://takeout.google.com → "모두 선택 해제" → **YouTube 및 YouTube Music**만 선택
2. "모든 YouTube 데이터 포함됨" → **기록(history)** 만 체크
3. "여러 형식" → 기록 형식을 **JSON** 으로 변경 → 내보내기 (메일로 링크 옴)

## 개인정보

- 모든 처리는 로컬에서만. 네트워크 전송 없음.
- 각 항목은 200자로 잘림. `retro_out/`은 `.gitignore` 처리됨 — 공유 전 직접 확인할 것.
