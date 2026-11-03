import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PYTHONPATH = str(ROOT / "src")
sys.path.insert(0, PYTHONPATH)

from aghub import github, hubclient, render  # noqa: E402
from aghub.board import Board  # noqa: E402
from aghub.hub import build_server  # noqa: E402

CONTRACT = """\
openapi: 3.0.0
info:
  title: Items
  version: "1"
paths:
  /items:
    get:
      summary: list
  /items/{id}:
    get:
      summary: get
"""

CHECK = """\
import pathlib, re, sys
root = pathlib.Path(__file__).parent
routes = re.findall(r"^\\s{2}(/[A-Za-z0-9_{}/-]+):\\s*$", (root / "contracts" / "api.yaml").read_text(), re.M)
sources = [p for d in ("src/api", "src/web") if (root / d).exists() for p in (root / d).rglob("*") if p.is_file()]
text = "\\n".join(p.read_text(errors="ignore") for p in sources)
missing = [r for r in routes if r not in text]
extra = sorted({m for m in re.findall(r"['\\"](/[a-z0-9_/{}-]+)['\\"]", text) if m not in routes})
if missing:
    print("missing:", missing); sys.exit(1)
if extra:
    print("not in contract:", extra); sys.exit(1)
print("contract ok")
"""


def git(cwd, *args, check=True):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class World:
    def __init__(self, base):
        self.base = Path(base)
        self.origin = self.base / "origin.git"
        self.fe = self.base / "fe"
        self.be = self.base / "be"
        self.ci = self.base / "ci"

    def aghub(self, where, *args, agent=None, check=True, env=None):
        cwd = getattr(self, where) if isinstance(where, str) else where
        merged = {**os.environ, "PYTHONPATH": PYTHONPATH}
        if agent:
            merged["AGHUB_AGENT"] = agent
        if env:
            merged.update(env)
        proc = subprocess.run(
            [sys.executable, "-m", "aghub", *args],
            cwd=cwd,
            env=merged,
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"aghub {' '.join(args)} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
            )
        return proc

    def commit_file(self, where, relative, content, message):
        cwd = getattr(self, where)
        path = Path(cwd) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        git(cwd, "add", "-A")
        git(cwd, "commit", "-qm", message)

    def seed(self):
        git(self.base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.base / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        git(seed, "config", "user.name", "seed")
        git(seed, "config", "user.email", "seed@example.com")
        (seed / "contracts").mkdir()
        (seed / "contracts" / "api.yaml").write_text(CONTRACT)
        (seed / "check.py").write_text(CHECK)
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "seed")
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "-qu", "origin", "main")
        for name, clone in (("fe", self.fe), ("be", self.be), ("ci", self.ci)):
            git(self.base, "clone", "-q", str(self.origin), str(clone))
            git(clone, "config", "user.name", name)
            git(clone, "config", "user.email", f"{name}@example.com")


class AghubTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aghub-test-")
        self.world = World(self.tmp)
        self.world.seed()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def bootstrap(self, ttl=60, check="python3 check.py"):
        w = self.world
        w.aghub("fe", "init", "--project", "test", "--ttl", str(ttl), "--check", check, agent="fe-1")
        w.aghub("fe", "contract", "freeze", "api", "1", "--from", "contracts/api.yaml", agent="fe-1")
        w.aghub("fe", "task", "add", "API", "--paths", "src/api/**", "--contract", "api@1", agent="fe-1")
        w.aghub("fe", "task", "add", "UI", "--paths", "src/web/**", "--contract", "api@1", agent="fe-1")
        w.aghub("be", "init", "--project", "test", agent="be-1")

    # -- protocol mechanics ---------------------------------------------

    def test_task_ids_increment(self):
        self.bootstrap()
        proc = self.world.aghub("fe", "task", "list", agent="fe-1")
        self.assertIn("T-001", proc.stdout)
        self.assertIn("T-002", proc.stdout)

    def test_task_import_is_one_board_write(self):
        w = self.world
        self.bootstrap()
        board_dir = Path(w.fe) / ".git" / "aghub-board"
        before = len(git(board_dir, "log", "--oneline").stdout.splitlines())
        specs = (
            "- title: Import A\n  paths: [src/a/**]\n"
            "- title: Import B\n  paths: [src/b/**]\n"
            "- title: Import C\n  paths: [src/c/**]\n  requires_approval: true\n"
        )
        (Path(w.fe) / "tasks.yaml").write_text(specs)
        out = w.aghub("fe", "task", "import", "tasks.yaml", agent="fe-1")
        self.assertIn("imported 3 task(s)", out.stdout)
        listed = w.aghub("fe", "task", "list", agent="fe-1").stdout
        for task_id in ("T-003", "T-004", "T-005"):
            self.assertIn(task_id, listed)
        after = len(git(board_dir, "log", "--oneline").stdout.splitlines())
        self.assertEqual(after - before, 1, "import should be a single board commit")
        self.assertIn("import 3 task(s)", git(board_dir, "log", "-1", "--format=%s").stdout)

    def test_sharded_boards_are_independent(self):
        w = self.world
        self.bootstrap()
        created = w.aghub(
            "fe", "init", "--project", "test-web", "--check", "true",
            agent="fe-1", env={"AGHUB_SHARD": "web"},
        )
        self.assertIn("shard", created.stdout)
        w.aghub(
            "fe", "task", "add", "Shard task", "--paths", "src/web/**",
            agent="fe-1", env={"AGHUB_SHARD": "web"},
        )
        default_list = w.aghub("fe", "task", "list", agent="fe-1").stdout
        self.assertNotIn("Shard task", default_list)
        web_list = w.aghub(
            "fe", "task", "list", agent="fe-1", env={"AGHUB_SHARD": "web"}
        ).stdout
        self.assertIn("Shard task", web_list)
        boards = json.loads(w.aghub("fe", "boards", "--json", agent="fe-1").stdout)
        by_name = {board["name"]: board for board in boards}
        self.assertEqual(set(by_name), {"default", "web"})
        self.assertEqual(by_name["web"]["branch"], "aghub/board-web")
        self.assertEqual(by_name["web"]["project"], "test-web")

    def test_shard_init_requires_a_default_board_and_rejects_duplicates(self):
        w = self.world
        self.bootstrap()
        w.aghub(
            "fe", "init", "--project", "p-web", "--check", "true",
            agent="fe-1", env={"AGHUB_SHARD": "web"},
        )
        dup = w.aghub(
            "fe", "init", "--project", "p-web", "--check", "true",
            agent="fe-1", env={"AGHUB_SHARD": "web"}, check=False,
        )
        self.assertEqual(dup.returncode, 1)
        self.assertIn("already exists", dup.stderr)

    def test_live_claim_cannot_be_stolen(self):
        self.bootstrap()
        self.world.aghub("fe", "claim", "T-001", agent="fe-1")
        proc = self.world.aghub("be", "claim", "T-001", agent="be-1", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claimed by fe-1", proc.stderr)

    def test_claim_creates_a_worktree(self):
        self.bootstrap()
        path = self.world.base / "fe-lane"
        proc = self.world.aghub(
            "fe", "claim", "T-001", "--worktree", str(path), agent="fe-1"
        )
        self.assertIn("worktree", proc.stdout)
        head = git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertEqual(head, "agent/fe-1/T-001")

    def test_claim_worktree_default_and_rollback(self):
        self.bootstrap()
        blocked = self.world.base / "occupied"
        blocked.mkdir()
        (blocked / "keep.txt").write_text("keep")
        proc = self.world.aghub(
            "fe", "claim", "T-001", "--worktree", str(blocked), agent="fe-1", check=False
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not an empty directory", proc.stderr)
        state = json.loads(
            self.world.aghub("fe", "status", "--json", agent="fe-1").stdout
        )
        task = next(item for item in state["tasks"] if item["id"] == "T-001")
        self.assertIsNone(task["owner"])

        default = self.world.base / "fe-fe-1-T-001"
        proc = self.world.aghub("fe", "claim", "T-001", "--worktree", agent="fe-1")
        self.assertIn(str(default), proc.stdout)
        self.assertTrue((default / ".git").exists())

    def test_path_claims_overlap(self):
        self.bootstrap()
        self.world.aghub("fe", "task", "add", "API v2", "--paths", "src/api/v2/**", "--contract", "api@1", agent="fe-1")
        self.world.aghub("be", "claim", "T-001", agent="be-1")
        proc = self.world.aghub("fe", "claim", "T-003", agent="fe-1", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("path conflict", proc.stderr)

    def test_stale_claim_can_be_stolen(self):
        self.bootstrap()
        self.world.aghub("fe", "claim", "T-001", agent="fe-1")
        self.world.aghub("ci", "init", "--project", "test", agent="ci")
        self.world.aghub("ci", "debug", "expire", "fe-1")
        proc = self.world.aghub("be", "claim", "T-001", agent="be-1", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("stale claim", proc.stderr)
        self.world.aghub("be", "claim", "T-001", "--steal", agent="be-1")
        state = json.loads(self.world.aghub("be", "status", "--json", agent="be-1").stdout)
        task = next(item for item in state["tasks"] if item["id"] == "T-001")
        self.assertEqual(task["owner"], "be-1")

    def test_draft_contract_blocks_claim(self):
        self.bootstrap()
        self.world.aghub("fe", "contract", "draft", "api", "2", "--from", "contracts/api.yaml", agent="fe-1")
        self.world.aghub("fe", "task", "add", "Next", "--paths", "src/api/**", "--contract", "api@2", agent="fe-1")
        proc = self.world.aghub("be", "claim", "T-003", agent="be-1", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("blocked", proc.stderr)
        self.world.aghub("fe", "contract", "freeze", "api", "2", agent="fe-1")
        self.world.aghub("be", "claim", "T-003", agent="be-1")

    def test_board_branch_is_isolated_from_main(self):
        self.bootstrap()
        self.assertTrue(git(self.world.fe, "rev-parse", "--verify", "refs/heads/aghub/board").returncode == 0)
        log = git(self.world.fe, "log", "--oneline", "main").stdout
        self.assertEqual(len(log.strip().splitlines()), 1)

    # -- codegen ---------------------------------------------------------

    def test_codegen_from_frozen_contract_and_drift(self):
        w = self.world
        self.bootstrap()
        out = w.fe / "generated"
        proc = w.aghub("fe", "codegen", "api", "1", "--out", str(out), agent="fe-1")
        self.assertIn("generated 5 file(s)", proc.stdout)
        for filename in ("api.ts", "api.py", "api_mock.py", "api.sql", "manifest.json"):
            self.assertTrue((out / filename).exists(), filename)
        self.assertIn('"/items"', (out / "api.ts").read_text())
        self.assertIn("ROUTES = (", (out / "api.py").read_text())
        self.assertIn("def match", (out / "api_mock.py").read_text())

        clean = w.aghub(
            "fe", "codegen", "api", "1", "--out", str(out), "--check", agent="fe-1"
        )
        self.assertIn("codegen clean", clean.stdout)

        with open(out / "api.ts", "a", encoding="utf-8") as handle:
            handle.write("// drift\n")
        drift = w.aghub(
            "fe", "codegen", "api", "1", "--out", str(out), "--check",
            agent="fe-1", check=False,
        )
        self.assertEqual(drift.returncode, 1)
        self.assertIn("drift", drift.stderr)

        w.aghub("fe", "contract", "draft", "api", "2", "--from", "contracts/api.yaml", agent="fe-1")
        refused = w.aghub(
            "fe", "codegen", "api", "2", "--out", str(out), agent="fe-1", check=False
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("not frozen", refused.stderr)

    def test_contract_diff_between_frozen_versions(self):
        w = self.world
        self.bootstrap()
        v2 = CONTRACT.replace(
            "paths:\n",
            "paths:\n  /tags:\n    get:\n      summary: list tags\n",
        )
        w.commit_file("fe", "contracts/api-v2.yaml", v2, "contract v2")
        w.aghub(
            "fe", "contract", "freeze", "api", "2",
            "--from", "contracts/api-v2.yaml", agent="fe-1",
        )
        text = w.aghub("fe", "contract", "diff", "api", agent="fe-1")
        self.assertIn("api@1 → api@2", text.stdout)
        self.assertIn("+ route      /tags", text.stdout)
        explicit = w.aghub("fe", "contract", "diff", "api", "1", "2", agent="fe-1")
        self.assertIn("+ operation  GET /tags", explicit.stdout)
        data = json.loads(
            w.aghub("fe", "contract", "diff", "api", "--json", agent="fe-1").stdout
        )
        self.assertEqual(data["from"], 1)
        self.assertEqual(data["to"], 2)
        self.assertEqual(data["diff"]["routes_added"], ["/tags"])
        self.assertEqual(
            data["diff"]["operations_added"], [{"method": "GET", "path": "/tags"}]
        )

        board = Board.discover(w.fe)
        snapshot = render.snapshot(board.load(), board)
        latest = next(
            item
            for item in snapshot["contracts"]
            if item["name"] == "api" and item["version"] == 2
        )
        self.assertEqual(latest["diff_from"], 1)
        self.assertEqual(latest["diff"]["routes_added"], ["/tags"])

    def test_init_sets_eval_ttl_and_publishes_attestation_policy(self):
        w = self.world
        init = w.aghub(
            "fe",
            "init",
            "--project",
            "test",
            "--check",
            "python3 check.py",
            "--eval-ttl",
            "0",
            agent="fe-1",
        )
        self.assertIn("eval ttl  0s", init.stdout)
        self.assertIn("eval sign ed25519", init.stdout)
        state = json.loads(w.aghub("fe", "status", "--json", agent="fe-1").stdout)
        self.assertEqual(state["project"]["eval_ttl_seconds"], 0)
        self.assertEqual(state["project"]["eval_attestation"], "ed25519")
        self.assertNotIn("eval_secret", state["project"])
        project_yaml = (w.fe / ".git" / "aghub-board" / "project.yaml").read_text()
        self.assertIn("eval_attestation: ed25519", project_yaml)
        self.assertNotIn("eval_secret:", project_yaml)

    def test_legacy_hmac_attestation_can_be_selected_explicitly(self):
        w = self.world
        init = w.aghub(
            "fe",
            "init",
            "--project",
            "test",
            "--attestation",
            "hmac-sha256",
            agent="fe-1",
        )
        self.assertIn("eval sign hmac-sha256", init.stdout)
        passed = w.aghub(
            "fe",
            "eval",
            "run",
            "smoke",
            "--command",
            "python3 -c 'print(\"ok\")'",
            agent="fe-1",
        )
        self.assertIn("attested  hmac-sha256", passed.stdout)
        eval_id = passed.stdout.split()[1]
        self.assertEqual(w.aghub("fe", "eval", "verify", eval_id).returncode, 0)
        project_yaml = (w.fe / ".git" / "aghub-board" / "project.yaml").read_text()
        self.assertIn("eval_attestation: hmac-sha256", project_yaml)
        self.assertIn("eval_secret:", project_yaml)

    def test_registry_profile_and_local_eval_scorecards(self):
        w = self.world
        self.bootstrap()
        profile = w.aghub(
            "be",
            "registry",
            "profile",
            "--model",
            "test-model",
            "--skills",
            "python,api",
            "--note",
            "backend lane",
            agent="be-1",
        )
        self.assertIn("test-model", profile.stdout)
        show = w.aghub("be", "registry", "show", agent="be-1")
        self.assertIn("model: test-model", show.stdout)
        self.assertIn("- python", show.stdout)
        self.assertIn("note: backend lane", show.stdout)

        passed = w.aghub(
            "be",
            "eval",
            "run",
            "smoke",
            "--command",
            "python3 -c 'print(\"ok\")'",
            agent="be-1",
        )
        self.assertIn("passed", passed.stdout)
        self.assertIn("exit 0", passed.stdout)
        self.assertIn("attested  ed25519", passed.stdout)
        self.assertIn("sandbox   ", passed.stdout)

        state = json.loads(w.aghub("be", "status", "--json", agent="be-1").stdout)
        latest = state["evals"][-1]
        self.assertEqual(latest["suite"], "smoke")
        self.assertEqual(latest["agent"], "be-1")
        self.assertTrue(latest["passed"])
        self.assertEqual(latest["machine"], os.uname().nodename)
        self.assertEqual(latest["attestation"]["scheme"], "ed25519")
        self.assertIn(latest["sandbox"]["backend"], ("bubblewrap", "copy"))
        self.assertTrue(latest["sandbox"]["snapshot"])
        self.assertNotIn("eval_secret", state["project"])
        self.assertEqual(
            state["project"]["eval_ttl_seconds"], 7 * 24 * 60 * 60
        )
        profile = next(item for item in state["agents"] if item["id"] == "be-1")
        self.assertEqual(profile["key_alg"], "ed25519")
        self.assertEqual(
            profile["key_id"], latest["attestation"]["key_id"]
        )
        self.assertNotIn("private_key", json.dumps(state))
        local = json.loads((w.be / ".git" / "aghub.local.json").read_text())
        self.assertIn("private_key", local["agent_keys"]["be-1"])
        self.assertIn("smoke ok", w.aghub("be", "status", agent="be-1").stdout)

        smoke = next(
            line
            for line in w.aghub("be", "eval", "list", "--agent", "be-1").stdout.splitlines()
            if " smoke " in line
        )
        smoke_id = smoke.split()[0]
        self.assertIn("signed", smoke)
        verified = w.aghub("be", "eval", "verify", smoke_id)
        self.assertIn("verified", verified.stdout)
        self.assertEqual(w.aghub("be", "eval", "ttl").stdout.strip(), str(7 * 24 * 60 * 60))
        self.assertIn("eval ttl 3600s", w.aghub("be", "eval", "ttl", "3600").stdout)
        self.assertEqual(w.aghub("be", "eval", "ttl").stdout.strip(), "3600")

        failed = w.aghub(
            "be",
            "eval",
            "run",
            "lint",
            "--command",
            "python3 -c 'import sys; sys.exit(2)'",
            agent="be-1",
            check=False,
        )
        self.assertEqual(failed.returncode, 1)
        self.assertIn("FAILED", failed.stdout)

        listing = w.aghub("be", "eval", "list", "--agent", "be-1")
        self.assertIn("smoke", listing.stdout)
        self.assertIn("lint", listing.stdout)
        lint = next(line for line in listing.stdout.splitlines() if " lint " in line)
        eval_id = lint.split()[0]
        detail = w.aghub("be", "eval", "show", eval_id)
        self.assertIn("exit_code: 2", detail.stdout)
        self.assertIn("passed: false", detail.stdout)

    def test_eval_rerun_detects_dirty_worktree_changes(self):
        w = self.world
        self.bootstrap()
        target = w.be / "src" / "api" / "input.txt"
        target.parent.mkdir(parents=True)
        target.write_text("one\n")

        first = w.aghub(
            "be",
            "eval",
            "run",
            "smoke",
            "--command",
            "python3 -c 'print(\"ok\")'",
            agent="be-1",
        )
        first_id = first.stdout.split()[1]
        rerun = w.aghub("be", "eval", "rerun", first_id, agent="be-1")
        self.assertIn("reproducible fingerprint matches", rerun.stdout)
        second_id = rerun.stdout.split()[1]

        target.write_text("two\n")
        changed = w.aghub("be", "eval", "rerun", second_id, agent="be-1")
        self.assertIn("fingerprint changed", changed.stdout)
        changed_id = changed.stdout.split()[1]

        diff = json.loads(
            w.aghub(
                "be", "eval", "diff", first_id, changed_id, "--json", agent="be-1"
            ).stdout
        )
        fields = {item["field"] for item in diff["changes"]}
        self.assertIn("worktree", fields)
        self.assertIn("fingerprint", fields)

    def test_agent_key_rotation_keeps_old_scorecards_verifiable(self):
        w = self.world
        self.bootstrap()
        first = w.aghub(
            "be",
            "eval",
            "run",
            "smoke",
            "--command",
            "python3 -c 'print(\"ok\")'",
            agent="be-1",
        )
        first_id = first.stdout.split()[1]
        self.assertIn("attested  ed25519", first.stdout)
        self.assertEqual(w.aghub("be", "eval", "verify", first_id).returncode, 0)

        local_before = json.loads((w.be / ".git" / "aghub.local.json").read_text())
        old_public = local_before["agent_keys"]["be-1"]["public_key"]
        rotated = w.aghub("be", "agent", "--rotate-key", agent="be-1")
        self.assertIn("rotated be-1 ed25519 key", rotated.stdout)
        local_after = json.loads((w.be / ".git" / "aghub.local.json").read_text())
        new_public = local_after["agent_keys"]["be-1"]["public_key"]
        self.assertNotEqual(old_public, new_public)

        state = json.loads(w.aghub("be", "status", "--json", agent="be-1").stdout)
        profile = next(item for item in state["agents"] if item["id"] == "be-1")
        self.assertEqual(profile["public_key"], new_public)
        self.assertEqual(profile["previous_public_keys"], [old_public])
        self.assertEqual(w.aghub("be", "eval", "verify", first_id).returncode, 0)

        second = w.aghub(
            "be",
            "eval",
            "run",
            "smoke",
            "--command",
            "python3 -c 'print(\"again\")'",
            agent="be-1",
        )
        second_id = second.stdout.split()[1]
        self.assertEqual(w.aghub("be", "eval", "verify", second_id).returncode, 0)

    def test_eval_gate_blocks_claim_until_agent_passes(self):
        w = self.world
        self.bootstrap()
        w.aghub(
            "fe",
            "task",
            "add",
            "Gated work",
            "--paths",
            "src/gated/**",
            "--eval",
            "smoke",
            agent="fe-1",
        )
        refused = w.aghub("be", "claim", "T-003", agent="be-1", check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("requires a passing eval", refused.stderr)
        self.assertIn("smoke (not run)", refused.stderr)
        self.assertNotIn("T-003", w.aghub("be", "next", agent="be-1").stdout)

        w.aghub(
            "be",
            "eval",
            "run",
            "smoke",
            "--command",
            "python3 -c 'print(\"ok\")'",
            agent="be-1",
        )
        w.aghub("be", "claim", "T-003", agent="be-1")
        w.aghub("be", "release", "T-003", agent="be-1")

        w.aghub(
            "be",
            "eval",
            "run",
            "smoke",
            "--command",
            "python3 -c 'import sys; sys.exit(1)'",
            agent="be-1",
            check=False,
        )
        refused = w.aghub("be", "claim", "T-003", agent="be-1", check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("smoke (last run failed)", refused.stderr)

    def test_generated_mock_serves_contract_routes(self):
        w = self.world
        self.bootstrap()
        out = w.fe / "generated"
        w.aghub("fe", "codegen", "api", "1", "--out", str(out), agent="fe-1")
        port = free_port()
        proc = subprocess.Popen(
            [sys.executable, str(out / "api_mock.py"), "--port", str(port)],
            cwd=str(w.fe),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            response = None
            for _ in range(60):
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/items", timeout=1
                    ) as handle:
                        response = json.loads(handle.read().decode())
                    break
                except OSError:
                    time.sleep(0.05)
            self.assertEqual(response, {"ok": True})
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/items/42", timeout=2
            ) as handle:
                self.assertEqual(json.loads(handle.read().decode()), {"ok": True})
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    # -- integration -----------------------------------------------------

    def test_two_agents_merge_serially(self):
        w = self.world
        self.bootstrap()
        w.aghub("fe", "claim", "T-002", agent="fe-1")
        git(w.fe, "checkout", "-q", "agent/fe-1/T-002")
        w.commit_file("fe", "src/web/api.ts", 'ROUTES = ["/items", "/items/{id}"]\n', "web")
        w.aghub("fe", "handoff", "T-002", "--next", "done", agent="fe-1")
        w.aghub("fe", "submit", "T-002", agent="fe-1")

        w.aghub("be", "claim", "T-001", agent="be-1")
        git(w.be, "checkout", "-q", "agent/be-1/T-001")
        w.commit_file("be", "src/api/items.py", 'ROUTES = ["/items", "/items/{id}"]\n', "api")
        w.aghub("be", "handoff", "T-001", "--next", "done", agent="be-1")
        w.aghub("be", "submit", "T-001", agent="be-1")

        w.aghub("ci", "init", "--project", "test", agent="ci")
        out = w.aghub("ci", "queue", "run", agent="ci")
        self.assertEqual(out.stdout.count("merged"), 2, out.stdout)

        state = json.loads(w.aghub("ci", "status", "--json", agent="ci").stdout)
        self.assertTrue(all(task["effective_status"] == "done" for task in state["tasks"]))
        log = git(w.ci, "log", "--oneline", "origin/main").stdout
        self.assertIn("merge T-001", log)
        self.assertIn("merge T-002", log)

    def test_queue_rejects_outside_claim(self):
        w = self.world
        self.bootstrap()
        w.aghub("be", "claim", "T-001", agent="be-1")
        git(w.be, "checkout", "-q", "agent/be-1/T-001")
        w.commit_file("be", "src/web/evil.ts", "// wrong lane\n", "wrong lane")
        w.aghub("be", "submit", "T-001", agent="be-1")
        w.aghub("ci", "init", "--project", "test", agent="ci")
        out = w.aghub("ci", "queue", "run", agent="ci")
        self.assertIn("REJECTED", out.stdout)
        self.assertIn("src/web/evil.ts", out.stdout)
        log = git(w.ci, "log", "--oneline", "origin/main").stdout
        self.assertNotIn("merge T-001", log)

    def test_queue_rejects_failed_check(self):
        w = self.world
        self.bootstrap()
        w.aghub("be", "claim", "T-001", agent="be-1")
        git(w.be, "checkout", "-q", "agent/be-1/T-001")
        w.commit_file("be", "src/api/items.py", 'ROUTES = ["/items", "/items/{id}", "/health"]\n', "health")
        w.aghub("be", "submit", "T-001", agent="be-1")
        w.aghub("ci", "init", "--project", "test", agent="ci")
        out = w.aghub("ci", "queue", "run", agent="ci")
        self.assertIn("check failed", out.stdout)
        self.assertIn("/health", out.stdout)

    def test_queue_failure_creates_assigned_trace_task(self):
        w = self.world
        self.bootstrap()
        w.aghub("be", "claim", "T-001", agent="be-1")
        git(w.be, "checkout", "-q", "agent/be-1/T-001")
        w.commit_file("be", "src/api/items.py", 'ROUTES = ["/items", "/items/{id}", "/health"]\n', "health")
        w.aghub("be", "submit", "T-001", agent="be-1")
        w.aghub("ci", "init", "--project", "test", agent="ci")

        out = w.aghub("ci", "queue", "run", agent="ci")
        self.assertIn("trace T-003", out.stdout)
        show = w.aghub("ci", "task", "show", "T-003", agent="ci").stdout
        for needle in (
            "kind: check",
            "exit_code: 1",
            "/health",
            "assigned_to: be-1",
            "task: T-001",
            "attempts: 1",
        ):
            self.assertIn(needle, show)

        self.assertIn("T-003", w.aghub("be", "next", agent="be-1").stdout)
        self.assertNotIn("T-003", w.aghub("fe", "next", agent="fe-1").stdout)

        # A second failed attempt refreshes the same trace task.
        w.aghub("be", "submit", "T-001", agent="be-1")
        again = w.aghub("ci", "queue", "run", agent="ci")
        self.assertIn("trace T-003", again.stdout)
        self.assertNotIn("T-004", w.aghub("ci", "task", "list", agent="ci").stdout)
        self.assertIn(
            "attempts: 2", w.aghub("ci", "task", "show", "T-003", agent="ci").stdout
        )

    def test_trace_task_can_be_stolen_after_assignee_expires(self):
        w = self.world
        self.bootstrap()
        w.aghub("be", "claim", "T-001", agent="be-1")
        git(w.be, "checkout", "-q", "agent/be-1/T-001")
        w.commit_file("be", "src/api/items.py", 'ROUTES = ["/items", "/items/{id}", "/health"]\n', "health")
        w.aghub("be", "submit", "T-001", agent="be-1")
        w.aghub("ci", "init", "--project", "test", agent="ci")
        w.aghub("ci", "queue", "run", agent="ci")

        w.aghub("ci", "debug", "expire", "be-1", agent="ci")
        self.assertIn("T-003", w.aghub("fe", "next", agent="fe-1").stdout)

        refused = w.aghub("fe", "claim", "T-003", agent="fe-1", check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("assigned to be-1", refused.stderr)
        self.assertIn("--steal", refused.stderr)

        w.aghub("fe", "claim", "T-003", "--steal", agent="fe-1")
        state = json.loads(w.aghub("fe", "status", "--json", agent="fe-1").stdout)
        task = next(item for item in state["tasks"] if item["id"] == "T-003")
        self.assertEqual(task["owner"], "fe-1")
        self.assertIsNone(task.get("assigned_to"))

    def test_successful_resubmit_closes_trace_task(self):
        w = self.world
        self.bootstrap()
        w.aghub("be", "claim", "T-001", agent="be-1")
        git(w.be, "checkout", "-q", "agent/be-1/T-001")
        w.commit_file("be", "src/api/items.py", 'ROUTES = ["/items", "/items/{id}", "/health"]\n', "health")
        w.aghub("be", "submit", "T-001", agent="be-1")
        w.aghub("ci", "init", "--project", "test", agent="ci")
        w.aghub("ci", "queue", "run", agent="ci")

        w.commit_file("be", "src/api/items.py", 'ROUTES = ["/items", "/items/{id}"]\n', "fix health")
        w.aghub("be", "submit", "T-001", agent="be-1")
        merged = w.aghub("ci", "queue", "run", agent="ci")
        self.assertIn("merged", merged.stdout)

        show = w.aghub("ci", "task", "show", "T-003", agent="ci").stdout
        self.assertIn("status: done", show)
        self.assertIn("source T-001 merged", show)
        state = json.loads(w.aghub("ci", "status", "--json", agent="ci").stdout)
        tasks = {task["id"]: task for task in state["tasks"]}
        self.assertEqual(tasks["T-001"]["effective_status"], "done")
        self.assertEqual(tasks["T-003"]["effective_status"], "done")

    def test_signed_handoff_verifies_and_tampering_is_detected(self):
        w = self.world
        self.bootstrap()
        w.aghub("fe", "claim", "T-001", agent="fe-1")
        w.aghub(
            "fe",
            "handoff",
            "T-001",
            "--next",
            "wire the client",
            "--note",
            "ready for review",
            agent="fe-1",
        )

        state = json.loads(w.aghub("fe", "status", "--json", agent="fe-1").stdout)
        record = state["handoffs"][-1]
        self.assertEqual(record["attestation"]["scheme"], "ed25519")
        self.assertEqual(record["attestation"]["signer"], "fe-1")

        verified = w.aghub("fe", "verify", agent="fe-1")
        self.assertIn("event chain  ok", verified.stdout)
        self.assertIn("0 invalid", verified.stdout)

        board = Board.discover(cwd=str(w.fe))
        config_path = Path(board.common) / "aghub.local.json"
        config = json.loads(config_path.read_text())
        config["remote"] = None
        config_path.write_text(json.dumps(config))

        path = Path(board.dir) / "handoffs" / f"{record['id']}.yaml"
        path.write_text(path.read_text().replace("ready for review", "edited later"))

        tampered = w.aghub("fe", "verify", agent="fe-1", check=False)
        self.assertEqual(tampered.returncode, 1)
        self.assertIn(f"INVALID handoff {record['id']}", tampered.stdout)

    def test_event_chain_tampering_is_rejected(self):
        w = self.world
        self.bootstrap()
        board = Board.discover(cwd=str(w.fe))
        config_path = Path(board.common) / "aghub.local.json"
        config = json.loads(config_path.read_text())
        config["remote"] = None
        config_path.write_text(json.dumps(config))

        events = sorted((Path(board.dir) / "events").glob("*.yaml"))
        self.assertTrue(events)
        target = next(
            (path for path in events if "title: API" in path.read_text()),
            None,
        )
        self.assertIsNotNone(target)
        target.write_text(target.read_text().replace("title: API", "title: TAMPERED", 1))

        proc = w.aghub("fe", "verify", agent="fe-1", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("hash mismatch", proc.stdout)

    def test_acl_roles_are_enforced(self):
        w = self.world
        self.bootstrap()
        w.aghub("fe", "acl", "init", agent="fe-1")
        last_admin = w.aghub(
            "fe", "acl", "set", "fe-1", "reader", agent="fe-1", check=False
        )
        self.assertEqual(last_admin.returncode, 1)
        self.assertIn("last admin", last_admin.stderr)
        w.aghub("fe", "acl", "set", "be-1", "writer", agent="fe-1")
        w.aghub("be", "claim", "T-002", agent="be-1")

        roles = w.aghub("fe", "acl", "list", agent="fe-1").stdout
        self.assertIn("fe-1", roles)
        self.assertIn("be-1", roles)
        self.assertIn("admin", roles)
        self.assertIn("writer", roles)

        w.aghub("fe", "acl", "set", "ci", "reader", agent="fe-1")
        listed = w.aghub("fe", "acl", "list", agent="fe-1").stdout
        self.assertRegex(listed, r"ci\s+reader")
        read_only = w.aghub(
            "fe", "task", "add", "Sneaky", "--paths", "docs/**", agent="ci", check=False
        )
        self.assertEqual(read_only.returncode, 1)
        self.assertIn("writer role required", read_only.stderr)

        w.aghub("fe", "acl", "remove", "be-1", agent="fe-1")
        removed = w.aghub("fe", "acl", "list", agent="fe-1").stdout
        self.assertNotIn("be-1", removed)
        refused = w.aghub("be", "claim", "T-001", agent="be-1", check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("writer role required", refused.stderr)

        denied = w.aghub("be", "acl", "set", "ci", "admin", agent="be-1", check=False)
        self.assertEqual(denied.returncode, 1)
        self.assertIn("admin role required", denied.stderr)

        contract = w.aghub(
            "be",
            "contract",
            "freeze",
            "api",
            "2",
            "--from",
            "contracts/api.yaml",
            agent="be-1",
            check=False,
        )
        self.assertEqual(contract.returncode, 1)
        self.assertIn("writer role required", contract.stderr)

        queue = w.aghub("be", "queue", "run", agent="be-1", check=False)
        self.assertEqual(queue.returncode, 1)
        self.assertIn("writer role required", queue.stderr)

    def test_revoked_eval_key_cannot_sign_again_until_rotation(self):
        w = self.world
        self.bootstrap()
        first = w.aghub(
            "fe",
            "eval",
            "run",
            "smoke",
            "--command",
            "python3 -c 'print(1)'",
            agent="fe-1",
        )
        eval_id = first.stdout.split()[1]

        w.aghub("fe", "agent", "--revoke-key", agent="fe-1")
        invalid = w.aghub("fe", "eval", "verify", eval_id, agent="fe-1", check=False)
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("INVALID", invalid.stderr)

        blocked = w.aghub(
            "fe",
            "eval",
            "run",
            "smoke",
            "--command",
            "python3 -c 'print(1)'",
            agent="fe-1",
            check=False,
        )
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("was revoked", blocked.stderr)

        w.aghub("fe", "agent", "--rotate-key", agent="fe-1")
        again = w.aghub(
            "fe",
            "eval",
            "run",
            "smoke",
            "--command",
            "python3 -c 'print(1)'",
            agent="fe-1",
        )
        self.assertIn("passed", again.stdout)

    def test_approval_gate_requires_a_different_signed_identity(self):
        w = self.world
        self.bootstrap()
        w.aghub(
            "fe",
            "task",
            "add",
            "Ship API",
            "--paths",
            "src/api/**",
            "--requires-approval",
            agent="fe-1",
        )
        w.aghub("be", "claim", "T-003", agent="be-1")
        git(w.be, "checkout", "-q", "agent/be-1/T-003")
        w.commit_file("be", "src/api/items.py", "print('ok')\n", "api")
        git(w.be, "push", "-qu", "origin", "agent/be-1/T-003")
        head = git(w.be, "rev-parse", "HEAD").stdout.strip()

        gated = w.aghub("be", "submit", "T-003", agent="be-1", check=False)
        self.assertEqual(gated.returncode, 1)
        self.assertIn("requires a human approval", gated.stderr)

        self_approve = w.aghub("be", "approve", "T-003", agent="be-1", check=False)
        self.assertEqual(self_approve.returncode, 1)
        self.assertIn("owns T-003", self_approve.stderr)

        w.aghub("fe", "approve", "T-003", "--note", "looks good", agent="fe-1")
        w.aghub("be", "submit", "T-003", agent="be-1")

        state = json.loads(w.aghub("fe", "status", "--json", agent="fe-1").stdout)
        approval = state["approvals"][-1]
        self.assertEqual(approval["agent"], "fe-1")
        self.assertEqual(approval["binding"], "commit")
        self.assertEqual(approval["commit"], head)
        self.assertEqual(approval["attestation"]["scheme"], "ed25519")
        verified = w.aghub("fe", "verify", agent="fe-1")
        self.assertEqual(verified.returncode, 0)
        self.assertIn("0 invalid", verified.stdout)

    def test_queue_enforces_protected_path_group(self):
        w = self.world
        self.bootstrap()
        # Protect the API lane: any diff under src/api/** needs the security
        # group, whose only member is the ci identity.
        w.aghub(
            "fe",
            "policy",
            "set",
            "--group",
            "security=ci",
            "--protect",
            "src/api/**:group=security",
            agent="fe-1",
        )
        w.aghub(
            "fe",
            "task",
            "add",
            "Ship API",
            "--paths",
            "src/api/**",
            "--requires-approval",
            agent="fe-1",
        )
        w.aghub("be", "claim", "T-003", agent="be-1")
        git(w.be, "checkout", "-q", "agent/be-1/T-003")
        w.commit_file(
            "be", "src/api/items.py", 'ROUTES = ["/items", "/items/{id}"]\n', "api"
        )
        git(w.be, "push", "-qu", "origin", "agent/be-1/T-003")

        # A general reviewer satisfies the base count but not the protected rule.
        w.aghub("fe", "approve", "T-003", "--note", "looks fine", agent="fe-1")
        gated = w.aghub("be", "submit", "T-003", agent="be-1", check=False)
        self.assertEqual(gated.returncode, 1)
        self.assertIn("security", gated.stderr)

        packet = json.loads(
            w.aghub("fe", "review", "T-003", "--json", agent="fe-1").stdout
        )
        rule = packet["approval"]["protected"][0]
        self.assertTrue(rule["triggered"])
        self.assertEqual(rule["matched"], ["src/api/items.py"])

        # Force it past the advisory submit gate; the queue is the authority.
        w.aghub("be", "submit", "T-003", "--force", agent="be-1")
        w.aghub("ci", "init", "--project", "test", agent="ci")
        rejected = w.aghub("ci", "queue", "run", agent="ci")
        self.assertIn("REJECTED", rejected.stdout)
        self.assertIn("security", rejected.stdout)
        self.assertNotIn("merge T-003", git(w.ci, "log", "--oneline", "origin/main").stdout)

        # The security reviewer clears the protected rule; the merge proceeds.
        w.aghub("ci", "approve", "T-003", "--note", "security ok", agent="ci")
        w.aghub("be", "submit", "T-003", agent="be-1")
        merged = w.aghub("ci", "queue", "run", agent="ci")
        self.assertIn("merged", merged.stdout)
        self.assertIn("merge T-003", git(w.ci, "log", "--oneline", "origin/main").stdout)

    def test_policy_show_round_trips_protected_and_sla(self):
        w = self.world
        self.bootstrap()
        w.aghub(
            "fe",
            "policy",
            "set",
            "--sla",
            "3600",
            "--group",
            "db=be-1",
            "--protect",
            "migrations/**,**/schema.sql:group=db,approvals=2",
            agent="fe-1",
        )
        policy = json.loads(w.aghub("fe", "policy", "show", "--json", agent="fe-1").stdout)
        self.assertEqual(policy["sla_seconds"], 3600)
        self.assertEqual(len(policy["protected_paths"]), 1)
        rule = policy["protected_paths"][0]
        self.assertEqual(rule["paths"], ["migrations/**", "**/schema.sql"])
        self.assertEqual(rule["groups"], ["db"])
        self.assertEqual(rule["approvals"], 2)

        w.aghub("fe", "policy", "set", "--no-sla", "--clear-protected", agent="fe-1")
        cleared = json.loads(w.aghub("fe", "policy", "show", "--json", agent="fe-1").stdout)
        self.assertIsNone(cleared["sla_seconds"])
        self.assertEqual(cleared["protected_paths"], [])

    def test_review_inbox_and_changes_requested_gate(self):
        w = self.world
        self.bootstrap()
        w.aghub(
            "fe",
            "task",
            "add",
            "Ship API",
            "--paths",
            "src/api/**",
            "--requires-approval",
            agent="fe-1",
        )
        w.aghub("be", "claim", "T-003", agent="be-1")
        git(w.be, "checkout", "-q", "agent/be-1/T-003")
        w.commit_file("be", "src/api/items.py", "print('ok')\n", "api")
        git(w.be, "push", "-qu", "origin", "agent/be-1/T-003")

        inbox = json.loads(w.aghub("fe", "inbox", "--json", agent="fe-1").stdout)
        approval = next(item for item in inbox["items"] if item["task"] == "T-003")
        self.assertEqual(approval["kind"], "approval")
        self.assertIn("requires a human approval", approval["detail"])

        changes = w.aghub(
            "fe",
            "review",
            "T-003",
            "--request-changes",
            "--note",
            "missing tests",
            agent="fe-1",
        )
        self.assertIn("requested changes on T-003", changes.stdout)

        gated = w.aghub("be", "submit", "T-003", agent="be-1", check=False)
        self.assertEqual(gated.returncode, 1)
        self.assertIn("has changes requested by fe-1", gated.stderr)

        inbox = json.loads(w.aghub("fe", "inbox", "--json", agent="fe-1").stdout)
        change_item = next(item for item in inbox["items"] if item["task"] == "T-003")
        self.assertEqual(change_item["kind"], "changes")
        self.assertIn("missing tests", change_item["detail"])

        w.aghub(
            "fe", "review", "T-003", "--approve", "--note", "tests look good", agent="fe-1"
        )
        approved = json.loads(w.aghub("fe", "review", "T-003", "--json", agent="fe-1").stdout)
        self.assertEqual(approved["approval"]["decision"], "approved")
        self.assertEqual(approved["approval"]["reviewer"], "fe-1")
        self.assertEqual(len(approved["approval"]["reviews"]), 2)

        w.aghub("be", "submit", "T-003", agent="be-1")
        state = json.loads(w.aghub("fe", "status", "--json", agent="fe-1").stdout)
        decisions = [item["decision"] for item in state["approvals"] if item["task"] == "T-003"]
        self.assertEqual(decisions, ["changes_requested", "approved"])

        clear = w.aghub("fe", "inbox", "--check", agent="fe-1")
        self.assertEqual(clear.returncode, 0)
        verified = w.aghub("fe", "verify", agent="fe-1")
        self.assertEqual(verified.returncode, 0)
        self.assertIn("0 invalid", verified.stdout)

    def test_web_review_worker_signs_an_intent_once(self):
        w = self.world
        self.bootstrap()
        server, store = build_server(
            "127.0.0.1",
            0,
            ":memory:",
            tokens={"tok": {"role": "admin", "name": "fe-1"}},
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            w.aghub(
                "fe",
                "task",
                "add",
                "Ship API",
                "--paths",
                "src/api/**",
                "--requires-approval",
                agent="fe-1",
            )
            w.aghub("fe", "hub", "connect", base, "--token", "tok", agent="fe-1")
            w.aghub("be", "hub", "connect", base, "--token", "tok", agent="be-1")
            w.aghub("be", "claim", "T-003", agent="be-1")
            git(w.be, "checkout", "-q", "agent/be-1/T-003")
            w.commit_file("be", "src/api/items.py", "print('ok')\n", "api")
            git(w.be, "push", "-qu", "origin", "agent/be-1/T-003")
            head = git(w.be, "rev-parse", "HEAD").stdout.strip()

            published = w.aghub(
                "be", "review", "T-003", "--publish", agent="be-1"
            )
            self.assertIn("published review packet", published.stdout)
            packet = hubclient.review_packet(base, "test", "T-003", token="tok")
            self.assertEqual(packet["head"], head)
            self.assertIn("src/api/items.py", packet["packet"]["changes"]["patch"])

            created = hubclient.request_review(
                base,
                "test",
                "T-003",
                "approved",
                note="looks good on the web",
                head=head,
                token="tok",
            )["request"]
            self.assertEqual(created["reviewer"], "fe-1")

            applied = w.aghub(
                "fe", "review", "--watch", "--once", "--json", agent="fe-1"
            )
            results = json.loads(applied.stdout)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["status"], "applied")
            self.assertEqual(results[0]["record"]["agent"], "fe-1")

            state = json.loads(w.aghub("fe", "status", "--json", agent="fe-1").stdout)
            approval = [item for item in state["approvals"] if item["task"] == "T-003"][-1]
            self.assertEqual(approval["agent"], "fe-1")
            self.assertEqual(approval["commit"], head)
            self.assertEqual(approval["external_id"], created["id"])
            self.assertEqual(approval["binding"], "commit")

            # A second poll cannot apply the same request twice.
            again = w.aghub(
                "fe", "review", "--watch", "--once", "--json", agent="fe-1"
            )
            self.assertEqual(json.loads(again.stdout), [])
            resolved = hubclient.review_requests(
                base, "test", status="all", token="tok"
            )["requests"][-1]
            self.assertEqual(resolved["status"], "applied")
            self.assertEqual(resolved["record_id"], approval["id"])

            # The signed approval is enough to submit the branch.
            w.aghub("be", "submit", "T-003", agent="be-1")
        finally:
            server.shutdown()
            server.server_close()
            store.close()

    def test_commit_bound_approval_goes_stale_and_can_be_reapproved(self):
        w = self.world
        self.bootstrap()
        w.aghub(
            "fe",
            "task",
            "add",
            "Ship API",
            "--paths",
            "src/api/**",
            "--requires-approval",
            agent="fe-1",
        )
        w.aghub("be", "claim", "T-003", agent="be-1")
        branch = "agent/be-1/T-003"
        git(w.be, "checkout", "-q", branch)
        w.commit_file(
            "be",
            "src/api/items.py",
            '# one\nROUTES = ["/items", "/items/{id}"]\n',
            "api one",
        )
        git(w.be, "push", "-qu", "origin", branch)
        first = git(w.be, "rev-parse", "HEAD").stdout.strip()

        w.aghub("fe", "review", "T-003", "--approve", "--note", "first pass", agent="fe-1")
        w.aghub("be", "submit", "T-003", agent="be-1")

        w.commit_file(
            "be",
            "src/api/items.py",
            '# two\nROUTES = ["/items", "/items/{id}"]\n',
            "api two",
        )
        git(w.be, "push", "-qu", "origin", branch)
        second = git(w.be, "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(first, second)

        packet = json.loads(
            w.aghub("fe", "review", "T-003", "--json", agent="fe-1").stdout
        )
        self.assertTrue(packet["approval"]["stale"])
        self.assertEqual(packet["approval"]["head"], second)
        self.assertTrue(
            any("aghub review T-003 --approve" in item for item in packet["actions"])
        )

        stale = w.aghub("be", "submit", "T-003", agent="be-1", check=False)
        self.assertEqual(stale.returncode, 1)
        self.assertIn("approval is for", stale.stderr)

        w.aghub("ci", "init", "--project", "test", agent="ci")
        queue = w.aghub("ci", "queue", "run", agent="ci")
        self.assertIn("approval is for", queue.stdout)
        self.assertIn("REJECTED", queue.stdout)

        inbox = json.loads(w.aghub("fe", "inbox", "--json", agent="fe-1").stdout)
        approval = next(
            item
            for item in inbox["items"]
            if item["task"] == "T-003" and item["kind"] == "approval"
        )
        self.assertIn("re-approve the current head", approval["detail"])

        w.aghub(
            "fe", "review", "T-003", "--approve", "--note", "second pass", agent="fe-1"
        )
        state = json.loads(w.aghub("fe", "status", "--json", agent="fe-1").stdout)
        records = [item for item in state["approvals"] if item["task"] == "T-003"]
        self.assertEqual(records[-1]["commit"], second)
        self.assertEqual(records[-1]["binding"], "commit")

        w.aghub("be", "submit", "T-003", agent="be-1")
        merged = w.aghub("ci", "queue", "run", agent="ci")
        self.assertIn("merged", merged.stdout)

    def test_board_review_policy_requires_distinct_reviewers_and_a_group(self):
        w = self.world
        self.bootstrap()
        w.aghub("ci", "init", "--project", "test", agent="ci")
        w.aghub(
            "fe",
            "policy",
            "set",
            "--approvals",
            "2",
            "--ttl",
            "3600",
            "--group",
            "backend=fe-1,ci",
            "--require-group",
            "backend",
            "--github",
            "alice=fe-1",
            agent="fe-1",
        )
        policy = json.loads(
            w.aghub("fe", "policy", "show", "--json", agent="fe-1").stdout
        )
        self.assertEqual(policy["approvals"], 2)
        self.assertEqual(policy["ttl_seconds"], 3600)
        self.assertEqual(policy["required_groups"], ["backend"])
        self.assertEqual(policy["github"], {"alice": "fe-1"})

        w.aghub(
            "fe",
            "task",
            "add",
            "Billing deploy",
            "--paths",
            "src/api/**",
            "--required-approvals",
            "2",
            "--review-ttl",
            "7200",
            agent="fe-1",
        )
        w.aghub("be", "claim", "T-003", agent="be-1")
        git(w.be, "checkout", "-q", "agent/be-1/T-003")
        w.commit_file(
            "be",
            "src/api/billing.py",
            'ROUTES = ["/items", "/items/{id}"]\n',
            "billing",
        )
        git(w.be, "push", "-qu", "origin", "agent/be-1/T-003")

        gated = w.aghub("be", "submit", "T-003", agent="be-1", check=False)
        self.assertEqual(gated.returncode, 1)
        self.assertIn("approval", gated.stderr)

        w.aghub("fe", "review", "T-003", "--approve", agent="fe-1")
        packet = json.loads(
            w.aghub("fe", "review", "T-003", "--json", agent="fe-1").stdout
        )
        approval = packet["approval"]
        self.assertFalse(approval["satisfied"])
        self.assertEqual(approval["required_approvals"], 2)
        self.assertEqual(approval["approved_count"], 1)
        self.assertEqual(approval["missing_approvals"], 1)
        self.assertEqual(approval["required_groups"], ["backend"])
        self.assertEqual(approval["missing_groups"], [])

        state = json.loads(w.aghub("fe", "status", "--json", agent="fe-1").stdout)
        self.assertEqual(state["project"]["review"]["approvals"], 2)
        self.assertEqual(state["project"]["review"]["required_groups"], ["backend"])

        still = w.aghub("be", "submit", "T-003", agent="be-1", check=False)
        self.assertEqual(still.returncode, 1)
        self.assertIn("still need 1 more", still.stderr)

        w.aghub("ci", "review", "T-003", "--approve", agent="ci")
        packet = json.loads(
            w.aghub("fe", "review", "T-003", "--json", agent="fe-1").stdout
        )
        self.assertTrue(packet["approval"]["satisfied"])
        self.assertEqual(packet["approval"]["approved_count"], 2)

        board = Board.discover(w.fe)
        snap = render.snapshot(board.load(), board)
        self.assertEqual(snap["review_policy"]["approvals"], 2)
        task = next(item for item in snap["tasks"] if item["id"] == "T-003")
        self.assertTrue(task["review_state"]["satisfied"])
        self.assertEqual(task["review_state"]["approved_count"], 2)

        w.aghub("be", "submit", "T-003", agent="be-1")
        merged = w.aghub("ci", "queue", "run", agent="ci")
        self.assertIn("merged", merged.stdout)

    def test_github_sync_records_trusted_review_once(self):
        w = self.world
        self.bootstrap()
        w.aghub(
            "fe",
            "task",
            "add",
            "Ship API",
            "--paths",
            "src/api/**",
            "--requires-approval",
            agent="fe-1",
        )
        w.aghub(
            "fe", "policy", "set", "--github", "alice=fe-1", agent="fe-1"
        )
        w.aghub("be", "claim", "T-003", agent="be-1")
        branch = "agent/be-1/T-003"
        git(w.be, "checkout", "-q", branch)
        w.commit_file("be", "src/api/items.py", "print('ok')\n", "api")
        git(w.be, "push", "-qu", "origin", branch)
        head = git(w.be, "rev-parse", "HEAD").stdout.strip()
        event = {
            "action": "submitted",
            "number": 7,
            "pull_request": {
                "number": 7,
                "title": "Ship API",
                "body": "",
                "html_url": "https://github.com/acme/repo/pull/7",
                "draft": False,
                "user": {"login": "alice"},
                "head": {
                    "ref": branch,
                    "sha": head,
                    "repo": {"full_name": "acme/repo"},
                },
                "base": {"ref": "main", "sha": "b" * 40},
            },
            "review": {
                "id": 9001,
                "state": "approved",
                "user": {"login": "alice"},
                "author_association": "MEMBER",
                "body": "ship it",
            },
        }
        event_path = Path(w.base) / "review-event.json"
        event_path.write_text(json.dumps(event))

        first = w.aghub(
            "fe",
            "github",
            "sync",
            "--event",
            str(event_path),
            "--repo",
            "acme/repo",
            "--record-reviews",
            "--reviewer-agent",
            "github-review",
            "--no-comment",
            "--no-check",
            agent="fe-1",
        )
        self.assertIn("recorded GitHub approved", first.stdout)

        state = json.loads(w.aghub("fe", "status", "--json", agent="fe-1").stdout)
        records = [item for item in state["approvals"] if item["task"] == "T-003"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source"], "github")
        self.assertEqual(records[0]["external_id"], "9001")
        self.assertEqual(records[0]["agent"], "github-review")
        self.assertEqual(records[0]["reviewer"], "fe-1")
        self.assertEqual(records[0]["github_login"], "alice")
        self.assertEqual(records[0]["binding"], "commit")
        self.assertEqual(records[0]["commit"], head)
        self.assertIn("alice", records[0]["note"])

        second = w.aghub(
            "fe",
            "github",
            "sync",
            "--event",
            str(event_path),
            "--repo",
            "acme/repo",
            "--record-reviews",
            "--reviewer-agent",
            "github-review",
            "--no-comment",
            "--no-check",
            agent="fe-1",
        )
        self.assertIn("already recorded", second.stdout)
        state = json.loads(w.aghub("fe", "status", "--json", agent="fe-1").stdout)
        self.assertEqual(
            len([item for item in state["approvals"] if item["task"] == "T-003"]), 1
        )

        event["pull_request"]["head"]["sha"] = "e" * 40
        event_path.write_text(json.dumps(event))
        stale = w.aghub(
            "fe",
            "github",
            "sync",
            "--event",
            str(event_path),
            "--repo",
            "acme/repo",
            "--record-reviews",
            "--dry-run",
            "--json",
            agent="fe-1",
        )
        payload = json.loads(stale.stdout)
        self.assertTrue(payload["approval"]["stale"])
        self.assertEqual(payload["check"]["conclusion"], "action_required")
        self.assertIn("older commit", payload["check"]["output"]["summary"])

        w.aghub("be", "submit", "T-003", agent="be-1")

    def test_github_dismissed_review_retracts_the_approval(self):
        w = self.world
        self.bootstrap()
        w.aghub(
            "fe", "task", "add", "Ship API", "--paths", "src/api/**",
            "--requires-approval", agent="fe-1",
        )
        w.aghub("fe", "policy", "set", "--github", "alice=fe-1", agent="fe-1")
        w.aghub("be", "claim", "T-003", agent="be-1")
        branch = "agent/be-1/T-003"
        git(w.be, "checkout", "-q", branch)
        w.commit_file("be", "src/api/items.py", "print('ok')\n", "api")
        git(w.be, "push", "-qu", "origin", branch)
        head = git(w.be, "rev-parse", "HEAD").stdout.strip()

        def write_event(action, state="approved"):
            event = {
                "action": action,
                "number": 7,
                "repository": {"full_name": "acme/repo"},
                "pull_request": {
                    "number": 7,
                    "title": "Ship API",
                    "body": "",
                    "html_url": "https://github.com/acme/repo/pull/7",
                    "draft": False,
                    "user": {"login": "alice"},
                    "head": {"ref": branch, "sha": head, "repo": {"full_name": "acme/repo"}},
                    "base": {"ref": "main", "repo": {"full_name": "acme/repo"}},
                },
                "review": {
                    "id": 9001,
                    "state": state,
                    "commit_id": head,
                    "user": {"login": "alice"},
                    "author_association": "MEMBER",
                    "body": "ship it",
                },
            }
            path = Path(w.base) / "ev.json"
            path.write_text(json.dumps(event))
            return str(path)

        # Record the approval, then confirm the gate is satisfied.
        w.aghub(
            "fe", "github", "sync", "--event", write_event("submitted"),
            "--record-reviews", "--reviewer-agent", "github-review",
            "--no-comment", "--no-check", agent="fe-1",
        )
        packet = json.loads(w.aghub("fe", "review", "T-003", "--json", agent="fe-1").stdout)
        self.assertTrue(packet["approval"]["satisfied"])

        # Dismiss the same review; the approval should stop counting.
        dismissed = w.aghub(
            "fe", "github", "sync", "--event", write_event("dismissed"),
            "--record-reviews", "--reviewer-agent", "github-review",
            "--no-comment", "--no-check", agent="fe-1",
        )
        self.assertIn("dismissal of review 9001", dismissed.stdout)
        packet = json.loads(w.aghub("fe", "review", "T-003", "--json", agent="fe-1").stdout)
        self.assertFalse(packet["approval"]["satisfied"])
        self.assertEqual(len(packet["approval"]["dismissed"]), 1)

        gated = w.aghub("be", "submit", "T-003", agent="be-1", check=False)
        self.assertEqual(gated.returncode, 1)

    def test_github_webhook_records_review_from_signed_fixture(self):
        w = self.world
        self.bootstrap()
        w.aghub(
            "fe",
            "task",
            "add",
            "Ship API",
            "--paths",
            "src/api/**",
            "--requires-approval",
            agent="fe-1",
        )
        w.aghub(
            "fe", "policy", "set", "--github", "alice=fe-1", agent="fe-1"
        )
        w.aghub("be", "claim", "T-003", agent="be-1")
        branch = "agent/be-1/T-003"
        git(w.be, "checkout", "-q", branch)
        w.commit_file("be", "src/api/items.py", "print('ok')\n", "api")
        git(w.be, "push", "-qu", "origin", branch)
        head = git(w.be, "rev-parse", "HEAD").stdout.strip()

        fixture = ROOT / "tests" / "fixtures" / "github" / "pull_request_review.approved.json"
        event = json.loads(fixture.read_text())
        event["pull_request"]["head"]["ref"] = branch
        event["pull_request"]["head"]["sha"] = head
        event["review"]["id"] = 424242
        body = json.dumps(event).encode("utf-8")
        signature = github.webhook_signature("hook-secret", body)
        event_path = Path(w.base) / "webhook-event.json"
        event_path.write_bytes(body)

        delivered = w.aghub(
            "fe",
            "github",
            "webhook",
            "--event",
            str(event_path),
            "--signature",
            signature,
            "--secret",
            "hook-secret",
            "--repo",
            "acme/repo",
            "--reviewer-agent",
            "github-review",
            "--no-comment",
            "--no-check",
            "--json",
            agent="fe-1",
        )
        payload = json.loads(delivered.stdout)
        self.assertEqual(payload["event"], "pull_request_review")
        self.assertEqual(payload["result"]["task"], "T-003")
        self.assertEqual(payload["result"]["recorded"]["external_id"], "424242")

        state = json.loads(w.aghub("fe", "status", "--json", agent="fe-1").stdout)
        records = [item for item in state["approvals"] if item["task"] == "T-003"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source"], "github")
        self.assertEqual(records[0]["reviewer"], "fe-1")
        self.assertEqual(records[0]["commit"], head)

        again = w.aghub(
            "fe",
            "github",
            "webhook",
            "--event",
            str(event_path),
            "--signature",
            signature,
            "--secret",
            "hook-secret",
            "--repo",
            "acme/repo",
            "--reviewer-agent",
            "github-review",
            "--no-comment",
            "--no-check",
            "--json",
            agent="fe-1",
        )
        self.assertIn(
            "already recorded", " ".join(json.loads(again.stdout)["result"]["messages"])
        )
        state = json.loads(w.aghub("fe", "status", "--json", agent="fe-1").stdout)
        self.assertEqual(
            len([item for item in state["approvals"] if item["task"] == "T-003"]), 1
        )

        rejected = w.aghub(
            "fe",
            "github",
            "webhook",
            "--event",
            str(event_path),
            "--signature",
            "sha256=" + "0" * 64,
            "--secret",
            "hook-secret",
            "--repo",
            "acme/repo",
            "--no-comment",
            "--no-check",
            agent="fe-1",
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("signature", rejected.stderr.lower())

        w.aghub("be", "submit", "T-003", agent="be-1")

    def test_github_sync_refuses_untrusted_or_fork_reviews(self):
        w = self.world
        self.bootstrap()
        w.aghub(
            "fe",
            "task",
            "add",
            "Ship API",
            "--paths",
            "src/api/**",
            "--requires-approval",
            agent="fe-1",
        )
        w.aghub("be", "claim", "T-003", agent="be-1")

        def event(association="CONTRIBUTOR", head_repo="acme/repo"):
            return {
                "action": "submitted",
                "number": 8,
                "pull_request": {
                    "number": 8,
                    "title": "Ship API",
                    "body": "",
                    "html_url": "https://github.com/acme/repo/pull/8",
                    "draft": False,
                    "user": {"login": "mallory"},
                    "head": {
                        "ref": "agent/be-1/T-003",
                        "sha": "c" * 40,
                        "repo": {"full_name": head_repo},
                    },
                    "base": {"ref": "main", "sha": "d" * 40},
                },
                "review": {
                    "id": 9002,
                    "state": "approved",
                    "user": {"login": "mallory"},
                    "author_association": association,
                    "body": "approve",
                },
            }

        path = Path(w.base) / "untrusted.json"
        path.write_text(json.dumps(event()))
        refused = w.aghub(
            "fe",
            "github",
            "sync",
            "--event",
            str(path),
            "--repo",
            "acme/repo",
            "--record-reviews",
            "--no-comment",
            "--no-check",
            agent="fe-1",
            check=False,
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("is not trusted", refused.stderr)

        path.write_text(json.dumps(event(association="MEMBER", head_repo="fork/repo")))
        fork = w.aghub(
            "fe",
            "github",
            "sync",
            "--event",
            str(path),
            "--repo",
            "acme/repo",
            "--record-reviews",
            "--no-comment",
            "--no-check",
            agent="fe-1",
        )
        self.assertIn("refusing to record", fork.stderr)
        state = json.loads(w.aghub("fe", "status", "--json", agent="fe-1").stdout)
        self.assertFalse(
            [item for item in state["approvals"] if item["task"] == "T-003"]
        )

    def test_monorepo_scope_rejects_tasks_and_merges_outside_the_subtree(self):
        tmp = tempfile.mkdtemp(prefix="aghub-scope-")
        try:
            w = World(tmp)
            w.seed()
            w.aghub(
                "fe",
                "init",
                "--project",
                "mono",
                "--scope",
                "packages/api",
                agent="fe-1",
            )
            w.aghub("be", "init", "--project", "mono", agent="be-1")

            refused = w.aghub(
                "fe",
                "task",
                "add",
                "outside",
                "--paths",
                "packages/web/**",
                agent="fe-1",
                check=False,
            )
            self.assertEqual(refused.returncode, 1)
            self.assertIn("packages/web/**", refused.stderr)
            self.assertIn("packages/api", refused.stderr)

            added = w.aghub("fe", "task", "add", "inside", agent="fe-1")
            self.assertIn("packages/api/**", added.stdout)

            w.aghub("be", "claim", "T-001", agent="be-1")
            git(w.be, "checkout", "-q", "agent/be-1/T-001")
            w.commit_file("be", "packages/web/x.ts", "export {};\n", "outside scope")
            w.aghub("be", "submit", "T-001", agent="be-1")
            w.aghub("ci", "init", "--project", "mono", agent="ci")

            result = w.aghub("ci", "queue", "run", agent="ci")
            self.assertIn("outside project scope", result.stdout)
            log = git(w.ci, "log", "--oneline", "origin/main").stdout
            self.assertNotIn("merge T-001", log)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_hub_is_authoritative_for_claims(self):
        server, store = build_server(
            "127.0.0.1", 0, str(self.world.base / "hub.db"), token="secret"
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(store.close)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"

        w = self.world
        self.bootstrap()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            dead = f"http://127.0.0.1:{sock.getsockname()[1]}"
        w.aghub(
            "fe",
            "hub",
            "connect",
            url,
            dead,
            "--token",
            "secret",
            agent="fe-1",
        )
        w.aghub("be", "hub", "connect", url, "--token", "secret", agent="be-1")
        status = w.aghub("fe", "hub", "status", agent="fe-1")
        self.assertIn("primary", status.stdout)
        self.assertIn("failover 1", status.stdout)
        self.assertIn("unreachable", status.stdout)

        w.aghub("fe", "claim", "T-001", agent="fe-1")
        data = hubclient.board(url, "test", token="secret")
        self.assertEqual([lease["task"] for lease in data["leases"]], ["T-001"])

        # A lease the git board knows nothing about. The hub must still win.
        hubclient.claim(url, "test", "T-999", "be-1", ["src/web/**"], 600, token="secret")
        w.aghub("fe", "release", "T-001", agent="fe-1")

        refused = w.aghub("fe", "claim", "T-002", agent="fe-1", check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("hub rejected", refused.stderr)

        export = self.world.base / "audit.jsonl"
        wrote = w.aghub(
            "fe", "hub", "audit", "--out", str(export), agent="fe-1"
        )
        self.assertIn("records", wrote.stdout)
        rows = [json.loads(line) for line in export.read_text().splitlines()]
        self.assertTrue(rows)
        self.assertTrue(all(row["actor"] == "admin" for row in rows))
        self.assertIn("lease.claim", {row["type"] for row in rows})

        state = json.loads(w.aghub("fe", "status", "--json", agent="fe-1").stdout)
        task = next(item for item in state["tasks"] if item["id"] == "T-002")
        self.assertIsNone(task["owner"])
        self.assertEqual(state["hub"], "live")

    def test_protected_paths_condition_the_approval_on_the_patch(self):
        w = self.world
        w.aghub("fe", "init", "--project", "gate", "--check", "python3 check.py", agent="fe-1")
        w.aghub("fe", "acl", "init", agent="fe-1")
        w.aghub("fe", "acl", "set", "sec-1", "admin", agent="fe-1")
        w.aghub("be", "init", "--project", "gate", agent="be-1")
        w.aghub("fe", "acl", "set", "be-1", "writer", agent="fe-1")
        w.aghub(
            "fe", "contract", "freeze", "api", "1",
            "--from", "contracts/api.yaml", agent="fe-1",
        )
        w.aghub(
            "fe",
            "policy",
            "set",
            "--group",
            "security=fe-1",
            "--reviewer",
            "fe-1",
            "--reviewer",
            "sec-1",
            "--protect",
            "src/api/secret/**=security",
            agent="fe-1",
        )
        policy = json.loads(
            w.aghub("fe", "policy", "show", "--json", agent="fe-1").stdout
        )
        self.assertEqual(
            policy["protected_paths"],
            [{"paths": ["src/api/secret/**"], "groups": ["security"]}],
        )

        w.aghub(
            "fe",
            "task",
            "add",
            "Rotate secrets",
            "--paths",
            "src/api/**",
            "--requires-approval",
            agent="fe-1",
        )
        w.aghub("be", "claim", "T-001", agent="be-1")
        branch = "agent/be-1/T-001"
        git(w.be, "checkout", "-q", branch)
        w.commit_file("be", "src/api/secret/keys.py", "KEYS = {}\n", "keys")
        git(w.be, "push", "-qu", "origin", branch)

        # Before the push the diff is unknown, so the gate is plain policy.
        blocked = w.aghub("be", "submit", "T-001", agent="be-1", check=False)
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("requires a human approval", blocked.stderr)

        w.aghub(
            "fe", "review", "T-001", "--approve", "--note", "keys look fine",
            agent="fe-1",
        )
        still_short = w.aghub("be", "submit", "T-001", agent="be-1", check=False)
        self.assertEqual(still_short.returncode, 1)
        self.assertIn("requires 2 approvals", still_short.stderr)

        w.aghub(
            "fe",
            "task",
            "add",
            "Plain change",
            "--paths",
            "src/api/**",
            "--requires-approval",
            agent="fe-1",
        )
        w.aghub("be", "claim", "T-002", agent="be-1")
        git(w.be, "checkout", "-q", "agent/be-1/T-002")
        w.commit_file("be", "src/api/items.py", "# items\n", "items")
        git(w.be, "push", "-qu", "origin", "agent/be-1/T-002")
        w.aghub("fe", "review", "T-002", "--approve", "--note", "ok", agent="fe-1")
        plain = w.aghub("be", "submit", "T-002", agent="be-1", check=False)
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertNotIn("protected", plain.stdout + plain.stderr)

        w.aghub(
            "fe", "review", "T-001", "--approve", "--note", "lgtm", agent="sec-1"
        )
        w.aghub("be", "submit", "T-001", agent="be-1")

    def test_unprotect_removes_rules_that_mention_the_glob(self):
        w = self.world
        self.bootstrap()
        w.aghub("fe", "policy", "set", "--group", "security=fe-1", agent="fe-1")
        w.aghub(
            "fe",
            "policy",
            "set",
            "--protect",
            "src/pay/**=security",
            "--protect",
            "schema.sql,db/**:group=security",
            agent="fe-1",
        )

        def rules():
            policy = json.loads(
                w.aghub("fe", "policy", "show", "--json", agent="fe-1").stdout
            )
            return [rule["paths"] for rule in policy["protected_paths"]]

        self.assertEqual(rules(), [["src/pay/**"], ["schema.sql", "db/**"]])
        w.aghub("fe", "policy", "set", "--unprotect", "db/**", agent="fe-1")
        self.assertEqual(rules(), [["src/pay/**"]])
        w.aghub("fe", "policy", "set", "--clear-protected", agent="fe-1")
        self.assertEqual(rules(), [])

    def test_review_sla_flags_an_old_review_request(self):
        w = self.world
        self.bootstrap()
        w.aghub("fe", "policy", "set", "--sla", "3600", agent="fe-1")
        policy = json.loads(
            w.aghub("fe", "policy", "show", "--json", agent="fe-1").stdout
        )
        self.assertEqual(policy["sla_seconds"], 3600)

        w.aghub(
            "fe",
            "task",
            "add",
            "Slow review",
            "--paths",
            "src/api/**",
            "--requires-approval",
            agent="fe-1",
        )
        w.aghub("be", "claim", "T-003", agent="be-1")
        branch = "agent/be-1/T-003"
        git(w.be, "checkout", "-q", branch)
        w.commit_file("be", "src/api/items.py", "# items\n", "items")
        git(w.be, "push", "-qu", "origin", branch)
        w.aghub("fe", "review", "T-003", "--approve", "--note", "items", agent="fe-1")
        w.aghub("be", "submit", "T-003", agent="be-1")

        packet = json.loads(
            w.aghub("fe", "review", "T-003", "--json", agent="fe-1").stdout
        )
        self.assertFalse(packet["approval"]["overdue"])
        self.assertEqual(packet["approval"]["sla_seconds"], 3600)
        self.assertIsNotNone(packet["approval"]["pending_since"])

        # A protected path arrives after review, so the same submission now
        # waits for a second reviewer: that wait is what the SLA tracks.
        w.aghub(
            "fe",
            "policy",
            "set",
            "--group",
            "security=fe-1",
            "--protect",
            "src/api/secret/**",
            agent="fe-1",
        )
        git(w.be, "checkout", "-q", branch)
        w.commit_file("be", "src/api/secret/key.py", "KEY = 1\n", "key")
        git(w.be, "push", "-qu", "origin", branch)
        head = git(w.be, "rev-parse", branch).stdout.strip()

        def edit_queue(board_dir):
            """Rewrite the shared queue as a human could, then push it."""
            git(board_dir, "fetch", "-q", "origin", "aghub/board")
            git(board_dir, "reset", "-q", "--hard", "origin/aghub/board")
            queue_file = Path(board_dir) / "queue.yaml"
            queue = yaml.safe_load(queue_file.read_text())
            changed = 0
            for entry in queue["entries"]:
                if entry.get("task") != "T-003":
                    continue
                entry["submitted_commit"] = head
                entry["submission"]["commit"] = head
                entry["submitted_at"] = "2020-01-01T00:00:00Z"
                changed += 1
            self.assertEqual(changed, 1)
            queue_file.write_text(yaml.safe_dump(queue, sort_keys=False))
            git(board_dir, "commit", "-qam", "expire review request")
            git(board_dir, "push", "-q", "origin", "aghub/board")

        edit_queue(str(Path(w.fe) / ".git" / "aghub-board"))

        inbox = json.loads(w.aghub("fe", "inbox", "--json", agent="fe-1").stdout)
        overdue = [item for item in inbox["items"] if item["task"] == "T-003"]
        self.assertEqual(len(overdue), 1)
        self.assertEqual(overdue[0]["kind"], "approval")
        self.assertTrue(overdue[0]["policy"]["overdue"])
        self.assertEqual(overdue[0]["priority"], -1)
        self.assertIn("SLA", overdue[0]["detail"])

        packet = json.loads(
            w.aghub("fe", "review", "T-003", "--json", agent="fe-1").stdout
        )
        self.assertTrue(packet["approval"]["overdue"])
        self.assertGreaterEqual(packet["approval"]["pending_seconds"], 3600)
        self.assertIn("SLA", packet["approval"]["reason"])

        w.aghub("fe", "policy", "set", "--no-sla", agent="fe-1")
        packet = json.loads(
            w.aghub("fe", "review", "T-003", "--json", agent="fe-1").stdout
        )
        self.assertFalse(packet["approval"]["overdue"])
        self.assertNotIn("SLA", packet["approval"]["reason"] or "")


class PathTests(unittest.TestCase):
    def test_globs(self):
        sys.path.insert(0, PYTHONPATH)
        from aghub.paths import matches, overlaps

        self.assertTrue(matches("src/**", "src/api/items.py"))
        self.assertTrue(matches("src/*/items.py", "src/api/items.py"))
        self.assertFalse(matches("src/*/items.py", "src/api/v2/items.py"))
        self.assertTrue(overlaps("src/**", "src/web/**"))
        self.assertTrue(overlaps("src/web/**", "src/web/components/x.tsx"))
        self.assertFalse(overlaps("src/web/**", "src/api/**"))
        self.assertFalse(overlaps("src/web/**", "src/website/**"))



if __name__ == "__main__":
    unittest.main()
