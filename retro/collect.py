#!/usr/bin/env python3
"""Collect local activity logs into one daily timeline for a retrospective.

Sources (all read-only, all local):
  - Claude Code transcripts   ~/.claude/projects/*/*.jsonl
  - Codex CLI sessions        ~/.codex/sessions/**/*.jsonl, ~/.codex/history.jsonl
  - git commits               repos the user opened a Claude Code / Codex session in
                              (last 30 days); --git-root adds a directory scan
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

import socket

LOCAL_TZ = dt.datetime.now().astimezone().tzinfo
HOST = socket.gethostname().split(".")[0]
MAX_TEXT = 600  # enough context for the LLM summary; timeline.md trims further
ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$")


def run(cmd, cwd=None, timeout=30):
    """subprocess.run(capture_output=True, text=True) without 3.7+ keywords."""
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=timeout)


def parse_ts(value):
    """Parse ISO string or epoch seconds/ms into an aware local datetime."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            if value > 1e12:
                value /= 1000
            return dt.datetime.fromtimestamp(value, dt.timezone.utc).astimezone(LOCAL_TZ)
        # Hand-rolled ISO 8601: fromisoformat needs 3.7+ and is strict before 3.11.
        m = ISO_RE.match(str(value).strip())
        if not m:
            return None
        y, mo, d, h, mi, sec, frac, tz = m.groups()
        usec = int((frac or "0")[:6].ljust(6, "0"))
        tzinfo = dt.timezone.utc
        if tz and tz != "Z":
            sign = -1 if tz[0] == "-" else 1
            tz = tz[1:].replace(":", "")
            tzinfo = dt.timezone(sign * dt.timedelta(hours=int(tz[:2]), minutes=int(tz[2:4] or 0)))
        d = dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0), usec, tzinfo=tzinfo)
        return d.astimezone(LOCAL_TZ)
    except (ValueError, OverflowError, OSError):
        return None


def clip(text, n=200):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def event(source, ts, text, project="", actor="human"):
    """actor: human (typed by the user) | agent (one AI instructing another) | auto (bots, jobs)."""
    return {"source": source, "ts": ts, "project": project, "text": clip(text, MAX_TEXT), "actor": actor, "host": HOST}


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


NOISE_PREFIXES = (
    "<",  # system reminders, command wrappers, environment context
    "Caveat:",
    "[Request interrupted",
    "This session is being continued from a previous conversation",  # auto context summary
    "The following is the Codex agent history",  # Codex auto-approval reviewer, not the user
    "You are ",  # sub-agent / reviewer system prompts
)


def is_noise(text):
    t = text.strip()
    return not t or t.startswith(NOISE_PREFIXES)


def strip_attachments(text):
    """Codex prefixes pasted files as '# Files mentioned by the user: ...'; keep only the request."""
    if text.lstrip().startswith("# Files mentioned by the user"):
        m = re.search(r"##\s*My request for Codex:?\s*(.*)", text, re.S)
        return ("[첨부] " + m.group(1)) if m else "[첨부 파일]"
    return text


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
            # headless runs (claude -p, Agent SDK) are another program driving Claude, not the user
            actor = "agent" if str(rec.get("entrypoint", "")).startswith("sdk") else "human"
            out.append(event("claude", ts, text, project, actor))
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
            text = strip_attachments(text)
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


REPO_LOOKBACK_DAYS = 30


def first_cwd(path):
    for rec in read_jsonl(path):
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        cwd = rec.get("cwd") or payload.get("cwd")
        if cwd:
            return cwd
    return ""


def session_repos(since):
    """Repos the user actually worked in, from the cwd recorded in AI sessions.

    Reads only known session files (and only up to their first cwd) instead of
    crawling the home folder.
    """
    cutoff = (since - dt.timedelta(days=REPO_LOOKBACK_DAYS)).timestamp()
    paths = glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
    paths += glob.glob(os.path.expanduser("~/.codex/sessions/**/*.jsonl"), recursive=True)
    dirs = set()
    for path in paths:
        try:
            if os.path.getmtime(path) < cutoff:
                continue
        except OSError:
            continue
        cwd = first_cwd(path)
        if cwd and os.path.isdir(cwd):
            dirs.add(cwd)
    repos = set()
    for d in dirs:
        try:
            top = run(["git", "rev-parse", "--show-toplevel"], cwd=d, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
        if top:
            repos.add(top)
    return repos


def git_repos(since, roots, max_depth):
    repos = session_repos(since)
    for root in roots or []:
        repos.update(find_repos(root, max_depth))
    return sorted(repos)


def collect_git(since, until, repos, author):
    out = []
    for repo in repos:
        cmd = [
            "git", "log", "--all", "--no-merges",
            f"--since={int(since.timestamp())}", f"--until={int(until.timestamp())}",
            "--pretty=format:%x1e%at %ae%x1f%s", "--shortstat",
        ]
        if author:
            cmd.append(f"--author={author}")
        try:
            res = run(cmd, cwd=repo)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for chunk in res.stdout.split("\x1e"):
            if "[bot]" in chunk.split("\x1f")[0] or "github-actions" in chunk.split("\x1f")[0]:
                continue
            if "\x1f" not in chunk:
                continue
            head, _, stat = chunk.partition("\n")
            meta, _, subject = head.partition("\x1f")
            if re.match(r"(backup|auto|chore\(backup\))[:\s]", subject, re.I):
                continue
            ts = parse_ts(int(meta.split(" ")[0])) if meta.split(" ")[0].isdigit() else None
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
    repos = git_repos(dt.datetime.now(LOCAL_TZ), args.git_root, args.git_depth)
    scan = f" + scan of {args.git_root} (depth {args.git_depth})" if args.git_root else ""
    print(f"git      {len(repos)} repos from AI sessions (last {REPO_LOOKBACK_DAYS} days){scan}, author={author!r}")
    for r in repos[:15]:
        out = run(["git", "log", "--all", "-1", "--pretty=%at %ae"], cwd=r).stdout.split()
        last = f"{parse_ts(int(out[0])):%Y-%m-%d %H:%M} {out[1]}" if out and out[0].isdigit() else ""
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
        return f"- {first['ts']:%H:%M} **{first['source']}** {proj}{clip(first['text'])}"
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
        lines += [format_run(r) for r in group_runs([e for e in items if e.get("actor") == "human"])]
        n_agent = sum(1 for e in items if e.get("actor") == "agent")
        if n_agent:
            lines.append(f"- (에이전트 간 지시 {n_agent}건 생략)")
        lines.append("")
    return "\n".join(lines)


def dedupe(events):
    """The same prompt often lands in several parallel sessions; keep the first."""
    seen, out = set(), []
    for e in sorted(events, key=lambda e: e["ts"]):
        key = (e["source"], e["ts"].strftime("%Y%m%d%H%M"), e["text"][:80])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def write_outputs(events, out_dir, to_stdout=False):
    events[:] = dedupe(events)
    if to_stdout:  # machine-readable, so events from several hosts can be merged by render.py
        for e in events:
            print(json.dumps({**e, "ts": e["ts"].isoformat()}, ensure_ascii=False))
        return
    timeline = render_timeline(events)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "events.jsonl"), "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps({**e, "ts": e["ts"].isoformat()}, ensure_ascii=False) + "\n")
    with open(os.path.join(out_dir, "timeline.md"), "w", encoding="utf-8") as f:
        f.write(timeline)


SOURCES = ("claude", "codex", "git", "youtube", "chrome")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=7, help="look back N days (default 7)")
    p.add_argument("--git-root", action="append",
                   help="also scan this directory for repos (repeatable; default: no scan, only repos from AI sessions)")
    p.add_argument("--git-depth", type=int, default=4, help="max directory depth for repo scan")
    p.add_argument("--git-author", default=None,
                   help="only commits whose author matches (default: all authors except bots)")
    p.add_argument("--youtube", help="path to Takeout watch-history.json or watch-history.html")
    p.add_argument("--out", default="retro_out")
    p.add_argument("--no-chrome", action="store_true", help="skip local Chrome history (same as --skip chrome)")
    p.add_argument("--skip", action="append", default=[], choices=SOURCES, help="turn a source off (repeatable)")
    p.add_argument("--stdout", action="store_true", help="print events as JSONL to stdout instead of files (for ssh)")
    p.add_argument("--doctor", action="store_true", help="show where each source looks and what it finds")
    args = p.parse_args()

    until = dt.datetime.now(LOCAL_TZ)
    since = (until - dt.timedelta(days=args.days)).replace(hour=0, minute=0, second=0, microsecond=0)

    author = args.git_author or None

    if args.doctor:
        doctor(args, author)
        return

    skip = set(args.skip) | ({"chrome"} if args.no_chrome else set())
    events = []
    for name, fn in [
        ("claude", lambda: collect_claude(since, until)),
        ("codex", lambda: collect_codex(since, until)),
        ("git", lambda: collect_git(since, until, git_repos(since, args.git_root, args.git_depth), author)),
        ("youtube", lambda: collect_youtube(args.youtube, since, until)),
        ("chrome", lambda: collect_chrome(since, until)),
    ]:
        got = [] if name in skip else fn()
        print(f"{name:8} {len(got):5} events", file=sys.stderr)
        events += got

    write_outputs(events, args.out, args.stdout)
    if args.stdout:
        return
    print(f"\nwrote {len(events)} events -> {args.out}/timeline.md", file=sys.stderr)


if __name__ == "__main__":
    main()
