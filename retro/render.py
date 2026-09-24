#!/usr/bin/env python3
"""Render a Notion-style daily retrospective page from collected events.

  python3 retro/render.py --date 2026-09-23 retro_out/events.jsonl retro_out/gpu.jsonl

Inputs are one or more events.jsonl files from collect.py (merge machines by
passing several). Numbers (hours, counts, commits) are computed here from the
labels; the narrative parts (summary, done, decisions, blockers, tomorrow,
activity mix) come from one Claude call. --no-llm renders the numbers only.

Needs: pip install anthropic, and ANTHROPIC_API_KEY (or `ant auth login`).
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
from collections import Counter, defaultdict

MODEL = "claude-opus-5"
ACTIVITY_TYPES = ["기획", "제작", "QA·검수", "개발", "리서치", "소통", "행정", "개인"]
SERIES = [  # (key, label, css var) — stacking order in charts
    ("codex", "나 → Codex", "--c-codex"),
    ("claude", "나 → Claude", "--c-claude"),
    ("agent", "에이전트 위임", "--c-agent"),
    ("chrome", "웹 탐색", "--c-web"),
    ("git", "커밋", "--c-git"),
    ("other", "기타", "--c-other"),
]
SERIES_KEYS = {k for k, _, _ in SERIES}
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
def day_stats(events):
    human = [e for e in events if e["actor"] == "human"]
    prompts = [e for e in human if e["source"] in ("claude", "codex")]
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
        "projects": Counter(e["project"] for e in prompts + commits if e["project"]).most_common(6),
        "sites": sites.most_common(6),
        "people": people.most_common(8),
        "first": min(active) if active else None,
        "last": max(active) if active else None,
        "hosts": sorted({e.get("host", "") for e in events if e.get("host")}),
        "sources": sorted({e["source"] for e in events}),
    }


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


def summarize(day, events, stats):
    import anthropic

    lines = []
    for e in events:
        if e["actor"] != "human":
            continue
        proj = f"[{e['project']}] " if e["project"] else ""
        lines.append(f"{e['ts']:%H:%M} {e['source']} {proj}{e['text']}")
    facts = (f"날짜: {day}\n직접 입력한 AI 지시 {stats['prompts']}건, 에이전트 간 지시 {stats['agent']}건, "
             f"커밋 {stats['commits']}건(+{stats['add']}/-{stats['dele']}), 웹 방문 {stats['web']}건")
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
        messages=[{"role": "user", "content": f"{facts}\n\n<log>\n" + "\n".join(lines) + "\n</log>"}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined to summarize this log")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("summary was cut off (max_tokens)")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


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


def week_chart(all_events, day):
    days = [day - dt.timedelta(days=i) for i in range(6, -1, -1)]
    per = {d: Counter() for d in days}
    for e in all_events:
        d = e["ts"].date()
        if d in per and (e["actor"] == "agent" or e["source"] in ("claude", "codex")):
            per[d][series_key(e)] += 1
    mx = max((sum(c.values()) for c in per.values()), default=0) or 1
    W, H, bw = 640, 110, 44
    step = W / len(days)
    out = [f'<svg viewBox="0 0 {W} {H + 34}" class="chart" role="img" aria-label="최근 7일">']
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
--c-codex:#2f6fde;--c-claude:#d9773b;--c-agent:#8a63d2;--c-web:#b9b9b4;--c-git:#2e9d6a;--c-other:#d4b24c}
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
@media (max-width:560px){.hbar{grid-template-columns:110px 1fr 32px}.props{grid-template-columns:90px 1fr}}
"""
MIX_COLORS = ["#2f6fde", "#d9773b", "#8a63d2", "#2e9d6a", "#d4b24c", "#c24f6b", "#4aa3b5", "#9b9b9b"]


def render_page(day, stats, summary, all_events):
    s = summary or {}
    labels = {p["raw"]: p["label"] for p in s.get("project_labels", [])}
    name = lambda raw: labels.get(raw, raw)  # noqa: E731
    title = f"{day.year}년 {day.month}월 {day.day}일 ({WEEKDAYS[day.weekday()]}) 일간 회고"
    span = (f"{stats['first']:%H:%M} – {stats['last']:%H:%M}" if stats["first"] else "–")
    merged = Counter()
    for k, n in stats["projects"]:  # several folders often belong to one project once labeled
        merged[name(k)] += n
    projects = merged.most_common()
    proj_chips = "".join(f'<span class="chip">{esc(p)}</span>' for p, _ in projects[:4])
    src_chips = "".join(f'<span class="chip">{esc(x)}</span>' for x in stats["sources"])
    legend = "".join(f'<span class="lg"><i style="background:var({v})"></i>{l}</span>'
                     for k, l, v in SERIES if any(stats["hours"][h].get(k) for h in stats["hours"]))
    tool = stats["prompts_by_tool"]
    parts = [f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>일간 회고 {day}</title><style>{CSS}</style></head>
<body><main><div class="icon">🗓</div><h1>{esc(title)}</h1>
<dl class="props"><dt>프로젝트</dt><dd>{proj_chips or '–'}</dd><dt>활동 시간</dt><dd>{span}</dd>
<dt>데이터 출처</dt><dd>{src_chips}</dd><dt>기기</dt><dd>{esc(', '.join(stats['hosts']))}</dd></dl>"""]
    if s.get("one_line"):
        parts.append(f'<div class="callout"><span>💡</span><div><b>한 줄 요약</b><br>{esc(s["one_line"])}</div></div>')
    parts.append(f"""<h2>📊 오늘의 숫자</h2><div class="kpis">
<div class="kpi"><b>{stats['prompts']}</b><span>내가 AI에 준 지시<br>{' · '.join(f'{k.title()} {v}' for k, v in tool.most_common())}</span></div>
<div class="kpi"><b>{stats['agent']}</b><span>에이전트 간 위임</span></div>
<div class="kpi"><b>{stats['commits']}</b><span>커밋<br>+{stats['add']:,} / −{stats['dele']:,}줄</span></div>
<div class="kpi"><b>{stats['web']}</b><span>웹 페이지 방문</span></div>
{f'<div class="kpi"><b>{len(stats["people"])}명</b><span>대화한 사람</span></div>' if stats['people'] else ''}
</div><h2>⏱ 시간대별 활동</h2><div class="legend">{legend}</div>{hour_chart(stats['hours'])}""")
    parts.append("<h2>🗂 어디에 썼나</h2>" + bars(projects, "--c-codex"))
    if s.get("activity_mix"):
        mix = s["activity_mix"]
        seg = "".join(f'<span style="width:{m["percent"]}%;background:{MIX_COLORS[i % 8]}" title="{esc(m["type"])} {m["percent"]}%"></span>'
                      for i, m in enumerate(mix))
        lg = "".join(f'<span class="lg"><i style="background:{MIX_COLORS[i % 8]}"></i>{esc(m["type"])} {m["percent"]}%</span>'
                     for i, m in enumerate(mix))
        parts.append(f'<div class="sub" style="margin-top:14px">활동 유형 (AI 추정)</div><div class="mix">{seg}</div><div class="legend">{lg}</div>')
    if stats["sites"]:
        parts.append('<div class="sub" style="margin-top:14px">많이 본 사이트</div>' + bars(stats["sites"], "--c-web"))
    if s.get("done"):
        rows = "".join(f'<tr><td>{esc(d["time"])}</td><td>{esc(name(d["project"]))}</td><td>{esc(d["result"])}</td><td>{esc(d["evidence"])}</td></tr>'
                       for d in s["done"])
        parts.append(f"<h2>✅ 오늘 한 일</h2><table><tr><th>시간</th><th>프로젝트</th><th>결과물</th><th>근거</th></tr>{rows}</table>")
    for key, head in (("decisions", "🧭 결정한 것"), ("blockers", "🚧 막힌 것 · 리스크")):
        if s.get(key):
            parts.append(f"<h2>{head}</h2><ul>" + "".join(f"<li>{esc(x)}</li>" for x in s[key]) + "</ul>")
    if stats["people"]:
        rows = "".join(f'<tr><td>{esc(p)}</td><td class="num">{n}</td></tr>' for p, n in stats["people"])
        parts.append(f"<h2>👥 사람</h2><table><tr><th>누구</th><th class=\"num\">메시지</th></tr>{rows}</table>")
    ai_rows = "".join(f'<tr><td>{k.title()}</td><td class="num">{v}</td></tr>' for k, v in tool.most_common())
    if ai_rows:
        parts.append(f'<h2>🤖 AI 사용</h2><table><tr><th>도구</th><th class="num">내 지시</th></tr>{ai_rows}'
                     f'<tr><td>에이전트 간 위임</td><td class="num">{stats["agent"]}</td></tr></table>')
    if s.get("tomorrow"):
        parts.append('<h2>➡️ 내일로</h2><ul class="todo">' + "".join(f"<li>{esc(x)}</li>" for x in s["tomorrow"]) + "</ul>")
    parts.append('<h2>✍️ 회고 한 줄</h2><div class="q"><b>오늘 가장 의미 있었던 일은?</b><p>(직접 작성)</p></div>')
    parts.append(f'<h2>📈 최근 7일</h2><div class="legend"><span class="lg"><i style="background:var(--c-codex)"></i>나 → Codex</span>'
                 f'<span class="lg"><i style="background:var(--c-claude)"></i>나 → Claude</span>'
                 f'<span class="lg"><i style="background:var(--c-agent)"></i>에이전트 위임</span></div>{week_chart(all_events, day)}')
    note = "숫자는 로그에서 계산 · 요약은 Claude가 작성" if summary else "숫자만 표시 (--no-llm 또는 요약 실패)"
    parts.append(f'<p class="foot">{note}</p></main></body></html>')
    return "".join(parts)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("events", nargs="+", help="events.jsonl files (one per machine)")
    p.add_argument("--date", help="YYYY-MM-DD (default: today)")
    p.add_argument("--no-llm", action="store_true", help="skip the Claude summary")
    p.add_argument("--out", help="output HTML path (default: retro_out/daily-DATE.html)")
    args = p.parse_args()

    all_events = load(args.events)
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    events = [e for e in all_events if e["ts"].date() == day]
    if not events:
        sys.exit(f"no events on {day}")
    stats = day_stats(events)

    summary = None
    if not args.no_llm:
        try:
            import anthropic
        except ImportError:
            anthropic = None
            print("anthropic SDK not installed (pip install anthropic) — rendering numbers only", file=sys.stderr)
        if anthropic:
            # the page is still useful without the summary, so every failure degrades to numbers only
            try:
                summary = summarize(day, events, stats)
            except anthropic.AuthenticationError:
                print("no valid API credentials (set ANTHROPIC_API_KEY or run `ant auth login`)", file=sys.stderr)
            except anthropic.RateLimitError:
                print("rate limited — try again in a minute", file=sys.stderr)
            except anthropic.APIConnectionError:
                print("network error reaching the API", file=sys.stderr)
            except anthropic.APIStatusError as e:
                print(f"API error {e.status_code}: {e.message}", file=sys.stderr)
            except (RuntimeError, ValueError) as e:  # refusal / truncation / bad JSON
                print(f"summary unusable: {e}", file=sys.stderr)

    out = args.out or os.path.join("retro_out", f"daily-{day}.html")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_page(day, stats, summary, all_events))
    print(f"wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
