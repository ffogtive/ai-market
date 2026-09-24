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
