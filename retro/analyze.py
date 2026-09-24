"""How the day went, computed from the log alone (no LLM, no I/O): PLAN-template.md §1 and §4.

Every function takes events as render.load() returns them (dicts with source,
ts (aware datetime), project, text, actor, host), sorted by ts.

  behavior = prompt_behavior(in_slice(events, "am"))
  rhythm = work_rhythm(events)
  facts = facts_for_llm(behavior, rhythm)   # appended to the LLM prompt

Prompt texts are clipped to 600 chars upstream and their whitespace (incl.
newlines) is collapsed to single spaces, so paste detection relies on content
cues, not line counts. All classifications are heuristics: show them as
"자동 분류(추정)".
"""
import datetime as dt
import difflib
import re
from collections import Counter, defaultdict

AI_SOURCES = ("claude", "codex", "claude.ai", "chatgpt")  # same as render.AI_SOURCES
WEB_SOURCES = ("chrome",)  # same as render's "web" count

# ---------------------------------------------------------------- thresholds (PLAN §4 구현 메모)
SHORT_CHARS = 20  # < this: short prompt; <= this: may be an approval
LONG_CHARS = 200  # > this: long prompt
RETRY_WINDOW = dt.timedelta(minutes=30)
RETRY_PREFIX = 40  # normalized first N chars equal -> same request
RETRY_SIMILARITY = 0.8  # or difflib ratio >= this
RETRY_COMPARE = 80  # the ratio looks at this many leading chars (difflib is slow on long pastes)
FOCUS_GAP = dt.timedelta(minutes=15)  # activity closer than this chains into one block
FOCUS_MIN = dt.timedelta(minutes=45)  # blocks at least this long count as focus
CONCURRENT_WINDOW = dt.timedelta(hours=1)
TOP_REPEATED = 5
FACTS_LIMIT = 600
FACTS_OMITTED = "(일부 생략)"  # last line of facts_for_llm when lines were dropped
AGAIN_SHORT = 30  # "다시" alone is a complaint only in a prompt this short (or next to a complaint cue)
PRE_BROWSE = dt.timedelta(minutes=10)  # web visits this soon before a prompt: "작업 직전 탐색"
CONTINUE_GAP = dt.timedelta(minutes=45)  # a project's own gap longer than this starts a new pickup block (PLAN §9.1)
TMP_PREFIX = "tmp."  # projects named like this are scratch folders, shown apart in "🔁 이어가기" (PLAN §9.4)

# ---------------------------------------------------------------- time slices
SLICES = {"all": (0, 24), "am": (0, 12), "pm": (12, 24)}  # [start hour, end hour)
PARTS = (("심야", 0, 6), ("오전", 6, 12), ("오후", 12, 18), ("저녁", 18, 24))


def in_slice(events, name):
    """Events whose local hour falls in SLICES[name]: am = 00:00–11:59, pm = 12:00–23:59."""
    lo, hi = SLICES[name]
    return [e for e in events if lo <= e["ts"].hour < hi]


def part_of_day(ts):
    return next(name for name, lo, hi in PARTS if lo <= ts.hour < hi)


# ---------------------------------------------------------------- who did what
def is_prompt(e):
    """A prompt the user typed to an AI tool ("내 지시")."""
    return e["actor"] == "human" and e["source"] in AI_SOURCES


def is_commit(e):
    return e["source"] == "git"


def is_web(e):
    return e["actor"] == "human" and e["source"] in WEB_SOURCES


def is_activity(e):
    """Signs of the user working: own prompts, commits, web visits."""
    return is_prompt(e) or is_commit(e) or is_web(e)


def minutes(delta):
    return int(delta.total_seconds() // 60)


# ---------------------------------------------------------------- prompt classification
INSTRUCT, QUESTION, APPROVE, FIX, PASTE, OTHER = "지시", "질문", "확인·승인", "수정·불만", "붙여넣기", "기타"
CATEGORIES = (INSTRUCT, QUESTION, APPROVE, FIX, PASTE, OTHER)

TAG_RE = re.compile(r"^\s*\[(음성|첨부|첨부 파일)\]\s*|\s*\[사진 \d+장\]\s*")  # added by collect.py

# terminal output: `user@host dir % cmd`, `user@host:~/dir$ cmd`, `$ git …`, stack traces, error lines.
# The (?<!…) lookbehinds here and below only let a greedy run start where a token starts: same matches, but a long
# run without spaces is scanned once instead of once per character.
PASTE_RE = re.compile(
    r"(?<![\w.-])[\w.-]+@[\w.-]+(:\S*[$#]|\s+\S+\s+[%$#])(\s|$)"
    r"|(^|\s)[$%] (git|npm|npx|pnpm|yarn|node|python3?|pip3?|cd|ls|cat|brew|curl|docker|make|sudo|bash|sh|zsh"
    r"|retro|claude|codex|gh)\b"
    r"|Traceback \(most recent call last\)|File \"[^\"]+\", line \d+"
    r"|\b[A-Z]\w*(Error|Exception)\b:|\bError:|\berror(\[\w+\])?:|npm ERR!|\bfatal:|\bpanic:"
    r"|command not found|No such file or directory|Permission denied")
LOG_MARK_RE = re.compile(  # one or two may be typed; three or more look like a pasted log
    r"\b\d{1,2}:\d{2}:\d{2}\b|\[(INFO|WARN|WARNING|ERROR|DEBUG)\]|\b(INFO|WARN|DEBUG)\b"
    r"|(?<![\w./-])[\w./-]+\.(py|js|ts|tsx|jsx|go|rs|java|rb|sh|swift|kt):\d+|\bat [\w.$<>]+ \(|\bexit (code|status) \d+")
PASTE_LINES = 5  # newlines, when a source keeps them

# "아니"는 "아니면"(if not)·"아니라"(A가 아니라 B, not A but B)일 때는 거부·불만이 아니라 서술적 부정이다.
# "말고"도 "-지 말고"(하지 말고 이어서 해)는 "하지 마" 연결어미이지 "이거 말고 저거"식 거부가 아니다.
FIX_RE = re.compile(
    r"아니(?!면|라)|왜\s*안|틀렸|틀린|틀려|(?<!지)(?<!지\s)말고|잘못|여전히|안\s*(돼|되네|되는데|되잖|됨|된다|먹|나와|보여|뜨)"
    r"|(계속|또)\s*(에러|오류|안)"
    r"|^(no|nope|nah)\b|\b(wrong|incorrect|not working|still (broken|failing|fails))\b"
    r"|\b(doesn'?t|does not|didn'?t|did not) work|\bthat'?s not\b|\bnot what\b", re.I)
# "다시 …" is often a plain request ("로그인 페이지 다시 설계하고 테스트 추가해줘"); with these it is a complaint
AGAIN_RE = re.compile(r"다시")
AGAIN_CUE_RE = re.compile(r"아니(?!면|라)|왜|안\s*돼|틀렸|(?<!지)(?<!지\s)말고|제대로")
APPROVE_RE = re.compile(
    r"^(응|어|네|넵|예|ㅇㅇ|ㅇㅋ|ㄱㄱ|y)(?![가-힣a-z])|오케이|좋아|좋습니다|좋네|좋다|좋음|그래(?![프픽])|머지|계속|재개|진행|고고"
    r"|알겠|맞아|감사|고마워"
    r"|\b(yes|yep|yeah|ok|okay|sure|lgtm|go ahead|continue|proceed|merge|ship it|sounds good|looks good|do it)\b"
    r"|^go\b", re.I)
# verbs that ask for new work; they stop a short text from being a bare approval
WORK_RE = re.compile(
    r"만들|고쳐|고치|추가|수정|삭제|지워|지우|넣어|바꿔|바꾸|변경|작성|구현|정리|적용|실행|돌려|설치|배포|커밋|푸시"
    r"|올려|반영|업데이트|리팩|검토|생성|제거|옮겨|이동|분석|조사|찾아|알려|설명|요약|번역|그려|열어|확인해|테스트해"
    r"|빌드|띄워|붙여|연결|설정|세팅|보내|완료해"  # "완료해"만 (완료됐어 같은 상황 공유는 지시가 아니다)
    r"|\b(fix|add|make|create|implement|update|remove|delete|write|run|build|refactor|rename|change|move|deploy"
    r"|commit|push|install|generate|convert|replace|check|review|explain|summarize|translate|show|find|open"
    r"|set ?up|clean ?up)\b", re.I)
REQUEST_RE = re.compile(r"해\s?줘|줘요?|주세요|해\s?봐|해\s?놔|하자|합시다|해라|하세요|부탁|진행|\b(please|let'?s)\b", re.I)
POLITE_RE = re.compile(r"줄래|줄\s?수|주실|주시겠|주겠|\b(can|could|would|will) you\b|\bplease\b", re.I)
QUESTION_END_RE = re.compile(r"[?？][!.\s]*$")
QUESTION_START_RE = re.compile(r"^(how|why|what|where|when|which|who|whose|is|are|was|were|does|did|should)\b", re.I)
QUESTION_WORD_RE = re.compile(
    r"어떻게|어때|왜|뭐|무엇|무슨|어디|언제|누가|누구|어느|몇|얼마|어떤|(까요?|나요|니|냐|는지|인가요?|던가)$"
    r"|\b(how|why|what|where|when|which|who)\b", re.I)


def clean(text):
    """Prompt text without collect.py's [음성]/[첨부] tags."""
    return TAG_RE.sub("", text or "").strip()


def is_paste(text):
    return bool(PASTE_RE.search(text)) or text.count("\n") >= PASTE_LINES or len(LOG_MARK_RE.findall(text)) >= 3


def is_fix(text):
    """수정·불만 cues; "다시" counts only in a short prompt or next to 아니·왜·안 돼·틀렸·말고·제대로."""
    if FIX_RE.search(text):
        return True
    return bool(AGAIN_RE.search(text)) and (len(text) <= AGAIN_SHORT or bool(AGAIN_CUE_RE.search(text)))


def classify_prompt(text):
    """One of CATEGORIES. First match wins:

    1. 붙여넣기  terminal prompt / stack trace / error line / 3+ log marks / 5+ lines
    2. 수정·불만  아니·왜 안·틀렸·말고·안 돼 …, no/wrong/doesn't work (before approval: "아니 계속" is a fix);
                  "다시" only when the prompt is <= 30 chars or has one of those cues (see is_fix)
                  — "아니면"/"아니라"(A가 아니라 B)와 "-지 말고"(하지 말고 이어서)는 거부가 아니라 서술적 표현이라 제외
    3. 확인·승인  <= 20 chars, an approval cue (응·ㅇㅇ·좋아·좋음·머지·계속·재개·진행·ok·yes …), no work verb, no trailing "?"
                  — so "PR 머지해줘" / "진행해줘" are approvals, "좋아 버튼 색 바꿔줘" is an instruction
    4. 질문       ends with "?" or starts with an English wh-/aux word
                  — unless it is a polite request ("해줄래?", "can you …?"), which is 지시
    5. 지시       a work verb (만들·고쳐·추가 …, fix/add …) or a request ending (해줘·해놔·주세요·하자·진행, please)
    6. 질문       a question word or ending anywhere (어떻게·왜·뭐 …, ~까/~나요, how/why/what)
    7. 기타
    """
    t = clean(text)
    if not t:
        return OTHER
    if is_paste(t):
        return PASTE
    if is_fix(t):
        return FIX
    asks = bool(QUESTION_END_RE.search(t))
    if len(t) <= SHORT_CHARS and APPROVE_RE.search(t) and not WORK_RE.search(t) and not asks:
        return APPROVE
    if asks or QUESTION_START_RE.search(t):
        return INSTRUCT if POLITE_RE.search(t) else QUESTION
    if WORK_RE.search(t) or REQUEST_RE.search(t):
        return INSTRUCT
    if QUESTION_WORD_RE.search(t):
        return QUESTION
    return OTHER


# ---------------------------------------------------------------- prompt behavior
def retry_key(text):
    """Letters and digits only, lowercased: what two retries of one request share."""
    return re.sub(r"\W+", "", clean(text).lower())


def near_same(a, b):
    """Near-identical retry keys: same first RETRY_PREFIX chars, or a difflib ratio >= RETRY_SIMILARITY
    over the first RETRY_COMPARE chars."""
    if a[:RETRY_PREFIX] == b[:RETRY_PREFIX]:
        return True
    sm = difflib.SequenceMatcher(None, a[:RETRY_COMPARE], b[:RETRY_COMPARE], autojunk=False)
    return sm.real_quick_ratio() >= RETRY_SIMILARITY and sm.quick_ratio() >= RETRY_SIMILARITY \
        and sm.ratio() >= RETRY_SIMILARITY


def retry_group(e):
    """Only prompts to the same tool, on the same machine, in the same project can retry each other."""
    return e["source"], e.get("host", ""), e["project"]


def find_retries(prompts, kinds):
    """Prompts that repeat an earlier one (near-identical, same retry_group) within RETRY_WINDOW.

    Approvals ("계속", "ㅇㅇ") are left out: repeating them is not being stuck.
    """
    out, seen = [], []  # seen: (ts, group, key) of earlier candidates
    for e, kind in zip(prompts, kinds):
        key = retry_key(e["text"])
        if kind == APPROVE or len(key) < 2:
            continue
        group = retry_group(e)
        seen = [x for x in seen if e["ts"] - x[0] <= RETRY_WINDOW]
        prev = next((ts for ts, g, k in seen if g == group and near_same(key, k)), None)
        if prev is not None:
            out.append({"ts": e["ts"], "prev_ts": prev, "project": e["project"], "text": clean(e["text"])[:80]})
        seen.append((e["ts"], group, key))
    return out


def complaint_streaks(prompts, kinds):
    """Runs of 2+ consecutive 수정·불만 prompts, no more than RETRY_WINDOW apart.

    Without the time limit a real day chained 20:29 to 08:01 the next morning (11.5 hours) into one
    "streak" — consecutive in order, but nothing a person would call one stretch of frustration.
    """
    runs, cur = [], []

    def close():
        if len(cur) >= 2:
            runs.append({"start": cur[0]["ts"], "end": cur[-1]["ts"], "count": len(cur), "project": cur[0]["project"]})
        del cur[:]

    for e, kind in zip(prompts + [None], kinds + [None]):
        if kind == FIX:
            if cur and e["ts"] - cur[-1]["ts"] > RETRY_WINDOW:
                close()
            cur.append(e)
            continue
        close()
    return runs


URL_RE = re.compile(r"https?://\S+|www\.\S+")
PATH_RE = re.compile(r"(?<!\S)\S*[/\\]\S*|(?<![\w-])[\w-]+\.[a-z]{1,5}\b")  # tokens with a slash (paths, ~/x), file names
PUNCT_NUM_RE = re.compile(r"[^\w\s]|\d|_")


def request_key(text):
    """Repeated-request key: lowercase, without URLs, paths, file names, numbers, punctuation, extra spaces."""
    t = clean(text).lower()
    for rx in (URL_RE, PATH_RE, PUNCT_NUM_RE):
        t = rx.sub(" ", t)
    return " ".join(t.split())


def repeated_groups(prompts, kinds, n=TOP_REPEATED):
    """[{count, examples}] for requests seen 2+ times ("유사 표현 후보"), most frequent first.

    examples: up to 2 distinct original texts (80 chars) in the order they came. Approvals are left out.
    """
    counts, examples = Counter(), {}
    for e, kind in zip(prompts, kinds):
        key = request_key(e["text"])
        if kind == APPROVE or not key:
            continue
        counts[key] += 1
        ex, text = examples.setdefault(key, []), clean(e["text"])[:80]
        if len(ex) < 2 and text not in ex:
            ex.append(text)
    return [{"count": c, "examples": examples[k]} for k, c in counts.most_common() if c >= 2][:n]


def prompt_behavior(events):
    """PLAN §4 prompt metrics over the user's own AI prompts in events.

    Keys: count, types {category: n} (all CATEGORIES, in order), length_median,
    short_ratio (< 20 chars), normal_ratio, long_ratio (> 200 chars), retries (n),
    retry_list [{ts, prev_ts, project, text}], complaint_streaks [{start, end, count,
    project}], max_complaint_streak, paste_ratio, repeated [{count, examples}],
    top_repeated [(first example, count)],
    by_slice {am, pm}, by_part {심야, 오전, 오후, 저녁}, first, last (datetime | None).
    Ratios are 0–1, rounded to 3 places; 0.0 when there are no prompts.
    """
    prompts = [e for e in events if is_prompt(e)]
    kinds = [classify_prompt(e["text"]) for e in prompts]
    lengths = sorted(len(clean(e["text"])) for e in prompts)
    n = len(prompts)

    def ratio(k):
        return round(k / n, 3) if n else 0.0

    short = sum(1 for x in lengths if x < SHORT_CHARS)
    long_ = sum(1 for x in lengths if x > LONG_CHARS)
    types = Counter(kinds)
    retries = find_retries(prompts, kinds)
    streaks = complaint_streaks(prompts, kinds)
    groups = repeated_groups(prompts, kinds)
    return {
        "count": n,
        "types": {c: types[c] for c in CATEGORIES},
        "length_median": median(lengths),
        "short_ratio": ratio(short), "normal_ratio": ratio(n - short - long_), "long_ratio": ratio(long_),
        "retries": len(retries), "retry_list": retries,
        "complaint_streaks": streaks,
        "max_complaint_streak": max((s["count"] for s in streaks), default=0),
        "paste_ratio": ratio(types[PASTE]),
        "repeated": groups,
        "top_repeated": [(g["examples"][0], g["count"]) for g in groups],
        "by_slice": {s: len(in_slice(prompts, s)) for s in ("am", "pm")},
        "by_part": {p: sum(1 for e in prompts if part_of_day(e["ts"]) == p) for p, _, _ in PARTS},
        "first": prompts[0]["ts"] if prompts else None,
        "last": prompts[-1]["ts"] if prompts else None,
    }


def median(xs):
    """Median of a sorted list (0 when empty); a whole number when it is one."""
    if not xs:
        return 0
    mid = len(xs) // 2
    m = xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2
    return int(m) if m == int(m) else m


# ---------------------------------------------------------------- 🔁 이어가기 (PLAN §9.1 + §9.4)
def is_tmp_project(project):
    """A scratch folder (e.g. "tmp.k12UodOzwB"), shown apart from real projects in "🔁 이어가기"."""
    return (project or "").startswith(TMP_PREFIX)


def last_instruction(prompts):
    """The most recent prompt in prompts with content once collect.py's tags are stripped (PLAN §9.4).

    A prompt that is only "[첨부 파일]"/"[음성]"/"[사진 n장]" has nothing to show, so it is skipped in
    favor of the content-bearing prompt before it. None when every prompt here is tag-only (or there are none).
    """
    for e in reversed(prompts):
        if clean(e["text"]):
            return e
    return None


def continue_points(events):
    """Where each project was last picked up today, for "🔁 이어가기" (PLAN §9.1 + §9.4).

    events: one day's events (or a slice), sorted by ts. Only the user's own AI prompts ("내 지시")
    build the segments and count — a project with none is left out.

    Returns a list of {project, tmp (bool, see is_tmp_project), segments [{start, end}] (chains of
    prompts with no gap over CONTINUE_GAP; a lone prompt is its own segment with start == end),
    count (prompts), last ({ts, text (<=200 chars, tags stripped)} | None, see last_instruction),
    commits ([{ts, text}], strictly after last's ts, same project; empty when last is None or there
    are none — PLAN §9.4: no commits is not shown as a fact, so an empty list, never a placeholder)},
    most active project first (ties broken by name).
    """
    by_project, commits_by_project = defaultdict(list), defaultdict(list)
    for e in events:
        if not e["project"]:
            continue
        if is_prompt(e):
            by_project[e["project"]].append(e)
        elif is_commit(e):
            commits_by_project[e["project"]].append(e)

    out = []
    for project, prompts in by_project.items():
        segs, cur = [], []
        for e in prompts:
            if cur and e["ts"] - cur[-1]["ts"] > CONTINUE_GAP:
                segs.append(cur)
                cur = []
            cur.append(e)
        if cur:
            segs.append(cur)
        segments = [{"start": c[0]["ts"], "end": c[-1]["ts"]} for c in segs]
        last = last_instruction(prompts)
        commits = [{"ts": c["ts"], "text": c["text"]} for c in commits_by_project.get(project, [])
                   if last and c["ts"] > last["ts"]]
        out.append({
            "project": project,
            "tmp": is_tmp_project(project),
            "segments": segments,
            "count": len(prompts),
            "last": {"ts": last["ts"], "text": clean(last["text"])[:200]} if last else None,
            "commits": commits,
        })
    out.sort(key=lambda p: (-p["count"], p["project"]))
    return out


def continue_facts(points, limit=4):
    """Per-project last-instruction anchors for continue_next's evidence (real projects only).

    None when there is nothing to anchor (no project has a content-bearing last instruction).
    """
    real = [p for p in points if not p["tmp"] and p["last"]]
    if not real:
        return None
    items = ", ".join(f"{p['project'][:20]} {p['last']['ts']:%H:%M}" for p in real[:limit])
    return f"프로젝트별 마지막 내 지시 시각(첨부·음성만 있는 지시는 제외): {items}"


# ---------------------------------------------------------------- work rhythm
def top_project(events):
    """Most frequent project among prompts/commits; web hosts only when there is nothing else."""
    work = Counter(e["project"] for e in events if not is_web(e) and e["project"])
    web = Counter(e["project"] for e in events if is_web(e) and e["project"])
    best = (work or web).most_common(1)
    return best[0][0] if best else ""


def focus_blocks(activity):
    """Chains of activity with gaps < FOCUS_GAP that last >= FOCUS_MIN and hold a prompt or a commit.

    A chain of web visits alone is browsing, not focused work.
    """
    chains, cur = [], []
    for e in activity:
        if cur and e["ts"] - cur[-1]["ts"] >= FOCUS_GAP:
            chains.append(cur)
            cur = []
        cur.append(e)
    if cur:
        chains.append(cur)
    return [{
        "start": c[0]["ts"], "end": c[-1]["ts"], "minutes": minutes(c[-1]["ts"] - c[0]["ts"]),
        "project": top_project(c),
        "prompts": sum(map(is_prompt, c)), "commits": sum(map(is_commit, c)), "web": sum(map(is_web, c)),
    } for c in chains if c[-1]["ts"] - c[0]["ts"] >= FOCUS_MIN and any(not is_web(e) for e in c)]


def project_segments(work):
    """Runs of consecutive work events on one project (events without a project are skipped).

    len(segments) - 1 is the number of context switches.
    """
    segs = []
    for e in work:
        if not e["project"]:
            continue
        if segs and segs[-1]["project"] == e["project"]:
            segs[-1]["end"] = e["ts"]
            segs[-1]["events"] += 1
        else:
            segs.append({"project": e["project"], "start": e["ts"], "end": e["ts"], "events": 1})
    return segs


def max_concurrent(work):
    """(most distinct projects inside any CONCURRENT_WINDOW, window start) — two pointers."""
    work = [e for e in work if e["project"]]
    best, at, left, inside = 0, None, 0, Counter()
    for e in work:
        inside[e["project"]] += 1
        while e["ts"] - work[left]["ts"] >= CONCURRENT_WINDOW:
            p = work[left]["project"]
            inside[p] -= 1
            if not inside[p]:
                del inside[p]
            left += 1
        if len(inside) > best:
            best, at = len(inside), work[left]["ts"]
    return best, at


def prompt_to_commit(work):
    """{project: {prompts, commits, first_prompt, first_commit, minutes}}, busiest project first.

    first_commit is the first commit at or after the first prompt; minutes is None without one.
    """
    out = {}
    for e in work:
        if not e["project"]:
            continue
        p = out.setdefault(e["project"], {"prompts": 0, "commits": 0, "first_prompt": None, "first_commit": None,
                                          "minutes": None})
        if is_prompt(e):
            p["prompts"] += 1
            p["first_prompt"] = p["first_prompt"] or e["ts"]
            continue
        p["commits"] += 1
        if p["first_prompt"] and not p["first_commit"]:
            p["first_commit"] = e["ts"]
            p["minutes"] = minutes(e["ts"] - p["first_prompt"])
    return dict(sorted(out.items(), key=lambda kv: -(kv[1]["prompts"] + kv[1]["commits"])))


def work_rhythm(events):
    """PLAN §4 rhythm metrics.

    Keys: focus_minutes, focus_blocks [{start, end, minutes, project, prompts, commits,
    web}], longest (a block | None), switches, segments [{project, start, end, events}],
    max_concurrent, max_concurrent_at, prompts, agent, leverage (agent / prompts, 2
    places, None without prompts), prompt_commit (see prompt_to_commit).
    """
    activity = [e for e in events if is_activity(e)]
    work = [e for e in activity if not is_web(e)]  # prompts + commits
    blocks = focus_blocks(activity)
    segs = project_segments(work)
    conc, conc_at = max_concurrent(work)
    prompts = sum(map(is_prompt, events))
    agent = sum(1 for e in events if e["actor"] == "agent")
    return {
        "focus_minutes": sum(b["minutes"] for b in blocks),
        "focus_blocks": blocks,
        "longest": max(blocks, key=lambda b: b["minutes"], default=None),
        "switches": max(len(segs) - 1, 0),
        "segments": segs,
        "max_concurrent": conc, "max_concurrent_at": conc_at,
        "prompts": prompts, "agent": agent,
        "leverage": round(agent / prompts, 2) if prompts else None,
        "prompt_commit": prompt_to_commit(work),
    }


def heatmap(events, days):
    """{date: [24 counts]} of the user's prompts + commits per hour, for each of days."""
    grid = {d: [0] * 24 for d in days}
    for e in events:
        d = e["ts"].date()
        if d in grid and (is_prompt(e) or is_commit(e)):
            grid[d][e["ts"].hour] += 1
    return grid


def browse_before_prompts(events, window=PRE_BROWSE, top=6):
    """Web visits within window before one of the user's prompts ("작업 직전 탐색").

    Keys: visits (n), web (all web visits), sites [(site, n)] most visited first.
    """
    prompt_ts = [e["ts"] for e in events if is_prompt(e)]
    web = [e for e in events if is_web(e)]
    hits, i = [], 0
    for e in web:  # both lists are sorted by ts: walk to the first prompt after this visit
        while i < len(prompt_ts) and prompt_ts[i] <= e["ts"]:
            i += 1
        if i < len(prompt_ts) and prompt_ts[i] - e["ts"] <= window:
            hits.append(e)
    sites = Counter(e["project"] for e in hits if e["project"])
    return {"visits": len(hits), "web": len(web), "sites": sites.most_common(top)}


# ---------------------------------------------------------------- facts for the LLM
def hm(minutes_):
    h, m = divmod(minutes_, 60)
    return f"{h}시간 {m}분" if h else f"{m}분"


def facts_for_llm(behavior, rhythm, limit=FACTS_LIMIT):
    """Key numbers as a short Korean block, so the LLM interprets instead of recounting.

    Lines are dropped from the end (repeated requests first) to stay within limit chars,
    and then the block ends with FACTS_OMITTED. Wording is neutral: blocks of continuous
    records, not "focus".
    """
    b, r = behavior, rhythm
    lines = ["[자동 계산 지표 — 다시 세지 말고 해석만]"]
    if b["count"]:
        lines += [
            f"내 지시 {b['count']}건: " + ", ".join(f"{k} {v}" for k, v in b["types"].items() if v),
            f"길이 중앙값 {b['length_median']}자, 짧음(<{SHORT_CHARS}자) {b['short_ratio']:.0%}, "
            f"김(>{LONG_CHARS}자) {b['long_ratio']:.0%}, 붙여넣기 {b['paste_ratio']:.0%}",
            f"30분 내 재시도 {b['retries']}회, 연속 수정·불만 최대 {b['max_complaint_streak']}회",
            "시간대: " + " ".join(f"{k} {v}" for k, v in b["by_part"].items()),
        ]
    else:
        lines.append("내 지시 없음")
    focus = f"연속 활동 구간 {hm(r['focus_minutes'])}({len(r['focus_blocks'])}개"
    if r["longest"]:
        lg = r["longest"]
        focus += f", 가장 긴 구간 {lg['start']:%H:%M}–{lg['end']:%H:%M} {lg['project']}".rstrip()
    lev = "계산 불가" if r["leverage"] is None else f"{r['leverage']:g}건"
    # The switch count is deliberately NOT sent to the LLM. A real weekly summary turned "73 switches" into
    # "컨텍스트가 계속 끊김" — but with agents running in parallel, A→B→A is the normal shape of the day
    # (66 switches in one real day), so there is nothing to interpret. The page shows it as a plain number.
    lines += [focus + ")",
              f"1시간 내 동시 프로젝트 최대 {r['max_concurrent']}개, 내 지시 1건당 자동 실행 {lev}"]
    pc = [f"{p[:20]} {v['prompts']}→{v['commits']}" + (f"({v['minutes']}분)" if v["minutes"] is not None else "")
          for p, v in list(r["prompt_commit"].items())[:4]]
    if pc:
        lines.append("지시→커밋(첫 커밋까지): " + ", ".join(pc))
    if b["top_repeated"]:
        lines.append("반복 요청: " + ", ".join(f"\"{t[:30]}\"×{c}" for t, c in b["top_repeated"]))
    if len("\n".join(lines)) > limit:
        while len(lines) > 2 and len("\n".join(lines + [FACTS_OMITTED])) > limit:
            lines.pop()
        lines.append(FACTS_OMITTED)
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit - 1] + "…"
