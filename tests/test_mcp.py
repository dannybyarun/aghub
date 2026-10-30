import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aghub import mcp  # noqa: E402


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


class McpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aghub-mcp-test-")
        self.repo = Path(self.tmp) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "README.md").write_text("seed\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "seed")
        previous = os.getcwd()
        os.chdir(self.repo)
        try:
            from aghub import cli

            self.assertEqual(
                cli.main(
                    ["--agent", "mcp-1", "init", "--project", "mcp-test", "--no-remote"]
                ),
                0,
            )
        finally:
            os.chdir(previous)
        self.server = mcp.Server(agent="mcp-1", cwd=str(self.repo))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rpc(self, method, params=None, request_id=1):
        return self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )

    def call(self, name, arguments=None):
        response = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        return response["result"]

    def test_initialize_and_tool_catalog(self):
        init = self.rpc("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(init["result"]["serverInfo"]["name"], "aghub")
        tools = self.rpc("tools/list")["result"]["tools"]
        names = {tool["name"] for tool in tools}
        for expected in (
            "aghub_status",
            "aghub_next",
            "aghub_claim",
            "aghub_task_add",
            "aghub_approve",
            "aghub_verify",
            "aghub_eval_run",
            "aghub_eval_list",
        ):
            self.assertIn(expected, names)
        self.assertIsNone(
            self.server.handle(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
        )

    def test_tools_create_and_read_tasks(self):
        created = self.call(
            "aghub_task_add",
            {
                "title": "MCP task",
                "paths": ["src/mcp/**"],
                "evals": ["smoke"],
                "acceptance": "works over stdio",
            },
        )
        self.assertFalse(created["isError"], created)
        self.assertIn("T-001", created["content"][0]["text"])

        status = self.call("aghub_status", {"json": True})
        self.assertFalse(status["isError"])
        payload = status["structuredContent"]
        self.assertEqual(payload["project"]["name"], "mcp-test")
        task = payload["tasks"][0]
        self.assertEqual(task["id"], "T-001")
        self.assertEqual(task["paths"], ["src/mcp/**"])

        shown = self.call("aghub_task_show", {"task": "T-001"})
        self.assertIn("MCP task", shown["content"][0]["text"])
        listing = self.call("aghub_task_list")
        self.assertIn("T-001", listing["content"][0]["text"])

    def test_resource_reads_board_status(self):
        resource = self.rpc(
            "resources/read", {"uri": "aghub://board/status"}
        )["result"]["contents"][0]
        self.assertEqual(resource["mimeType"], "application/json")
        self.assertEqual(json.loads(resource["text"])["project"]["name"], "mcp-test")

    def test_agent_native_eval_approval_and_verify(self):
        created = self.call(
            "aghub_task_add", {"title": "gated work", "requires_approval": True}
        )
        self.assertFalse(created["isError"], created)
        approved = self.call(
            "aghub_approve", {"task": "T-001", "note": "mcp approval"}
        )
        self.assertFalse(approved["isError"], approved)
        self.assertIn("approved T-001", approved["content"][0]["text"])

        ran = self.call(
            "aghub_eval_run",
            {
                "suite": "smoke",
                "command": "python3 -c 'print(1)'",
                "timeout": 30,
            },
        )
        self.assertFalse(ran["isError"], ran)
        self.assertIn("passed", ran["content"][0]["text"])

        verified = self.call("aghub_verify", {"json": True})
        self.assertFalse(verified["isError"], verified)
        payload = verified["structuredContent"]
        self.assertTrue(payload["chain"]["ok"])
        kinds = {item["kind"] for item in payload["records"]}
        self.assertIn("approval", kinds)
        self.assertIn("eval", kinds)
        self.assertTrue(all(item["status"] == "ok" for item in payload["records"]))

    def test_python_sdk_round_trip(self):
        from aghub.sdk import Agent

        with Agent("sdk-1", cwd=str(self.repo)) as agent:
            created = agent.add_task(
                "SDK task", paths=["src/sdk/**"], acceptance="works from Python"
            )
            self.assertIn("T-001", created)
            state = agent.status()
            self.assertEqual(state["project"]["name"], "mcp-test")
            self.assertEqual(state["tasks"][0]["title"], "SDK task")
            self.assertIn("T-001", agent.next_task())
            self.assertIn("ed25519", agent.key())
            self.assertTrue(agent.verify()["chain"]["ok"])
            self.assertIn("queue is empty", agent.queue_list())

    def test_unknown_tool_and_method_are_errors(self):
        unknown = self.rpc("tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(unknown["error"]["code"], -32602)
        missing = self.rpc("nope/nope")
        self.assertEqual(missing["error"]["code"], -32601)

    def test_stdio_transport_newline_delimited(self):
        incoming = io.StringIO(
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
            )
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                }
            )
            + "\n"
        )
        outgoing = io.StringIO()
        mcp.serve(instream=incoming, outstream=outgoing, agent="mcp-1", cwd=str(self.repo))
        lines = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual([line["id"] for line in lines], [1, 2])
        self.assertEqual(lines[0]["result"]["serverInfo"]["name"], "aghub")


if __name__ == "__main__":
    unittest.main()
