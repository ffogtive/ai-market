#!/usr/bin/env python3
"""Render a Notion-style daily (or weekly) retrospective page from collected events.

  python3 retro/render.py --date 2026-09-23 retro_out/events.jsonl retro_out/gpu.jsonl
  python3 retro/render.py --week --date 2026-09-23 retro_out/events.jsonl   # Mon–Sun week of that day

Inputs are one or more events.jsonl files from collect.py (merge machines by
passing several). Numbers (hours, counts, commits) are computed here from the
labels, and the "how I worked" metrics by analyze.py; the narrative parts
(summary, keywords, done, decisions, blockers, TIL, tomorrow, KPT, prompt
coaching, automation ideas, activity mix) come from one Claude call per page.
--no-llm renders the numbers only.

The daily page has 전체 / 오전 / 오후 tabs (CSS only, so they work from file://;
daily-DATE.html#am opens the morning). Every number is computed per slice; the
summary is one call per day, and its "한 일" rows are split by their time.

The summary is saved next to the page (summary-daily-DATE.json /
summary-weekly-MONDAY.json) and reused while the logs stay the same, so a
re-render costs no LLM call; --refresh summarizes again anyway; --llm cached
never calls it (the saved summary, or numbers only). --preview prints
the exact text a summary would send (and to which backend) and stops: no call,
no page, no cache file. Each run also
rewrites index.html (every page, newest first) and the ← · → rows at the top
of the pages in that folder.

My own notes (notes-DATE.json in the same folder, written by `retro app`: KPT,
가장 의미 있었던 일, 내일 첫 할 일, the next day's 완료/이어가기/취소) fill the
"(직접 작성)" slots. They sit between <!--notes:…--> markers, so refresh_notes()
rewrites just those parts after a save — no logs, no LLM. Notes never go into a prompt.

Needs: pip install anthropic, and ANTHROPIC_API_KEY (or `ant auth login`).
"""
import argparse
import datetime as dt
import hashlib
import html
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze  # noqa: E402
import notes  # noqa: E402

MODEL = "claude-opus-5"
ACTIVITY_TYPES = ["기획", "제작", "QA·검수", "개발", "리서치", "소통", "행정", "개인"]
SERIES = [  # (key, label, css var) — stacking order in charts
    ("codex", "나 → Codex", "--c-codex"),
    ("claude", "나 → Claude Code", "--c-claude"),
    ("claude.ai", "나 → Claude 앱", "--c-claudeapp"),
    ("chatgpt", "나 → ChatGPT", "--c-chatgpt"),
    ("agent", "에이전트 위임", "--c-agent"),
    ("chrome", "웹 탐색", "--c-web"),
    ("git", "커밋", "--c-git"),
    ("other", "기타", "--c-other"),
]
SERIES_KEYS = {k for k, _, _ in SERIES}
AI_SOURCES = ("claude", "codex", "claude.ai", "chatgpt")  # places where the user talks to an AI
TOOL_NAMES = {"claude": "Claude Code", "codex": "Codex", "claude.ai": "Claude 앱", "chatgpt": "ChatGPT"}
WEEKDAYS = "월화수목금토일"


# ---------------------------------------------------------------- load
def load(paths):
    events = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue  # tolerate stderr noise captured with --stdout
                e = json.loads(line)
                e["ts"] = dt.datetime.fromisoformat(e["ts"])
                e.setdefault("actor", "human")
                events.append(e)
    events.sort(key=lambda e: e["ts"])
    return events


def series_key(e):
    if e["actor"] == "agent":
        return "agent"
    return e["source"] if e["source"] in SERIES_KEYS else "other"


# ---------------------------------------------------------------- numbers
def day_stats(events, top=6):
    human = [e for e in events if e["actor"] == "human"]
    prompts = [e for e in human if e["source"] in AI_SOURCES]
    commits = [e for e in events if e["source"] == "git"]
    add = sum(int(m) for e in commits for m in re.findall(r"\(\+(\d+)/", e["text"]))
    dele = sum(int(m) for e in commits for m in re.findall(r"/-(\d+)\)", e["text"]))
    hours = defaultdict(Counter)
    for e in events:
        hours[e["ts"].hour][series_key(e)] += 1
    sites = Counter(e["project"] for e in human if e["source"] == "chrome" and e["project"])
    people = Counter(e["project"] for e in human if e["source"] in ("slack", "gmail", "kakao") and e["project"])
    active = [e["ts"] for e in human if e["source"] != "git"]
    agent = [e for e in events if e["actor"] == "agent"]  # run by a program, not typed by me
    return {
        "prompts": len(prompts),
        "prompts_by_tool": Counter(e["source"] for e in prompts),
        "prompts_by": Counter((e["source"], e.get("host", "")) for e in prompts),
        "agent": len(agent),
        "agent_by_tool": Counter(e["source"] for e in agent),  # the tool that ran it, not who sent it
        "agent_by": Counter((e["source"], e.get("host", "")) for e in agent),
        "commits": len(commits), "add": add, "dele": dele,
        "web": sum(1 for e in human if e["source"] == "chrome"),
        "hours": hours,
        "projects": Counter(e["project"] for e in prompts + commits if e["project"]).most_common(top),
        "sites": sites.most_common(6),
        "people": people.most_common(8),
        "first": min(active) if active else None,
        "last": max(active) if active else None,
        "hosts": sorted({e.get("host", "") for e in events if e.get("host")}),
        "sources": sorted({e["source"] for e in events}),
    }


def week_days(day):
    """Mon–Sun of the week containing day."""
    monday = day - dt.timedelta(days=day.weekday())
    return [monday + dt.timedelta(days=i) for i in range(7)]


def split_days(events, days):
    by = {d: [] for d in days}
    for e in events:
        if e["ts"].date() in by:
            by[e["ts"].date()].append(e)
    return by


def week_stats(by_day):
    """day_stats over the whole week, plus one day_stats per day."""
    stats = day_stats([e for evs in by_day.values() for e in evs], top=10)
    stats["per_day"] = {d: day_stats(evs) for d, evs in by_day.items()}
    stats["active_days"] = sum(1 for evs in by_day.values() if any(e["actor"] == "human" for e in evs))
    return stats


# ---------------------------------------------------------------- LLM
def strict(props):
    """An object schema where every property is required (structured outputs)."""
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def string_prop(description):
    return {"type": "string", "description": description}


def string_list(description):
    return {"type": "array", "description": description, "items": {"type": "string"}}


def refs(when):
    """Evidence: which log lines back an item (empty when none)."""
    return {"type": "array", "description": "근거가 된 로그 줄 (없으면 빈 배열)", "items": strict({
        "time": string_prop(when), "source": string_prop("로그의 도구 이름 (claude, codex, git, chrome …)")})}


STATUS = {"type": "string", "enum": ["완료", "요청함"],
          "description": "완료: 커밋이나 완료 언급이 로그에 있음. 요청함: 지시만 있고 완료 기록은 없음"}


def v2_props(when):
    """PLAN-template.md §5 fields shared by the daily and weekly schemas; when: how a log time is written."""
    return {
        "keywords": string_list("대표 단어 정확히 3개"),
        "til": {"type": "array", "description": "학습 후보: 로그에서 드러난 새 사실·결론 0~3개",
                "items": strict({"text": string_prop("한 문장"), "evidence": refs(when)})},
        "kpt": strict({"keep": string_prop("계속할 것 1~2줄"), "problem": string_prop("문제였던 것 1~2줄"),
                       "try": string_prop("다음에 해볼 것 1~2줄")}),
        "prompt_coaching": {"type": "array", "description": "검토할 지시 0~1개. 로그의 실제 지시를 인용할 수 있을 때만",
                            "items": strict({"kind": {"type": "string", "enum": ["잘한 점", "고칠 점"]},
                                             "prompt": string_prop("로그의 지시 원문 인용"),
                                             "better": string_prop("더 나은 지시 문장 (잘한 점이면 빈 문자열)"),
                                             "why": string_prop("이유 한 문장"), "evidence": refs(when)})},
        "automation_ideas": {"type": "array", "description": "[반복 요청 Top]을 규칙·스킬·스크립트로 바꿀 검토 후보 0~3개",
                             "items": strict({"request": string_prop("반복된 요청"),
                                              "kind": {"type": "string", "enum": ["규칙", "스킬", "스크립트"]},
                                              "idea": string_prop("무엇을 만들면 되는지 한 문장")})},
    }


DAY_V2 = v2_props("HH:MM")
SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["one_line", "keywords", "done", "decisions", "blockers", "tomorrow", "first_task_tomorrow", "til",
                 "kpt", "prompt_coaching", "automation_ideas", "activity_mix", "project_labels"],
    "properties": {
        "one_line": {"type": "string", "description": "하루를 한 문장으로. 결과물 중심."},
        "keywords": DAY_V2["keywords"],
        "done": {"type": "array", "items": strict({
            "time": string_prop("HH:MM 또는 HH:MM–HH:MM"),
            "project": {"type": "string"},
            "result": string_prop("무엇을 만들었거나 끝냈는지. 과정이 아니라 결과. 요청함이면 무엇을 요청했는지"),
            "status": STATUS,
            "evidence": refs("HH:MM"),
        })},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "tomorrow": {"type": "array", "items": {"type": "string"}},
        "first_task_tomorrow": string_prop("내일 바로 시작할 수 있는 크기의 첫 할 일 1개 (없으면 빈 문자열)"),
        "til": DAY_V2["til"],
        "kpt": DAY_V2["kpt"],
        "prompt_coaching": DAY_V2["prompt_coaching"],
        "automation_ideas": DAY_V2["automation_ideas"],
        "activity_mix": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["type", "percent"],
            "properties": {"type": {"type": "string", "enum": ACTIVITY_TYPES}, "percent": {"type": "integer"}}}},
        "project_labels": {"type": "array", "description": "로그의 폴더·레포 이름을 사람이 읽는 프로젝트 이름으로", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["raw", "label"],
            "properties": {"raw": {"type": "string"}, "label": {"type": "string"}}}},
    },
}

HONESTY = """- 요청은 완료 증거가 아닙니다. 완료가 기록(커밋, 완료 언급)에 있을 때만 status를 "완료"로, 아니면 "요청함"으로 쓰세요.
- evidence에는 근거가 된 로그 줄의 시각과 도구를 적으세요. til(학습 후보)·prompt_coaching은 근거 줄이 있을 때만 쓰고, 없으면 빈 배열.
- prompt_coaching은 로그의 실제 지시를 인용할 수 있을 때 0~1개. automation_ideas는 [반복 요청 Top]만 보고 0~3개, 없으면 빈 배열.
- [자동 계산 지표]는 이미 센 값입니다. 다시 세거나 옮겨 적지 말고 해석의 근거로만 쓰세요. 점수·평가는 하지 마세요.
- 로그에 없는 사실을 만들지 마세요. 짧고 구체적인 한국어로 쓰세요."""

SYSTEM = """당신은 사용자의 하루 활동 로그를 읽고 일간 회고를 쓰는 비서입니다.
로그는 사용자가 AI 도구(Claude Code, Codex)에 직접 입력한 지시, git 커밋, 웹 방문, 메신저 기록입니다.
- '무엇을 했나'보다 '무엇이 결과로 남았나'를 적으세요. 지시 문장을 그대로 옮기지 말고 결과로 바꿔 쓰세요.
- done은 시간순 5~10개, decisions는 사용자가 명시적으로 확정·승인·방향 전환한 것만, blockers는 반복된 문제나 대기·비용 이슈.
- tomorrow는 로그에서 이어질 것이 분명한 일만. 추측으로 채우지 마세요. first_task_tomorrow는 바로 시작할 수 있는 크기의 1개.
- activity_mix의 percent 합은 100. keywords는 오늘을 대표하는 단어 3개, kpt는 Keep·Problem·Try 각 1~2줄 초안.
""" + HONESTY
DAILY_TASK = "표준 입력의 로그로 일간 회고를 작성하세요."

WEEK_V2 = v2_props("요일 HH:MM (예: 화 14:05)")
WEEK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["one_line", "keywords", "highlights", "decisions", "blockers", "next_week", "til", "kpt",
                 "prompt_coaching", "automation_ideas", "activity_mix", "project_labels"],
    "properties": {
        "one_line": {"type": "string", "description": "한 주를 한 문장으로. 결과물 중심."},
        "keywords": WEEK_V2["keywords"],
        "highlights": {"type": "array", "description": "이번 주 주요 결과 5~10개", "items": strict({
            "days": string_prop("요일 (예: 화, 수–목)"),
            "project": {"type": "string"},
            "result": string_prop("무엇을 만들었거나 끝냈는지. 과정이 아니라 결과. 요청함이면 무엇을 요청했는지"),
            "status": STATUS,
            "evidence": refs("요일 HH:MM (예: 화 14:05)"),
        })},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "description": "여러 날 반복된 문제·대기·비용 이슈와 패턴", "items": {"type": "string"}},
        "next_week": {"type": "array", "items": {"type": "string"}},
        "til": WEEK_V2["til"],
        "kpt": WEEK_V2["kpt"],
        "prompt_coaching": WEEK_V2["prompt_coaching"],
        "automation_ideas": WEEK_V2["automation_ideas"],
        "activity_mix": SUMMARY_SCHEMA["properties"]["activity_mix"],
        "project_labels": SUMMARY_SCHEMA["properties"]["project_labels"],
    },
}

WEEK_SYSTEM = """당신은 사용자의 한 주 활동 로그를 읽고 주간 회고를 쓰는 비서입니다.
로그는 사용자가 AI 도구(Claude Code, Codex 등)에 직접 입력한 지시와 git 커밋을 요일별로 줄인 것입니다. 긴 날은 하루 전체에서 고르게 뽑은 일부만 있고, 요일별 건수는 로그 앞의 숫자가 정확합니다.
- '무엇을 했나'보다 '무엇이 결과로 남았나'를 적으세요. 지시 문장을 그대로 옮기지 말고 결과로 바꿔 쓰세요.
- highlights는 요일순 5~10개, decisions는 사용자가 명시적으로 확정·승인·방향 전환한 것만, blockers는 여러 날 반복된 문제나 대기·비용 이슈, 되풀이되는 작업 패턴.
- next_week는 로그에서 이어질 것이 분명한 일만. 추측으로 채우지 마세요.
- activity_mix의 percent 합은 100. keywords는 이번 주를 대표하는 단어 3개, kpt는 주간 Keep·Problem·Try 각 1~2줄 초안.
""" + HONESTY
WEEK_TASK = "표준 입력의 로그로 주간 회고를 작성하세요."
WEEK_LINE_WIDTH = 120  # chars per log line in the weekly prompt
WEEK_DAY_LINES = 60  # log lines per day in the weekly prompt
WEEK_DAY_LINES_WITH_DAILY = 20  # fewer raw lines on days whose daily summary is in the prompt


def pick_backend(choice):
    """auto: API key → Claude Code CLI (uses the user's own subscription) → none."""
    if choice != "auto":
        return None if choice == "none" else choice
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "api"
    if shutil.which("claude"):
        return "claude"
    return None


def llm_facts(events):
    """analyze.facts_for_llm() for these events, then the top repeated requests (the input for automation_ideas)."""
    behavior = analyze.prompt_behavior(events)
    out = analyze.facts_for_llm(dict(behavior, top_repeated=[]), analyze.work_rhythm(events))
    if behavior["top_repeated"]:
        out += "\n[반복 요청 Top]\n" + "\n".join(f"- {clip(t, 60)} ×{c}" for t, c in behavior["top_repeated"])
    return out


def build_prompt(day, events, stats):
    lines = []
    for e in events:
        if e["actor"] != "human":
            continue
        proj = f"[{e['project']}] " if e["project"] else ""
        lines.append(f"{e['ts']:%H:%M} {e['source']} {proj}{e['text']}")
    facts = (f"날짜: {day}\n직접 입력한 AI 지시 {stats['prompts']}건, 에이전트 간 지시 {stats['agent']}건, "
             f"커밋 {stats['commits']}건(+{stats['add']}/-{stats['dele']}), 웹 방문 {stats['web']}건")
    return f"{facts}\n{llm_facts(events)}\n\n<log>\n" + "\n".join(lines) + "\n</log>"


def sample_evenly(items, n):
    """n items spread evenly over the list (first and last kept)."""
    if len(items) <= n:
        return list(items)
    if n <= 1:
        return list(items[:n])
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


def clip(line, width=WEEK_LINE_WIDTH):
    return line if len(line) <= width else line[:width - 1] + "…"


def compact_day(events, cap=WEEK_DAY_LINES, width=WEEK_LINE_WIDTH):
    """A day's own AI prompts + commits as short lines, for the weekly prompt.

    Consecutive near-duplicates ("continue", retries) are dropped; commits are
    kept first (they are results), then prompts sampled evenly across the day.
    Returns (lines, number of lines before sampling).
    """
    prompts, commits, last = [], [], None
    for e in events:
        if e["actor"] != "human" or e["source"] not in AI_SOURCES + ("git",):
            continue
        text = " ".join(e["text"].split())
        key = (e["source"], re.sub(r"\W+", "", text.lower())[:40])
        if key == last:
            continue
        last = key
        proj = f"[{e['project']}] " if e["project"] else ""
        line = clip(f"{e['ts']:%H:%M} {e['source']} {proj}{text}", width)
        (commits if e["source"] == "git" else prompts).append(line)
    total = len(prompts) + len(commits)
    if total > cap:
        commits = sample_evenly(commits, cap // 3)
        prompts = sample_evenly(prompts, cap - len(commits))
    return sorted(commits + prompts), total  # lines start with HH:MM


def daily_digest(label, summary, fresh):
    """A day's saved daily summary in a few lines: its one-liner and results."""
    out = [f"# {label} 일간 요약" + ("" if fresh else " (그 뒤 로그 일부 미반영)"),
           clip("한 줄: " + str(summary.get("one_line", "")))]
    for x in (summary.get("done") or [])[:10]:
        proj = f"[{x.get('project')}] " if x.get("project") else ""
        out.append(clip(f"- {x.get('time', '')} {proj}{x.get('result', '')}"))
    return out


def build_week_prompt(days, by_day, stats, dailies=None):
    """dailies: {day: (daily summary, fresh)} — those days get the summary first and fewer raw lines."""
    dailies = {d: v for d, v in (dailies or {}).items() if by_day.get(d)}
    facts = [f"기간: {days[0]} (월) – {days[-1]} (일)",
             f"이번 주 합계: 직접 입력한 AI 지시 {stats['prompts']}건, 에이전트 간 지시 {stats['agent']}건, "
             f"커밋 {stats['commits']}건(+{stats['add']}/-{stats['dele']}), 웹 방문 {stats['web']}건, "
             f"활동한 날 {stats['active_days']}일", "요일별:"]
    log = []
    for d in days:
        s = stats["per_day"][d]
        label = f"{d:%m/%d} ({WEEKDAYS[d.weekday()]})"
        if not by_day[d]:
            facts.append(f"- {label}: 기록 없음")
            continue
        span = f", {s['first']:%H:%M}–{s['last']:%H:%M}" if s["first"] else ""
        facts.append(f"- {label}: AI 지시 {s['prompts']}건, 에이전트 간 지시 {s['agent']}건, "
                     f"커밋 {s['commits']}건(+{s['add']}/-{s['dele']}), 웹 방문 {s['web']}건{span}")
        if d in dailies:
            log += daily_digest(label, *dailies[d])
        lines, total = compact_day(by_day[d], cap=WEEK_DAY_LINES_WITH_DAILY if d in dailies else WEEK_DAY_LINES)
        if lines:
            note = f"{total}줄 중 {len(lines)}줄, 고르게 뽑음" if total > len(lines) else f"{total}줄"
            log += [f"# {label} ({note})"] + lines
    if dailies:
        facts.append(f"일간 요약이 있는 날은 그 요약을 먼저 싣고, 원문은 하루 {WEEK_DAY_LINES_WITH_DAILY}줄까지만 고르게 뽑았습니다.")
    facts.append(llm_facts([e for d in days for e in by_day[d]]))
    return "\n".join(facts) + "\n\n<log>\n" + "\n".join(log) + "\n</log>"


def summarize_cli(prompt, system=SYSTEM, schema=SUMMARY_SCHEMA, task=DAILY_TASK, drop_api_key=False):
    """Headless Claude Code: no API key needed, no tools, nothing saved as a session.

    drop_api_key: the key already failed, so make the CLI use the user's own
    Claude login instead of that key (the CLI prefers an API key when one is set).
    """
    env = None
    if drop_api_key:
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    with tempfile.TemporaryDirectory() as cwd:  # keep project CLAUDE.md files out of the prompt
        res = subprocess.run(
            ["claude", "-p", system + "\n\n" + task,
             "--output-format", "json", "--json-schema", json.dumps(schema),
             "--tools", "", "--no-session-persistence"],
            input=prompt, capture_output=True, text=True, cwd=cwd, env=env, timeout=900)
    try:
        out = json.loads(res.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude CLI failed: {(res.stderr or res.stdout).strip()[:300]}")
    if out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"claude CLI returned no summary: {str(out.get('result'))[:300]}")
    return out["structured_output"]


def summarize(prompt, system=SYSTEM, schema=SUMMARY_SCHEMA):
    import anthropic

    client = anthropic.Anthropic()
    create = client.beta.messages.create
    params = dict(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        betas=["server-side-fallback-2026-07-01"],  # the header that goes with fallbacks="default"
        fallbacks="default",
        system=system,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    # Python 3.9 only gets the 0.x SDK, which may not know newer request fields; send those raw.
    known = inspect.signature(create).parameters
    extra = {k: params.pop(k) for k in list(params) if k not in known}
    if extra:
        params["extra_body"] = extra
    response = create(**params)
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined to summarize this log")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("summary was cut off (max_tokens)")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise RuntimeError("no text in the response")
    return json.loads(text)


# why the last get_summary() produced no summary, in words the user can act on (shown in the page footer)
LAST_FAILURE = ""
LAST_BACKEND = ""  # which backend wrote the last summary (api / claude); saved with it


def explain_failure(msg):
    m = msg.lower()
    if "oauth" in m or "not logged in" in m or "failed to authenticate" in m or "/login" in m:
        return "Claude 로그인이 만료됐습니다. 터미널에서 claude auth login 실행 후 retro를 다시 실행하세요."
    if "invalid api key" in m or "credentials" in m or "401" in m:
        return "API 키가 올바르지 않습니다. 키를 지우면(unset ANTHROPIC_API_KEY) 내 Claude 로그인으로 요약합니다."
    if "timed out" in m or "timeout" in m:
        return "요약 시간이 초과됐습니다. 잠시 후 retro를 다시 실행하세요."
    if "rate limit" in m or "429" in m or "usage limit" in m:
        return "사용량 한도에 걸렸습니다. 잠시 후 다시 실행하세요."
    return "요약 실패: " + msg.strip()[:120]


def get_summary(choice, prompt, system=SYSTEM, schema=SUMMARY_SCHEMA, task=DAILY_TASK):
    """auto: API → on any failure the claude CLI with the user's own login → None (numbers only)."""
    global LAST_FAILURE, LAST_BACKEND
    summary, why = None, ""
    api_failed = False
    backend = pick_backend(choice)
    if backend == "api":
        try:
            import anthropic
        except ImportError:
            anthropic = None
            print("anthropic SDK not installed (pip install anthropic) — rendering numbers only", file=sys.stderr)
        if anthropic:
            # the page is still useful without the summary, so every failure degrades to numbers only
            try:
                summary = summarize(prompt, system, schema)
            except anthropic.AuthenticationError:
                why = "no valid API credentials (set ANTHROPIC_API_KEY or run `ant auth login`)"
            except anthropic.RateLimitError:
                why = "rate limited — try again in a minute"
            except anthropic.APIConnectionError:
                why = "network error reaching the API"
            except anthropic.APIStatusError as e:
                why = f"API error {e.status_code}: {e.message}"
            except (RuntimeError, ValueError) as e:  # refusal / truncation / bad JSON
                why = f"summary unusable: {e}"
            except Exception as e:  # anything else the SDK throws must not cost the page
                why = f"summary failed: {e!r}"
            if why:
                print(why, file=sys.stderr)
        else:
            why = "anthropic SDK not installed"
        # auto: a stale or invalid API key shouldn't cost the summary when Claude Code is installed
        if not summary and choice == "auto" and shutil.which("claude"):
            print("· API 요약 실패 → claude CLI(내 Claude 로그인)로 재시도", file=sys.stderr)
            backend, api_failed = "claude", True
    if backend == "claude":
        try:
            summary = summarize_cli(prompt, system, schema, task, drop_api_key=api_failed)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
            why = str(e) or "timeout"
            print(f"summary failed ({e}) — rendering numbers only", file=sys.stderr)
    LAST_BACKEND = backend if summary else ""
    if summary:
        LAST_FAILURE = ""
    elif choice == "none":
        LAST_FAILURE = "요약 끔 (--llm none)"
    elif not backend:
        LAST_FAILURE = "요약 도구가 없습니다. Claude Code(claude)를 설치하고 로그인하거나 API 키를 설정하세요."
    else:
        LAST_FAILURE = explain_failure(why)
    if LAST_FAILURE and choice != "none":
        print(f"· {LAST_FAILURE}", file=sys.stderr)
    print(f"· 요약: {backend or '없음 (숫자만)'}{'' if summary or not backend else ' 실패'}", file=sys.stderr)
    return summary


# ---------------------------------------------------------------- summary cache
def prompt_hash(prompt, system, schema):
    """Same logs (and same instructions) → same hash → the saved summary still fits."""
    blob = json.dumps([system, schema, prompt], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def cache_path(cache_dir, kind, day):
    return os.path.join(cache_dir, f"summary-{kind}-{day}.json")


def read_entry(path):
    """The saved JSON dict (a summary and/or the page's numbers), or None."""
    try:
        with open(path, encoding="utf-8") as f:
            entry = json.load(f)
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) else None


def read_cache(path):
    """A saved entry that holds a summary (entries with only numbers don't count)."""
    entry = read_entry(path)
    return entry if entry and isinstance(entry.get("summary"), dict) else None


def write_file(path, text):
    """Write via a temp file: a crash or a second retro running at the same time never leaves half a file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_cache(path, entry):
    write_file(path, json.dumps(entry, ensure_ascii=False, indent=1))


def save_numbers(path, numbers):
    """Keep the page's counts in its summary file (alone when there is no summary, e.g. --llm none) for index.html.

    The counts are computed locally, so this never needs the LLM; the summary and its hash are left as they are.
    """
    entry = read_entry(path) or {}
    if entry.get("numbers") != numbers:
        entry["numbers"] = numbers
        write_cache(path, entry)


def made_at(entry):
    try:
        return f"{dt.datetime.fromisoformat(entry['created']):%m/%d %H:%M}"
    except (KeyError, TypeError, ValueError):
        return "이전"


def cached_summary(choice, prompt, system, schema, task, path, refresh=False, numbers=None):
    """get_summary(), except the same logs are never summarized twice.

    Returns (summary, footer note, stale). The saved summary is reused while the
    prompt hash matches; new logs or --refresh summarize again, and if that fails
    the older summary is shown instead of numbers only — then stale is when it was
    made ("09/24 18:02") and both the footer and the summary box say so.
    numbers: a few counts saved alongside, for index.html.
    choice "cached": never summarize (no network) — the saved summary, marked stale when the logs changed since.
    """
    if choice == "cached":
        return saved_only(prompt, system, schema, path)
    if choice == "none" or not path:
        return get_summary(choice, prompt, system, schema, task), "", ""
    digest = prompt_hash(prompt, system, schema)
    saved = read_cache(path)
    if saved and saved.get("prompt_sha256") == digest and not refresh:
        print("· 요약: 캐시 재사용", file=sys.stderr)
        return saved["summary"], f"요약은 Claude가 작성 ({made_at(saved)}에 만든 요약 재사용)", ""
    summary = get_summary(choice, prompt, system, schema, task)
    if summary:
        write_cache(path, {"prompt_sha256": digest, "created": dt.datetime.now().isoformat(timespec="seconds"),
                           "backend": LAST_BACKEND, "summary": summary, "numbers": numbers or {}})
        return summary, "", ""
    if saved:
        print("· 새 요약 실패 → 이전 요약 표시", file=sys.stderr)
        return saved["summary"], (f"이전 요약 표시 — {made_at(saved)}에 만든 요약이라 그 뒤 로그는 빠져 있을 수 있습니다. "
                                  f"{LAST_FAILURE}"), made_at(saved)
    return None, "", ""


BACKEND_NAMES = {"api": "Anthropic API (ANTHROPIC_API_KEY, 내 계정)", "claude": "Claude Code (claude -p, 내 Claude 로그인)"}


def preview_text(what, prompt, system, schema, task, choice, path, refresh=False):
    """--preview: exactly what one summary call would send and to where. Calls nothing, writes nothing.

    what: "2026-09-23 일간"; path: the summary cache file (a matching saved summary means nothing is sent).
    """
    backend = pick_backend(choice)
    # summarize() sends system as the system prompt; summarize_cli() sends system + task as `claude -p`'s prompt
    fixed = system + "\n\n" + task if backend == "claude" else system
    saved = read_cache(path) if path else None
    reuse = bool(saved) and saved.get("prompt_sha256") == prompt_hash(prompt, system, schema)
    head = [f"요약 미리보기 — {what}. 아무것도 보내지 않았고 페이지도 만들지 않았습니다."]
    if not backend:
        why = "요약 끔(--llm none)" if choice == "none" else "요약 도구 없음(API 키도 claude CLI도 없음)"
        head.append(f"보낼 곳: 없음 — {why}. 숫자만 만들고 아무것도 보내지 않습니다. 아래는 요약을 켜면 보낼 내용입니다.")
    else:
        retry = " (실패하면 Claude Code로 재시도)" if backend == "api" and choice == "auto" and shutil.which("claude") else ""
        head.append(f"보낼 곳: {BACKEND_NAMES.get(backend, backend)}{retry}")
    head.append(f"보낼 기록: {len(prompt):,}자 (아래 '보낼 기록' 전부) + 고정 지시문 {len(fixed):,}자 + 답 형식(JSON 스키마)")
    if backend and reuse and not refresh:
        head.append("저장된 요약이 바로 이 내용으로 만든 것입니다 → 지금 실행하면 보내지 않고 저장된 요약을 다시 씁니다"
                    " (--refresh면 다시 보냄).")
    return "\n".join(head + ["", "---- 고정 지시문 (retro가 붙이는 문구, 내 기록 아님) ----", fixed,
                             "", "---- 보낼 기록 ----", prompt])


def saved_only(prompt, system, schema, path):
    """--llm cached: the saved summary without any LLM call; numbers only when there is none."""
    global LAST_FAILURE
    saved = read_cache(path) if path else None
    if not saved:
        LAST_FAILURE = "저장된 요약 없음 (--llm cached: 요약을 새로 받지 않음)"
        return None, "", ""
    print("· 요약: 저장된 요약 사용 (--llm cached)", file=sys.stderr)
    if saved.get("prompt_sha256") == prompt_hash(prompt, system, schema):
        return saved["summary"], f"요약은 Claude가 작성 ({made_at(saved)}에 만든 요약 재사용)", ""
    return saved["summary"], f"저장된 요약 표시 — {made_at(saved)}에 만든 요약이라 그 뒤 로그는 빠져 있을 수 있습니다.", made_at(saved)


def saved_dailies(days, by_day, stats, cache_dir):
    """{day: (daily summary, fresh)} for the week's days that have a saved daily summary.

    fresh: made from exactly these logs (the hash the daily page would get now).
    """
    out = {}
    for d in days:
        saved = read_cache(cache_path(cache_dir, "daily", d))
        if saved and by_day[d]:
            digest = prompt_hash(build_prompt(d, by_day[d], stats["per_day"][d]), SYSTEM, SUMMARY_SCHEMA)
            out[d] = (saved["summary"], saved.get("prompt_sha256") == digest)
    return out


# ---------------------------------------------------------------- charts
def hour_chart(hours):
    shown = [h for h in range(24) if hours.get(h)]
    if not shown:
        return ""
    lo, hi = min(shown), max(shown)
    span = list(range(lo, hi + 1))
    mx = max(sum(hours[h].values()) for h in span) or 1
    W, H, pad = 640, 180, 28
    step = (W - pad) / len(span)
    bw = min(22.0, step * 0.7)
    out = [f'<svg viewBox="0 0 {W} {H + 24}" class="chart" role="img" aria-label="시간대별 활동">']
    for g in (0.5, 1.0):
        y = H - (H - 10) * g
        out.append(f'<line x1="{pad}" x2="{W}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{pad - 6}" y="{y + 4:.1f}" class="axis" text-anchor="end">{round(mx * g)}</text>')
    for i, h in enumerate(span):
        x = pad + i * step + (step - bw) / 2
        y = H
        for key, label, var in SERIES:
            n = hours[h].get(key, 0)
            if not n:
                continue
            bh = (H - 10) * n / mx
            y -= bh
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="2" '
                       f'style="fill:var({var})"><title>{h}시 · {label} {n}</title></rect>')
        out.append(f'<text x="{x + bw / 2:.1f}" y="{H + 16}" class="axis" text-anchor="middle">{h}</text>')
    out.append("</svg>")
    return "".join(out)


AI_KEYS = set(AI_SOURCES) | {"agent"}  # the daily page's 7-day chart: talking to AIs only


def week_chart(all_events, day, days=None, keys=AI_KEYS, label="최근 7일"):
    """Stacked bars per day (default: the 7 days up to day); day's label is bold."""
    days = days or [day - dt.timedelta(days=i) for i in range(6, -1, -1)]
    per = {d: Counter() for d in days}
    for e in all_events:
        d = e["ts"].date()
        if d in per and series_key(e) in keys:
            per[d][series_key(e)] += 1
    mx = max((sum(c.values()) for c in per.values()), default=0) or 1
    W, H, bw = 640, 110, 44
    step = W / len(days)
    out = [f'<svg viewBox="0 0 {W} {H + 34}" class="chart" role="img" aria-label="{label}">']
    for i, d in enumerate(days):
        x, y = i * step + (step - bw) / 2, H
        for key, label, var in SERIES:
            n = per[d].get(key, 0)
            if not n:
                continue
            bh = (H - 8) * n / mx
            y -= bh
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw}" height="{bh:.1f}" rx="2" '
                       f'style="fill:var({var})"><title>{d:%m/%d} {label} {n}</title></rect>')
        cls = "axis strong" if d == day else "axis"
        out.append(f'<text x="{x + bw / 2:.1f}" y="{H + 15}" class="{cls}" text-anchor="middle">{d:%m/%d}</text>'
                   f'<text x="{x + bw / 2:.1f}" y="{H + 29}" class="{cls}" text-anchor="middle">{WEEKDAYS[d.weekday()]}</text>')
    out.append("</svg>")
    return "".join(out)


def bars(rows, var):
    if not rows:
        return ""
    mx = max(n for _, n in rows) or 1
    return "".join(
        f'<div class="hbar"><span class="hl">{esc(k)}</span><span class="track"><span class="fill" '
        f'style="width:{100 * n / mx:.0f}%;background:var({var})"></span></span><span class="hn">{n}</span></div>'
        for k, n in rows)


def esc(s):
    return html.escape(str(s))


# ---------------------------------------------------------------- page
CSS = """
:root{--bg:#fff;--fg:#1f1f1f;--muted:#6b6b6b;--line:#e8e8e6;--soft:#f7f7f5;--chip:#f1f1ef;
--c-codex:#2f6fde;--c-claude:#d9773b;--c-claudeapp:#e8a878;--c-chatgpt:#3fa7a0;--c-agent:#8a63d2;--c-web:#b9b9b4;--c-git:#2e9d6a;--c-other:#d4b24c}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#191919;--fg:#e9e9e7;--muted:#9b9b9b;--line:#2f2f2f;--soft:#202020;--chip:#2a2a2a;--c-web:#5a5a57}}
:root[data-theme=dark]{--bg:#191919;--fg:#e9e9e7;--muted:#9b9b9b;--line:#2f2f2f;--soft:#202020;--chip:#2a2a2a;--c-web:#5a5a57}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Pretendard",sans-serif}
main{max-width:760px;margin:0 auto;padding:48px 16px 80px}
.icon{font-size:44px;line-height:1}h1{font-size:30px;margin:10px 0 4px;letter-spacing:-.5px}
h2{font-size:19px;margin:36px 0 10px;padding-bottom:4px;border-bottom:1px solid var(--line)}h2 .sub{font-weight:400}
.props{display:grid;grid-template-columns:110px 1fr;gap:6px 12px;margin:14px 0 8px;font-size:14px}.props dt{color:var(--muted)}.props dd{margin:0;min-width:0;overflow-wrap:anywhere}
.chip{display:inline-block;background:var(--chip);border-radius:4px;padding:0 7px;margin:0 4px 4px 0;font-size:13px}
.callout{background:var(--soft);border-radius:6px;padding:14px 16px;display:flex;gap:10px;margin:14px 0}.callout p{margin:6px 0 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
.kpi{border:1px solid var(--line);border-radius:8px;padding:10px 12px}.kpi b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.kpi span{color:var(--muted);font-size:12.5px}
.kpi small{font-size:12px;font-weight:400;color:var(--muted);margin-left:6px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{border:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top}th{background:var(--soft);font-weight:600;color:var(--muted);font-size:12.5px}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}td.write{color:var(--muted);min-width:90px}
.chart{width:100%;height:auto}.grid{stroke:var(--line)}.axis{fill:var(--muted);font-size:11px}.axis.strong{fill:var(--fg);font-weight:700}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12.5px;color:var(--muted);margin:4px 0 8px}.lg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.hbar{display:grid;grid-template-columns:180px 1fr 40px;align-items:center;gap:10px;font-size:13.5px;margin:6px 0}.hl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.track{background:var(--soft);border-radius:3px;height:10px}.fill{display:block;height:10px;border-radius:3px}.hn{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.mix{display:flex;height:14px;border-radius:4px;overflow:hidden;margin:6px 0}.mix span{display:block}
.stack{display:flex;height:10px;border-radius:3px;overflow:hidden;margin:6px 0}.track .stack{margin:0}.stack span{display:block}
.tl{position:relative;height:12px}.tl i{position:absolute;top:0;height:12px;min-width:3px;border-radius:2px}
.tl-axis{position:relative;height:16px;font-size:11px;color:var(--muted)}.tl-axis b{position:absolute;font-weight:400;transform:translateX(-50%)}
.hm{display:grid;grid-template-columns:64px repeat(24,minmax(0,1fr));gap:2px;align-items:center;font-size:11px;color:var(--muted)}
.hm b{font-weight:400}.hc{display:block;height:16px;border-radius:2px;background:var(--c-codex)}.hc.z{background:var(--soft)}
.card{border:1px solid var(--line);border-radius:8px;padding:10px 12px}.card p{margin:4px 0}
.note{background:var(--soft);border-radius:6px;padding:8px 12px;font-size:13px;color:var(--muted)}
details.more>summary{cursor:pointer;list-style:none}details.more>summary::-webkit-details-marker{display:none}
details.more>summary h2::after{content:"펼쳐보기 ▸";float:right;font-size:12.5px;font-weight:400;color:var(--muted);margin-top:4px}
details.more[open]>summary h2::after{content:"접기 ▾"}
ul.todo{list-style:none;padding:0}ul.todo li::before{content:"☐ ";color:var(--muted)}
.sub{color:var(--muted);font-size:13px}.warn{color:#c24f3b}.foot{margin-top:40px;color:var(--muted);font-size:12.5px}
a{color:var(--fg);text-underline-offset:2px}.pnav{font-size:13px;color:var(--muted);margin:0 0 20px}.pnav .off{opacity:.45}
.anchor{position:absolute;top:0}
.tabs{display:flex;gap:4px;margin:18px 0 0;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:1}
.tabs a{padding:8px 14px;text-decoration:none;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px}.tabs small{font-size:12px;color:var(--muted)}
.tabs .t-all,#am:target~.tabs .t-am,#pm:target~.tabs .t-pm{color:var(--fg);font-weight:600;border-bottom-color:var(--fg)}
#am:target~.tabs .t-all,#pm:target~.tabs .t-all{color:var(--muted);font-weight:400;border-bottom-color:transparent}
.s-am,.s-pm,#am:target~.s-all,#pm:target~.s-all{display:none}#am:target~.s-am,#pm:target~.s-pm{display:block}
@media (max-width:560px){.hbar{grid-template-columns:110px 1fr 32px}.props{grid-template-columns:90px 1fr}.hm{grid-template-columns:48px repeat(24,minmax(0,1fr));gap:1px}.hm .hm-d{font-size:10px}}
"""
MIX_COLORS = ["#2f6fde", "#d9773b", "#8a63d2", "#2e9d6a", "#d4b24c", "#c24f6b", "#4aa3b5", "#9b9b9b"]
# prompt-wording categories: plain palette colors, none of them meaning good or bad
TYPE_COLORS = {analyze.INSTRUCT: "#2f6fde", analyze.QUESTION: "#3fa7a0", analyze.APPROVE: "#8a63d2",
               analyze.FIX: "#d4b24c", analyze.PASTE: "#d9773b", analyze.OTHER: "#9b9b9b"}
SLICE_TABS = (("all", "전체", "00:00–23:59"), ("am", "오전", "00:00–11:59"), ("pm", "오후", "12:00–23:59"))
TAB_ANCHORS = "".join(f'<span id="{k}" class="anchor"></span>' for k, _, _ in SLICE_TABS)  # :target picks the tab
TIMELINE_ROWS = 8  # projects shown in the timeline
SIGNAL_ROWS = 8  # stuck signals listed per slice
CLOUD_NOTE = "클라우드 Claude Code 대화는 수집하지 않음(커밋으로만 반영)"
BLOCK_CAVEAT = "기록이 이어진 구간입니다. 실제 집중·근무 시간과 다를 수 있습니다."
BLOCK_NOTE = "연속 활동 구간: " + BLOCK_CAVEAT
LEVERAGE_LABEL = "내 지시 1건당 자동 실행"


def labeler(summary, events=()):
    """raw project → the LLM's label; a name the user set (`retro alias`, the events carry project_raw) stays as is."""
    labels = {p["raw"]: p["label"] for p in (summary or {}).get("project_labels", [])}
    for e in events:
        if e.get("project_raw"):
            labels.pop(e["project"], None)
    return lambda raw: labels.get(raw, raw)


def merge_projects(rows, name):
    merged = Counter()
    for k, n in rows:  # several folders often belong to one project once labeled
        merged[name(k)] += n
    return merged.most_common()


def chips(items):
    return "".join(f'<span class="chip">{esc(x)}</span>' for x in items)


def page_head(tab, icon, title, props, nav="", pre=""):
    """<head>, the ← · → nav row, icon, title and the Notion-style property list [(name, html)].

    pre: markup placed first in <main> (the daily page's tab anchors).
    """
    rows = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in props)
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">{GENERATOR}<title>{esc(tab)}</title>'
            f'<style>{CSS}</style></head>\n<body><main>{pre}{nav}<div class="icon">{icon}</div><h1>{esc(title)}</h1>\n'
            f'<dl class="props">{rows}</dl>')


def source_label(source, host):
    return TOOL_NAMES.get(source, source) + (f" ({host})" if host else "")


COLLECT_FAILURES = []  # set by main from --failed: sources this run could not collect (see retro.collect_all)


def coverage(events, off=(), fmt="%H:%M"):
    """관측 범위: what the page is built from — records per source and host, first–last record, sources turned off."""
    by = Counter((e["source"], e.get("host", "")) for e in events)
    seen = " · ".join(f"{esc(source_label(src, host))} {n}" for (src, host), n in by.most_common()) or "기록 없음"
    span = f" · {events[0]['ts'].strftime(fmt)} – {events[-1]['ts'].strftime(fmt)}" if events else ""
    off = f"<br>꺼진 소스: {esc(', '.join(off))}" if off else ""
    failed = "".join(f'<br><span class="warn">⚠️ 수집 실패 — {esc(f)}</span>' for f in COLLECT_FAILURES)
    return f'{seen}{span}{off}{failed}<br><span class="sub">{CLOUD_NOTE}</span>'


def more(head, body):
    """A section folded by default, so the first view stays light; nothing when body is empty."""
    return f'<details class="more"><summary><h2>{head}</h2></summary>{body}</details>' if body else ""


def refs_text(evidence):
    """Evidence refs [{time, source}] as "10:05 claude, 10:40 git" (an older summary's plain string as is)."""
    if isinstance(evidence, str):
        return evidence
    return ", ".join(" ".join(x for x in (r.get("time", ""), r.get("source", "")) if x)
                     for r in evidence or [] if isinstance(r, dict))


def headline(s, label="한 줄 요약", stale=""):
    """💡 the one-line summary and its keywords; stale: when an older summary (kept after a failed refresh) was made."""
    if not s.get("one_line"):
        return ""
    kw = chips(s.get("keywords") or [])
    kw = f'<div style="margin-top:6px">{kw}</div>' if kw else ""
    old = f'<p class="sub">※ {esc(stale)}에 만든 요약 — 현재 숫자와 다른 시점의 요약입니다.</p>' if stale else ""
    return f'<div class="callout"><span>💡</span><div><b>{label}</b><br>{esc(s["one_line"])}{kw}{old}</div></div>'


def by_tool(counter, unit=""):
    return " · ".join(f"{esc(TOOL_NAMES.get(k, k))} {v}{unit}" for k, v in counter.most_common())


def kpis(stats, extra=(), deltas=None):
    """The KPI row: my AI prompts by tool, automatic AI runs, commits (only when there are any), web visits, extra.

    extra: [(value html, label html)]; deltas: {prompts|agent|commits|web: html} after those numbers (weekly ▲▼).
    """
    d = deltas or {}
    auto = f"<br>{by_tool(stats['agent_by_tool'])}" if stats["agent_by_tool"] else ""
    cells = [(f"{stats['prompts']}{d.get('prompts', '')}", "내가 AI에 준 지시<br>" + by_tool(stats["prompts_by_tool"])),
             (f"{stats['agent']}{d.get('agent', '')}", "AI 자동 실행 (에이전트)" + auto)]
    if stats["commits"]:
        cells.append((f"{stats['commits']}{d.get('commits', '')}", f"커밋<br>+{stats['add']:,} / −{stats['dele']:,}줄"))
    cells += [(f"{stats['web']}{d.get('web', '')}", "웹 페이지 방문")] + list(extra)
    if stats["people"]:
        cells.append((f"{len(stats['people'])}명", "대화한 사람"))
    return '<div class="kpis">\n' + "\n".join(f'<div class="kpi"><b>{n}</b><span>{l}</span></div>' for n, l in cells) + "\n</div>"


def leverage(x):
    return "계산 불가" if x is None else f"{x:g}건"


def block_label(blocks, longest):
    """Caption of the 연속 활동 구간 KPI: how many blocks, the longest one in small print."""
    if not blocks:
        return "연속 활동 구간<br>15분 안 간격으로 45분 넘게 이어진 기록 없음"
    return f"연속 활동 구간<br>{len(blocks)}개 · 가장 긴 구간 {analyze.hm(longest['minutes'])}"


def rhythm_kpis(r):
    """연속 활동 구간, 기록상 프로젝트 변경, 내 지시 1건당 자동 실행 — neutral names, no scores."""
    return [(analyze.hm(r["focus_minutes"]), block_label(r["focus_blocks"], r["longest"])),
            (f"{r['switches']}회", "기록상 프로젝트 변경"),
            (leverage(r["leverage"]), LEVERAGE_LABEL)]


def legend(keys):
    return '<div class="legend">' + "".join(f'<span class="lg"><i style="background:var({v})"></i>{l}</span>'
                                            for k, l, v in SERIES if k in keys) + "</div>"


def mix_bar(mix):
    seg = "".join(f'<span style="width:{m["percent"]}%;background:{MIX_COLORS[i % 8]}" title="{esc(m["type"])} {m["percent"]}%"></span>'
                  for i, m in enumerate(mix))
    lg = "".join(f'<span class="lg"><i style="background:{MIX_COLORS[i % 8]}"></i>{esc(m["type"])} {m["percent"]}%</span>'
                 for i, m in enumerate(mix))
    return f'<div class="sub" style="margin-top:14px">활동 유형 (AI 추정)</div><div class="mix">{seg}</div><div class="legend">{lg}</div>'


def stack(parts, width=100):
    """One stacked bar of parts [(label, n, css color)], width % of its track long."""
    total = sum(n for _, n, _ in parts) or 1
    segs = "".join(f'<span style="width:{100 * n / total:.1f}%;background:{c}" title="{esc(l)} {n}"></span>'
                   for l, n, c in parts)
    return f'<span class="stack" style="width:{width:.0f}%">{segs}</span>'


def stack_legend(parts, total=None):
    """Legend for stack(); with total, each entry also shows its share."""
    share = (lambda n: f" ({n / total:.0%})") if total else (lambda n: "")
    return '<div class="legend">' + "".join(f'<span class="lg"><i style="background:{c}"></i>{esc(l)} {n}{share(n)}</span>'
                                            for l, n, c in parts) + "</div>"


def lists(s, heads, cls=""):
    return "".join(f"<h2>{head}</h2><ul{cls}>" + "".join(f"<li>{esc(x)}</li>" for x in s[key]) + "</ul>"
                   for key, head in heads if s.get(key))


# ---------------------------------------------------------------- ⏱ 흐름: one time axis per slice
def hour_span(events):
    """[first hour, last hour + 1) of the (sorted) events."""
    return events[0]["ts"].hour, events[-1]["ts"].hour + 1


def x_pct(ts, span):
    lo, hi = span
    return 100 * (ts.hour + ts.minute / 60 + ts.second / 3600 - lo) / (hi - lo)


def mark(start, end, span, title):
    left = x_pct(start, span)
    return left, x_pct(end, span) - left, title


def tl_row(label, marks, color, count=""):
    """A labelled track with marks [(left %, width %, title)] (CSS keeps a one-event mark 3px wide)."""
    inner = "".join(f'<i style="left:{l:.1f}%;width:{w:.1f}%;background:{color}" title="{esc(t)}"></i>' for l, w, t in marks)
    return (f'<div class="hbar"><span class="hl">{esc(label)}</span><span class="track tl">{inner}</span>'
            f'<span class="hn">{count}</span></div>')


def tl_axis(span):
    lo, hi = span
    step = 1 if hi - lo <= 8 else 2 if hi - lo <= 16 else 3
    ticks = "".join(f'<b style="left:{100 * (h - lo) / (hi - lo):.1f}%">{h}</b>' for h in range(lo, hi + 1, step))
    return f'<div class="hbar"><span></span><span class="tl-axis">{ticks}</span><span></span></div>'


def block_band(rhythm, span, name):
    """연속 활동 띠: blocks of my prompts/commits less than 15 min apart for 45+ min, on the time axis."""
    marks = [mark(b["start"], b["end"], span, f'연속 활동 {b["start"]:%H:%M}–{b["end"]:%H:%M} · {analyze.hm(b["minutes"])} · '
                  f'{name(b["project"])}') for b in rhythm["focus_blocks"]]
    return tl_row("연속 활동", marks, "var(--muted)", len(marks) or "")


def project_timeline(segments, span, name):
    """One row per (labeled) project: when I was on it — runs of my prompts and commits in a row."""
    rows = {}
    for seg in segments:
        rows.setdefault(name(seg["project"]), []).append(seg)
    ranked = sorted(rows.items(), key=lambda kv: -sum(x["events"] for x in kv[1]))
    out = [tl_row(label, [mark(x["start"], x["end"], span, f'{label} {x["start"]:%H:%M}–{x["end"]:%H:%M} · {x["events"]}건')
                          for x in segs], MIX_COLORS[i % len(MIX_COLORS)], sum(x["events"] for x in segs))
           for i, (label, segs) in enumerate(ranked[:TIMELINE_ROWS])]
    if len(ranked) > TIMELINE_ROWS:
        out.append(f'<p class="sub">외 {len(ranked) - TIMELINE_ROWS}개 프로젝트</p>')
    return "".join(out)


def block_note(rhythm, name):
    blocks = rhythm["focus_blocks"]
    if not blocks:
        return '<p class="sub">연속 활동 구간 없음 — 내 지시·커밋이 15분 안 간격으로 45분 넘게 이어진 기록이 없습니다.</p>'
    items = " · ".join(f'{b["start"]:%H:%M}–{b["end"]:%H:%M} ({analyze.hm(b["minutes"])}'
                       + (f', {esc(name(b["project"]))})' if b["project"] else ")") for b in blocks)
    return f'<p class="sub">연속 활동 구간 {analyze.hm(rhythm["focus_minutes"])}: {items}. {BLOCK_CAVEAT}</p>'


def flow(events, stats, rhythm, name):
    """⏱ 흐름 body: activity per hour, then the 연속 활동 band and the project timeline on one time axis."""
    keys = {k for k, _, _ in SERIES if any(stats["hours"][h].get(k) for h in stats["hours"])}
    span = hour_span(events)
    return (f"{legend(keys)}{hour_chart(stats['hours'])}"
            '<div class="sub" style="margin-top:10px">연속 활동 구간 · 프로젝트별 타임라인 (내 지시 + 커밋)</div>'
            + block_band(rhythm, span, name) + project_timeline(rhythm["segments"], span, name) + tl_axis(span)
            + block_note(rhythm, name))


# ---------------------------------------------------------------- ✅ 한 일, 🧭 결정 / 🚧 막힘, ➡️, ✍️
HM_RE = re.compile(r"(\d{1,2}):(\d{2})")


def row_slices(time_text):
    """{"am"}, {"pm"}, both (the row spans noon) or none (no time given: it stays on the 전체 tab only)."""
    marks = [int(h) * 60 + int(m) for h, m in HM_RE.findall(time_text or "")]
    if not marks:
        return set()
    start, end = marks[0], marks[-1]
    if end < start:  # runs past midnight
        end += 24 * 60
    return {k for k, hit in (("am", start < 12 * 60), ("pm", end >= 12 * 60)) if hit}


def done_rows(s, key):
    rows = [d for d in s.get("done") or [] if isinstance(d, dict)]
    return rows if key == "all" else [d for d in rows if key in row_slices(d.get("time", ""))]


def status_chip(item):
    """Only "요청함" is marked: asked for, with no commit or completion in the log."""
    return f' <span class="chip">{esc(item["status"])}</span>' if item.get("status") == "요청함" else ""


def done_section(s, key, name):
    """✅ 한 일: the summary's rows whose time falls in this slice; the 전체 tab adds the activity mix."""
    mix = mix_bar(s["activity_mix"]) if key == "all" and s.get("activity_mix") else ""
    if not s.get("done") and not mix:
        return ""
    rows = "".join(f'<tr><td>{esc(d.get("time", ""))}</td><td>{esc(name(d.get("project", "")))}</td>'
                   f'<td>{esc(d.get("result", ""))}{status_chip(d)}</td><td>{esc(refs_text(d.get("evidence")))}</td></tr>'
                   for d in done_rows(s, key))
    body = (f"<table><tr><th>시간</th><th>프로젝트</th><th>결과물</th><th>근거</th></tr>{rows}</table>" if rows
            else '<p class="sub">이 시간대에 해당하는 한 일이 없습니다.</p>' if s.get("done") else "")
    head = "✅ 한 일" if key == "all" else '✅ 한 일 <span class="sub">하루 전체 요약 중 이 시간대</span>'
    return f"<h2>{head}</h2>{body}{mix}"


def hhmm(ts):
    return f"{ts:%H:%M}"


def day_hhmm(ts):
    """The weekly page's times: 수 08:05."""
    return f"{WEEKDAYS[ts.weekday()]} {ts:%H:%M}"


def signal_line(b, clock=hhmm):
    """Stuck signals in one line (재시도·연속 수정·불만 with times), under the blockers."""
    parts = []
    if b["retries"]:
        times = ", ".join(clock(r["ts"]) for r in b["retry_list"][:6]) + ("…" if b["retries"] > 6 else "")
        parts.append(f"30분 안 같은 도구·프로젝트에서 거의 같은 지시 {b['retries']}회 ({times})")
    if b["complaint_streaks"]:
        parts.append("연속 수정·불만 문구 " + ", ".join(f"{clock(c['start'])}–{c['end']:%H:%M} {c['count']}회"
                                                 for c in b["complaint_streaks"][:4]))
    return f'<p class="sub">신호 (자동 분류, 추정): {" · ".join(parts)}</p>' if parts else ""


def decisions_blockers(s, b, blockers_head="🚧 막힌 것 · 리스크", clock=hhmm):
    out = lists(s, (("decisions", "🧭 결정한 것"),))
    signals = signal_line(b, clock)
    if s.get("blockers") or signals:
        items = "".join(f"<li>{esc(x)}</li>" for x in s.get("blockers") or [])
        out += f"<h2>{blockers_head}</h2>" + (f"<ul>{items}</ul>" if items else "") + signals
    return out


def next_section(s, key="tomorrow", head="➡️ 내일로", mine=""):
    """➡️ 내일로 (내일 첫 할 일 on top) — or 다음 주로 on the weekly page. mine: the first task I saved (wins over the draft)."""
    first, items = mine or s.get("first_task_tomorrow"), s.get(key) or []
    if not first and not items:
        return ""
    out = f"<h2>{head}</h2>"
    if first:
        who = ' <span class="sub">내가 정함</span>' if mine else ""
        out += f'<div class="callout"><span>▶️</span><div><b>내일 첫 할 일</b>{who}<br>{esc(first)}</div></div>'
    return out + ('<ul class="todo">' + "".join(f"<li>{esc(x)}</li>" for x in items) + "</ul>" if items else "")


def kpt_section(kpt, head="✍️ KPT", mine=None):
    """Keep / Problem / Try: the LLM's draft beside my own column — "(직접 작성)" until I write it in retro app."""
    kpt, mine = (kpt if isinstance(kpt, dict) else {}), mine or {}
    rows = "".join(f'<tr><th>{label}</th><td>{esc(kpt.get(k) or "–")}</td>'
                   + (f"<td>{multiline(mine[k])}</td>" if mine.get(k) else '<td class="write">(직접 작성)</td>') + "</tr>"
                   for k, label in (("keep", "Keep"), ("problem", "Problem"), ("try", "Try")))
    return f'<h2>{head}</h2><table><tr><th></th><th>AI 초안</th><th>내 생각</th></tr>{rows}</table>'


# ---------------------------------------------------------------- my notes (notes.py), rewritten in place after a save
NOTES_EDIT = "수정은 retro app에서"
NOTE_RE = re.compile(r"<!--notes:(\w+)( ai)?-->.*?<!--/notes:\1-->", re.S)


def note_block(name, body, ai):
    """A part built from my notes, between markers refresh_notes() finds; ai: the page was made with a summary."""
    return f"<!--notes:{name}{' ai' if ai else ''}-->{body}<!--/notes:{name}-->"


def multiline(text):
    return esc(text).replace("\n", "<br>")


def md_label(d):
    return f"{d:%m/%d} ({WEEKDAYS[d.weekday()]})"


def followup_labels(day, prev_day):
    """{item: label} — "어제 정한 첫 할 일" / "어제의 Try", or with the date when the last notes are older."""
    if prev_day == day - dt.timedelta(days=1):
        return {"first_task": "어제 정한 첫 할 일", "try": "어제의 Try"}
    return {"first_task": f"{md_label(prev_day)}에 정한 첫 할 일", "try": f"{md_label(prev_day)}의 Try"}


AI_MARK = ' <span class="sub">(AI 제안)</span>'  # a first task the summary drafted and I never saved


def followup_callout(day, notes_dir):
    """↩️ the first task and Try of my last notes before day, and how they went (완료 / 이어가기 / 취소, set in retro app)."""
    prev = notes.previous(notes_dir, day)
    if not prev:
        return ""
    d, n = prev
    rows = "".join(f'<br>{label}: {esc(notes.text(n, item))}{AI_MARK if notes.is_ai(n, item) else ""} '
                   f'<span class="chip">{notes.status(n, item) or notes.UNCHECKED}</span>'
                   for item, label in followup_labels(day, d).items() if notes.text(n, item))
    return (f'<div class="callout"><span>↩️</span><div><b>지난 회고 확인</b>{rows}'
            f'<br><span class="sub">{NOTES_EDIT}</span></div></div>')


def my_kpt(s, n, head, reflection_label):
    """✍️ KPT with my column from my notes, then what I wrote as the most meaningful thing."""
    out = kpt_section(s.get("kpt"), head, notes.kpt(n))
    if n.get("reflection"):
        out += (f'<div class="callout"><span>🌱</span><div><b>{reflection_label}</b><br>'
                f'{multiline(n["reflection"])}</div></div>')
    return out + (f'<p class="sub">{NOTES_EDIT}</p>' if notes.has_any(n) else "")


def week_tasks_section(days, notes_dir):
    """▶️ the first tasks I set on each day of the week and how the next day went — counts, no score."""
    tasks = notes.week_tasks(notes_dir, days)
    if not tasks:
        return ""
    counts = Counter(st or notes.UNCHECKED for _, _, st, _ in tasks)
    tally = " · ".join(f"{k} {counts[k]}" for k in notes.STATUSES + (notes.UNCHECKED,))
    n_ai = sum(1 for *_, ai in tasks if ai)
    rows = "".join(f"<tr><td>{WEEKDAYS[d.weekday()]} {d:%m/%d}</td><td>{esc(t)}{AI_MARK if ai else ''}</td>"
                   f"<td>{st or notes.UNCHECKED}</td></tr>" for d, t, st, ai in tasks)
    source = f"정한 첫 할 일 {len(tasks)}개" + (f" (AI 제안 {n_ai}개 포함)" if n_ai else "")
    return (f'<h2>▶️ 이번 주 첫 할 일</h2><p class="sub">{source} · {tally}</p>'
            f'<table><tr><th>정한 날</th><th>첫 할 일</th><th>다음 날 확인</th></tr>{rows}</table>'
            f'<p class="sub">{NOTES_EDIT}</p>')


def daily_notes(day, summary, notes_dir):
    """{name: marked html} of the daily page's parts that show my notes (전체 tab)."""
    s, n, ai = summary or {}, notes.load(notes_dir, "daily", day), bool(summary)
    return {"followup": note_block("followup", followup_callout(day, notes_dir), ai),
            "next": note_block("next", next_section(s, mine=notes.text(n, "first_task")), ai),
            "kpt": note_block("kpt", my_kpt(s, n, "✍️ KPT", "오늘 가장 의미 있었던 일"), ai)}


def weekly_notes(days, summary, notes_dir):
    s, n, ai = summary or {}, notes.load(notes_dir, "weekly", days[0]), bool(summary)
    return {"tasks": note_block("tasks", week_tasks_section(days, notes_dir), ai),
            "kpt": note_block("kpt", my_kpt(s, n, "✍️ 주간 KPT", "이번 주 가장 의미 있었던 일"), ai)}


def patch_notes(page_path, blocks_for):
    """Rewrite the marked parts of one page; blocks_for(page had a summary) → {name: html}. False: no page or no markers."""
    try:
        with open(page_path, encoding="utf-8") as f:
            page = f.read()
    except OSError:
        return False
    found = NOTE_RE.findall(page)
    if not found:
        return False  # made before notes existed: only a full render adds them
    blocks = blocks_for(any(ai for _, ai in found))
    new = NOTE_RE.sub(lambda m: blocks.get(m.group(1), m.group(0)), page)
    if new != page:
        write_page(page_path, new, quiet=True)
    return True


def refresh_notes(site_dir, cache_dir, days):
    """After my notes changed: rewrite the notes parts of the daily pages of days and of their weeks' pages.

    Reads only the notes and the saved summaries (for the AI draft column) — no logs, no LLM, no network.
    Returns {page file name: True if rewritten in place, False if it has no markers}; pages that don't exist are left out.
    """
    done = {}
    for kind, when in [("daily", d) for d in sorted(set(days))] + [("weekly", m) for m in sorted({week_days(d)[0] for d in days})]:
        name = page_file(kind, when)
        path = os.path.join(site_dir, name)
        if not os.path.exists(path):
            continue

        def blocks_for(ai, kind=kind, when=when):
            saved = read_cache(cache_path(cache_dir, kind, when)) if ai else None
            summary = saved["summary"] if saved else None
            return daily_notes(when, summary, cache_dir) if kind == "daily" else weekly_notes(week_days(when), summary, cache_dir)
        done[name] = patch_notes(path, blocks_for)
    return done


def til_body(s):
    """학습 후보: the LLM's candidates with the log lines behind them."""
    items = []
    for x in s.get("til") or []:
        if isinstance(x, str):  # an older summary
            items.append(esc(x))
        elif isinstance(x, dict) and x.get("text"):
            ref = refs_text(x.get("evidence"))
            items.append(esc(x["text"]) + (f' <span class="sub">({esc(ref)})</span>' if ref else ""))
    return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>" if items else ""


def slice_note(label):
    return (f'<p class="note">{label} 탭은 이 시간대의 숫자와 한 일만 보여줍니다. 결정·막힘·내일·KPT·학습 후보는 '
            '하루 단위라 <a href="#all">전체</a> 탭에 있습니다.</p>')


# ---------------------------------------------------------------- 🧠 프롬프트 문구 신호
SIGNALS_HEAD = '🧠 프롬프트 문구 신호 <span class="sub">자동 분류, 추정</span>'


def signal_list(b, clock=hhmm):
    """Each retry and complaint streak with its time."""
    items = [(r["ts"], f'{clock(r["ts"])} 거의 같은 지시 다시 ({r["prev_ts"]:%H:%M}에 먼저): “{r["text"][:50]}”')
             for r in b["retry_list"]]
    items += [(c["start"], f'{clock(c["start"])}–{c["end"]:%H:%M} 수정·불만 문구 {c["count"]}번 연속'
               + (f' · {c["project"]}' if c["project"] else "")) for c in b["complaint_streaks"]]
    if not items:
        return '<p class="sub">재시도·연속 수정 신호 없음</p>'
    items.sort(key=lambda x: x[0])
    more_ = f'<li class="sub">외 {len(items) - SIGNAL_ROWS}개</li>' if len(items) > SIGNAL_ROWS else ""
    return ('<div class="sub" style="margin-top:14px">재시도·연속 수정 (같은 도구·기기·프로젝트, 30분 안)</div><ul>'
            + "".join(f"<li>{esc(t)}</li>" for _, t in items[:SIGNAL_ROWS]) + more_ + "</ul>")


def repeated_list(groups):
    """유사 표현 후보: each group of similar requests with two of its original sentences."""
    if not groups:
        return ""
    items = "".join(f"<li>비슷한 요청이 {g['count']}회 있었습니다. 수정 과정인지 반복 업무인지 확인해보세요."
                    + "".join(f'<br><span class="sub">“{esc(x)}”</span>' for x in g["examples"]) + "</li>"
                    for g in groups)
    return f'<div class="sub" style="margin-top:14px">유사 표현 후보</div><ul>{items}</ul>'


def type_parts(types):
    return [(k, n, TYPE_COLORS[k]) for k, n in types.items() if n]


def behavior_body(b, clock=hhmm):
    """Wording categories, length, paste ratio, stuck signals and similar requests (all computed, all heuristic)."""
    parts = type_parts(b["types"])
    return (f'<p class="sub">지시 문구를 규칙으로 나눈 것이라 실제 의도와 다를 수 있습니다.</p>'
            f'<div class="sub">문구 유형 (내 지시 {b["count"]}건)</div>{stack(parts)}{stack_legend(parts, b["count"])}'
            f'<p class="sub">길이 중앙값 {b["length_median"]}자 · 짧음(&lt;{analyze.SHORT_CHARS}자) {b["short_ratio"]:.0%} · '
            f'보통 {b["normal_ratio"]:.0%} · 김(&gt;{analyze.LONG_CHARS}자) {b["long_ratio"]:.0%} · '
            f'붙여넣기 비율 {b["paste_ratio"]:.0%}</p>' + signal_list(b, clock) + repeated_list(b["repeated"]))


def coaching(s):
    """At most one prompt to review, and only when the LLM quoted it and named the log line."""
    items = s.get("prompt_coaching")
    items = items if isinstance(items, list) else []
    c = next((x for x in items if isinstance(x, dict) and x.get("prompt") and refs_text(x.get("evidence"))), None)
    if not c:
        return ""
    better = f'<p>→ {esc(c["better"])}</p>' if c.get("better") else ""
    return (f'<div class="sub" style="margin-top:14px">검토할 지시 후보 (AI)</div><div class="card">'
            f'<span class="chip">{esc(c.get("kind", ""))}</span><span class="sub">{esc(refs_text(c["evidence"]))}</span>'
            f'<p>“{esc(c["prompt"])}”</p>{better}<p class="sub">{esc(c.get("why", ""))}</p></div>')


def automation(s):
    ideas = [i for i in s.get("automation_ideas") or [] if isinstance(i, dict) and i.get("idea")]
    if not ideas:
        return ""
    items = "".join(f'<li><span class="chip">{esc(i.get("kind", ""))}</span>{esc(i["idea"])}'
                    + (f' <span class="sub">← {esc(i["request"])}</span>' if i.get("request") else "") + "</li>"
                    for i in ideas)
    return f'<div class="sub" style="margin-top:14px">자동화 검토 후보 (AI)</div><ul>{items}</ul>'


def behavior_more(b, s=None, extra="", clock=hhmm):
    """The folded 문구 신호 section; s (전체 tab, weekly page) adds the LLM's automation and coaching candidates."""
    body = behavior_body(b, clock) + extra if b["count"] else '<p class="sub">이 시간대에는 내 지시가 없습니다.</p>'
    if s:
        body += automation(s) + coaching(s)
    return more(SIGNALS_HEAD, body)


# ---------------------------------------------------------------- 🤖 AI 사용, 👥 사람, 🌐 탐색
def ai_usage(stats):
    """🤖 AI 사용 body: my prompts by tool, then the automatic runs (agent events) by the tool that ran them and host.

    Which tool sent an automatic run is not in the logs, so only where it ran is named.
    """
    mine, auto = stats["prompts_by"], stats["agent_by"]
    if not mine and not auto:
        return ""
    chain = "나 → " + (by_tool(stats["prompts_by_tool"], "건") or "직접 지시 없음")
    if auto:
        chain += " → (자동 실행) " + " · ".join(f"{esc(source_label(k, h))} {n}건" for (k, h), n in auto.most_common())
    keys = sorted(set(mine) | set(auto), key=lambda k: -(mine[k] + auto[k]))
    rows = "".join(f'<tr><td>{esc(TOOL_NAMES.get(k, k))}</td><td>{esc(h) or "–"}</td><td class="num">{mine[(k, h)]}</td>'
                   f'<td class="num">{auto[(k, h)]}</td></tr>' for k, h in keys)
    return (f'<p>{chain}</p>'
            f'<table><tr><th>도구</th><th>기기</th><th class="num">내 지시</th><th class="num">자동 실행</th></tr>{rows}</table>'
            '<p class="sub">자동 실행 = 사람이 아니라 프로그램(에이전트)이 그 도구에 보낸 지시. 누가 보냈는지는 기록에 없어 '
            '실행된 도구와 기기만 적습니다.</p>')


def people_table(stats):
    if not stats["people"]:
        return ""
    rows = "".join(f'<tr><td>{esc(p)}</td><td class="num">{n}</td></tr>' for p, n in stats["people"])
    return f'<table><tr><th>누구</th><th class="num">메시지</th></tr>{rows}</table>'


def browse_body(stats, pre):
    """🌐 탐색 body: most visited sites, and what I looked at in the 10 minutes before a prompt."""
    if not stats["sites"]:
        return ""
    window = analyze.minutes(analyze.PRE_BROWSE)
    return ('<div class="sub">많이 본 사이트</div>' + bars(stats["sites"], "--c-web")
            + f'<div class="sub" style="margin-top:14px">작업 직전 탐색 — 내 지시 전 {window}분 안에 본 페이지 '
            f'{pre["visits"]}건 (웹 방문 {pre["web"]}건 중)</div>' + bars(pre["sites"], "--c-codex"))


def details_sections(stats):
    """The folded tail every page shares: 🤖 AI 사용, 👥 사람."""
    return more("🤖 AI 사용", ai_usage(stats)) + more("👥 사람", people_table(stats))


def foot(summary, note=""):
    """note: from cached_summary — the summary was reused, or an older one is shown."""
    note = (f"숫자는 로그에서 계산 · {note or '요약은 Claude가 작성'}" if summary
            else f"숫자만 표시 — {LAST_FAILURE or '요약 없음'}")
    return f'<p class="foot">{esc(note)}</p></main></body></html>'


# ---------------------------------------------------------------- daily page
def tabs(slices):
    """전체 / 오전 / 오후 links (small number: my prompts). CSS shows the panel named by the URL's #fragment."""
    return '<nav class="tabs">' + "".join(
        f'<a class="t-{k}" href="#{k}" title="{rng}">{label} <small>{sum(map(analyze.is_prompt, evs))}</small></a>'
        for k, label, rng, evs in slices) + "</nav>"


def slice_panel(key, label, events, s, name, stale="", mine=None):
    """One tab. First view: summary → numbers → 한 일 (→ 결정/막힘 → 내일 → KPT on 전체);
    folded below: 학습 후보, 흐름, 문구 신호, AI 사용, 탐색 of this slice's events.
    mine: daily_notes() — the 전체 tab's parts that show my notes.
    """
    day_level = key == "all"
    stats, b, r = day_stats(events), analyze.prompt_behavior(events), analyze.work_rhythm(events)
    mine = mine or {}
    parts = [headline(s, "한 줄 요약" if day_level else "하루 전체 요약", stale)]
    if day_level:
        parts.append(mine.get("followup", ""))
    if events:
        parts.append(f"<h2>📊 {'오늘의' if day_level else label} 숫자</h2>" + kpis(stats, rhythm_kpis(r))
                     + f'<p class="sub">{BLOCK_NOTE}</p>')
    else:
        parts.append(f'<p class="sub" style="margin-top:24px">{label}에는 기록이 없습니다.</p>')
    parts.append(done_section(s, key, name))
    if day_level:
        parts += [decisions_blockers(s, b), mine.get("next") or next_section(s), mine.get("kpt") or kpt_section(s.get("kpt")),
                  more("📚 학습 후보", til_body(s))]
    else:
        parts.append(slice_note(label))
    if events:
        parts += [more("⏱ 흐름", flow(events, stats, r, name)), behavior_more(b, s if day_level else None),
                  details_sections(stats), more("🌐 탐색", browse_body(stats, analyze.browse_before_prompts(events)))]
    return f'<section class="slice s-{key}">' + "".join(parts) + "</section>"


def render_page(day, stats, summary, all_events, nav="", note="", off=(), stale="", notes_dir=None):
    """Daily page. off: sources turned off (shown in 관측 범위); stale: see headline(); notes_dir: where my notes are."""
    s = summary or {}
    name = labeler(summary, all_events)
    events = [e for e in all_events if e["ts"].date() == day]
    title = f"{day.year}년 {day.month}월 {day.day}일 ({WEEKDAYS[day.weekday()]}) 일간 회고"
    projects = merge_projects(stats["projects"], name)
    slices = [(k, label, rng, events if k == "all" else analyze.in_slice(events, k)) for k, label, rng in SLICE_TABS]
    parts = [page_head(f"일간 회고 {day}", "🗓", title, [
        ("프로젝트", chips(p for p, _ in projects[:4]) or "–"), ("관측 범위", coverage(events, off))],
        nav, TAB_ANCHORS), tabs(slices)]
    mine = daily_notes(day, summary, notes_dir)
    parts += [slice_panel(k, label, evs, s, name, stale, mine if k == "all" else None) for k, label, _, evs in slices]
    parts.append(f"<h2>📈 최근 7일</h2>{legend(AI_KEYS)}{week_chart(all_events, day)}")
    parts.append(foot(summary, note))
    return "".join(parts)


# ---------------------------------------------------------------- weekly page
def week_span(days):
    """2026년 9월 21일 – 27일"""
    a, b = days[0], days[-1]
    end = f"{b.day}일" if b.month == a.month else f"{b.month}월 {b.day}일"
    return f"{a.year}년 {a.month}월 {a.day}일 – {end}"


def week_numbers(events):
    """The weekly KPIs of these events (also for the week before, for ▲▼). Leverage is the week's sums divided."""
    s, r = day_stats(events), analyze.work_rhythm(events)
    return {"prompts": s["prompts"], "agent": s["agent"], "commits": s["commits"], "web": s["web"],
            "active_days": len({e["ts"].date() for e in events if e["actor"] == "human"}),
            "focus": r["focus_minutes"], "blocks": r["focus_blocks"], "longest": r["longest"],
            "switches": r["switches"], "leverage": r["leverage"]}


def observed(events):
    """The (source, host) pairs these events came from."""
    return {(e["source"], e.get("host", "")) for e in events}


def delta(cur, prev, fmt=str):
    """▲▼ against the week before as a percentage; the plain difference when last week was 0."""
    if cur is None or prev is None:
        return ""
    diff = round(cur - prev, 2)
    if not diff:
        return "<small>±0</small>"
    size = fmt(abs(diff)) if not prev else f"{abs(diff) / prev:.0%}"
    return f'<small>{"▲" if diff > 0 else "▼"}{size}</small>'


def week_kpis(stats, cur, prev=None, mismatch=False):
    """KPIs of the week; prev (week_numbers of the week before, when loaded) adds ▲▼.

    mismatch: the two weeks were observed from different sources, so the ▲▼ may mislead.
    """
    def d(key, fmt=str):
        return delta(cur[key], prev[key], fmt) if prev else ""

    extra = [(f"{cur['active_days']}일{d('active_days')}", "활동한 날 (7일 중)"),
             (analyze.hm(cur["focus"]) + d("focus", analyze.hm), block_label(cur["blocks"], cur["longest"])),
             (f"{cur['switches']}회{d('switches')}", "기록상 프로젝트 변경"),
             (leverage(cur["leverage"]) + d("leverage", lambda x: f"{x:g}"), LEVERAGE_LABEL)]
    out = kpis(stats, extra, {k: d(k) for k in ("prompts", "agent", "commits", "web")})
    notes = [BLOCK_NOTE] + (["▲▼ 지난주 대비"] if prev else []) + (["관측 소스가 달라 비교가 부정확할 수 있음"] if mismatch else [])
    return out + f'<p class="sub">{" · ".join(notes)}</p>'


def link_later(href, text, exists):
    """A link when the page exists, else a marked span that refresh_navs turns into a link once it does."""
    return f'<a href="{href}">{text}</a>' if exists else f'<span data-link="{href}">{text}</span>'


def heat_cell(label, hour, n, mx):
    if not n:
        return f'<i class="hc z" title="{label} {hour}시 · 0건"></i>'
    return f'<i class="hc" style="opacity:{0.2 + 0.8 * n / mx:.2f}" title="{label} {hour}시 · {n}건"></i>'


def heatmap_section(events, days, day_links):
    """🗓 요일×시간대: 7×24 cells, darker = more of my prompts + commits; day labels open the daily pages."""
    grid = analyze.heatmap(events, days)
    mx = max(max(row) for row in grid.values()) or 1
    cells = ["<span></span>"] + [f"<b>{h if h % 3 == 0 else ''}</b>" for h in range(24)]
    for d in days:
        label = f"{WEEKDAYS[d.weekday()]} {d:%m/%d}"
        cells.append(f'<span class="hm-d">{link_later(page_file("daily", d), label, d in day_links)}</span>')
        cells += [heat_cell(label, h, n, mx) for h, n in enumerate(grid[d])]
    return ('<h2>🗓 요일×시간대</h2><p class="sub">칸이 진할수록 기록(내 지시 + 커밋)이 많은 시간 · 요일을 누르면 그날 일간 페이지</p>'
            f'<div class="hm">{"".join(cells)}</div>')


def project_counts(events, name):
    """{labeled project: my prompts + commits}"""
    return Counter(name(e["project"]) for e in events
                   if e["project"] and (analyze.is_prompt(e) or analyze.is_commit(e)))


def day_rows(days, per, colors, other):
    """One stacked bar per day (length = that day's total) from per {day: Counter}."""
    mx = max((sum(c.values()) for c in per.values()), default=0) or 1
    rows = []
    for d in days:
        n = sum(per[d].values())
        parts = [(k, v, colors.get(k, other)) for k, v in per[d].most_common()]
        rows.append(f'<div class="hbar"><span class="hl">{WEEKDAYS[d.weekday()]} {d:%m/%d}</span><span class="track">'
                    f'{stack(parts, 100 * n / mx) if n else ""}</span><span class="hn">{n or ""}</span></div>')
    return "".join(rows)


def week_projects(by_day, days, stats, s, name):
    """🗂 어디에 썼나 body: each day's projects stacked, the week's totals as the legend, then mix and sites."""
    total = project_counts([e for d in days for e in by_day[d]], name)
    top = total.most_common(len(MIX_COLORS) - 1)
    colors = {p: MIX_COLORS[i] for i, (p, _) in enumerate(top)}
    rest = sum(total.values()) - sum(n for _, n in top)
    legend_ = stack_legend([(p, n, colors[p]) for p, n in top] + ([("기타", rest, MIX_COLORS[-1])] if rest else []))
    out = ('<div class="sub">요일별 프로젝트 (내 지시 + 커밋) · 범례는 주간 합계</div>'
           + day_rows(days, {d: project_counts(by_day[d], name) for d in days}, colors, MIX_COLORS[-1]) + legend_)
    if s.get("activity_mix"):
        out += mix_bar(s["activity_mix"])
    if stats["sites"]:
        out += '<div class="sub" style="margin-top:14px">많이 본 사이트</div>' + bars(stats["sites"], "--c-web")
    return out


def week_behavior(b, by_day, days, s):
    """🧠 문구 신호 (주간): the week's numbers, the wording mix day by day, similar requests → automation, coaching."""
    per = {d: Counter(analyze.classify_prompt(e["text"]) for e in by_day[d] if analyze.is_prompt(e)) for d in days}
    change = '<div class="sub" style="margin-top:14px">요일별 문구 유형</div>' + day_rows(days, per, TYPE_COLORS, "#9b9b9b")
    return behavior_more(b, s, change, day_hhmm)


def day_table(days, stats, by_day, today, name, day_links):
    """📅 날짜별: counts per day; the 오전/오후 prompt counts open that tab of the daily page."""
    rows = []
    for d in days:
        ds = stats["per_day"][d]
        label = f"{d:%m/%d} ({WEEKDAYS[d.weekday()]})"
        if d > today:
            rows.append(f'<tr><td class="sub">{label}</td>' + "<td></td>" * 6 + "</tr>")
            continue
        top = merge_projects(ds["projects"], name)
        span = f"{ds['first']:%H:%M}–{ds['last']:%H:%M}" if ds["first"] else "–"
        href, has = page_file("daily", d), d in day_links
        am, pm = (sum(map(analyze.is_prompt, analyze.in_slice(by_day[d], k))) for k in ("am", "pm"))
        if has:
            label = f'<a href="{esc(day_links[d])}">{label}</a>'
        rows.append(f'<tr><td>{label}</td><td class="num">{ds["prompts"]}</td>'
                    f'<td class="num">{link_later(href + "#am", am, has)}</td><td class="num">{link_later(href + "#pm", pm, has)}</td>'
                    f'<td class="num">{ds["commits"]}</td><td>{esc(top[0][0]) if top else "–"}</td><td>{span}</td></tr>')
    return ('<h2>📅 날짜별</h2><table><tr><th>날짜</th><th class="num">내 지시</th><th class="num">오전</th>'
            '<th class="num">오후</th><th class="num">커밋</th><th>주요 프로젝트</th><th>활동 시간</th></tr>'
            f'{"".join(rows)}</table>')


def highlights(s, name):
    rows = "".join(f'<tr><td>{esc(h.get("days", ""))}</td><td>{esc(name(h.get("project", "")))}</td>'
                   f'<td>{esc(h.get("result", ""))}{status_chip(h)}</td><td>{esc(refs_text(h.get("evidence")))}</td></tr>'
                   for h in s.get("highlights") or [] if isinstance(h, dict))
    return (f"<h2>✅ 이번 주 한 일</h2><table><tr><th>요일</th><th>프로젝트</th><th>결과물</th><th>근거</th></tr>{rows}</table>"
            if rows else "")


def render_week(days, stats, summary, events, today=None, nav="", note="", day_links=None, prev_events=None,
                off=(), stale="", notes_dir=None):
    """Weekly page: same look as the daily one; days after today are left empty.

    day_links: {day: href} of the daily pages that exist; the heatmap and the 날짜별 table link to them.
    prev_events: the week before's events, when they were loaded — the KPIs then show ▲▼ against it.
    off, stale, notes_dir: as for render_page.
    """
    s = summary or {}
    name = labeler(summary, events)
    today = today or dt.date.today()
    day_links = day_links or {}
    by_day = split_days(events, days)
    title = f"{week_span(days)} 주간 회고"
    projects = merge_projects(stats["projects"], name)
    parts = [page_head(f"주간 회고 {days[0]}", "📅", title, [
        ("프로젝트", chips(p for p, _ in projects[:4]) or "–"), ("기간", f"{days[0]:%m/%d} (월) – {days[-1]:%m/%d} (일)"),
        ("관측 범위", coverage(events, off, "%m/%d %H:%M"))], nav), headline(s, stale=stale)]
    prev = week_numbers(prev_events) if prev_events else None
    mismatch = bool(prev_events) and observed(prev_events) != observed(events)
    parts.append("<h2>📊 이번 주 숫자</h2>" + week_kpis(stats, week_numbers(events), prev, mismatch))
    parts.append(heatmap_section(events, days, day_links))
    parts.append(highlights(s, name))
    behavior = analyze.prompt_behavior(events)
    parts.append(decisions_blockers(s, behavior, "🔁 반복된 문제 · 패턴", day_hhmm))
    parts.append(next_section(s, "next_week", "➡️ 다음 주로"))
    mine = weekly_notes(days, summary, notes_dir)
    parts += [mine["tasks"], mine["kpt"]]
    parts.append(day_table(days, stats, by_day, today, name, day_links))
    parts.append(more("📚 학습 후보", til_body(s)))
    parts.append(more("🗂 어디에 썼나", week_projects(by_day, days, stats, s, name)))
    parts.append(week_behavior(behavior, by_day, days, s))
    parts.append(details_sections(stats))
    parts.append(foot(summary, note))
    return "".join(parts)


def write_page(out, page, quiet=False):
    write_file(out, page)
    if not quiet:
        print(f"wrote {out}", file=sys.stderr)


# ---------------------------------------------------------------- nav + index (all pages sit in one folder)
GENERATOR = '<meta name="generator" content="retro">'  # marks files retro may rewrite
PAGE_RE = re.compile(r"(daily|weekly)-(\d{4}-\d{2}-\d{2})\.html")
NAV_RE = re.compile(r'<nav class="pnav">.*?</nav>', re.S)
LATER_RE = re.compile(r'<span data-link="(daily-(\d{4}-\d{2}-\d{2})\.html(?:#\w+)?)">(.*?)</span>')  # see link_later


def page_file(kind, day):
    return f"{kind}-{day}.html"


def site_pages(site_dir):
    """{"daily": {dates}, "weekly": {mondays}} of the pages in site_dir."""
    found = {"daily": set(), "weekly": set()}
    try:
        names = os.listdir(site_dir)
    except OSError:
        names = []
    for n in names:
        m = PAGE_RE.fullmatch(n)
        if m:
            try:
                found[m.group(1)].add(dt.date.fromisoformat(m.group(2)))
            except ValueError:
                pass
    return found


def nav_item(text, href):
    return f'<a href="{href}">{text}</a>' if href else f'<span class="off">{text}</span>'


def page_nav(kind, day, pages):
    """The row at the top of a page; links only to pages that exist (relative names, works from file://)."""
    have = pages[kind]
    prev = max((d for d in have if d < day), default=None)
    nxt = min((d for d in have if d > day), default=None)
    prev, nxt = (d and page_file(kind, d) for d in (prev, nxt))
    if kind == "daily":
        monday = week_days(day)[0]
        items = [nav_item("← 이전 날", prev),
                 nav_item("주간 보기", page_file("weekly", monday) if monday in pages["weekly"] else None),
                 nav_item("목록", "index.html"), nav_item("다음 날 →", nxt)]
    else:
        items = [nav_item("← 지난주", prev), nav_item("목록", "index.html"), nav_item("다음 주 →", nxt)]
    return '<nav class="pnav">' + " · ".join(items) + "</nav>"


def refresh_navs(site_dir, pages):
    """Pages made earlier get links to pages made since (the next day, the week's page).

    Only pages that already have a nav row are touched; older pages get one when re-rendered.
    """
    for kind in ("daily", "weekly"):
        for d in pages[kind]:
            path = os.path.join(site_dir, page_file(kind, d))
            try:
                with open(path, encoding="utf-8") as f:
                    page = f.read()
            except OSError:
                continue
            if not NAV_RE.search(page):
                continue
            new = NAV_RE.sub(lambda _: page_nav(kind, d, pages), page, count=1)
            if kind == "weekly":  # link days whose daily page appeared later
                new = link_new_dailies(new, d, pages["daily"])
            if new != page:
                write_page(path, new, quiet=True)


def link_new_dailies(page, monday, dailies):
    """A weekly page with links to the daily pages made since: 날짜별 labels, 오전/오후 counts, heatmap days."""
    for x in week_days(monday):
        label = f"{x:%m/%d} ({WEEKDAYS[x.weekday()]})"
        if x in dailies:
            page = page.replace(f"<td>{label}</td>", f'<td><a href="{page_file("daily", x)}">{label}</a></td>', 1)

    def link(m):
        try:
            made = dt.date.fromisoformat(m.group(2)) in dailies
        except ValueError:
            made = False
        return f'<a href="{m.group(1)}">{m.group(3)}</a>' if made else m.group(0)
    return LATER_RE.sub(link, page)


def forget_daily_links(site_dir, day):
    """After `retro forget --date`: the week's page stops linking to that day's deleted page (the inverse of
    link_new_dailies, so the links come back if the day is made again). → the weekly file name when changed."""
    name = page_file("weekly", week_days(day)[0])
    path = os.path.join(site_dir, name)
    try:
        with open(path, encoding="utf-8") as f:
            page = f.read()
    except OSError:
        return None
    href, label = page_file("daily", day), f"{day:%m/%d} ({WEEKDAYS[day.weekday()]})"
    new = page.replace(f'<td><a href="{href}">{label}</a></td>', f"<td>{label}</td>")
    new = re.sub(rf'<a href="({re.escape(href)}(?:#\w+)?)">(.*?)</a>', r'<span data-link="\1">\2</span>', new)
    if new == page:
        return None
    write_page(path, new, quiet=True)
    return name


def write_index(site_dir, cache_dir, pages):
    """index.html: newest week first — its weekly page, then each day with the saved one-liner and counts."""
    path = os.path.join(site_dir, "index.html")
    try:
        with open(path, encoding="utf-8") as f:
            if GENERATOR not in f.read():
                return  # someone else's index.html (render.py --out can point anywhere)
    except OSError:
        pass
    weeks = sorted({week_days(d)[0] for d in pages["daily"]} | pages["weekly"], reverse=True)
    parts = [page_head("회고 목록", "🗂", "회고 목록",
                       [("일간", f"{len(pages['daily'])}개"), ("주간", f"{len(pages['weekly'])}개")])]
    for monday in weeks:
        days = week_days(monday)
        if monday in pages["weekly"]:
            parts.append(f'<h2><a href="{page_file("weekly", monday)}">{week_span(days)} 주간 회고</a></h2>')
        else:
            parts.append(f'<h2>{week_span(days)} <span class="sub">· 주간 페이지 없음 (retro week --date {monday})</span></h2>')
        saved = read_cache(cache_path(cache_dir, "weekly", monday))
        if saved and saved["summary"].get("one_line"):
            parts.append(f'<p class="sub">{esc(saved["summary"]["one_line"])}</p>')
        rows = []
        for d in reversed(days):
            if d not in pages["daily"]:
                continue
            saved = read_entry(cache_path(cache_dir, "daily", d)) or {}
            s, n = saved.get("summary") or {}, saved.get("numbers") or {}
            rows.append(f'<tr><td><a href="{page_file("daily", d)}">{d:%m/%d} ({WEEKDAYS[d.weekday()]})</a></td>'
                        f'<td>{esc(s.get("one_line", ""))}</td><td class="num">{esc(n.get("prompts", ""))}</td>'
                        f'<td class="num">{esc(n.get("commits", ""))}</td></tr>')
        if rows:
            parts.append('<table><tr><th>날짜</th><th>한 줄 요약</th><th class="num">내 지시</th><th class="num">커밋</th></tr>'
                         + "".join(rows) + "</table>")
    if not weeks:
        parts.append('<p class="sub">아직 페이지가 없습니다.</p>')
    parts.append('<p class="foot">retro를 실행할 때마다 새로 만듭니다 · 한 줄 요약은 저장된 요약에서, 숫자는 마지막으로 만든 페이지에서</p>'
                 '</main></body></html>')
    write_page(path, "".join(parts), quiet=True)


def update_site(site_dir, cache_dir):
    pages = site_pages(site_dir)
    refresh_navs(site_dir, pages)
    write_index(site_dir, cache_dir, pages)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("events", nargs="+", help="events.jsonl files (one per machine)")
    p.add_argument("--date", help="YYYY-MM-DD (default: today)")
    p.add_argument("--week", action="store_true", help="weekly page for the Mon–Sun week containing --date")
    p.add_argument("--llm", default="auto", choices=["auto", "api", "claude", "none", "cached"],
                   help="summary backend (auto: API key → claude CLI → numbers only; cached: the saved summary, no LLM call)")
    p.add_argument("--no-llm", action="store_true", help="same as --llm none")
    p.add_argument("--out", help="output HTML path (default: retro_out/daily-DATE.html or weekly-MONDAY.html)")
    p.add_argument("--refresh", action="store_true", help="summarize again even if the saved summary matches the logs")
    p.add_argument("--cache-dir", help="where summaries are saved and reused (default: the folder of --out)")
    p.add_argument("--off", default="", help="comma-separated sources turned off, shown in the page's 관측 범위")
    p.add_argument("--failed", action="append", default=[], help="a source this run could not collect (repeatable)")
    p.add_argument("--preview", action="store_true",
                   help="print exactly what the summary would send, and to where; no LLM call, nothing written")
    args = p.parse_args(argv)
    COLLECT_FAILURES[:] = args.failed
    off = [x for x in args.off.split(",") if x]

    all_events = load(args.events)
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    choice = "none" if args.no_llm else args.llm
    if args.week:
        days = week_days(day)
        by_day = split_days(all_events, days)
        if not any(by_day.values()):
            print(f"no events in the week of {days[0]}", file=sys.stderr)
            return 1
        out = args.out or os.path.join("retro_out", f"weekly-{days[0]}.html")
        site = os.path.dirname(out) or "."
        cache_dir = args.cache_dir or site
        stats = week_stats(by_day)
        prompt = build_week_prompt(days, by_day, stats, saved_dailies(days, by_day, stats, cache_dir))
        if args.preview:
            print(preview_text(f"{days[0]} 주간", prompt, WEEK_SYSTEM, WEEK_SCHEMA, WEEK_TASK, choice,
                               cache_path(cache_dir, "weekly", days[0]), args.refresh))
            return 0
        numbers = {"prompts": stats["prompts"], "commits": stats["commits"]}
        summary, note, stale = cached_summary(choice, prompt, WEEK_SYSTEM, WEEK_SCHEMA, WEEK_TASK,
                                              cache_path(cache_dir, "weekly", days[0]), args.refresh, numbers)
        save_numbers(cache_path(cache_dir, "weekly", days[0]), numbers)
        events = [e for evs in by_day.values() for e in evs]
        before = week_days(days[0] - dt.timedelta(days=7))
        prev_events = [e for e in all_events if before[0] <= e["ts"].date() <= before[-1]]  # only when it was loaded
        pages = site_pages(site)
        write_page(out, render_week(days, stats, summary, events, nav=page_nav("weekly", days[0], pages), note=note,
                                    day_links={d: page_file("daily", d) for d in days if d in pages["daily"]},
                                    prev_events=prev_events or None, off=off, stale=stale, notes_dir=cache_dir))
        update_site(site, cache_dir)
        return 0

    events = [e for e in all_events if e["ts"].date() == day]
    if not events:
        print(f"no events on {day}", file=sys.stderr)
        return 1
    out = args.out or os.path.join("retro_out", f"daily-{day}.html")
    site = os.path.dirname(out) or "."
    cache_dir = args.cache_dir or site
    stats = day_stats(events)
    if args.preview:
        print(preview_text(f"{day} 일간", build_prompt(day, events, stats), SYSTEM, SUMMARY_SCHEMA, DAILY_TASK, choice,
                           cache_path(cache_dir, "daily", day), args.refresh))
        return 0
    numbers = {"prompts": stats["prompts"], "commits": stats["commits"]}
    summary, note, stale = cached_summary(choice, build_prompt(day, events, stats), SYSTEM, SUMMARY_SCHEMA, DAILY_TASK,
                                          cache_path(cache_dir, "daily", day), args.refresh, numbers)
    save_numbers(cache_path(cache_dir, "daily", day), numbers)
    write_page(out, render_page(day, stats, summary, all_events, nav=page_nav("daily", day, site_pages(site)), note=note,
                                off=off, stale=stale, notes_dir=cache_dir))
    update_site(site, cache_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
