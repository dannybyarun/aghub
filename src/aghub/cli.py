import argparse
import hashlib
import json
import os
import shlex
import sys
import threading
import time

import yaml

from . import codegen, gitops, hubclient, keys, render, sandbox, service
from .board import (
    Board,
    ensure_local_agent_key,
    init_board,
    read_local_config,
    write_local_config,
)
from .errors import AghubError
from .util import ago_iso, now_iso, slug


def _resolve_agent(args, config):
    return slug(
        args.agent
        or os.environ.get("AGHUB_AGENT")
        or (config or {}).get("agent")
        or "agent-1"
    )


def _fresh_board():
    board = Board.discover()
    board.sync()
    return board


def _trunk_base(board, state):
    trunk = state.project.get("trunk", "main")
    if board.remote:
        ref = f"refs/remotes/{board.remote}/{trunk}"
        if gitops.ref_exists(board.repo, ref):
            return ref, trunk
    ref = f"refs/heads/{trunk}"
    return (ref if gitops.ref_exists(board.repo, ref) else None), trunk


def _ensure_branch(board, branch):
    if gitops.ref_exists(board.repo, f"refs/heads/{branch}"):
        return False
    state = board.load()
    trunk = state.project.get("trunk", "main")
    if board.remote and gitops.has_remote(board.repo, board.remote):
        gitops.git(
            ["fetch", board.remote, f"+refs/heads/{trunk}:refs/remotes/{board.remote}/{trunk}"],
            cwd=board.repo,
            check=False,
        )
    base, _ = _trunk_base(board, state)
    if base is None:
        raise AghubError(f"cannot create {branch}: trunk is missing")
    gitops.git(["branch", branch, base], cwd=board.repo)
    return True


def _claim_worktree(board, task, agent, requested):
    """Create an isolated checkout for a freshly claimed task."""
    if requested is None:
        return None
    if requested:
        path = os.path.abspath(os.path.expanduser(requested))
    else:
        repo = os.path.abspath(board.repo)
        path = os.path.join(
            os.path.dirname(repo),
            f"{os.path.basename(repo)}-{slug(agent)}-{task['id']}",
        )
    if os.path.exists(path):
        if not os.path.isdir(path) or os.listdir(path):
            raise AghubError(f"worktree path is not an empty directory: {path}")
        os.rmdir(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    result = gitops.git(
        ["worktree", "add", path, task["branch"]], cwd=board.repo, check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise AghubError(
            f"could not create worktree at {path}" + (f": {detail}" if detail else "")
        )
    return path


def _undo_claim(board, project, task_id, agent, leased):
    """Give a claim back if the local checkout could not be prepared."""
    try:
        board.transaction(
            f"release {task_id}",
            lambda current: service.release(current, task_id, agent),
            agent=agent,
        )
    except Exception:
        pass
    if leased:
        try:
            hubclient.release(
                board.hub_urls, project, task_id, agent, token=board.hub_token
            )
        except hubclient.HubError:
            pass


def _diff_summary(board, state, task):
    branch = task.get("branch")
    if not branch or not gitops.ref_exists(board.repo, f"refs/heads/{branch}"):
        return ""
    base, _ = _trunk_base(board, state)
    if base is None:
        return ""
    proc = gitops.git(
        ["diff", "--stat", base, f"refs/heads/{branch}"], cwd=board.repo, check=False
    )
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def _put_contract(state, name, version, status, agent, blob=None, filename=None):
    service.require_role(state, agent, "writer", "edit contracts")
    versions = state.contracts.setdefault(name, {})
    meta = versions.get(version)
    if meta and meta.get("status") == "frozen":
        raise AghubError(f"{name}@{version} is frozen and cannot be changed")
    if meta is None:
        meta = {
            "name": name,
            "version": version,
            "status": "draft",
            "artifact": None,
            "sha256": None,
            "created_at": now_iso(),
            "created_by": agent,
            "frozen_at": None,
            "frozen_by": None,
        }
        versions[version] = meta
    if blob is not None:
        extension = os.path.splitext(filename or "")[1] or ".yaml"
        artifact = f"artifact{extension}"
        state.new_artifacts[os.path.join("contracts", name, str(version), artifact)] = blob
        meta["artifact"] = artifact
        meta["sha256"] = hashlib.sha256(blob).hexdigest()
    if status == "frozen":
        if not meta.get("artifact"):
            raise AghubError("freeze needs a contract file — pass --from <file>")
        meta["status"] = "frozen"
        meta["frozen_at"] = now_iso()
        meta["frozen_by"] = agent
    else:
        meta["status"] = "draft"
    meta["updated_at"] = now_iso()
    state.emit("contract." + status, agent=agent, contract=f"{name}@{version}")
    return meta


# -- commands ------------------------------------------------------------


def cmd_init(args):
    repo = gitops.repo_root()
    remote = None if args.no_remote else args.remote
    if not args.no_remote and remote is None and gitops.has_remote(repo, "origin"):
        remote = "origin"
    agent = _resolve_agent(args, None)
    board = init_board(
        repo,
        args.project,
        args.trunk,
        args.ttl,
        remote,
        check=args.check,
        eval_ttl=args.eval_ttl,
        attestation=args.attestation,
        check_sandbox=args.check_sandbox,
        check_timeout=args.check_timeout,
        require_eval_sandbox=args.require_eval_sandbox,
        scope=args.scope,
    )
    config = read_local_config(board.common)
    config["agent"] = agent
    write_local_config(board.common, config)
    print(f'board {board.branch} ready for "{args.project}"')
    print(f"  worktree  {board.dir}")
    print(f"  remote    {remote or '(local only)'}")
    state = board.load()
    print(f"  check     {state.project.get('check') or '(none)'}")
    print(f"  eval ttl  {state.project.get('eval_ttl_seconds', service.EVAL_TTL_DEFAULT)}s")
    print(f"  eval sign {state.project.get('eval_attestation') or 'unsigned'}")
    print(f"  review    {service.describe_review_policy(state)}")
    print(f"  scope     {state.project.get('scope') or '(whole repository)'}")
    print(f"  agent     {agent}")
    if not state.contracts:
        print("next: aghub contract freeze api 1 --from contracts/api.yaml")
    return 0


def cmd_agent(args):
    repo = gitops.repo_root()
    common = gitops.common_dir(repo)
    config = read_local_config(common)
    if not config:
        raise AghubError("aghub is not initialized here — run `aghub init --project <name>`")
    if args.id:
        config["agent"] = slug(args.id)
        write_local_config(common, config)
    agent = config.get("agent") or "unset"
    if args.revoke_key:
        if agent == "unset":
            raise AghubError("set an agent id first — `aghub agent <id>`")
        board = Board.discover()
        board.sync()
        record = board.transaction(
            f"revoke key {agent}",
            lambda current: service.revoke_agent_key(
                current, agent, args.key_id, agent
            ),
            agent=agent,
        )
        print(f"revoked {agent} key(s) on the board")
        print(
            "  next: aghub agent --rotate-key   # publish a fresh key before signing again"
        )
        return 0
    if args.key or args.rotate_key:
        if agent == "unset":
            raise AghubError("set an agent id first — `aghub agent <id>`")
        entry = ensure_local_agent_key(common, agent, rotate=args.rotate_key)
        board = Board.discover()
        board.sync()
        record = board.transaction(
            f"agent key {agent}",
            lambda current: service.register_agent_key(
                current, agent, entry["public_key"]
            ),
            agent=agent,
        )
        if args.rotate_key:
            print(f"rotated {agent} ed25519 key")
        print(f"{agent} · ed25519 {record['key_id']}")
        print(f"  public  {entry['public_key']}")
        print(f"  local   private key stays in .git/aghub.local.json")
        return 0
    print(agent)
    return 0


def cmd_acl(args):
    board = _fresh_board()
    state = board.load()
    agent = _resolve_agent(args, read_local_config(board.common))
    if args.acl_command == "list":
        roles = service.role_map(state)
        if roles is None:
            print("ACLs are open (any writer may act) — run `aghub acl init` to lock them")
            return 0
        for target in sorted(roles):
            print(f"{target:<16} {roles[target]}")
        return 0
    if args.acl_command == "init":
        roles = board.transaction(
            "acl init",
            lambda current: service.init_acl(current, agent),
            agent=agent,
        )
        print(f"ACLs enabled; {agent} is admin")
        return 0
    if args.acl_command == "set":
        roles = board.transaction(
            f"acl {args.agent_id} {args.role}",
            lambda current: service.set_role(
                current, agent, slug(args.agent_id), args.role
            ),
            agent=agent,
        )
        print(f"{slug(args.agent_id)} is {roles.get(slug(args.agent_id)) or 'reader'}")
        return 0
    if args.acl_command == "remove":
        roles = board.transaction(
            f"acl remove {args.agent_id}",
            lambda current: service.remove_role(
                current, agent, slug(args.agent_id)
            ),
            agent=agent,
        )
        print(f"removed {slug(args.agent_id)} from the board ACLs")
        return 0
    if args.acl_command == "revoke-key":
        record = board.transaction(
            f"revoke key {args.agent_id}",
            lambda current: service.revoke_agent_key(
                current, slug(args.agent_id), args.key_id, agent
            ),
            agent=agent,
        )
        revoked = [
            item.get("key_id") if isinstance(item, dict) else item
            for item in record.get("revoked_key_ids") or []
        ]
        print(f"revoked {args.agent_id} key(s): {', '.join(str(item) for item in revoked)}")
        return 0
    raise AghubError(f"unknown acl command {args.acl_command}")


def cmd_policy(args):
    board = _fresh_board()
    state = board.load()
    agent = _resolve_agent(args, read_local_config(board.common))

    if args.policy_command == "show":
        policy = service.normalize_review_policy(state.project)
        if args.json:
            print(json.dumps(policy, indent=2, sort_keys=False))
        else:
            print(yaml.safe_dump({"review": policy}, sort_keys=False).rstrip())
        return 0

    updates = {}
    if args.clear:
        updates["clear"] = True
    if args.approvals is not None:
        updates["approvals"] = args.approvals
    if args.no_ttl:
        updates["clear_ttl"] = True
    elif args.ttl is not None:
        updates["ttl_seconds"] = args.ttl
    if args.clear_reviewers:
        updates["clear_reviewers"] = True
    if args.reviewers:
        updates["reviewers"] = args.reviewers
    groups = {}
    for spec in args.groups or []:
        name, sep, members = spec.partition("=")
        if not sep or not name.strip():
            raise AghubError("--group needs NAME=AGENT[,AGENT...]")
        groups[name.strip()] = [
            member.strip() for member in members.split(",") if member.strip()
        ]
    if groups:
        updates["groups"] = groups
    if args.clear_groups:
        updates["clear_groups"] = True
    if args.clear_required_groups:
        updates["clear_required_groups"] = True
    if args.required_groups:
        updates["required_groups"] = args.required_groups
    github = {}
    for spec in args.github or []:
        login, sep, target = spec.partition("=")
        if not sep or not login.strip() or not target.strip():
            raise AghubError("--github needs LOGIN=AGENT")
        github[login.strip()] = target.strip()
    if github:
        updates["github"] = github
    if args.clear_github:
        updates["clear_github"] = True
    if not updates:
        raise AghubError("nothing to change — pass --approvals, --ttl, --group, ...")

    policy = board.transaction(
        "review policy",
        lambda current: service.set_review_policy(current, updates, agent),
        agent=agent,
    )
    if args.json:
        print(json.dumps(policy, indent=2, sort_keys=False))
    else:
        print(yaml.safe_dump({"review": policy}, sort_keys=False).rstrip())
    return 0


def _review_key(board, state, agent):
    if service.eval_attestation_mode(state) == service.EVAL_ED25519_SCHEME:
        return ensure_local_agent_key(board.common, agent)
    return {}


def _review_target(board, task, explicit=None, required=False):
    """Resolve the commit a review applies to, preferring the pushed branch."""
    if explicit:
        return explicit
    from . import review

    head = review.branch_head(board, task, update=True)
    if head or not required:
        return head
    branch = task.get("branch")
    hint = (
        f"push {branch} (or pass --commit SHA)"
        if branch
        else "claim the task, push its branch, and try again"
    )
    raise AghubError(f"{task['id']} has no commit to review — {hint}")


def _record_review(
    board,
    agent,
    task_id,
    decision,
    note="",
    commit=None,
    force=False,
    source=None,
    external_id=None,
    reviewer=None,
    github_login=None,
):
    """Record an approval or a changes-requested review under the local key."""
    state = board.load()
    key = _review_key(board, state, agent)
    reviewer_fn = service.approve if decision == "approved" else service.request_changes
    label = "approve" if decision == "approved" else "request changes"
    return board.transaction(
        f"{label} {task_id}",
        lambda current: reviewer_fn(
            current,
            task_id,
            agent,
            note,
            commit,
            private_key=key.get("private_key"),
            public_key=key.get("public_key"),
            force=force,
            source=source,
            external_id=external_id,
            reviewer=reviewer,
            github_login=github_login,
        ),
        agent=agent,
    )


def cmd_approve(args):
    board = _fresh_board()
    state = board.load()
    agent = _resolve_agent(args, read_local_config(board.common))
    task = state.tasks.get(args.task)
    if task is None:
        raise AghubError(f"unknown task {args.task}")
    commit = _review_target(board, task, args.commit, required=True)
    record = _record_review(
        board, agent, args.task, "approved", args.note, commit, args.force
    )
    print(f"approved {record['task']} by {record['agent']} ({record['id']})")
    if record.get("note"):
        print(f"  note {record['note']}")
    return 0


def cmd_inbox(args):
    from . import review

    board = _fresh_board()
    state = board.load()
    items = review.inbox(state, board)
    project = (state.project or {}).get("name")
    if args.json:
        print(
            json.dumps(
                {"project": project, "items": items}, indent=2, sort_keys=False
            )
        )
    else:
        print(review.render_inbox(project, items))
    return 1 if args.check and items else 0


def cmd_review(args):
    from . import review

    board = _fresh_board()
    state = board.load()
    agent = _resolve_agent(args, read_local_config(board.common))

    if args.watch:
        if args.task:
            raise AghubError("`review --watch` does not take a task id")
        results = review.watch(
            board,
            agent,
            once=args.once,
            interval=args.interval,
            limit=args.limit,
            force=args.force,
            out=(None if args.json else (lambda line: print(line, flush=True))),
        )
        if args.json:
            print(json.dumps(results, indent=2, sort_keys=False))
        return 0

    if not args.task:
        raise AghubError("review needs a task id (or --watch for web requests)")
    task = state.tasks.get(args.task)
    if task is None:
        raise AghubError(f"unknown task {args.task}")

    if args.approve or args.request_changes:
        decision = "approved" if args.approve else "changes_requested"
        commit = _review_target(
            board, task, args.commit, required=bool(args.approve)
        )
        record = _record_review(
            board,
            agent,
            args.task,
            decision,
            args.note,
            commit,
            args.force,
        )
        if args.publish:
            review.publish_packet(board, board.load(), args.task)
        if args.json:
            print(json.dumps(record, indent=2, sort_keys=False))
        else:
            verb = "approved" if decision == "approved" else "requested changes on"
            print(f"{verb} {record['task']} by {record['agent']} ({record['id']})")
            if record.get("note"):
                print(f"  note {record['note']}")
        return 0

    packet = review.review_packet(board, state, args.task, include_diff=args.diff)
    if args.publish:
        result = review.publish_packet(board, state, args.task)
        if result:
            print(
                f"published review packet for {args.task} at "
                f"{board.hub}/?project={packet['project']}"
            )
        elif not board.hub:
            raise AghubError("not connected to a hub — run `aghub hub connect <url>`")
    if args.json:
        print(json.dumps(packet, indent=2, sort_keys=False))
    else:
        print(review.render_review(packet, include_diff=args.diff))
    return 0


def cmd_registry(args):
    board = _fresh_board()
    state = board.load()
    agent = _resolve_agent(args, read_local_config(board.common))

    if args.registry_command == "profile":
        if args.model is None and args.skills is None and args.note is None:
            raise AghubError("nothing to update — pass --model, --skills, or --note")
        skills = None
        if args.skills is not None:
            skills = [slug(part) for part in args.skills.replace(",", " ").split() if part]
        record = board.transaction(
            f"profile {agent}",
            lambda current: service.profile_agent(
                current, agent, args.model, skills, args.note
            ),
            agent=agent,
        )
        print(f"profile {agent}")
        print(yaml.safe_dump(record, sort_keys=False).rstrip())
        return 0

    if args.registry_command == "show":
        target = slug(args.agent_id) if args.agent_id else agent
        record = state.agents.get(target)
        if record is None:
            raise AghubError(f"unknown agent {target}")
        print(yaml.safe_dump(record, sort_keys=False).rstrip())
        latest = service.latest_eval(state, target)
        if latest:
            print("\nlatest eval")
            print(yaml.safe_dump(latest, sort_keys=False).rstrip())
        return 0

    rows = sorted(state.agents)
    if not rows:
        print("no agents registered")
        return 0
    for agent_id in rows:
        record = state.agents[agent_id]
        latest = service.latest_eval(state, agent_id)
        verdict = "—" if latest is None else (
            f"{latest.get('suite')} {'ok' if latest.get('passed') else 'FAIL'}"
        )
        print(f"{agent_id:<12} {record.get('model') or '—':<22} {verdict}")
    return 0


def _eval_local_key(board, state, agent):
    mode = service.eval_attestation_mode(state)
    if mode == service.EVAL_HMAC_SCHEME:
        return {}
    return ensure_local_agent_key(board.common, agent)


def _run_eval(board, state, agent, suite, command, timeout, mode, task):
    key = _eval_local_key(board, state, agent)
    mode = service.eval_mode(state, state.tasks.get(task) if task else None, mode)
    started = time.monotonic()
    result = sandbox.run(command, board.repo, timeout=timeout, mode=mode)
    duration_ms = int((time.monotonic() - started) * 1000)
    record = service.eval_record(
        agent,
        suite,
        command,
        result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_ms=duration_ms,
        timeout=timeout,
        task=task,
        branch=gitops.current_branch(board.repo),
        commit=gitops.short(board.repo, "HEAD"),
        tree=gitops.git(["rev-parse", "HEAD^{tree}"], cwd=board.repo).stdout.strip(),
        worktree=gitops.worktree_fingerprint(board.repo),
        timed_out=result.timed_out,
        sandbox=result.sandbox,
    )
    return board.transaction(
        f"eval {suite} {agent}",
        lambda current: service.record_eval(
            current,
            record,
            private_key=key.get("private_key"),
            public_key=key.get("public_key"),
        ),
        agent=agent,
    )


def _print_eval(record):
    print(
        f"eval {record['id']} {record.get('suite')} "
        f"{'passed' if record.get('passed') else 'FAILED'} for {record.get('agent')} "
        f"({record.get('duration_ms')}ms, exit {record.get('exit_code')})"
    )
    print(
        f"  {record.get('branch') or 'detached'} {record.get('commit') or '-'} · "
        f"{record.get('task') or 'no task'} · {record.get('machine') or 'unknown host'}"
    )
    policy = record.get("sandbox") or {}
    print(
        f"  sandbox   {policy.get('backend') or 'none'} · "
        f"network {policy.get('network') or 'host'} · "
        f"{'snapshot' if policy.get('snapshot') else 'live checkout'}"
    )
    print(f"  fingerprint {str(record.get('fingerprint') or '')[:16]}")
    attestation = record.get("attestation") or {}
    if attestation:
        print(
            f"  attested  {attestation.get('scheme')} "
            f"{attestation.get('key_id')} (verify: aghub eval verify {record['id']})"
        )
    else:
        print("  attested  unsigned (the board has no eval policy)")
    if not record.get("passed"):
        for stream in ("stdout", "stderr"):
            text = record.get(stream)
            if text:
                print(f"--- {stream} ---")
                print(text)
    print(f"  show: aghub eval show {record['id']}")


def cmd_eval(args):
    board = _fresh_board()
    state = board.load()

    if args.eval_command == "list":
        records = state.evals
        if args.agent_id:
            records = [record for record in records if record.get("agent") == args.agent_id]
        if args.suite:
            records = [record for record in records if record.get("suite") == args.suite]
        if not records:
            print("no evals recorded")
            return 0
        for record in records:
            verdict = "pass" if record.get("passed") else "FAIL"
            verified = service.eval_verified(state, record)
            attestation = "signed" if verified else ("BAD" if verified is False else "unsigned")
            print(
                f"{record['id']}  {record.get('suite', ''):<12} "
                f"{record.get('agent', ''):<10} {verdict:<4} "
                f"{attestation:<8} {record.get('duration_ms', 0)}ms"
            )
        return 0

    if args.eval_command == "show":
        record = next(
            (item for item in state.evals if item.get("id") == args.eval_id), None
        )
        if record is None:
            raise AghubError(f"unknown eval {args.eval_id}")
        print(yaml.safe_dump(record, sort_keys=False).rstrip())
        return 0

    if args.eval_command == "verify":
        record = next(
            (item for item in state.evals if item.get("id") == args.eval_id), None
        )
        if record is None:
            raise AghubError(f"unknown eval {args.eval_id}")
        verified = service.eval_verified(state, record)
        if verified:
            attestation = record.get("attestation") or {}
            print(
                f"eval {record['id']} verified "
                f"({attestation.get('scheme')} {attestation.get('key_id')})"
            )
            return 0
        if verified is False:
            print(f"eval {record['id']} signature INVALID", file=sys.stderr)
            return 1
        mode = service.eval_attestation_mode(state)
        if mode == service.EVAL_ED25519_SCHEME:
            print(
                f"eval {record['id']} is not signed by its agent's published key",
                file=sys.stderr,
            )
        elif mode == service.EVAL_HMAC_SCHEME:
            print(f"eval {record['id']} is unsigned", file=sys.stderr)
        else:
            print(
                f"eval {record['id']} cannot be verified: the board has no eval policy",
                file=sys.stderr,
            )
        return 1

    if args.eval_command in ("rerun", "diff"):
        def find(eval_id):
            record = next(
                (item for item in state.evals if item.get("id") == eval_id), None
            )
            if record is None:
                raise AghubError(f"unknown eval {eval_id}")
            return record

        if args.eval_command == "rerun":
            previous = find(args.eval_id)
            agent = previous.get("agent") or _resolve_agent(
                args, read_local_config(board.common)
            )
            policy = previous.get("sandbox") or {}
            stored_mode = policy.get("mode")
            if stored_mode not in ("auto", "off", "require"):
                stored_mode = "off" if policy.get("backend") == "none" else "auto"
            record = _run_eval(
                board,
                state,
                agent,
                previous.get("suite"),
                previous.get("command"),
                args.timeout if args.timeout is not None else previous.get("timeout", 300),
                args.sandbox or stored_mode,
                previous.get("task"),
            )
            _print_eval(record)
            if record.get("fingerprint") == previous.get("fingerprint"):
                print(f"  reproducible fingerprint matches {previous['id']}")
            else:
                print(
                    f"  fingerprint changed from {str(previous.get('fingerprint'))[:16]} "
                    f"to {str(record.get('fingerprint'))[:16]}"
                )
            return 0 if record["passed"] else 1

        left, right = find(args.left), find(args.right)
        fields = (
            "suite",
            "agent",
            "command",
            "passed",
            "exit_code",
            "duration_ms",
            "commit",
            "tree",
            "worktree",
            "fingerprint",
        )
        changes = []
        for field in fields:
            if left.get(field) != right.get(field):
                changes.append(
                    {"field": field, "left": left.get(field), "right": right.get(field)}
                )
        left_policy = left.get("sandbox") or {}
        right_policy = right.get("sandbox") or {}
        for field in ("mode", "backend", "network", "snapshot"):
            if left_policy.get(field) != right_policy.get(field):
                changes.append(
                    {
                        "field": f"sandbox.{field}",
                        "left": left_policy.get(field),
                        "right": right_policy.get(field),
                    }
                )
        payload = {
            "left": left.get("id"),
            "right": right.get("id"),
            "same_fingerprint": left.get("fingerprint") == right.get("fingerprint"),
            "changes": changes,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"eval {left['id']} → {right['id']}")
            if not changes:
                print("  identical scorecards")
            for change in changes:
                print(
                    f"  {change['field']:<18} {change['left']!r} → {change['right']!r}"
                )
            if payload["same_fingerprint"]:
                print("  fingerprint matches")
        return 0

    if args.eval_command == "ttl":
        if args.seconds is None:
            print(service.eval_ttl(state))
            return 0
        agent = _resolve_agent(args, read_local_config(board.common))
        seconds = max(0, int(args.seconds))
        result = board.transaction(
            f"eval ttl {seconds}",
            lambda current: service.set_eval_ttl(current, seconds, agent),
            agent=agent,
        )
        print(f"eval ttl {result}s")
        return 0

    agent = _resolve_agent(args, read_local_config(board.common))
    if args.task and args.task not in state.tasks:
        raise AghubError(f"unknown task {args.task}")
    record = _run_eval(
        board,
        state,
        agent,
        slug(args.suite),
        args.command,
        args.timeout,
        args.sandbox,
        args.task,
    )
    _print_eval(record)
    return 0 if record["passed"] else 1


def cmd_status(args):
    board = _fresh_board()
    state = board.load()
    hub_mode = _hub_presence(board, state)
    if args.json:
        print(render.status_json(state, board, hub_mode=hub_mode))
    else:
        print(render.status_text(state, board, for_agent=args.for_agent, hub_mode=hub_mode))
    return 0


def _signature_state(state, record):
    verdict = service.verify_attestation(state, record)
    if verdict is True:
        return "ok"
    return "invalid" if verdict is False else "unsigned"


def _verification_report(board, state):
    from .board import verify_event_chain

    chain = verify_event_chain(state.events, state.agents)
    checks = []
    for record in state.evals:
        checks.append(
            {
                "kind": "eval",
                "id": record.get("id"),
                "agent": record.get("agent"),
                "status": (
                    "ok"
                    if service.eval_verified(state, record) is True
                    else "invalid"
                    if service.eval_verified(state, record) is False
                    else "unsigned"
                ),
            }
        )
    for record in state.handoffs:
        checks.append(
            {
                "kind": "handoff",
                "id": record.get("id"),
                "agent": record.get("agent"),
                "status": _signature_state(state, record),
            }
        )
    for entry in state.queue:
        submission = entry.get("submission")
        if submission:
            checks.append(
                {
                    "kind": "queue.submit",
                    "id": submission.get("id") or entry.get("id"),
                    "agent": submission.get("agent"),
                    "status": _signature_state(state, submission),
                }
            )
        if entry.get("attestation"):
            checks.append(
                {
                    "kind": "queue.result",
                    "id": entry.get("id"),
                    "agent": entry.get("agent"),
                    "status": _signature_state(state, entry),
                }
            )
    for record in state.approvals:
        checks.append(
            {
                "kind": "approval",
                "id": record.get("id"),
                "agent": record.get("agent"),
                "status": _signature_state(state, record),
            }
        )
    return chain, checks


def cmd_verify(args):
    board = Board.discover()
    board.sync()
    state = board.load()
    chain, checks = _verification_report(board, state)
    invalid = [item for item in checks if item["status"] == "invalid"]
    unsigned = [item for item in checks if item["status"] == "unsigned"]
    if getattr(args, "json", False):
        print(
            json.dumps(
                {"chain": chain, "records": checks}, indent=2, sort_keys=False
            )
        )
    else:
        print(
            f"event chain  {'ok' if chain['ok'] else 'BROKEN'} · "
            f"{chain['events']} events · {chain['signed']} signed · "
            f"{chain['unsigned']} unsigned · {chain['legacy']} legacy"
        )
        if chain["head"]:
            print(f"  head {chain['head'][:16]}")
        for error in chain["errors"][:10]:
            print(f"  ERROR {error}")
        counts = {}
        for item in checks:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        print(
            "records      "
            + " · ".join(
                f"{counts.get(key, 0)} {key}"
                for key in ("ok", "invalid", "unsigned")
            )
        )
        for item in invalid[:20]:
            print(f"  INVALID {item['kind']} {item['id']} ({item['agent']})")
        if unsigned and getattr(args, "strict", False):
            for item in unsigned[:20]:
                print(f"  UNSIGNED {item['kind']} {item['id']} ({item['agent']})")
    if not chain["ok"] or invalid:
        return 1
    if getattr(args, "strict", False) and unsigned:
        return 1
    return 0


def cmd_board(args):
    if getattr(args, "board_command", None) == "verify":
        return cmd_verify(args)
    return cmd_status(args)


def cmd_next(args):
    board = _fresh_board()
    state = board.load()
    agent = _resolve_agent(args, read_local_config(board.common))
    task = service.next_ready(state, agent)
    if task is None:
        print("nothing is ready")
        for candidate in sorted(state.tasks.values(), key=lambda item: item["id"]):
            status = service.effective_status(state, candidate)
            if status == "blocked":
                print(f"  {candidate['id']} {candidate['title']}: " + "; ".join(service.blockers(state, candidate)))
            elif status == "ready":
                gaps = service.eval_gate(state, candidate, agent)
                if gaps:
                    print(f"  {candidate['id']} {candidate['title']}: eval " + "; ".join(gaps))
        return 0
    print(f"next: {task['id']} {task['title']}")
    print(f"  paths      {', '.join(task.get('paths') or []) or '—'}")
    print(f"  contracts  {', '.join(task.get('contracts') or []) or '—'}")
    if task.get("trace"):
        trace = task["trace"]
        print(f"  trace      {trace.get('kind')} — {trace.get('reason')}")
        print(f"  show       aghub task show {task['id']}")
    print(f"claim it: aghub claim {task['id']}")
    return 0


def cmd_task(args):
    board = _fresh_board()
    state = board.load()
    agent = _resolve_agent(args, read_local_config(board.common))
    if args.task_command == "add":
        task = board.transaction(
            f"add {args.title}",
            lambda current: service.add_task(
                current,
                args.title,
                args.paths,
                args.contract,
                args.depends,
                args.acceptance,
                agent,
                args.evals,
                require_sandbox=args.requires_sandbox,
                requires_approval=args.requires_approval,
                required_approvals=args.required_approvals,
                review_groups=args.review_groups,
                reviewers=args.reviewers,
                review_ttl=args.review_ttl,
            ),
            agent=agent,
        )
        print(f"added {task['id']} {task['title']}")
        print(f"  paths  {', '.join(task['paths']) or '(none — submit will reject unclaimed paths)'}")
        if task.get("evals"):
            print(f"  evals  {', '.join(task['evals'])}")
        if task.get("requires_approval"):
            print(f"  review {service.describe_task_review_policy(state, task)}")
        return 0
    if args.task_command == "show":
        task = state.tasks.get(args.task)
        if task is None:
            raise AghubError(f"unknown task {args.task}")
        print(yaml.safe_dump(task, sort_keys=False).rstrip())
        return 0
    for task in sorted(state.tasks.values(), key=lambda item: item["id"]):
        status = service.effective_status(state, task)
        marker = " fix" if (task.get("source") or {}).get("task") else (" fail" if task.get("trace") else "")
        print(f"{task['id']}  {status:<8}{marker:<4} {task['title']}")
    return 0


def _format_contract_diff(name, old_version, new_version, diff):
    lines = [f"{name}@{old_version} → {name}@{new_version}"]
    changes = []
    for route in diff.get("routes_added") or []:
        changes.append(f"  + route      {route}")
    for route in diff.get("routes_removed") or []:
        changes.append(f"  - route      {route}")
    for operation in diff.get("operations_added") or []:
        changes.append(f"  + operation  {operation['method']} {operation['path']}")
    for operation in diff.get("operations_removed") or []:
        changes.append(f"  - operation  {operation['method']} {operation['path']}")
    for operation in diff.get("operations_changed") or []:
        fields = ", ".join(operation.get("fields") or [])
        changes.append(
            f"  ~ operation  {operation['method']} {operation['path']} ({fields})"
        )
    for schema in diff.get("schemas_added") or []:
        changes.append(f"  + schema     {schema}")
    for schema in diff.get("schemas_removed") or []:
        changes.append(f"  - schema     {schema}")
    for schema, detail in sorted((diff.get("schemas_changed") or {}).items()):
        fields = []
        if detail.get("fields_added"):
            fields.append("+" + ",".join(detail["fields_added"]))
        if detail.get("fields_removed"):
            fields.append("-" + ",".join(detail["fields_removed"]))
        if detail.get("fields"):
            fields.append("~" + ",".join(detail["fields"]))
        props = []
        if detail.get("properties_added"):
            props.append("+" + ",".join(detail["properties_added"]))
        if detail.get("properties_removed"):
            props.append("-" + ",".join(detail["properties_removed"]))
        if detail.get("properties_changed"):
            props.append("~" + ",".join(detail["properties_changed"]))
        suffix = ""
        if fields:
            suffix += f" fields: {', '.join(fields)}"
        if props:
            suffix += f" props: {', '.join(props)}"
        changes.append(f"  ~ schema     {schema}{suffix}")
    if not changes:
        lines.append("  no structural changes")
    else:
        lines.extend(changes)
    return "\n".join(lines)


def cmd_contract(args):
    board = _fresh_board()
    state = board.load()
    agent = _resolve_agent(args, read_local_config(board.common))

    if args.contract_command in ("draft", "freeze"):
        blob = None
        if args.source:
            if not os.path.exists(args.source):
                raise AghubError(f"no such file: {args.source}")
            with open(args.source, "rb") as handle:
                blob = handle.read()
        status = "frozen" if args.contract_command == "freeze" else "draft"
        meta = board.transaction(
            f"contract {status} {args.name}@{args.version}",
            lambda current: _put_contract(
                current, args.name, args.version, status, agent, blob, args.source
            ),
            agent=agent,
        )
        verb = "froze" if status == "frozen" else "drafted"
        print(f"{verb} {args.name}@{args.version} (sha256 {meta['sha256'][:12] if meta.get('sha256') else '—'})")
        return 0

    if args.contract_command == "show":
        versions = state.contracts.get(args.name) or {}
        if not versions:
            raise AghubError(f"unknown contract {args.name}")
        version = args.version or max(versions)
        meta = versions.get(version)
        if meta is None:
            raise AghubError(f"unknown contract {args.name}@{version}")
        print(yaml.safe_dump(meta, sort_keys=False).rstrip())
        if meta.get("artifact"):
            path = os.path.join(board.dir, "contracts", args.name, str(version), meta["artifact"])
            if os.path.exists(path):
                print("\n--- artifact ---")
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    print(handle.read().rstrip())
        return 0

    if args.contract_command == "diff":
        versions = state.contracts.get(args.name) or {}
        if not versions:
            raise AghubError(f"unknown contract {args.name}")
        frozen = service.frozen_versions(state, args.name)
        if args.new_version is not None:
            old_version, new_version = args.old_version, args.new_version
        else:
            if len(frozen) < 2:
                raise AghubError(
                    f"{args.name} needs two frozen versions to diff; pass OLD NEW"
                )
            old_version, new_version = frozen[-2], frozen[-1]
        if old_version is None:
            raise AghubError("contract diff needs an old and a new version")
        diff = service.contract_diff(
            board, state, args.name, old_version, new_version
        )
        if diff is None:
            raise AghubError(
                f"cannot diff {args.name}@{old_version} → {args.name}@{new_version}: "
                "a frozen artifact is missing or is not OpenAPI"
            )
        if args.json:
            print(
                json.dumps(
                    {
                        "name": args.name,
                        "from": old_version,
                        "to": new_version,
                        "diff": diff,
                    },
                    indent=2,
                    sort_keys=False,
                )
            )
        else:
            print(
                _format_contract_diff(
                    args.name, old_version, new_version, diff
                )
            )
        return 0

    for name in sorted(state.contracts):
        for version in sorted(state.contracts[name]):
            meta = state.contracts[name][version]
            print(f"{name}@{version}  {meta.get('status'):<7} {meta.get('sha256', '')[:12]}")
    return 0


def cmd_codegen(args):
    board = _fresh_board()
    state = board.load()
    versions = state.contracts.get(args.name) or {}
    if not versions:
        raise AghubError(f"unknown contract {args.name}")
    if args.version is None:
        frozen = [version for version, meta in versions.items() if meta.get("status") == "frozen"]
        if not frozen:
            raise AghubError(f"{args.name} has no frozen version — freeze one first")
        version = max(frozen)
    else:
        version = args.version
    meta = versions.get(version)
    if meta is None:
        raise AghubError(f"unknown contract {args.name}@{version}")
    if meta.get("status") != "frozen":
        raise AghubError(
            f"{args.name}@{version} is {meta.get('status')} — not frozen; codegen only runs on frozen contracts"
        )
    if not meta.get("artifact"):
        raise AghubError(f"{args.name}@{version} has no artifact to generate from")
    source = os.path.join(board.dir, "contracts", args.name, str(version), meta["artifact"])
    if not os.path.exists(source):
        raise AghubError(f"contract artifact is missing: {source}")
    with open(source, "rb") as handle:
        blob = handle.read()
    files = codegen.generate(
        args.name, version, meta, codegen.load_spec(blob), args.target
    )
    out = os.path.abspath(os.path.expanduser(args.out))

    if args.check:
        stale = []
        for filename in sorted(files):
            target = os.path.join(out, filename)
            if not os.path.exists(target):
                stale.append(filename)
                continue
            with open(target, "r", encoding="utf-8") as handle:
                if handle.read() != files[filename]:
                    stale.append(filename)
        if stale:
            print("codegen drift: " + ", ".join(stale), file=sys.stderr)
            print(f"run: aghub codegen {args.name} {version} --out {args.out}", file=sys.stderr)
            return 1
        print(f"codegen clean: {len(files)} file(s) match {args.name}@{version}")
        return 0

    os.makedirs(out, exist_ok=True)
    for filename in sorted(files):
        target = os.path.join(out, filename)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(files[filename])
        os.replace(tmp, target)
    print(f"generated {len(files)} file(s) from {args.name}@{version} in {out}")
    for filename in sorted(files):
        print(f"  {filename}")
    if args.target in ("all", "mock"):
        print(f"mock: python3 {os.path.join(out, codegen.mock_filename(args.name))} --port 4010")
    return 0


def _hub_presence(board, state):
    """Overlay live hub presence onto the git board. Returns live/offline/None."""
    if not board.hub:
        return None
    try:
        payload = hubclient.board(
            board.hub_urls, state.project.get("name") or "default", board.hub_token
        )
    except hubclient.HubError:
        return "offline"
    for agent in payload.get("agents", []):
        record = dict(state.agents.get(agent["id"], {}))
        record.update(agent)
        state.agents[agent["id"]] = record
    return "live"


def cmd_claim(args):
    board = _fresh_board()
    state = board.load()
    agent = _resolve_agent(args, read_local_config(board.common))
    project = state.project.get("name") or "default"
    ttl = int(state.project.get("ttl_seconds", 600))
    task = service.validate_claim(state, args.task, agent, args.steal, args.force)

    leased = False
    if board.hub:
        try:
            hubclient.claim(
                board.hub_urls,
                project,
                task["id"],
                agent,
                task.get("paths") or [],
                ttl,
                args.steal,
                args.force,
                board.hub_token,
            )
            leased = True
        except hubclient.HubConflict as exc:
            raise AghubError(f"hub rejected the claim: {exc}")
        except hubclient.HubError as exc:
            hubclient.warn_once(
                f"hub at {board.hub} is unreachable ({exc}); falling back to git-only claims"
            )

    try:
        task = board.transaction(
            f"claim {args.task}",
            lambda current: service.claim(current, args.task, agent, args.steal, args.force),
            agent=agent,
        )
    except Exception:
        if leased:
            try:
                hubclient.release(board.hub_urls, project, args.task, agent, token=board.hub_token)
            except hubclient.HubError:
                pass
        raise

    worktree = None
    try:
        created = _ensure_branch(board, task["branch"])
        worktree = _claim_worktree(board, task, agent, args.worktree)
    except Exception:
        _undo_claim(board, project, task["id"], agent, leased)
        raise

    print(f"claimed {task['id']} {task['title']} ({agent})")
    print(f"  branch  {task['branch']}{' (new)' if created else ''}")
    print(f"  paths   {', '.join(task.get('paths') or []) or '—'}")
    if worktree:
        print(f"  worktree {worktree}")
    if board.hub:
        print(f"  hub     lease granted{f' (stole from a stale holder)' if args.steal else ''}")
    if worktree:
        print(f"next: cd {shlex.quote(worktree)}")
    else:
        print(f"next: git checkout {task['branch']}   # or: git worktree add ../{slug(agent)} {task['branch']}")
    return 0


def cmd_release(args):
    board = _fresh_board()
    state = board.load()
    agent = _resolve_agent(args, read_local_config(board.common))
    task = board.transaction(
        f"release {args.task}",
        lambda current: service.release(current, args.task, agent, args.force),
        agent=agent,
    )
    if board.hub:
        try:
            hubclient.release(
                board.hub_urls,
                state.project.get("name") or "default",
                args.task,
                agent,
                args.force,
                board.hub_token,
            )
        except hubclient.HubConflict as exc:
            print(f"aghub: hub note: {exc}", file=sys.stderr)
        except hubclient.HubError as exc:
            hubclient.warn_once(f"hub at {board.hub} is unreachable ({exc})")
    print(f"released {task['id']} {task['title']}")
    return 0


def cmd_handoff(args):
    board = _fresh_board()
    state = board.load()
    agent = _resolve_agent(args, read_local_config(board.common))
    task = state.tasks.get(args.task)
    if task is None:
        raise AghubError(f"unknown task {args.task}")
    changes = _diff_summary(board, state, task)
    key = (
        ensure_local_agent_key(board.common, agent)
        if service.eval_attestation_mode(state) == service.EVAL_ED25519_SCHEME
        else {}
    )
    record = board.transaction(
        f"handoff {args.task}",
        lambda current: service.handoff(
            current,
            args.task,
            agent,
            args.next_step,
            args.note,
            changes,
            args.done,
            args.force,
            private_key=key.get("private_key"),
            public_key=key.get("public_key"),
        ),
        agent=agent,
    )
    print(f"handoff {record['id']} saved for {args.task}")
    if args.done and board.hub:
        try:
            hubclient.release(
                board.hub_urls,
                state.project.get("name") or "default",
                args.task,
                agent,
                token=board.hub_token,
            )
        except hubclient.HubError as exc:
            hubclient.warn_once(f"hub at {board.hub} is unreachable ({exc})")
    if record["next"]:
        print(f"  next  {record['next']}")
    if record["changed"]:
        print(f"  changed  {record['changed']}")
    return 0


def cmd_submit(args):
    board = _fresh_board()
    state = board.load()
    agent = _resolve_agent(args, read_local_config(board.common))
    task = state.tasks.get(args.task)
    if task is None:
        raise AghubError(f"unknown task {args.task}")
    if not board.remote:
        raise AghubError("submit needs a shared remote — initialize with --remote origin")
    branch = args.branch or task.get("branch")
    if not branch:
        raise AghubError(f"{args.task} has no branch — claim it first")
    local = gitops.git(["rev-parse", branch], cwd=board.repo, check=False)
    if local.returncode != 0:
        detail = (local.stderr or local.stdout or "").strip()
        raise AghubError(f"cannot resolve {branch}: {detail}")
    head = local.stdout.strip()
    missing = service.approval_gate(state, task, head=head)
    if missing and not args.force:
        raise AghubError(missing)
    push = gitops.git(["push", "-u", board.remote, branch], cwd=board.repo, check=False)
    if push.returncode != 0:
        detail = (push.stderr or push.stdout or "").strip()
        raise AghubError(f"could not push {branch}: {detail}")
    local = gitops.git(["rev-parse", branch], cwd=board.repo, check=False)
    head = local.stdout.strip() or head
    missing = service.approval_gate(state, task, head=head)
    if missing and not args.force:
        raise AghubError(missing)
    key = (
        ensure_local_agent_key(board.common, agent)
        if service.eval_attestation_mode(state) == service.EVAL_ED25519_SCHEME
        else {}
    )
    entry, position = board.transaction(
        f"submit {args.task}",
        lambda current: service.submit(
            current,
            args.task,
            agent,
            branch,
            args.force,
            private_key=key.get("private_key"),
            public_key=key.get("public_key"),
            head=head,
        ),
        agent=agent,
    )
    print(f"queued {entry['id']} for {args.task} on {branch} (position {position})")
    if board.hub:
        from . import review

        review.publish_packet(board, board.load(), args.task)
    print("next: aghub queue run   # on the integration machine")
    return 0


def cmd_queue(args):
    board = _fresh_board()
    state = board.load()
    agent = _resolve_agent(args, read_local_config(board.common))
    if args.queue_command == "list":
        if not state.queue:
            print("queue is empty")
        for entry in state.queue:
            extra = f" — {entry['reason']}" if entry.get("reason") else ""
            commit = f" {entry['commit']}" if entry.get("commit") else ""
            fix = f" → {entry['fix_task']}" if entry.get("fix_task") else ""
            print(f"{entry['task']}  {entry['status']:<8} {entry['agent']:<10} {entry['branch']}{commit}{fix}{extra}")
        return 0
    if args.queue_command == "show":
        entry = next(
            (item for item in state.queue if item.get("id") == args.entry_id), None
        )
        if entry is None:
            raise AghubError(f"unknown queue entry {args.entry_id}")
        print(yaml.safe_dump(entry, sort_keys=False).rstrip())
        return 0
    if args.queue_command == "verify":
        return _verify_queue(args.entry_id, state)
    key = ensure_local_agent_key(board.common, agent)
    service.run_queue(
        board,
        agent,
        private_key=key.get("private_key"),
        public_key=key.get("public_key"),
    )
    return 0


def _verify_queue(entry_id, state):
    entries = [
        entry for entry in state.queue if not entry_id or entry.get("id") == entry_id
    ]
    if not entries:
        raise AghubError(f"unknown queue entry {entry_id}" if entry_id else "queue is empty")
    failed = False
    for entry in entries:
        submission = entry.get("submission") or {}
        for label, record in (("submission", submission), ("result", entry)):
            if not record or not record.get("attestation"):
                continue
            status = _signature_state(state, record)
            print(f"{entry['id']}  {label:<10} {status}")
            if status == "invalid":
                failed = True
    return 1 if failed else 0


def cmd_ci(args):
    from . import ci

    if args.ci_command == "workflow":
        board = _fresh_board()
        state = board.load()
        project = args.project or state.project.get("name")
        if args.github_review:
            text = ci.github_workflow(
                python=args.python,
                project=project,
                trunk=args.trunk or state.project.get("trunk") or "main",
            )
        else:
            text = ci.workflow(
                python=args.python,
                project=project,
                trunk=args.trunk or state.project.get("trunk") or "main",
                contract=args.contract,
                codegen_out=args.codegen_out,
            )
        path = args.out
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"wrote {path}")
        return 0
    board = _fresh_board()
    state = board.load()
    from . import review

    heads = {
        task["id"]: review.branch_head(board, task, update=False)
        for task in state.tasks.values()
        if task.get("status") != "done" and task.get("requires_approval")
    }
    payload = ci.status(state, board, heads=heads)
    print(json.dumps(payload, indent=2, sort_keys=False))
    return 0 if payload["ok"] else 1


def _github_sync_event(board, args, agent, event, token=None, api=None):
    """Record trusted reviews and publish the review comment/check for one event."""
    from . import github, review

    messages = []
    info = github.pull_request_info(event)
    state = board.load()

    task = None
    if args.task:
        task = state.tasks.get(args.task)
        if task is None:
            raise AghubError(f"unknown task {args.task}")
    else:
        task = github.find_task(
            state.tasks,
            info.get("branch"),
            (info.get("body") or "") + "\n" + (info.get("title") or ""),
        )
    if task is None:
        where = f"PR #{info.get('number')}" if info.get("number") else "this event"
        return {
            "task": None,
            "reason": (
                f"no aghub task for {where} "
                f"(branch {info.get('branch') or '—'})"
            ),
            "messages": messages,
        }

    recorded = None
    review_info = info.get("review") or {}
    if (
        args.record_reviews
        and info.get("action") == "submitted"
        and review_info.get("state") in ("approved", "changes_requested")
        and task.get("requires_approval")
    ):
        allowed = {
            item.strip().upper()
            for item in (args.allow_association or "").split(",")
            if item.strip()
        }
        if review_info.get("association") not in allowed:
            raise AghubError(
                f"GitHub reviewer {review_info.get('login') or '?'} is not trusted "
                f"({review_info.get('association') or 'no association'})"
            )
        head_repo = info.get("head_repo")
        repository = args.repo or os.environ.get("GITHUB_REPOSITORY")
        fork = bool(head_repo and repository and head_repo.lower() != repository.lower())
        if fork and not args.allow_fork_reviews:
            messages.append(
                "aghub: warning: refusing to record an approval from a fork PR "
                "(pass --allow-fork-reviews to change this)"
            )
        else:
            external_id = review_info.get("id")
            if not info.get("sha"):
                raise AghubError(
                    "pull request head sha is missing; cannot bind this review to a commit"
                )
            already = any(
                item.get("source") == "github"
                and str(item.get("external_id") or "") == str(external_id)
                for item in state.approvals
            )
            if already:
                messages.append(f"review {external_id} already recorded")
            else:
                decision = (
                    "approved"
                    if review_info["state"] == "approved"
                    else "changes_requested"
                )
                note = f"GitHub review by {review_info.get('login') or '?'}"
                if review_info.get("body"):
                    note += f": {review_info['body']}"
                signer = args.reviewer_agent or agent
                login = review_info.get("login") or ""
                reviewer = service.github_reviewer(state, login) or signer
                if args.dry_run:
                    messages.append(
                        f"would record {decision} for {task['id']} from "
                        f"{login or '?'} as {reviewer}"
                    )
                else:
                    recorded = _record_review(
                        board,
                        signer,
                        task["id"],
                        decision,
                        note,
                        info.get("sha") or None,
                        False,
                        source="github",
                        external_id=external_id,
                        reviewer=reviewer,
                        github_login=login or None,
                    )
                    state = board.load()
                    via = (
                        f" (reviewer {reviewer})"
                        if reviewer and reviewer != signer
                        else ""
                    )
                    messages.append(
                        f"recorded GitHub {review_info['state']} from "
                        f"{review_info.get('login') or '?'} as {signer}{via}"
                    )

    packet = review.review_packet(board, state, task["id"])
    github.mark_stale_approval(packet, info.get("sha"))
    markdown = github.comment_body(packet)
    details_url = args.details_url or info.get("url") or None
    check = github.check_payload(packet, details_url=details_url)
    result = {
        "task": task["id"],
        "recorded": recorded,
        "approval": packet["approval"],
        "comment": markdown,
        "check": check,
        "messages": messages,
    }

    if args.dry_run:
        return result

    if not info.get("number") or not info.get("sha"):
        raise AghubError(
            "GitHub sync needs a pull_request or pull_request_review payload "
            "with a PR number and head sha"
        )
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY")
    if args.comment:
        published = github.upsert_comment(
            repo, info["number"], markdown, token=token, api=api
        )
        result["comment_result"] = published
        messages.append(
            f"comment {published['id']} "
            f"{'updated' if published['updated'] else 'created'} "
            f"on PR #{info['number']}"
        )
    if args.check:
        published = github.upsert_check(
            repo, info["sha"], check, token=token, api=api
        )
        result["check_result"] = published
        messages.append(
            f"check {published['id']} "
            f"{'updated' if published['updated'] else 'created'} "
            f"on {info['sha'][:12]} ({check['conclusion']})"
        )
    return result


def cmd_github(args):
    from . import github

    board = _fresh_board()
    agent = _resolve_agent(args, read_local_config(board.common))
    auth = github.app_auth(
        getattr(args, "app_id", None),
        getattr(args, "private_key", None),
        getattr(args, "installation_id", None),
        api=getattr(args, "api_url", None),
    )
    event = github.load_event(args.event)

    token = args.token or (auth.token() if auth and not args.dry_run else None)
    result = _github_sync_event(
        board, args, agent, event, token=token, api=args.api_url
    )

    if result["task"] is None:
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=False))
        else:
            print(result["reason"])
        return 0

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=False))
        return 0

    for message in result["messages"]:
        if message.startswith("aghub: warning:"):
            print(message, file=sys.stderr)
        else:
            print(message)

    if args.dry_run:
        print(result["comment"])
        print(
            f"\ncheck: {result['check']['name']} · "
            f"{result['check']['conclusion']} · "
            f"{result['check']['output']['title']}"
        )
        return 0
    return 0


def _github_webhook_summary(delivery):
    inner = delivery.get("result") or {}
    if delivery.get("event") == "ping":
        return "pong"
    if inner.get("task") is None:
        return inner.get("reason") or "ignored"
    lines = []
    for message in inner.get("messages") or []:
        lines.append(message)
    approval = inner.get("approval") or {}
    lines.append(
        f"{inner['task']}: approval {approval.get('decision') or 'pending'}"
    )
    return "\n".join(lines)


def cmd_github_webhook(args):
    """Run a verified GitHub webhook receiver on this trusted clone."""
    from . import github

    secret = (
        args.secret
        or os.environ.get("AGHUB_GITHUB_WEBHOOK_SECRET")
        or os.environ.get("GITHUB_WEBHOOK_SECRET")
    )
    if not secret:
        raise AghubError(
            "a webhook secret is required (--secret or "
            "AGHUB_GITHUB_WEBHOOK_SECRET)"
        )
    board = _fresh_board()
    agent = _resolve_agent(args, read_local_config(board.common))
    auth = github.app_auth(
        getattr(args, "app_id", None),
        getattr(args, "private_key", None),
        getattr(args, "installation_id", None),
        api=getattr(args, "api_url", None),
    )
    lock = threading.Lock()

    def on_event(kind, event, delivery):
        if kind == "ping":
            return {"message": "pong", "delivery": delivery}
        if kind != "pull_request_review":
            return {
                "ignored": f"{kind or 'unknown'} is not a pull_request_review event"
            }
        sync_args = argparse.Namespace(
            task=args.task,
            repo=args.repo,
            record_reviews=True,
            allow_association=args.allow_association,
            allow_fork_reviews=args.allow_fork_reviews,
            reviewer_agent=args.reviewer_agent,
            comment=not args.no_comment,
            check=not args.no_check,
            details_url=args.details_url,
            dry_run=args.dry_run,
            json=True,
        )
        with lock:
            board.sync()
            token = None
            if not args.dry_run:
                token = args.token or (auth.token() if auth else None)
            return _github_sync_event(
                board, sync_args, agent, event, token=token, api=args.api_url
            )

    receiver = github.WebhookReceiver(secret, on_event, max_bytes=args.max_body)

    if args.event:
        if not args.signature:
            raise AghubError("--signature is required with --event")
        with open(args.event, "rb") as handle:
            body = handle.read()
        headers = {
            "X-GitHub-Event": args.event_name,
            "X-GitHub-Delivery": args.delivery or "",
            "X-Hub-Signature-256": args.signature,
        }
        delivery = receiver.receive(headers, body)
        if args.json:
            print(json.dumps(delivery, indent=2, sort_keys=False))
        else:
            print(_github_webhook_summary(delivery))
        return 0

    server = github.webhook_server(receiver, args.bind, args.port)
    host, port = server.server_address[:2]
    print(
        f"aghub: listening for GitHub webhooks on "
        f"http://{host}:{port}{github.WEBHOOK_PATH}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

def cmd_heartbeat(args):
    board = Board.discover()
    agent = _resolve_agent(args, read_local_config(board.common))

    def mutate(current):
        return current.touch(agent, min_interval=0)

    record = board.transaction("heartbeat", mutate, agent=agent)
    if board.hub:
        state = board.load()
        try:
            hubclient.heartbeat(
                board.hub_urls,
                state.project.get("name") or "default",
                agent,
                record.get("current_task"),
                record.get("branch"),
                int(state.project.get("ttl_seconds", 600)),
                board.hub_token,
                machine=os.uname().nodename,
            )
        except hubclient.HubError as exc:
            hubclient.warn_once(f"hub at {board.hub} is unreachable ({exc})")
    print(f"{agent} heartbeat at {record.get('last_seen')} (task {record.get('current_task') or '—'})")
    return 0


def cmd_sync(args):
    board = Board.discover()
    synced = board.sync()
    state = board.load()
    trunk = state.project.get("trunk", "main")
    if board.remote and gitops.has_remote(board.repo, board.remote):
        gitops.git(
            ["fetch", board.remote, f"+refs/heads/{trunk}:refs/remotes/{board.remote}/{trunk}"],
            cwd=board.repo,
            check=False,
        )
    print(f"board  {board.head}{'' if synced else '  (local only)'}")
    _, trunk_sha = render._trunk_ref(state, board)
    print(f"trunk  {trunk} {trunk_sha}")
    return 0


def cmd_debug(args):
    board = Board.discover()
    target = args.agent_id

    def mutate(current):
        record = current.agents.get(target)
        if record is None:
            raise AghubError(f"unknown agent {target}")
        record["last_seen"] = ago_iso(int(current.project.get("ttl_seconds", 600)) + 5)
        current.emit("debug.expire", agent=target)
        return record

    record = board.transaction(f"expire {target}", mutate)
    if board.hub:
        try:
            hubclient.expire(
                board.hub_urls,
                board.load().project.get("name") or "default",
                target,
                int(board.load().project.get("ttl_seconds", 600)),
                board.hub_token,
            )
        except hubclient.HubError as exc:
            hubclient.warn_once(f"hub at {board.hub} is unreachable ({exc})")
    print(f"expired {target}'s lease (last_seen {record['last_seen']})")
    return 0


def cmd_mcp(args):
    from . import mcp

    if args.mcp_command == "tools":
        print(json.dumps(mcp.TOOLS, indent=2))
        return 0
    mcp.serve(agent=getattr(args, "mcp_agent", None) or args.agent)
    return 0


def _hub_urls(config):
    """Return the primary + failover hub URLs from local config."""
    extra = config.get("hub_failover") or []
    if isinstance(extra, str):
        extra = [extra]
    urls = []
    for url in [config.get("hub"), *extra]:
        if url and url not in urls:
            urls.append(url)
    return urls


def cmd_hub(args):
    if args.hub_command == "serve":
        from .hub import load_tokens, serve

        tokens = load_tokens(args.tokens_file) if args.tokens_file else None
        serve(
            args.host,
            args.port,
            args.db,
            args.token or os.environ.get("AGHUB_HUB_TOKEN"),
            args.verbose,
            read_token=args.read_token,
            tokens=tokens,
            rate_limit=args.rate_limit,
            postgres=args.postgres,
        )
        return 0

    repo = gitops.repo_root()
    common = gitops.common_dir(repo)
    config = read_local_config(common)
    if not config:
        raise AghubError("aghub is not initialized here — run `aghub init --project <name>`")

    if args.hub_command == "connect":
        urls = []
        for raw in args.url:
            url = raw.rstrip("/")
            if url and url not in urls:
                urls.append(url)
        if not urls:
            raise AghubError("at least one hub URL is required")
        primary = urls[0]
        token = args.token if args.token is not None else config.get("hub_token")
        try:
            info = hubclient.health(primary, token)
        except hubclient.HubError as exc:
            raise AghubError(f"cannot reach a hub at {primary}: {exc}")
        config["hub"] = primary
        config["hub_failover"] = urls[1:]
        config["hub_token"] = token
        write_local_config(common, config)
        print(
            f"connected to {primary} "
            f"(v{info.get('version')}, {info.get('projects', 0)} project(s))"
        )
        for url in urls[1:]:
            try:
                hubclient.health(url, token)
            except hubclient.HubError as exc:
                print(f"  failover {url} unreachable ({exc})", file=sys.stderr)
            else:
                print(f"  failover {url} ok")
        try:
            board = Board.discover()
            agent = _resolve_agent(args, config)
            board.transaction("hub connect", lambda current: None, agent=agent)
        except Exception as exc:
            print(
                f"aghub: warning: connected, but the first snapshot failed: {exc}",
                file=sys.stderr,
            )
        print(f"  board  {primary}/")
        return 0

    if args.hub_command == "disconnect":
        config["hub"] = None
        config["hub_token"] = None
        config["hub_failover"] = []
        write_local_config(common, config)
        print("disconnected from hub — continuing with git-only state")
        return 0

    hubs = _hub_urls(config)
    if not hubs:
        raise AghubError("not connected to a hub — run `aghub hub connect <url>`")

    if args.hub_command == "audit":
        try:
            text = hubclient.audit_export(
                hubs,
                project=args.project,
                actor=args.actor,
                after=args.after,
                limit=args.limit,
                fmt=args.format,
                token=config.get("hub_token"),
            )
        except hubclient.HubError as exc:
            raise AghubError(f"audit export failed: {exc}")
        if args.out:
            path = os.path.abspath(args.out)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            print(f"wrote {path} ({len(text.splitlines())} records)")
        else:
            sys.stdout.write(text)
        return 0

    healthy = None
    for index, url in enumerate(hubs):
        label = "primary" if index == 0 else f"failover {index}"
        try:
            info = hubclient.health(url, config.get("hub_token"))
        except hubclient.HubError as exc:
            print(f"hub {url} unreachable ({label}): {exc}")
            continue
        print(
            f"hub {url} ok ({label}, v{info.get('version')}, "
            f"{info.get('projects', 0)} projects, "
            f"{info.get('live_leases', 0)} live leases)"
        )
        if healthy is None:
            healthy = (url, info)
    if healthy is None:
        raise AghubError("no hub in the failover set is reachable")
    try:
        data = hubclient.board(healthy[0], None, config.get("hub_token"))
    except hubclient.HubError as exc:
        raise AghubError(f"hub at {healthy[0]} is unreachable: {exc}")
    if data:
        print(
            f"project {data['project']} · updated {data.get('updated_at')} · "
            f"{len(data.get('agents', []))} agent(s)"
        )
    return 0


# -- parser --------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="aghub",
        description="A shared board so parallel coding agents can work without talking to each other.",
    )
    parser.add_argument("--agent", help="agent identity (default: $AGHUB_AGENT or local config)")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create the shared board in this repository")
    init.add_argument("--project", required=True)
    init.add_argument("--trunk", default="main")
    init.add_argument("--ttl", type=int, default=600, help="lease length in seconds (default 600)")
    init.add_argument("--remote", default=None)
    init.add_argument("--no-remote", action="store_true")
    init.add_argument("--check", default=None, help="command run inside every merge, e.g. 'python3 check.py'")
    init.add_argument(
        "--check-sandbox",
        choices=("auto", "off", "require"),
        default="auto",
        help="sandbox for merge checks (default: auto — bubblewrap when available)",
    )
    init.add_argument(
        "--check-timeout",
        type=float,
        default=300,
        help="merge check timeout in seconds (default 300)",
    )
    init.add_argument(
        "--require-eval-sandbox",
        action="store_true",
        help="only accept eval scorecards produced under bubblewrap",
    )
    init.add_argument(
        "--scope",
        default=None,
        help="monorepo subdirectory this board owns (e.g. packages/web)",
    )
    init.add_argument(
        "--eval-ttl",
        type=int,
        default=service.EVAL_TTL_DEFAULT,
        help=f"how long a passing eval stays valid in seconds (default {service.EVAL_TTL_DEFAULT})",
    )
    init.add_argument(
        "--attestation",
        choices=("ed25519", "hmac-sha256"),
        default="ed25519",
        help="how eval scorecards are signed (default: ed25519 per-agent keys)",
    )
    init.set_defaults(func=cmd_init)

    agent = sub.add_parser("agent", help="show or set this clone's agent id and key")
    agent.add_argument("id", nargs="?")
    agent.add_argument("--key", action="store_true", help="show/generate this agent's eval key")
    agent.add_argument("--rotate-key", action="store_true", help="rotate the eval key")
    agent.add_argument("--revoke-key", action="store_true", help="revoke this agent's published key")
    agent.add_argument("--key-id", default=None, help="specific key to revoke")
    agent.set_defaults(func=cmd_agent)

    registry = sub.add_parser(
        "registry", help="agent profiles and local eval scorecards"
    )
    registry_sub = registry.add_subparsers(dest="registry_command", required=True)
    registry_profile = registry_sub.add_parser("profile")
    registry_profile.add_argument("--model", default=None)
    registry_profile.add_argument("--skills", default=None, help="comma or space separated")
    registry_profile.add_argument("--note", default=None)
    registry_show = registry_sub.add_parser("show")
    registry_show.add_argument("agent_id", nargs="?")
    registry_sub.add_parser("list")
    registry.set_defaults(func=cmd_registry)

    acl = sub.add_parser("acl", help="project roles and key revocation")
    acl_sub = acl.add_subparsers(dest="acl_command", required=True)
    acl_init = acl_sub.add_parser("init", help="make this agent the first admin")
    acl_init.add_argument("--force", action="store_true")
    acl_list = acl_sub.add_parser("list")
    acl_set = acl_sub.add_parser("set")
    acl_set.add_argument("agent_id")
    acl_set.add_argument("role", choices=("reader", "writer", "admin"))
    acl_remove = acl_sub.add_parser("remove")
    acl_remove.add_argument("agent_id")
    acl_revoke = acl_sub.add_parser("revoke-key")
    acl_revoke.add_argument("agent_id")
    acl_revoke.add_argument("--key-id", default=None)
    acl.set_defaults(func=cmd_acl)

    policy = sub.add_parser(
        "policy", help="review policy: approvals, groups, expiry, GitHub identities"
    )
    policy_sub = policy.add_subparsers(dest="policy_command", required=True)
    policy_show = policy_sub.add_parser("show")
    policy_show.add_argument("--json", action="store_true")
    policy_set = policy_sub.add_parser("set")
    policy_set.add_argument("--approvals", type=int, help="distinct approvals required (default 1)")
    policy_set.add_argument(
        "--ttl", type=int, default=None, help="approval lifetime in seconds (0 clears it)"
    )
    policy_set.add_argument("--no-ttl", action="store_true", help="approvals never expire")
    policy_set.add_argument(
        "--reviewer",
        dest="reviewers",
        action="append",
        default=[],
        help="add an agent to the reviewer allowlist (repeatable)",
    )
    policy_set.add_argument(
        "--group",
        dest="groups",
        action="append",
        default=[],
        help="define NAME=AGENT[,AGENT...]; an empty list deletes the group",
    )
    policy_set.add_argument(
        "--require-group",
        dest="required_groups",
        action="append",
        default=[],
        help="require at least one approval from this group (repeatable)",
    )
    policy_set.add_argument(
        "--github",
        dest="github",
        action="append",
        default=[],
        help="map LOGIN=AGENT for GitHub pull-request reviews (repeatable)",
    )
    policy_set.add_argument("--clear-reviewers", action="store_true")
    policy_set.add_argument("--clear-groups", action="store_true")
    policy_set.add_argument("--clear-required-groups", action="store_true")
    policy_set.add_argument("--clear-github", action="store_true")
    policy_set.add_argument("--clear", action="store_true", help="reset the whole review policy")
    policy_set.add_argument("--json", action="store_true")
    policy.set_defaults(func=cmd_policy)

    evaluation = sub.add_parser(
        "eval", help="run evals locally and publish the scorecard"
    )
    eval_sub = evaluation.add_subparsers(dest="eval_command", required=True)
    eval_run = eval_sub.add_parser("run")
    eval_run.add_argument("suite")
    eval_run.add_argument("--command", required=True)
    eval_run.add_argument("--timeout", type=float, default=300)
    eval_run.add_argument("--task", default=None)
    eval_run.add_argument(
        "--sandbox",
        choices=("auto", "off", "require"),
        default="auto",
        help="auto: bubblewrap if available, else a scrubbed copy; require: fail without bubblewrap",
    )
    eval_list = eval_sub.add_parser("list")
    eval_list.add_argument("--agent", dest="agent_id", default=None)
    eval_list.add_argument("--suite", default=None)
    eval_show = eval_sub.add_parser("show")
    eval_show.add_argument("eval_id")
    eval_verify = eval_sub.add_parser("verify", help="check a scorecard's attestation")
    eval_verify.add_argument("eval_id")
    eval_rerun = eval_sub.add_parser("rerun", help="repeat an eval with the same command")
    eval_rerun.add_argument("eval_id")
    eval_rerun.add_argument(
        "--sandbox", choices=("auto", "off", "require"), default=None
    )
    eval_rerun.add_argument("--timeout", type=float, default=None)
    eval_diff = eval_sub.add_parser("diff", help="compare two scorecards")
    eval_diff.add_argument("left")
    eval_diff.add_argument("right")
    eval_diff.add_argument("--json", action="store_true")
    eval_ttl = eval_sub.add_parser("ttl", help="show or set how long a passing eval stays valid")
    eval_ttl.add_argument("seconds", nargs="?", type=int)
    evaluation.set_defaults(func=cmd_eval)

    status = sub.add_parser("status", help="show the board")
    status.add_argument("--json", action="store_true")
    status.add_argument("--for", dest="for_agent", default=None, help="compact view for one agent")
    status.set_defaults(func=cmd_status)

    board = sub.add_parser("board", help="alias for status, or `board verify`")
    board.add_argument("--json", action="store_true")
    board.add_argument("--for", dest="for_agent", default=None)
    board_sub = board.add_subparsers(dest="board_command")
    board_verify = board_sub.add_parser("verify", help="verify event chain and signatures")
    board_verify.add_argument("--json", action="store_true")
    board_verify.add_argument("--strict", action="store_true")
    board.set_defaults(func=cmd_board, for_agent=None)

    verify = sub.add_parser(
        "verify", help="verify the board event chain and all signed records"
    )
    verify.add_argument("--json", action="store_true")
    verify.add_argument("--strict", action="store_true")
    verify.set_defaults(func=cmd_verify)

    nxt = sub.add_parser("next", help="show the next claimable task")
    nxt.set_defaults(func=cmd_next)

    inbox = sub.add_parser("inbox", help="what needs a human now")
    inbox.add_argument("--json", action="store_true")
    inbox.add_argument(
        "--check", action="store_true", help="exit 1 when the inbox is not clear"
    )
    inbox.set_defaults(func=cmd_inbox)

    review = sub.add_parser("review", help="inspect a task and record a human review")
    review.add_argument("task", nargs="?", help="task id (omit with --watch)")
    review.add_argument("--json", action="store_true")
    review.add_argument("--diff", action="store_true", help="include the full patch")
    verdict = review.add_mutually_exclusive_group()
    verdict.add_argument("--approve", action="store_true", help="record approval")
    verdict.add_argument(
        "--request-changes", action="store_true", help="record changes requested"
    )
    review.add_argument("--note", default="")
    review.add_argument("--commit", default=None)
    review.add_argument(
        "--publish",
        action="store_true",
        help="mirror the review packet (with diff) to the connected hub",
    )
    review.add_argument(
        "--watch",
        action="store_true",
        help="apply web review requests from the hub on this machine",
    )
    review.add_argument(
        "--once", action="store_true", help="with --watch: process pending requests once"
    )
    review.add_argument(
        "--interval", type=float, default=5.0, help="seconds between review polls"
    )
    review.add_argument(
        "--limit", type=int, default=50, help="maximum requests per poll"
    )
    review.add_argument(
        "--force", action="store_true", help="allow the task owner to review it"
    )
    review.set_defaults(func=cmd_review)

    task = sub.add_parser("task", help="create and list tasks")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_add = task_sub.add_parser("add")
    task_add.add_argument("title")
    task_add.add_argument("--paths", action="append", default=[], help="glob this task may write")
    task_add.add_argument("--contract", action="append", default=[], help="contract dependency, e.g. api@1")
    task_add.add_argument("--depends", action="append", default=[], help="task dependency")
    task_add.add_argument(
        "--eval", dest="evals", action="append", default=[], help="required passing eval suite"
    )
    task_add.add_argument("--acceptance", default="")
    task_add.add_argument(
        "--requires-approval",
        action="store_true",
        help="refuse submit/merge until a human posts an approval",
    )
    task_add.add_argument(
        "--required-approvals",
        type=int,
        default=None,
        help="distinct approvals required for this task (tightens the board policy)",
    )
    task_add.add_argument(
        "--reviewer-group",
        dest="review_groups",
        action="append",
        default=[],
        help="require an approval from this group (repeatable)",
    )
    task_add.add_argument(
        "--reviewer",
        dest="reviewers",
        action="append",
        default=[],
        help="restrict this task to an eligible reviewer (repeatable)",
    )
    task_add.add_argument(
        "--review-ttl",
        type=int,
        default=None,
        help="approval lifetime for this task in seconds (tightens the board policy)",
    )
    task_add.add_argument(
        "--requires-sandbox",
        action="store_true",
        help="require bubblewrap for this task's merge check and eval gates",
    )
    task_sub.add_parser("list")
    task_show = task_sub.add_parser("show")
    task_show.add_argument("task")
    task.set_defaults(func=cmd_task)

    contract = sub.add_parser("contract", help="freeze interfaces before parallel work")
    contract_sub = contract.add_subparsers(dest="contract_command", required=True)
    for name in ("draft", "freeze"):
        item = contract_sub.add_parser(name)
        item.add_argument("name")
        item.add_argument("version", type=int)
        item.add_argument("--from", dest="source", default=None, help="contract file")
    contract_show = contract_sub.add_parser("show")
    contract_show.add_argument("name")
    contract_show.add_argument("version", type=int, nargs="?")
    contract_diff = contract_sub.add_parser("diff")
    contract_diff.add_argument("name")
    contract_diff.add_argument("old_version", type=int, nargs="?")
    contract_diff.add_argument("new_version", type=int, nargs="?")
    contract_diff.add_argument("--json", action="store_true")
    contract_sub.add_parser("list")
    contract.set_defaults(func=cmd_contract, source=None)

    gen = sub.add_parser(
        "codegen", help="generate TypeScript, Python, and a mock server from a frozen contract"
    )
    gen.add_argument("name")
    gen.add_argument("version", type=int, nargs="?", default=None)
    gen.add_argument("--out", default="generated", help="output directory (default: generated)")
    gen.add_argument(
        "--target", choices=("all", "ts", "py", "mock", "sql"), default="all"
    )
    gen.add_argument(
        "--check", action="store_true", help="fail if the output directory is out of date"
    )
    gen.set_defaults(func=cmd_codegen)

    claim = sub.add_parser("claim", help="take a task and its path lease")
    claim.add_argument("task")
    claim.add_argument("--steal", action="store_true", help="take over a stale claim")
    claim.add_argument("--force", action="store_true")
    claim.add_argument(
        "--worktree",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="create an isolated checkout (default: ../<repo>-<agent>-<task>)",
    )
    claim.set_defaults(func=cmd_claim)

    release = sub.add_parser("release", help="give a task back")
    release.add_argument("task")
    release.add_argument("--force", action="store_true")
    release.set_defaults(func=cmd_release)

    handoff = sub.add_parser("handoff", help="write the async delta for the next agent")
    handoff.add_argument("task")
    handoff.add_argument("--next", dest="next_step", default="")
    handoff.add_argument("--note", default="")
    handoff.add_argument("--done", action="store_true")
    handoff.add_argument("--force", action="store_true")
    handoff.set_defaults(func=cmd_handoff)

    approve = sub.add_parser(
        "approve", help="record a human approval for a task that requires one"
    )
    approve.add_argument("task")
    approve.add_argument("--note", default="")
    approve.add_argument("--commit", default=None)
    approve.add_argument("--force", action="store_true", help="allow the task owner to approve")
    approve.set_defaults(func=cmd_approve)

    submit = sub.add_parser("submit", help="push your branch and enqueue it for integration")
    submit.add_argument("task")
    submit.add_argument("--branch", default=None)
    submit.add_argument("--force", action="store_true")
    submit.set_defaults(func=cmd_submit)

    queue = sub.add_parser("queue", help="inspect or run the serial merge queue")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_sub.add_parser("run")
    queue_sub.add_parser("list")
    queue_show = queue_sub.add_parser("show")
    queue_show.add_argument("entry_id")
    queue_verify = queue_sub.add_parser("verify")
    queue_verify.add_argument("entry_id", nargs="?")
    queue.set_defaults(func=cmd_queue)

    ci = sub.add_parser("ci", help="GitHub Actions template and CI status bridge")
    ci_sub = ci.add_subparsers(dest="ci_command", required=True)
    ci_workflow = ci_sub.add_parser("workflow", help="write a GitHub Actions workflow")
    ci_workflow.add_argument(
        "--out", default=".github/workflows/aghub.yml", help="output path"
    )
    ci_workflow.add_argument("--python", default="3.10")
    ci_workflow.add_argument("--project", default=None)
    ci_workflow.add_argument("--trunk", default=None, help="branch CI runs on")
    ci_workflow.add_argument(
        "--contract", default=None, help="contract to check for codegen drift"
    )
    ci_workflow.add_argument(
        "--codegen-out", default="generated", help="generated output directory"
    )
    ci_workflow.add_argument(
        "--github-review",
        action="store_true",
        help="write the pull-request review bridge workflow instead",
    )
    ci_status = ci_sub.add_parser("status", help="machine-readable board/queue gate")
    ci_status.add_argument("--json", action="store_true", default=True)
    ci.set_defaults(func=cmd_ci)

    github = sub.add_parser(
        "github", help="sync a task review onto a pull request and check"
    )
    github_sub = github.add_subparsers(dest="github_command", required=True)
    github_sync = github_sub.add_parser(
        "sync", help="record trusted PR reviews and publish the review comment/check"
    )
    github_sync.add_argument("--task", default=None, help="explicit task id")
    github_sync.add_argument(
        "--event", default=None, help="GitHub event payload (default: GITHUB_EVENT_PATH)"
    )
    github_sync.add_argument("--repo", default=None, help="owner/name (default: GITHUB_REPOSITORY)")
    github_sync.add_argument("--token", default=None, help="default: GITHUB_TOKEN or GH_TOKEN")
    github_sync.add_argument(
        "--record-reviews",
        action="store_true",
        help="record approved / changes_requested PR reviews on the board",
    )
    github_sync.add_argument(
        "--reviewer-agent",
        default=None,
        help="board identity for recorded reviews (default: this agent)",
    )
    github_sync.add_argument(
        "--allow-association",
        default="OWNER,MEMBER,COLLABORATOR",
        help="trusted GitHub author associations (comma separated)",
    )
    github_sync.add_argument(
        "--allow-fork-reviews",
        action="store_true",
        help="record reviews from fork PRs (unsafe by default)",
    )
    github_sync.add_argument("--comment", action=argparse.BooleanOptionalAction, default=True)
    github_sync.add_argument("--check", action=argparse.BooleanOptionalAction, default=True)
    github_sync.add_argument("--details-url", default=None)
    github_sync.add_argument("--dry-run", action="store_true", help="print, do not call GitHub")
    github_sync.add_argument("--json", action="store_true")
    github_sync.add_argument(
        "--app-id", default=None, help="GitHub App id (default: AGHUB_GITHUB_APP_ID)"
    )
    github_sync.add_argument(
        "--private-key",
        default=None,
        help="GitHub App private key PEM or path (default: AGHUB_GITHUB_APP_PRIVATE_KEY)",
    )
    github_sync.add_argument(
        "--installation-id",
        default=None,
        help="GitHub App installation id (default: AGHUB_GITHUB_INSTALLATION_ID)",
    )
    github_sync.add_argument(
        "--api-url", default=None, help="GitHub API base (default: GITHUB_API_URL)"
    )
    github.set_defaults(func=cmd_github)

    github_webhook = github_sub.add_parser(
        "webhook",
        help="receive signed GitHub App webhooks and record reviews on a trusted clone",
    )
    github_webhook.add_argument("--task", default=None, help="explicit task id")
    github_webhook.add_argument(
        "--event", default=None, help="process one payload file instead of serving"
    )
    github_webhook.add_argument(
        "--event-name", default="pull_request_review", help="X-GitHub-Event value for --event"
    )
    github_webhook.add_argument(
        "--signature", default=None, help="X-Hub-Signature-256 value for --event"
    )
    github_webhook.add_argument(
        "--delivery", default=None, help="X-GitHub-Delivery value for --event"
    )
    github_webhook.add_argument(
        "--secret",
        default=None,
        help="webhook secret (default: AGHUB_GITHUB_WEBHOOK_SECRET)",
    )
    github_webhook.add_argument("--bind", default="127.0.0.1")
    github_webhook.add_argument("--port", type=int, default=8787)
    github_webhook.add_argument("--repo", default=None, help="owner/name")
    github_webhook.add_argument("--token", default=None, help="explicit GitHub token")
    github_webhook.add_argument(
        "--reviewer-agent",
        default=None,
        help="board identity for recorded reviews (default: this agent)",
    )
    github_webhook.add_argument(
        "--allow-association",
        default="OWNER,MEMBER,COLLABORATOR",
        help="trusted GitHub author associations (comma separated)",
    )
    github_webhook.add_argument(
        "--allow-fork-reviews",
        action="store_true",
        help="record reviews from fork PRs (unsafe by default)",
    )
    github_webhook.add_argument("--no-comment", action="store_true")
    github_webhook.add_argument("--no-check", action="store_true")
    github_webhook.add_argument("--details-url", default=None)
    github_webhook.add_argument("--dry-run", action="store_true")
    github_webhook.add_argument("--json", action="store_true")
    github_webhook.add_argument(
        "--max-body",
        type=int,
        default=2097152,
        help="maximum webhook payload bytes (default: 2 MiB)",
    )
    github_webhook.add_argument("--app-id", default=None)
    github_webhook.add_argument("--private-key", default=None)
    github_webhook.add_argument("--installation-id", default=None)
    github_webhook.add_argument("--api-url", default=None)
    github_webhook.set_defaults(func=cmd_github_webhook)

    heartbeat = sub.add_parser("heartbeat", help="refresh this agent's lease")
    heartbeat.set_defaults(func=cmd_heartbeat)

    sync = sub.add_parser("sync", help="fetch the latest board and trunk")
    sync.set_defaults(func=cmd_sync)

    debug = sub.add_parser("debug", help="fault injection for demos and tests")
    debug_sub = debug.add_subparsers(dest="debug_command", required=True)
    expire = debug_sub.add_parser("expire", help="pretend an agent died")
    expire.add_argument("agent_id")
    debug.set_defaults(func=cmd_debug)

    hub = sub.add_parser("hub", help="live presence, authoritative leases, web board")
    hub_sub = hub.add_subparsers(dest="hub_command", required=True)
    hub_serve = hub_sub.add_parser("serve")
    hub_serve.add_argument("--host", default="127.0.0.1")
    hub_serve.add_argument("--port", type=int, default=8787)
    hub_serve.add_argument("--db", default="~/.aghub/hub.db")
    hub_serve.add_argument("--token", default=None, help="legacy admin token")
    hub_serve.add_argument("--read-token", default=None, help="read-only token")
    hub_serve.add_argument(
        "--tokens-file",
        default=None,
        help="JSON token -> role/projects mapping for scoped access",
    )
    hub_serve.add_argument(
        "--rate-limit", type=int, default=600, help="requests per minute per IP (0 disables)"
    )
    hub_serve.add_argument(
        "--postgres",
        default=None,
        metavar="DSN",
        help="Postgres DSN (requires the optional psycopg package)",
    )
    hub_serve.add_argument("--verbose", action="store_true")
    hub_connect = hub_sub.add_parser("connect")
    hub_connect.add_argument(
        "url", nargs="+", help="primary hub URL followed by optional failover URLs"
    )
    hub_connect.add_argument("--token", default=None)
    hub_sub.add_parser("disconnect")
    hub_audit = hub_sub.add_parser("audit", help="export the hub operation log")
    hub_audit.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    hub_audit.add_argument("--actor", default=None)
    hub_audit.add_argument("--project", default=None)
    hub_audit.add_argument("--after", type=int, default=0)
    hub_audit.add_argument("--limit", type=int, default=1000)
    hub_audit.add_argument("--out", default=None, metavar="PATH")
    hub_sub.add_parser("status")
    hub.set_defaults(func=cmd_hub)

    mcp = sub.add_parser(
        "mcp",
        help="serve the board to MCP clients (Claude Desktop, Cursor, editor agents)",
    )
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_sub.add_parser("serve", help="serve newline-delimited MCP JSON-RPC on stdio")
    mcp_serve.add_argument("--agent", dest="mcp_agent", default=None)
    mcp_sub.add_parser("tools", help="print the MCP tool catalog as JSON")
    mcp.set_defaults(func=cmd_mcp)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "board":
        args.for_agent = None
    try:
        return args.func(args)
    except AghubError as exc:
        print(f"aghub: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
