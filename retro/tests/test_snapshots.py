"""Last good collection per source (retro.save_snapshot / fall_back) and the 관측 범위 failure line."""
import datetime as dt
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import render  # noqa: E402
import retro  # noqa: E402


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        p = mock.patch.object(retro, "SNAP_DIR", os.path.join(self.dir, "sources"))
        p.start()
        self.addCleanup(p.stop)
        retro.LAST_FAILURES.clear()

    def test_fall_back_uses_the_last_good_events(self):
        good = [{"source": "claude", "ts": "2026-09-24T21:10:00+09:00", "project": "p", "text": "x",
                 "actor": "agent", "host": "gpu"}]
        retro.save_snapshot("host-100.76.129.71", good)
        with redirect_stderr(io.StringIO()):
            got = retro.fall_back("host-100.76.129.71", "서버 100.76.129.71", "연결 실패")
        self.assertEqual(got, good)
        self.assertEqual(len(retro.LAST_FAILURES), 1)
        self.assertIn("서버 100.76.129.71: 연결 실패 — 마지막 성공(", retro.LAST_FAILURES[0])
        self.assertIn("기록으로 대신함", retro.LAST_FAILURES[0])
        self.assertEqual(retro.failed_args(), ["--failed", retro.LAST_FAILURES[0]])

    def test_nothing_saved_yet(self):
        with redirect_stderr(io.StringIO()):
            got = retro.fall_back("local-chrome", "chrome", "권한 없음")
        self.assertEqual(got, [])
        self.assertEqual(retro.LAST_FAILURES, ["chrome: 권한 없음 — 이번 회고에서 빠짐"])

    def test_page_shows_the_failure(self):
        ev = {"source": "git", "ts": dt.datetime(2026, 9, 24, 21, 59), "host": "mac"}
        with mock.patch.object(render, "COLLECT_FAILURES", ["서버 gpu: 연결 실패 — 이번 회고에서 빠짐"]):
            html = render.coverage([ev])
        self.assertIn("⚠️ 수집 실패 — 서버 gpu: 연결 실패", html)
        self.assertNotIn("수집 실패", render.coverage([ev]))


if __name__ == "__main__":
    unittest.main()


class RemoteOutputTest(unittest.TestCase):
    """The 9/24 22:00 run: over ssh without LANG, Python 3.6 printed the counts, then died on the first
    Korean prompt, and the run treated the counts as success."""

    def test_collect_writes_utf8_even_with_an_ascii_stdout(self):
        import json
        import subprocess
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        os.makedirs(os.path.join(home, ".claude", "projects", "p"))
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with open(os.path.join(home, ".claude", "projects", "p", "s.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "cwd": "/w/retro", "timestamp": now,
                                "message": {"content": "회고 페이지 고쳐줘"}}, ensure_ascii=False) + "\n")
        collect = os.path.join(os.path.dirname(__file__), "..", "collect.py")
        env = dict(os.environ, HOME=home, PYTHONIOENCODING="ascii")
        res = subprocess.run([sys.executable, collect, "--days", "1", "--stdout", "--no-chrome", "--skip", "git"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
        self.assertNotIn(b"Traceback", res.stderr)
        lines = [json.loads(l) for l in res.stdout.decode("utf-8").splitlines()]
        self.assertEqual([e["text"] for e in lines], ["회고 페이지 고쳐줘"])

    def test_remote_failure(self):
        crash = ("claude      25 events\ncodex        0 events\nTraceback (most recent call last):\n  File x\n"
                 "UnicodeEncodeError: 'ascii' codec can't encode")
        self.assertTrue(retro.remote_failure([], crash).startswith("수집 중 오류: UnicodeEncodeError"))
        self.assertTrue(retro.remote_failure([{"a": 1}], crash))  # partial output with a crash is still a failure
        self.assertEqual(retro.remote_failure([], "claude      25 events\ncodex 0 events"), "기록 25건을 셌지만 받지 못함")
        self.assertEqual(retro.remote_failure([{"a": 1}], "claude 1 events"), "")
        self.assertEqual(retro.remote_failure([], "claude 0 events\ncodex 0 events"), "")  # a quiet day is fine
        self.assertIn("연결 실패(ssh: connect", retro.remote_failure([], "ssh: connect to host x: Operation timed out"))
