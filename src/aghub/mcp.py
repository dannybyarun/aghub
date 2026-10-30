"""Model Context Protocol (MCP) server for aghub.

The server speaks newline-delimited JSON-RPC 2.0 over stdio, which is the
transport used by MCP clients such as Claude Desktop, Cursor, and editor
agents. Every tool is a thin, explicit wrapper over the same CLI used by
humans and scripts, so MCP clients cannot bypass leases or board policy.

Run it from a repository where `aghub init` has already been executed:

    aghub mcp serve --agent planner
"""

import contextlib
import io
import json
import os
import sys

from . import __version__, cli

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "aghub_status",
        "description": "Read the shared board, tasks, agents, queue, evals, and leases.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "json": {"type": "boolean", "default": True},
                "for_agent": {"type": "string"},
            },
        },
    },
    {
        "name": "aghub_next",
        "description": "Show the next task this agent can claim.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "aghub_task_list",
        "description": "List tasks with effective status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "aghub_task_show",
        "description": "Show one task, including its trace and acceptance criteria.",
        "inputSchema": {
            "type": "object",
            "properties": {"task": {"type": "string"}},
            "required": ["task"],
        },
    },
    {
        "name": "aghub_task_add",
        "description": "Create a task with path ownership, contracts, dependencies, evals, or an approval gate.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
                "contracts": {"type": "array", "items": {"type": "string"}},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "evals": {"type": "array", "items": {"type": "string"}},
                "acceptance": {"type": "string"},
                "requires_approval": {"type": "boolean"},
                "requires_sandbox": {"type": "boolean"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "aghub_claim",
        "description": "Claim a task and optionally create an isolated worktree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "worktree": {"type": "string"},
                "steal": {"type": "boolean"},
                "force": {"type": "boolean"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "aghub_release",
        "description": "Release a claimed task.",
        "inputSchema": {
            "type": "object",
            "properties": {"task": {"type": "string"}, "force": {"type": "boolean"}},
            "required": ["task"],
        },
    },
    {
        "name": "aghub_handoff",
        "description": "Write a signed async handoff for the next agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "next": {"type": "string"},
                "note": {"type": "string"},
                "done": {"type": "boolean"},
                "force": {"type": "boolean"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "aghub_submit",
        "description": "Push the task branch and enqueue it for serial integration.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "branch": {"type": "string"},
                "force": {"type": "boolean"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "aghub_heartbeat",
        "description": "Refresh this agent's presence and lease.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "aghub_approve",
        "description": "Record a human approval for a task that requires one.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "note": {"type": "string"},
                "commit": {"type": "string"},
                "force": {"type": "boolean"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "aghub_verify",
        "description": "Verify the board event chain and all signed records.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "strict": {"type": "boolean"},
                "json": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "aghub_eval_run",
        "description": "Run an eval locally and publish a signed scorecard.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "suite": {"type": "string"},
                "command": {"type": "string"},
                "timeout": {"type": "number"},
                "sandbox": {"type": "string", "enum": ["auto", "off", "require"]},
                "task": {"type": "string"},
            },
            "required": ["suite", "command"],
        },
    },
    {
        "name": "aghub_eval_list",
        "description": "List signed local eval scorecards.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string"},
                "suite": {"type": "string"},
            },
        },
    },
    {
        "name": "aghub_queue_list",
        "description": "List merge queue entries and their status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "aghub_agent_key",
        "description": "Show or create this clone's Ed25519 key for the current agent.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _string_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _bool(value):
    return bool(value)


def build_argv(name, arguments, agent):
    """Translate an MCP tool call into CLI arguments."""
    args = ["--agent", agent] if agent else []
    if name == "aghub_status":
        args.append("status")
        if arguments.get("for_agent"):
            args.extend(["--for", arguments["for_agent"]])
        if arguments.get("json", True):
            args.append("--json")
    elif name == "aghub_next":
        args.append("next")
    elif name == "aghub_task_list":
        args.extend(["task", "list"])
    elif name == "aghub_task_show":
        args.extend(["task", "show", arguments["task"]])
    elif name == "aghub_task_add":
        args.extend(["task", "add", arguments["title"]])
        for path in _string_list(arguments.get("paths")):
            args.extend(["--paths", path])
        for contract in _string_list(arguments.get("contracts")):
            args.extend(["--contract", contract])
        for dependency in _string_list(arguments.get("depends_on")):
            args.extend(["--depends", dependency])
        for suite in _string_list(arguments.get("evals")):
            args.extend(["--eval", suite])
        if arguments.get("acceptance"):
            args.extend(["--acceptance", arguments["acceptance"]])
        if arguments.get("requires_approval"):
            args.append("--requires-approval")
        if arguments.get("requires_sandbox"):
            args.append("--requires-sandbox")
    elif name == "aghub_claim":
        args.extend(["claim", arguments["task"]])
        if "worktree" in arguments:
            args.extend(["--worktree", arguments.get("worktree") or ""])
        if arguments.get("steal"):
            args.append("--steal")
        if arguments.get("force"):
            args.append("--force")
    elif name == "aghub_release":
        args.extend(["release", arguments["task"]])
        if arguments.get("force"):
            args.append("--force")
    elif name == "aghub_handoff":
        args.extend(["handoff", arguments["task"]])
        if arguments.get("next"):
            args.extend(["--next", arguments["next"]])
        if arguments.get("note"):
            args.extend(["--note", arguments["note"]])
        if arguments.get("done"):
            args.append("--done")
        if arguments.get("force"):
            args.append("--force")
    elif name == "aghub_submit":
        args.extend(["submit", arguments["task"]])
        if arguments.get("branch"):
            args.extend(["--branch", arguments["branch"]])
        if arguments.get("force"):
            args.append("--force")
    elif name == "aghub_heartbeat":
        args.append("heartbeat")
    elif name == "aghub_approve":
        args.extend(["approve", arguments["task"]])
        if arguments.get("note"):
            args.extend(["--note", arguments["note"]])
        if arguments.get("commit"):
            args.extend(["--commit", arguments["commit"]])
        if arguments.get("force"):
            args.append("--force")
    elif name == "aghub_verify":
        args.append("verify")
        if arguments.get("strict"):
            args.append("--strict")
        if arguments.get("json"):
            args.append("--json")
    elif name == "aghub_eval_run":
        args.extend(["eval", "run", arguments["suite"]])
        args.extend(["--command", arguments["command"]])
        if arguments.get("timeout") is not None:
            args.extend(["--timeout", str(arguments["timeout"])])
        if arguments.get("sandbox"):
            args.extend(["--sandbox", arguments["sandbox"]])
        if arguments.get("task"):
            args.extend(["--task", arguments["task"]])
    elif name == "aghub_eval_list":
        args.extend(["eval", "list"])
        if arguments.get("agent"):
            args.extend(["--agent", arguments["agent"]])
        if arguments.get("suite"):
            args.extend(["--suite", arguments["suite"]])
    elif name == "aghub_queue_list":
        args.extend(["queue", "list"])
    elif name == "aghub_agent_key":
        args.extend(["agent", "--key"])
    else:
        raise KeyError(name)
    return args


class ToolRunner:
    """Run aghub CLI operations in-process and capture their output."""

    def __init__(self, agent=None, cwd=None):
        self.agent = agent or os.environ.get("AGHUB_AGENT")
        self.cwd = cwd

    def call(self, name, arguments=None):
        arguments = arguments or {}
        previous = None
        if self.cwd:
            previous = os.getcwd()
            os.chdir(self.cwd)
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(build_argv(name, arguments, self.agent))
        except SystemExit as exc:  # argparse errors
            code = int(exc.code or 2)
        except Exception as exc:  # turn programmer errors into tool errors
            code = 1
            stderr.write(f"{type(exc).__name__}: {exc}\n")
        finally:
            if previous is not None:
                os.chdir(previous)
        return code, stdout.getvalue(), stderr.getvalue()


def _rpc_error(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _rpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _text_result(code, stdout, stderr):
    text = stdout.strip() or stderr.strip() or "(no output)"
    payload = {
        "content": [{"type": "text", "text": text}],
        "isError": code != 0,
    }
    if code == 0 and stdout.lstrip().startswith(("{", "[")):
        try:
            payload["structuredContent"] = json.loads(stdout)
        except ValueError:
            pass
    return payload


class Server:
    def __init__(self, runner=None, agent=None, cwd=None):
        self.runner = runner or ToolRunner(agent=agent, cwd=cwd)

    def handle(self, message):
        """Handle one decoded JSON-RPC message; return a dict or None."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _rpc_error(message.get("id") if isinstance(message, dict) else None, -32600, "invalid request")
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        # Notifications (no id) never receive a response.
        is_notification = "id" not in message or request_id is None
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return _rpc_result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                    },
                    "serverInfo": {"name": "aghub", "version": __version__},
                    "instructions": (
                        "Use aghub_status to inspect the board, aghub_next to find "
                        "claimable work, and aghub_claim before editing files. "
                        "Always release or hand off a task when you stop."
                    ),
                },
            )
        if method == "ping":
            return None if is_notification else _rpc_result(request_id, {})
        if method == "tools/list":
            return _rpc_result(request_id, {"tools": TOOLS})
        if method == "resources/list":
            return _rpc_result(
                request_id,
                {
                    "resources": [
                        {
                            "uri": "aghub://board/status",
                            "name": "Board status",
                            "description": "Current JSON status of this repository's board.",
                            "mimeType": "application/json",
                        },
                        {
                            "uri": "aghub://board/tasks",
                            "name": "Tasks",
                            "description": "Task list with effective status.",
                            "mimeType": "text/plain",
                        },
                    ]
                },
            )
        if method == "resources/read":
            return self._read_resource(request_id, params.get("uri"))
        if method == "prompts/list":
            return _rpc_result(request_id, {"prompts": []})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return _rpc_error(request_id, -32602, "arguments must be an object")
            if name not in {tool["name"] for tool in TOOLS}:
                return _rpc_error(request_id, -32602, f"unknown tool '{name}'")
            code, stdout, stderr = self.runner.call(name, arguments)
            return _rpc_result(request_id, _text_result(code, stdout, stderr))
        return _rpc_error(request_id, -32601, f"method not found: {method}")

    def _read_resource(self, request_id, uri):
        if uri == "aghub://board/status":
            code, stdout, stderr = self.runner.call("aghub_status", {"json": True})
            return _rpc_result(
                request_id,
                {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json" if code == 0 else "text/plain",
                            "text": stdout if code == 0 else (stderr or stdout),
                        }
                    ]
                },
            )
        if uri == "aghub://board/tasks":
            code, stdout, stderr = self.runner.call("aghub_task_list", {})
            return _rpc_result(
                request_id,
                {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "text/plain",
                            "text": stdout if code == 0 else (stderr or stdout),
                        }
                    ]
                },
            )
        return _rpc_error(request_id, -32602, f"unknown resource '{uri}'")


def serve(instream=None, outstream=None, agent=None, cwd=None):
    stream_in = instream or sys.stdin
    stream_out = outstream or sys.stdout
    server = Server(agent=agent, cwd=cwd)
    for raw in stream_in:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            response = _rpc_error(None, -32700, f"parse error: {exc}")
        else:
            if isinstance(message, list):
                responses = [server.handle(item) for item in message]
                response = [item for item in responses if item is not None]
            else:
                response = server.handle(message)
        if response is None:
            continue
        stream_out.write(json.dumps(response, separators=(",", ":")) + "\n")
        stream_out.flush()
