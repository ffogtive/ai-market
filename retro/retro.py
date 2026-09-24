# /// script
# requires-python = ">=3.10"
# dependencies = ["anthropic>=1"]
# ///
"""retro — one command for a daily (or weekly) retrospective page.

  retro                          today's page (collect this Mac + saved hosts, summarize, open)
  retro --date 2026-09-23        a specific day
  retro week                     this week's page, Mon–Sun (--date 2026-09-23: that day's week)
  retro --refresh                summarize again (by default a saved summary is reused while the logs are unchanged)
                                 ~/Retro/index.html lists every page; each page links to the day/week before and after
  retro --preview                print exactly what the summary would send, and to where; sends nothing, writes no page
                                 (also `retro week --preview`, with --date)
  retro add-host "ssh -p 10024 me@100.76.129.71"   also collect from a server, every run
  retro hosts | remove-host NAME
  retro add-repo ~/code          also collect commits from repos here (a repo, or a folder of repos),
                                 even ones you never opened an AI session in
  retro repos | remove-repo PATH
  retro sources                  what each source reads, and which are off
  retro off chrome | on chrome   turn a source off/on (claude codex git chrome extension)
  retro alias ai-market-2 "AI 마켓"   count a folder/repo name as that project (web sites are never merged)
  retro aliases | alias --remove RAW
  retro forget --date 2026-09-23 delete what retro made for that day (page, summary, notes), never the source logs
  retro forget --all             delete everything retro wrote in ~/Retro (asks first; --yes skips the question)
  retro doctor                   check every source and the summary backend
  retro schedule --at 22:00      open the page automatically every day (macOS)
  retro app                      buttons and settings in a browser page (this machine only)
  retro --llm none               numbers only (no log text leaves this machine)

Run through `uv run`, so the system Python version does not matter. Servers
need only python3 (3.6+); nothing is installed there — collect.py is streamed
over ssh.
"""
import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))

# `retro schedule` runs under launchd with a bare PATH; look where installers put the claude CLI
for _d in ("~/.local/bin", "~/.claude/local", "~/.npm-global/bin", "/opt/homebrew/bin", "/usr/local/bin"):
    _d = os.path.expanduser(_d)
    if os.path.isdir(_d) and _d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + _d
sys.path.insert(0, HERE)
import render  # noqa: E402

CONFIG = os.path.expanduser("~/.config/retro/config.json")
OUT_DIR = os.path.expanduser("~/Retro")
COLLECT = os.path.join(HERE, "collect.py")
EXTENSION_DIR = os.path.expanduser("~/Downloads/retro")  # where the browser extension saves browser-DATE.jsonl
CHROME_HELP = (
    "Chrome 기록을 읽으려면 권한이 필요합니다: 시스템 설정 → 개인정보 보호 및 보안 → "
    "전체 디스크 접근 권한 → 사용 중인 터미널 앱 켜기 (건너뛰려면 --no-chrome)"
)


# what each source reads — shown by `retro sources`, mirrored in PRIVACY.md
SOURCES = {
    "claude": "~/.claude/projects/*/*.jsonl (Claude Code 프롬프트)",
    "codex": "~/.codex/sessions, ~/.codex/archived_sessions, ~/.codex/history.jsonl (Codex 프롬프트)",
    "git": "AI 세션을 연 폴더(최근 30일)의 git 커밋 제목",
    "chrome": "Chrome 방문 기록 (History 파일 복사본)",
    "extension": "~/Downloads/retro/browser-*.jsonl (브라우저 확장이 저장한 대화·방문 기록)",
}


# ---------------------------------------------------------------- config
def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"hosts": []}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def host_name(ssh_cmd):
    args = shlex.split(ssh_cmd)[1:]
    target = next((a for a in args if "@" in a), args[-1] if args else ssh_cmd)
    return target.split("@")[-1]


# ---------------------------------------------------------------- collect
def skip_args(off):
    return [a for s in off if s in SOURCES and s != "extension" for a in ("--skip", s)]


def git_root_args(cfg):
    return [a for d in cfg.get("git_roots", []) for a in ("--git-root", d)]


def collect_local(days, chrome, off=(), extra=()):
    cmd = [sys.executable, COLLECT, "--days", str(days), "--stdout"] + ([] if chrome else ["--no-chrome"]) + skip_args(off) + list(extra)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if "Operation not permitted" in res.stderr or "unable to open database" in res.stderr:
        print("⚠️  " + CHROME_HELP, file=sys.stderr)
    return parse_jsonl(res.stdout), res.stderr


def ssh_argv(ssh_cmd):
    # BatchMode: fail fast instead of hanging on a password prompt
    argv = shlex.split(ssh_cmd)
    argv[1:1] = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    return argv


def collect_remote(ssh_cmd, days, off=()):
    if not shutil.which("ssh"):
        return [], "ssh 명령이 없습니다"
    argv = ssh_argv(ssh_cmd)
    argv.append(" ".join([f"python3 - --days {days} --no-chrome --stdout"] + skip_args(off)))
    with open(COLLECT, "rb") as script:
        try:
            res = subprocess.run(argv, stdin=script, capture_output=True, timeout=600)
        except subprocess.TimeoutExpired:
            return [], "timed out after 10 minutes"
    return parse_jsonl(res.stdout.decode("utf-8", "replace")), res.stderr.decode("utf-8", "replace")


def extension_events(day, days):
    """browser-YYYY-MM-DD.jsonl files written by retro/extension (one full file per day)."""
    out = []
    for i in range(days + 1):
        path = os.path.join(EXTENSION_DIR, f"browser-{day - dt.timedelta(days=i)}.jsonl")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                out += parse_jsonl(f.read())
    return out


def parse_jsonl(text):
    out = []
    for line in text.splitlines():
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


WEB_SOURCES = ("chrome", "youtube")  # their "project" is a site or channel: never merged into a work project


def apply_aliases(events, aliases):
    """config "aliases" {raw folder/repo name: project name} → the events' project (web events untouched).

    The raw name stays in project_raw, so the app can offer it again and the page keeps the user's
    name over the LLM's project_labels.
    """
    if not isinstance(aliases, dict) or not aliases:
        return events
    for e in events:
        raw = e.get("project")
        name = aliases.get(raw) if raw and e.get("source") not in WEB_SOURCES else None
        if isinstance(name, str) and name and name != raw:
            e["project"], e["project_raw"] = name, raw
    return events


def recent_projects(limit=40):
    """[(raw folder/repo name, events)] in the last collected ~/Retro/events.jsonl, most first; web sites left out."""
    try:
        with open(os.path.join(OUT_DIR, "events.jsonl"), encoding="utf-8") as f:
            events = parse_jsonl(f.read())
    except OSError:
        events = []
    seen = {}
    for e in events:
        raw = e.get("project_raw") or e.get("project")
        if raw and isinstance(raw, str) and e.get("source") not in WEB_SOURCES:
            seen[raw] = seen.get(raw, 0) + 1
    return sorted(seen.items(), key=lambda x: (-x[1], x[0]))[:limit]


def counts(stderr):
    """collect.py prints 'source   N events' lines on stderr."""
    rows = [l.split()[:2] for l in stderr.splitlines() if l.strip().endswith("events")]
    return ", ".join(f"{s} {n}" for s, n in rows if n != "0") or "0"


# ---------------------------------------------------------------- commands
def collect_all(day, days, no_chrome, path=None):
    """This machine + browser extension + saved servers → ~/Retro/events.jsonl (or path); returns its path."""
    cfg = load_config()
    path = path or os.path.join(OUT_DIR, "events.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    off = cfg.get("off", [])
    if off:
        print(f"· 꺼진 소스: {', '.join(off)} (켜기: retro on <소스>)", file=sys.stderr)
    ext_events = [] if "extension" in off else extension_events(day, days)
    # the extension already records browsing; reading Chrome's DB too would double count
    events, err = collect_local(days, chrome=not no_chrome and not ext_events, off=off, extra=git_root_args(cfg))
    print(f"· 이 기기: {counts(err)}", file=sys.stderr)
    if ext_events:
        by = {}
        for e in ext_events:
            by[e["source"]] = by.get(e["source"], 0) + 1
        print("· 브라우저 확장: " + ", ".join(f"{k} {v}" for k, v in by.items()), file=sys.stderr)
        events += ext_events
    for ssh_cmd in cfg.get("hosts", []):
        got, err = collect_remote(ssh_cmd, days, off)
        status = counts(err) if got or "events" in err else f"실패 — {err.strip().splitlines()[-1] if err.strip() else '응답 없음'}"
        print(f"· {host_name(ssh_cmd)}: {status}", file=sys.stderr)
        events += got

    apply_aliases(events, cfg.get("aliases"))  # before anything reads the events: pages, summaries, the app
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    return path


def render_and_open(argv, out, no_open):
    if render.main(argv + ["--out", out]) != 0:
        return 1
    if not no_open:
        webbrowser.open("file://" + out)
    return 0


def cache_args(args):
    """summaries are saved as JSON next to the pages and reused while the logs are unchanged"""
    return ["--cache-dir", OUT_DIR] + (["--refresh"] if getattr(args, "refresh", False) else [])


def off_args(args):
    """sources left out of this run (`retro off …`, --no-chrome), for the page's 관측 범위 line"""
    off = list(load_config().get("off", []))
    if getattr(args, "no_chrome", False) and "chrome" not in off:
        off.append("chrome")
    return ["--off", ",".join(off)] if off else []


def preview(render_argv, day, days, args):
    """--preview: collect as the run would (into a temp file, ~/Retro untouched), print what the summary would send."""
    with tempfile.TemporaryDirectory() as tmp:
        path = collect_all(day, days, args.no_chrome, os.path.join(tmp, "events.jsonl"))
        return render.main(render_argv + ["--preview", "--llm", args.llm, path] + cache_args(args))


def cmd_run(args):
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    days = (dt.date.today() - day).days + 7  # enough history for the 7-day chart
    if getattr(args, "preview", False):
        return preview(["--date", str(day)], day, days, args)
    path = collect_all(day, days, args.no_chrome)
    return render_and_open(["--date", str(day), "--llm", args.llm, path] + cache_args(args) + off_args(args),
                           os.path.join(OUT_DIR, f"daily-{day}.html"), args.no_open)


def cmd_week(args):
    """Mon–Sun page for the week containing --date (default: this week), plus the week before for ▲▼."""
    today = dt.date.today()
    day = dt.date.fromisoformat(args.date) if args.date else today
    monday = render.week_days(day)[0]
    end = min(monday + dt.timedelta(days=6), today)  # days after today have no logs yet
    if getattr(args, "preview", False):
        return preview(["--week", "--date", str(day)], end, max((today - monday).days, 0) + 7, args)
    path = collect_all(end, max((today - monday).days, 0) + 7, args.no_chrome)  # back to the Monday before
    return render_and_open(["--week", "--date", str(day), "--llm", args.llm, path] + cache_args(args) + off_args(args),
                           os.path.join(OUT_DIR, f"weekly-{monday}.html"), args.no_open)


def cmd_add_host(args):
    cfg = load_config()
    ssh_cmd = args.ssh.strip()
    if not ssh_cmd.startswith("ssh "):
        ssh_cmd = "ssh " + ssh_cmd
    if not shutil.which("ssh"):
        print("ssh 명령이 없습니다", file=sys.stderr)
        return 1
    print(f"연결 확인 중: {ssh_cmd}", file=sys.stderr)
    res = subprocess.run(ssh_argv(ssh_cmd) + ["python3 --version"], capture_output=True, text=True)
    if res.returncode != 0:
        print("연결 실패. 비밀번호 없이 접속되는지(ssh 키) 확인하세요:\n" + res.stderr.strip(), file=sys.stderr)
        return 1
    print(f"OK — 서버 {(res.stdout or res.stderr).strip()}", file=sys.stderr)
    if ssh_cmd not in cfg["hosts"]:
        cfg["hosts"].append(ssh_cmd)
        save_config(cfg)
    print(f"등록됨. 이제 `retro` 실행 시 {host_name(ssh_cmd)}도 함께 수집합니다.", file=sys.stderr)
    return 0


def cmd_hosts(_args):
    hosts = load_config().get("hosts", [])
    print("\n".join(f"{host_name(h)}\t{h}" for h in hosts) or "(등록된 서버 없음)")
    return 0


def cmd_remove_host(args):
    cfg = load_config()
    cfg["hosts"] = [h for h in cfg["hosts"] if host_name(h) != args.name and h != args.name]
    save_config(cfg)
    return cmd_hosts(args)


def cmd_add_repo(args):
    path = os.path.abspath(os.path.expanduser(args.path))
    if not os.path.isdir(path):
        print(f"폴더가 없습니다: {path}", file=sys.stderr)
        return 1
    cfg = load_config()
    roots = cfg.setdefault("git_roots", [])
    if path not in roots:
        roots.append(path)
        save_config(cfg)
    kind = "저장소" if os.path.isdir(os.path.join(path, ".git")) else "폴더(안의 저장소들, 깊이 4까지)"
    print(f"등록됨: {path} — {kind}. AI 세션이 없어도 여기 커밋을 수집합니다.", file=sys.stderr)
    return 0


def cmd_repos(_args):
    roots = load_config().get("git_roots", [])
    print("\n".join(roots) or "(추가한 저장소 없음 — AI 세션을 연 저장소만 수집)")
    return 0


def cmd_remove_repo(args):
    cfg = load_config()
    path = os.path.abspath(os.path.expanduser(args.path))
    cfg["git_roots"] = [d for d in cfg.get("git_roots", []) if d not in (path, args.path)]
    save_config(cfg)
    return cmd_repos(args)


def cmd_sources(_args):
    cfg = load_config()
    off = set(cfg.get("off", []))
    for name, what in SOURCES.items():
        if name == "git" and cfg.get("git_roots"):
            what += " + " + ", ".join(cfg["git_roots"])
        print(f"{'off' if name in off else 'on ':3}  {name:9} {what}")
    print("\n끄기/켜기: retro off <소스> / retro on <소스>. 읽기만 하며, 요약 시 본인 Anthropic 계정 외로는 보내지 않습니다.")
    return 0


def cmd_toggle(args):
    cfg = load_config()
    off = [s for s in cfg.get("off", []) if s != args.source]
    if args.cmd == "off":
        off.append(args.source)
    cfg["off"] = off
    save_config(cfg)
    return cmd_sources(args)


def clean_name(text):
    """A name as typed: one line, trimmed, at most 100 characters; "" when unusable."""
    text = " ".join(str(text or "").split())
    return text if text.isprintable() and len(text) <= 100 else ""


def cmd_alias(args):
    """`retro alias RAW NAME`: count the folder/repo RAW as project NAME; `--remove RAW` undoes it."""
    cfg = load_config()
    aliases = cfg.get("aliases") if isinstance(cfg.get("aliases"), dict) else {}
    raw = clean_name(args.raw)
    if not raw:
        print("폴더·레포 이름을 넣어주세요 (한 줄, 100자 이하).", file=sys.stderr)
        return 1
    if getattr(args, "remove", False):
        if raw not in aliases:
            print(f"묶여 있지 않습니다: {raw}", file=sys.stderr)
            return 1
        del aliases[raw]
        cfg["aliases"] = aliases
        save_config(cfg)
        print(f"풀었습니다: {raw} (다음 retro 실행부터 원래 이름으로)", file=sys.stderr)
        return 0
    name = clean_name(getattr(args, "name", ""))
    if not name or name == raw:
        print('묶을 프로젝트 이름을 넣어주세요 (폴더 이름과 다르게, 한 줄, 100자 이하). 예: retro alias ai-market-2 "AI 마켓"',
              file=sys.stderr)
        return 1
    aliases[raw] = name
    cfg["aliases"] = aliases
    save_config(cfg)
    print(f"묶었습니다: {raw} → {name}. 다음 retro 실행부터 적용됩니다 (웹사이트는 묶지 않음).", file=sys.stderr)
    return 0


def cmd_aliases(_args):
    aliases = load_config().get("aliases") or {}
    print("\n".join(f"{raw}\t→ {name}" for raw, name in sorted(aliases.items()))
          or "(묶은 프로젝트 없음 — retro alias <폴더·레포 이름> <프로젝트 이름>)")
    return 0


# retro's own files in ~/Retro; `retro forget --all` deletes only these (and a leftover NAME.<pid>.tmp of one)
RETRO_FILE_RE = re.compile(r"(?:daily|weekly)-\d{4}-\d{2}-\d{2}\.html|summary-(?:daily|weekly)-\d{4}-\d{2}-\d{2}\.json"
                           r"|notes-[\w-]+\.json|events\.jsonl|index\.html|retro\.log")
TMP_RE = re.compile(r"(.+)\.\d+\.tmp")
FORGET_NOTE = ("원본 기록(Claude·Codex·git·Chrome)은 그대로입니다. 다시 retro를 실행하면 같은 날이 다시 만들어집니다. "
               "수집을 멈추려면 retro off <소스>")
FORGET_KEPT = "남은 것: 그 주의 주간 페이지·주간 요약(그날 숫자·요약이 섞여 있음), events.jsonl(마지막 수집 사본, 다음 실행 때 덮어씀)"


def forget_targets(day=None):
    """What `retro forget` deletes: one day's page, daily summary and notes, or (no day) every retro file in ~/Retro."""
    if day:
        names = [f"daily-{day}.html", f"summary-daily-{day}.json", f"notes-{day}.json"]
    else:
        try:
            names = sorted(os.listdir(OUT_DIR))
        except OSError:
            names = []
        names = [n for n in names if RETRO_FILE_RE.fullmatch(TMP_RE.sub(r"\1", n))]
    out = []
    for n in names:
        path = os.path.join(OUT_DIR, n)
        if not (os.path.isfile(path) or os.path.islink(path)):
            continue
        if n == "index.html":  # someone else's index.html is not ours (render.write_index leaves it alone too)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    if render.GENERATOR not in f.read():
                        continue
            except OSError:
                continue
        out.append(path)
    return out


def cmd_forget(args):
    """`retro forget --date D | --all [--yes]`: delete retro's derived files, never the source logs."""
    day = None
    if getattr(args, "date", None):
        try:
            day = dt.date.fromisoformat(args.date)
        except ValueError:
            print(f"날짜는 YYYY-MM-DD로 넣어주세요: {args.date}", file=sys.stderr)
            return 2
    elif not getattr(args, "all", False):
        print("--date YYYY-MM-DD 또는 --all 중 하나를 주세요.", file=sys.stderr)
        return 2
    paths = forget_targets(day)
    if not paths:
        print(f"지울 파일이 없습니다 ({day or OUT_DIR}).\n{FORGET_NOTE}")
        return 0
    if not getattr(args, "yes", False):
        print(f"지울 파일 ({len(paths)}개):\n" + "\n".join(f"  {p}" for p in paths))
        try:
            answer = input("지울까요? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("취소했습니다. 아무것도 지우지 않았습니다.")
            return 1
    gone = []
    for p in paths:
        try:
            os.remove(p)
            gone.append(p)
        except OSError as err:
            print(f"지우지 못함: {p} ({err.strerror})", file=sys.stderr)
    print(f"지웠습니다 ({len(gone)}개):\n" + "\n".join(f"  {p}" for p in gone))
    if day and os.path.isdir(OUT_DIR):
        weekly = render.forget_daily_links(OUT_DIR, day)
        if weekly:
            print(f"주간 페이지에서 이 날 링크를 뺐습니다: {weekly}")
        render.update_site(OUT_DIR, OUT_DIR)  # index.html and the ← · → rows stop pointing at the deleted page
        print("목록(index.html)과 이웃 페이지의 이동 링크를 다시 만들었습니다.")
        print(FORGET_KEPT)
    print(FORGET_NOTE)
    return 0 if len(gone) == len(paths) else 1


PLIST = os.path.expanduser("~/Library/LaunchAgents/com.retro.daily.plist")


def cmd_schedule(args):
    """macOS: a LaunchAgent runs `retro` every day and opens the page."""
    if sys.platform != "darwin":
        print(f"macOS 전용입니다. Linux는 crontab에: {args.at.split(':')[1]} {args.at.split(':')[0]} * * * retro --no-open",
              file=sys.stderr)
        return 1
    hour, minute = (int(x) for x in args.at.split(":"))
    shim = os.path.expanduser("~/.local/bin/retro")
    log = os.path.join(OUT_DIR, "retro.log")
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "w", encoding="utf-8") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.retro.daily</string>
  <key>ProgramArguments</key><array><string>/bin/sh</string><string>-lc</string><string>{shim}</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer></dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
""")
    subprocess.run(["launchctl", "unload", PLIST], capture_output=True)
    res = subprocess.run(["launchctl", "load", PLIST], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"launchctl 실패: {res.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"✓ 매일 {hour:02d}:{minute:02d}에 회고 페이지가 열립니다 (로그: {log}). 끄기: retro unschedule", file=sys.stderr)
    print("  참고: 자동 실행에서는 macOS가 Chrome 기록 권한을 물어볼 수 없어 Chrome 항목이 빠질 수 있습니다.", file=sys.stderr)
    return 0


def cmd_unschedule(_args):
    if os.path.exists(PLIST):
        subprocess.run(["launchctl", "unload", PLIST], capture_output=True)
        os.remove(PLIST)
    print("자동 실행을 껐습니다.", file=sys.stderr)
    return 0


def cmd_doctor(args):
    subprocess.run([sys.executable, COLLECT, "--doctor"] + git_root_args(load_config()))
    for ssh_cmd in load_config().get("hosts", []):
        print(f"\n--- {host_name(ssh_cmd)} ---")
        if not shutil.which("ssh"):
            print("ssh 명령이 없습니다")
            continue
        with open(COLLECT, "rb") as script:
            subprocess.run(ssh_argv(ssh_cmd) + ["python3 - --doctor"], stdin=script)
    print(f"\nsummary  {render.pick_backend('auto') or 'none (숫자만)'}"
          f"  — API 키: {'있음' if os.environ.get('ANTHROPIC_API_KEY') else '없음'},"
          f" claude CLI: {'있음' if shutil.which('claude') else '없음'}")
    log = os.path.join(OUT_DIR, "retro.log")
    if os.path.exists(PLIST) or os.path.exists(log):
        when = dt.datetime.fromtimestamp(os.path.getmtime(log)).strftime("%m-%d %H:%M") if os.path.exists(log) else "아직 없음"
        print(f"schedule {'켜짐' if os.path.exists(PLIST) else '꺼짐'}  — 마지막 자동 실행: {when}")
        if os.path.exists(log):
            with open(log, encoding="utf-8", errors="replace") as f:
                for line in f.read().splitlines()[-4:]:
                    print(f"         {line}")
    return 0


def cmd_app(args):
    """Local web page with the same commands as buttons (see app.py)."""
    import app
    return app.serve(open_browser=not args.no_open)


def page_options(p, sub=False):
    # on a subcommand, SUPPRESS keeps `retro --date X week` from being reset to the defaults
    keep = argparse.SUPPRESS if sub else None
    p.add_argument("--date", default=keep, help="YYYY-MM-DD (default: today)")
    p.add_argument("--llm", default=keep or "auto", choices=["auto", "api", "claude", "none"],
                   help="summary backend (auto: API key → claude CLI → numbers only)")
    p.add_argument("--no-chrome", action="store_true", default=keep or False)
    p.add_argument("--no-open", action="store_true", default=keep or False, help="don't open the page in a browser")
    p.add_argument("--refresh", action="store_true", default=keep or False,
                   help="summarize again even if the saved summary matches the logs")
    p.add_argument("--preview", action="store_true", default=keep or False,
                   help="print exactly what the summary would send, and to where (sends nothing, writes no page)")


def main():
    p = argparse.ArgumentParser(prog="retro", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    page_options(p)
    page_options(sub.add_parser("week", help="this week's page (Mon–Sun)"), sub=True)
    a = sub.add_parser("add-host", help="collect from a server too")
    a.add_argument("ssh", help='e.g. "ssh -p 10024 me@100.76.129.71"')
    sub.add_parser("hosts")
    r = sub.add_parser("remove-host")
    r.add_argument("name")
    sub.add_parser("doctor")
    ar = sub.add_parser("add-repo", help="also collect commits from a repo or a folder of repos")
    ar.add_argument("path")
    sub.add_parser("repos")
    rr = sub.add_parser("remove-repo")
    rr.add_argument("path")
    sub.add_parser("sources", help="what each source reads, and which are off")
    for name in ("off", "on"):
        t = sub.add_parser(name, help=f"turn a source {name}")
        t.add_argument("source", choices=list(SOURCES))
    al = sub.add_parser("alias", help="count a folder/repo name as one project (web sites are never merged)")
    al.add_argument("raw", help="folder or repo name as it appears in the logs")
    al.add_argument("name", nargs="?", default="", help="project name to show")
    al.add_argument("--remove", action="store_true", help="undo the alias for RAW")
    sub.add_parser("aliases", help="list project aliases")
    fg = sub.add_parser("forget", help="delete what retro made (pages, summaries, notes), never the source logs")
    which = fg.add_mutually_exclusive_group(required=True)
    which.add_argument("--date", help="YYYY-MM-DD: that day's page, daily summary and notes")
    which.add_argument("--all", action="store_true", help="everything retro wrote in ~/Retro")
    fg.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    sc = sub.add_parser("schedule", help="run every day (macOS)")
    sc.add_argument("--at", default="22:00", help="HH:MM (default 22:00)")
    sub.add_parser("unschedule")
    sub.add_parser("app", help="buttons and settings in a browser page (this machine only)")
    args = p.parse_args()
    handler = {"week": cmd_week, "app": cmd_app, "add-host": cmd_add_host, "hosts": cmd_hosts, "remove-host": cmd_remove_host,
               "doctor": cmd_doctor, "add-repo": cmd_add_repo, "repos": cmd_repos, "remove-repo": cmd_remove_repo, "sources": cmd_sources, "off": cmd_toggle, "on": cmd_toggle, "schedule": cmd_schedule, "unschedule": cmd_unschedule,
               "alias": cmd_alias, "aliases": cmd_aliases, "forget": cmd_forget}.get(args.cmd, cmd_run)
    sys.exit(handler(args))


if __name__ == "__main__":
    main()
