#!/usr/bin/env python3
"""Render a Notion-style daily (or weekly) retrospective page from collected events.

  python3 retro/render.py --date 2026-09-23 retro_out/events.jsonl retro_out/gpu.jsonl
  python3 retro/render.py --week --date 2026-09-23 retro_out/events.jsonl   # Mon–Sun week of that day

Inputs are one or more events.jsonl files from collect.py (merge machines by
passing several). Numbers (hours, counts, commits) are computed here from the
labels; the narrative parts (summary, done, decisions, blockers, tomorrow,
activity mix) come from one Claude call. --no-llm renders the numbers only.

The summary is saved next to the page (summary-daily-DATE.json /
summary-weekly-MONDAY.json) and reused while the logs stay the same, so a
re-render costs no LLM call; --refresh summarizes again anyway. Each run also
rewrites index.html (every page, newest first) and the ← · → rows at the top
of the pages in that folder.

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
    return {
        "prompts": len(prompts),
        "prompts_by_tool": Counter(e["source"] for e in prompts),
        "agent": sum(1 for e in events if e["actor"] == "agent"),
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
SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["one_line", "done", "decisions", "blockers", "tomorrow", "activity_mix", "project_labels"],
    "properties": {
        "one_line": {"type": "string", "description": "하루를 한 문장으로. 결과물 중심."},
        "done": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["time", "project", "result", "evidence"],
            "properties": {
                "time": {"type": "string", "description": "HH:MM 또는 HH:MM–HH:MM"},
                "project": {"type": "string"},
                "result": {"type": "string", "description": "무엇을 만들었거나 끝냈는지. 과정이 아니라 결과."},
                "evidence": {"type": "string", "description": "근거 출처(도구 이름)"},
            }}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "tomorrow": {"type": "array", "items": {"type": "string"}},
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

SYSTEM = """당신은 사용자의 하루 활동 로그를 읽고 일간 회고를 쓰는 비서입니다.
로그는 사용자가 AI 도구(Claude Code, Codex)에 직접 입력한 지시, git 커밋, 웹 방문, 메신저 기록입니다.
- '무엇을 했나'보다 '무엇이 결과로 남았나'를 적으세요. 지시 문장을 그대로 옮기지 말고 결과로 바꿔 쓰세요.
- done은 시간순 5~10개, decisions는 사용자가 명시적으로 확정·승인·방향 전환한 것만, blockers는 반복된 문제나 대기·비용 이슈.
- tomorrow는 로그에서 이어질 것이 분명한 일만. 추측으로 채우지 마세요.
- activity_mix의 percent 합은 100.
- 로그에 없는 사실을 만들지 마세요. 짧고 구체적인 한국어로 쓰세요."""
DAILY_TASK = "표준 입력의 로그로 일간 회고를 작성하세요."

WEEK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["one_line", "highlights", "decisions", "blockers", "next_week", "activity_mix", "project_labels"],
    "properties": {
        "one_line": {"type": "string", "description": "한 주를 한 문장으로. 결과물 중심."},
        "highlights": {"type": "array", "description": "이번 주 주요 결과 5~10개", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["days", "project", "result"],
            "properties": {
                "days": {"type": "string", "description": "요일 (예: 화, 수–목)"},
                "project": {"type": "string"},
                "result": {"type": "string", "description": "무엇을 만들었거나 끝냈는지. 과정이 아니라 결과."},
            }}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "description": "여러 날 반복된 문제·대기·비용 이슈와 패턴", "items": {"type": "string"}},
        "next_week": {"type": "array", "items": {"type": "string"}},
        "activity_mix": SUMMARY_SCHEMA["properties"]["activity_mix"],
        "project_labels": SUMMARY_SCHEMA["properties"]["project_labels"],
    },
}

WEEK_SYSTEM = """당신은 사용자의 한 주 활동 로그를 읽고 주간 회고를 쓰는 비서입니다.
로그는 사용자가 AI 도구(Claude Code, Codex 등)에 직접 입력한 지시와 git 커밋을 요일별로 줄인 것입니다. 긴 날은 하루 전체에서 고르게 뽑은 일부만 있고, 요일별 건수는 로그 앞의 숫자가 정확합니다.
- '무엇을 했나'보다 '무엇이 결과로 남았나'를 적으세요. 지시 문장을 그대로 옮기지 말고 결과로 바꿔 쓰세요.
- highlights는 요일순 5~10개, decisions는 사용자가 명시적으로 확정·승인·방향 전환한 것만, blockers는 여러 날 반복된 문제나 대기·비용 이슈, 되풀이되는 작업 패턴.
- next_week는 로그에서 이어질 것이 분명한 일만. 추측으로 채우지 마세요.
- activity_mix의 percent 합은 100.
- 로그에 없는 사실을 만들지 마세요. 짧고 구체적인 한국어로 쓰세요."""
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


def build_prompt(day, events, stats):
    lines = []
    for e in events:
        if e["actor"] != "human":
            continue
        proj = f"[{e['project']}] " if e["project"] else ""
        lines.append(f"{e['ts']:%H:%M} {e['source']} {proj}{e['text']}")
    facts = (f"날짜: {day}\n직접 입력한 AI 지시 {stats['prompts']}건, 에이전트 간 지시 {stats['agent']}건, "
             f"커밋 {stats['commits']}건(+{stats['add']}/-{stats['dele']}), 웹 방문 {stats['web']}건")
    return f"{facts}\n\n<log>\n" + "\n".join(lines) + "\n</log>"


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


def read_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            entry = json.load(f)
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) and isinstance(entry.get("summary"), dict) else None


def write_file(path, text):
    """Write via a temp file: a crash or a second retro running at the same time never leaves half a file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_cache(path, entry):
    write_file(path, json.dumps(entry, ensure_ascii=False, indent=1))


def made_at(entry):
    try:
        return f"{dt.datetime.fromisoformat(entry['created']):%m/%d %H:%M}"
    except (KeyError, TypeError, ValueError):
        return "이전"


def cached_summary(choice, prompt, system, schema, task, path, refresh=False, numbers=None):
    """get_summary(), except the same logs are never summarized twice.

    Returns (summary, footer note). The saved summary is reused while the prompt
    hash matches; new logs or --refresh summarize again, and if that fails the
    older summary is shown (the footer says so) instead of numbers only.
    numbers: a few counts saved alongside, for index.html.
    """
    if choice == "none" or not path:
        return get_summary(choice, prompt, system, schema, task), ""
    digest = prompt_hash(prompt, system, schema)
    saved = read_cache(path)
    if saved and saved.get("prompt_sha256") == digest and not refresh:
        print("· 요약: 캐시 재사용", file=sys.stderr)
        return saved["summary"], f"요약은 Claude가 작성 ({made_at(saved)}에 만든 요약 재사용)"
    summary = get_summary(choice, prompt, system, schema, task)
    if summary:
        write_cache(path, {"prompt_sha256": digest, "created": dt.datetime.now().isoformat(timespec="seconds"),
                           "backend": LAST_BACKEND, "summary": summary, "numbers": numbers or {}})
        return summary, ""
    if saved:
        print("· 새 요약 실패 → 이전 요약 표시", file=sys.stderr)
        return saved["summary"], (f"이전 요약 표시 — {made_at(saved)}에 만든 요약이라 그 뒤 로그는 빠져 있을 수 있습니다. "
                                  f"{LAST_FAILURE}")
    return None, ""


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
h2{font-size:19px;margin:36px 0 10px;padding-bottom:4px;border-bottom:1px solid var(--line)}
.props{display:grid;grid-template-columns:110px 1fr;gap:6px 12px;margin:14px 0 8px;font-size:14px}.props dt{color:var(--muted)}.props dd{margin:0}
.chip{display:inline-block;background:var(--chip);border-radius:4px;padding:0 7px;margin:0 4px 4px 0;font-size:13px}
.callout{background:var(--soft);border-radius:6px;padding:14px 16px;display:flex;gap:10px;margin:14px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
.kpi{border:1px solid var(--line);border-radius:8px;padding:10px 12px}.kpi b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.kpi span{color:var(--muted);font-size:12.5px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{border:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top}th{background:var(--soft);font-weight:600;color:var(--muted);font-size:12.5px}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.chart{width:100%;height:auto}.grid{stroke:var(--line)}.axis{fill:var(--muted);font-size:11px}.axis.strong{fill:var(--fg);font-weight:700}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12.5px;color:var(--muted);margin:4px 0 8px}.lg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.hbar{display:grid;grid-template-columns:180px 1fr 40px;align-items:center;gap:10px;font-size:13.5px;margin:6px 0}.hl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.track{background:var(--soft);border-radius:3px;height:10px}.fill{display:block;height:10px;border-radius:3px}.hn{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.mix{display:flex;height:14px;border-radius:4px;overflow:hidden;margin:6px 0}.mix span{display:block}
ul.todo{list-style:none;padding:0}ul.todo li::before{content:"☐ ";color:var(--muted)}
.q{border-left:3px solid var(--fg);padding:2px 12px;margin:10px 0}.q p{margin:2px 0;color:var(--muted)}
.sub{color:var(--muted);font-size:13px}.foot{margin-top:40px;color:var(--muted);font-size:12.5px}
a{color:var(--fg);text-underline-offset:2px}.pnav{font-size:13px;color:var(--muted);margin:0 0 20px}.pnav .off{opacity:.45}
@media (max-width:560px){.hbar{grid-template-columns:110px 1fr 32px}.props{grid-template-columns:90px 1fr}}
"""
MIX_COLORS = ["#2f6fde", "#d9773b", "#8a63d2", "#2e9d6a", "#d4b24c", "#c24f6b", "#4aa3b5", "#9b9b9b"]


def labeler(summary):
    labels = {p["raw"]: p["label"] for p in (summary or {}).get("project_labels", [])}
    return lambda raw: labels.get(raw, raw)


def merge_projects(rows, name):
    merged = Counter()
    for k, n in rows:  # several folders often belong to one project once labeled
        merged[name(k)] += n
    return merged.most_common()


def chips(items):
    return "".join(f'<span class="chip">{esc(x)}</span>' for x in items)


def page_head(tab, icon, title, props, nav=""):
    """<head>, the ← · → nav row, icon, title and the Notion-style property list [(name, html)]."""
    rows = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in props)
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">{GENERATOR}<title>{esc(tab)}</title>'
            f'<style>{CSS}</style></head>\n<body><main>{nav}<div class="icon">{icon}</div><h1>{esc(title)}</h1>\n'
            f'<dl class="props">{rows}</dl>')


def callout(s):
    if not s.get("one_line"):
        return ""
    return f'<div class="callout"><span>💡</span><div><b>한 줄 요약</b><br>{esc(s["one_line"])}</div></div>'


def kpis(stats, extra=()):
    """The KPI row: my AI prompts by tool, agent delegation, commits, web visits (+ extra (value, label))."""
    tool = stats["prompts_by_tool"]
    cells = [(stats["prompts"], "내가 AI에 준 지시<br>" + " · ".join(f"{TOOL_NAMES.get(k, k)} {v}" for k, v in tool.most_common())),
             (stats["agent"], "에이전트 간 위임"),
             (stats["commits"], f"커밋<br>+{stats['add']:,} / −{stats['dele']:,}줄"),
             (stats["web"], "웹 페이지 방문")] + list(extra)
    if stats["people"]:
        cells.append((f"{len(stats['people'])}명", "대화한 사람"))
    return '<div class="kpis">\n' + "\n".join(f'<div class="kpi"><b>{n}</b><span>{l}</span></div>' for n, l in cells) + "\n</div>"


def legend(keys):
    return '<div class="legend">' + "".join(f'<span class="lg"><i style="background:var({v})"></i>{l}</span>'
                                            for k, l, v in SERIES if k in keys) + "</div>"


def mix_bar(mix):
    seg = "".join(f'<span style="width:{m["percent"]}%;background:{MIX_COLORS[i % 8]}" title="{esc(m["type"])} {m["percent"]}%"></span>'
                  for i, m in enumerate(mix))
    lg = "".join(f'<span class="lg"><i style="background:{MIX_COLORS[i % 8]}"></i>{esc(m["type"])} {m["percent"]}%</span>'
                 for i, m in enumerate(mix))
    return f'<div class="sub" style="margin-top:14px">활동 유형 (AI 추정)</div><div class="mix">{seg}</div><div class="legend">{lg}</div>'


def where(stats, s, name):
    """Projects bar list, the AI-estimated activity mix, most visited sites."""
    out = "<h2>🗂 어디에 썼나</h2>" + bars(merge_projects(stats["projects"], name), "--c-codex")
    if s.get("activity_mix"):
        out += mix_bar(s["activity_mix"])
    if stats["sites"]:
        out += '<div class="sub" style="margin-top:14px">많이 본 사이트</div>' + bars(stats["sites"], "--c-web")
    return out


def lists(s, heads, cls=""):
    return "".join(f"<h2>{head}</h2><ul{cls}>" + "".join(f"<li>{esc(x)}</li>" for x in s[key]) + "</ul>"
                   for key, head in heads if s.get(key))


def people_and_ai(stats):
    out = ""
    if stats["people"]:
        rows = "".join(f'<tr><td>{esc(p)}</td><td class="num">{n}</td></tr>' for p, n in stats["people"])
        out += f"<h2>👥 사람</h2><table><tr><th>누구</th><th class=\"num\">메시지</th></tr>{rows}</table>"
    ai_rows = "".join(f'<tr><td>{TOOL_NAMES.get(k, k)}</td><td class="num">{v}</td></tr>'
                      for k, v in stats["prompts_by_tool"].most_common())
    if ai_rows:
        out += (f'<h2>🤖 AI 사용</h2><table><tr><th>도구</th><th class="num">내 지시</th></tr>{ai_rows}'
                f'<tr><td>에이전트 간 위임</td><td class="num">{stats["agent"]}</td></tr></table>')
    return out


def reflection(question):
    return f'<h2>✍️ 회고 한 줄</h2><div class="q"><b>{question}</b><p>(직접 작성)</p></div>'


def foot(summary, note=""):
    """note: from cached_summary — the summary was reused, or an older one is shown."""
    note = (f"숫자는 로그에서 계산 · {note or '요약은 Claude가 작성'}" if summary
            else f"숫자만 표시 — {LAST_FAILURE or '요약 없음'}")
    return f'<p class="foot">{esc(note)}</p></main></body></html>'


def render_page(day, stats, summary, all_events, nav="", note=""):
    s = summary or {}
    name = labeler(summary)
    title = f"{day.year}년 {day.month}월 {day.day}일 ({WEEKDAYS[day.weekday()]}) 일간 회고"
    span = (f"{stats['first']:%H:%M} – {stats['last']:%H:%M}" if stats["first"] else "–")
    projects = merge_projects(stats["projects"], name)
    parts = [page_head(f"일간 회고 {day}", "🗓", title, [
        ("프로젝트", chips(p for p, _ in projects[:4]) or "–"), ("활동 시간", span),
        ("데이터 출처", chips(stats["sources"])), ("기기", esc(", ".join(stats["hosts"])))], nav), callout(s)]
    hour_keys = {k for k, _, _ in SERIES if any(stats["hours"][h].get(k) for h in stats["hours"])}
    parts.append("<h2>📊 오늘의 숫자</h2>" + kpis(stats)
                 + f"<h2>⏱ 시간대별 활동</h2>{legend(hour_keys)}{hour_chart(stats['hours'])}")
    parts.append(where(stats, s, name))
    if s.get("done"):
        rows = "".join(f'<tr><td>{esc(d["time"])}</td><td>{esc(name(d["project"]))}</td><td>{esc(d["result"])}</td><td>{esc(d["evidence"])}</td></tr>'
                       for d in s["done"])
        parts.append(f"<h2>✅ 오늘 한 일</h2><table><tr><th>시간</th><th>프로젝트</th><th>결과물</th><th>근거</th></tr>{rows}</table>")
    parts.append(lists(s, (("decisions", "🧭 결정한 것"), ("blockers", "🚧 막힌 것 · 리스크"))))
    parts.append(people_and_ai(stats))
    parts.append(lists(s, (("tomorrow", "➡️ 내일로"),), ' class="todo"'))
    parts.append(reflection("오늘 가장 의미 있었던 일은?"))
    parts.append(f"<h2>📈 최근 7일</h2>{legend(AI_KEYS)}{week_chart(all_events, day)}")
    parts.append(foot(summary, note))
    return "".join(parts)


WEEK_KEYS = AI_KEYS | {"git"}  # the weekly chart also stacks commits


def week_span(days):
    """2026년 9월 21일 – 27일"""
    a, b = days[0], days[-1]
    end = f"{b.day}일" if b.month == a.month else f"{b.month}월 {b.day}일"
    return f"{a.year}년 {a.month}월 {a.day}일 – {end}"


def render_week(days, stats, summary, events, today=None, nav="", note="", day_links=None):
    """Weekly page: same look as the daily one; days after today are left empty.

    day_links: {day: href} of the daily pages that exist; the 날짜별 table links to them.
    """
    s = summary or {}
    name = labeler(summary)
    today = today or dt.date.today()
    day_links = day_links or {}
    a, b = days[0], days[-1]
    title = f"{week_span(days)} 주간 회고"
    projects = merge_projects(stats["projects"], name)
    parts = [page_head(f"주간 회고 {a}", "📅", title, [
        ("프로젝트", chips(p for p, _ in projects[:4]) or "–"), ("기간", f"{a:%m/%d} (월) – {b:%m/%d} (일)"),
        ("데이터 출처", chips(stats["sources"])), ("기기", esc(", ".join(stats["hosts"])))], nav), callout(s)]
    parts.append("<h2>📊 이번 주 숫자</h2>" + kpis(stats, [(f"{stats['active_days']}일", "활동한 날 (7일 중)")]))
    parts.append(f"<h2>📈 요일별 활동</h2>{legend(WEEK_KEYS)}"
                 + week_chart(events, today, days=days, keys=WEEK_KEYS, label="요일별 활동"))
    parts.append(where(stats, s, name))
    rows = []
    for d in days:
        ds = stats["per_day"][d]
        label = f"{d:%m/%d} ({WEEKDAYS[d.weekday()]})"
        if d > today:
            rows.append(f'<tr><td class="sub">{label}</td><td></td><td></td><td></td><td></td></tr>')
            continue
        top = merge_projects(ds["projects"], name)
        span = f"{ds['first']:%H:%M}–{ds['last']:%H:%M}" if ds["first"] else "–"
        if d in day_links:
            label = f'<a href="{esc(day_links[d])}">{label}</a>'
        rows.append(f'<tr><td>{label}</td><td class="num">{ds["prompts"]}</td><td class="num">{ds["commits"]}</td>'
                    f'<td>{esc(top[0][0]) if top else "–"}</td><td>{span}</td></tr>')
    parts.append('<h2>📅 날짜별</h2><table><tr><th>날짜</th><th class="num">내 지시</th><th class="num">커밋</th>'
                 f'<th>주요 프로젝트</th><th>활동 시간</th></tr>{"".join(rows)}</table>')
    if s.get("highlights"):
        rows = "".join(f'<tr><td>{esc(h["days"])}</td><td>{esc(name(h["project"]))}</td><td>{esc(h["result"])}</td></tr>'
                       for h in s["highlights"])
        parts.append(f"<h2>✅ 이번 주 한 일</h2><table><tr><th>요일</th><th>프로젝트</th><th>결과물</th></tr>{rows}</table>")
    parts.append(lists(s, (("decisions", "🧭 결정한 것"), ("blockers", "🔁 반복된 문제 · 패턴"))))
    parts.append(people_and_ai(stats))
    parts.append(lists(s, (("next_week", "➡️ 다음 주로"),), ' class="todo"'))
    parts.append(reflection("이번 주 가장 의미 있었던 일은?"))
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
            if kind == "weekly":  # 날짜별 table: link days whose daily page appeared later
                for x in week_days(d):
                    label = f"{x:%m/%d} ({WEEKDAYS[x.weekday()]})"
                    if x in pages["daily"]:
                        new = new.replace(f"<td>{label}</td>", f'<td><a href="{page_file("daily", x)}">{label}</a></td>', 1)
            if new != page:
                write_page(path, new, quiet=True)


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
            saved = read_cache(cache_path(cache_dir, "daily", d)) or {}
            s, n = saved.get("summary", {}), saved.get("numbers", {})
            rows.append(f'<tr><td><a href="{page_file("daily", d)}">{d:%m/%d} ({WEEKDAYS[d.weekday()]})</a></td>'
                        f'<td>{esc(s.get("one_line", ""))}</td><td class="num">{esc(n.get("prompts", ""))}</td>'
                        f'<td class="num">{esc(n.get("commits", ""))}</td></tr>')
        if rows:
            parts.append('<table><tr><th>날짜</th><th>한 줄 요약</th><th class="num">내 지시</th><th class="num">커밋</th></tr>'
                         + "".join(rows) + "</table>")
    if not weeks:
        parts.append('<p class="sub">아직 페이지가 없습니다.</p>')
    parts.append('<p class="foot">retro를 실행할 때마다 새로 만듭니다 · 한 줄 요약과 숫자는 저장된 요약에서</p></main></body></html>')
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
    p.add_argument("--llm", default="auto", choices=["auto", "api", "claude", "none"],
                   help="summary backend (auto: API key → claude CLI → numbers only)")
    p.add_argument("--no-llm", action="store_true", help="same as --llm none")
    p.add_argument("--out", help="output HTML path (default: retro_out/daily-DATE.html or weekly-MONDAY.html)")
    p.add_argument("--refresh", action="store_true", help="summarize again even if the saved summary matches the logs")
    p.add_argument("--cache-dir", help="where summaries are saved and reused (default: the folder of --out)")
    args = p.parse_args(argv)

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
        summary, note = cached_summary(choice, prompt, WEEK_SYSTEM, WEEK_SCHEMA, WEEK_TASK,
                                       cache_path(cache_dir, "weekly", days[0]), args.refresh,
                                       {"prompts": stats["prompts"], "commits": stats["commits"]})
        events = [e for evs in by_day.values() for e in evs]
        pages = site_pages(site)
        write_page(out, render_week(days, stats, summary, events, nav=page_nav("weekly", days[0], pages), note=note,
                                    day_links={d: page_file("daily", d) for d in days if d in pages["daily"]}))
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
    summary, note = cached_summary(choice, build_prompt(day, events, stats), SYSTEM, SUMMARY_SCHEMA, DAILY_TASK,
                                   cache_path(cache_dir, "daily", day), args.refresh,
                                   {"prompts": stats["prompts"], "commits": stats["commits"]})
    write_page(out, render_page(day, stats, summary, all_events, nav=page_nav("daily", day, site_pages(site)), note=note))
    update_site(site, cache_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
