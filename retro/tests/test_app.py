"""`retro app`: token, Host, Origin checks, settings actions, page files, background jobs.

No network: the server listens on an ephemeral 127.0.0.1 port in a thread, HOME is a
temp folder, and jobs run a fake retro.py.

Run: python3 -m unittest discover retro/tests
"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402
import retro  # noqa: E402

# stands in for retro.py: prints progress like cmd_run, writes a page, knows no `week`
FAKE_RETRO = r"""
import os, sys
if "week" in sys.argv:
    sys.stderr.write("usage: retro [-h]\nretro: error: argument cmd: invalid choice: 'week'\n")
    sys.exit(2)
out = os.path.join(os.path.expanduser("~"), "Retro", "daily-2026-09-24.html")
print("· 이 기기: claude 3", file=sys.stderr)
with open(out, "w") as f:
    f.write("<p>fake</p>")
print("wrote " + out, file=sys.stderr)
"""


class AppTestCase(unittest.TestCase):
    """Each test gets an empty temp HOME and its own server."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.out = os.path.join(self.home, "Retro")
        os.makedirs(self.out)
        self.config = os.path.join(self.home, ".config", "retro", "config.json")
        # retro.py computes its paths at import time, so point them at the temp HOME
        for name, value in (("CONFIG", self.config), ("OUT_DIR", self.out),
                            ("PLIST", os.path.join(self.home, "com.retro.daily.plist"))):
            p = mock.patch.object(retro, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.server = app.make_server()
        self.port, self.token = self.server.port, self.server.token
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        shutil.rmtree(self.home)

    def request(self, method, path, form=None, cookie=True, host=None, origin="self", referer=None):
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if cookie:
            h["Cookie"] = f"{self.server.cookie}={self.token}"
        if origin:
            h["Origin"] = f"http://127.0.0.1:{self.port}" if origin == "self" else origin
        if referer:
            h["Referer"] = referer
        body = None
        if form is not None:
            body = urlencode(form)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=h)
            res = conn.getresponse()
            return res.status, {k.lower(): v for k, v in res.getheaders()}, res.read().decode("utf-8")
        finally:
            conn.close()

    def get(self, path, **kw):
        return self.request("GET", path, origin=None, **kw)

    def post(self, path, form, **kw):
        return self.request("POST", path, form, **kw)

    def cfg(self):
        with open(self.config, encoding="utf-8") as f:
            return json.load(f)

    def touch(self, name, text="<p>page</p>"):
        with open(os.path.join(self.out, name), "w", encoding="utf-8") as f:
            f.write(text)


class SecurityTest(AppTestCase):
    def test_no_token_is_forbidden(self):
        for path in ("/", "/settings", "/status", "/file/daily-2026-09-24.html"):
            self.assertEqual(self.get(path, cookie=False)[0], 403, path)
        self.assertEqual(self.get("/?t=wrong", cookie=False)[0], 403)
        self.assertEqual(self.post("/settings/source", {"name": "chrome", "on": "0"}, cookie=False)[0], 403)

    def test_token_in_url_becomes_cookie(self):
        status, headers, _ = self.get(f"/settings?t={self.token}", cookie=False)
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/settings")  # token leaves the address bar
        cookie = headers["set-cookie"]
        self.assertIn(f"{self.server.cookie}={self.token}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("오늘 회고 만들기", body)
        # an odd path after the token never turns into an off-site redirect
        for path in ("//evil.example/", "/\\evil.example/"):
            location = self.get(f"{path}?t={self.token}", cookie=False)[1]["location"]
            self.assertTrue(location.startswith("/") and not location.startswith(("//", "/\\")), location)

    def test_wrong_host_is_rejected(self):
        self.assertEqual(self.get("/", host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.get("/", host="127.0.0.1")[0], 403)
        self.assertEqual(self.get("/", host=f"localhost:{self.port}")[0], 200)

    def test_post_needs_our_origin(self):
        form = {"name": "chrome", "on": "0"}
        self.assertEqual(self.post("/settings/source", form, origin=None)[0], 403)
        self.assertEqual(self.post("/settings/source", form, origin="http://evil.example")[0], 403)
        self.assertEqual(self.post("/settings/source", form, origin="null")[0], 403)
        self.assertEqual(self.post("/settings/source", form, origin=None,
                                   referer="http://evil.example/x")[0], 403)
        self.assertFalse(os.path.exists(self.config))
        self.assertEqual(self.post("/settings/source", form, origin=None,
                                   referer=f"http://127.0.0.1:{self.port}/settings")[0], 303)
        self.assertEqual(self.cfg()["off"], ["chrome"])

    def test_pages_are_locked_down(self):
        _, headers, _ = self.get("/")
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertEqual(headers["x-frame-options"], "DENY")


class FileTest(AppTestCase):
    def test_serves_only_page_files(self):
        self.touch("daily-2026-09-24.html", "<p>daily</p>")
        self.touch("weekly-2026-09-21.html", "<p>weekly</p>")
        self.touch("events.jsonl", "{}")
        self.touch("daily-2026-09-24.html.bak")
        status, headers, body = self.get("/file/daily-2026-09-24.html")
        self.assertEqual((status, body), (200, "<p>daily</p>"))
        self.assertEqual(headers["content-security-policy"], "sandbox")
        self.assertEqual(self.get("/file/weekly-2026-09-21.html")[2], "<p>weekly</p>")
        for bad in ("events.jsonl", "daily-2026-09-24.html.bak", "daily-2026-09-25.html",
                    "../.config/retro/config.json", "..%2F..%2Fetc%2Fpasswd", "daily-..%2F..%2Fx.html",
                    "%2Fetc%2Fpasswd", "Daily-2026-09-24.html"):
            self.assertEqual(self.get("/file/" + bad)[0], 404, bad)

    def test_symlink_out_of_retro_is_blocked(self):
        secret = os.path.join(self.home, "secret.txt")
        with open(secret, "w") as f:
            f.write("secret")
        os.symlink(secret, os.path.join(self.out, "daily-2026-01-01.html"))
        self.assertEqual(self.get("/file/daily-2026-01-01.html")[0], 404)

    def test_home_lists_pages_newest_first(self):
        for name in ("daily-2026-09-22.html", "daily-2026-09-24.html", "weekly-2026-09-14.html",
                     "weekly-2026-09-21.html", "notes.html"):
            self.touch(name)
        body = self.get("/")[2]
        self.assertLess(body.index("/file/daily-2026-09-24.html"), body.index("/file/daily-2026-09-22.html"))
        self.assertLess(body.index("/file/weekly-2026-09-21.html"), body.index("/file/weekly-2026-09-14.html"))
        self.assertIn("2026-09-24 (목)", body)
        self.assertIn("2026-09-21 ~ 09-27", body)
        self.assertNotIn("notes.html", body)


class SettingsTest(AppTestCase):
    def test_toggle_source(self):
        self.assertEqual(self.post("/settings/source", {"name": "chrome", "on": "0"})[0], 303)
        self.assertEqual(self.cfg()["off"], ["chrome"])
        self.assertIn("chrome: 꺼짐", self.get("/settings")[2])
        self.post("/settings/source", {"name": "chrome", "on": "1"})
        self.assertEqual(self.cfg()["off"], [])
        self.post("/settings/source", {"name": "nope", "on": "0"})
        self.assertEqual(self.cfg()["off"], [])
        self.assertIn("알 수 없는 소스", self.get("/settings")[2])

    def test_add_repo(self):
        missing = os.path.join(self.home, "nope")
        self.post("/settings/repo/add", {"path": missing})
        self.assertFalse(os.path.exists(self.config))
        self.assertIn("폴더가 없습니다", self.get("/settings")[2])
        self.post("/settings/repo/add", {"path": "relative/dir"})
        self.assertFalse(os.path.exists(self.config))
        repo = os.path.join(self.home, "code")
        os.makedirs(repo)
        self.post("/settings/repo/add", {"path": repo})
        self.assertEqual(self.cfg()["git_roots"], [repo])
        self.assertIn(repo, self.get("/settings")[2])
        self.post("/settings/repo/remove", {"path": repo})
        self.assertEqual(self.cfg()["git_roots"], [])

    def test_ssh_check(self):
        ok = ["ssh me@host", "ssh -p 10024 me@100.76.129.71", "ssh -p10024 -i ~/.ssh/id_ed25519 host",
              "ssh -o StrictHostKeyChecking=no -J jump me@host", "ssh -4C host"]
        for cmd in ok:
            self.assertEqual(app.check_ssh(cmd)[0], cmd, cmd)
        # like `retro add-host`, a bare target gets "ssh " in front
        self.assertEqual(app.check_ssh("me@host")[0], "ssh me@host")
        self.assertEqual(app.check_ssh("-p 10024 me@host")[0], "ssh -p 10024 me@host")
        bad = ["", "ssh", "ssh host; rm -rf ~", "ssh host uptime", "ssh $(id) host",
               "ssh -o ProxyCommand=touch host", "ssh -oLocalCommand=x host", "ssh -F cfg host",
               "ssh -p abc host", "ssh -J -oProxyCommand=x host", "ssh 'host'", "ssh host\nid", "scp host"]
        for cmd in bad:
            self.assertIsNone(app.check_ssh(cmd)[0], cmd)

    def test_repo_pick(self):
        repo = os.path.join(self.home, "picked")
        os.makedirs(repo)
        picked = subprocess.CompletedProcess([], 0, stdout=repo + "/\n", stderr="")
        with mock.patch.object(app.sys, "platform", "darwin"), \
                mock.patch.object(app.subprocess, "run", return_value=picked):
            self.post("/settings/repo/pick", {})
        self.assertEqual(len(self.cfg()["git_roots"]), 1)
        cancelled = subprocess.CompletedProcess([], 1, stdout="", stderr="User canceled.")
        with mock.patch.object(app.sys, "platform", "darwin"), \
                mock.patch.object(app.subprocess, "run", return_value=cancelled):
            self.post("/settings/repo/pick", {})
        self.assertEqual(len(self.cfg()["git_roots"]), 1)
        with mock.patch.object(app.sys, "platform", "linux"):
            self.post("/settings/repo/pick", {})
        self.assertEqual(len(self.cfg()["git_roots"]), 1)

    def test_add_host(self):
        with mock.patch.object(retro.subprocess, "run") as run:
            self.post("/settings/host/add", {"ssh": "ssh -o ProxyCommand=touch me@host"})
            run.assert_not_called()
        self.assertFalse(os.path.exists(self.config))
        self.assertIn("쓸 수 없는 ssh 옵션", self.get("/settings")[2])
        done = subprocess.CompletedProcess([], 0, stdout="Python 3.10.12\n", stderr="")
        with mock.patch.object(retro.shutil, "which", return_value="/usr/bin/ssh"), \
                mock.patch.object(retro.subprocess, "run", return_value=done) as run:
            self.post("/settings/host/add", {"ssh": "ssh -p 10024 me@gpu"})
        self.assertEqual(run.call_args[0][0][:1], ["ssh"])  # an argv list, never a shell string
        self.assertEqual(self.cfg()["hosts"], ["ssh -p 10024 me@gpu"])
        self.assertIn("OK — 서버 Python 3.10.12", self.get("/settings")[2])
        self.post("/settings/host/remove", {"ssh": "ssh -p 10024 me@gpu"})
        self.assertEqual(self.cfg()["hosts"], [])

    def test_schedule_input_is_checked(self):
        self.post("/settings/schedule", {"at": "25:00"})
        self.assertIn("HH:MM", self.get("/settings")[2])
        self.assertFalse(os.path.exists(retro.PLIST))

    def test_schedule_off_mac(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIn(app.NOT_MAC, self.get("/settings")[2])
            self.post("/settings/schedule", {"at": "22:00"})
            self.assertIn(app.NOT_MAC, self.get("/settings")[2])
        self.assertFalse(os.path.exists(retro.PLIST))

    def test_schedule_on_mac(self):
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        # launchctl is mocked, so this never loads a real LaunchAgent even on a Mac
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(retro.subprocess, "run", return_value=done) as run:
            self.assertIn("<p>꺼짐 ", self.get("/settings")[2])
            self.post("/settings/schedule", {"at": "07:30"})
            body = self.get("/settings")[2]
            self.assertIn("켜짐 — 매일 07:30", body)
            self.assertIn("매일 07:30에 회고 페이지가 열립니다", body)
            self.assertIn(["launchctl", "load", retro.PLIST], [c[0][0] for c in run.call_args_list])
            self.post("/settings/unschedule", {})
            self.assertFalse(os.path.exists(retro.PLIST))
            self.assertIn("자동 실행을 껐습니다", self.get("/settings")[2])

    def test_summary_row(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
            self.assertIn("API 키: 있음", self.get("/settings")[2])


class JobTest(AppTestCase):
    def setUp(self):
        super().setUp()
        fake = os.path.join(self.home, "fake_retro.py")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE_RETRO)
        p = mock.patch.object(app, "RETRO_PY", fake)
        p.start()
        self.addCleanup(p.stop)

    def wait(self):
        for _ in range(100):
            state = json.loads(self.get("/status")[2])
            if not state["running"]:
                return state
            time.sleep(0.1)
        self.fail("job did not finish")

    def test_daily_run(self):
        self.assertEqual(self.post("/run", {"kind": "daily"})[1]["location"], "/")
        state = self.wait()
        self.assertTrue(state["ok"], state)
        self.assertEqual(state["page"], "daily-2026-09-24.html")
        self.assertIn("· 이 기기: claude 3", state["lines"])
        body = self.get("/")[2]
        self.assertIn('href="/file/daily-2026-09-24.html"', body)
        self.assertIn("결과 열기", body)

    def test_one_job_at_a_time(self):
        server = self.server
        with server.job.lock:
            server.job.state["running"] = True
        self.post("/run", {"kind": "daily"})
        self.assertIn("이미 만드는 중", self.get("/")[2])

    def test_week_not_available(self):
        self.post("/run", {"kind": "weekly"})
        state = self.wait()
        self.assertEqual(state["message"], app.WEEK_SOON)
        self.assertIsNone(state["ok"])


if __name__ == "__main__":
    unittest.main()
