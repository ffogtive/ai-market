"""Template v2 pages: 전체/오전/오후 tabs, new sections, schema §5, weekly heatmap and ▲▼, 관측 범위, index numbers.

No network: the claude CLI backend is faked (as in test_cache_nav.py); pages are rendered into a temp folder.

Run: python3 -m unittest discover retro/tests
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import shutil
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
DAYS = render.week_days(MON)
PREV = render.week_days(MON - dt.timedelta(days=7))


def ev(day, hm, source, text, project="shop", actor="human", host="mac"):
    h, m = hm
    ts = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)
    return {"source": source, "ts": ts, "project": project, "text": text, "actor": actor, "host": host}


# a summary with every template v2 field filled, as the LLM would return it
FULL_DAILY = {
    "one_line": "결제와 가격 작업을 했다",
    "keywords": ["결제", "가격", "테스트"],
    "done": [
        {"time": "09:05", "project": "shop", "result": "결제 버그 수정", "status": "완료",
         "evidence": [{"time": "09:05", "source": "claude"}, {"time": "12:30", "source": "git"}]},
        {"time": "11:30–12:30", "project": "shop", "result": "결제 테스트 추가", "status": "완료",
         "evidence": [{"time": "12:30", "source": "git"}]},
        {"time": "15:00", "project": "blog", "result": "가격 페이지 초안", "status": "요청함",
         "evidence": [{"time": "15:00", "source": "codex"}]},
        {"time": "", "project": "blog", "result": "시각 없는 항목", "status": "완료", "evidence": []},
    ],
    "decisions": ["가격은 월 구독으로"],
    "blockers": ["테스트가 자주 깨짐"],
    "tomorrow": ["가격 페이지 문구 다듬기"],
    "first_task_tomorrow": "가격 페이지 첫 문단 쓰기",
    "til": [{"text": "결제 웹훅은 재시도된다", "evidence": [{"time": "09:30", "source": "claude"}]}],
    "kpt": {"keep": "작게 커밋", "problem": "테스트가 느림", "try": "테스트 병렬화"},
    "prompt_coaching": [
        {"kind": "고칠 점", "prompt": "add tests", "better": "결제 실패 경로에 테스트 3개 추가해줘", "why": "범위가 드러남",
         "evidence": [{"time": "09:30", "source": "claude"}]},
        {"kind": "잘한 점", "prompt": "두 번째 코칭", "better": "", "why": "1개만 보여야 함",
         "evidence": [{"time": "09:05", "source": "claude"}]},
    ],
    "automation_ideas": [{"request": "테스트 돌려줘", "kind": "스크립트", "idea": "커밋 전에 테스트를 자동 실행"}],
    "activity_mix": [{"type": "개발", "percent": 100}],
    "project_labels": [],
}
FULL_WEEKLY = {
    "one_line": "결제를 고치고 가격 페이지를 냈다",
    "keywords": ["결제", "가격", "출시"],
    "highlights": [{"days": "월", "project": "shop", "result": "결제 버그 수정", "status": "완료",
                    "evidence": [{"time": "월 12:30", "source": "git"}]}],
    "decisions": [], "blockers": [], "next_week": ["출시 공지"],
    "til": [{"text": "웹훅은 재시도된다", "evidence": [{"time": "월 09:30", "source": "claude"}]}],
    "kpt": {"keep": "작게 커밋", "problem": "테스트가 느림", "try": "병렬화"},
    "prompt_coaching": [], "automation_ideas": [],
    "activity_mix": [{"type": "개발", "percent": 100}],
    "project_labels": [],
}


def check_schema(test, schema, value, path="$"):
    """value fits schema the way structured outputs require: every object strict, every field present."""
    kind = schema["type"]
    if kind == "object":
        test.assertIs(schema.get("additionalProperties"), False, path)
        test.assertEqual(set(schema["required"]), set(schema["properties"]), path)
        test.assertIsInstance(value, dict, path)
        test.assertEqual(set(value), set(schema["properties"]), path)
        for k, sub in schema["properties"].items():
            check_schema(test, sub, value[k], f"{path}.{k}")
    elif kind == "array":
        test.assertIsInstance(value, list, path)
        for i, x in enumerate(value):
            check_schema(test, schema["items"], x, f"{path}[{i}]")
    elif kind == "string":
        test.assertIsInstance(value, str, path)
        if "enum" in schema:
            test.assertIn(value, schema["enum"], path)
    elif kind == "integer":
        test.assertIsInstance(value, int, path)


def panel(page, key):
    """The HTML of one tab (all / am / pm)."""
    return re.search(rf'<section class="slice s-{key}">(.*?)</section>', page, re.S).group(1)


def noon_day():
    """11:59 in the morning vs 12:00 at noon, a commit after noon, and web visits before a prompt."""
    return [ev(MON, (9, 5), "claude", "fix checkout"), ev(MON, (9, 30), "claude", "add tests"),
            ev(MON, (11, 50), "chrome", "Stripe docs", "stripe.com"),
            ev(MON, (11, 59), "claude", "오전 마지막 지시"),
            ev(MON, (12, 0), "codex", "pricing page", "blog"),
            ev(MON, (12, 30), "git", "fix: checkout (+10/-2)"),
            ev(MON, (15, 0), "codex", "worker run", "blog", actor="agent", host="gpu")]


class Site(unittest.TestCase):
    """A temp ~/Retro, an events file, and a fake claude CLI returning FULL_DAILY / FULL_WEEKLY."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.src = os.path.join(self.dir, "events.jsonl")
        self.prompts = []
        self.save(noon_day())

    def save(self, events):
        with open(self.src, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(dict(e, ts=e["ts"].isoformat()), ensure_ascii=False) + "\n")

    def fake_cli(self, prompt, system, schema, task, drop_api_key=False):
        self.prompts.append((prompt, system))
        return FULL_WEEKLY if schema is render.WEEK_SCHEMA else FULL_DAILY

    def render(self, day=MON, *extra, week=False):
        kind = "weekly" if week else "daily"
        out = os.path.join(self.dir, f"{kind}-{render.week_days(day)[0] if week else day}.html")
        argv = (["--week"] if week else []) + ["--date", str(day), "--out", out, self.src] + list(extra)
        err = io.StringIO()
        with mock.patch.object(render, "summarize", mock.Mock(side_effect=AssertionError("API used"))), \
                mock.patch.object(render, "summarize_cli", self.fake_cli), \
                mock.patch.object(render.shutil, "which", lambda name: "/bin/claude"), \
                mock.patch.dict(os.environ, {}, clear=False), redirect_stderr(err):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            self.assertEqual(render.main(argv), 0, err.getvalue())
        with open(out, encoding="utf-8") as f:
            return f.read()


class TabsTest(Site):
    def test_tabs_and_deep_links(self):
        page = self.render()
        # CSS-only tabs: anchors first in <main>, then the tab row and one section per slice, all siblings
        self.assertIn('<main><span id="all" class="anchor"></span><span id="am" class="anchor"></span>'
                      '<span id="pm" class="anchor"></span><nav class="pnav">', page)
        for key, label in (("all", "전체"), ("am", "오전"), ("pm", "오후")):
            self.assertIn(f'<a class="t-{key}" href="#{key}"', page)
            self.assertIn(f'<section class="slice s-{key}">', page)
        self.assertIn("#am:target~.s-am", page)  # daily-DATE.html#am opens the morning without a script
        self.assertNotIn("<script", page)
        self.assertIn('오전 <small>3</small>', page)  # my prompts per tab
        self.assertIn('오후 <small>1</small>', page)

    def test_noon_boundary(self):
        page = self.render()
        am, pm, day = panel(page, "am"), panel(page, "pm"), panel(page, "all")
        self.assertIn("11시 · 나 → Claude Code 1", am)  # 11:59 is morning
        self.assertNotIn("12시 ·", am)
        self.assertIn("12시 · 나 → Codex 1", pm)  # 12:00 is afternoon
        self.assertNotIn("11시 ·", pm)
        self.assertIn('<div class="kpi"><b>3</b><span>내가 AI에 준 지시<br>Claude Code 3</span></div>', am)
        self.assertIn('<div class="kpi"><b>1</b><span>내가 AI에 준 지시<br>Codex 1</span></div>', pm)
        self.assertIn('<div class="kpi"><b>4</b><span>내가 AI에 준 지시<br>Claude Code 3 · Codex 1</span></div>', day)
        self.assertNotIn("커밋<br>", am)  # no commits in the morning: no commit card
        self.assertIn("커밋<br>+10 / −2줄", pm)

    def test_done_rows_split_by_time(self):
        self.assertEqual(render.row_slices("09:05"), {"am"})
        self.assertEqual(render.row_slices("11:30–12:30"), {"am", "pm"})  # spans noon
        self.assertEqual(render.row_slices("11:59"), {"am"})
        self.assertEqual(render.row_slices("12:00"), {"pm"})
        self.assertEqual(render.row_slices("23:30–01:00"), {"pm"})  # past midnight
        self.assertEqual(render.row_slices(""), set())  # untimed: 전체 only
        page = self.render()
        am, pm, day = panel(page, "am"), panel(page, "pm"), panel(page, "all")
        for text, where in (("결제 버그 수정", (am, day)), ("결제 테스트 추가", (am, pm, day)),
                            ("가격 페이지 초안", (pm, day)), ("시각 없는 항목", (day,))):
            for part in (am, pm, day):
                (self.assertIn if part in where else self.assertNotIn)(text, part)
        self.assertIn("하루 전체 요약", am)  # day-level text on a slice tab is labelled as such
        self.assertIn("하루 전체 요약 중 이 시간대", pm)
        for day_only in ("가격은 월 구독으로", "테스트 병렬화", "가격 페이지 첫 문단 쓰기"):
            self.assertIn(day_only, day)
            self.assertNotIn(day_only, am)
        self.assertIn('<a href="#all">전체</a> 탭에 있습니다', am)


class SectionsTest(Site):
    FIRST_VIEW = ["💡", "📊 오늘의 숫자", "✅ 한 일", "🧭 결정한 것", "🚧 막힌 것", "➡️ 내일로", "✍️ KPT",
                  "📚 학습 후보", "⏱ 흐름", "🧠 프롬프트 문구 신호", "🤖 AI 사용", "🌐 탐색"]

    def test_with_summary(self):
        day = panel(self.render(), "all")
        order = [day.index(x) for x in self.FIRST_VIEW]
        self.assertEqual(order, sorted(order))  # light first view, details after KPT
        for folded in ("📚 학습 후보", "⏱ 흐름", "🧠 프롬프트 문구 신호", "🤖 AI 사용", "🌐 탐색"):
            self.assertIn(f'<details class="more"><summary><h2>{folded}', day)  # closed by default
        self.assertNotIn("<details open", day)
        for part in ('<span class="chip">결제</span>', "내일 첫 할 일", "가격 페이지 첫 문단 쓰기", "(직접 작성)",
                     "작게 커밋", "결제 웹훅은 재시도된다", "(09:30 claude)", "09:05 claude, 12:30 git",
                     '<span class="chip">요청함</span>', "자동화 검토 후보", "커밋 전에 테스트를 자동 실행",
                     "검토할 지시 후보", "결제 실패 경로에 테스트 3개 추가해줘", "연속 활동 구간", "기록상 프로젝트 변경",
                     "내 지시 1건당 자동 실행", "자동 분류, 추정", "작업 직전 탐색", "stripe.com", "활동 유형 (AI 추정)"):
            self.assertIn(part, day)
        self.assertNotIn("두 번째 코칭", day)  # at most one coaching item

    def test_without_summary(self):
        page = self.render(MON, "--llm", "none")
        day = panel(page, "all")
        for part in ("📊 오늘의 숫자", "⏱ 흐름", "연속 활동 구간", "🧠 프롬프트 문구 신호", "🤖 AI 사용", "🌐 탐색",
                     "작업 직전 탐색", "✍️ KPT", "(직접 작성)"):
            self.assertIn(part, day)
        for llm_only in ("💡", "✅ 한 일", "📚 학습 후보", "내일 첫 할 일", "자동화 검토 후보", "검토할 지시 후보"):
            self.assertNotIn(llm_only, day)
        self.assertIn("숫자만 표시 — 요약 끔", page)

    def test_neutral_names_and_caveat(self):
        page = self.render()
        self.assertIn("연속 활동 구간: 기록이 이어진 구간입니다. 실제 집중·근무 시간과 다를 수 있습니다.", page)
        for loaded in ("몰입", "레버리지 비율", "컨텍스트 전환", "최장", "배운 것"):
            self.assertNotIn(loaded, page)
        self.assertNotIn("#c24f6b", render.TYPE_COLORS.values())  # no warning red for 수정·불만

    def test_pre_work_browsing(self):
        am = panel(self.render(), "am")
        self.assertIn("작업 직전 탐색 — 내 지시 전 10분 안에 본 페이지 1건 (웹 방문 1건 중)", am)  # 11:50 → 11:59

    def test_coaching_needs_a_cited_prompt(self):
        s = dict(FULL_DAILY, prompt_coaching=[dict(FULL_DAILY["prompt_coaching"][0], evidence=[])])
        self.assertEqual(render.coaching(s), "")
        self.assertEqual(render.coaching(dict(FULL_DAILY, prompt_coaching=[])), "")
        self.assertIn("add tests", render.coaching(FULL_DAILY))


class CoverageTest(Site):
    def test_scope_line(self):
        page = self.render(MON, "--off", "chrome,codex")
        scope = re.search(r"<dt>관측 범위</dt><dd>(.*?)</dd>", page).group(1)
        self.assertIn("Claude Code (mac) 3", scope)
        self.assertIn("Codex (gpu) 1", scope)  # per source and host
        self.assertIn("09:05 – 15:00", scope)  # first–last record
        self.assertIn("꺼진 소스: chrome, codex", scope)
        self.assertIn("클라우드 Claude Code 대화는 수집하지 않음(커밋으로만 반영)", scope)

    def test_retro_passes_off_sources(self):
        import retro
        with mock.patch.object(retro, "load_config", lambda: {"off": ["codex"]}):
            self.assertEqual(retro.off_args(argparse.Namespace(no_chrome=True)), ["--off", "codex,chrome"])
        with mock.patch.object(retro, "load_config", lambda: {}):
            self.assertEqual(retro.off_args(argparse.Namespace(no_chrome=False)), [])

    def test_retro_week_loads_the_week_before(self):
        import retro
        seen = {}
        today = dt.date.today()
        monday = render.week_days(today)[0]
        with mock.patch.object(retro, "collect_all", lambda day, days, no_chrome: seen.update(days=days) or "ev.jsonl"), \
                mock.patch.object(retro, "render_and_open", lambda argv, out, no_open: seen.update(argv=argv) or 0), \
                mock.patch.object(retro, "load_config", lambda: {}):
            retro.cmd_week(argparse.Namespace(date=None, llm="none", no_chrome=False, no_open=True, refresh=False))
        self.assertEqual(seen["days"], (today - monday).days + 7)  # back to the Monday before, for ▲▼


class AIUsageTest(unittest.TestCase):
    def test_delegation_chain_by_tool_and_host(self):
        events = [ev(MON, (9, i), "codex", f"작업 {i} 해줘") for i in range(2)]
        events += [ev(MON, (10, i % 60), "claude", "agent step", actor="agent", host="gpu") for i in range(300)]
        events.sort(key=lambda e: e["ts"])
        page = render.render_page(MON, render.day_stats(events), None, events)
        day = panel(page, "all")
        self.assertIn("나 → Codex 2건 → (자동 실행) Claude Code (gpu) 300건", day)
        self.assertIn('<td>Claude Code</td><td>gpu</td><td class="num">0</td><td class="num">300</td>', day)
        self.assertIn('<div class="kpi"><b>150건</b><span>내 지시 1건당 자동 실행</span></div>', day)
        self.assertIn("AI 자동 실행 (에이전트)<br>Claude Code 300", day)
        self.assertIn("누가 보냈는지는 기록에 없어", day)  # the source of an automatic run is not claimed

    def test_no_prompts_is_not_computable(self):
        events = [ev(MON, (10, 0), "claude", "agent step", actor="agent", host="gpu")]
        page = render.render_page(MON, render.day_stats(events), None, events)
        self.assertIn('<div class="kpi"><b>계산 불가</b><span>내 지시 1건당 자동 실행</span></div>', page)


class SchemaTest(unittest.TestCase):
    V2 = {"keywords", "til", "kpt", "prompt_coaching", "automation_ideas"}

    def test_new_required_fields(self):
        self.assertLessEqual(self.V2 | {"first_task_tomorrow"}, set(render.SUMMARY_SCHEMA["required"]))
        self.assertLessEqual(self.V2, set(render.WEEK_SCHEMA["required"]))
        done = render.SUMMARY_SCHEMA["properties"]["done"]["items"]["properties"]
        self.assertEqual(done["status"]["enum"], ["완료", "요청함"])
        for schema in (render.SUMMARY_SCHEMA["properties"]["done"]["items"],
                       render.SUMMARY_SCHEMA["properties"]["til"]["items"],
                       render.SUMMARY_SCHEMA["properties"]["prompt_coaching"]["items"],
                       render.WEEK_SCHEMA["properties"]["highlights"]["items"]):
            self.assertEqual(set(schema["properties"]["evidence"]["items"]["properties"]), {"time", "source"})

    def test_stubs_fill_the_schema(self):
        check_schema(self, render.SUMMARY_SCHEMA, FULL_DAILY)
        check_schema(self, render.WEEK_SCHEMA, FULL_WEEKLY)
        import test_cache_nav  # the stubs other tests use are complete too
        check_schema(self, render.SUMMARY_SCHEMA, test_cache_nav.DAILY)
        check_schema(self, render.WEEK_SCHEMA, test_cache_nav.WEEKLY)

    def test_instructions(self):
        for system in (render.SYSTEM, render.WEEK_SYSTEM):
            self.assertIn("요청은 완료 증거가 아닙니다", system)
            self.assertIn("다시 세거나", system)  # interpret the facts, don't recount
            self.assertIn("빈 배열", system)

    def test_prompt_gets_facts_and_repeats(self):
        events = [ev(MON, (9, i * 5), "claude", "retro/render.py 테스트 돌려줘") for i in range(3)]
        events.append(ev(MON, (10, 0), "claude", "로그인 페이지 만들어줘"))
        p = render.build_prompt(MON, events, render.day_stats(events))
        self.assertIn("[자동 계산 지표 — 다시 세지 말고 해석만]", p)
        self.assertIn("[반복 요청 Top]\n- retro/render.py 테스트 돌려줘 ×3", p)
        self.assertEqual(p.count("반복 요청"), 1)  # not twice (facts line + list)
        self.assertLess(p.index("[자동 계산 지표"), p.index("<log>"))
        facts = p[:p.index("<log>")]
        self.assertLess(len(facts), 1200)  # small: the log is the prompt, the facts are a few lines

    def test_summary_renders_all_fields(self):
        page = render.render_page(MON, render.day_stats(noon_day()), FULL_DAILY, noon_day())
        for part in ("결제", "가격 페이지 첫 문단 쓰기", "결제 웹훅은 재시도된다", "테스트 병렬화", "add tests",
                     "커밋 전에 테스트를 자동 실행"):
            self.assertIn(part, page)

    def test_old_summary_shapes_still_render(self):
        old = {"one_line": "옛 요약", "done": [{"time": "09:05", "project": "shop", "result": "x", "evidence": "Claude Code"}],
               "til": ["옛 배운 것"], "decisions": [], "blockers": [], "tomorrow": [], "activity_mix": [],
               "project_labels": []}
        page = render.render_page(MON, render.day_stats(noon_day()), old, noon_day())
        self.assertIn("<td>Claude Code</td>", page)
        self.assertIn("옛 배운 것", page)


def week_events(prev=True, same_sources=False):
    """This week (Mon + Wed) and, optionally, the week before."""
    wed = MON + dt.timedelta(days=2)
    events = [ev(MON, (9, 5), "claude", "fix checkout"), ev(MON, (9, 30), "claude", "add tests"),
              ev(MON, (11, 0), "codex", "write post", "blog"), ev(MON, (12, 0), "git", "fix (+10/-2)"),
              ev(wed, (14, 0), "claude", "plan the launch"), ev(wed, (15, 0), "git", "feat (+5/-1)")]
    if prev:
        events += [ev(PREV[0], (9, 0), "claude", "last week 1"), ev(PREV[2], (9, 0), "claude", "last week 2")]
        if same_sources:
            events += [ev(PREV[2], (10, 0), "codex", "old post", "blog"), ev(PREV[2], (11, 0), "git", "old (+1/-0)")]
    return sorted(events, key=lambda e: e["ts"])


class WeeklyTest(Site):
    def render_week(self, events, **kw):
        this = [e for e in events if e["ts"].date() in DAYS]
        before = [e for e in events if e["ts"].date() in PREV]
        stats = render.week_stats(render.split_days(this, DAYS))
        return render.render_week(DAYS, stats, None, this, today=DAYS[-1], prev_events=before or None, **kw)

    def test_heatmap_7x24_with_links(self):
        page = self.render_week(week_events(), day_links={MON: "daily-2026-09-21.html"})
        grid = re.search(r'<div class="hm">(.*?)</div>', page, re.S).group(1)
        self.assertEqual(len(re.findall(r'<i class="hc', grid)), 7 * 24)
        self.assertEqual(len(re.findall(r'class="hm-d"', grid)), 7)
        self.assertIn('<a href="daily-2026-09-21.html">월 09/21</a>', grid)
        self.assertIn('<span data-link="daily-2026-09-23.html">수 09/23</span>', grid)  # no page yet
        self.assertIn('title="월 09/21 9시 · 2건"', grid)
        self.assertIn('<i class="hc z" title="화 09/22 9시 · 0건"></i>', grid)
        self.assertIn('<a href="daily-2026-09-21.html#am">3</a>', page)  # 날짜별: 오전 count → that tab
        self.assertIn('<a href="daily-2026-09-21.html#pm">0</a>', page)

    def test_delta_only_with_last_week(self):
        without = self.render_week(week_events(prev=False))
        for mark in ("▲", "▼", "지난주 대비"):
            self.assertNotIn(mark, without)
        page = self.render_week(week_events())
        self.assertIn("지난주 대비", page)
        self.assertIn("<b>4<small>▲100%</small></b>", page)  # prompts 4 vs 2: a percentage
        self.assertIn("<b>2<small>▲2</small></b>", page)  # commits 2 vs 0: the plain difference
        self.assertIn("관측 소스가 달라 비교가 부정확할 수 있음", page)  # last week had no codex/git
        same = self.render_week(week_events(same_sources=True))
        self.assertNotIn("관측 소스가 달라", same)

    def test_signal_times_carry_the_weekday(self):
        wed = MON + dt.timedelta(days=2)
        retries = [ev(d, (8, m), "claude", "테스트 돌려줘") for d in (MON, wed) for m in (0, 5)]
        page = self.render_week(sorted(week_events(prev=False) + retries, key=lambda e: e["ts"]))
        self.assertIn("거의 같은 지시 2회 (월 08:05, 수 08:05)", page)
        self.assertIn("<li>수 08:05 거의 같은 지시 다시 (08:00에 먼저)", page)

    def test_week_leverage_is_sums_divided(self):
        events = week_events(prev=False) + [ev(MON, (13, 0), "codex", "run", "blog", actor="agent", host="gpu")] * 3
        page = self.render_week(sorted(events, key=lambda e: e["ts"]))
        self.assertIn("<b>0.75건</b><span>내 지시 1건당 자동 실행</span>", page)  # 3 runs / 4 prompts, not a mean of days

    def test_weekly_page_via_main(self):
        self.save(week_events())
        page = self.render(week=True)
        self.assertIn("지난주 대비", page)
        self.save(week_events(prev=False))
        self.assertNotIn("지난주 대비", self.render(week=True))
        for part in ("💡", "결제", "✍️ 주간 KPT", "(직접 작성)", "📚 학습 후보", "웹훅은 재시도된다", "🗓 요일×시간대",
                     "🧠 프롬프트 문구 신호", "요일별 문구 유형", "🗂 어디에 썼나", "요일별 프로젝트", "➡️ 다음 주로"):
            self.assertIn(part, page)

    def test_similar_requests(self):
        events = week_events(prev=False) + [ev(MON, (16, i * 20), "claude", t) for i, t in
                                            enumerate(["retro/render.py 테스트 돌려줘", "~/x/collect.py 테스트 돌려줘",
                                                       "테스트 돌려줘 (3)"])]
        page = self.render_week(sorted(events, key=lambda e: e["ts"]))
        self.assertIn("비슷한 요청이 3회 있었습니다. 수정 과정인지 반복 업무인지 확인해보세요."
                      '<br><span class="sub">“retro/render.py 테스트 돌려줘”</span>'
                      '<br><span class="sub">“~/x/collect.py 테스트 돌려줘”</span>', page)


class IndexNumbersTest(Site):
    def test_llm_none_saves_numbers(self):
        self.render(MON, "--llm", "none")
        self.assertEqual(self.prompts, [])
        path = render.cache_path(self.dir, "daily", MON)
        self.assertIsNone(render.read_cache(path))  # no summary…
        self.assertEqual(render.read_entry(path)["numbers"], {"prompts": 4, "commits": 1})  # …but the counts
        with open(os.path.join(self.dir, "index.html"), encoding="utf-8") as f:
            index = f.read()
        self.assertIn('09/21 (월)</a></td><td></td><td class="num">4</td><td class="num">1</td>', index)
        self.render()  # then with a summary: the numbers-only file is not reused as one
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(render.read_cache(path)["summary"], FULL_DAILY)


if __name__ == "__main__":
    unittest.main()
