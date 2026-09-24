# /// script
# requires-python = ">=3.10"
# dependencies = ["anthropic>=1"]
# ///
"""retro — one command for a daily retrospective page.

  retro                          today's page (collect this Mac + saved hosts, summarize, open)
  retro --date 2026-09-23        a specific day
  retro add-host "ssh -p 10024 me@100.76.129.71"   also collect from a server, every run
  retro hosts | remove-host NAME
  retro doctor                   check every source and the summary backend
  retro schedule --at 22:00      open the page automatically every day (macOS)
  retro --llm none               numbers only (no log text leaves this machine)

Run through `uv run`, so the system Python version does not matter. Servers
need only python3 (3.6+); nothing is installed there — collect.py is streamed
over ssh.
"""
import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import subprocess
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import render  # noqa: E402

CONFIG = os.path.expanduser("~/.config/retro/config.json")
OUT_DIR = os.path.expanduser("~/Retro")
COLLECT = os.path.join(HERE, "collect.py")
CHROME_HELP = (
    "Chrome 기록을 읽으려면 권한이 필요합니다: 시스템 설정 → 개인정보 보호 및 보안 → "
    "전체 디스크 접근 권한 → 사용 중인 터미널 앱 켜기 (건너뛰려면 --no-chrome)"
)


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
def collect_local(days, chrome):
    cmd = [sys.executable, COLLECT, "--days", str(days), "--stdout"] + ([] if chrome else ["--no-chrome"])
    res = subprocess.run(cmd, capture_output=True, text=True)
    if "Operation not permitted" in res.stderr or "unable to open database" in res.stderr:
        print("⚠️  " + CHROME_HELP, file=sys.stderr)
    return parse_jsonl(res.stdout), res.stderr


def ssh_argv(ssh_cmd):
    # BatchMode: fail fast instead of hanging on a password prompt
    argv = shlex.split(ssh_cmd)
    argv[1:1] = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    return argv


def collect_remote(ssh_cmd, days):
    if not shutil.which("ssh"):
        return [], "ssh 명령이 없습니다"
    argv = ssh_argv(ssh_cmd)
    argv.append(f"python3 - --days {days} --no-chrome --stdout")
    with open(COLLECT, "rb") as script:
        try:
            res = subprocess.run(argv, stdin=script, capture_output=True, timeout=600)
        except subprocess.TimeoutExpired:
            return [], "timed out after 10 minutes"
    return parse_jsonl(res.stdout.decode("utf-8", "replace")), res.stderr.decode("utf-8", "replace")


def parse_jsonl(text):
    out = []
    for line in text.splitlines():
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def counts(stderr):
    """collect.py prints 'source   N events' lines on stderr."""
    rows = [l.split()[:2] for l in stderr.splitlines() if l.strip().endswith("events")]
    return ", ".join(f"{s} {n}" for s, n in rows if n != "0") or "0"


# ---------------------------------------------------------------- commands
def cmd_run(args):
    cfg = load_config()
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    days = (dt.date.today() - day).days + 7  # enough history for the 7-day chart
    os.makedirs(OUT_DIR, exist_ok=True)

    events, err = collect_local(days, not args.no_chrome)
    print(f"· 이 기기: {counts(err)}", file=sys.stderr)
    for ssh_cmd in cfg.get("hosts", []):
        got, err = collect_remote(ssh_cmd, days)
        status = counts(err) if got or "events" in err else f"실패 — {err.strip().splitlines()[-1] if err.strip() else '응답 없음'}"
        print(f"· {host_name(ssh_cmd)}: {status}", file=sys.stderr)
        events += got

    path = os.path.join(OUT_DIR, "events.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in events)

    out = os.path.join(OUT_DIR, f"daily-{day}.html")
    argv = ["--date", str(day), "--llm", args.llm, "--out", out, path]
    if render.main(argv) != 0:
        return 1
    if not args.no_open:
        webbrowser.open("file://" + out)
    return 0


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
    subprocess.run([sys.executable, COLLECT, "--doctor"])
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
    return 0


def main():
    p = argparse.ArgumentParser(prog="retro", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    p.add_argument("--date", help="YYYY-MM-DD (default: today)")
    p.add_argument("--llm", default="auto", choices=["auto", "api", "claude", "none"],
                   help="summary backend (auto: API key → claude CLI → numbers only)")
    p.add_argument("--no-chrome", action="store_true")
    p.add_argument("--no-open", action="store_true", help="don't open the page in a browser")
    a = sub.add_parser("add-host", help="collect from a server too")
    a.add_argument("ssh", help='e.g. "ssh -p 10024 me@100.76.129.71"')
    sub.add_parser("hosts")
    r = sub.add_parser("remove-host")
    r.add_argument("name")
    sub.add_parser("doctor")
    sc = sub.add_parser("schedule", help="run every day (macOS)")
    sc.add_argument("--at", default="22:00", help="HH:MM (default 22:00)")
    sub.add_parser("unschedule")
    args = p.parse_args()
    handler = {"add-host": cmd_add_host, "hosts": cmd_hosts, "remove-host": cmd_remove_host,
               "doctor": cmd_doctor, "schedule": cmd_schedule, "unschedule": cmd_unschedule}.get(args.cmd, cmd_run)
    sys.exit(handler(args))


if __name__ == "__main__":
    main()
