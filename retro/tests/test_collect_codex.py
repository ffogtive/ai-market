"""Regression tests for the Codex CLI parser in retro/collect.py.

Fixtures mirror the rollout format in openai/codex (codex-rs/history, codex-rs/rollout):
one {timestamp, [ordinal], type, payload} record per line under
~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl.

Run: python3 -m unittest discover retro/tests
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collect  # noqa: E402

UTC = dt.timezone.utc
BASE = (dt.datetime.now(UTC) - dt.timedelta(days=1)).replace(hour=3, minute=0, second=0, microsecond=0)

AGENTS_MD = "# AGENTS.md instructions for /work/shop\n\n<INSTRUCTIONS>\nUse tabs.\n</INSTRUCTIONS>"
ENV_CTX = "<environment_context>\n  <cwd>/work/shop</cwd>\n  <shell>zsh</shell>\n</environment_context>"


def iso(minutes, seconds=0):
    t = BASE + dt.timedelta(minutes=minutes, seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


def rec(minutes, kind, payload, ordinal=None, seconds=0):
    line = {"timestamp": iso(minutes, seconds), "type": kind, "payload": payload}
    if ordinal is not None:
        line["ordinal"] = ordinal
    return line


def meta(cwd, source="vscode", **extra):
    m = {"session_id": "t1", "id": "t1", "timestamp": iso(0), "cwd": cwd, "originator": "codex_vscode",
         "cli_version": "0.200.0", "source": source, "model_provider": "openai",
         "base_instructions": {"text": "You are Codex, a coding agent."}}
    m.update(extra)
    return m


def user_item(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def paginated_user(text):
    return {"type": "item_completed", "thread_id": "t1", "turn_id": "turn-1", "completed_at_ms": 0,
            "item": {"type": "UserMessage", "id": "u1",
                     "content": [{"type": "text", "text": text, "text_elements": []}]}}


def legacy_user(text):
    return {"type": "user_message", "message": text, "images": []}


class HomeTestCase(unittest.TestCase):
    """Each test gets an empty temp HOME."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.since = BASE - dt.timedelta(days=2)
        self.until = dt.datetime.now(UTC) + dt.timedelta(minutes=1)
        self.day = os.path.join(self.home, ".codex", "sessions", BASE.strftime("%Y"), BASE.strftime("%m"),
                                BASE.strftime("%d"))

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        shutil.rmtree(self.home)

    def write(self, name, records, folder=None):
        folder = folder or self.day
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return path



class CodexTest(HomeTestCase):
    def collect(self):
        return sorted(collect.collect_codex(self.since, self.until), key=lambda e: e["ts"])

    # -------------------------------------------------------------- formats
    def test_paginated_format(self):
        """Current app-server threads: no event_msg/user_message, prompts are item_completed/UserMessage."""
        self.write("rollout-a-paginated.jsonl", [
            rec(0, "session_meta", meta("/work/shop", history_mode="paginated"), 0),
            rec(0, "response_item", {"type": "message", "role": "developer",
                                     "content": [{"type": "input_text", "text": "<permissions instructions>"}]}, 1),
            rec(0, "response_item", user_item(AGENTS_MD), 2),
            rec(0, "response_item", user_item(ENV_CTX), 3),
            rec(0, "turn_context", {"cwd": "/work/shop", "model": "gpt-5-codex"}, 4),
            rec(0, "event_msg", {"type": "turn_started", "turn_id": "turn-1"}, 5),
            rec(0, "response_item", user_item("fix the checkout bug"), 6),
            rec(0, "event_msg", paginated_user("fix the checkout bug"), 7),
            rec(1, "response_item", {"type": "message", "role": "assistant",
                                     "content": [{"type": "output_text", "text": "Done."}]}, 8),
            rec(5, "response_item", user_item("# Context from my IDE setup:\n\n## Active file: a.py\n\n"
                                              "## My request for Codex:\nadd tests"), 9),
            rec(5, "event_msg", paginated_user("# Context from my IDE setup:\n\n## Active file: a.py\n\n"
                                               "## My request for Codex:\nadd tests"), 10),
        ])
        got = self.collect()
        self.assertEqual([e["text"] for e in got], ["fix the checkout bug", "add tests"])
        self.assertEqual({e["project"] for e in got}, {"shop"})
        self.assertEqual({e["actor"] for e in got}, {"human"})
        self.assertEqual(got[0]["ts"], collect.parse_ts(iso(0)))

    def test_legacy_format(self):
        """Envelope format with event_msg/user_message next to the response_item copy."""
        self.write("rollout-b-legacy.jsonl", [
            rec(10, "session_meta", meta("/work/blog", source="cli")),
            rec(10, "response_item", user_item("<user_instructions>\nbe brief\n</user_instructions>")),
            rec(10, "response_item", user_item(ENV_CTX)),
            rec(11, "response_item", user_item("write the release notes")),
            rec(11, "event_msg", legacy_user("write the release notes")),
            rec(12, "event_msg", {"type": "agent_message", "message": "ok"}),
        ])
        got = self.collect()
        self.assertEqual([(e["text"], e["project"]) for e in got], [("write the release notes", "blog")])

    def test_oldest_bare_format(self):
        """Early 2025 files: no envelope, only the first line has a timestamp."""
        self.write("rollout-c-bare.jsonl", [
            {"id": "t0", "timestamp": iso(20), "instructions": None},
            {"record_type": "state"},
            user_item(ENV_CTX),
            user_item("rename the module"),
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]},
        ])
        got = self.collect()
        self.assertEqual([e["text"] for e in got], ["rename the module"])
        self.assertEqual(got[0]["ts"], collect.parse_ts(iso(20)))

    # -------------------------------------------------------------- noise / dedupe / actor
    def test_injected_context_is_noise(self):
        """With no user events, the response_item fallback must still drop injected context."""
        self.write("rollout-d-noise.jsonl", [
            rec(30, "session_meta", meta("/work/shop")),
            rec(30, "response_item", user_item(AGENTS_MD)),
            rec(30, "response_item", user_item(ENV_CTX)),
            rec(30, "response_item", user_item("<user_shell_command>ls</user_shell_command>")),
            rec(31, "response_item", user_item("real prompt")),
        ])
        self.assertEqual([e["text"] for e in self.collect()], ["real prompt"])

    def test_app_thread_without_user_events(self):
        """Codex app threads seen on the user's Mac: user text only in response_item/message/user,
        next to agent_message / inter-agent / tool records that are not prompts."""
        self.write("rollout-l-app.jsonl", [
            rec(100, "session_meta", meta("/work/shop", originator="Codex Desktop", history_mode="paginated"), 0),
            rec(100, "response_item", {"type": "message", "role": "developer",
                                       "content": [{"type": "input_text", "text": "<permissions instructions>"}]}, 1),
            rec(100, "world_state", {"full": True, "state": {"environments": []}}, 2),
            rec(100, "turn_context", {"cwd": "/work/shop"}, 3),
            rec(100, "event_msg", {"type": "task_started", "turn_id": "turn-1"}, 4),
            # injected blocks and the typed text in one message, tagged per block
            rec(100, "response_item", dict(user_item(AGENTS_MD), content=[
                {"type": "input_text", "text": AGENTS_MD}, {"type": "input_text", "text": ENV_CTX}],
                internal_chat_message_metadata_passthrough={
                    "content_item_kinds": ["agents_md.instructions", "environment.context"]}), 5),
            rec(101, "response_item", dict(user_item("ship the pricing page"),
                                           internal_chat_message_metadata_passthrough={
                                               "content_item_kinds": ["user.text"]}), 6),
            rec(101, "response_item", {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Also check mobile"},
                {"type": "input_text", "text": "untagged injected block"}],
                "internal_chat_message_metadata_passthrough": {
                    "content_item_kinds": ["user.text", "additional_context.x"]}}, 7),
            rec(102, "response_item", {"type": "agent_message", "author": "/root/worker", "recipient": "/root",
                                       "content": [{"type": "input_text", "text": "worker finished"}]}, 8),
            rec(102, "inter_agent_communication_metadata", {"trigger_turn": True}, 9),
            rec(102, "inter_agent_communication", {"author": "/root", "recipient": "/root/worker",
                                                   "content": "do the css", "trigger_turn": True}, 10),
            rec(102, "response_item", {"type": "custom_tool_call", "name": "apply_patch", "input": "x"}, 11),
            rec(102, "response_item", {"type": "function_call", "name": "shell", "arguments": "{}"}, 12),
            rec(102, "response_item", user_item("<subagent_notification>done</subagent_notification>"), 13),
            rec(103, "response_item", {"type": "message", "role": "assistant",
                                       "content": [{"type": "output_text", "text": "Done."}]}, 14),
            rec(103, "token_usage_record", {"total": 1}, 15),
            rec(103, "event_msg", {"type": "task_complete"}, 16),
        ])
        got = self.collect()
        self.assertEqual([(e["text"], e["project"], e["actor"]) for e in got],
                         [("ship the pricing page", "shop", "human"), ("Also check mobile", "shop", "human")])

    def test_inherited_user_message_is_skipped(self):
        self.write("rollout-m.jsonl", [
            rec(110, "session_meta", meta("/work/shop")),
            dict(rec(110, "response_item", user_item("parent context copy")), metadata={"inherited_user_message": True}),
            rec(111, "response_item", user_item("own prompt")),
        ])
        self.assertEqual([e["text"] for e in self.collect()], ["own prompt"])

    def test_voice_transcript(self):
        """Realtime (voice) turns: the user's words are realtime_item/transcript_segment; the agent
        gets a <realtime_delegation> handoff, which is injected context."""
        handoff = "<realtime_delegation>\n  <input>refactor the cart</input>\n</realtime_delegation>"
        self.write("rollout-n-voice.jsonl", [
            rec(120, "session_meta", meta("/work/shop", history_mode="paginated"), 0),
            rec(120, "realtime_item", {"id": "r1", "realtime_session_id": "s1", "type": "realtime_session_started"}, 1),
            rec(120, "realtime_item", {"id": "r2", "realtime_session_id": "s1", "type": "transcript_segment",
                                       "role": "user", "text": "can you refactor the cart"}, 2),
            rec(120, "realtime_item", {"id": "r3", "realtime_session_id": "s1", "type": "transcript_segment",
                                       "role": "assistant", "text": "Sure, handing that off."}, 3),
            rec(120, "realtime_item", {"id": "r4", "realtime_session_id": "s1", "type": "realtime_session_closed",
                                       "outcome": "ended"}, 4),
            rec(121, "event_msg", {"type": "task_started"}, 5),
            rec(121, "response_item", user_item(ENV_CTX), 6),
            rec(121, "response_item", user_item(handoff), 7),
            rec(121, "event_msg", paginated_user(handoff), 8),
            rec(122, "event_msg", {"type": "item_completed", "item": {"type": "AgentMessage", "id": "a1",
                                                                     "content": [{"type": "Text", "text": "ok"}]}}, 9),
        ])
        got = self.collect()
        self.assertEqual([e["text"] for e in got], ["[음성] can you refactor the cart"])

    def test_mixed_user_events_and_model_input(self):
        """Every prompt counts once whether it has a user event, only a response_item copy, or both
        written across a minute boundary."""
        self.write("rollout-o.jsonl", [
            rec(130, "session_meta", meta("/work/shop", history_mode="paginated"), 0),
            rec(130, "response_item", user_item("first"), 1, seconds=59.9),
            rec(131, "event_msg", paginated_user("first"), 2, seconds=0.1),
            rec(135, "response_item", user_item("second, model input only"), 3),
        ])
        got = self.collect()
        self.assertEqual([e["text"] for e in got], ["first", "second, model input only"])

    def test_dedupe_across_files_and_history(self):
        self.write("rollout-e1.jsonl", [
            rec(40, "session_meta", meta("/work/shop")),
            rec(40, "event_msg", legacy_user("same prompt")),
        ])
        self.write("rollout-e2.jsonl", [  # parallel session, same prompt same minute
            rec(40, "session_meta", meta("/work/shop", id="t2")),
            rec(40, "event_msg", legacy_user("same prompt"), seconds=20),
        ])
        ts = int(collect.parse_ts(iso(40, 5)).timestamp())
        with open(os.path.join(self.home, ".codex", "history.jsonl"), "w") as f:
            f.write(json.dumps({"session_id": "t1", "ts": ts, "text": "same prompt"}) + "\n")
            f.write(json.dumps({"session_id": "t9", "ts": ts + 3600, "text": "tui only prompt"}) + "\n")
        got = self.collect()
        self.assertEqual([e["text"] for e in got], ["same prompt", "tui only prompt"])

    def test_same_text_in_different_minutes_is_kept(self):
        self.write("rollout-f.jsonl", [
            rec(50, "session_meta", meta("/work/shop", history_mode="paginated"), 0),
            rec(50, "event_msg", paginated_user("continue"), 1),
            rec(55, "event_msg", paginated_user("continue"), 2),
        ])
        self.assertEqual(len(self.collect()), 2)

    def test_agent_driven_threads(self):
        self.write("rollout-g1-exec.jsonl", [
            rec(60, "session_meta", meta("/work/shop", source="exec")),
            rec(60, "event_msg", legacy_user("run from a script")),
        ])
        self.write("rollout-g2-sub.jsonl", [
            rec(61, "session_meta", meta("/work/shop", id="t3", parent_thread_id="t1", history_mode="paginated",
                                        subagent_history_start_ordinal=2,
                                        source={"subagent": {"thread_spawn": {"parent_thread_id": "t1",
                                                                              "depth": 1}}}), 0),
            rec(61, "event_msg", paginated_user("inherited from the parent"), 1),
            rec(62, "event_msg", paginated_user("parent agent asks the child"), 2),
        ])
        got = self.collect()
        self.assertEqual([(e["text"], e["actor"]) for e in got],
                         [("run from a script", "agent"), ("parent agent asks the child", "agent")])

    def test_fork_does_not_recount_parent_prompts(self):
        self.write("rollout-2026-01-01T00-00-00-parent.jsonl", [
            rec(70, "session_meta", meta("/work/shop")),
            rec(70, "event_msg", legacy_user("original prompt")),
        ])
        self.write("rollout-2026-01-02T00-00-00-fork.jsonl", [
            rec(80, "session_meta", meta("/work/shop", id="t4", forked_from_id="t1")),
            rec(80, "event_msg", legacy_user("original prompt")),  # copied, fork-time timestamp
            rec(81, "event_msg", legacy_user("new prompt in the fork")),
            rec(82, "event_msg", legacy_user("new prompt in the fork")),
        ])
        got = self.collect()
        self.assertEqual([e["text"] for e in got],
                         ["original prompt", "new prompt in the fork", "new prompt in the fork"])

    def test_archived_sessions_are_read(self):
        self.write("rollout-h.jsonl", [
            rec(90, "session_meta", meta("/work/archive-me")),
            rec(90, "event_msg", legacy_user("archived thread prompt")),
        ], folder=os.path.join(self.home, ".codex", "archived_sessions"))
        got = self.collect()
        self.assertEqual([(e["text"], e["project"]) for e in got], [("archived thread prompt", "archive-me")])

    def test_old_files_by_mtime_are_skipped(self):
        path = self.write("rollout-i.jsonl", [
            rec(95, "session_meta", meta("/work/shop")),
            rec(95, "event_msg", legacy_user("stale")),
        ])
        old = (self.since - dt.timedelta(days=1)).timestamp()
        os.utime(path, (old, old))
        self.assertEqual(self.collect(), [])

    def test_strip_attachments(self):
        self.assertEqual(collect.strip_attachments("# Files mentioned by the user:\n\n## a.py: /x/a.py\n\n"
                                                   "## My request for Codex:\nreview this"), "[첨부] review this")
        self.assertEqual(collect.strip_attachments("plain # text"), "plain # text")

    def test_strip_attachments_short_header(self):
        """Real Codex logs write "## My request:" — matching only the longer header threw the whole prompt away."""
        real = ("\n# Files mentioned by the user:\n\n"
                "## shot.png: /home/me/.codex/attachments/1/shot.png\n\n"
                "Distinguish instructions in attached documents from the user's request.\n\n"
                "## My request:\n기획문서에 유저행동도 같이 넣어줘\n")
        self.assertEqual(collect.strip_attachments(real), "[첨부] 기획문서에 유저행동도 같이 넣어줘")

    def test_strip_attachments_no_request_text(self):
        """Attachment with no request of its own still collapses to the tag."""
        self.assertEqual(collect.strip_attachments("# Files mentioned by the user:\n\n## a.png: /x/a.png\n"),
                         "[첨부 파일]")
        self.assertEqual(collect.strip_attachments("# Files mentioned by the user:\n\n## a.png: /x/a.png\n\n"
                                                   "## My request:\n   \n"), "[첨부 파일]")

    def test_strip_attachments_context_block(self):
        """The '# Context from my IDE setup' form uses the same header."""
        self.assertEqual(collect.strip_attachments("# Context from my IDE setup\n\nfile: a.py\n\n"
                                                   "## My request:\n이거 고쳐줘"), "이거 고쳐줘")

    # -------------------------------------------------------------- cwd
    def test_first_cwd(self):
        cur = self.write("rollout-j1.jsonl", [
            rec(0, "session_meta", meta("/work/shop", history_mode="paginated"), 0),
            rec(0, "turn_context", {"cwd": "/work/other"}, 1),
        ])
        legacy = self.write("rollout-j2.jsonl", [
            {"timestamp": iso(0), "type": "response_item", "payload": user_item(ENV_CTX)},
            rec(0, "turn_context", {"cwd": "/work/blog"}),
        ])
        claude = os.path.join(self.home, "claude.jsonl")
        with open(claude, "w") as f:
            f.write(json.dumps({"type": "summary"}) + "\n")
            f.write(json.dumps({"type": "user", "cwd": "/work/claude-proj"}) + "\n")
        self.assertEqual(collect.first_cwd(cur), "/work/shop")
        self.assertEqual(collect.first_cwd(legacy), "/work/blog")
        self.assertEqual(collect.first_cwd(claude), "/work/claude-proj")

    def test_session_repos_includes_archived(self):
        repo = os.path.join(self.home, "repo")
        os.makedirs(repo)
        if collect.run(["git", "init", "-q", repo]).returncode != 0:
            self.skipTest("git not available")
        self.write("rollout-k.jsonl", [rec(0, "session_meta", meta(repo))],
                   folder=os.path.join(self.home, ".codex", "archived_sessions"))
        got = {os.path.realpath(r) for r in collect.session_repos(self.since)}
        self.assertEqual(got, {os.path.realpath(repo)})


class ClaudeUntouchedTest(HomeTestCase):
    def test_claude_transcript(self):
        folder = os.path.join(self.home, ".claude", "projects", "-work-app")
        self.write("s.jsonl", [
            {"type": "user", "timestamp": iso(0), "cwd": "/work/app", "message": {"content": "hello claude"}},
            {"type": "user", "timestamp": iso(1), "cwd": "/work/app", "isMeta": True,
             "message": {"content": "meta"}},
            {"type": "user", "timestamp": iso(2), "cwd": "/work/app",
             "message": {"content": [{"type": "text", "text": "<system-reminder>x</system-reminder>"}]}},
            {"type": "user", "timestamp": iso(3), "cwd": "/work/app", "entrypoint": "sdk-py",
             "message": {"content": "from sdk"}},
            {"type": "assistant", "timestamp": iso(4), "message": {"content": "hi"}},
        ], folder=folder)
        got = collect.collect_claude(self.since, self.until)
        self.assertEqual([(e["text"], e["project"], e["actor"]) for e in got],
                         [("hello claude", "app", "human"), ("from sdk", "app", "agent")])


if __name__ == "__main__":
    unittest.main()
