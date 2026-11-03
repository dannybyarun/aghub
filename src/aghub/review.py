"""Human review helpers: an attention inbox and self-contained review packets.

The board is machine-readable first. These helpers turn it into the two views a
human needs: what needs me now, and everything I need to decide one task without
opening five other commands. The web review workspace reuses the same packet:
the hub mirrors it for the browser, and a trusted local worker signs the
resulting decision onto the board.
"""

import time

from . import gitops, hubclient, service
from .board import ensure_local_agent_key
from .errors import AghubError
from .util import human_age, now_iso

MAX_PUBLISHED_PATCH = 1024 * 1024


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


def inbox(state, board=None):
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
        head = (
            branch_head(board, task, update=True)
            if board is not None and task.get("requires_approval")
            else None
        )
        changed = (
            service.branch_changes(board, head)
            if board is not None and head
            else None
        )
        gap = service.approval_gate(state, task, head=head, changed=changed)
        base = {
            "task": task_id,
            "title": task.get("title") or "",
            "status": status,
            "owner": task.get("owner"),
            "branch": task.get("branch"),
            "head": head,
            "queue": latest_queue.get("id") if latest_queue else None,
        }

        if decision == "changes_requested" and record:
            items.append(
                {
                    **base,
                    "kind": "changes",
                    "priority": -1,
                    "detail": (
                        f"{service.reviewer_identity(record) or 'a reviewer'} "
                        f"requested changes: "
                        f"{record.get('note') or '(no note)'}"
                    ),
                    "action": f"aghub review {task_id} --diff",
                }
            )
        elif gap and (task.get("owner") or entries):
            summary = service.approval_summary(
                state, task, head=head, changed=changed
            )
            items.append(
                {
                    **base,
                    "kind": "approval",
                    "priority": -1 if summary["overdue"] else 0,
                    "detail": gap,
                    "action": f"aghub review {task_id} --approve --note ...",
                    "policy": {
                        "required_approvals": summary["required_approvals"],
                        "approved_count": summary["approved_count"],
                        "required_groups": summary["required_groups"],
                        "missing_groups": summary["missing_groups"],
                        "protected": [
                            hit["path"] for hit in summary["protected"]
                        ],
                        "sla_seconds": summary["sla_seconds"],
                        "pending_seconds": summary["pending_seconds"],
                        "overdue": summary["overdue"],
                    },
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


def branch_head(board, task, update=True, prefer="remote"):
    """Resolve the task branch tip, preferring the remote for review."""
    branch = (task or {}).get("branch")
    if not branch:
        return None
    if update and board.remote:
        _fetch_branch(board, branch)
    refs = []
    if board.remote:
        refs.append(f"refs/remotes/{board.remote}/{branch}")
    refs.append(f"refs/heads/{branch}")
    if prefer == "local":
        refs.reverse()
    for ref in refs:
        if not gitops.ref_exists(board.repo, ref):
            continue
        resolved = gitops.git(["rev-parse", ref], cwd=board.repo, check=False)
        head = resolved.stdout.strip()
        if head:
            return head
    return None


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
    resolved_head = gitops.git(
        ["rev-parse", branch_ref], cwd=board.repo, check=False
    ).stdout.strip()
    payload = {
        "trunk": trunk,
        "base": gitops.short(board.repo, base_sha),
        "head": resolved_head or None,
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


def _reviews(state, task_id, analysis=None):
    analysis = analysis or {}
    entries = []
    for record in state.approvals:
        if record.get("task") != task_id:
            continue
        view = analysis.get(record.get("id")) or {}
        entries.append(
            {
                "id": record.get("id"),
                "at": record.get("at"),
                "agent": record.get("agent"),
                "reviewer": service.reviewer_identity(record),
                "github_login": record.get("github_login"),
                "decision": record.get("decision") or "approved",
                "note": record.get("note") or "",
                "commit": record.get("commit"),
                "binding": record.get("binding"),
                "source": record.get("source"),
                "external_id": record.get("external_id"),
                "eligible": bool(view.get("eligible", True)),
                "stale": bool(view.get("stale")),
                "expired": bool(view.get("expired")),
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
    head = branch_head(board, task, update=True)
    summary = service.approval_summary(
        state, task, head=head, changed=service.branch_changes(board, head)
    )
    stale = summary["latest_stale"]
    analysis = {}
    for item in (
        summary["approvals"]
        + summary["expired"]
        + summary["stale"]
        + summary["ineligible"]
    ):
        analysis[item.get("id")] = item
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
            "reviewer": service.reviewer_identity(record) if record else None,
            "note": (record or {}).get("note") or "",
            "commit": (record or {}).get("commit"),
            "binding": (record or {}).get("binding"),
            "head": head,
            "stale": stale,
            "source": (record or {}).get("source"),
            "external_id": (record or {}).get("external_id"),
            "satisfied": summary["satisfied"],
            "reason": summary["reason"],
            "required_approvals": summary["required_approvals"],
            "approved_count": summary["approved_count"],
            "missing_approvals": summary["missing_approvals"],
            "required_groups": summary["required_groups"],
            "missing_groups": summary["missing_groups"],
            "groups": summary["groups"],
            "protected": summary["protected"],
            "sla_seconds": summary["sla_seconds"],
            "pending_since": summary["pending_since"],
            "pending_seconds": summary["pending_seconds"],
            "overdue": summary["overdue"],
            "reviewers": summary["reviewers"],
            "ttl_seconds": summary["ttl_seconds"],
            "approvals": summary["approvals"],
            "expired": summary["expired"],
            "ineligible": summary["ineligible"],
            "stale_reviews": summary["stale"],
            "reviews": _reviews(state, task_id, analysis=analysis),
        },
        "contracts": _contracts(board, state, task),
        "evals": _evals(state, task),
        "queue": entries,
        "handoff": latest_handoff,
        "failure": task.get("trace"),
        "changes": branch_changes(board, state, task, include_diff=include_diff),
        "actions": [],
    }
    if task.get("requires_approval") and not summary["satisfied"]:
        packet["actions"].append(f"aghub review {task_id} --approve --note ...")
        if decision != "changes_requested":
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
    required = int(approval.get("required_approvals") or 1)
    approved = int(approval.get("approved_count") or 0)
    label = approval["decision"]
    if approval.get("decision") == "approved" and (required > 1 or approved != 1):
        label += f" {approved}/{required}"
    elif approval.get("decision") == "pending" and required > 1:
        label += f" {approved}/{required}"
    if approval.get("reviewer"):
        label += f" by {approval['reviewer']}"
    if approval.get("note"):
        label += f" — {approval['note']}"
    if approval.get("stale"):
        label += " (stale; re-approve the current head)"
    if approval.get("expired"):
        label += f" ({len(approval['expired'])} expired)"
    if approval.get("ineligible"):
        label += f" ({len(approval['ineligible'])} ineligible)"
    if approval.get("required") and approval.get("satisfied") is False:
        label += " (policy not satisfied)"
    lines.append(f"  review    {label}" + (" (required)" if approval["required"] else ""))
    if approval["required"]:
        policy = [f"{approved}/{required} approval" + ("" if required == 1 else "s")]
        if approval.get("ttl_seconds"):
            policy.append(f"ttl {human_age(approval['ttl_seconds'])}")
        if approval.get("required_groups"):
            missing = set(approval.get("missing_groups") or [])
            groups = [
                f"{name}" + (" (missing)" if name in missing else "")
                for name in approval["required_groups"]
            ]
            policy.append("groups " + ", ".join(groups))
        if approval.get("reviewers"):
            policy.append("reviewers " + ", ".join(approval["reviewers"]))
        lines.append(f"  policy    {' · '.join(policy)}")
    if approval.get("head"):
        lines.append(f"  head      {str(approval['head'])[:7]}")
    if approval.get("commit"):
        lines.append(f"  reviewed  {str(approval['commit'])[:7]}")

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
            flags = []
            if record.get("stale"):
                flags.append("stale")
            if record.get("expired"):
                flags.append("expired")
            if record.get("eligible") is False:
                flags.append("ineligible")
            if record.get("github_login"):
                flags.append("github " + record["github_login"])
            suffix = f" [{', '.join(flags)}]" if flags else ""
            lines.append(
                f"  {record['decision']:<18} "
                f"{record.get('reviewer') or record['agent']}{note}{suffix} "
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


# -- web review workspace ------------------------------------------------


def publish_packet(board, state, task_id, include_diff=True):
    """Mirror a review packet (with an inline patch) to the connected hub.

    The hub never runs git, so the CLI that owns the branch publishes the diff.
    `submit`, `aghub review --publish`, and the review worker call this.
    """
    if not board.hub:
        return None
    if task_id not in state.tasks:
        raise AghubError(f"unknown task {task_id}")
    packet = review_packet(board, state, task_id, include_diff=include_diff)
    changes = packet.get("changes") or {}
    patch = changes.get("patch")
    if isinstance(patch, str) and len(patch) > MAX_PUBLISHED_PATCH:
        changes["patch"] = patch[:MAX_PUBLISHED_PATCH] + "\n... (truncated by the publisher)"
        changes["truncated"] = True
    project = (state.project or {}).get("name") or "default"
    try:
        return hubclient.publish_review_packet(
            board.hub_urls, project, task_id, packet, board.hub_token
        )
    except hubclient.HubError as exc:
        hubclient.warn_once(
            f"hub at {board.hub} is unreachable ({exc}); review packet not published"
        )
        return None


def _worker_key(board, state, agent):
    if service.eval_attestation_mode(state) == service.EVAL_ED25519_SCHEME:
        return ensure_local_agent_key(board.common, agent)
    return {}


def apply_review_request(board, request, agent, force=False):
    """Sign one web review intent onto the board and push it.

    The branch head is re-resolved before signing. If the browser reviewed a
    different head, the request is rejected instead of approving new commits by
    accident. A request that was already applied is idempotent: the existing
    signed record is returned when `external_id` matches.
    """
    state = board.load()
    existing = next(
        (
            record
            for record in state.approvals
            if record.get("external_id") == request.get("id")
        ),
        None,
    )
    if existing is not None:
        return existing
    task_id = request.get("task")
    task = state.tasks.get(task_id)
    if task is None:
        raise AghubError(f"unknown task {task_id}")
    if task.get("status") == "done":
        raise AghubError(f"{task_id} is already done")
    decision = request.get("decision")
    if decision not in ("approved", "changes_requested"):
        raise AghubError(f"unknown review decision {decision!r}")
    head = branch_head(board, task, update=True)
    expected = request.get("head")
    if expected and head and not service.same_commit(expected, head):
        raise AghubError(
            f"branch moved from {str(expected)[:7]} to {str(head)[:7]} after the "
            "review was opened — refresh the workspace and submit again"
        )
    if not head:
        raise AghubError(
            f"branch {task.get('branch') or '(none)'} is not available; push it "
            "before applying the review"
        )
    key = _worker_key(board, state, agent)
    note = request.get("note") or ""
    reviewer_fn = service.approve if decision == "approved" else service.request_changes
    reviewer = request.get("reviewer") or agent
    return board.transaction(
        f"web review {task_id}",
        lambda current: reviewer_fn(
            current,
            task_id,
            agent,
            note,
            head,
            private_key=key.get("private_key"),
            public_key=key.get("public_key"),
            force=force,
            source="hub",
            external_id=request.get("id"),
            reviewer=reviewer,
        ),
        agent=agent,
    )


def apply_pending(board, agent, limit=50, force=False, out=None):
    """Apply every pending web review request once. Returns result records."""
    if not board.hub:
        raise AghubError(
            "not connected to a hub — run `aghub hub connect <url>` first"
        )
    project = (board.load().project or {}).get("name") or "default"
    try:
        data = hubclient.review_requests(
            board.hub_urls, project, status="pending", limit=limit, token=board.hub_token
        )
    except hubclient.HubError as exc:
        raise AghubError(f"cannot read review requests: {exc}")
    printer = out or (lambda line: None)
    results = []
    for request in data.get("requests") or []:
        try:
            claimed = hubclient.claim_review_request(
                board.hub_urls, request["id"], token=board.hub_token
            )
        except hubclient.HubError as exc:
            printer(f"skip {request['task']}: {exc}")
            continue
        if not claimed.get("claimed"):
            continue
        request = claimed.get("request") or request
        try:
            record = apply_review_request(board, request, agent, force=force)
        except (AghubError, hubclient.HubError) as exc:
            message = str(exc)
            try:
                hubclient.resolve_review_request(
                    board.hub_urls,
                    request["id"],
                    "rejected",
                    error=message,
                    token=board.hub_token,
                )
            except hubclient.HubError:
                pass
            printer(f"rejected {request['task']}: {message}")
            results.append({**request, "status": "rejected", "error": message})
            continue
        try:
            hubclient.resolve_review_request(
                board.hub_urls,
                request["id"],
                "applied",
                record_id=record.get("id"),
                token=board.hub_token,
            )
        except hubclient.HubError as exc:
            printer(
                f"applied {request['task']} but could not report it to the hub: {exc}"
            )
        verb = "approved" if record.get("decision") == "approved" else "requested changes on"
        printer(
            f"{verb} {record.get('task')} by {record.get('agent')} "
            f"({record.get('id')})"
        )
        results.append({**request, "status": "applied", "record": record})
    return results


def watch(board, agent, once=False, interval=5, limit=50, force=False, out=None):
    """Poll the hub for web review requests and sign them locally."""
    printer = out or (lambda line: None)
    while True:
        results = apply_pending(board, agent, limit=limit, force=force, out=printer)
        if once:
            return results
        if not results:
            printer("waiting for review requests…")
        time.sleep(max(1, interval))
