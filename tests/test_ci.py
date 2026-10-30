import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aghub import ci  # noqa: E402


def make_state(queue=None, tasks=None, approvals=None):
    return SimpleNamespace(
        project={"name": "demo", "trunk": "main", "scope": "packages/web"},
        tasks=tasks or {},
        queue=queue or [],
        approvals=approvals or [],
        evals=[],
        agents={},
    )


class CiTests(unittest.TestCase):
    def test_status_reports_open_gated_and_failed_work(self):
        task = {
            "id": "T-001",
            "title": "gated",
            "status": "ready",
            "requires_approval": True,
            "paths": ["packages/web/**"],
        }
        state = make_state(
            tasks={"T-001": task},
            queue=[{"id": "Q-1", "task": "T-001", "status": "rejected"}],
        )
        payload = ci.status(state, SimpleNamespace(head="abc1234"))
        self.assertEqual(payload["project"], "demo")
        self.assertEqual(payload["scope"], "packages/web")
        self.assertEqual(payload["tasks"]["open"], 1)
        self.assertEqual(payload["queue"]["failed"], 1)
        self.assertEqual(payload["approvals"]["gated"], 1)
        self.assertFalse(payload["ok"])

    def test_workflow_contains_the_gate_steps(self):
        text = ci.workflow(project="demo")
        self.assertIn("name: aghub", text)
        self.assertIn("aghub status --json", text)
        self.assertIn("aghub verify", text)
        self.assertIn("aghub queue run", text)
        self.assertIn("aghub ci status --json", text)
        self.assertIn("git config user.name", text)
        self.assertIn("AGHUB_AGENT_KEY", text)
        self.assertIn("AGHUB_PROJECT", text)
        self.assertIn("aghub init --project", text)
        self.assertIn("contents: write", text)
        self.assertNotIn("|| true", text)
        self.assertNotIn("Codegen drift", text)

    def test_workflow_can_gate_codegen_drift(self):
        text = ci.workflow(project="demo", contract="api", codegen_out="gen")
        self.assertIn("aghub codegen api --out gen --check", text)
        self.assertNotIn("|| true", text)


if __name__ == "__main__":
    unittest.main()
