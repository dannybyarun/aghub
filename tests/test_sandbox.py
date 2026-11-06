import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aghub import gitops, sandbox  # noqa: E402
from aghub.errors import AghubError  # noqa: E402


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def py(code):
    """A shell command that runs Python with platform-appropriate quoting."""
    return f'"{sys.executable}" -c "{code}"'


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aghub-sandbox-test-")
        self.repo = Path(self.tmp) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "tracked.txt").write_text("tracked")
        (self.repo / ".gitignore").write_text("ignored.txt\n")
        git(self.repo, "add", "tracked.txt", ".gitignore")
        git(self.repo, "commit", "-qm", "seed")
        (self.repo / "untracked.txt").write_text("untracked")
        (self.repo / "ignored.txt").write_text("ignored")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_snapshot_copies_tracked_and_untracked_but_not_ignored(self):
        work = sandbox.snapshot(str(self.repo))
        try:
            self.assertTrue((Path(work) / "tracked.txt").exists())
            self.assertTrue((Path(work) / "untracked.txt").exists())
            self.assertFalse((Path(work) / "ignored.txt").exists())
            self.assertFalse((Path(work) / ".git").exists())
            self.assertTrue((Path(work) / ".aghub-sandbox" / "tmp").is_dir())
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_worktree_fingerprint_tracks_dirty_and_untracked_files(self):
        first = gitops.worktree_fingerprint(str(self.repo))
        self.assertIsNotNone(first)
        (self.repo / "untracked.txt").write_text("changed")
        self.assertNotEqual(first, gitops.worktree_fingerprint(str(self.repo)))
        (self.repo / "untracked.txt").unlink()
        self.assertEqual(gitops.worktree_fingerprint(str(self.repo)), None)
        (self.repo / "tracked.txt").write_text("modified")
        dirty = gitops.worktree_fingerprint(str(self.repo))
        self.assertIsNotNone(dirty)
        self.assertNotEqual(first, dirty)
        (self.repo / "tracked.txt").write_text("tracked")
        self.assertIsNone(gitops.worktree_fingerprint(str(self.repo)))

    def test_run_scrubs_env_and_uses_a_snapshot(self):
        os.environ["AGHUB_SANDBOX_SECRET"] = "do-not-leak"
        try:
            result = sandbox.run(
                py(
                    "import os; print(os.getcwd()); "
                    "print(os.getenv('AGHUB_SANDBOX_SECRET')); "
                    "open('created.txt', 'w').write('inside')"
                ),
                str(self.repo),
                timeout=30,
            )
        finally:
            os.environ.pop("AGHUB_SANDBOX_SECRET", None)
        self.assertEqual(result.exit_code, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertNotEqual(Path(lines[0]).resolve(), self.repo.resolve())
        self.assertEqual(lines[1], "None")
        self.assertFalse((self.repo / "created.txt").exists())
        self.assertIn(result.sandbox["backend"], ("bubblewrap", "copy"))
        self.assertTrue(result.sandbox["snapshot"])

    def test_timeout_is_reported_and_cleaned_up(self):
        result = sandbox.run(
            py("import time; time.sleep(10)"),
            str(self.repo),
            timeout=1,
        )
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, 124)

    def test_require_refuses_without_bubblewrap(self):
        with mock.patch.object(sandbox, "bubblewrap", return_value=None):
            with self.assertRaises(AghubError):
                sandbox.run("true", str(self.repo), mode="require")

    def test_off_mode_runs_in_the_checkout(self):
        result = sandbox.run(
            py("import os; print(os.getcwd())"),
            str(self.repo),
            timeout=30,
            mode="off",
        )
        self.assertEqual(Path(result.stdout.strip()).resolve(), self.repo.resolve())
        self.assertEqual(result.sandbox["backend"], "none")


if __name__ == "__main__":
    unittest.main()
