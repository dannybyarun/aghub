"""Concurrent swarm regression tests for the git-backed board.

These tests use real clones and real git remotes: the failure mode they guard
against is several agents losing the board-ref race at the same time and
exhausting the retry budget, or a writer lease failing to serialize them.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHONPATH = str(ROOT / "src")
sys.path.insert(0, PYTHONPATH)

from aghub import hubclient  # noqa: E402
from aghub.board import Board  # noqa: E402
from aghub.hub import build_server  # noqa: E402

AGENTS = 8


def git(cwd, *args, check=True):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


def dead_url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


class SwarmScaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aghub-swarm-test-"))
        self.origin = self.tmp / "origin.git"
        self.seed = self.tmp / "seed"
        self.clones = [self.tmp / f"w{i:02d}" for i in range(1, AGENTS + 1)]
        self._seed()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, cwd, *args, agent=None, check=True, env=None):
        run_env = {**os.environ, "PYTHONPATH": PYTHONPATH}
        if agent:
            run_env["AGHUB_AGENT"] = agent
        run_env.update(env or {})
        proc = subprocess.run(
            [sys.executable, "-m", "aghub", *args],
            cwd=cwd,
            env=run_env,
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

    def _bootstrap(self):
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
        return first

    def _start_hub(self):
        server, store = build_server(
            "127.0.0.1", 0, ":memory:", token="swarm", rate_limit=0
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(store.close)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def _claim_all(self, env=None):
        run_env = {
            **os.environ,
            "PYTHONPATH": PYTHONPATH,
            "AGHUB_TRACE_BOARD": "1",
            **(env or {}),
        }
        processes = []
        for index, clone in enumerate(self.clones, start=1):
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
                    env={**run_env, "AGHUB_AGENT": f"w{index:02d}"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )

        failures = []
        stderr = []
        for index, process in enumerate(processes, start=1):
            stdout, err = process.communicate(timeout=180)
            stderr.append(err)
            if process.returncode != 0:
                failures.append(
                    f"w{index:02d} exited {process.returncode}:\n{stdout}\n{err}"
                )
        return failures, "\n".join(stderr)

    def _assert_claimed(self, first):
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

    def test_eight_agents_claim_disjoint_lanes_concurrently(self):
        first = self._bootstrap()
        failures, _ = self._claim_all()
        self.assertEqual(failures, [])
        self._assert_claimed(first)

    def test_hub_writer_lock_serializes_shared_board_writes(self):
        first = self._bootstrap()
        url = self._start_hub()
        for index, clone in enumerate(self.clones, start=1):
            self._run(
                clone,
                "hub",
                "connect",
                url,
                "--token",
                "swarm",
                agent=f"w{index:02d}",
            )
        self.assertFalse(
            hubclient.board_lock_status(url, "swarm", token="swarm")["active"]
        )

        failures, stderr = self._claim_all(
            {"AGHUB_BOARD_LOCK_WAIT": "120"}
        )
        self.assertEqual(failures, [])
        self.assertIn("board lock busy", stderr)
        self.assertNotIn("board retry", stderr)
        self.assertFalse(
            hubclient.board_lock_status(url, "swarm", token="swarm")["active"]
        )
        audit = hubclient.audit(url, "swarm", limit=500, token="swarm")["events"]
        locks = [item for item in audit if item["type"] == "board.lock"]
        self.assertGreaterEqual(len(locks), AGENTS)
        self._assert_claimed(first)

    def test_unreachable_hub_falls_back_to_optimistic_board_writes(self):
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
        config_path = first / ".git" / "aghub.local.json"
        config = json.loads(config_path.read_text())
        config["hub"] = dead_url()
        config["hub_token"] = None
        config["hub_failover"] = []
        config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")

        proc = self._run(
            first,
            "task",
            "add",
            "Offline lock",
            "--paths",
            "offline/**",
            agent="w01",
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("optimistic board writes", proc.stderr)

        board = Board.discover(str(first))
        board.sync()
        state = board.load()
        self.assertTrue(
            any(task.get("title") == "Offline lock" for task in state.tasks.values())
        )


if __name__ == "__main__":
    unittest.main()
