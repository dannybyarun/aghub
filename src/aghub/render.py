import json

from . import gitops
from .service import (
    approval_summary,
    blockers,
    contract_diff,
    contract_meta,
    describe_review_policy,
    effective_status,
    eval_issue,
    eval_short_status,
    eval_ttl,
    latest_contract,
    latest_eval,
    normalize_review_policy,
    owner_age,
)
from .util import age_seconds, human_age, now_iso


def _contract_summary(state):
    parts = []
    for name in sorted(state.contracts):
        latest = latest_contract(state, name)
        if latest is None:
            continue
        status = latest.get("status")
        parts.append(f"{name}@{latest['version']} {status}")
    return ", ".join(parts) if parts else "none"


def _trunk_ref(state, board):
    trunk = state.project.get("trunk", "main")
    remote = board.remote
    candidates = []
    if remote:
        candidates.append(f"refs/remotes/{remote}/{trunk}")
    candidates.append(f"refs/heads/{trunk}")
    for ref in candidates:
        if gitops.ref_exists(board.repo, ref):
            return trunk, gitops.short(board.repo, ref)
    return trunk, "-"


def _task_suffix(state, task):
    parts = []
    if task.get("trace"):
        source = (task.get("source") or {}).get("task")
        parts.append(f"fix for {source}" if source else "failed")
    if task.get("evals"):
        parts.append("eval " + ",".join(task["evals"]))
    status = effective_status(state, task)
    if status == "blocked":
        parts.append("; ".join(blockers(state, task)))
    elif status in ("claimed", "stale"):
        age = owner_age(state, task)
        suffix = f"{task.get('owner')}, {human_age(age)}"
        if status == "stale":
            suffix += " STALE"
        parts.append(suffix)
    return ", ".join(parts)


def _aged_agents(state, ttl):
    rows = []
    for agent_id in sorted(state.agents):
        agent = state.agents[agent_id]
        age = age_seconds(agent.get("last_seen"))
        rows.append((agent_id, agent, age, age is None or age > ttl))
    return rows


def _eval_summary(state, agent_id):
    record = latest_eval(state, agent_id)
    if record is None:
        return ""
    age = human_age(age_seconds(record.get("at")))
    return f"eval {record.get('suite')} {eval_short_status(state, record)} {age}"


def _public_project(state):
    project = dict(state.project or {})
    project.pop("eval_secret", None)
    return project


def status_text(state, board, for_agent=None, hub_mode=None):
    ttl = int(state.project.get("ttl_seconds", 600))
    trunk_name, trunk_sha = _trunk_ref(state, board)
    lines = []
    suffix = f" · hub {hub_mode}" if hub_mode else ""
    lines.append(
        f"{state.project.get('name', 'board')} · board {board.head} · "
        f"trunk {trunk_name} {trunk_sha} · ttl {ttl}s{suffix}"
    )
    lines.append(f"contracts  {_contract_summary(state)}")
    lines.append(f"review     {describe_review_policy(state)}")

    if for_agent:
        lines.append("")
        mine = next(
            (task for task in state.tasks.values() if task.get("owner") == for_agent and task.get("status") != "done"),
            None,
        )
        lines.append(f"FOR {for_agent}")
        if mine is None:
            lines.append("  your task  — nothing claimed")
        else:
            status = effective_status(state, mine)
            lines.append(
                f"  your task  {mine['id']} {mine['title']} [{status}, {human_age(owner_age(state, mine))}]"
            )
            lines.append(f"  paths      {', '.join(mine.get('paths') or []) or '—'}")
            contracts = ", ".join(
                f"{ref} {contract_meta(state, ref).get('status') if contract_meta(state, ref) else 'missing'}"
                for ref in mine.get("contracts") or []
            )
            lines.append(f"  contracts  {contracts or '—'}")
            lines.append(f"  depends    {', '.join(mine.get('depends_on') or []) or '—'}")
            if mine.get("evals"):
                gates = []
                for suite in mine["evals"]:
                    issue = eval_issue(
                        state, latest_eval(state, for_agent, suite), mine
                    )
                    gates.append(f"{suite} {issue or 'ok'}")
                lines.append(
                    f"  eval gate  {', '.join(gates)} "
                    f"(ttl {human_age(eval_ttl(state))})"
                )

        queued = [entry for entry in state.queue if entry.get("task") == (mine or {}).get("id")]
        if queued:
            lines.append(f"  queue      {queued[-1]['status']}")

        ready = [task for task in state.tasks.values() if effective_status(state, task) == "ready"]
        if ready:
            lines.append("  next       " + ", ".join(f"{task['id']} {task['title']}" for task in ready[:3]))
        assigned = next(
            (
                task
                for task in sorted(state.tasks.values(), key=lambda item: item["id"])
                if task.get("assigned_to") == for_agent and task.get("status") != "done"
            ),
            None,
        )
        if assigned:
            lines.append(f"  fix        {assigned['id']} {assigned['title']}")
        latest = latest_eval(state, for_agent)
        if latest:
            lines.append(
                f"  eval       {latest.get('suite')} "
                f"{'ok' if latest.get('passed') else 'FAIL'} · "
                f"{latest.get('command')} · {human_age(age_seconds(latest.get('at')))}"
            )
        blocked = [task for task in state.tasks.values() if effective_status(state, task) == "blocked"]
        for task in blocked[:3]:
            lines.append(f"  blocked    {task['id']} {task['title']} — {'; '.join(blockers(state, task))}")

        nearby = []
        if mine:
            from .paths import overlaps

            for other in state.tasks.values():
                if other.get("id") == mine.get("id") or other.get("status") == "done":
                    continue
                if effective_status(state, other) != "claimed":
                    continue
                for left in mine.get("paths") or []:
                    for right in other.get("paths") or []:
                        if overlaps(left, right):
                            nearby.append(
                                f"{right} claimed by {other.get('owner')} on {other['id']}"
                            )
        lines.append(f"  nearby     {'; '.join(nearby) if nearby else '—'}")
        return "\n".join(lines)

    lines.append("")
    lines.append("AGENTS")
    rows = _aged_agents(state, ttl)
    if not rows:
        lines.append("  (none)")
    for agent_id, agent, age, stale in rows:
        task = agent.get("current_task") or "—"
        paths = ""
        if task in state.tasks:
            paths = " ".join(state.tasks[task].get("paths") or [])
        flag = "  STALE" if stale else ""
        line = f"  {agent_id:<10} {task:<8} {paths:<28} seen {human_age(age)}{flag}"
        summary = _eval_summary(state, agent_id)
        if summary:
            line += f" · {summary}"
        if agent.get("model"):
            line += f" · {agent['model']}"
        lines.append(line)

    lines.append("")
    lines.append("TASKS")
    groups = {"ready": [], "claimed": [], "stale": [], "blocked": [], "done": []}
    for task in sorted(state.tasks.values(), key=lambda item: item["id"]):
        groups[effective_status(state, task)].append(task)
    for label, key in (("ready", "ready"), ("flight", "claimed"), ("stale", "stale"), ("blocked", "blocked")):
        for task in groups[key]:
            suffix = _task_suffix(state, task)
            title = f"{task['id']} {task['title']}"
            paths = ", ".join(task.get("paths") or [])
            shown = "fix" if (task.get("source") or {}).get("task") else label
            lines.append(f"  {shown:<8} {title:<38} {paths:<24} {suffix}".rstrip())
    lines.append(f"  {'done':<8} {len(groups['done'])}")

    waiting = [entry for entry in state.queue if entry.get("status") == "waiting"]
    merging = [entry for entry in state.queue if entry.get("status") == "merging"]
    last = next(
        (entry for entry in reversed(state.queue) if entry.get("status") in ("merged", "rejected")),
        None,
    )
    lines.append("")
    if waiting or merging:
        detail = f"  {len(waiting)} waiting"
        if merging:
            detail += f" · {len(merging)} merging"
        if last:
            detail += f" · last {last['status']} {last['task']}"
            if last.get("commit"):
                detail += f" {last['commit']}"
            if last["status"] == "rejected" and last.get("reason"):
                detail += f" ({last['reason']})"
        lines.append("QUEUE" + detail)
    elif last and last.get("status") == "rejected" and state.tasks.get(last["task"], {}).get("status") != "done":
        lines.append(f"QUEUE  last rejected {last['task']} — {last.get('reason')}")
    else:
        lines.append("QUEUE  empty")

    recent = state.handoffs[-3:]
    if recent:
        lines.append("")
        lines.append("RECENT")
        for record in reversed(recent):
            note = record.get("next") or record.get("note") or ""
            lines.append(f"  {record.get('agent'):<10} {record.get('task')}  → {note}")

    return "\n".join(lines)


def status_json(state, board, hub_mode=None):
    tasks = []
    for task in sorted(state.tasks.values(), key=lambda item: item["id"]):
        tasks.append(
            {
                **task,
                "effective_status": effective_status(state, task),
                "blockers": blockers(state, task),
            }
        )
    return json.dumps(
        {
            "project": _public_project(state),
            "board": board.head,
            "trunk": _trunk_ref(state, board)[1],
            "hub": hub_mode,
            "agents": [state.agents[key] for key in sorted(state.agents)],
            "tasks": tasks,
            "contracts": state.contracts,
            "queue": state.queue,
            "handoffs": state.handoffs[-10:],
            "approvals": state.approvals[-20:],
            "evals": state.evals[-20:],
        },
        indent=2,
        sort_keys=False,
    )


EVAL_SUMMARY_KEYS = (
    "id",
    "at",
    "agent",
    "machine",
    "suite",
    "command",
    "exit_code",
    "passed",
    "timed_out",
    "duration_ms",
    "timeout",
    "task",
    "branch",
    "commit",
    "tree",
    "fingerprint",
    "sandbox",
)


def _eval_summary_record(state, record):
    summary = {key: record.get(key) for key in EVAL_SUMMARY_KEYS}
    summary["status"] = eval_short_status(state, record)
    return summary


def snapshot(state, board):
    """Presentation data published to the hub after every board transaction."""
    trunk_name, trunk_sha = _trunk_ref(state, board)
    contracts = []
    for name in sorted(state.contracts):
        previous = None
        for version in sorted(state.contracts[name]):
            meta = state.contracts[name][version]
            entry = {
                "name": name,
                "version": version,
                "status": meta.get("status"),
                "sha256": meta.get("sha256"),
                "frozen_at": meta.get("frozen_at"),
            }
            if meta.get("status") == "frozen":
                if previous is not None:
                    diff = contract_diff(board, state, name, previous, version)
                    if diff is not None:
                        entry["diff_from"] = previous
                        entry["diff"] = diff
                previous = version
            contracts.append(entry)
    tasks = []
    for key in sorted(state.tasks):
        task = dict(state.tasks[key])
        if task.get("requires_approval"):
            summary = approval_summary(state, task)
            task["review_state"] = {
                "satisfied": summary["satisfied"],
                "decision": summary["decision"],
                "required_approvals": summary["required_approvals"],
                "approved_count": summary["approved_count"],
                "required_groups": summary["required_groups"],
                "missing_groups": summary["missing_groups"],
                "groups": summary["groups"],
                "protected": summary["protected"],
                "sla_seconds": summary["sla_seconds"],
                "pending_since": summary["pending_since"],
                "pending_seconds": summary["pending_seconds"],
                "overdue": summary["overdue"],
                "ttl_seconds": summary["ttl_seconds"],
                "expired_count": len(summary["expired"]),
                "stale_count": len(summary["stale"]),
                "ineligible_count": len(summary["ineligible"]),
                "reason": summary["reason"],
            }
        tasks.append(task)
    return {
        "project": _public_project(state),
        "board": board.head,
        "trunk": {"name": trunk_name, "sha": trunk_sha},
        "generated_at": now_iso(),
        "review_policy": normalize_review_policy(state.project),
        "agents": [state.agents[key] for key in sorted(state.agents)],
        "tasks": tasks,
        "contracts": contracts,
        "queue": state.queue[-20:],
        "handoffs": state.handoffs[-20:],
        "approvals": state.approvals[-20:],
        "evals": [_eval_summary_record(state, record) for record in state.evals[-20:]],
    }
