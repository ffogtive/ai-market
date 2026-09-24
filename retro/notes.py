"""My own retrospective notes: KPT, 오늘 가장 의미 있었던 일, 내일 첫 할 일, and the next day's check.

  ~/Retro/notes-2026-09-23.json          a day   (notes-weekly-2026-09-21.json: a week, by its Monday)
  {"kpt": {"keep": "", "problem": "", "try": ""}, "reflection": "", "first_task": "",
   "followup": {"first_task": "완료", "try": "이어가기"}, "updated": "2026-09-23T22:10:00"}

followup: how this day's first task and Try went, checked on a later day (완료 / 이어가기 / 취소).
When I never saved a first task for a day, its summary's AI draft (first_task_tomorrow in
summary-daily-DAY.json beside the notes) is checked instead, marked as AI 제안.
Only `retro app` writes these files (atomically); render.py reads them. They are never put in an
LLM prompt. Standard library only.
"""
import datetime as dt
import json
import os
import tempfile
import threading

FIELDS = ("keep", "problem", "try")
STATUSES = ("완료", "이어가기", "취소")
UNCHECKED = "아직 확인 안 함"
ITEMS = {"first_task": "첫 할 일", "try": "Try"}  # what a later day checks
LOOKBACK = 7  # a day's check looks this many days back for the last notes with a first task or Try
MAX_CHARS = 2000  # per field

_lock = threading.Lock()  # the app is threaded: one read-modify-write at a time


def path(notes_dir, kind, day):
    return os.path.join(notes_dir, f"notes-{day}.json" if kind == "daily" else f"notes-weekly-{day}.json")


def clean(text, limit=MAX_CHARS):
    """Form text as stored: \\r\\n → \\n, no control characters, trimmed, at most limit characters."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c == "\n" or c == "\t" or c.isprintable())
    return text.strip()[:limit]


def load(notes_dir, kind, day):
    """The saved notes, or {} (no file, unreadable, or not a JSON object)."""
    if not notes_dir:
        return {}
    try:
        with open(path(notes_dir, kind, day), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def kpt(notes):
    k = notes.get("kpt")
    return {f: str(k.get(f) or "") for f in FIELDS} if isinstance(k, dict) else {f: "" for f in FIELDS}


def text(notes, item):
    """This day's first task or Try as I wrote it ("" when not written)."""
    return kpt(notes)["try"] if item == "try" else str(notes.get("first_task") or "")


def ai_first_task(notes_dir, day):
    """The day's summary draft of tomorrow's first task (summary-daily-DAY.json beside the notes), or ""."""
    if not notes_dir:
        return ""
    try:
        with open(os.path.join(notes_dir, f"summary-daily-{day}.json"), encoding="utf-8") as f:
            s = json.load(f).get("summary")
    except (OSError, ValueError, AttributeError):
        return ""
    return clean(s.get("first_task_tomorrow"), 500) if isinstance(s, dict) else ""


def with_ai_draft(notes_dir, day, notes):
    """notes plus the AI draft as the first task when I never saved one (a saved empty one stays empty)."""
    if "first_task" in notes:
        return notes
    draft = ai_first_task(notes_dir, day)
    return dict(notes, first_task=draft, first_task_ai=True) if draft else notes


def is_ai(notes, item):
    """True when this item is the summary's draft, not something I saved."""
    return item == "first_task" and bool(notes.get("first_task_ai"))


def status(notes, item):
    s = (notes.get("followup") or {}).get(item) if isinstance(notes.get("followup"), dict) else None
    return s if s in STATUSES else ""


def has_any(notes):
    return any(kpt(notes).values()) or bool(notes.get("reflection")) or bool(notes.get("first_task"))


def write_json(file, data):
    """Temp file in the same folder, then os.replace: a crash never leaves half a file, readers see old or new."""
    folder = os.path.dirname(file) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(file) + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, file)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _update(notes_dir, kind, day, change):
    with _lock:
        data = load(notes_dir, kind, day)
        change(data)
        data["updated"] = dt.datetime.now().isoformat(timespec="seconds")
        write_json(path(notes_dir, kind, day), data)
        return data


def save(notes_dir, kind, day, keep="", problem="", try_="", reflection="", first_task=None):
    """Replace my text for that day or week; the checks made on later days (followup) are kept.

    first_task: None leaves it out (weekly notes have none).
    """
    def change(data):
        data["kpt"] = {"keep": clean(keep), "problem": clean(problem), "try": clean(try_)}
        data["reflection"] = clean(reflection)
        if first_task is not None:
            data["first_task"] = clean(first_task, 500)
    return _update(notes_dir, kind, day, change)


def set_status(notes_dir, day, item, value):
    """Record how day's first task / Try went (value in STATUSES)."""
    if item not in ITEMS or value not in STATUSES:
        raise ValueError(f"{item}: {value}")

    def change(data):
        followup = data.get("followup") if isinstance(data.get("followup"), dict) else {}
        followup[item] = value
        data["followup"] = followup
    return _update(notes_dir, "daily", day, change)


def previous(notes_dir, day):
    """(date, notes) of the last day before day (up to LOOKBACK days) with a first task or Try, else None.

    A day I wrote no first task for still counts through its AI draft (see with_ai_draft).
    """
    for i in range(1, LOOKBACK + 1):
        d = day - dt.timedelta(days=i)
        n = with_ai_draft(notes_dir, d, load(notes_dir, "daily", d))
        if text(n, "first_task") or text(n, "try"):
            return d, n
    return None


def carried(notes_dir, day):
    """{item: text} of the previous day's items marked 이어가기 — the defaults for day's own first task / Try."""
    prev = previous(notes_dir, day)
    if not prev:
        return {}
    return {item: text(prev[1], item) for item in ITEMS if status(prev[1], item) == "이어가기" and text(prev[1], item)}


def week_tasks(notes_dir, days):
    """[(day, first task, status or "", from AI)] for the days with a first task (mine, or else the AI draft)."""
    out = []
    for d in days:
        n = with_ai_draft(notes_dir, d, load(notes_dir, "daily", d))
        if text(n, "first_task"):
            out.append((d, text(n, "first_task"), status(n, "first_task"), is_ai(n, "first_task")))
    return out
