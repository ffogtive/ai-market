"""Summary cache, weekly-from-dailies prompt, page nav and index.html.

No network: the claude CLI backend is faked (as in test_weekly.py), every run
writes into a temp folder.

Run: python3 -m unittest discover retro/tests
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import render  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=9))
MON = dt.date(2026, 9, 21)  # a Monday
WED = MON + dt.timedelta(days=2)
DAYS = render.week_days(MON)

DAILY = {
    "one_line": "결제 흐름을 고쳤다",
    "done": [{"time": "09:05", "project": "shop", "result": "결제 버그 수정", "evidence": "Claude Code"}],
    "decisions": [], "blockers": [], "tomorrow": [],
    "activity_mix": [{"type": "개발", "percent": 100}],
    "project_labels": [],
}
WEEKLY = {
    "one_line": "결제와 가격 페이지를 냈다",
    "highlights": [{"days": "월", "project": "shop", "result": "결제 버그 수정"}],
    "decisions": [], "blockers": [], "next_week": [],
    "activity_mix": [{"type": "개발", "percent": 100}],
    "project_labels": [],
}


def ev(day, hm, source, text, project="shop", actor="human"):
    h, m = hm
    ts = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)
    return {"source": source, "ts": ts, "project": project, "text": text, "actor": actor, "host": "mac"}


def fixture():
    return [ev(MON, (9, 5), "claude", "fix checkout"), ev(MON, (9, 30), "claude", "add tests"),
            ev(MON, (12, 0), "git", "fix: checkout (+10/-2)"),
            ev(WED, (14, 0), "codex", "pricing page", "blog"), ev(WED, (15, 0), "git", "feat: pricing (+5/-1)", "blog")]


class Site(unittest.TestCase):
    """A temp ~/Retro, an events file, and a fake claude CLI that counts its calls."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.src = os.path.join(self.dir, "events.jsonl")
        self.events = fixture()
        self.save_events()
        self.prompts = []
        self.fail = None

    def tearDown(self):
        shutil.rmtree(self.dir)

    def save_events(self):
        with open(self.src, "w", encoding="utf-8") as f:
            for e in self.events:
                f.write(json.dumps(dict(e, ts=e["ts"].isoformat()), ensure_ascii=False) + "\n")

    def add_event(self, *args, **kw):
        self.events.append(ev(*args, **kw))
        self.save_events()

    def fake_cli(self, prompt, system, schema, task, drop_api_key=False):
        self.prompts.append(prompt)
        if self.fail:
            raise self.fail
        return WEEKLY if schema is render.WEEK_SCHEMA else DAILY

    def render(self, day=MON, *extra, week=False):
        kind = "weekly" if week else "daily"
        out = os.path.join(self.dir, f"{kind}-{render.week_days(day)[0] if week else day}.html")
        argv = (["--week"] if week else []) + ["--date", str(day), "--out", out, self.src] + list(extra)
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(render, "summarize", mock.Mock(side_effect=AssertionError("API used"))), \
                mock.patch.object(render, "summarize_cli", self.fake_cli), \
                mock.patch.object(render.shutil, "which", lambda name: "/bin/claude"), \
                redirect_stderr(err):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            self.assertEqual(render.main(argv), 0, err.getvalue())
        return self.read(os.path.basename(out)), err.getvalue()

    def read(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return f.read()

    def cache(self, kind="daily", day=MON):
        with open(render.cache_path(self.dir, kind, day), encoding="utf-8") as f:
            return json.load(f)


class CacheTest(Site):
    def test_hit_skips_llm(self):
        page, _ = self.render()
        self.assertIn("결제 흐름을 고쳤다", page)
        saved = self.cache()
        self.assertLessEqual({"prompt_sha256", "created", "backend", "summary"}, set(saved))
        self.assertEqual((saved["backend"], saved["summary"]), ("claude", DAILY))
        self.assertEqual(saved["numbers"], {"prompts": 2, "commits": 1})

        page, err = self.render()
        self.assertEqual(len(self.prompts), 1)  # no second LLM call
        self.assertIn("· 요약: 캐시 재사용", err)
        self.assertIn("결제 흐름을 고쳤다", page)
        self.assertIn("요약 재사용", page)

    def test_new_logs_summarize_again(self):
        self.render()
        before = self.cache()["prompt_sha256"]
        self.add_event(MON, (18, 0), "claude", "write the release note")
        _, err = self.render()
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("write the release note", self.prompts[1])
        self.assertNotIn("캐시 재사용", err)
        self.assertNotEqual(self.cache()["prompt_sha256"], before)

    def test_failure_shows_the_older_summary(self):
        self.render()
        saved = self.cache()
        self.add_event(MON, (18, 0), "claude", "write the release note")
        self.fail = RuntimeError("claude CLI failed: Not logged in · Please run /login")
        page, _ = self.render()
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("결제 흐름을 고쳤다", page)  # not numbers only
        foot = re.search(r'<p class="foot">(.*?)</p>', page).group(1)
        self.assertIn("이전 요약 표시 — ", foot)
        self.assertIn("Claude 로그인이 만료", foot)  # LAST_FAILURE
        self.assertNotIn("숫자만 표시", foot)
        self.assertEqual(self.cache(), saved)  # the older summary stays; next run tries again

    def test_failure_without_cache_is_numbers_only(self):
        self.fail = RuntimeError("boom")
        page, _ = self.render()
        self.assertIn("숫자만 표시 — 요약 실패: boom", page)
        self.assertFalse(os.path.exists(render.cache_path(self.dir, "daily", MON)))

    def test_refresh_forces(self):
        self.render()
        _, err = self.render(MON, "--refresh")
        self.assertEqual(len(self.prompts), 2)
        self.assertNotIn("캐시 재사용", err)
        self.render(MON, "--refresh", week=True)
        self.render(MON, "--refresh", week=True)
        self.assertEqual(len(self.prompts), 4)

    def test_weekly_cache(self):
        self.render(week=True)
        _, err = self.render(week=True)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("· 요약: 캐시 재사용", err)
        self.assertEqual(self.cache("weekly")["summary"], WEEKLY)

    def test_llm_none_never_uses_the_cache(self):
        self.render()
        page, _ = self.render(MON, "--llm", "none")
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("숫자만 표시 — 요약 끔", page)
        self.assertNotIn("결제 흐름을 고쳤다", page)

    def test_cache_beside_out_or_cache_dir(self):
        other = os.path.join(self.dir, "cache")
        self.render(MON, "--cache-dir", other)
        self.assertTrue(os.path.exists(render.cache_path(other, "daily", MON)))
        self.assertFalse(os.path.exists(render.cache_path(self.dir, "daily", MON)))

    def test_corrupt_cache_is_ignored(self):
        with open(render.cache_path(self.dir, "daily", MON), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.render()
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.cache()["summary"], DAILY)

    def test_retro_passes_out_dir_and_refresh(self):
        import retro
        self.assertEqual(retro.cache_args(argparse.Namespace(refresh=False)), ["--cache-dir", retro.OUT_DIR])
        self.assertEqual(retro.cache_args(argparse.Namespace()), ["--cache-dir", retro.OUT_DIR])  # callers without the flag
        self.assertEqual(retro.cache_args(argparse.Namespace(refresh=True))[-1], "--refresh")


class WeekFromDailiesTest(Site):
    def busy(self):
        out = []
        for d in DAYS:
            for i in range(300):
                out.append(ev(d, (8 + i // 60, i % 60), "claude", f"prompt {i} " + "x" * 200))
        return out

    def test_prompt_includes_dailies_and_shrinks(self):
        by = render.split_days(self.busy(), DAYS)
        stats = render.week_stats(by)
        plain = render.build_week_prompt(DAYS, by, stats)
        dailies = {d: (DAILY, True) for d in DAYS}
        p = render.build_week_prompt(DAYS, by, stats, dailies)
        self.assertEqual(plain, render.build_week_prompt(DAYS, by, stats, {}))  # no-cache path unchanged
        self.assertNotIn("일간 요약", plain)
        self.assertIn("300줄 중 60줄", plain)
        self.assertIn("# 09/21 (월) 일간 요약\n한 줄: 결제 흐름을 고쳤다\n- 09:05 [shop] 결제 버그 수정\n# 09/21 (월) (300줄 중 20줄", p)
        self.assertEqual(p.count("일간 요약\n"), 7)
        self.assertLess(len(p), len(plain) / 2)
        self.assertNotIn("미반영", p)

    def test_only_days_with_a_saved_daily(self):
        by = render.split_days(self.busy(), DAYS)
        stats = render.week_stats(by)
        p = render.build_week_prompt(DAYS, by, stats, {MON: (DAILY, False)})
        self.assertIn("# 09/21 (월) 일간 요약 (그 뒤 로그 일부 미반영)", p)
        self.assertIn("# 09/21 (월) (300줄 중 20줄", p)
        self.assertIn("# 09/22 (화) (300줄 중 60줄", p)

    def test_weekly_run_reads_saved_dailies(self):
        self.render(MON)
        self.render(week=True)
        weekly_prompt = self.prompts[-1]
        self.assertIn("# 09/21 (월) 일간 요약\n한 줄: 결제 흐름을 고쳤다", weekly_prompt)
        self.assertNotIn("09/23 (수) 일간 요약", weekly_prompt)  # Wed has no daily summary
        self.assertIn("일간 요약이 있는 날은", weekly_prompt)

        self.add_event(MON, (20, 0), "claude", "late work")  # Mon's saved daily no longer covers every log
        self.render(week=True)
        self.assertIn("# 09/21 (월) 일간 요약 (그 뒤 로그 일부 미반영)", self.prompts[-1])
        self.assertIn("late work", self.prompts[-1])

    def test_new_daily_summary_changes_the_weekly_hash(self):
        self.render(week=True)
        self.render(MON)
        _, err = self.render(week=True)
        self.assertEqual(len(self.prompts), 3)
        self.assertNotIn("캐시 재사용", err)


def hrefs(page):
    return re.findall(r'href="([^"]+)"', page)


def nav(page):
    return re.search(r'<nav class="pnav">(.*?)</nav>', page).group(1)


class NavTest(Site):
    def old_page(self, name, text="<html>old page, no nav</html>"):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_links_only_to_existing_pages(self):
        mon, _ = self.render(MON)
        self.assertEqual(nav(mon), '<span class="off">← 이전 날</span> · <span class="off">주간 보기</span> · '
                                   '<a href="index.html">목록</a> · <span class="off">다음 날 →</span>')
        wed, _ = self.render(WED)
        self.assertIn('<a href="daily-2026-09-21.html">← 이전 날</a>', nav(wed))  # nearest earlier page
        self.assertIn('<span class="off">다음 날 →</span>', nav(wed))
        # Mon was rendered first; its row now points to Wed
        self.assertIn('<a href="daily-2026-09-23.html">다음 날 →</a>', nav(self.read("daily-2026-09-21.html")))

        week, _ = self.render(week=True)
        self.assertEqual(nav(week), '<span class="off">← 지난주</span> · <a href="index.html">목록</a> · '
                                    '<span class="off">다음 주 →</span>')
        self.assertIn('<td><a href="daily-2026-09-21.html">09/21 (월)</a></td>', week)
        self.assertIn('<td><a href="daily-2026-09-23.html">09/23 (수)</a></td>', week)
        self.assertIn("<td>09/22 (화)</td>", week)  # no daily page, no link
        for name in ("daily-2026-09-21.html", "daily-2026-09-23.html"):
            self.assertIn('<a href="weekly-2026-09-21.html">주간 보기</a>', nav(self.read(name)))

        for name in os.listdir(self.dir):
            if name.endswith(".html"):
                for href in hrefs(self.read(name)):
                    self.assertTrue(os.path.exists(os.path.join(self.dir, href)), f"{name} → {href}")

    def test_weeks_link_to_each_other(self):
        self.old_page("weekly-2026-09-14.html")
        week, _ = self.render(week=True)
        self.assertIn('<a href="weekly-2026-09-14.html">← 지난주</a>', nav(week))

    def test_weekly_table_links_days_rendered_later(self):
        self.render(week=True)
        self.render(WED)
        self.assertIn('<td><a href="daily-2026-09-23.html">09/23 (수)</a></td>', self.read("weekly-2026-09-21.html"))

    def test_old_pages_are_left_alone(self):
        self.old_page("daily-2026-09-20.html")
        mon, _ = self.render(MON)
        self.assertIn('<a href="daily-2026-09-20.html">← 이전 날</a>', nav(mon))
        self.assertEqual(self.read("daily-2026-09-20.html"), "<html>old page, no nav</html>")


class IndexTest(Site):
    def test_newest_first_grouped_by_week(self):
        with open(os.path.join(self.dir, "daily-2026-09-15.html"), "w", encoding="utf-8") as f:
            f.write("<html>a page from before this change</html>")
        self.render(MON)
        self.render(WED, "--llm", "none")
        self.render(week=True)
        index = self.read("index.html")
        order = [index.index(x) for x in ('href="weekly-2026-09-21.html"', 'href="daily-2026-09-23.html"',
                                          'href="daily-2026-09-21.html"', "2026년 9월 14일 – 20일",
                                          'href="daily-2026-09-15.html"')]
        self.assertEqual(order, sorted(order))
        self.assertIn("결제와 가격 페이지를 냈다", index)  # the week's saved one-liner
        self.assertIn('<td>결제 흐름을 고쳤다</td><td class="num">2</td><td class="num">1</td>', index)
        self.assertIn('<a href="daily-2026-09-23.html">09/23 (수)</a></td><td></td><td class="num"></td>', index)  # no summary yet
        self.assertIn("주간 페이지 없음", index)  # week of 9/14 has only a daily page
        for href in hrefs(index):
            self.assertTrue(os.path.exists(os.path.join(self.dir, href)), href)

    def test_does_not_overwrite_a_foreign_index(self):
        with open(os.path.join(self.dir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html>my own site</html>")
        self.render(MON)
        self.assertEqual(self.read("index.html"), "<html>my own site</html>")


class RetroCommandTest(unittest.TestCase):
    """`retro` and `retro week` with --refresh, in an empty temp HOME with one Claude Code transcript."""

    def test_pages_nav_and_index_in_retro_folder(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)
        day = now.astimezone().date()
        folder = os.path.join(home, ".claude", "projects", "-work-app")
        os.makedirs(folder)
        with open(os.path.join(folder, "s.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "cwd": "/work/app",
                                "message": {"content": "캐시 만들기"}}, ensure_ascii=False) + "\n")
        env = dict(os.environ, HOME=home)
        for args in ([], ["week"]):
            res = subprocess.run([sys.executable, os.path.join(HERE, "retro.py")] + args
                                 + ["--llm", "none", "--no-open", "--refresh", "--date", str(day)],
                                 env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(res.returncode, 0, res.stderr)
        out = os.path.join(home, "Retro")
        monday = render.week_days(day)[0]
        with open(os.path.join(out, f"daily-{day}.html"), encoding="utf-8") as f:
            self.assertIn(f'<a href="weekly-{monday}.html">주간 보기</a>', f.read())
        with open(os.path.join(out, "index.html"), encoding="utf-8") as f:
            index = f.read()
        self.assertIn(f'href="daily-{day}.html"', index)
        self.assertIn(f'href="weekly-{monday}.html"', index)


if __name__ == "__main__":
    unittest.main()
