"""analyze.py: time slices, prompt classification, prompt behavior, work rhythm, heatmap, LLM facts.

Synthetic events only, no I/O. Run: python3 -m unittest discover retro/tests
"""
import datetime as dt
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import analyze as an  # noqa: E402
import render  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=9))
DAY = dt.date(2026, 9, 23)
T0 = dt.datetime(2026, 9, 23, 9, 0, tzinfo=TZ)


def at(minutes_, day=DAY):
    """09:00 on day + minutes."""
    return dt.datetime(day.year, day.month, day.day, 9, 0, tzinfo=TZ) + dt.timedelta(minutes=minutes_)


def ev(ts, source, text, project="", actor="human"):
    if isinstance(ts, tuple):
        ts = dt.datetime(DAY.year, DAY.month, DAY.day, ts[0], ts[1], tzinfo=TZ)
    return {"source": source, "ts": ts, "project": project, "text": text, "actor": actor, "host": "mac"}


def prompt(ts, text="fix the checkout page", project="shop", source="claude"):
    return ev(ts, source, text, project)


class SliceTest(unittest.TestCase):
    def test_noon_boundary(self):
        events = [prompt((0, 0), "a"), prompt((11, 59), "b"), prompt((12, 0), "c"), prompt((23, 59), "d")]
        self.assertEqual([e["text"] for e in an.in_slice(events, "am")], ["a", "b"])
        self.assertEqual([e["text"] for e in an.in_slice(events, "pm")], ["c", "d"])
        self.assertEqual(len(an.in_slice(events, "all")), 4)

    def test_parts_of_day(self):
        self.assertEqual([an.part_of_day(ev((h, 0), "claude", "")["ts"]) for h in (0, 5, 6, 11, 12, 17, 18, 23)],
                         ["심야", "심야", "오전", "오전", "오후", "오후", "저녁", "저녁"])

    def test_ai_sources_match_render(self):
        self.assertEqual(an.AI_SOURCES, render.AI_SOURCES)


class ClassifyTest(unittest.TestCase):
    CASES = {
        an.PASTE: [
            "sjy@lucass-MacBook ai-market % retro week --llm none zsh: command not found: retro",
            "jy@box:~/ai-market$ python3 retro/render.py --week",
            "$ git push origin main ! [rejected] main -> main (fetch first)",
            "Traceback (most recent call last): File \"retro/render.py\", line 12, in <module> KeyError: 'ts'",
            "TypeError: Cannot read properties of undefined (reading 'map') 이거 고쳐줘",
            "npm ERR! code ERESOLVE",
            "12:00:01 INFO start 12:00:02 WARN slow 12:00:03 INFO done",
            "line one\nline two\nline three\nline four\nline five\nline six",
        ],
        an.FIX: [
            "아니 그거 말고 다른 파일", "다시 해봐", "왜 안 돼?", "틀렸어, 월요일부터야", "계속 에러 나", "여전히 안 보여",
            "no, use the other file", "that's wrong", "it still doesn't work",
        ],
        an.APPROVE: [
            "PR 머지해줘", "머지해줘", "진행해줘", "ㅇㅇ", "응", "응 진행해", "좋아", "계속", "ok", "yes, go ahead",
            "Continue", "lgtm", "[음성] 좋아 그렇게 해", "재개", "3개 다 좋음",
        ],
        an.QUESTION: [
            "이거 어떻게 고쳐?", "좋아?", "아니면 B안으로 할까?", "이게 뭐야", "이 함수는 뭐하는 거지",
            "how do I add a test", "What does this function return?", "why is the build slow",
        ],
        an.INSTRUCT: [
            "로그인 페이지 만들어줘", "좋아 그런데 버튼 색 바꿔줘", "이거 고쳐줄래?", "왜 실패했는지 확인해줘",
            "결제 모듈 리팩터링 진행해 줘 테스트도 같이 돌려", "[첨부] README 업데이트", "[음성] 테스트 돌려줘",
            "fix the login bug", "Can you add a dark mode toggle?", "please rename the file",
            "이 아이디어 킵해놔", "정면 각도만이 아니라 다양한 각도로 보여주는 스킬도 추가해줘",
            "화면 전환은 생략하지 말고 자연스럽게 이어지게 만들어줘",
        ],
        an.OTHER: ["retro 테스트", "[첨부 파일]", "", "sjy@ffogtive.com"],
    }

    def test_categories(self):
        for want, texts in self.CASES.items():
            for text in texts:
                with self.subTest(text=text):
                    self.assertEqual(an.classify_prompt(text), want)

    def test_email_with_percent_is_not_a_shell_prompt(self):
        self.assertEqual(an.classify_prompt("sjy@ffogtive.com 으로 50% 할인 쿠폰 보내"), an.INSTRUCT)

    def test_again_alone_is_not_a_complaint(self):
        # "다시" in a normal, longer instruction is a request, not a complaint
        for text in ("결제 페이지를 처음부터 다시 설계하고 테스트까지 추가해줘",
                     "로그인 페이지 레이아웃을 다시 정리하고 버튼 색도 파란색으로 바꿔줘",
                     "[음성] 어제 만든 가격 페이지 문구를 다시 읽어보고 오타를 전부 고쳐줘"):
            with self.subTest(text=text):
                self.assertGreater(len(an.clean(text)), an.AGAIN_SHORT)
                self.assertEqual(an.classify_prompt(text), an.INSTRUCT)
        # short, or next to 아니·왜·안 돼·틀렸·말고·제대로: a complaint
        for text in ("다시", "다시 해봐", "테스트 다시 돌려줘", "처음부터 다시 만들어줘 이번엔 꼭",
                     "왜 이렇게 됐는지 모르겠는데 결제 모듈 전체를 다시 만들어줘 테스트도",
                     "결제 모듈 전체를 제대로 다시 만들어줘 테스트까지 전부 포함해서 부탁해",
                     "이건 안돼 결제 모듈 쪽을 처음부터 다시 확인해서 전부 고쳐줘 이번엔",
                     "그거 말고 결제 모듈 전체를 처음부터 다시 만들어줘 테스트도 같이"):
            with self.subTest(text=text):
                self.assertEqual(an.classify_prompt(text), an.FIX)
        # "왜" without "다시" is not a complaint on its own
        self.assertEqual(an.classify_prompt("왜 이렇게 동작하는지 결제 모듈 코드를 읽고 설명해줘"), an.INSTRUCT)

    def test_long_approval_is_instruction(self):
        # "진행" is an approval only when short; a long text with it is a request
        self.assertEqual(an.classify_prompt("좋아 이 방향으로 진행하고 결제 모듈 테스트까지 추가해줘"), an.INSTRUCT)

    def test_ani_ra_and_ji_malgo_are_descriptive_not_complaints(self):
        # "A가 아니라 B"(A 아니고 B)와 "…하지 말고 …해줘"(하지 마 연결어미)는 실사용 로그에서 반복 등장한
        # 서술적 표현이지 거부·불만이 아니다. "아니면"과 같은 취급.
        for text in ("이 색은 기본값이 아니라 강조색으로 써줘",
                     "여기서 멈추지 말고 다음 단계까지 진행해줘"):
            with self.subTest(text=text):
                self.assertEqual(an.classify_prompt(text), an.INSTRUCT)
        # but a bare rejection ("아니", "그거 말고") is still a complaint
        self.assertEqual(an.classify_prompt("아니 그거 말고 다른 파일"), an.FIX)


class BehaviorTest(unittest.TestCase):
    def test_counts_only_my_ai_prompts(self):
        events = [
            prompt(at(0), "로그인 페이지 만들어줘"),
            ev(at(1), "codex", "worker task", "shop", actor="agent"),
            ev(at(2), "git", "feat: login (+1/-0)", "shop"),
            ev(at(3), "chrome", "Docs", "github.com"),
            prompt(at(4), "이게 뭐야", source="claude.ai"),
            prompt(at(5), "ㅇㅇ", source="chatgpt"),
        ]
        b = an.prompt_behavior(events)
        self.assertEqual(b["count"], 3)
        self.assertEqual(list(b["types"]), list(an.CATEGORIES))
        self.assertEqual((b["types"]["지시"], b["types"]["질문"], b["types"]["확인·승인"]), (1, 1, 1))
        self.assertEqual((b["first"], b["last"]), (at(0), at(5)))

    def test_length_buckets(self):
        texts = ["ㅇㅇ", "x" * 19, "x" * 20, "x" * 200, "x" * 201, "[음성] " + "x" * 201]
        b = an.prompt_behavior([prompt(at(i * 40), t) for i, t in enumerate(texts)])
        self.assertEqual(b["length_median"], 110)  # (20 + 200) / 2; the [음성] tag is not counted
        self.assertAlmostEqual(b["short_ratio"], 0.333)
        self.assertAlmostEqual(b["long_ratio"], 0.333)
        self.assertAlmostEqual(b["normal_ratio"], 0.333)

    def test_retry_within_30_minutes(self):
        b = an.prompt_behavior([prompt(at(0), "테스트 돌려줘"), prompt(at(29), "테스트 돌려줘!")])
        self.assertEqual(b["retries"], 1)
        self.assertEqual(b["retry_list"][0]["prev_ts"], at(0))

    def test_retry_outside_30_minutes(self):
        b = an.prompt_behavior([prompt(at(0), "테스트 돌려줘"), prompt(at(31), "테스트 돌려줘")])
        self.assertEqual(b["retries"], 0)

    def test_retry_needs_same_tool_host_project(self):
        first = prompt(at(0), "테스트 돌려줘")
        for other in (prompt(at(5), "테스트 돌려줘", project="blog"), prompt(at(5), "테스트 돌려줘", source="codex"),
                      dict(prompt(at(5), "테스트 돌려줘"), host="gpu")):
            with self.subTest(other=(other["source"], other["host"], other["project"])):
                self.assertEqual(an.prompt_behavior([first, other])["retries"], 0)
        self.assertEqual(an.prompt_behavior([first, prompt(at(5), "테스트 돌려줘")])["retries"], 1)

    def test_retry_similarity_uses_the_start(self):
        long_ = "결제 모듈 테스트를 돌리고 실패한 케이스를 정리해줘 " * 10
        b = an.prompt_behavior([prompt(at(0), long_), prompt(at(5), "지금 " + long_)])
        self.assertEqual(b["retries"], 1)  # same start → retry, even though only the first 80 chars are compared

    def test_retry_similar_not_prefix(self):
        b = an.prompt_behavior([prompt(at(0), "fix the login bug in auth"),
                                prompt(at(5), "please fix the login bug in auth")])
        self.assertEqual(b["retries"], 1)

    def test_different_requests_and_approvals_are_not_retries(self):
        b = an.prompt_behavior([prompt(at(0), "로그인 페이지 만들어줘"), prompt(at(1), "결제 모듈 테스트 추가해줘"),
                                prompt(at(2), "계속"), prompt(at(3), "계속"), prompt(at(4), "계속")])
        self.assertEqual(b["retries"], 0)

    def test_complaint_streaks(self):
        texts = ["로그인 만들어줘", "아니 그거 말고", "다시 해봐", "왜 안 돼?", "좋아", "틀렸어", "고마워"]
        b = an.prompt_behavior([prompt(at(i), t) for i, t in enumerate(texts)])
        self.assertEqual(b["max_complaint_streak"], 3)
        self.assertEqual(len(b["complaint_streaks"]), 1)  # the lone "틀렸어" is not a streak
        self.assertEqual(b["complaint_streaks"][0]["start"], at(1))

    def test_complaint_streak_breaks_after_30_minutes(self):
        """A real day chained 20:29 to 08:01 the next morning into one streak — consecutive, but not one stretch."""
        b = an.prompt_behavior([prompt(at(0), "아니 틀렸어"), prompt(at(6), "왜 안 돼?"),
                                prompt(at(6 + 60 * 12), "아니 그거 말고")])  # 12 hours later
        self.assertEqual(len(b["complaint_streaks"]), 1)
        self.assertEqual(b["complaint_streaks"][0]["count"], 2)  # the far one does not join
        self.assertEqual(b["complaint_streaks"][0]["end"], at(6))

    def test_complaint_streak_within_30_minutes_stays(self):
        b = an.prompt_behavior([prompt(at(0), "아니 틀렸어"), prompt(at(6), "왜 안 돼?"), prompt(at(21), "다시 해봐")])
        self.assertEqual(b["max_complaint_streak"], 3)

    def test_plain_command_form_is_an_instruction(self):
        """"작업완료해" 같은 반말 명령형이 어떤 지시 단서에도 안 걸려 기타로 빠지고 있었다."""
        self.assertEqual(an.classify_prompt("최종본으로 작업완료해"), an.INSTRUCT)
        self.assertNotEqual(an.classify_prompt("작업 완료됐어"), an.INSTRUCT)  # 상황 공유는 지시가 아니다
        self.assertNotEqual(an.classify_prompt("이부분은 질감이 너무 이상해"), an.INSTRUCT)  # 형용사 오탐 금지

    def test_paste_ratio(self):
        b = an.prompt_behavior([prompt(at(0), "sjy@mac ai-market % retro"), prompt(at(1), "고쳐줘"),
                                prompt(at(2), "ValueError: bad"), prompt(at(3), "좋아")])
        self.assertEqual(b["paste_ratio"], 0.5)

    def test_top_repeated_normalizes(self):
        texts = [
            "retro/render.py 테스트 돌려줘", "~/ai-market/collect.py 테스트 돌려줘", "테스트 돌려줘 (2)",
            "https://github.com/x/y/pull/7 리뷰해줘", "https://github.com/x/y/pull/8 리뷰해줘",
            "한 번만 나온 요청", "ㅇㅇ", "ㅇㅇ",
        ]
        b = an.prompt_behavior([prompt(at(i * 40), t) for i, t in enumerate(texts)])
        self.assertEqual(b["top_repeated"], [("retro/render.py 테스트 돌려줘", 3),
                                             ("https://github.com/x/y/pull/7 리뷰해줘", 2)])

    def test_repeated_groups_keep_two_examples(self):
        texts = ["retro/render.py 테스트 돌려줘", "retro/render.py 테스트 돌려줘", "~/x/collect.py 테스트 돌려줘",
                 "테스트 돌려줘 (3)"]
        b = an.prompt_behavior([prompt(at(i * 40), t) for i, t in enumerate(texts)])
        self.assertEqual(b["repeated"], [{"count": 4, "examples": ["retro/render.py 테스트 돌려줘",
                                                                   "~/x/collect.py 테스트 돌려줘"]}])
        self.assertEqual(b["top_repeated"], [("retro/render.py 테스트 돌려줘", 4)])

    def test_long_runs_stay_fast(self):
        # a 600-char run without spaces (a pasted token, base64 …) must not make the regexes quadratic
        import time
        events = [prompt(at(i), "x" * 600 + str(i)) for i in range(200)]
        start = time.time()
        an.prompt_behavior(events)
        self.assertLess(time.time() - start, 5)

    def test_top_repeated_cap(self):
        events = [prompt(at(k * 2 + i), f"요청 {chr(0xAC00 + k)} 해줘") for k in range(7) for i in range(2)]
        self.assertEqual(len(an.prompt_behavior(events)["top_repeated"]), an.TOP_REPEATED)

    def test_slices_and_parts(self):
        hours = [(2, 0), (9, 0), (11, 59), (12, 0), (19, 0), (23, 0)]
        b = an.prompt_behavior([prompt(h, f"요청 {i}") for i, h in enumerate(hours)])
        self.assertEqual(b["by_slice"], {"am": 3, "pm": 3})
        self.assertEqual(b["by_part"], {"심야": 1, "오전": 2, "오후": 1, "저녁": 2})

    def test_no_prompts(self):
        b = an.prompt_behavior([ev(at(0), "git", "x", "shop")])
        self.assertEqual((b["count"], b["length_median"], b["paste_ratio"], b["first"]), (0, 0, 0.0, None))


class RhythmTest(unittest.TestCase):
    def chain(self, gap, n, start=0, project="shop"):
        return [prompt(at(start + i * gap), f"step {i}", project) for i in range(n)]

    def test_gap_14_chains_gap_16_breaks(self):
        r = an.work_rhythm(self.chain(14, 5))  # 56 min
        self.assertEqual(len(r["focus_blocks"]), 1)
        self.assertEqual(r["focus_minutes"], 56)
        r = an.work_rhythm(self.chain(16, 5))
        self.assertEqual(r["focus_blocks"], [])

    def test_min_block_length(self):
        short = [prompt(at(0)), prompt(at(14)), prompt(at(28)), prompt(at(42)), prompt(at(44))]
        self.assertEqual(an.work_rhythm(short)["focus_blocks"], [])
        long_ = short[:-1] + [prompt(at(46))]
        r = an.work_rhythm(long_)
        self.assertEqual(r["focus_minutes"], 46)
        self.assertEqual(r["longest"]["minutes"], 46)

    def test_blocks_mix_activity_and_pick_longest(self):
        events = [prompt(at(0), "a", "shop"), ev(at(10), "chrome", "Docs", "github.com"),
                  ev(at(20), "git", "fix (+1/-0)", "shop"), prompt(at(30), "b", "blog"), prompt(at(40), "c", "shop"),
                  prompt(at(50), "d", "shop"), ev(at(64), "codex", "agent", "shop", actor="agent")]  # not my activity
        events += self.chain(10, 8, start=200, project="blog")  # 70 min
        r = an.work_rhythm(events)
        self.assertEqual([b["minutes"] for b in r["focus_blocks"]], [50, 70])
        first = r["focus_blocks"][0]
        self.assertEqual((first["project"], first["prompts"], first["commits"], first["web"]), ("shop", 4, 1, 1))
        self.assertEqual((r["longest"]["project"], r["longest"]["start"]), ("blog", at(200)))
        self.assertEqual(r["focus_minutes"], 120)

    def test_web_only_chain_is_not_a_block(self):
        web = [ev(at(i * 10), "chrome", "Docs", "github.com") for i in range(8)]  # 70 min of browsing only
        self.assertEqual(an.work_rhythm(web)["focus_blocks"], [])
        with_prompt = sorted(web + [prompt(at(35))], key=lambda e: e["ts"])
        blocks = an.work_rhythm(with_prompt)["focus_blocks"]
        self.assertEqual([(b["minutes"], b["prompts"], b["web"]) for b in blocks], [(70, 1, 8)])

    def test_browse_before_prompts(self):
        events = [ev(at(0), "chrome", "A", "docs.python.org"),  # next prompt 12 min later: no
                  ev(at(5), "chrome", "B", "stackoverflow.com"),  # 7 min: yes
                  prompt(at(12)),
                  ev(at(30), "chrome", "C", "news.com"),  # 31 min: no
                  ev(at(51), "chrome", "D", "stackoverflow.com"),  # exactly 10 min: yes
                  prompt(at(61)), ev(at(62), "chrome", "E", "after.com")]  # no prompt after it
        r = an.browse_before_prompts(events)
        self.assertEqual((r["visits"], r["web"]), (2, 5))
        self.assertEqual(r["sites"], [("stackoverflow.com", 2)])

    def test_context_switches_ignore_web_and_empty(self):
        events = [prompt(at(0), "a", "shop"), ev(at(1), "chrome", "Docs", "github.com"), prompt(at(2), "b", "shop"),
                  prompt(at(3), "c", ""), prompt(at(4), "d", "blog"), ev(at(5), "git", "x", "blog"),
                  prompt(at(6), "e", "shop")]
        r = an.work_rhythm(events)
        self.assertEqual(r["switches"], 2)  # shop → blog → shop
        self.assertEqual([(s["project"], s["events"]) for s in r["segments"]], [("shop", 2), ("blog", 2), ("shop", 1)])

    def test_max_concurrent(self):
        events = [prompt(at(0), "a", "p1"), prompt(at(10), "b", "p2"), prompt(at(20), "c", "p3"),
                  prompt(at(100), "d", "p4"), prompt(at(170), "e", "p1")]  # p4 and p1 are 70 min apart
        r = an.work_rhythm(events)
        self.assertEqual((r["max_concurrent"], r["max_concurrent_at"]), (3, at(0)))

    def test_leverage(self):
        r = an.work_rhythm([prompt(at(0)), prompt(at(1)), ev(at(2), "codex", "w", "shop", actor="agent")])
        self.assertEqual(r["leverage"], 0.5)
        r = an.work_rhythm([ev(at(0), "codex", "w", "shop", actor="agent"), ev(at(1), "git", "x", "shop")])
        self.assertIsNone(r["leverage"])

    def test_prompt_to_commit(self):
        events = [ev(at(-60), "git", "old commit", "shop"), prompt(at(0), "a", "shop"), prompt(at(5), "b", "blog"),
                  prompt(at(10), "c", "shop"), ev(at(45), "git", "fix", "shop"), ev(at(50), "git", "more", "shop")]
        pc = an.work_rhythm(events)["prompt_commit"]
        self.assertEqual(list(pc), ["shop", "blog"])
        self.assertEqual((pc["shop"]["prompts"], pc["shop"]["commits"], pc["shop"]["minutes"]), (2, 3, 45))
        self.assertEqual(pc["shop"]["first_commit"], at(45))  # the commit before the first prompt is not it
        self.assertEqual((pc["blog"]["commits"], pc["blog"]["minutes"]), (0, None))

    def test_empty(self):
        r = an.work_rhythm([])
        self.assertEqual((r["focus_minutes"], r["longest"], r["switches"], r["max_concurrent"]), (0, None, 0, 0))


class ContinuePointsTest(unittest.TestCase):
    """🔁 이어가기 (PLAN §9.1): per-project pickup segments, last instruction, commits after it."""

    def test_gap_45_chains_gap_46_breaks(self):
        chained = an.continue_points([prompt(at(0), "a"), prompt(at(44), "b"), prompt(at(89), "c")])  # gaps 44, 45
        self.assertEqual(len(chained[0]["segments"]), 1)
        split = an.continue_points([prompt(at(0), "a"), prompt(at(46), "b")])  # gap 46 > CONTINUE_GAP
        self.assertEqual(len(split[0]["segments"]), 2)

    def test_lone_instruction_segment_has_equal_start_and_end(self):
        p = an.continue_points([prompt(at(0), "only one")])[0]
        self.assertEqual((p["count"], p["segments"]), (1, [{"start": at(0), "end": at(0)}]))

    def test_last_instruction_skips_attachment_only_tail(self):
        """PLAN §9.4: 첨부·음성 태그를 떼면 내용이 없는 지시는 건너뛰고 그 앞의 내용 있는 지시를 쓴다."""
        events = [prompt(at(0), "실제 지시 내용"), prompt(at(10), "[첨부 파일]"), prompt(at(20), "[음성]")]
        p = an.continue_points(events)[0]
        self.assertEqual((p["last"]["ts"], p["last"]["text"]), (at(0), "실제 지시 내용"))

    def test_last_instruction_none_when_every_prompt_is_tag_only(self):
        p = an.continue_points([prompt(at(0), "[첨부 파일]"), prompt(at(10), "[음성]")])[0]
        self.assertIsNone(p["last"])
        self.assertEqual(p["commits"], [])  # nothing to anchor "after the last instruction" to

    def test_last_instruction_text_is_clipped_to_200_chars(self):
        p = an.continue_points([prompt(at(0), "x" * 250)])[0]
        self.assertEqual(len(p["last"]["text"]), 200)

    def test_commits_only_count_strictly_after_the_last_instruction(self):
        events = [prompt(at(0), "a"), ev(at(5), "git", "old commit", "shop"),
                  prompt(at(10), "b"), ev(at(20), "git", "new commit", "shop")]
        p = an.continue_points(events)[0]
        self.assertEqual(p["last"]["ts"], at(10))
        self.assertEqual([c["text"] for c in p["commits"]], ["new commit"])

    def test_no_commits_is_an_empty_list(self):
        """PLAN §9.4: "이후 커밋 없음"을 나타내는 특수 값이 아니라 그냥 빈 리스트 — render가 줄 자체를 생략한다."""
        self.assertEqual(an.continue_points([prompt(at(0), "a")])[0]["commits"], [])

    def test_tmp_projects_are_flagged(self):
        self.assertTrue(an.continue_points([prompt(at(0), "a", project="tmp.k12UodOzwB")])[0]["tmp"])
        self.assertFalse(an.continue_points([prompt(at(0), "a", project="shop")])[0]["tmp"])

    def test_projects_without_prompts_are_excluded(self):
        self.assertEqual(an.continue_points([ev(at(0), "git", "x", "shop")]), [])
        self.assertEqual(an.continue_points([prompt(at(0), "a", project="")]), [])

    def test_sorted_by_count_descending(self):
        events = [prompt(at(0), "a", "small")] + [prompt(at(i * 50), f"p{i}", "big") for i in range(3)]
        points = an.continue_points(events)
        self.assertEqual([p["project"] for p in points], ["big", "small"])


class ContinueFactsTest(unittest.TestCase):
    def test_anchors_real_projects_with_a_last_instruction(self):
        events = [prompt(at(0), "a", "shop"), prompt(at(50), "b", "blog")]
        facts = an.continue_facts(an.continue_points(events))
        self.assertIn("shop", facts)
        self.assertIn("blog", facts)

    def test_none_when_nothing_to_anchor(self):
        self.assertIsNone(an.continue_facts(an.continue_points([prompt(at(0), "[첨부 파일]", "shop")])))

    def test_tmp_projects_are_excluded_from_anchors(self):
        self.assertIsNone(an.continue_facts(an.continue_points([prompt(at(0), "a", "tmp.xyz")])))


class HeatmapTest(unittest.TestCase):
    def test_shape_and_counts(self):
        days = [DAY + dt.timedelta(days=i) for i in range(7)]
        events = [prompt((9, 5)), prompt((9, 55)), ev((9, 30), "git", "x", "shop"),
                  ev((10, 0), "chrome", "Docs", "github.com"), ev((10, 0), "codex", "w", "shop", actor="agent"),
                  prompt(at(0, DAY + dt.timedelta(days=1))), prompt(at(0, DAY + dt.timedelta(days=30)))]
        grid = an.heatmap(events, days)
        self.assertEqual(list(grid), days)
        self.assertTrue(all(len(row) == 24 for row in grid.values()))
        self.assertEqual(grid[DAY][9], 3)
        self.assertEqual(grid[DAY][10], 0)  # web and agent events are not counted
        self.assertEqual(grid[days[1]][9], 1)
        self.assertEqual(sum(map(sum, grid.values())), 4)  # the day outside the range is dropped


class FactsTest(unittest.TestCase):
    def busy(self):
        """Every category, many projects, 7 distinct long repeated requests: over the cap before trimming."""
        kinds = ["{} 페이지를 처음부터 다시 설계하고 테스트까지 전부 추가해줘", "이거 왜 이렇게 동작하는지 {}",
                 "좋아", "아니 그거 말고 {}", "ValueError: {} not found"]
        events = []
        for i in range(300):
            project = f"project-with-a-long-name-{i % 9}"
            text = kinds[i % 5].format("결제모듈" + chr(0xAC00 + i % 7) * 20)
            events.append(prompt(at(i * 3), text, project))
            events.append(ev(at(i * 3 + 1), "git", "commit", project))
        return events

    def test_contents(self):
        events = [prompt(at(0), "로그인 만들어줘"), prompt(at(10), "로그인 만들어줘"), prompt(at(20), "좋아"),
                  ev(at(30), "chrome", "Docs", "github.com"), ev(at(44), "chrome", "Docs", "github.com"),
                  ev(at(58), "chrome", "Docs", "github.com"), ev(at(60), "git", "feat", "shop")]
        facts = an.facts_for_llm(an.prompt_behavior(events), an.work_rhythm(events))
        self.assertIn("내 지시 3건: 지시 2, 확인·승인 1", facts)
        self.assertIn("30분 내 재시도 1회", facts)
        self.assertIn("연속 활동 구간 1시간 0분(1개, 가장 긴 구간 09:00–10:00 shop)", facts)
        self.assertIn("내 지시 1건당 자동 실행 0건", facts)
        self.assertNotIn("몰입", facts)  # neutral wording: records that run on, not focus
        self.assertNotIn("일부 생략", facts)
        self.assertIn("shop 3→1(60분)", facts)
        self.assertIn('"로그인 만들어줘"×2', facts)

    def test_length_cap(self):
        events = self.busy()
        b, r = an.prompt_behavior(events), an.work_rhythm(events)
        self.assertGreater(len(an.facts_for_llm(b, r, limit=10_000)), an.FACTS_LIMIT)  # the cap really bites
        facts = an.facts_for_llm(b, r)
        self.assertLessEqual(len(facts), an.FACTS_LIMIT)
        self.assertIn("내 지시 300건", facts)
        self.assertIn("연속 활동 구간", facts)  # trimming drops the tail (repeated requests) first
        self.assertTrue(facts.endswith("(일부 생략)"), facts)  # …and says so
        self.assertLessEqual(len(an.facts_for_llm(b, r, limit=80)), 80)

    def test_facts_hide_the_switch_count(self):
        """A real weekly summary turned "전환 73회" into "컨텍스트가 계속 끊김" — so the LLM never sees the count."""
        events = self.busy()
        facts = an.facts_for_llm(an.prompt_behavior(events), an.work_rhythm(events))
        self.assertNotIn("프로젝트 변경", facts)
        self.assertIn("동시 프로젝트", facts)  # the neutral one stays

    def test_no_prompts(self):
        facts = an.facts_for_llm(an.prompt_behavior([]), an.work_rhythm([]))
        self.assertIn("내 지시 없음", facts)
        self.assertIn("내 지시 1건당 자동 실행 계산 불가", facts)


if __name__ == "__main__":
    unittest.main()
