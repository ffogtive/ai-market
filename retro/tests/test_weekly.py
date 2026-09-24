"""Weekly retrospective: numbers, week boundaries, prompt size, page, backends, `retro week`.

No network: the LLM backends are faked, the end-to-end run uses --llm none.

Run: python3 -m unittest discover retro/tests
"""
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import render  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=9))
MON = dt.date(2026, 9, 21)  # a Monday
DAYS = [MON + dt.timedelta(days=i) for i in range(7)]


def ev(day, hm, source, text, project="", actor="human"):
    h, m = hm
    ts = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)
    return {"source": source, "ts": ts, "project": project, "text": text, "actor": actor, "host": "mac"}


def week_fixture():
    """Mon + Wed active; the Sunday before and the Monday after must not count."""
    wed = MON + dt.timedelta(days=2)
    return [
        ev(MON - dt.timedelta(days=1), (10, 0), "claude", "last week", "shop"),
        ev(MON, (9, 5), "claude", "fix checkout", "shop"),
        ev(MON, (9, 30), "claude", "add tests", "shop"),
        ev(MON, (11, 0), "codex", "write post", "blog"),
        ev(MON, (11, 5), "codex", "worker task", "blog", actor="agent"),
        ev(MON, (12, 0), "git", "fix: checkout (+10/-2)", "shop"),
        ev(MON, (18, 40), "chrome", "Docs", "github.com"),
        ev(wed, (14, 0), "claude.ai", "plan the launch"),
        ev(wed, (15, 0), "git", "feat: pricing (+5/-1)", "shop"),
        ev(wed, (16, 0), "git", "docs: readme (+3/-0)", "blog"),
        ev(MON + dt.timedelta(days=7), (9, 0), "claude", "next week", "shop"),
    ]


class WeekBoundsTest(unittest.TestCase):
    def test_monday_to_sunday(self):
        for day in DAYS:  # any day of the week, including Mon and Sun themselves
            self.assertEqual(render.week_days(day), DAYS)
        self.assertEqual(render.week_days(MON + dt.timedelta(days=3))[0].weekday(), 0)
        self.assertEqual(render.week_days(MON - dt.timedelta(days=1))[-1], MON - dt.timedelta(days=1))

    def test_split_drops_other_weeks(self):
        by = render.split_days(week_fixture(), DAYS)
        self.assertEqual(list(by), DAYS)
        texts = [e["text"] for evs in by.values() for e in evs]
        self.assertNotIn("last week", texts)
        self.assertNotIn("next week", texts)
        self.assertEqual(len(texts), 9)


class WeekStatsTest(unittest.TestCase):
    def setUp(self):
        self.stats = render.week_stats(render.split_days(week_fixture(), DAYS))

    def test_totals(self):
        s = self.stats
        self.assertEqual(s["prompts"], 4)
        self.assertEqual(dict(s["prompts_by_tool"]), {"claude": 2, "codex": 1, "claude.ai": 1})
        self.assertEqual(s["agent"], 1)
        self.assertEqual((s["commits"], s["add"], s["dele"]), (3, 18, 3))
        self.assertEqual(s["web"], 1)
        self.assertEqual(s["active_days"], 2)
        self.assertEqual(s["projects"][0], ("shop", 4))  # 2 prompts + 2 commits

    def test_per_day(self):
        mon, tue, wed = (self.stats["per_day"][d] for d in DAYS[:3])
        self.assertEqual((mon["prompts"], mon["commits"], mon["agent"]), (3, 1, 1))
        self.assertEqual((tue["prompts"], tue["commits"], tue["first"]), (0, 0, None))
        self.assertEqual((wed["prompts"], wed["commits"]), (1, 2))
        self.assertEqual(f"{mon['first']:%H:%M}–{mon['last']:%H:%M}", "09:05–18:40")

    def test_prompt_facts(self):
        by = render.split_days(week_fixture(), DAYS)
        p = render.build_week_prompt(DAYS, by, self.stats)
        self.assertIn("직접 입력한 AI 지시 4건", p)
        self.assertIn("09/21 (월): AI 지시 3건", p)
        self.assertIn("09/22 (화): 기록 없음", p)
        self.assertNotIn("worker task", p)  # agent-to-agent text is not the user's
        self.assertNotIn("last week", p)


class CompactTest(unittest.TestCase):
    def busy_week(self):
        out = []
        for d in DAYS:
            for i in range(500):
                out.append(ev(d, (8 + i // 60 % 14, i % 60), "claude", f"prompt {i} " + "x" * 600, "shop"))
            for i in range(30):
                out.append(ev(d, (9 + i % 12, 30), "git", f"commit {i} (+1/-1)", "shop"))
        out.sort(key=lambda e: e["ts"])
        return out

    def test_caps_size(self):
        events = self.busy_week()
        by = render.split_days(events, DAYS)
        for d in DAYS:
            lines, total = render.compact_day(by[d])
            self.assertEqual(total, 530)
            self.assertEqual(len(lines), render.WEEK_DAY_LINES)
            self.assertTrue(all(len(l) <= render.WEEK_LINE_WIDTH for l in lines))
            self.assertEqual(sum(" git " in l for l in lines), render.WEEK_DAY_LINES // 3)
            self.assertEqual(lines, sorted(lines))
        p = render.build_week_prompt(DAYS, by, render.week_stats(by))
        raw = sum(len(e["text"]) for e in events)
        self.assertLess(len(p), 7 * render.WEEK_DAY_LINES * (render.WEEK_LINE_WIDTH + 1) + 2000)
        self.assertLess(len(p), raw / 30)
        self.assertIn("530줄 중 60줄", p)

    def test_even_sampling_keeps_both_ends(self):
        day = [ev(MON, (8 + i // 60, i % 60), "codex", f"step {i}") for i in range(300)]
        lines, _ = render.compact_day(day)
        self.assertIn("step 0", lines[0])
        self.assertIn("step 299", lines[-1])
        self.assertEqual(render.sample_evenly(list(range(10)), 4), [0, 3, 6, 9])
        self.assertEqual(render.sample_evenly([1, 2], 5), [1, 2])

    def test_drops_consecutive_near_duplicates(self):
        day = [ev(MON, (9, i), "claude", t) for i, t in
               enumerate(["continue", "Continue.", "continue!", "fix the bug", "continue"])]
        lines, total = render.compact_day(day)
        self.assertEqual([l.split(" ", 2)[2] for l in lines], ["continue", "fix the bug", "continue"])
        self.assertEqual(total, 3)

    def test_small_day_keeps_everything(self):
        day = [ev(MON, (9, i), "claude", f"p{i}") for i in range(20)] + [ev(MON, (10, i), "git", f"c{i}") for i in range(30)]
        lines, total = render.compact_day(day)
        self.assertEqual((len(lines), total), (50, 50))

    def test_multiline_text_is_one_line(self):
        lines, _ = render.compact_day([ev(MON, (9, 0), "claude", "first\n\nsecond\tthird")])
        self.assertEqual(lines, ["09:00 claude first second third"])


SUMMARY = {
    "one_line": "결제 흐름을 고치고 가격 페이지를 냈다 <script>",
    "keywords": ["결제", "가격", "출시"],
    "highlights": [{"days": "월", "project": "shop", "result": "결제 버그 수정", "status": "완료",
                    "evidence": [{"time": "월 12:00", "source": "git"}]},
                   {"days": "수", "project": "blog", "result": "README 정리", "status": "요청함", "evidence": []}],
    "decisions": ["가격은 월 구독으로"],
    "blockers": ["테스트가 자주 깨짐"],
    "next_week": ["출시 공지"],
    "til": [{"text": "월 구독이 해지율이 낮다", "evidence": [{"time": "수 14:00", "source": "claude.ai"}]}],
    "open_questions": [{"text": "가격은 언제부터 적용?", "evidence": [{"time": "월 09:30", "source": "claude"},
                                                          {"time": "수 14:00", "source": "claude.ai"}]}],
    "kpt": {"keep": "커밋을 작게", "problem": "테스트가 느림", "try": "테스트 병렬화"},
    "prompt_coaching": [{"kind": "고칠 점", "prompt": "add tests", "better": "결제 실패 경로에 테스트 3개 추가해줘",
                         "why": "범위가 드러남", "evidence": [{"time": "월 09:30", "source": "claude"}]}],
    "automation_ideas": [{"request": "테스트 돌려줘", "kind": "스크립트", "idea": "커밋 전에 테스트를 자동 실행"}],
    "activity_mix": [{"type": "개발", "percent": 70}, {"type": "기획", "percent": 30}],
    "project_labels": [{"raw": "shop", "label": "쇼핑몰"}],
}


class RenderWeekTest(unittest.TestCase):
    def render(self, summary):
        by = render.split_days(week_fixture(), DAYS)
        events = [e for evs in by.values() for e in evs]
        return render.render_week(DAYS, render.week_stats(by), summary, events, today=DAYS[3])

    def test_numbers_only(self):
        page = self.render(None)
        for part in ("2026년 9월 21일 – 27일 주간 회고", "이번 주 숫자", "요일×시간대", "어디에 썼나", "날짜별",
                     "09/21 (월)", "09/27 (일)", "활동한 날", "AI 사용", "숫자만 표시", '<div class="hm">'):
            self.assertIn(part, page)
        self.assertNotIn("한 줄 요약", page)
        self.assertIn('<td>09/21 (월)</td><td class="num">3</td><td class="num"><span data-link="daily-2026-09-21.html#am">3</span>'
                      '</td><td class="num"><span data-link="daily-2026-09-21.html#pm">0</span></td><td class="num">1</td>'
                      '<td>shop</td><td>09:05–18:40</td>', page)
        self.assertIn('<td class="sub">09/25 (금)</td><td></td>', page)  # after "today": empty

    def test_with_summary(self):
        page = self.render(SUMMARY)
        for part in ("한 줄 요약", "이번 주 한 일", "결제 버그 수정", "결정한 것", "반복된 문제", "다음 주로",
                     "활동 유형", "요약은 Claude가 작성", "학습 후보", "월 구독이 해지율이 낮다", "주간 KPT", "테스트 병렬화",
                     "자동화 검토 후보", "커밋 전에 테스트를 자동 실행", "검토할 지시 후보", "결제 실패 경로에 테스트 3개 추가해줘",
                     '<span class="chip">결제</span>', '<span class="chip">요청함</span>', "월 12:00 git",
                     "❓ 물었지만 기록상 답이 안 보이는 것", "가격은 언제부터 적용?"):
            self.assertIn(part, page)
        self.assertIn("쇼핑몰", page)  # project labels apply to the chips, bars and table
        self.assertNotIn("<td>shop</td>", page)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("🔁 이어가기", page)  # PLAN §9.1: per-project continuing is daily-only, not duplicated weekly

    def test_month_boundary_title(self):
        days = render.week_days(dt.date(2026, 10, 1))
        by = render.split_days([], days)
        page = render.render_week(days, render.week_stats(by), None, [], today=days[0])
        self.assertIn("2026년 9월 28일 – 10월 4일 주간 회고", page)

    def test_schema_matches_page(self):
        self.assertEqual(set(render.WEEK_SCHEMA["required"]), set(SUMMARY))
        self.assertEqual(render.WEEK_SCHEMA["properties"]["activity_mix"]["items"]["properties"]["type"]["enum"],
                         render.ACTIVITY_TYPES)


def fake_anthropic():
    m = types.ModuleType("anthropic")

    class APIStatusError(Exception):
        status_code, message = 500, "boom"

    m.APIStatusError = APIStatusError
    m.AuthenticationError = type("AuthenticationError", (APIStatusError,), {})
    m.RateLimitError = type("RateLimitError", (APIStatusError,), {})
    m.APIConnectionError = type("APIConnectionError", (Exception,), {})
    return m


class BackendTest(unittest.TestCase):
    """Weekly uses the daily backends and fallback: API → claude CLI (own login) → numbers only."""

    def run_summary(self, choice, api=None, cli=None, key=True, claude=True):
        calls = []

        def fake_api(prompt, system, schema):
            calls.append(("api", schema))
            if isinstance(api, Exception):
                raise api
            return api

        def fake_cli(prompt, system, schema, task, drop_api_key=False):
            calls.append(("cli", schema, drop_api_key, task))
            if isinstance(cli, Exception):
                raise cli
            return cli

        env = {"ANTHROPIC_API_KEY": "sk-test"} if key else {}
        with mock.patch.dict(sys.modules, {"anthropic": fake_anthropic()}), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(render, "summarize", fake_api), \
                mock.patch.object(render, "summarize_cli", fake_cli), \
                mock.patch.object(render.shutil, "which", lambda name: "/bin/claude" if claude else None), \
                redirect_stderr(io.StringIO()):
            if not key:
                os.environ.pop("ANTHROPIC_API_KEY", None)
                os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            got = render.get_summary(choice, "log", render.WEEK_SYSTEM, render.WEEK_SCHEMA, render.WEEK_TASK)
        return got, calls

    def test_auto_api_ok(self):
        got, calls = self.run_summary("auto", api=SUMMARY)
        self.assertEqual(got, SUMMARY)
        self.assertEqual(calls, [("api", render.WEEK_SCHEMA)])

    def test_auto_api_fails_then_cli_with_own_login(self):
        for failure in (RuntimeError("refusal"), ValueError("bad json"), KeyError("anything")):
            got, calls = self.run_summary("auto", api=failure, cli=SUMMARY)
            self.assertEqual(got, SUMMARY)
            self.assertEqual(calls[1], ("cli", render.WEEK_SCHEMA, True, render.WEEK_TASK))

    def test_auto_without_key_uses_cli(self):
        got, calls = self.run_summary("auto", cli=SUMMARY, key=False)
        self.assertEqual(calls, [("cli", render.WEEK_SCHEMA, False, render.WEEK_TASK)])

    def test_explicit_api_does_not_fall_back(self):
        got, calls = self.run_summary("api", api=RuntimeError("x"), cli=SUMMARY)
        self.assertIsNone(got)
        self.assertEqual([c[0] for c in calls], ["api"])

    def test_explicit_claude_and_none(self):
        self.assertEqual(self.run_summary("claude", cli=SUMMARY)[1], [("cli", render.WEEK_SCHEMA, False, render.WEEK_TASK)])
        self.assertEqual(self.run_summary("none", api=SUMMARY, cli=SUMMARY), (None, []))

    def test_everything_fails_numbers_only(self):
        got, calls = self.run_summary("auto", api=RuntimeError("x"), cli=RuntimeError("y"))
        self.assertIsNone(got)
        self.assertEqual([c[0] for c in calls], ["api", "cli"])


class EndToEndTest(unittest.TestCase):
    """`retro week --llm none --no-open` with an empty temp HOME plus one Claude Code transcript."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = dict(os.environ, HOME=self.home)
        now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)
        self.day = now.astimezone().date()
        folder = os.path.join(self.home, ".claude", "projects", "-work-app")
        os.makedirs(folder)
        with open(os.path.join(folder, "s.jsonl"), "w", encoding="utf-8") as f:
            for i, text in enumerate(["주간 페이지 만들기", "테스트 추가"]):
                ts = (now + dt.timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                f.write(json.dumps({"type": "user", "timestamp": ts, "cwd": "/work/app",
                                    "message": {"content": text}}, ensure_ascii=False) + "\n")

    def tearDown(self):
        shutil.rmtree(self.home)

    def retro(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "retro.py")] + list(args),
                              env=self.env, capture_output=True, text=True, timeout=300)

    def test_retro_week(self):
        res = self.retro("week", "--llm", "none", "--no-open", "--date", str(self.day))
        self.assertEqual(res.returncode, 0, res.stderr)
        monday = render.week_days(self.day)[0]
        path = os.path.join(self.home, "Retro", f"weekly-{monday}.html")
        self.assertTrue(os.path.exists(path), res.stderr)
        with open(path, encoding="utf-8") as f:
            page = f.read()
        self.assertIn("주간 회고", page)
        self.assertIn('<div class="kpi"><b>2</b><span>내가 AI에 준 지시<br>Claude Code 2</span></div>', page)
        self.assertIn(f"{self.day:%m/%d} ({render.WEEKDAYS[self.day.weekday()]})", page)
        self.assertIn("숫자만 표시", page)

    def test_daily_still_works(self):
        res = self.retro("--llm", "none", "--no-open", "--date", str(self.day))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.home, "Retro", f"daily-{self.day}.html")), res.stderr)

    def test_render_cli_week(self):
        src = os.path.join(self.home, "events.jsonl")
        with open(src, "w", encoding="utf-8") as f:
            for e in week_fixture():
                f.write(json.dumps(dict(e, ts=e["ts"].isoformat()), ensure_ascii=False) + "\n")
        out = os.path.join(self.home, "w.html")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(render.main(["--week", "--no-llm", "--date", "2026-09-24", "--out", out, src]), 0)
            self.assertEqual(render.main(["--week", "--no-llm", "--date", "2026-10-07", "--out", out, src]), 1)
        with open(out, encoding="utf-8") as f:
            self.assertIn("2026년 9월 21일 – 27일 주간 회고", f.read())


if __name__ == "__main__":
    unittest.main()
