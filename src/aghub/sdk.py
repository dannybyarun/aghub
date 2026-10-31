"""Small Python client for agents that prefer a library over the CLI.

The SDK wraps the CLI so it has the same identity, lease, and policy behavior
as human commands. It deliberately does not reimplement board mutation logic.

    from aghub.sdk import Agent
    with Agent("planner") as agent:
        print(agent.status())
        agent.claim("T-001", worktree="../lane")
"""

import contextlib
import io
import json
import os

from . import cli


class AgentError(RuntimeError):
    pass


class Agent:
    def __init__(self, agent_id, cwd=None):
        self.agent_id = agent_id
        self.cwd = cwd

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def run(self, *argv):
        """Run a CLI command in this agent's identity and return (code, out, err)."""
        previous = None
        if self.cwd:
            previous = os.getcwd()
            os.chdir(self.cwd)
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(["--agent", self.agent_id, *argv])
        finally:
            if previous is not None:
                os.chdir(previous)
        return code, stdout.getvalue(), stderr.getvalue()

    def call(self, *argv):
        code, stdout, stderr = self.run(*argv)
        if code != 0:
            raise AgentError((stderr or stdout).strip() or f"aghub exited {code}")
        return stdout

    def json(self, *argv):
        return json.loads(self.call(*argv))

    def status(self):
        return self.json("status", "--json")

    def next_task(self):
        return self.call("next")

    def tasks(self):
        return self.json("status", "--json")["tasks"]

    def claim(self, task, worktree=None, steal=False, force=False):
        argv = ["claim", task]
        if worktree is not None:
            argv.extend(["--worktree", worktree])
        if steal:
            argv.append("--steal")
        if force:
            argv.append("--force")
        return self.call(*argv)

    def release(self, task, force=False):
        argv = ["release", task]
        if force:
            argv.append("--force")
        return self.call(*argv)

    def handoff(self, task, next_step="", note="", done=False):
        argv = ["handoff", task, "--next", next_step, "--note", note]
        if done:
            argv.append("--done")
        return self.call(*argv)

    def submit(self, task, branch=None):
        argv = ["submit", task]
        if branch:
            argv.extend(["--branch", branch])
        return self.call(*argv)

    def heartbeat(self):
        return self.call("heartbeat")

    def add_task(
        self,
        title,
        paths=None,
        contracts=None,
        evals=None,
        acceptance="",
        requires_approval=False,
        required_approvals=None,
        review_groups=None,
        reviewers=None,
        review_ttl=None,
    ):
        argv = ["task", "add", title]
        for path in paths or []:
            argv.extend(["--paths", path])
        for contract in contracts or []:
            argv.extend(["--contract", contract])
        for suite in evals or []:
            argv.extend(["--eval", suite])
        if acceptance:
            argv.extend(["--acceptance", acceptance])
        if requires_approval:
            argv.append("--requires-approval")
        if required_approvals is not None:
            argv.extend(["--required-approvals", str(required_approvals)])
        for group in review_groups or []:
            argv.extend(["--reviewer-group", group])
        for reviewer in reviewers or []:
            argv.extend(["--reviewer", reviewer])
        if review_ttl is not None:
            argv.extend(["--review-ttl", str(review_ttl)])
        return self.call(*argv)

    def approve(self, task, note="", commit=None):
        argv = ["approve", task, "--note", note]
        if commit:
            argv.extend(["--commit", commit])
        return self.call(*argv)

    def eval_run(self, suite, command, timeout=300, sandbox="auto", task=None):
        argv = [
            "eval", "run", suite, "--command", command,
            "--timeout", str(timeout), "--sandbox", sandbox,
        ]
        if task:
            argv.extend(["--task", task])
        return self.call(*argv)

    def eval_verify(self, eval_id):
        return self.call("eval", "verify", eval_id)

    def eval_list(self):
        return self.call("eval", "list")

    def queue_list(self):
        return self.call("queue", "list")

    def verify(self, strict=False):
        argv = ["verify", "--json"]
        if strict:
            argv.append("--strict")
        return self.json(*argv)

    def key(self):
        return self.call("agent", "--key")
