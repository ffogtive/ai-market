"""회고 쓰기: my notes (notes.py), saving them in `retro app`, the in-place page rewrite, the next day's check.

No network: the server listens on an ephemeral 127.0.0.1 port in a thread (test_app.AppTestCase), HOME is a
temp folder, the claude CLI is faked while the pages are first made, and after that any LLM call fails the test.

Run: python3 -m unittest discover retro/tests
"""
import datetime as dt
import io
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from unittest import mock

from test_app import AppTestCase
from test_template_v2 import FULL_DAILY, FULL_WEEKLY, MON, ev

import notes  # noqa: E402  (test_app put retro/ on sys.path)
import render  # noqa: E402

TUE, WED, THU = (MON + dt.timedelta(days=i) for i in (1, 2, 3))
MARKER = re.compile(r"<!--notes:(\w+)( ai)?-->.*?<!--/notes:\1-->", re.S)


def events():
    return [ev(MON, (9, 5), "claude", "fix checkout"), ev(MON, (12, 30), "git", "fix: checkout (+10/-2)"),
            ev(TUE, (10, 0), "claude", "pricing page", "blog"), ev(WED, (11, 0), "codex", "copy edits", "blog")]


def write_events(path, evs):
    with open(path, "w", encoding="utf-8") as f:
        for e in evs:
            f.write(json.dumps(dict(e, ts=e["ts"].isoformat()), ensure_ascii=False) + "\n")


def make_pages(out, src, runs, prompts=None):
    """render.main for each argv in runs, into out, with a fake claude CLI (prompts collects what it was sent)."""
    def fake_cli(prompt, system, schema, task, drop_api_key=False):
        if prompts is not None:
            prompts.append(prompt)
        return FULL_WEEKLY if schema is render.WEEK_SCHEMA else FULL_DAILY

    with mock.patch.object(render, "summarize", mock.Mock(side_effect=AssertionError("API used"))), \
            mock.patch.object(render, "summarize_cli", fake_cli), \
            mock.patch.object(render.shutil, "which", lambda name: "/bin/claude"), \
            mock.patch.dict(os.environ, {}), redirect_stderr(io.StringIO()) as err:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        for argv in runs:
            kind = "weekly" if "--week" in argv else "daily"
            day = dt.date.fromisoformat(argv[argv.index("--date") + 1])
            name = render.page_file(kind, render.week_days(day)[0] if kind == "weekly" else day)
            assert render.main(argv + ["--out", os.path.join(out, name), "--cache-dir", out, src]) == 0, err.getvalue()


def daily(day, *extra):
    return ["--date", str(day)] + list(extra)


def weekly(day, *extra):
    return ["--week", "--date", str(day)] + list(extra)


# ---------------------------------------------------------------- the store
class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_save_cleans_text_and_keeps_checks(self):
        notes.save(self.dir, "daily", MON, "keep\r\nline 2", "", "try\x00 it", "의미", "첫 일 " + "x" * 600)
        notes.set_status(self.dir, MON, "first_task", "완료")
        n = notes.save(self.dir, "daily", MON, "keep again", "", "", "", "첫 일")  # a later edit keeps the check
        self.assertEqual(n["followup"], {"first_task": "완료"})
        with open(notes.path(self.dir, "daily", MON), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["kpt"], {"keep": "keep again", "problem": "", "try": ""})
        self.assertIn("updated", saved)
        first = notes.save(self.dir, "daily", TUE, "keep\r\nline 2", "", "try\x00 it", "의미", "첫 일 " + "x" * 600)
        self.assertEqual(first["kpt"]["keep"], "keep\nline 2")
        self.assertEqual(first["kpt"]["try"], "try it")  # control characters dropped
        self.assertEqual(len(first["first_task"]), 500)
        self.assertEqual(os.path.basename(notes.path(self.dir, "weekly", MON)), "notes-weekly-2026-09-21.json")
        self.assertEqual(os.path.basename(notes.path(self.dir, "daily", MON)), "notes-2026-09-21.json")
        with self.assertRaises(ValueError):
            notes.set_status(self.dir, MON, "first_task", "done")

    def test_atomic_write(self):
        notes.save(self.dir, "daily", MON, "old")
        with mock.patch.object(notes.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                notes.save(self.dir, "daily", MON, "new")
        self.assertEqual(notes.kpt(notes.load(self.dir, "daily", MON))["keep"], "old")  # the old file is whole
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])  # and no temp file is left

        def write(i):
            notes.save(self.dir, "daily", MON, f"keep {i}" * 200)
        threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(notes.path(self.dir, "daily", MON), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["kpt"]["keep"].startswith("keep "))  # one whole write, never a mix
        self.assertEqual(sorted(os.listdir(self.dir)), ["notes-2026-09-21.json"])

    def test_unreadable_file_is_no_notes(self):
        with open(notes.path(self.dir, "daily", MON), "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertEqual(notes.load(self.dir, "daily", MON), {})
        self.assertEqual(notes.load(None, "daily", MON), {})

    def test_previous_looks_back_to_the_last_first_task_or_try(self):
        self.assertIsNone(notes.previous(self.dir, THU))
        notes.save(self.dir, "daily", MON, first_task="A")
        notes.save(self.dir, "daily", WED, reflection="only a reflection")  # no first task or Try: skipped
        self.assertEqual(notes.previous(self.dir, THU)[0], MON)
        self.assertIsNone(notes.previous(self.dir, MON + dt.timedelta(days=notes.LOOKBACK + 1)))
        self.assertEqual(render.followup_labels(TUE, MON)["first_task"], "어제 정한 첫 할 일")
        self.assertEqual(render.followup_labels(THU, MON), {"first_task": "09/21 (월)에 정한 첫 할 일",
                                                            "try": "09/21 (월)의 Try"})
        notes.set_status(self.dir, MON, "first_task", "이어가기")
        self.assertEqual(notes.carried(self.dir, TUE), {"first_task": "A"})


# ---------------------------------------------------------------- the pages alone (no app)
class RenderTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.src = os.path.join(self.dir, "events.jsonl")
        write_events(self.src, events())

    def read(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return f.read()

    def test_no_notes_page_is_unchanged(self):
        evs = [e for e in events() if e["ts"].date() == MON]
        stats = render.day_stats(evs)
        legacy = render.render_page(MON, stats, FULL_DAILY, evs)  # callers without notes_dir
        empty = render.render_page(MON, stats, FULL_DAILY, evs, notes_dir=self.dir)
        self.assertEqual(legacy, empty)
        plain = re.sub(r"<!--/?notes:\w+( ai)?-->", "", empty)  # the markers are the only addition
        self.assertEqual(plain.count("(직접 작성)"), 3)
        self.assertIn("가격 페이지 첫 문단 쓰기", plain)  # the AI's first task, as before
        for new in ("지난 회고 확인", notes.UNCHECKED, render.NOTES_EDIT, "내가 정함", "가장 의미 있었던 일", "<form"):
            self.assertNotIn(new, plain)
        days = render.week_days(MON)
        by = render.split_days(events(), days)
        week = render.render_week(days, render.week_stats(by), FULL_WEEKLY, events(), today=days[-1], notes_dir=self.dir)
        self.assertEqual(week, render.render_week(days, render.week_stats(by), FULL_WEEKLY, events(), today=days[-1]))
        self.assertNotIn("이번 주 첫 할 일", week)
        self.assertEqual(re.sub(r"<!--/?notes:\w+( ai)?-->", "", week).count("(직접 작성)"), 3)

    def test_saved_notes_show_on_static_pages(self):
        notes.save(self.dir, "daily", MON, "작게 커밋 유지", "", "테스트 먼저 <b>", "결제 버그를\n끝냄", "가격 첫 문단")
        notes.set_status(self.dir, MON, "first_task", "완료")
        make_pages(self.dir, self.src, [daily(MON), daily(TUE)])
        mon, tue = self.read("daily-2026-09-21.html"), self.read("daily-2026-09-22.html")
        self.assertIn("<td>작게 커밋 유지</td>", mon)
        self.assertIn("<td>테스트 먼저 &lt;b&gt;</td>", mon)  # escaped
        self.assertEqual(mon.count("(직접 작성)"), 1)  # Problem is still empty
        self.assertIn("결제 버그를<br>끝냄", mon)
        self.assertIn('<b>내일 첫 할 일</b> <span class="sub">내가 정함</span><br>가격 첫 문단', mon)
        self.assertIn(render.NOTES_EDIT, mon)
        self.assertNotIn("<form", mon)  # file:// pages show, retro app edits
        self.assertIn('어제 정한 첫 할 일: 가격 첫 문단 <span class="chip">완료</span>', tue)
        self.assertIn(f'어제의 Try: 테스트 먼저 &lt;b&gt; <span class="chip">{notes.UNCHECKED}</span>', tue)

    def test_refresh_rewrites_only_the_notes_parts(self):
        make_pages(self.dir, self.src, [daily(MON), daily(TUE), weekly(MON)])
        tue = self.read("daily-2026-09-22.html")
        before = {n: self.read(n) for n in ("daily-2026-09-21.html", "weekly-2026-09-21.html")}
        notes.save(self.dir, "daily", MON, "유지", first_task="첫 일")
        with mock.patch.object(render, "get_summary", side_effect=AssertionError("LLM called")) as llm:
            done = render.refresh_notes(self.dir, self.dir, [MON])
        llm.assert_not_called()
        self.assertEqual(done, {"daily-2026-09-21.html": True, "weekly-2026-09-21.html": True})
        for name, old in before.items():
            new = self.read(name)
            self.assertNotEqual(new, old, name)
            self.assertEqual(MARKER.sub("", new), MARKER.sub("", old), name)  # nothing outside the notes parts moved
        self.assertIn("<td>유지</td>", self.read("daily-2026-09-21.html"))
        self.assertIn("<td>작게 커밋</td>", self.read("daily-2026-09-21.html"))  # the AI draft column stays
        self.assertEqual(self.read("daily-2026-09-22.html"), tue)  # not in the refreshed days
        self.assertIn("<td>월 09/21</td><td>첫 일</td>", self.read("weekly-2026-09-21.html"))

    def test_numbers_only_page_stays_without_ai_text(self):
        make_pages(self.dir, self.src, [daily(MON)])
        make_pages(self.dir, self.src, [daily(MON, "--llm", "none")])  # a summary is saved, but this page has none
        notes.save(self.dir, "daily", MON, "유지")
        render.refresh_notes(self.dir, self.dir, [MON])
        page = self.read("daily-2026-09-21.html")
        self.assertIn("<td>유지</td>", page)
        self.assertNotIn("작게 커밋", page)
        self.assertNotIn("가격 페이지 첫 문단 쓰기", page)

    def test_llm_cached_never_summarizes(self):
        with mock.patch.object(render, "get_summary", side_effect=AssertionError("LLM called")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(render.main(daily(MON, "--llm", "cached") + ["--out", os.path.join(self.dir, "daily-2026-09-21.html"),
                                                                          self.src]), 0)
        page = self.read("daily-2026-09-21.html")
        self.assertIn("숫자만 표시 — 저장된 요약 없음", page)
        make_pages(self.dir, self.src, [daily(MON)])
        write_events(self.src, events() + [ev(MON, (20, 0), "claude", "late work")])
        with mock.patch.object(render, "get_summary", side_effect=AssertionError("LLM called")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(render.main(daily(MON, "--llm", "cached") + ["--out", os.path.join(self.dir, "daily-2026-09-21.html"),
                                                                          "--cache-dir", self.dir, self.src]), 0)
        page = self.read("daily-2026-09-21.html")
        self.assertIn("결제와 가격 작업을 했다", page)  # the saved summary…
        self.assertIn("현재 숫자와 다른 시점의 요약입니다", page)  # …said to be older than the numbers

    def test_notes_never_reach_the_llm(self):
        secret = "비밀스러운-내-회고-문장"
        for day in (MON, TUE):
            notes.save(self.dir, "daily", day, secret, secret, secret, secret, secret)
        notes.save(self.dir, "weekly", MON, secret, secret, secret, secret)
        prompts = []
        make_pages(self.dir, self.src, [daily(MON, "--refresh"), daily(TUE, "--refresh"), weekly(MON, "--refresh")], prompts)
        self.assertEqual(len(prompts), 3)
        for p in prompts:
            self.assertNotIn(secret, p)
        self.assertIn(secret, self.read("weekly-2026-09-21.html"))  # shown on the page, never sent


# ---------------------------------------------------------------- retro app
class NotesAppTest(AppTestCase):
    """Pages for Mon–Wed and the week are made first (fake LLM); after that every LLM call fails the test."""

    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.out, "events.jsonl")
        write_events(self.src, events())
        make_pages(self.out, self.src, [daily(MON), daily(TUE), daily(WED), weekly(MON)])
        p = mock.patch.object(render, "get_summary", side_effect=AssertionError("LLM called"))
        self.llm = p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.llm.assert_not_called()
        super().tearDown()

    def page(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as f:
            return f.read()

    def saved(self, day, kind="daily"):
        with open(notes.path(self.out, kind, day), encoding="utf-8") as f:
            return json.load(f)

    def write(self, day, **form):
        return self.post("/notes", dict({"date": str(day)}, **form))

    def check(self, day, prev_day, item, status):
        return self.post("/notes/followup", {"date": str(day), "from": str(prev_day), "item": item, "status": status})

    def test_save_rewrites_the_page_without_llm(self):
        before = self.page("daily-2026-09-21.html")
        status, headers, _ = self.write(MON, keep="작게 커밋 유지", problem="", reflection="결제 버그를 끝냄",
                                        first_task="가격 페이지 첫 문단")
        self.assertEqual((status, headers["location"]), (303, "/notes?date=2026-09-21"))
        n = self.saved(MON)
        self.assertEqual(n["kpt"], {"keep": "작게 커밋 유지", "problem": "", "try": ""})
        self.assertEqual((n["reflection"], n["first_task"]), ("결제 버그를 끝냄", "가격 페이지 첫 문단"))
        page = self.page("daily-2026-09-21.html")
        self.assertIn("<td>작게 커밋 유지</td>", page)
        self.assertEqual(page.count("(직접 작성)"), 2)
        self.assertIn("결제 버그를 끝냄", page)
        self.assertIn('내가 정함</span><br>가격 페이지 첫 문단', page)
        self.assertEqual(MARKER.sub("", page), MARKER.sub("", before))  # numbers and summary untouched
        screen = self.get("/notes?date=2026-09-21")[2]
        self.assertIn("저장했습니다. 페이지에 반영했습니다", screen)
        self.assertIn("작게 커밋 유지</textarea>", screen)
        # the next day's page and the week's page were rewritten too
        self.assertIn("어제 정한 첫 할 일: 가격 페이지 첫 문단", self.page("daily-2026-09-22.html"))
        self.assertIn("<td>월 09/21</td><td>가격 페이지 첫 문단</td>", self.page("weekly-2026-09-21.html"))
        home = self.get("/")[2]
        self.assertIn('<a href="/notes?date=2026-09-21">회고 쓰기</a> <span class="muted">(작성함)</span>', home)
        self.assertIn('<a href="/notes?date=2026-09-22">회고 쓰기</a></li>', home)
        self.assertIn('<a href="/notes?week=2026-09-21">회고 쓰기</a>', home)

    def test_needs_token_and_our_origin(self):
        form = {"date": str(MON), "keep": "x"}
        self.assertEqual(self.post("/notes", form, origin="http://evil.example")[0], 403)
        self.assertEqual(self.post("/notes", form, origin=None, referer="http://evil.example/notes")[0], 403)
        self.assertEqual(self.post("/notes", form, origin=None)[0], 403)
        self.assertEqual(self.post("/notes", form, cookie=False)[0], 403)
        self.assertEqual(self.get("/notes?date=2026-09-21", cookie=False)[0], 403)
        self.assertEqual(self.post("/notes", form, host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.post("/notes/followup", {"date": str(TUE), "from": str(MON), "item": "try",
                                                      "status": "완료"}, origin="http://evil.example")[0], 403)
        self.assertFalse(os.path.exists(notes.path(self.out, "daily", MON)))
        self.assertNotIn("<td>x</td>", self.page("daily-2026-09-21.html"))
        _, headers, _ = self.get("/notes")
        self.assertIn("form-action 'self'", headers["content-security-policy"])

    def test_screen_prefills_the_ai_drafts(self):
        body = self.get("/notes?date=2026-09-21")[2]
        self.assertIn("2026-09-21 (월) 회고 쓰기", body)
        self.assertIn('placeholder="AI 초안: 작게 커밋"', body)
        self.assertIn('placeholder="AI 초안: 테스트 병렬화"', body)
        self.assertIn('name="first_task" value="가격 페이지 첫 문단 쓰기"', body)  # default = the AI's first task
        self.assertIn('<a href="/file/daily-2026-09-21.html"', body)
        self.assertIn("AI 요약에는 보내지 않습니다", body)
        self.assertNotIn("지난 회고 확인", body)  # nothing to check yet
        self.write(MON, keep='<script>alert(1)</script>', first_task="")
        body = self.get("/notes?date=2026-09-21")[2]
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;</textarea>", body)
        self.assertNotIn("<script>alert", body)
        self.assertIn('name="first_task" value=""', body)  # cleared on purpose: the draft doesn't come back
        self.assertIn("&lt;script&gt;", self.page("daily-2026-09-21.html"))
        self.assertEqual(self.get("/notes")[0], 200)  # today
        for bad in ("2026-13-01", "2026-9-1", "../x", "2026-09-21&week=x"):
            self.assertEqual(self.get("/notes?date=" + bad)[0], 404, bad)

    def test_next_day_checks_yesterday(self):
        self.write(MON, **{"try": "테스트 먼저", "first_task": "가격 첫 문단"})
        tue = self.page("daily-2026-09-22.html")
        self.assertIn(f'어제 정한 첫 할 일: 가격 첫 문단 <span class="chip">{notes.UNCHECKED}</span>', tue)
        self.assertIn(f'어제의 Try: 테스트 먼저 <span class="chip">{notes.UNCHECKED}</span>', tue)
        screen = self.get("/notes?date=2026-09-22")[2]
        self.assertIn("↩️ 지난 회고 확인", screen)
        self.assertIn("<b>어제 정한 첫 할 일</b><div>가격 첫 문단</div>", screen)
        for s in notes.STATUSES:
            self.assertIn(f'<button aria-pressed="false">{s}</button>', screen)

        status, headers, _ = self.check(TUE, MON, "first_task", "완료")
        self.assertEqual((status, headers["location"]), (303, "/notes?date=2026-09-22"))
        self.assertEqual(self.saved(MON)["followup"], {"first_task": "완료"})
        self.assertIn('어제 정한 첫 할 일: 가격 첫 문단 <span class="chip">완료</span>', self.page("daily-2026-09-22.html"))
        screen = self.get("/notes?date=2026-09-22")[2]
        self.assertIn("어제 정한 첫 할 일: 완료", screen)  # the flash
        self.assertIn('<button aria-pressed="true">완료</button>', screen)
        # two days later (no notes on Tue): Mon is still the last one to look back to, with its date
        self.assertIn("09/21 (월)에 정한 첫 할 일: 가격 첫 문단", self.page("daily-2026-09-23.html"))

    def test_continue_carries_into_todays_defaults(self):
        self.write(MON, **{"try": "테스트 먼저", "first_task": "가격 첫 문단"})
        self.check(TUE, MON, "first_task", "이어가기")
        self.check(TUE, MON, "try", "이어가기")
        screen = self.get("/notes?date=2026-09-22")[2]
        self.assertIn('name="first_task" value="가격 첫 문단"', screen)  # not the AI draft
        self.assertIn("이어가기로 가져옴 · AI 초안: 가격 페이지 첫 문단 쓰기", screen)
        self.assertIn(">테스트 먼저</textarea>", screen)
        self.write(TUE, first_task="문구 다듬기")  # what I save wins over the carried item
        self.assertIn('name="first_task" value="문구 다듬기"', self.get("/notes?date=2026-09-22")[2])
        self.assertIn("어제 정한 첫 할 일: 문구 다듬기", self.page("daily-2026-09-23.html"))

    def test_check_input_is_validated(self):
        self.write(MON, first_task="가격 첫 문단")
        for form, why in (({"item": "first_task", "status": "done"}, "알 수 없는 요청"),
                          ({"item": "keep", "status": "완료"}, "알 수 없는 요청"),
                          ({"item": "try", "status": "완료"}, "확인할 항목이 바뀌었습니다"),  # Mon has no Try
                          ({"item": "first_task", "status": "완료", "from": "2026-09-20"},  # not what Tue looks back to
                           "확인할 항목이 바뀌었습니다")):
            self.post("/notes/followup", dict({"date": str(TUE), "from": str(MON)}, **form))
            self.assertIn(why, self.get("/notes?date=2026-09-22")[2], form)
        self.post("/notes/followup", {"date": "nope", "from": str(MON), "item": "first_task", "status": "완료"})
        self.assertIn("알 수 없는 요청", self.get("/notes")[2])
        self.assertNotIn("followup", self.saved(MON))
        self.assertEqual(self.write("2026-02-30", keep="x")[1]["location"], "/notes")
        self.assertIn("날짜가 올바르지 않습니다", self.get("/notes")[2])

    def test_weekly_counts(self):
        self.write(MON, first_task="A")
        self.write(TUE, first_task="B")
        self.write(WED, first_task="C")
        self.check(TUE, MON, "first_task", "완료")
        self.check(WED, TUE, "first_task", "이어가기")
        week = self.page("weekly-2026-09-21.html")
        self.assertIn("내가 정한 첫 할 일 3개 · 완료 1 · 이어가기 1 · 취소 0 · 아직 확인 안 함 1", week)
        self.assertIn("<td>월 09/21</td><td>A</td><td>완료</td>", week)
        self.assertIn("<td>화 09/22</td><td>B</td><td>이어가기</td>", week)
        self.assertIn("<td>수 09/23</td><td>C</td><td>아직 확인 안 함</td>", week)
        self.check(THU, WED, "first_task", "취소")  # Thursday has no page yet; the week's page still updates
        self.assertIn("완료 1 · 이어가기 1 · 취소 1 · 아직 확인 안 함 0", self.page("weekly-2026-09-21.html"))
        self.assertNotIn("%", MARKER.search(self.page("weekly-2026-09-21.html")).group(0))

    def test_weekly_notes(self):
        status, headers, _ = self.post("/notes", {"week": "2026-09-23", "keep": "주간 유지", "reflection": "출시"})
        self.assertEqual(headers["location"], "/notes?week=2026-09-21")  # any day → its Monday
        self.assertEqual(self.saved(MON, "weekly")["kpt"]["keep"], "주간 유지")
        self.assertNotIn("first_task", self.saved(MON, "weekly"))
        week = self.page("weekly-2026-09-21.html")
        self.assertIn("<td>주간 유지</td>", week)
        self.assertIn("이번 주 가장 의미 있었던 일", week)
        body = self.get("/notes?week=2026-09-21")[2]
        self.assertIn("주간 회고 쓰기", body)
        self.assertIn('placeholder="AI 초안: 병렬화"', body)
        self.assertNotIn('name="first_task"', body)

    def test_old_page_is_rendered_again_from_the_saved_summary(self):
        path = os.path.join(self.out, "daily-2026-09-21.html")
        with open(path, encoding="utf-8") as f:
            old = re.sub(r"<!--/?notes:\w+( ai)?-->", "", f.read())  # a page made before notes existed
        with open(path, "w", encoding="utf-8") as f:
            f.write(old)
        self.write(MON, keep="유지")
        page = self.page("daily-2026-09-21.html")
        self.assertIn("<td>유지</td>", page)
        self.assertIn("결제와 가격 작업을 했다", page)  # the saved summary, no LLM call (tearDown checks)
        self.assertIn("<!--notes:kpt ai-->", page)
        self.assertIn("저장된 요약으로 다시 만들었습니다", self.get("/notes?date=2026-09-21")[2])

    def test_old_page_without_logs_waits(self):
        path = os.path.join(self.out, "daily-2026-09-21.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write("<html>old page</html>")
        os.remove(self.src)
        self.write(MON, keep="유지")
        self.assertEqual(self.page("daily-2026-09-21.html"), "<html>old page</html>")
        self.assertIn("예전 형식이라 다음에", self.get("/notes?date=2026-09-21")[2])
        self.assertEqual(self.saved(MON)["kpt"]["keep"], "유지")

    def test_no_page_yet(self):
        self.write(THU, reflection="아직 페이지 없음")
        body = self.get("/notes?date=2026-09-24")[2]
        self.assertIn("회고 페이지를 만들면 함께 보입니다", body)
        self.assertIn("아직 페이지 없음 — 회고를 만들면", body)
        self.assertFalse(os.path.exists(os.path.join(self.out, "daily-2026-09-24.html")))


if __name__ == "__main__":
    unittest.main()
