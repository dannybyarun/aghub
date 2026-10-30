"""Human review helpers: an attention inbox and self-contained review packets.

The board is machine-readable first. These helpers turn it into the two views a
human needs: what needs me now, and everything I need to decide one task without
opening five other commands.
"""

from . import gitops, service
from .errors import AghubError
from .util import human_age, now_iso


def _queue_for(state, task_id):
    return sorted(
        (entry for entry in state.queue if entry.get("task") == task_id),
        key=lambda entry: (
            str(entry.get("submitted_at") or ""),
            str(entry.get("id") or ""),
        ),
    )


def _latest(entries):
    return entries[-1] if entries else None


def inbox(state):
    """Return actionable human-review items, highest priority first."""
    items = []
    ttl = int((state.project or {}).get("ttl_seconds", 600) or 600)
    for task_id in sorted(state.tasks):
        task = state.tasks[task_id]
        if task.get("status") == "done":
            continue
        entries = _queue_for(state, task_id)
        latest_queue = _latest(entries)
        status = service.effective_status(state, task)
        decision, record = service.review_decision(state, task_id)
        gap = service.approval_gate(state, task)
        base = {
            "task": task_id,
            "title": task.get("title") or "",
            "status": status,
            "owner": task.get("owner"),
            "branch": task.get("branch"),
            "queue": latest_queue.get("id") if latest_queue else None,
        }

        if decision == "changes_requested" and record:
            items.append(
                {
                    **base,
                    "kind": "changes",
                    "priority": 0,
                    "detail": (
                        f"{record.get('agent') or 'a reviewer'} requested changes: "
                        f"{record.get('note') or '(no note)'}"
                    ),
                    "action": f"aghub review {task_id} --diff",
                }
            )
        elif gap and (task.get("owner") or entries):
            items.append(
                {
                    **base,
                    "kind": "approval",
                    "priority": 0,
                    "detail": gap,
                    "action": f"aghub review {task_id} --approve --note ...",
                }
            )

        for entry in entries:
            if entry.get("status") != "rejected":
                continue
            fix = entry.get("fix_task")
            items.append(
                {
                    **base,
                    "kind": "failed",
                    "priority": 0,
                    "queue": entry.get("id"),
                    "fix_task": fix,
                    "detail": entry.get("reason") or "queue rejected the submission",
                    "action": (
                        f"aghub task show {fix}" if fix else f"aghub task show {task_id}"
                    ),
                }
            )

        if status == "stale" and task.get("owner"):
            age = service.owner_age(state, task)
            items.append(
                {
                    **base,
                    "kind": "stale",
                    "priority": 1,
                    "detail": (
                        f"{task.get('owner')} has not heartbeated for "
                        f"{human_age(age)} (ttl {ttl}s)"
                    ),
                    "action": f"aghub claim {task_id} --steal",
                }
            )

    items.sort(key=lambda item: (item.get("priority", 9), item.get("task", ""), item.get("kind", "")))
    return items


def render_inbox(project, items):
    if not items:
        return f"inbox clear · {project or 'project'}"
    lines = [f"INBOX  {project or 'project'} · {len(items)} item(s)", ""]
    for item in items:
        origin = item.get("owner") or "unowned"
        bits = [item.get("status") or "unknown", origin]
        if item.get("queue"):
            bits.append(item["queue"])
        if item.get("fix_task"):
            bits.append(f"fix {item['fix_task']}")
        lines.append(
            f"  {item['kind']:<9} {item['task']:<7} {item['title']}"
        )
        lines.append(f"            {item['detail']}")
        lines.append(f"            {item['action']}")
        lines.append(f"            {' · '.join(str(bit) for bit in bits)}")
    lines.append("")
    lines.append("next: aghub review <task>   # full packet and diff")
    return "\n".join(lines)


def _resolve_ref(board, branch):
    candidates = []
    if board.remote:
        candidates.append(f"refs/remotes/{board.remote}/{branch}")
    candidates.extend([f"refs/heads/{branch}", branch])
    for ref in candidates:
        if gitops.ref_exists(board.repo, ref):
            return ref
    return None


def _trunk_ref(board, state):
    trunk = (state.project or {}).get("trunk", "main")
    ref = _resolve_ref(board, trunk)
    return ref, trunk


def _fetch_branch(board, branch):
    if not board.remote:
        return None
    ref = f"refs/remotes/{board.remote}/{branch}"
    gitops.git(
        ["fetch", board.remote, f"+refs/heads/{branch}:{ref}"],
        cwd=board.repo,
        check=False,
    )
    return ref if gitops.ref_exists(board.repo, ref) else None


def branch_changes(board, state, task, include_diff=False):
    branch = task.get("branch")
    if not branch:
        return {"error": "task has no branch yet"}
    branch_ref = _resolve_ref(board, branch)
    if branch_ref is None:
        branch_ref = _fetch_branch(board, branch)
    if branch_ref is None:
        return {
            "error": (
                f"branch {branch} is not available locally — run `aghub sync` "
                "or fetch it from the remote"
            )
        }
    trunk_ref, trunk = _trunk_ref(board, state)
    if trunk_ref is None:
        return {"error": f"trunk {trunk} is not available locally"}
    base = gitops.git(
        ["merge-base", trunk_ref, branch_ref], cwd=board.repo, check=False
    )
    if base.returncode != 0 or not base.stdout.strip():
        return {"error": f"no merge base between {trunk} and {branch}"}
    base_sha = base.stdout.strip()
    status = gitops.git(
        ["diff", "--name-status", "--find-renames", base_sha, branch_ref],
        cwd=board.repo,
        check=False,
    )
    files = []
    for line in status.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append({"status": parts[0], "path": parts[-1]})
    stat = gitops.git(
        ["diff", "--stat", base_sha, branch_ref], cwd=board.repo, check=False
    ).stdout.strip()
    payload = {
        "trunk": trunk,
        "base": gitops.short(board.repo, base_sha),
        "branch": branch,
        "files": files,
        "stat": stat,
    }
    if include_diff:
        payload["patch"] = gitops.git(
            ["diff", "--find-renames", base_sha, branch_ref],
            cwd=board.repo,
            check=False,
        ).stdout
    return payload


def _contracts(board, state, task):
    entries = []
    for ref in task.get("contracts") or []:
        try:
            name, version = service.parse_contract_ref(ref)
        except AghubError:
            entries.append({"ref": ref, "status": "invalid"})
            continue
        meta = service.contract_meta(state, ref)
        latest = service.latest_contract(state, name)
        entry = {
            "ref": ref,
            "status": (meta or {}).get("status") or "missing",
            "sha256": (meta or {}).get("sha256"),
            "latest": (latest or {}).get("version"),
            "stale": bool(latest and latest.get("version") != version),
        }
        if latest and latest.get("version") != version:
            diff = service.contract_diff(board, state, name, version, latest["version"])
            if diff is not None:
                entry["diff"] = diff
        entries.append(entry)
    return entries


def _evals(state, task):
    entries = []
    for suite in task.get("evals") or []:
        record = service.latest_eval(state, task.get("owner"), suite)
        entries.append(
            {
                "suite": suite,
                "id": (record or {}).get("id"),
                "at": (record or {}).get("at"),
                "status": service.eval_short_status(state, record),
                "issue": service.eval_issue(state, record, task),
            }
        )
    return entries


def _reviews(state, task_id):
    entries = []
    for record in state.approvals:
        if record.get("task") != task_id:
            continue
        entries.append(
            {
                "id": record.get("id"),
                "at": record.get("at"),
                "agent": record.get("agent"),
                "decision": record.get("decision") or "approved",
                "note": record.get("note") or "",
                "commit": record.get("commit"),
                "source": record.get("source"),
                "external_id": record.get("external_id"),
                "verified": service.verify_attestation(state, record) is True,
            }
        )
    return entries


def review_packet(board, state, task_id, include_diff=False):
    """Build one complete review view for a human, without mutating the board."""
    task = state.tasks.get(task_id)
    if task is None:
        raise AghubError(f"unknown task {task_id}")
    entries = _queue_for(state, task_id)
    decision, record = service.review_decision(state, task_id)
    latest_handoff = next(
        (item for item in reversed(state.handoffs) if item.get("task") == task_id),
        None,
    )
    packet = {
        "project": (state.project or {}).get("name"),
        "board": board.head,
        "generated_at": now_iso(),
        "task": task,
        "status": service.effective_status(state, task),
        "owner_age": service.owner_age(state, task),
        "approval": {
            "required": bool(task.get("requires_approval")),
            "decision": decision or "pending",
            "reviewer": (record or {}).get("agent"),
            "note": (record or {}).get("note") or "",
            "commit": (record or {}).get("commit"),
            "source": (record or {}).get("source"),
            "external_id": (record or {}).get("external_id"),
            "reviews": _reviews(state, task_id),
        },
        "contracts": _contracts(board, state, task),
        "evals": _evals(state, task),
        "queue": entries,
        "handoff": latest_handoff,
        "failure": task.get("trace"),
        "changes": branch_changes(board, state, task, include_diff=include_diff),
        "actions": [],
    }
    if task.get("requires_approval") and decision != "approved":
        packet["actions"].append(f"aghub review {task_id} --approve --note ...")
        packet["actions"].append(
            f"aghub review {task_id} --request-changes --note ..."
        )
    rejected = [entry for entry in entries if entry.get("status") == "rejected"]
    if rejected:
        latest = rejected[-1]
        if latest.get("fix_task"):
            packet["actions"].append(f"aghub task show {latest['fix_task']}")
        else:
            packet["actions"].append(f"aghub task show {task_id}")
    if not packet["actions"]:
        status = packet["status"]
        if status == "claimed":
            packet["actions"].append(f"aghub submit {task_id}")
        elif status == "ready":
            packet["actions"].append(f"aghub claim {task_id}")
        elif status == "stale":
            packet["actions"].append(f"aghub claim {task_id} --steal")
        elif status == "blocked":
            packet["actions"].append(f"aghub task show {task_id}")
    return packet


def render_review(packet, include_diff=False):
    task = packet["task"]
    lines = [
        f"{task['id']} {task.get('title') or ''}",
        f"  status    {packet['status']}",
        f"  owner     {task.get('owner') or '—'}",
        f"  branch    {task.get('branch') or '—'}",
        f"  paths     {', '.join(task.get('paths') or []) or '—'}",
    ]
    if task.get("acceptance"):
        lines.append(f"  accept    {task['acceptance']}")
    approval = packet["approval"]
    label = approval["decision"]
    if approval.get("reviewer"):
        label += f" by {approval['reviewer']}"
    if approval.get("note"):
        label += f" — {approval['note']}"
    lines.append(f"  review    {label}" + (" (required)" if approval["required"] else ""))

    lines.append("")
    lines.append("contracts")
    if not packet["contracts"]:
        lines.append("  (none)")
    for item in packet["contracts"]:
        extra = f" · latest {item['latest']} available" if item.get("stale") else ""
        lines.append(f"  {item['ref']:<16} {item['status']}{extra}")

    lines.append("")
    lines.append("queue")
    if not packet["queue"]:
        lines.append("  (not submitted)")
    for entry in packet["queue"]:
        lines.append(
            f"  {entry.get('id')}  {entry.get('status'):<8} "
            f"{entry.get('reason') or ''}".rstrip()
        )

    lines.append("")
    lines.append("evals")
    if not packet["evals"]:
        lines.append("  (none required)")
    for item in packet["evals"]:
        issue = f" — {item['issue']}" if item.get("issue") else ""
        lines.append(f"  {item['suite']:<12} {item['status']}{issue}")

    changes = packet["changes"]
    lines.append("")
    lines.append("changes")
    if changes.get("error"):
        lines.append(f"  {changes['error']}")
    else:
        lines.append(
            f"  {changes['base']} → {changes['branch']} · "
            f"{len(changes['files'])} file(s)"
        )
        if changes.get("stat"):
            for line in changes["stat"].splitlines():
                lines.append(f"  {line}")
        if include_diff and changes.get("patch"):
            lines.append("")
            lines.append(changes["patch"].rstrip())

    if packet["approval"]["reviews"]:
        lines.append("")
        lines.append("reviews")
        for record in packet["approval"]["reviews"]:
            note = f" — {record['note']}" if record["note"] else ""
            lines.append(
                f"  {record['decision']:<18} {record['agent']}{note} "
                f"({record['at']})"
            )

    if packet.get("failure"):
        trace = packet["failure"]
        lines.append("")
        lines.append("failure")
        lines.append(f"  kind    {trace.get('kind') or '—'}")
        lines.append(f"  reason  {trace.get('reason') or '—'}")
        if trace.get("command"):
            lines.append(f"  command {trace['command']}")

    if packet["actions"]:
        lines.append("")
        lines.append("next")
        for action in packet["actions"]:
            lines.append(f"  {action}")
    return "\n".join(lines)
