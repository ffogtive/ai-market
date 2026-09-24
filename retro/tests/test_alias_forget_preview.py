"""Project aliases, `retro forget`, and the summary preview — CLI, render.py and `retro app`.

No network, no LLM: HOME is a temp folder, the collectors are faked, and every summary
backend raises if it is called.

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
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(TESTS)
sys.path.insert(0, HERE)
sys.path.insert(0, TESTS)
import app  # noqa: E402
import render  # noqa: E402
import retro  # noqa: E402
from test_app import AppTestCase  # noqa: E402

MON = dt.date(2026, 9, 21)
TUE, WED = MON + dt.timedelta(days=1), MON + dt.timedelta(days=2)


def ev(day, hm, source, text, project="", actor="human"):
    return {"source": source, "ts": f"{day}T{hm}:00+09:00", "project": project, "text": text, "actor": actor,
            "host": "mac"}


def events():
    """Two work folders of one project, a web site and a YouTube channel that share that folder's name."""
    return [ev(TUE, "09:00", "claude", "로그인 고쳐줘", "ai-market-2"),
            ev(TUE, "10:00", "codex", "테스트 추가", "ai-market"),
            ev(WED, "09:05", "claude", "결제 버그 고쳐줘", "ai-market-2"),
            ev(WED, "09:30", "chrome", "ai-market-2 docs", "ai-market-2"),
            ev(WED, "09:40", "youtube", "강의", "ai-market-2"),
            ev(WED, "10:00", "git", "fix: checkout (+10/-2)", "ai-market-2"),
            ev(WED, "11:00", "claude.ai", "[질문] 요금제"),
            ev(WED, "14:00", "claude", "가격 페이지", "blog")]


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def no_llm():
    """Every way a summary could be sent fails the test."""
    return [mock.patch.object(render, "summarize", mock.Mock(side_effect=AssertionError("API called"))),
            mock.patch.object(render, "summarize_cli", mock.Mock(side_effect=AssertionError("claude CLI called")))]


class TempHome(unittest.TestCase):
    """retro.py's paths point into an empty temp HOME."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        self.out = os.path.join(self.home, "Retro")
        self.config = os.path.join(self.home, ".config", "retro", "config.json")
        for name, value in (("CONFIG", self.config), ("OUT_DIR", self.out),
                            ("EXTENSION_DIR", os.path.join(self.home, "Downloads", "retro"))):
            p = mock.patch.object(retro, name, value)
            p.start()
            self.addCleanup(p.stop)
        for p in no_llm():
            p.start()
            self.addCleanup(p.stop)

    def run_cmd(self, fn, **kw):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fn(argparse.Namespace(**kw))
        return code, out.getvalue(), err.getvalue()

    def cfg(self):
        with open(self.config, encoding="utf-8") as f:
            return json.load(f)

    def fake_collect(self, rows):
        """collect.py stand-in: returns these events for this machine."""
        return mock.patch.object(retro, "collect_local", return_value=([dict(r) for r in rows], "claude 3 events\n"))


# ---------------------------------------------------------------- aliases
class AliasTest(TempHome):
    def test_only_non_web_events_are_merged(self):
        got = retro.apply_aliases(events(), {"ai-market-2": "AI 마켓", "ai-market": "AI 마켓"})
        by = {(e["source"], e["text"]): e for e in got}
        self.assertEqual(by[("claude", "로그인 고쳐줘")]["project"], "AI 마켓")
        self.assertEqual(by[("claude", "로그인 고쳐줘")]["project_raw"], "ai-market-2")
        self.assertEqual(by[("codex", "테스트 추가")]["project"], "AI 마켓")
        self.assertEqual(by[("git", "fix: checkout (+10/-2)")]["project"], "AI 마켓")
        for key in (("chrome", "ai-market-2 docs"), ("youtube", "강의")):  # a site / channel is never a work project
            self.assertEqual(by[key]["project"], "ai-market-2")
            self.assertNotIn("project_raw", by[key])
        self.assertEqual(by[("claude.ai", "[질문] 요금제")]["project"], "")
        self.assertEqual(by[("claude", "가격 페이지")]["project"], "blog")
        self.assertNotIn("project_raw", by[("claude", "가격 페이지")])
        self.assertEqual(retro.apply_aliases(events(), None), events())
        self.assertEqual(retro.apply_aliases(events(), ["not", "a", "dict"]), events())

    def test_applied_before_events_jsonl_is_written(self):
        os.makedirs(os.path.dirname(self.config))
        retro.save_config({"hosts": [], "aliases": {"ai-market-2": "AI 마켓"}})
        with self.fake_collect(events()), redirect_stderr(io.StringIO()):
            path = retro.collect_all(WED, 3, no_chrome=True)
        self.assertEqual(path, os.path.join(self.out, "events.jsonl"))
        saved = retro.parse_jsonl(read(path))
        self.assertEqual({e["project"] for e in saved if e["source"] == "claude"}, {"AI 마켓", "blog"})
        self.assertEqual({e["project"] for e in saved if e["source"] in ("chrome", "youtube")}, {"ai-market-2"})
        # the app offers raw names from here: the aliased folder is still listed under its own name, web never
        self.assertEqual(retro.recent_projects(), [("ai-market-2", 3), ("ai-market", 1), ("blog", 1)])

    def test_cli_add_list_remove(self):
        code, _, err = self.run_cmd(retro.cmd_alias, raw="ai-market-2", name=" AI  마켓 ", remove=False)
        self.assertEqual(code, 0, err)
        self.assertIn("ai-market-2 → AI 마켓", err)
        self.run_cmd(retro.cmd_alias, raw="ai-market", name="AI 마켓", remove=False)
        self.assertEqual(self.cfg()["aliases"], {"ai-market-2": "AI 마켓", "ai-market": "AI 마켓"})
        _, out, _ = self.run_cmd(retro.cmd_aliases)
        self.assertEqual(out, "ai-market\t→ AI 마켓\nai-market-2\t→ AI 마켓\n")
        code, _, _ = self.run_cmd(retro.cmd_alias, raw="ai-market-2", name="", remove=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.cfg()["aliases"], {"ai-market": "AI 마켓"})
        self.assertEqual(self.run_cmd(retro.cmd_alias, raw="nope", name="", remove=True)[0], 1)
        for raw, name in (("", "x"), ("a", ""), ("a", "a"), ("a", "x" * 101), ("a\x00", "x")):
            self.assertEqual(self.run_cmd(retro.cmd_alias, raw=raw, name=name, remove=False)[0], 1, (raw, name))
        self.assertEqual(self.cfg()["aliases"], {"ai-market": "AI 마켓"})
        self.run_cmd(retro.cmd_alias, raw="ai-market", name="", remove=True)
        self.assertIn("묶은 프로젝트 없음", self.run_cmd(retro.cmd_aliases)[1])

    def test_cli_commands(self):
        env = dict(os.environ, HOME=self.home)

        def run(*args):
            return subprocess.run([sys.executable, os.path.join(HERE, "retro.py")] + list(args),
                                  env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run("alias", "ai-market-2", "AI 마켓").returncode, 0)
        self.assertIn("ai-market-2\t→ AI 마켓", run("aliases").stdout)
        self.assertEqual(run("alias", "--remove", "ai-market-2").returncode, 0)
        self.assertIn("묶은 프로젝트 없음", run("aliases").stdout)

    def test_alias_wins_over_the_llm_label(self):
        rows = retro.apply_aliases(events(), {"ai-market-2": "AI 마켓"})
        path = os.path.join(self.home, "events.jsonl")
        write_jsonl(path, rows)
        loaded = render.load([path])
        summary = {"project_labels": [{"raw": "AI 마켓", "label": "엉뚱한 이름"}, {"raw": "blog", "label": "블로그"}]}
        name = render.labeler(summary, loaded)
        self.assertEqual((name("AI 마켓"), name("blog")), ("AI 마켓", "블로그"))
        self.assertEqual(render.labeler(summary)("AI 마켓"), "엉뚱한 이름")  # no alias in the events: LLM label as before
        day = [e for e in loaded if e["ts"].date() == WED]
        page = render.render_page(WED, render.day_stats(day), dict(summary, one_line="x"), loaded)
        self.assertIn("AI 마켓", page)
        self.assertNotIn("엉뚱한 이름", page)
        self.assertIn("블로그", page)


class AppAliasTest(AppTestCase):
    def setUp(self):
        super().setUp()
        write_jsonl(os.path.join(self.out, "events.jsonl"), events())

    def test_settings_offers_recent_non_web_names(self):
        body = self.get("/settings")[2]
        self.assertIn("프로젝트 이름 묶기", body)
        self.assertIn('<option value="ai-market-2">ai-market-2 (3건)</option>', body)
        self.assertIn('<option value="blog">', body)
        self.assertNotIn("(질문)", body)
        os.remove(os.path.join(self.out, "events.jsonl"))
        self.assertIn('<input type="text" name="raw"', self.get("/settings")[2])  # nothing collected: type it

    def test_add_and_remove(self):
        form = {"raw": "ai-market-2", "name": "AI 마켓"}
        self.assertEqual(self.post("/settings/alias/add", form, origin=None)[0], 403)
        self.assertEqual(self.post("/settings/alias/add", form, origin="http://evil.example")[0], 403)
        self.assertEqual(self.post("/settings/alias/add", form, cookie=False)[0], 403)
        self.assertEqual(self.post("/settings/alias/add", form, host=f"evil.example:{self.port}")[0], 403)
        self.assertFalse(os.path.exists(self.config))
        self.assertEqual(self.post("/settings/alias/add", form)[0], 303)
        self.assertEqual(self.cfg()["aliases"], {"ai-market-2": "AI 마켓"})
        body = self.get("/settings")[2]
        self.assertIn("<b>ai-market-2</b> → AI 마켓", body)
        self.assertIn("ai-market-2 (3건) → AI 마켓</option>", body)
        self.assertEqual(self.post("/settings/alias/remove", {"raw": "ai-market-2"}, origin="http://evil.example")[0], 403)
        self.assertEqual(self.cfg()["aliases"], {"ai-market-2": "AI 마켓"})
        self.post("/settings/alias/remove", {"raw": "ai-market-2"})
        self.assertEqual(self.cfg()["aliases"], {})
        self.assertIn("묶은 프로젝트 없음", self.get("/settings")[2])

    def test_bad_input_is_refused(self):
        self.post("/settings/alias/add", {"raw": "ai-market-2", "name": ""})
        self.assertIn("묶을 프로젝트 이름을 넣어주세요", self.get("/settings")[2])
        self.assertFalse(os.path.exists(self.config))


# ---------------------------------------------------------------- forget
SOURCES = (".claude/projects/-work-app/s.jsonl", ".codex/history.jsonl", "code/app/.git/HEAD",
           "Library/Application Support/Google/Chrome/Default/History", "Downloads/retro/browser-2026-09-23.jsonl")


def make_site(home, out):
    """A real ~/Retro: daily pages for Tue and Wed, the week page, index.html (render.py --llm none), notes, a log,
    a file of the user's own; and the source logs retro reads."""
    for rel in SOURCES:
        path = os.path.join(home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("source " + rel)
    src = os.path.join(out, "events.jsonl")
    write_jsonl(src, events())
    with redirect_stderr(io.StringIO()):
        for day in (TUE, WED):
            render.main(["--date", str(day), "--llm", "none", "--out", os.path.join(out, f"daily-{day}.html"), src])
        render.main(["--week", "--date", str(MON), "--llm", "none", "--out", os.path.join(out, f"weekly-{MON}.html"), src])
    for name, text in (("notes-2026-09-22.json", "{}"), ("notes-2026-09-23.json", "{}"), ("retro.log", "log"),
                       ("my-notes.txt", "mine"), ("daily-2026-09-20.html.4242.tmp", "half")):
        with open(os.path.join(out, name), "w", encoding="utf-8") as f:
            f.write(text)


class ForgetTest(TempHome):
    def setUp(self):
        super().setUp()
        make_site(self.home, self.out)
        self.sources = {rel: read(os.path.join(self.home, rel)) for rel in SOURCES}

    def files(self):
        return sorted(os.listdir(self.out)) if os.path.isdir(self.out) else []

    def assert_sources_kept(self):
        for rel, text in self.sources.items():
            self.assertEqual(read(os.path.join(self.home, rel)), text, rel)

    def test_date_deletes_only_that_days_derived_files(self):
        before = self.files()
        self.assertIn("summary-daily-2026-09-23.json", before)  # counts saved even with --llm none
        code, out, _ = self.run_cmd(retro.cmd_forget, date="2026-09-23", all=False, yes=True)
        self.assertEqual(code, 0, out)
        gone = {"daily-2026-09-23.html", "summary-daily-2026-09-23.json", "notes-2026-09-23.json"}
        self.assertEqual(set(before) - set(self.files()), gone)
        for name in gone:
            self.assertIn(os.path.join(self.out, name), out)  # exactly what was deleted
        self.assertIn(retro.FORGET_NOTE, out)
        self.assertIn("원본 기록(Claude·Codex·git·Chrome)은 그대로입니다. 다시 retro를 실행하면 같은 날이 다시 만들어집니다. "
                      "수집을 멈추려면 retro off <소스>", out)
        self.assert_sources_kept()
        # index.html, the Tue page's → and the week page no longer point at the deleted page
        for name in self.files():
            if name.endswith(".html"):
                self.assertNotIn('href="daily-2026-09-23.html', read(os.path.join(self.out, name)), name)
        self.assertIn('href="daily-2026-09-22.html"', read(os.path.join(self.out, "index.html")))
        self.assertIn("주간 페이지에서 이 날 링크를 뺐습니다: weekly-2026-09-21.html", out)
        # made again: the week page links to it again
        with redirect_stderr(io.StringIO()):
            render.main(["--date", str(WED), "--llm", "none", "--out", os.path.join(self.out, f"daily-{WED}.html"),
                         os.path.join(self.out, "events.jsonl")])
        week = read(os.path.join(self.out, "weekly-2026-09-21.html"))
        self.assertIn('<td><a href="daily-2026-09-23.html">09/23 (수)</a></td>', week)
        self.assertIn('<a href="daily-2026-09-23.html">수 09/23</a>', week)

    def test_nothing_for_that_date(self):
        code, out, _ = self.run_cmd(retro.cmd_forget, date="2026-01-01", all=False, yes=True)
        self.assertEqual(code, 0)
        self.assertIn("지울 파일이 없습니다", out)
        self.assertEqual(self.run_cmd(retro.cmd_forget, date="2026-13-01", all=False, yes=True)[0], 2)

    def test_all_asks_first(self):
        before = self.files()
        for answer in ("n", "", "아니"):
            with mock.patch("builtins.input", return_value=answer):
                code, out, _ = self.run_cmd(retro.cmd_forget, date=None, all=True, yes=False)
            self.assertEqual(code, 1)
            self.assertIn("취소했습니다", out)
            self.assertIn(os.path.join(self.out, "events.jsonl"), out)  # the list was shown before asking
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertEqual(self.run_cmd(retro.cmd_forget, date=None, all=True, yes=False)[0], 1)
        self.assertEqual(self.files(), before)
        with mock.patch("builtins.input", return_value="y"):
            code, out, _ = self.run_cmd(retro.cmd_forget, date=None, all=True, yes=False)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.files(), ["my-notes.txt"])  # the user's own file stays
        for name in set(before) - {"my-notes.txt"}:
            self.assertIn(os.path.join(self.out, name), out)
        self.assertIn(retro.FORGET_NOTE, out)
        self.assert_sources_kept()

    def test_all_with_yes_skips_the_question(self):
        with mock.patch("builtins.input", side_effect=AssertionError("asked")):
            code, out, _ = self.run_cmd(retro.cmd_forget, date=None, all=True, yes=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.files(), ["my-notes.txt"])
        self.assert_sources_kept()

    def test_someone_elses_index_is_kept(self):
        with open(os.path.join(self.out, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html>my own site</html>")
        self.run_cmd(retro.cmd_forget, date=None, all=True, yes=True)
        self.assertEqual(self.files(), ["index.html", "my-notes.txt"])

    def test_cli(self):
        env = dict(os.environ, HOME=self.home)
        cmd = [sys.executable, os.path.join(HERE, "retro.py"), "forget"]
        res = subprocess.run(cmd + ["--all"], input="n\n", env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 1)
        self.assertIn("지울까요? [y/N]", res.stdout)
        self.assertIn("my-notes.txt", self.files())
        self.assertIn("events.jsonl", self.files())
        res = subprocess.run(cmd + ["--date", "2026-09-23", "--yes"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("daily-2026-09-23.html", self.files())
        self.assertIn("daily-2026-09-22.html", self.files())
        self.assertNotEqual(subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60).returncode, 0)


class AppForgetTest(AppTestCase):
    def setUp(self):
        super().setUp()
        with mock.patch.object(retro, "OUT_DIR", self.out):
            make_site(self.home, self.out)

    def files(self):
        return sorted(os.listdir(self.out))

    def test_section(self):
        body = self.get("/settings")[2]
        self.assertIn("기록 삭제", body)
        self.assertIn('<input type="date" name="date" required>', body)
        self.assertIn("전부 삭제…", body)

    def test_date_needs_the_confirm_step(self):
        before = self.files()
        self.assertEqual(self.post("/settings/forget", {"date": "2026-09-23"})[0], 303)
        self.assertEqual(self.files(), before)
        body = self.get("/settings")[2]
        self.assertIn(os.path.join(self.out, "daily-2026-09-23.html"), body)
        self.assertIn('<input type="hidden" name="confirm" value="1"><button>정말 지우기</button>', body)
        # the confirm POST gets the same protections
        form = {"date": "2026-09-23", "confirm": "1"}
        self.assertEqual(self.post("/settings/forget", form, origin="http://evil.example")[0], 403)
        self.assertEqual(self.post("/settings/forget", form, origin=None)[0], 403)
        self.assertEqual(self.post("/settings/forget", form, cookie=False)[0], 403)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.post("/settings/forget", form)[0], 303)
        self.assertEqual(set(before) - set(self.files()),
                         {"daily-2026-09-23.html", "summary-daily-2026-09-23.json", "notes-2026-09-23.json"})
        body = self.get("/settings")[2]
        self.assertIn("지웠습니다 (3개)", body)
        self.assertIn("원본 기록(Claude·Codex·git·Chrome)은 그대로입니다", body)

    def test_all_needs_the_confirm_step(self):
        before = self.files()
        self.post("/settings/forget", {"scope": "all"})
        self.assertEqual(self.files(), before)
        self.assertIn('<input type="hidden" name="scope" value="all"><input type="hidden" name="confirm" value="1">',
                      self.get("/settings")[2])
        self.post("/settings/forget", {"scope": "all", "confirm": "1"})
        self.assertEqual(self.files(), ["my-notes.txt"])
        for rel in SOURCES:
            self.assertTrue(os.path.exists(os.path.join(self.home, rel)), rel)

    def test_bad_or_empty_date(self):
        for date in ("", "nope", "2026-02-30", "../x"):
            self.post("/settings/forget", {"date": date, "confirm": "1"})
            self.assertIn("날짜를 골라주세요", self.get("/settings")[2])
        self.post("/settings/forget", {"date": "2026-01-01"})
        self.assertIn("지울 파일이 없습니다", self.get("/settings")[2])


# ---------------------------------------------------------------- preview
class PreviewTest(TempHome):
    def setUp(self):
        super().setUp()
        self.site = os.path.join(self.home, "site")
        self.src = os.path.join(self.site, "events.jsonl")
        write_jsonl(self.src, events())

    def preview(self, *argv, claude=True):
        out = io.StringIO()
        with mock.patch.object(render.shutil, "which", lambda name: "/bin/claude" if claude else None), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = render.main(list(argv) + ["--preview", "--cache-dir", self.site, self.src])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_daily_prints_the_exact_prompt_and_writes_nothing(self):
        text = self.preview("--date", str(WED))
        loaded = [e for e in render.load([self.src]) if e["ts"].date() == WED]
        prompt = render.build_prompt(WED, loaded, render.day_stats(loaded))
        self.assertTrue(text.rstrip("\n").endswith("---- 보낼 기록 ----\n" + prompt), text)
        self.assertIn("결제 버그 고쳐줘", text)
        self.assertIn(f"보낼 기록: {len(prompt):,}자", text)
        self.assertIn("보낼 곳: Claude Code (claude -p, 내 Claude 로그인)", text)
        self.assertIn(render.SYSTEM + "\n\n" + render.DAILY_TASK, text)  # what `claude -p` gets as its prompt
        self.assertIn("아무것도 보내지 않았고 페이지도 만들지 않았습니다", text)
        self.assertEqual(os.listdir(self.site), ["events.jsonl"])  # no page, no summary, no index

    def test_weekly(self):
        text = self.preview("--week", "--date", str(WED))
        loaded = render.load([self.src])
        by = render.split_days(loaded, render.week_days(MON))
        prompt = render.build_week_prompt(render.week_days(MON), by, render.week_stats(by))
        self.assertTrue(text.rstrip("\n").endswith(prompt))
        self.assertIn("2026-09-21 주간", text)
        self.assertEqual(os.listdir(self.site), ["events.jsonl"])

    def test_backend_and_cache_lines(self):
        self.assertIn("보낼 곳: 없음 — 요약 끔(--llm none)", self.preview("--date", str(WED), "--llm", "none"))
        self.assertIn("보낼 곳: 없음 — 요약 도구 없음", self.preview("--date", str(WED), claude=False))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
                mock.patch.object(render, "pick_backend", lambda choice: "api"):
            text = self.preview("--date", str(WED))
        self.assertIn("보낼 곳: Anthropic API", text)
        self.assertNotIn(render.DAILY_TASK, text)  # the API gets the system prompt alone
        self.assertNotIn("저장된 요약이", text)
        loaded = [e for e in render.load([self.src]) if e["ts"].date() == WED]
        prompt = render.build_prompt(WED, loaded, render.day_stats(loaded))
        render.write_cache(render.cache_path(self.site, "daily", WED),
                           {"prompt_sha256": render.prompt_hash(prompt, render.SYSTEM, render.SUMMARY_SCHEMA),
                            "summary": {"one_line": "x"}})
        self.assertIn("저장된 요약이 바로 이 내용으로 만든 것입니다", self.preview("--date", str(WED)))
        self.assertNotIn("저장된 요약이", self.preview("--date", str(WED), "--refresh"))

    def args(self, **kw):
        return dict(dict(date=str(WED), llm="auto", no_chrome=True, no_open=True, refresh=False, preview=True), **kw)

    def test_retro_preview_collects_but_sends_and_writes_nothing(self):
        os.makedirs(os.path.dirname(self.config))
        retro.save_config({"hosts": [], "aliases": {"ai-market-2": "AI 마켓"}})
        with self.fake_collect(events()), mock.patch.object(render.shutil, "which", lambda name: "/bin/claude"), \
                mock.patch.object(retro.webbrowser, "open", side_effect=AssertionError("opened")):
            code, out, err = self.run_cmd(retro.cmd_run, **self.args())
        self.assertEqual(code, 0, err)
        self.assertIn("---- 보낼 기록 ----", out)
        self.assertIn("[AI 마켓] 결제 버그 고쳐줘", out)  # aliases apply to what is sent, too
        self.assertNotIn("[ai-market-2] 결제", out)
        self.assertIn("보낼 곳: Claude Code", out)
        self.assertFalse(os.path.exists(self.out))  # ~/Retro untouched: no page, no events.jsonl, no summary

    def test_retro_week_preview(self):
        with self.fake_collect(events()), mock.patch.object(retro.webbrowser, "open", side_effect=AssertionError):
            code, out, err = self.run_cmd(retro.cmd_week, **self.args(llm="none"))
        self.assertEqual(code, 0, err)
        self.assertIn("요약 미리보기 — 2026-09-21 주간", out)
        self.assertIn("보낼 곳: 없음", out)
        self.assertFalse(os.path.exists(self.out))

    def test_page_options_take_preview(self):
        with mock.patch.object(sys, "argv", ["retro", "week", "--preview"]), \
                mock.patch.object(retro, "cmd_week", lambda a: sys.exit(0 if a.preview else 3)):
            with self.assertRaises(SystemExit) as done:
                retro.main()
        self.assertEqual(done.exception.code, 0)


class AppPreviewTest(AppTestCase):
    def setUp(self):
        super().setUp()
        for p in no_llm():
            p.start()
            self.addCleanup(p.stop)
        write_jsonl(os.path.join(self.out, "events.jsonl"), retro.apply_aliases(events(), {"ai-market-2": "AI 마켓"}))

    def test_link_per_page(self):
        self.touch("daily-2026-09-23.html")
        self.touch("weekly-2026-09-21.html")
        body = self.get("/")[2]
        self.assertIn('href="/preview?kind=daily&amp;date=2026-09-23">요약에 보내는 내용 보기</a>', body)
        self.assertIn('href="/preview?kind=weekly&amp;date=2026-09-21">요약에 보내는 내용 보기</a>', body)

    def test_shows_the_prompt(self):
        before = sorted(os.listdir(self.out))
        status, _, body = self.get("/preview?kind=daily&date=2026-09-23")
        self.assertEqual(status, 200)
        pre = re.search(r'<pre class="full">(.*?)</pre>', body, re.S).group(1)
        self.assertIn("---- 보낼 기록 ----", pre)
        self.assertIn("[AI 마켓] 결제 버그 고쳐줘", pre)
        self.assertIn("retro --preview --date 2026-09-23", body)
        self.assertIn("2026-09-21 주간", self.get("/preview?kind=weekly&date=2026-09-21")[2])
        self.assertEqual(sorted(os.listdir(self.out)), before)  # nothing written

    def test_bad_request_and_missing_logs(self):
        self.assertIn("날짜가 올바르지 않습니다", self.get("/preview?kind=daily&date=nope")[2])
        self.assertIn("날짜가 올바르지 않습니다", self.get("/preview?kind=x&date=2026-09-23")[2])
        self.assertIn("마지막으로 모은 기록에 이 날이 없습니다", self.get("/preview?kind=daily&date=2026-01-01")[2])
        self.assertEqual(self.get("/preview?kind=daily&date=2026-09-23", cookie=False)[0], 403)
        os.remove(os.path.join(self.out, "events.jsonl"))
        self.assertIn("아직 모은 기록이 없습니다", self.get("/preview?kind=daily&date=2026-09-23")[2])


if __name__ == "__main__":
    unittest.main()
