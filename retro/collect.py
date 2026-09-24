#!/usr/bin/env python3
"""Collect local activity logs into one daily timeline for a retrospective.

Sources (all read-only, all local):
  - Claude Code transcripts   ~/.claude/projects/*/*.jsonl
  - Codex CLI sessions        ~/.codex/sessions/**/*.jsonl, ~/.codex/history.jsonl
  - git commits               repos found under --git-root (default: ~)
  - YouTube watch history     Google Takeout watch-history.json or .html (--youtube)

Output (default ./retro_out):
  events.jsonl   one normalized event per line
  timeline.md    per-day timeline, ready to paste into an LLM for summarizing

Python 3.9+, standard library only.
"""
import argparse
import datetime as dt
import glob
import html
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

LOCAL_TZ = dt.datetime.now().astimezone().tzinfo
MAX_TEXT = 200


def parse_ts(value):
    """Parse ISO string or epoch seconds/ms into an aware local datetime."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            if value > 1e12:
                value /= 1000
            return dt.datetime.fromtimestamp(value, dt.timezone.utc).astimezone(LOCAL_TZ)
        s = str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1)
        # Python <3.11 accepts only 3 or 6 fractional digits
        s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(LOCAL_TZ)
    except (ValueError, OverflowError, OSError):
        return None


def clip(text, n=MAX_TEXT):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def event(source, ts, text, project=""):
    return {"source": source, "ts": ts, "project": project, "text": clip(text)}


def read_jsonl(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def text_of(content):
    """Extract human-typed text from a message content field (str or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "input_text"):
                parts.append(block.get("text", ""))
        return " ".join(parts)
    return ""


def is_noise(text):
    t = text.strip()
    return (
        not t
        or t.startswith("<")  # system reminders, command wrappers, environment context
        or t.startswith("Caveat:")
        or t.startswith("[Request interrupted")
    )


# ---------------------------------------------------------------- Claude Code
def collect_claude(since, until):
    out = []
    root = os.path.expanduser("~/.claude/projects")
    for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
        if dt.datetime.fromtimestamp(os.path.getmtime(path), LOCAL_TZ) < since:
            continue
        for rec in read_jsonl(path):
            if rec.get("type") != "user" or rec.get("isSidechain") or rec.get("isMeta"):
                continue
            ts = parse_ts(rec.get("timestamp"))
            if not ts or not (since <= ts < until):
                continue
            text = text_of((rec.get("message") or {}).get("content"))
            if is_noise(text):
                continue
            project = os.path.basename(rec.get("cwd") or "") or os.path.basename(os.path.dirname(path))
            out.append(event("claude", ts, text, project))
    return out


# ---------------------------------------------------------------- Codex CLI
def collect_codex(since, until):
    out = []
    seen = set()
    root = os.path.expanduser("~/.codex")
    for path in glob.glob(os.path.join(root, "sessions", "**", "*.jsonl"), recursive=True):
        if dt.datetime.fromtimestamp(os.path.getmtime(path), LOCAL_TZ) < since:
            continue
        cwd = ""
        file_ts = None  # older formats only timestamp the first line
        for rec in read_jsonl(path):
            if file_ts is None:
                pl = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
                file_ts = parse_ts(rec.get("timestamp") or pl.get("timestamp"))
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else rec
            kind = rec.get("type")
            if kind == "session_meta" or "cwd" in payload:
                cwd = payload.get("cwd") or cwd
            text = ""
            if kind == "event_msg" and payload.get("type") == "user_message":
                text = payload.get("message", "")
            elif payload.get("type") == "message" and payload.get("role") == "user":
                text = text_of(payload.get("content"))
            if is_noise(text):
                continue
            ts = parse_ts(rec.get("timestamp") or payload.get("timestamp")) or file_ts
            if not ts or not (since <= ts < until):
                continue
            key = (ts.isoformat()[:16], clip(text, 60))
            if key in seen:
                continue
            seen.add(key)
            out.append(event("codex", ts, text, os.path.basename(cwd)))
    # history.jsonl covers prompts even when session files are gone
    for rec in read_jsonl(os.path.join(root, "history.jsonl")):
        ts = parse_ts(rec.get("ts"))
        text = rec.get("text", "")
        if not ts or not (since <= ts < until) or is_noise(text):
            continue
        key = (ts.isoformat()[:16], clip(text, 60))
        if key not in seen:
            seen.add(key)
            out.append(event("codex", ts, text))
    return out


# ---------------------------------------------------------------- git
SKIP_DIRS = {"node_modules", "Library", ".Trash", ".cache", "venv", ".venv", "dist", "build", "Pods"}


def find_repos(root, max_depth):
    root = os.path.expanduser(root)
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, _ in os.walk(root):
        if ".git" in dirnames:
            yield dirpath
            dirnames[:] = []  # don't descend into a repo
            continue
        if dirpath.count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]


def collect_git(since, until, roots, max_depth, author):
    out = []
    for root in roots:
        for repo in find_repos(root, max_depth):
            cmd = [
                "git", "-C", repo, "log", "--all", "--no-merges",
                f"--since={since.isoformat()}", f"--until={until.isoformat()}",
                "--pretty=format:%x1e%aI %ae%x1f%s", "--shortstat",
            ]
            if author:
                cmd.append(f"--author={author}")
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                continue
            for chunk in res.stdout.split("\x1e"):
                if "[bot]" in chunk.split("\x1f")[0] or "github-actions" in chunk.split("\x1f")[0]:
                    continue
                if "\x1f" not in chunk:
                    continue
                head, _, stat = chunk.partition("\n")
                meta, _, subject = head.partition("\x1f")
                ts = parse_ts(meta.split(" ")[0])
                if not ts:
                    continue
                stat = stat.strip()
                m = re.findall(r"(\d+) (?:insertion|deletion)", stat)
                suffix = f" (+{m[0]}/-{m[1]})" if len(m) == 2 else ""
                out.append(event("git", ts, subject + suffix, os.path.basename(repo)))
    return out


# ---------------------------------------------------------------- Chrome (local)
CHROME_DIRS = [
    "~/Library/Application Support/Google/Chrome",  # macOS
    "~/.config/google-chrome",  # Linux
]
WEBKIT_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)


def chrome_history_files():
    for base in CHROME_DIRS:
        base = os.path.expanduser(base)
        for prof in ["Default"] + sorted(os.path.basename(p) for p in glob.glob(os.path.join(base, "Profile *"))):
            path = os.path.join(base, prof, "History")
            if os.path.isfile(path):
                yield prof, path


def collect_chrome(since, until):
    import shutil
    import sqlite3
    import tempfile

    out = []
    lo = int((since - WEBKIT_EPOCH).total_seconds() * 1e6)
    hi = int((until - WEBKIT_EPOCH).total_seconds() * 1e6)
    for prof, path in chrome_history_files():
        # Chrome keeps the DB locked while running; read a copy.
        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, "History")
            try:
                shutil.copyfile(path, copy)
                con = sqlite3.connect(copy)
                rows = con.execute(
                    "SELECT v.visit_time, u.title, u.url FROM visits v JOIN urls u ON u.id = v.url "
                    "WHERE v.visit_time BETWEEN ? AND ? ORDER BY v.visit_time", (lo, hi)).fetchall()
                con.close()
            except (OSError, sqlite3.Error) as e:
                print(f"[chrome] cannot read {path}: {e}", file=sys.stderr)
                continue
        last = None
        for usec, title, url in rows:
            host = re.sub(r"^https?://(www\.)?", "", url or "").split("/")[0]
            label = title or url
            if (host, label) == last:  # collapse reloads / redirects of the same page
                continue
            last = (host, label)
            ts = (WEBKIT_EPOCH + dt.timedelta(microseconds=usec)).astimezone(LOCAL_TZ)
            out.append(event("chrome", ts, label, host))
    return out


# ---------------------------------------------------------------- diagnostics
def doctor(args, author):
    def newest(paths):
        paths = list(paths)
        if not paths:
            return "0 files"
        m = max(os.path.getmtime(p) for p in paths)
        return f"{len(paths)} files, newest {dt.datetime.fromtimestamp(m):%Y-%m-%d %H:%M}"

    print(f"python   {sys.version.split()[0]}  home={os.path.expanduser('~')}  tz={LOCAL_TZ}")
    claude_root = os.path.expanduser("~/.claude/projects")
    print(f"claude   {claude_root} exists={os.path.isdir(claude_root)}  "
          f"{newest(glob.glob(os.path.join(claude_root, '*', '*.jsonl')))}")
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        print(f"         note: CLAUDE_CONFIG_DIR={os.environ['CLAUDE_CONFIG_DIR']}")
    codex_root = os.path.expanduser("~/.codex")
    print(f"codex    {codex_root} exists={os.path.isdir(codex_root)}  "
          f"{newest(glob.glob(os.path.join(codex_root, 'sessions', '**', '*.jsonl'), recursive=True))}  "
          f"history.jsonl={os.path.isfile(os.path.join(codex_root, 'history.jsonl'))}")
    repos = [r for root in (args.git_root or ["~"]) for r in find_repos(root, args.git_depth)]
    print(f"git      {len(repos)} repos under {args.git_root or ['~']} (depth {args.git_depth}), author={author!r}")
    for r in repos[:15]:
        last = subprocess.run(["git", "-C", r, "log", "--all", "-1", "--pretty=%aI %ae"],
                              capture_output=True, text=True).stdout.strip()
        print(f"         {r}  last: {last}")
    files = list(chrome_history_files())
    print(f"chrome   {len(files)} profiles: {[p for p, _ in files]}")
    print(f"youtube  {args.youtube or '(no --youtube given)'}")


# ---------------------------------------------------------------- YouTube (Takeout)
def collect_youtube(path, since, until):
    if not path:
        return []
    path = os.path.expanduser(path)
    if path.endswith(".json"):
        return _youtube_json(path, since, until)
    return _youtube_html(path, since, until)


def _yt_title(raw):
    for prefix in ("Watched ", "Viewed "):
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return re.sub(r"\s*을\(를\) 시청했습니다\.?$", "", raw)


def _youtube_json(path, since, until):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[youtube] cannot read {path}: {e}", file=sys.stderr)
        return out
    for it in items:
        ts = parse_ts(it.get("time"))
        if not ts or not (since <= ts < until):
            continue
        if "details" in it:  # ads shown, not something the user chose to watch
            continue
        channel = ((it.get("subtitles") or [{}])[0]).get("name", "")
        out.append(event("youtube", ts, _yt_title(it.get("title", "")), channel))
    return out


# Takeout HTML dates are locale-formatted; handle English and Korean.
_EN_DATE = re.compile(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4}), (\d{1,2}):(\d{2}):(\d{2})\s*([AP]M)")
_KO_DATE = re.compile(r"(\d{4})\. (\d{1,2})\. (\d{1,2})\. (오전|오후) (\d{1,2}):(\d{2}):(\d{2})")
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _html_date(text):
    m = _EN_DATE.search(text)
    if m:
        mon, day, year, h, mi, s, ap = m.groups()
        h = int(h) % 12 + (12 if ap == "PM" else 0)
        return dt.datetime(int(year), _MONTHS[mon], int(day), h, int(mi), int(s), tzinfo=LOCAL_TZ)
    m = _KO_DATE.search(text)
    if m:
        year, mon, day, ap, h, mi, s = m.groups()
        h = int(h) % 12 + (12 if ap == "오후" else 0)
        return dt.datetime(int(year), int(mon), int(day), h, int(mi), int(s), tzinfo=LOCAL_TZ)
    return None


def _youtube_html(path, since, until):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            doc = f.read()
    except OSError as e:
        print(f"[youtube] cannot read {path}: {e}", file=sys.stderr)
        return out
    for cell in re.split(r'<div class="outer-cell', doc)[1:]:
        links = re.findall(r'<a href="[^"]*">(.*?)</a>', cell)
        if not links:
            continue
        plain = html.unescape(re.sub(r"<[^>]+>", "\n", cell))
        ts = _html_date(plain)
        if not ts or not (since <= ts < until):
            continue
        title = html.unescape(links[0])
        channel = html.unescape(links[1]) if len(links) > 1 else ""
        out.append(event("youtube", ts, title, channel))
    return out


# ---------------------------------------------------------------- output
GROUP_GAP = dt.timedelta(minutes=15)


def group_runs(items):
    """Merge consecutive browsing on the same site into one line (chrome/youtube only)."""
    runs = []
    for e in items:
        last = runs[-1] if runs else None
        if (last and e["source"] in ("chrome", "youtube") and last[0]["source"] == e["source"]
                and last[0]["project"] == e["project"] and e["ts"] - last[-1]["ts"] <= GROUP_GAP):
            last.append(e)
        else:
            runs.append([e])
    return runs


def format_run(run):
    first, lastev = run[0], run[-1]
    proj = f"[{first['project']}] " if first["project"] else ""
    if len(run) == 1:
        return f"- {first['ts']:%H:%M} **{first['source']}** {proj}{first['text']}"
    titles = []
    for e in run:
        t = clip(e["text"], 70)
        if t not in titles and not t.startswith("http"):
            titles.append(t)
    shown = " · ".join(titles[:4]) or clip(first["text"], 70)
    more = f" 외 {len(titles) - 4}" if len(titles) > 4 else ""
    return (f"- {first['ts']:%H:%M}–{lastev['ts']:%H:%M} **{first['source']}** {proj}"
            f"×{len(run)}: {shown}{more}")


def render_timeline(events):
    by_day = defaultdict(list)
    for e in events:
        by_day[e["ts"].strftime("%Y-%m-%d (%a)")].append(e)
    lines = ["# Activity timeline", ""]
    for day, items in by_day.items():
        counts = defaultdict(int)
        for e in items:
            counts[e["source"]] += 1
        summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        lines += [f"## {day} — {summary}", ""]
        lines += [format_run(r) for r in group_runs(items)]
        lines.append("")
    return "\n".join(lines)


def write_outputs(events, out_dir, to_stdout=False):
    events.sort(key=lambda e: e["ts"])
    timeline = render_timeline(events)
    if to_stdout:
        print(timeline)
        return
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "events.jsonl"), "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps({**e, "ts": e["ts"].isoformat()}, ensure_ascii=False) + "\n")
    with open(os.path.join(out_dir, "timeline.md"), "w", encoding="utf-8") as f:
        f.write(timeline)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=7, help="look back N days (default 7)")
    p.add_argument("--git-root", action="append", help="directory to scan for repos (repeatable, default ~)")
    p.add_argument("--git-depth", type=int, default=4, help="max directory depth for repo scan")
    p.add_argument("--git-author", default=None,
                   help="only commits whose author matches (default: all authors except bots)")
    p.add_argument("--youtube", help="path to Takeout watch-history.json or watch-history.html")
    p.add_argument("--out", default="retro_out")
    p.add_argument("--no-chrome", action="store_true", help="skip local Chrome history")
    p.add_argument("--stdout", action="store_true", help="print timeline to stdout instead of files (for ssh)")
    p.add_argument("--doctor", action="store_true", help="show where each source looks and what it finds")
    args = p.parse_args()

    until = dt.datetime.now(LOCAL_TZ)
    since = (until - dt.timedelta(days=args.days)).replace(hour=0, minute=0, second=0, microsecond=0)

    author = args.git_author or None

    if args.doctor:
        doctor(args, author)
        return

    events = []
    for name, fn in [
        ("claude", lambda: collect_claude(since, until)),
        ("codex", lambda: collect_codex(since, until)),
        ("git", lambda: collect_git(since, until, args.git_root or ["~"], args.git_depth, author)),
        ("youtube", lambda: collect_youtube(args.youtube, since, until)),
        ("chrome", lambda: [] if args.no_chrome else collect_chrome(since, until)),
    ]:
        got = fn()
        print(f"{name:8} {len(got):5} events", file=sys.stderr)
        events += got

    write_outputs(events, args.out, args.stdout)
    if args.stdout:
        return
    print(f"\nwrote {len(events)} events -> {args.out}/timeline.md", file=sys.stderr)


if __name__ == "__main__":
    main()
