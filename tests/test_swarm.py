"""Concurrent swarm regression tests for the git-backed board.

These tests use real clones and real git remotes: the failure mode they guard
against is several agents losing the board-ref race at the same time and
exhausting the retry budget.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHONPATH = str(ROOT / "src")
sys.path.insert(0, PYTHONPATH)

from aghub.board import Board  # noqa: E402

AGENTS = 8


def git(cwd, *args, check=True):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


class SwarmScaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aghub-swarm-test-"))
        self.origin = self.tmp / "origin.git"
        self.seed = self.tmp / "seed"
        self.clones = [self.tmp / f"w{i:02d}" for i in range(1, AGENTS + 1)]
        self._seed()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, cwd, *args, agent=None, check=True):
        env = {**os.environ, "PYTHONPATH": PYTHONPATH}
        if agent:
            env["AGHUB_AGENT"] = agent
        proc = subprocess.run(
            [sys.executable, "-m", "aghub", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"aghub {' '.join(args)} failed ({proc.returncode}):\n"
                f"{proc.stdout}\n{proc.stderr}"
            )
        return proc

    def _seed(self):
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.seed.mkdir()
        git(self.seed, "init", "-q", "-b", "main")
        git(self.seed, "config", "user.name", "seed")
        git(self.seed, "config", "user.email", "seed@example.com")
        (self.seed / "README.md").write_text("swarm\n")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-qm", "seed")
        git(self.seed, "remote", "add", "origin", str(self.origin))
        git(self.seed, "push", "-qu", "origin", "main")
        for clone in self.clones:
            git(self.tmp, "clone", "-q", str(self.origin), str(clone))
            git(clone, "config", "user.name", clone.name)
            git(clone, "config", "user.email", f"{clone.name}@example.com")

    def test_eight_agents_claim_disjoint_lanes_concurrently(self):
        first = self.clones[0]
        self._run(
            first,
            "init",
            "--project",
            "swarm",
            "--ttl",
            "120",
            agent="w01",
        )
        for index, clone in enumerate(self.clones[1:], start=2):
            self._run(
                clone,
                "init",
                "--project",
                "swarm",
                agent=f"w{index:02d}",
            )
        for index in range(1, AGENTS + 1):
            self._run(
                first,
                "task",
                "add",
                f"Lane {index:02d}",
                "--paths",
                f"lanes/{index:02d}/**",
                agent="w01",
            )

        env = {**os.environ, "PYTHONPATH": PYTHONPATH, "AGHUB_TRACE_BOARD": "1"}
        processes = []
        for index, clone in enumerate(self.clones, start=1):
            agent = f"w{index:02d}"
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "aghub",
                        "claim",
                        f"T-{index:03d}",
                    ],
                    cwd=clone,
                    env={**env, "AGHUB_AGENT": agent},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )

        failures = []
        for index, process in enumerate(processes, start=1):
            stdout, stderr = process.communicate(timeout=180)
            if process.returncode != 0:
                failures.append(
                    f"w{index:02d} exited {process.returncode}:\n{stdout}\n{stderr}"
                )
        self.assertEqual(failures, [])

        board = Board.discover(str(first))
        board.sync()
        state = board.load()
        owners = {}
        for index in range(1, AGENTS + 1):
            task = state.tasks[f"T-{index:03d}"]
            self.assertEqual(task["status"], "claimed")
            self.assertEqual(task["owner"], f"w{index:02d}")
            owners[task["owner"]] = task["id"]
        self.assertEqual(len(owners), AGENTS)
        self.assertEqual(
            len({event["id"] for event in state.events if event["type"] == "task.claim"}),
            AGENTS,
        )


if __name__ == "__main__":
    unittest.main()
