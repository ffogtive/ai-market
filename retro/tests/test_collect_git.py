"""collect_git: commit line counts when a commit only adds or only deletes."""
import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import collect  # noqa: E402


@unittest.skipUnless(shutil.which("git"), "git not installed")
class CommitStatTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo)
        self.git("init", "-q")

    def git(self, *args):
        subprocess.run(["git", "-c", "user.email=me@x", "-c", "user.name=me"] + list(args),
                       cwd=self.repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def commit(self, text, msg):
        with open(os.path.join(self.repo, "f"), "w") as f:
            f.write(text)
        self.git("add", "f")
        self.git("commit", "-qm", msg)

    def test_line_counts(self):
        self.commit("a\nb\nc\n", "add only")
        self.commit("b\nc\n", "delete only")
        self.commit("c\nx\ny\n", "both")
        self.git("commit", "-q", "--allow-empty", "-m", "empty")
        now = dt.datetime.now(collect.LOCAL_TZ)
        got = {e["text"] for e in collect.collect_git(now - dt.timedelta(days=1), now + dt.timedelta(minutes=1),
                                                     [self.repo], None, fetch=False)}
        self.assertEqual(got, {"add only (+3/-0)", "delete only (+0/-1)", "both (+2/-1)", "empty"})


if __name__ == "__main__":
    unittest.main()
