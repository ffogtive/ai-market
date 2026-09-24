"""retro app — the same commands as buttons and forms, in a browser page on this machine.

  retro app          start, print the address, open the browser (Ctrl+C stops it)

The page can run commands, so: it listens on 127.0.0.1 only; every request needs
this run's random token (?t= once, then an HttpOnly SameSite=Strict cookie); the
Host header must be 127.0.0.1/localhost (DNS rebinding); every change is a POST
with a same-origin Origin/Referer (CSRF); only ~/Retro/daily-*.html and
weekly-*.html are served; user input never reaches a shell. Standard library only.
"""
import argparse
import contextlib
import datetime as dt
import html
import io
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import retro  # noqa: E402

RETRO_PY = os.path.join(HERE, "retro.py")  # jobs run this with the same interpreter
PAGE_RE = re.compile(r"(daily|weekly)-[0-9A-Za-z_-]{1,40}\.html")  # fullmatch: no dots or slashes → no traversal
WEEK_SOON = "주간 회고는 아직 준비 중입니다."
NOT_MAC = "자동 실행은 macOS에서만 설정할 수 있습니다."

# ssh runs commands of its own (ProxyCommand, a trailing remote command), so only plain options and one host pass
SHELL_META = set("|&;<>()$`\\\"'*?[]{}!#")
SSH_VALUE_OPTS = {"p", "i", "l", "J"}  # port, key file, user, jump host
SSH_FLAG_OPTS = set("46ACTqv")
SSH_O_KEYS = {"port", "user", "identityfile", "identitiesonly", "proxyjump", "connecttimeout",
              "serveraliveinterval", "stricthostkeychecking", "userknownhostsfile", "hostkeyalias"}


def check_ssh(text):
    """→ (ssh command, "") or (None, why)."""
    text = text.strip()
    if not text.isprintable() or any(c in SHELL_META for c in text):
        return None, "따옴표·; | & $ 같은 특수문자는 쓸 수 없습니다."
    args = shlex.split(text)
    if args and args[0] != "ssh":  # "me@host" or "-p 10024 me@host" — same as `retro add-host`
        args, text = ["ssh"] + args, "ssh " + text
    if args[:1] != ["ssh"]:
        return None, "ssh로 시작하는 접속 명령을 넣어주세요. 예: ssh -p 10024 user@서버"
    hosts, i = [], 1
    while i < len(args):
        a = args[i]
        if a.startswith("-") and len(a) > 1:
            if a[1] in SSH_VALUE_OPTS or a[1] == "o":
                value = a[2:] or (args[i + 1] if i + 1 < len(args) else "")
                i += 0 if a[2:] else 1
                if not value or value.startswith("-") or (a[1] == "p" and not value.isdigit()):
                    return None, f"{a[:2]} 뒤의 값이 올바르지 않습니다."
                if a[1] == "o" and value.split("=")[0].lower() not in SSH_O_KEYS:
                    return None, f"이 화면에서는 쓸 수 없는 ssh 옵션입니다: -o {value}"
            elif not set(a[1:]) <= SSH_FLAG_OPTS:
                return None, f"이 화면에서는 쓸 수 없는 ssh 옵션입니다: {a}"
        else:
            hosts.append(a)
        i += 1
    if len(hosts) != 1:
        return None, "서버 주소 하나만 넣어주세요 (뒤에 실행할 명령은 붙이지 않습니다)."
    return " ".join(args), ""


_capture = threading.Lock()


def call(fn, **kw):
    """Run a retro.cmd_* and return (ok, what it printed): the CLI's own text doubles as the UI message."""
    buf = io.StringIO()
    with _capture, contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = fn(argparse.Namespace(**kw))
    return code == 0, buf.getvalue().strip()


# ---------------------------------------------------------------- jobs
class Job:
    """One `retro` / `retro week` run at a time; the home page polls /status for its lines."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"kind": None, "running": False, "lines": [], "ok": None, "page": None, "message": ""}

    def snapshot(self):
        with self.lock:
            return dict(self.state, lines=list(self.state["lines"]))

    def start(self, kind):
        with self.lock:
            if self.state["running"]:
                return False
            self.state = {"kind": kind, "running": True, "lines": [], "ok": None, "page": None, "message": ""}
        threading.Thread(target=self._run, args=(kind,), daemon=True).start()
        return True

    def _run(self, kind):
        try:
            ok, page, message = self._exec(kind)
        except Exception as err:  # noqa: BLE001 — a crashed run must not block the next one
            ok, page, message = False, None, f"실패 — {err}"
        with self.lock:
            self.state.update(running=False, ok=ok, page=page, message=message)

    def _exec(self, kind):
        """→ (ok: True/False/None for 'not available', page file name, message)."""
        argv = [sys.executable, RETRO_PY] + (["week"] if kind == "weekly" else []) + ["--no-open"]
        started, page = time.time(), None
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace",
                              env=dict(os.environ, PYTHONUNBUFFERED="1")) as proc:
            for line in proc.stdout:
                line = line.rstrip()
                if line.startswith("wrote "):  # render.py's last line names the page
                    page = os.path.basename(line[6:].strip())
                with self.lock:
                    self.state["lines"] = (self.state["lines"] + [line])[-300:]
        out = "\n".join(self.state["lines"])
        if kind == "weekly" and "invalid choice: 'week'" in out:
            return None, None, WEEK_SOON
        if proc.returncode == 0:
            if not (page and PAGE_RE.fullmatch(page)):
                page = newest_page("daily-" if kind == "daily" else "weekly-", started)
            return True, page, "완료"
        if "no events" in out:
            return False, None, "이 기간에 모인 활동 기록이 없습니다. 설정에서 소스가 켜져 있는지 확인하세요."
        last = next((l for l in reversed(out.splitlines()) if l.strip()), "")
        return False, None, f"실패 — {last}" if last else "실패"


def list_pages(prefix):
    try:
        names = os.listdir(retro.OUT_DIR)
    except OSError:
        return []
    return sorted((n for n in names if n.startswith(prefix) and PAGE_RE.fullmatch(n)), reverse=True)


def newest_page(prefix, since):
    got = [(os.path.getmtime(os.path.join(retro.OUT_DIR, n)), n) for n in list_pages(prefix)]
    got = [x for x in got if x[0] >= since - 1]
    return max(got)[1] if got else None


# ---------------------------------------------------------------- pages
CSS = """
:root{color-scheme:light dark}
body{margin:0;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif}
nav{display:flex;gap:16px;align-items:center;padding:12px 16px;border-bottom:1px solid GrayText}
main{max-width:720px;margin:0 auto;padding:0 16px 48px}
section{border:1px solid GrayText;border-radius:8px;padding:4px 16px 12px;margin:16px 0}
h1{font-size:22px}h2{font-size:17px;margin:12px 0 8px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;padding:8px 0;border-top:1px solid GrayText}
.row:first-of-type{border-top:0}
.grow{flex:1;min-width:0;overflow-wrap:anywhere}
form{margin:0}form.add{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
input[type=text]{flex:1;min-width:200px;font:inherit;padding:8px}
input[type=time]{font:inherit;padding:6px}
button{font:inherit;min-height:40px;padding:6px 14px;cursor:pointer}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
.muted{color:GrayText;font-size:14px}
.flash{border:2px solid;border-radius:8px;padding:8px 12px;margin:16px 0}
.flash.err{border-color:#c33}.flash.ok{border-color:#393}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;margin:8px 0;max-height:320px;overflow:auto}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:0 24px}
ul{padding-left:20px;margin:8px 0}li{padding:2px 0}
"""

# forms stay plain HTML; the only script shows progress and keeps a slow button from being pressed twice
SCRIPT = """
document.querySelectorAll('form').forEach(f => f.addEventListener('submit', e => {
  if (f.dataset.busy) return e.preventDefault();
  f.dataset.busy = '1';
  const b = f.querySelector('button[data-wait]');
  if (b) b.textContent = b.dataset.wait;
}));
const job = document.getElementById('job');
if (job && job.dataset.running === '1') {
  const lines = document.getElementById('lines');
  const tick = async () => {
    try {
      const s = await (await fetch('/status', {cache: 'no-store'})).json();
      lines.textContent = s.lines.join('\\n');
      lines.scrollTop = lines.scrollHeight;
      if (!s.running) return location.reload();
    } catch (e) {}
    setTimeout(tick, 1000);
  };
  setTimeout(tick, 1000);
}
"""

e = html.escape


def layout(title, body, nonce):
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{e(title)} · retro</title><style>{CSS}</style></head><body>'
            f'<nav><b>retro</b><a href="/">홈</a><a href="/settings">설정</a></nav>'
            f'<main>{body}</main><script nonce="{nonce}">{SCRIPT}</script></body></html>')


def flash_html(flash):
    if not flash:
        return ""
    kind, text = flash
    return f'<div class="flash {kind}"><pre>{e(text)}</pre></div>'


def post_button(action, label, fields=()):
    hidden = "".join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in fields)
    return f'<form method="post" action="{action}">{hidden}<button>{e(label)}</button></form>'


def page_label(name):
    stem = name[:-len(".html")].split("-", 1)[1]
    try:
        d = dt.date.fromisoformat(stem)
    except ValueError:
        return stem
    if name.startswith("weekly-"):
        return f"{d:%Y-%m-%d} ~ {d + dt.timedelta(days=6):%m-%d}"
    return f"{d:%Y-%m-%d} ({retro.render.WEEKDAYS[d.weekday()]})"


def home_body(job, flash):
    running = job["running"]
    week = post_button("/run", "이번 주 회고 만들기", [("kind", "weekly")]) if hasattr(retro, "cmd_week") \
        else f'<span class="muted">{WEEK_SOON}</span>'
    parts = [flash_html(flash), "<h1>회고</h1>",
             '<section><div class="actions">', post_button("/run", "오늘 회고 만들기", [("kind", "daily")]), week,
             '</div><p class="muted">이 컴퓨터와 등록한 서버의 기록을 모아 요약합니다. 1~2분 걸릴 수 있습니다.</p></section>']
    if job["kind"]:
        what = "오늘 회고" if job["kind"] == "daily" else "이번 주 회고"
        status = f"{what} 만드는 중…" if running else f"{what}: {job['message']}"
        link = f'<p><a href="/file/{e(job["page"])}" target="_blank" rel="noopener">결과 열기 →</a></p>' \
            if job["page"] else ""
        parts.append(f'<section id="job" data-running="{1 if running else 0}"><h2>{e(status)}</h2>'
                     f'<pre id="lines">{e(chr(10).join(job["lines"]))}</pre>{link}</section>')
    cols = []
    for prefix, title in (("daily-", "일간"), ("weekly-", "주간")):
        items = "".join(f'<li><a href="/file/{e(n)}" target="_blank" rel="noopener">{e(page_label(n))}</a></li>'
                        for n in list_pages(prefix))
        cols.append(f"<div><h2>{title}</h2>{f'<ul>{items}</ul>' if items else '<p class=muted>아직 없음</p>'}</div>")
    parts.append(f'<section><h2>만든 회고</h2><div class="cols">{"".join(cols)}</div></section>')
    return "".join(parts)


def schedule_state():
    """'HH:MM' when the LaunchAgent exists, else None."""
    try:
        with open(retro.PLIST, encoding="utf-8") as f:
            m = re.search(r"<key>Hour</key><integer>(\d+)</integer><key>Minute</key><integer>(\d+)</integer>", f.read())
    except OSError:
        return None
    return f"{int(m[1]):02d}:{int(m[2]):02d}" if m else "?"


def settings_body(flash):
    cfg = retro.load_config()
    off = set(cfg.get("off", []))
    parts = [flash_html(flash), "<h1>설정</h1>"]

    rows = "".join(
        f'<div class="row"><div class="grow"><b>{e(name)}</b> — {"꺼짐" if name in off else "켜짐"}'
        f'<div class="muted">{e(what)}</div></div>'
        + post_button("/settings/source", "켜기" if name in off else "끄기",
                      [("name", name), ("on", "1" if name in off else "0")]) + "</div>"
        for name, what in retro.SOURCES.items())
    parts.append(f'<section><h2>수집할 소스</h2>{rows}'
                 '<p class="muted">읽기만 합니다. 요약할 때 내 Anthropic 계정 외로는 보내지 않습니다.</p></section>')

    hosts = cfg.get("hosts", [])
    rows = "".join(f'<div class="row"><div class="grow"><b>{e(retro.host_name(h))}</b>'
                   f'<div class="muted">{e(h)}</div></div>{post_button("/settings/host/remove", "삭제", [("ssh", h)])}</div>'
                   for h in hosts) or '<p class="muted">등록된 서버 없음</p>'
    parts.append(f'<section><h2>ssh 서버</h2>{rows}'
                 '<form class="add" method="post" action="/settings/host/add">'
                 '<input type="text" name="ssh" placeholder="user@서버 또는 -p 10024 user@서버" autocomplete="off" required>'
                 '<button data-wait="연결 확인 중… (최대 10초)">추가</button></form>'
                 '<p class="muted">비밀번호 없이(ssh 키로) 접속되는 서버만 됩니다. 추가할 때 연결을 확인합니다.</p></section>')

    roots = cfg.get("git_roots", [])
    rows = "".join(f'<div class="row"><div class="grow">{e(d)}</div>'
                   f'{post_button("/settings/repo/remove", "삭제", [("path", d)])}</div>' for d in roots) \
        or '<p class="muted">추가한 저장소 없음 — AI 세션을 연 저장소만 수집합니다.</p>'
    parts.append(f'<section><h2>저장소</h2>{rows}'
                 '<form class="add" method="post" action="/settings/repo/add">'
                 '<input type="text" name="path" placeholder="~/code" autocomplete="off" required>'
                 '<button>추가</button></form>'
                 + (f'<div class="actions">{post_button("/settings/repo/pick", "폴더 선택…")}</div>'
                    if sys.platform == "darwin" else "")
                 + '<p class="muted">AI 세션 없이 커밋한 저장소, 또는 저장소들이 들어 있는 폴더.</p></section>')

    at = schedule_state()
    if sys.platform == "darwin":
        log = os.path.join(retro.OUT_DIR, "retro.log")
        last = dt.datetime.fromtimestamp(os.path.getmtime(log)).strftime("%m-%d %H:%M") if os.path.exists(log) else "아직 없음"
        body = (f'<p>{"켜짐 — 매일 " + at if at else "꺼짐"} <span class="muted">· 마지막 자동 실행: {last}</span></p>'
                '<form class="add" method="post" action="/settings/schedule">'
                f'<input type="time" name="at" value="{e(at if at and at != "?" else "22:00")}" required>'
                f'<button>{"시간 바꾸기" if at else "켜기"}</button></form>'
                + (f'<div class="actions">{post_button("/settings/unschedule", "끄기")}</div>' if at else ""))
    else:
        body = f'<p class="muted">{NOT_MAC}</p>'
    parts.append(f'<section><h2>자동 실행</h2>{body}</section>')

    backend = {"api": "Anthropic API", "claude": "Claude Code (claude -p)"}.get(retro.render.pick_backend("auto"), "없음 (숫자만)")
    parts.append('<section><h2>요약 상태</h2>'
                 f'<p>요약: {e(backend)} · API 키: {"있음" if os.environ.get("ANTHROPIC_API_KEY") else "없음"}'
                 f' · claude CLI: {"있음" if shutil.which("claude") else "없음"}</p></section>')
    return "".join(parts)


# ---------------------------------------------------------------- actions (POST) → (flash kind, message)
def act_run(server, form):
    kind = form.get("kind")
    if kind not in ("daily", "weekly"):
        return "err", "알 수 없는 작업입니다."
    if kind == "weekly" and not hasattr(retro, "cmd_week"):
        return "err", WEEK_SOON
    if not server.job.start(kind):
        return "err", "이미 만드는 중입니다. 끝난 뒤 다시 눌러주세요."
    return None


def act_source(_server, form):
    name, on = form.get("name", ""), form.get("on") == "1"
    if name not in retro.SOURCES:
        return "err", "알 수 없는 소스입니다."
    call(retro.cmd_toggle, cmd="on" if on else "off", source=name)  # same as `retro on/off NAME`
    return "ok", f"{name}: {'켜짐' if on else '꺼짐'}"


def act_host_add(_server, form):
    ssh_cmd, why = check_ssh(form.get("ssh", ""))
    if not ssh_cmd:
        return "err", why
    ok, out = call(retro.cmd_add_host, ssh=ssh_cmd)
    return ("ok" if ok else "err"), out


def act_host_remove(_server, form):
    call(retro.cmd_remove_host, name=form.get("ssh", ""))
    return "ok", f"삭제됨: {form.get('ssh', '')}"


def act_repo_add(_server, form):
    path = form.get("path", "").strip()
    if not path.isprintable() or not path.startswith(("/", "~")):
        return "err", "/ 또는 ~/ 로 시작하는 폴더 경로를 넣어주세요. 예: ~/code"
    ok, out = call(retro.cmd_add_repo, path=path)  # checks that the folder exists
    return ("ok" if ok else "err"), out


def act_repo_pick(_server, _form):
    """macOS folder picker, so nobody has to type a path."""
    if sys.platform != "darwin":
        return "err", "폴더 선택 창은 맥에서만 됩니다. 경로를 직접 넣어주세요."
    try:
        res = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "수집할 저장소 폴더를 고르세요")'],
                             capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return "err", "폴더 선택 창을 열지 못했습니다."
    path = res.stdout.strip()
    if res.returncode != 0 or not path:
        return "err", "선택을 취소했습니다."
    ok, out = call(retro.cmd_add_repo, path=path)
    return ("ok" if ok else "err"), out


def act_repo_remove(_server, form):
    call(retro.cmd_remove_repo, path=form.get("path", ""))
    return "ok", f"삭제됨: {form.get('path', '')}"


def act_schedule(_server, form):
    at = form.get("at", "")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        return "err", "시간은 HH:MM 형식으로 넣어주세요. 예: 22:00"
    if sys.platform != "darwin":
        return "err", NOT_MAC
    ok, out = call(retro.cmd_schedule, at=at)
    return ("ok" if ok else "err"), out


def act_unschedule(_server, _form):
    ok, out = call(retro.cmd_unschedule)
    return ("ok" if ok else "err"), out


ACTIONS = {"/run": act_run, "/settings/source": act_source, "/settings/host/add": act_host_add,
           "/settings/host/remove": act_host_remove, "/settings/repo/add": act_repo_add,
           "/settings/repo/remove": act_repo_remove, "/settings/repo/pick": act_repo_pick, "/settings/schedule": act_schedule,
           "/settings/unschedule": act_unschedule}


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "retro"

    def log_message(self, *_args):
        pass  # the first URL carries the token; keep it (and the noise) out of the terminal

    def do_GET(self):
        self.guarded("GET")

    def do_POST(self):
        self.guarded("POST")

    def guarded(self, method):
        try:
            self.route(method)
        except Exception as err:  # noqa: BLE001 — show the error instead of dropping the connection
            self.send_text(500, f"오류: {err}")

    def route(self, method):
        s = self.server
        origins = {f"http://127.0.0.1:{s.port}", f"http://localhost:{s.port}"}
        if "http://" + self.headers.get("Host", "") not in origins:  # DNS rebinding: evil.example → 127.0.0.1
            return self.send_text(403, "잘못된 주소입니다.")
        url = urlsplit(self.path)
        given = parse_qs(url.query).get("t", [""])[0]
        if method == "GET" and given and self.token_ok(given):
            # trade the URL token for a cookie and take it out of the address bar
            path = url.path if re.fullmatch(r"/[\w/.-]*", url.path) and not url.path.startswith("//") else "/"
            return self.redirect(path, cookie=True)
        if not self.token_ok(self.cookie_token()):
            return self.send_page("열 수 없음", "<h1>열 수 없음</h1><p>터미널에서 <b>retro app</b>을 실행했을 때 나온 "
                                  "주소로 열어주세요. 이미 껐다면 다시 실행하세요.</p>", 403)
        if method == "POST":
            src = self.headers.get("Origin") or "{0.scheme}://{0.netloc}".format(urlsplit(self.headers.get("Referer", "")))
            if src not in origins:  # CSRF: another site can make the browser POST here, but not with our Origin
                return self.send_text(403, "다른 사이트에서 온 요청은 받지 않습니다.")
            action = ACTIONS.get(url.path)
            if not action:
                return self.send_text(404, "없는 기능입니다.")
            length = max(0, min(int(self.headers.get("Content-Length") or 0), 64 * 1024))
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace")).items()}
            s.flash = action(s, form)
            return self.redirect("/" if url.path == "/run" else "/settings")
        if url.path == "/":
            flash, s.flash = s.flash, None
            return self.send_page("회고", home_body(s.job.snapshot(), flash))
        if url.path == "/settings":
            flash, s.flash = s.flash, None
            return self.send_page("설정", settings_body(flash))
        if url.path == "/status":
            return self.send(200, "application/json", json.dumps(s.job.snapshot(), ensure_ascii=False).encode())
        if url.path.startswith("/file/"):
            return self.send_file(unquote(url.path[len("/file/"):]))
        self.send_text(404, "없는 페이지입니다.")

    def token_ok(self, given):
        return bool(given) and secrets.compare_digest(given.encode(), self.server.token.encode())

    def cookie_token(self):
        try:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get(self.server.cookie)
        except CookieError:
            return ""
        return morsel.value if morsel else ""

    def send_file(self, name):
        path = os.path.join(retro.OUT_DIR, name)
        # exact name pattern, and it must really live in ~/Retro (not a symlink out of it)
        if not PAGE_RE.fullmatch(name) or not os.path.isfile(path) \
                or os.path.dirname(os.path.realpath(path)) != os.path.realpath(retro.OUT_DIR):
            return self.send_text(404, "없는 파일입니다.")
        with open(path, "rb") as f:
            body = f.read()
        # sandbox: the page gets an opaque origin, so nothing in it can call this server
        self.send(200, "text/html; charset=utf-8", body, {"Content-Security-Policy": "sandbox"})

    def send_page(self, title, body, code=200):
        nonce = secrets.token_urlsafe(12)
        csp = (f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; connect-src 'self'; "
               "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send(code, "text/html; charset=utf-8", layout(title, body, nonce).encode(), {"Content-Security-Policy": csp})

    def send_text(self, code, text):
        self.send(code, "text/plain; charset=utf-8", text.encode())

    def redirect(self, path, cookie=False):
        extra = {"Location": path}
        if cookie:
            extra["Set-Cookie"] = f"{self.server.cookie}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"
        self.send(303, "text/plain; charset=utf-8", b"", extra)

    def send(self, code, ctype, body, extra=None):
        self.send_response(code)
        headers = {"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                   "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin"}
        headers.update(extra or {})
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


def make_server(port=0):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # loopback only, never 0.0.0.0
    server.port = server.server_address[1]
    server.token = secrets.token_urlsafe(24)
    server.cookie = f"retro_{server.port}"  # cookies ignore ports; one name per run keeps runs apart
    server.job, server.flash = Job(), None
    return server


def serve(open_browser=True):
    server = make_server()
    url = f"http://127.0.0.1:{server.port}/?t={server.token}"
    print(f"retro 화면: {url}\n  이 주소는 이 컴퓨터에서만 열립니다. 끄려면 이 창에서 Ctrl+C", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nretro 화면을 껐습니다.", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(serve())
