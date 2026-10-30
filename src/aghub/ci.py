"""CI helpers: a GitHub Actions workflow template and a machine-readable gate."""

import json

from . import service


def workflow(
    checkout="actions/checkout@v4",
    python="3.10",
    project=None,
    trunk="main",
    contract=None,
    codegen_out="generated",
):
    name = project or "${{ github.event.repository.name }}"
    codegen = ""
    if contract:
        codegen = f"""
      - name: Codegen drift
        run: ./bin/aghub codegen {contract} --out {codegen_out} --check
"""
    return f"""\
name: aghub
on:
  push:
    branches: [{trunk}]
  pull_request:
jobs:
  board:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    env:
      AGHUB_AGENT: ci
      AGHUB_PROJECT: "{name}"
      AGHUB_HUB_TOKEN: ${{{{ secrets.AGHUB_HUB_TOKEN }}}}
      AGHUB_AGENT_KEY: ${{{{ secrets.AGHUB_AGENT_KEY }}}}
    steps:
      - name: Checkout
        uses: {checkout}
        with:
          fetch-depth: 0
      - name: Python
        uses: actions/setup-python@v5
        with:
          python-version: "{python}"
      - name: Git identity
        run: |
          git config user.name "aghub ci"
          git config user.email "ci@aghub.local"
      - name: Install aghub
        run: |
          python -m pip install --upgrade pip
          pip install pyyaml
      - name: Attach board
        run: ./bin/aghub init --project "$AGHUB_PROJECT" --trunk "{trunk}"
      - name: Board status
        run: ./bin/aghub status --json
      - name: Verify signatures and history
        run: ./bin/aghub verify
{codegen}      - name: Merge queue
        run: ./bin/aghub queue run
      - name: CI bridge
        run: ./bin/aghub ci status --json
"""


def status(state, board):
    tasks = list(state.tasks.values())
    open_tasks = [task for task in tasks if task.get("status") != "done"]
    waiting = [entry for entry in state.queue if entry.get("status") == "waiting"]
    merging = [entry for entry in state.queue if entry.get("status") == "merging"]
    failed = [
        entry
        for entry in state.queue
        if entry.get("status") == "rejected"
        and not state.tasks.get(entry.get("task"), {}).get("done_at")
    ]
    approvals = [
        record
        for record in state.approvals
        if record.get("decision", "approved") == "approved"
    ]
    gated = [
        task
        for task in open_tasks
        if task.get("requires_approval")
        and not any(item.get("task") == task["id"] for item in approvals)
    ]
    return {
        "project": (state.project or {}).get("name"),
        "board": board.head,
        "trunk": (state.project or {}).get("trunk", "main"),
        "scope": (state.project or {}).get("scope"),
        "tasks": {
            "total": len(tasks),
            "open": len(open_tasks),
            "ready": sum(
                1 for task in open_tasks if service.effective_status(state, task) == "ready"
            ),
            "claimed": sum(
                1 for task in open_tasks if service.effective_status(state, task) == "claimed"
            ),
            "blocked": sum(
                1 for task in open_tasks if service.effective_status(state, task) == "blocked"
            ),
        },
        "queue": {
            "waiting": len(waiting),
            "merging": len(merging),
            "failed": len(failed),
        },
        "approvals": {"approved": len(approvals), "gated": len(gated)},
        "evals": len(state.evals),
        "ok": not failed,
    }


def render_status(state, board):
    return json.dumps(status(state, board), indent=2, sort_keys=False)
