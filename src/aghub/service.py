import hashlib
import hmac
import json
import os
import secrets

from . import codegen, gitops, keys, sandbox
from .errors import AghubError
from .paths import matches, overlaps
from .ulid import new_ulid
from .util import age_seconds, human_age, machine_name, now_iso, slug

EVAL_TTL_DEFAULT = 7 * 24 * 60 * 60
EVAL_ED25519_SCHEME = "ed25519"
EVAL_HMAC_SCHEME = "hmac-sha256"
EVAL_ATTESTATION_SCHEME = EVAL_HMAC_SCHEME
ROLE_LEVELS = {"reader": 0, "writer": 1, "admin": 2}


def role_map(state):
    roles = (state.project or {}).get("roles")
    return roles if isinstance(roles, dict) else None


def role_of(state, agent):
    roles = role_map(state)
    if roles is None:
        return "writer"
    return roles.get(agent)


def require_role(state, agent, minimum="writer", action=None):
    """Enforce project roles only when the board has opted into ACLs."""
    roles = role_map(state)
    if roles is None:
        return True
    role = roles.get(agent)
    needed = ROLE_LEVELS[minimum]
    if role not in ROLE_LEVELS or ROLE_LEVELS[role] < needed:
        suffix = f" to {action}" if action else ""
        raise AghubError(
            f"{agent} is {role or 'not a member'} on this board; "
            f"{minimum} role required{suffix}"
        )
    return True


def _guard_last_admin(roles, target):
    admins = [name for name, role in roles.items() if role == "admin"]
    if target in admins and len(admins) == 1:
        raise AghubError(
            "cannot remove the last admin — appoint another admin first"
        )


def set_role(state, actor, target, role):
    require_role(state, actor, "admin", "change roles")
    if role not in ROLE_LEVELS:
        raise AghubError(f"unknown role '{role}' (reader, writer, admin)")
    roles = state.project.setdefault("roles", {})
    if role != "admin":
        _guard_last_admin(roles, target)
    roles[target] = role
    state.emit("acl.set", agent=actor, target=target, role=role)
    return dict(roles)


def remove_role(state, actor, target):
    require_role(state, actor, "admin", "remove roles")
    roles = state.project.setdefault("roles", {})
    _guard_last_admin(roles, target)
    previous = roles.pop(target, None)
    if previous is None:
        raise AghubError(f"{target} is not on this board's ACL")
    state.emit("acl.remove", agent=actor, target=target, role=previous)
    return dict(roles)


def init_acl(state, actor):
    roles = role_map(state)
    if roles:
        require_role(state, actor, "admin", "initialize ACLs")
        return dict(roles)
    state.project["roles"] = {actor: "admin"}
    state.emit("acl.init", agent=actor)
    return dict(state.project["roles"])


# -- contracts -----------------------------------------------------------


def parse_contract_ref(ref):
    name, _, version = str(ref).partition("@")
    if not name or not version.isdigit():
        raise AghubError(f"invalid contract reference '{ref}' — expected name@version")
    return name, int(version)


def contract_meta(state, ref):
    try:
        name, version = parse_contract_ref(ref)
    except AghubError:
        return None
    return state.contracts.get(name, {}).get(version)


def contract_ok(state, ref):
    meta = contract_meta(state, ref)
    return bool(meta and meta.get("status") == "frozen")


def latest_contract(state, name):
    versions = state.contracts.get(name) or {}
    if not versions:
        return None
    version = max(versions)
    return versions[version]


def contract_artifact_path(board, state, name, version):
    meta = (state.contracts.get(name) or {}).get(version)
    if not meta or not meta.get("artifact"):
        return None
    path = os.path.join(
        board.dir, "contracts", name, str(version), str(meta["artifact"])
    )
    return path if os.path.exists(path) else None


def contract_model(board, state, name, version):
    path = contract_artifact_path(board, state, name, version)
    if path is None:
        return None
    try:
        with open(path, "rb") as handle:
            return codegen.extract(codegen.load_spec(handle.read()))
    except (OSError, AghubError):
        return None


def contract_diff(board, state, name, old_version, new_version):
    old = contract_model(board, state, name, old_version)
    new = contract_model(board, state, name, new_version)
    if old is None or new is None:
        return None
    return codegen.diff_specs(old["spec"], new["spec"])


def frozen_versions(state, name):
    versions = state.contracts.get(name) or {}
    return sorted(
        version for version, meta in versions.items() if meta.get("status") == "frozen"
    )


def eval_ttl(state):
    value = (state.project or {}).get("eval_ttl_seconds", EVAL_TTL_DEFAULT)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return EVAL_TTL_DEFAULT


def eval_key(state):
    return str((state.project or {}).get("eval_secret") or "")


def eval_attestation_mode(state):
    """ed25519, hmac-sha256 (legacy), or None for unsigned boards."""
    mode = (state.project or {}).get("eval_attestation")
    if mode in (EVAL_ED25519_SCHEME, EVAL_HMAC_SCHEME):
        return mode
    return EVAL_HMAC_SCHEME if eval_key(state) else None


def ensure_eval_key(state):
    """Create a board key for older boards that predate eval attestation."""
    secret = eval_key(state)
    if not secret:
        secret = secrets.token_hex(24)
        state.project["eval_secret"] = secret
    return secret


def eval_key_id(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _eval_payload(record):
    body = {key: value for key, value in record.items() if key != "attestation"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def signed_payload(record, field="attestation"):
    """Canonical bytes for any board record signature."""
    body = {key: value for key, value in record.items() if key != field}
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def revoked_key_ids(profile):
    revoked = set()
    for item in (profile or {}).get("revoked_key_ids") or []:
        if isinstance(item, dict):
            value = item.get("key_id")
        else:
            value = item
        if value:
            revoked.add(str(value))
    return revoked


def key_is_revoked(profile, key_id):
    return bool(key_id) and str(key_id) in revoked_key_ids(profile)


def attest_record(record, private_key, public_key, field="attestation", signer=None):
    """Attach a per-agent Ed25519 signature to any JSON-serializable record."""
    try:
        derived = keys.public_key(bytes.fromhex(private_key)).hex()
        fingerprint = keys.key_id(public_key)
    except (TypeError, ValueError):
        raise AghubError("invalid ed25519 agent key")
    if derived != public_key:
        raise AghubError("agent private key does not match its public key")
    record.pop(field, None)
    record[field] = {
        "scheme": EVAL_ED25519_SCHEME,
        "key_id": fingerprint,
        "public_key": public_key,
        "signature": keys.sign(private_key, signed_payload(record, field)),
    }
    if signer:
        record[field]["signer"] = signer
    return record


def verify_attestation(state, record, field="attestation", agent_field="agent"):
    """True/False/None for a signed record against the board's agent profiles."""
    attestation = (record or {}).get(field) or {}
    if not attestation:
        return None
    if attestation.get("scheme") != EVAL_ED25519_SCHEME:
        return False
    agent = str(
        attestation.get("signer") or (record or {}).get(agent_field) or ""
    )
    profile = (getattr(state, "agents", None) or {}).get(agent) or {}
    current = str(profile.get("public_key") or "")
    if key_is_revoked(profile, attestation.get("key_id")):
        return False
    embedded = str(attestation.get("public_key") or "")
    allowed = [current] + [
        str(item) for item in profile.get("previous_public_keys") or [] if item
    ]
    public_key = embedded or current
    if not current or public_key not in allowed:
        return False
    try:
        if attestation.get("key_id") != keys.key_id(public_key):
            return False
    except (TypeError, ValueError):
        return False
    return keys.verify(
        public_key,
        signed_payload(record, field),
        str(attestation.get("signature") or ""),
    )


def sign_eval(state, record):
    """Attach a legacy HMAC over the scorecard using the board's shared key."""
    secret = eval_key(state)
    record.pop("attestation", None)
    if not secret:
        return record
    signature = hmac.new(
        secret.encode("utf-8"),
        _eval_payload(record).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    record["attestation"] = {
        "scheme": EVAL_HMAC_SCHEME,
        "key_id": eval_key_id(secret),
        "signature": signature,
    }
    return record


def register_agent_key(state, agent, public_key, alg=EVAL_ED25519_SCHEME):
    """Publish an agent's public key on its board profile."""
    try:
        fingerprint = keys.key_id(public_key)
    except (TypeError, ValueError):
        raise AghubError(f"invalid {alg} public key for {agent}")
    record = state.agents.get(agent)
    if record is None:
        record = {
            "id": agent,
            "current_task": None,
            "branch": None,
            "registered_at": now_iso(),
        }
        state.agents[agent] = record
    if key_is_revoked(record, fingerprint):
        raise AghubError(f"key {fingerprint} was revoked for {agent}; generate a new key")
    previous = record.get("public_key")
    if previous and previous != public_key:
        old_keys = record.setdefault("previous_public_keys", [])
        if previous not in old_keys:
            old_keys.append(previous)
    changed = previous != public_key or record.get("key_id") != fingerprint
    if changed:
        record["key_alg"] = alg
        record["key_id"] = fingerprint
        record["public_key"] = public_key
        record["key_updated_at"] = now_iso()
    touch = getattr(state, "touch", None)
    if touch:
        touch(agent)
    if changed and getattr(state, "emit", None):
        state.emit("agent.key", agent=agent, alg=alg, key_id=fingerprint)
    return record


def revoke_agent_key(state, agent, key_id=None, by=None):
    """Revoke one published key. New signatures with it stop verifying."""
    if by and by != agent:
        require_role(state, by, "admin", "revoke another agent's key")
    record = state.agents.get(agent)
    if record is None:
        raise AghubError(f"unknown agent {agent}")
    target = key_id or record.get("key_id")
    if not target:
        raise AghubError(f"{agent} has no published key to revoke")
    revoked = record.setdefault("revoked_key_ids", [])
    if not any(
        (item.get("key_id") if isinstance(item, dict) else item) == target
        for item in revoked
    ):
        revoked.append({"key_id": target, "revoked_at": now_iso(), "by": by})
    if record.get("key_id") == target:
        record.pop("public_key", None)
        record.pop("key_id", None)
        record.pop("key_alg", None)
    state.touch(by or agent, min_interval=0)
    state.emit("agent.key.revoked", agent=agent, key_id=target, by=by)
    return record


def attest_eval(record, private_key, public_key):
    """Sign a scorecard with a per-agent Ed25519 key."""
    return attest_record(record, private_key, public_key)


def eval_verified(state, record):
    """True/False when a policy and attestation exist; None when they do not."""
    attestation = (record or {}).get("attestation") or {}
    if not attestation:
        return None
    scheme = attestation.get("scheme")
    if scheme == EVAL_ED25519_SCHEME:
        return verify_attestation(state, record)
    if scheme == EVAL_HMAC_SCHEME:
        secret = eval_key(state)
        if not secret:
            return False
        if attestation.get("key_id") != eval_key_id(secret):
            return False
        expected = hmac.new(
            secret.encode("utf-8"),
            _eval_payload(record).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(str(attestation.get("signature") or ""), expected)
    return False


def eval_fresh(state, record):
    ttl = eval_ttl(state)
    if ttl <= 0:
        return True
    age = age_seconds((record or {}).get("at"))
    return age is not None and age <= ttl


def eval_short_status(state, record):
    """ok / FAIL / STALE / UNSIGNED / BAD, for compact board display."""
    if record is None:
        return "—"
    if not record.get("passed"):
        return "FAIL"
    verified = eval_verified(state, record)
    if verified is False:
        return "BAD"
    if verified is None and eval_attestation_mode(state):
        return "UNSIGNED"
    if not eval_fresh(state, record):
        return "STALE"
    return "ok"


def eval_sandbox_required(state, task=None):
    return bool(
        (task or {}).get("require_sandbox")
        or (state.project or {}).get("require_eval_sandbox")
    )


def eval_sandbox_ok(record):
    policy = (record or {}).get("sandbox") or {}
    return (
        policy.get("backend") == "bubblewrap"
        and policy.get("network") == "none"
        and bool(policy.get("snapshot"))
    )


def eval_mode(state, task, requested):
    """Escape hatch: an auto eval on a sandbox-required gate asks for bwrap.

    Without this, a board that requires Bubblewrap would happily record a
    fallback scorecard that can never satisfy its own gate. An explicit
    ``off`` still wins so operators can deliberately opt out.
    """
    if requested == "auto" and eval_sandbox_required(state, task):
        return "require"
    return requested


def eval_issue(state, record, task=None):
    """Return why a scorecard cannot satisfy a gate, or None if it can."""
    if record is None:
        return "not run"
    status = eval_short_status(state, record)
    if status == "ok":
        if eval_sandbox_required(state, task) and not eval_sandbox_ok(record):
            policy = record.get("sandbox") or {}
            backend = policy.get("backend") or "none"
            return f"not sandboxed ({backend}; bubblewrap required)"
        return None
    if status == "FAIL":
        return "last run failed"
    if status == "BAD":
        return "signature invalid"
    if status == "UNSIGNED":
        return "not attested"
    if status == "STALE":
        age = age_seconds(record.get("at"))
        return f"stale: {human_age(age)} old, ttl {human_age(eval_ttl(state))}"
    return status


def eval_gate(state, task, agent):
    """Return failing/missing/stale suites gating this task for this agent."""
    gaps = []
    for suite in task.get("evals") or []:
        record = latest_eval(state, agent, suite)
        issue = eval_issue(state, record, task)
        if issue:
            gaps.append(f"{suite} ({issue})")
    return gaps


def blockers(state, task):
    reasons = []
    for ref in task.get("contracts") or []:
        meta = contract_meta(state, ref)
        if meta is None:
            reasons.append(f"contract {ref} is missing")
        elif meta.get("status") != "frozen":
            reasons.append(f"contract {ref} is {meta.get('status')}")
    for dependency in task.get("depends_on") or []:
        if state.tasks.get(dependency, {}).get("status") != "done":
            reasons.append(f"waiting on {dependency}")
    return reasons


def effective_status(state, task):
    if task.get("status") == "done":
        return "done"
    owner = task.get("owner")
    if task.get("status") == "claimed" and owner:
        ttl = int(state.project.get("ttl_seconds", 600))
        agent = state.agents.get(owner)
        age = age_seconds(agent.get("last_seen")) if agent else None
        if age is not None and age <= ttl:
            return "claimed"
        return "stale"
    if blockers(state, task):
        return "blocked"
    return "ready"


def owner_age(state, task):
    agent = state.agents.get(task.get("owner") or "")
    return age_seconds(agent.get("last_seen")) if agent else None


# -- tasks ---------------------------------------------------------------


def add_task(
    state,
    title,
    paths,
    contracts,
    depends_on,
    acceptance,
    agent,
    evals=None,
    require_sandbox=False,
    requires_approval=False,
    required_approvals=None,
    review_groups=None,
    reviewers=None,
    review_ttl=None,
    review_sla=None,
):
    if agent != "queue":
        require_role(state, agent, "writer", "add tasks")
    paths = list(paths or [])
    review_groups = _string_list(review_groups)
    reviewers = _string_list(reviewers)
    if required_approvals is not None:
        required_approvals = int(required_approvals)
        if required_approvals < 1:
            raise AghubError("required approvals must be at least 1")
    if review_ttl is not None:
        review_ttl = int(review_ttl)
        if review_ttl < 0:
            raise AghubError("review TTL must be zero or more seconds")
    if review_sla is not None:
        review_sla = int(review_sla)
        if review_sla < 0:
            raise AghubError("review SLA must be zero or more seconds")
    policy = normalize_review_policy(state.project)
    unknown = [name for name in review_groups if name not in policy["groups"]]
    if unknown:
        raise AghubError(
            "unknown review group(s): "
            + ", ".join(unknown)
            + " — define them with `aghub policy set --group NAME=agent1,agent2`"
        )
    if reviewers and policy["reviewers"]:
        outside = [reviewer for reviewer in reviewers if reviewer not in policy["reviewers"]]
        if outside:
            raise AghubError(
                "reviewer(s) outside the board allowlist: "
                + ", ".join(outside)
                + " — add them with `aghub policy set --reviewer AGENT`"
            )
    if (
        required_approvals is not None
        or review_groups
        or reviewers
        or review_ttl is not None
        or review_sla is not None
    ):
        requires_approval = True
    scope = str((state.project or {}).get("scope") or "").strip("/")
    if scope:
        if not paths:
            paths = [f"{scope}/**"]
        outside = [
            path
            for path in paths
            if not (path == scope or path.startswith(scope + "/"))
        ]
        if outside:
            raise AghubError(
                f"task paths must stay inside project scope '{scope}': "
                + ", ".join(outside)
            )
    highest = 0
    for task_id in state.tasks:
        if task_id.startswith("T-") and task_id[2:].isdigit():
            highest = max(highest, int(task_id[2:]))
    task_id = f"T-{highest + 1:03d}"
    task = {
        "id": task_id,
        "title": title,
        "status": "ready",
        "owner": None,
        "branch": None,
        "paths": paths,
        "contracts": list(contracts or []),
        "depends_on": list(depends_on or []),
        "evals": list(evals or []),
        "acceptance": acceptance or "",
        "require_sandbox": bool(require_sandbox),
        "requires_approval": bool(requires_approval),
        "commit": None,
        "created_at": now_iso(),
        "created_by": agent,
        "updated_at": now_iso(),
    }
    if required_approvals is not None:
        task["required_approvals"] = required_approvals
    if review_groups:
        task["review_groups"] = review_groups
    if reviewers:
        task["reviewers"] = reviewers
    if review_ttl is not None:
        task["review_ttl_seconds"] = review_ttl
    if review_sla is not None:
        task["review_sla_seconds"] = review_sla
    state.tasks[task_id] = task
    state.emit("task.add", agent=agent, task=task_id, title=title)
    return task


def _check_owner(task, agent, force, action):
    owner = task.get("owner")
    if owner and owner != agent and not force:
        raise AghubError(f"{task['id']} is owned by {owner}; cannot {action} (use --force to override)")


def validate_claim(state, task_id, agent, steal=False, force=False):
    """Validate a claim without mutating state.

    Used both by `claim` and by the CLI before asking the hub for a lease, so a
    request that cannot succeed never creates an authoritative lease.
    """
    require_role(state, agent, "writer", "claim tasks")
    task = state.tasks.get(task_id)
    if task is None:
        raise AghubError(f"unknown task {task_id}")
    if task.get("status") == "done":
        raise AghubError(f"{task_id} is already done")

    status = effective_status(state, task)
    owner = task.get("owner")

    if status == "blocked":
        raise AghubError(f"{task_id} is blocked: " + "; ".join(blockers(state, task)))

    gates = eval_gate(state, task, agent)
    if gates:
        raise AghubError(
            f"{task_id} requires a passing eval from {agent}: "
            + "; ".join(gates)
            + " — run `aghub eval run SUITE --command ...`"
        )

    if owner and owner != agent:
        age = owner_age(state, task)
        if status == "claimed" and not force:
            raise AghubError(
                f"{task_id} is claimed by {owner} (heartbeat {human_age(age)} ago) — use --force to override"
            )
        if status == "stale" and not (steal or force):
            raise AghubError(
                f"{task_id} has a stale claim from {owner} (heartbeat {human_age(age)} ago) — use --steal to take it over"
            )

    assignee = task.get("assigned_to")
    if assignee and assignee != agent:
        age = age_seconds(state.agents.get(assignee, {}).get("last_seen"))
        ttl = int(state.project.get("ttl_seconds", 600))
        live = age is not None and age <= ttl
        if live and not force:
            raise AghubError(
                f"{task_id} is a fix task assigned to {assignee} "
                f"(heartbeat {human_age(age)} ago) — use --force to take it over"
            )
        if not live and not (steal or force):
            raise AghubError(
                f"{task_id} was assigned to {assignee} "
                f"(heartbeat {human_age(age)} ago) — use --steal to take it over"
            )

    for other_id, other in state.tasks.items():
        if other_id == task_id or other.get("status") == "done":
            continue
        if other.get("owner") == agent:
            continue
        if effective_status(state, other) != "claimed":
            continue
        for mine in task.get("paths") or []:
            for theirs in other.get("paths") or []:
                if overlaps(mine, theirs):
                    raise AghubError(
                        f"path conflict: {mine} overlaps {theirs}, claimed by "
                        f"{other['owner']} on {other_id}"
                    )

    return task


def apply_claim(state, task, agent):
    task_id = task["id"]
    owner = task.get("owner")
    branch = f"agent/{slug(agent)}/{task_id}" if owner != agent or not task.get("branch") else task["branch"]
    if owner and owner != agent and state.agents.get(owner, {}).get("current_task") == task_id:
        state.agents[owner]["current_task"] = None
        state.agents[owner]["branch"] = None
    task.update(
        {
            "status": "claimed",
            "owner": agent,
            "branch": branch,
            "claimed_at": now_iso(),
            "updated_at": now_iso(),
            "assigned_to": None,
        }
    )
    agent_record = state.touch(agent, min_interval=0)
    agent_record["current_task"] = task_id
    agent_record["branch"] = branch
    state.emit(
        "task.claim",
        agent=agent,
        task=task_id,
        branch=branch,
        paths=task.get("paths") or [],
    )
    return task


def claim(state, task_id, agent, steal=False, force=False):
    task = validate_claim(state, task_id, agent, steal, force)
    return apply_claim(state, task, agent)


def release(state, task_id, agent, force=False):
    require_role(state, agent, "writer", "release tasks")
    task = state.tasks.get(task_id)
    if task is None:
        raise AghubError(f"unknown task {task_id}")
    if task.get("status") == "done":
        raise AghubError(f"{task_id} is already done")
    _check_owner(task, agent, force, "release it")
    previous = task.get("owner")
    task.update({"status": "ready", "owner": None, "branch": None, "claimed_at": None, "updated_at": now_iso()})
    if previous and state.agents.get(previous, {}).get("current_task") == task_id:
        state.agents[previous]["current_task"] = None
        state.agents[previous]["branch"] = None
    state.emit("task.release", agent=agent, task=task_id, previous_owner=previous)
    return task


def handoff(
    state,
    task_id,
    agent,
    next_step,
    note="",
    changes="",
    done=False,
    force=False,
    private_key=None,
    public_key=None,
):
    require_role(state, agent, "writer", "write handoffs")
    task = state.tasks.get(task_id)
    if task is None:
        raise AghubError(f"unknown task {task_id}")
    _check_owner(task, agent, force, "write a handoff for it")
    record = {
        "id": new_ulid(),
        "at": now_iso(),
        "task": task_id,
        "agent": agent,
        "next": next_step or "",
        "note": note or "",
        "changed": changes or "",
        "done": bool(done),
    }
    if private_key and public_key:
        register_agent_key(state, agent, public_key)
        attest_record(record, private_key, public_key, signer=agent)
    state.new_handoffs.append(record)
    if done:
        task.update({"status": "done", "done_at": now_iso(), "updated_at": now_iso()})
        if state.agents.get(agent, {}).get("current_task") == task_id:
            state.agents[agent]["current_task"] = None
            state.agents[agent]["branch"] = None
    else:
        task["updated_at"] = now_iso()
    state.emit("task.handoff", agent=agent, task=task_id, next=next_step or "", done=bool(done))
    return record


# -- approvals -----------------------------------------------------------


def approvals_for(state, task_id):
    """Every approved review record for a task, oldest first."""
    return [
        record
        for record in getattr(state, "approvals", [])
        if record.get("task") == task_id and record.get("decision", "approved") == "approved"
    ]


def review_records(state, task_id, include_dismissals=False):
    """Valid review records for a task, oldest first.

    A review is either an approval or a request for changes. A dismissal is a
    meta-record that retracts a specific earlier review (by its ``external_id``)
    and is excluded here unless ``include_dismissals`` is set, so the latest
    decision and the counts ignore it. Signatures are enforced when the board
    uses Ed25519 attestations, matching the approval gate's existing behavior.
    """
    records = [
        record
        for record in getattr(state, "approvals", [])
        if record.get("task") == task_id
        and (include_dismissals or record.get("decision") != "dismissed")
    ]
    if eval_attestation_mode(state) == EVAL_ED25519_SCHEME:
        records = [
            record for record in records if verify_attestation(state, record) is True
        ]
    return sorted(
        records,
        key=lambda record: (str(record.get("at") or ""), str(record.get("id") or "")),
    )


def dismissed_external_ids(state, task_id):
    """External ids of reviews retracted by a dismissal record for this task."""
    dismissed = set()
    for record in review_records(state, task_id, include_dismissals=True):
        if record.get("decision") == "dismissed" and record.get("dismisses"):
            dismissed.add(str(record.get("dismisses")))
    return dismissed


def latest_review(state, task_id):
    records = review_records(state, task_id)
    return records[-1] if records else None


def review_decision(state, task_id):
    record = latest_review(state, task_id)
    if record is None:
        return None, None
    return record.get("decision") or "approved", record


def reviewer_identity(record):
    """The person a review represents, which may differ from its signer.

    Local reviews sign and review as the same agent. GitHub and hub reviews are
    signed by the trusted worker that applies them, while `reviewer` records the
    mapped human or board identity the decision belongs to.
    """
    record = record or {}
    return str(record.get("reviewer") or record.get("agent") or "")


def same_commit(left, right):
    """Compare full or abbreviated commit ids without needing a repository."""
    left = str(left or "").strip()
    right = str(right or "").strip()
    if not left or not right:
        return False
    if left == right:
        return True
    return len(left) >= 7 and len(right) >= 7 and (
        left.startswith(right) or right.startswith(left)
    )


def _string_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    try:
        items = list(value)
    except TypeError:
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_protected(raw_protected):
    """Parse the protected-path rules, dropping anything without a path glob."""
    rules = []
    if not isinstance(raw_protected, list):
        return rules
    for item in raw_protected:
        if not isinstance(item, dict):
            continue
        globs = _string_list(item.get("paths"))
        if not globs:
            continue
        rule = {"paths": globs}
        groups = _string_list(item.get("groups"))
        if groups:
            rule["groups"] = groups
        reviewers = _string_list(item.get("reviewers"))
        if reviewers:
            rule["reviewers"] = reviewers
        approvals = _int_or_none(item.get("approvals"))
        if approvals is not None:
            rule["approvals"] = max(1, approvals)
        name = str(item.get("name") or "").strip()
        if name:
            rule["name"] = name
        rules.append(rule)
    return rules


def normalize_review_policy(project):
    """Board review policy with stable defaults for boards that predate it."""
    raw = (project or {}).get("review")
    raw = raw if isinstance(raw, dict) else {}
    approvals = _int_or_none(raw.get("approvals"))
    approvals = max(1, approvals if approvals is not None else 1)
    ttl = _int_or_none(raw.get("ttl_seconds"))
    ttl = max(0, ttl) if ttl is not None else None
    if ttl == 0:
        ttl = None
    sla = _int_or_none(raw.get("sla_seconds"))
    sla = max(0, sla) if sla is not None else None
    if sla == 0:
        sla = None
    groups = {}
    raw_groups = raw.get("groups")
    if isinstance(raw_groups, dict):
        for name, members in raw_groups.items():
            name = str(name).strip()
            members = _string_list(members)
            if name and members:
                groups[name] = members
    github = {}
    raw_github = raw.get("github")
    if isinstance(raw_github, dict):
        for login, agent in raw_github.items():
            login = str(login).strip().lower()
            agent = str(agent).strip()
            if login and agent:
                github[login] = agent
    return {
        "approvals": approvals,
        "ttl_seconds": ttl,
        "sla_seconds": sla,
        "groups": groups,
        "required_groups": _string_list(raw.get("required_groups")),
        "reviewers": _string_list(raw.get("reviewers")),
        "protected_paths": _normalize_protected(raw.get("protected_paths")),
        "github": github,
    }


def review_policy_for(state, task=None):
    """Effective policy for one task: board defaults plus tightening overrides."""
    policy = normalize_review_policy(state.project)
    task = task or {}
    task_required = _int_or_none(task.get("required_approvals"))
    required = policy["approvals"]
    if task_required is not None:
        required = max(required, task_required, 1)
    ttl = policy["ttl_seconds"]
    task_ttl = _int_or_none(task.get("review_ttl_seconds"))
    if task_ttl is not None and task_ttl > 0:
        ttl = task_ttl if ttl is None else min(ttl, task_ttl)
    sla = policy["sla_seconds"]
    task_sla = _int_or_none(task.get("review_sla_seconds"))
    if task_sla is not None and task_sla > 0:
        sla = task_sla if sla is None else min(sla, task_sla)
    board_reviewers = list(policy["reviewers"])
    task_reviewers = _string_list(task.get("reviewers"))
    restricted = bool(board_reviewers or task_reviewers)
    if task_reviewers:
        reviewers = [
            reviewer
            for reviewer in task_reviewers
            if not board_reviewers or reviewer in board_reviewers
        ]
    else:
        reviewers = board_reviewers
    required_groups = list(policy["required_groups"])
    for name in _string_list(task.get("review_groups")):
        if name not in required_groups:
            required_groups.append(name)
    return {
        "required": bool(task.get("requires_approval")),
        "approvals": required,
        "ttl_seconds": ttl,
        "sla_seconds": sla,
        "reviewers": reviewers,
        "allowlist": restricted,
        "groups": dict(policy["groups"]),
        "required_groups": required_groups,
        "protected_paths": list(policy["protected_paths"]),
        "github": dict(policy["github"]),
    }


def changed_paths(board, base, head):
    """Paths changed between two commits, or None when git cannot answer."""
    if not base or not head:
        return None
    result = gitops.git(
        ["diff", "--name-only", base, head], cwd=board.repo, check=False
    )
    if result.returncode != 0:
        return None
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def branch_changes(board, head, trunk=None):
    """Paths that `head` changes against trunk, or None when git cannot tell.

    Unlike `branch_changed_paths`, which diffs the fetched remote branch, this
    works on whatever `head` resolves to locally, so the inbox, packets, and
    CI see the same change set as the queue without a network round trip.
    """
    if not head:
        return None
    if trunk is None:
        # Read only the trunk ref: a full board load per gate would make
        # `queue run` and the inbox reload the whole project for every task.
        trunk = board.project_config().get("trunk") or "main"
    for ref in (
        f"refs/remotes/{board.remote}/{trunk}" if board.remote else None,
        trunk,
    ):
        if not ref:
            continue
        found = gitops.git(["merge-base", ref, head], cwd=board.repo, check=False)
        base = (found.stdout or "").strip()
        if found.returncode == 0 and base:
            return changed_paths(board, base, head)
    return None


def describe_review_policy(state):
    policy = normalize_review_policy(state.project)
    bits = [
        f"{policy['approvals']} approval" + ("" if policy["approvals"] == 1 else "s")
    ]
    if policy["ttl_seconds"]:
        bits.append(f"expires after {human_age(policy['ttl_seconds'])}")
    if policy["sla_seconds"]:
        bits.append(f"sla {human_age(policy['sla_seconds'])}")
    if policy["required_groups"]:
        bits.append("groups " + ", ".join(policy["required_groups"]))
    if policy["reviewers"]:
        bits.append(f"reviewers {', '.join(policy['reviewers'])}")
    if policy["protected_paths"]:
        bits.append(
            f"{len(policy['protected_paths'])} protected path rule"
            + ("" if len(policy["protected_paths"]) == 1 else "s")
        )
    if policy["github"]:
        bits.append(f"github maps {len(policy['github'])} login(s)")
    return " · ".join(bits)


def github_reviewer(state, login, fallback=None):
    """Resolve a GitHub login to the board identity it reviews as."""
    if login:
        mapped = normalize_review_policy(state.project)["github"].get(
            str(login).strip().lower()
        )
        if mapped:
            return mapped
    return fallback


def describe_task_review_policy(state, task):
    policy = review_policy_for(state, task)
    bits = [
        f"{policy['approvals']} approval" + ("" if policy["approvals"] == 1 else "s")
    ]
    if policy["ttl_seconds"]:
        bits.append(f"expires after {human_age(policy['ttl_seconds'])}")
    if policy["sla_seconds"]:
        bits.append(f"sla {human_age(policy['sla_seconds'])}")
    if policy["required_groups"]:
        bits.append("groups " + ", ".join(policy["required_groups"]))
    if policy["reviewers"]:
        bits.append(f"reviewers {', '.join(policy['reviewers'])}")
    if policy["protected_paths"]:
        bits.append(
            f"{len(policy['protected_paths'])} protected path rule"
            + ("" if len(policy["protected_paths"]) == 1 else "s")
        )
    return " · ".join(bits)


def _review_summary(record, eligible=True):
    record = record or {}
    return {
        "id": record.get("id"),
        "at": record.get("at"),
        "agent": record.get("agent"),
        "reviewer": reviewer_identity(record),
        "github_login": record.get("github_login"),
        "decision": record.get("decision") or "approved",
        "note": record.get("note") or "",
        "commit": record.get("commit"),
        "binding": record.get("binding"),
        "source": record.get("source"),
        "external_id": record.get("external_id"),
        "eligible": bool(eligible),
        "stale": False,
        "expired": False,
        "dismissed": False,
    }


def review_pending_since(state, task, head=None):
    """When the change under review started waiting, for reviewer-SLA accounting.

    The clock starts at the oldest queue submission that carried `head`, so a
    fresh commit restarts it. Without a matching submission the claim (or the
    task itself, when work exists but was never queued) stands in; a task with
    neither an owner nor a branch has nothing to review yet.
    """
    task = task or {}
    times = []
    for entry in getattr(state, "queue", []) or []:
        if entry.get("task") != task.get("id"):
            continue
        if entry.get("status") not in ("waiting", "merging", "rejected"):
            continue
        entry_head = entry.get("submitted_commit") or entry.get("commit")
        if head and entry_head and not same_commit(entry_head, head):
            continue
        stamp = entry.get("submitted_at") or entry.get("at")
        if stamp:
            times.append(stamp)
    if times:
        return min(times)
    if task.get("branch") and task.get("owner"):
        return (
            task.get("claimed_at")
            or task.get("updated_at")
            or task.get("created_at")
        )
    return None


def _apply_review_sla(summary, state, task, head, policy):
    sla = policy.get("sla_seconds") or summary.get("sla_seconds")
    if not sla:
        return
    pending_since = review_pending_since(state, task, head=head)
    summary["pending_since"] = pending_since
    if pending_since is None:
        return
    age = age_seconds(pending_since)
    summary["pending_age"] = age
    summary["pending_seconds"] = age
    summary["overdue"] = bool(age is not None and age > sla)
    summary["sla_breached"] = bool(
        summary["overdue"]
        and not summary["satisfied"]
        and summary["decision"] != "changes_requested"
    )


def _evaluate_protected(policy, changed_paths):
    """Decide which protected-path rules a diff triggers.

    Returns the per-rule view, the groups and minimum approval count the
    triggered rules add, whether the diff was known, and the changed paths that
    matched one or more rules. With no diff (``changed_paths is None``) rules are
    reported as indeterminate and add nothing: protection is advisory until the
    queue evaluates the real diff.
    """
    known = changed_paths is not None
    changed = [str(path).strip() for path in (changed_paths or []) if str(path).strip()]
    view = []
    triggered_groups = []
    matched_paths = set()
    min_approvals = 0
    for rule in policy["protected_paths"]:
        if known:
            matched = [
                path
                for path in changed
                if any(matches(glob, path) for glob in rule["paths"])
            ]
            triggered = bool(matched)
        else:
            matched = []
            triggered = None
        entry = {
            "name": rule.get("name"),
            "label": rule.get("name") or ", ".join(rule["paths"]),
            "paths": list(rule["paths"]),
            "groups": list(rule.get("groups") or []),
            "reviewers": list(rule.get("reviewers") or []),
            "approvals": rule.get("approvals"),
            "triggered": triggered,
            "matched": matched,
            "satisfied": None,
        }
        if triggered:
            matched_paths.update(matched)
            for name in entry["groups"]:
                if name not in triggered_groups:
                    triggered_groups.append(name)
            if entry["approvals"]:
                min_approvals = max(min_approvals, entry["approvals"])
        view.append(entry)
    return view, triggered_groups, min_approvals, known, sorted(matched_paths)


def approval_summary(state, task, head=None, changed_paths=None):
    """Evaluate every review against the task's effective review policy.

    Returns a serializable summary used by the gate, packets, CI, and the web
    board. An approval only counts when it is for the current head, inside the
    reviewer allowlist, and younger than the configured TTL. Reviews are counted
    once per reviewer identity, newest first.

    ``changed_paths`` is the list of files the task branch touches against
    trunk. When supplied, protected-path rules whose globs match a changed file
    are enforced (extra required groups, reviewers, or a higher approval count),
    and any match raises the count to at least one more than `review.approvals`.
    When omitted the rules are reported but not enforced, so protection is
    authoritative only where the real diff is known — the merge queue.
    """
    task = task or {}
    policy = review_policy_for(state, task)
    (
        protected_view,
        triggered_groups,
        protected_min_approvals,
        protected_known,
        protected_matched,
    ) = _evaluate_protected(policy, changed_paths)
    required_approvals = max(policy["approvals"], protected_min_approvals)
    if protected_matched:
        # A patch into a protected area is higher risk than an ordinary one,
        # whatever the rules themselves ask for.
        required_approvals = max(required_approvals, policy["approvals"] + 1)
    required_groups = list(policy["required_groups"])
    for name in triggered_groups:
        if name not in required_groups:
            required_groups.append(name)
    summary = {
        "required": bool(task.get("requires_approval")),
        "satisfied": True,
        "decision": "pending",
        "reason": None,
        "required_approvals": required_approvals,
        "approved_count": 0,
        "missing_approvals": required_approvals,
        "required_groups": required_groups,
        "missing_groups": [],
        "groups": {},
        "reviewers": list(policy["reviewers"]),
        "allowlist": policy["allowlist"],
        "ttl_seconds": policy["ttl_seconds"],
        "sla_seconds": policy["sla_seconds"],
        "pending_since": None,
        "pending_age": None,
        "pending_seconds": None,
        "overdue": False,
        "sla_breached": False,
        "protected": protected_view,
        "protected_known": protected_known,
        "protected_paths_matched": list(protected_matched),
        "changed": changed_paths,
        "approvals": [],
        "expired": [],
        "stale": [],
        "ineligible": [],
        "dismissed": [],
        "latest_id": None,
        "latest_stale": False,
        "latest_expired": False,
        "latest_ineligible": False,
    }
    if not summary["required"]:
        return summary

    records = review_records(state, task["id"])
    dismissed_ids = dismissed_external_ids(state, task["id"])
    latest = records[-1] if records else None
    summary["decision"] = (
        (latest.get("decision") or "approved") if latest else "pending"
    )
    summary["latest_id"] = (latest or {}).get("id") if latest else None

    valid = {}
    reasons = {}
    for record in records:
        if (record.get("decision") or "approved") != "approved":
            continue
        identity = reviewer_identity(record)
        eligible = not policy["allowlist"] or identity in policy["reviewers"]
        entry = _review_summary(record, eligible=eligible)
        external_id = record.get("external_id")
        if external_id and str(external_id) in dismissed_ids:
            entry["dismissed"] = True
            reasons[entry["id"]] = (
                f"{task['id']} approval by {identity or 'a reviewer'} was dismissed "
                f"on GitHub — re-approve the current head "
                f"with `aghub review {task['id']} --approve --note ...`"
            )
            summary["dismissed"].append(entry)
            continue
        if head and str(record.get("binding") or "") == "commit":
            if not record.get("commit"):
                entry["stale"] = True
                reasons[entry["id"]] = (
                    f"{task['id']} approval by {identity or 'a reviewer'} is not "
                    "bound to a commit — re-approve the current head "
                    f"with `aghub review {task['id']} --approve --note ...`"
                )
                summary["stale"].append(entry)
                continue
            if not same_commit(record.get("commit"), head):
                entry["stale"] = True
                reasons[entry["id"]] = (
                    f"{task['id']} approval is for "
                    f"{str(record.get('commit'))[:7]}, but the current head is "
                    f"{str(head)[:7]} — re-approve the current head "
                    f"with `aghub review {task['id']} --approve --note ...`"
                )
                summary["stale"].append(entry)
                continue
        if policy["ttl_seconds"]:
            age = age_seconds(record.get("at"))
            if age is None or age > policy["ttl_seconds"]:
                entry["expired"] = True
                reasons[entry["id"]] = (
                    f"{task['id']} approval by {identity or 'a reviewer'} expired "
                    f"after {human_age(policy['ttl_seconds'])} — re-approve the "
                    "current head "
                    f"with `aghub review {task['id']} --approve --note ...`"
                )
                summary["expired"].append(entry)
                continue
        if not eligible:
            entry["eligible"] = False
            reasons[entry["id"]] = (
                f"{task['id']} approval by {identity} is not from an eligible "
                f"reviewer (allowed: "
                f"{', '.join(policy['reviewers']) or 'none — check the board and task allowlists'})"
            )
            summary["ineligible"].append(entry)
            continue
        valid[identity] = entry

    approvals = sorted(
        valid.values(),
        key=lambda item: (str(item.get("at") or ""), str(item.get("id") or "")),
    )
    summary["approvals"] = approvals
    summary["approved_count"] = len(approvals)
    summary["missing_approvals"] = max(
        0, required_approvals - summary["approved_count"]
    )

    for name in required_groups:
        members = policy["groups"].get(name)
        source = "policy" if name in policy["required_groups"] else "protected"
        if members is None:
            summary["groups"][name] = {
                "defined": False,
                "members": [],
                "satisfied": False,
                "source": source,
            }
            summary["missing_groups"].append(name)
            continue
        satisfied = any(item["reviewer"] in members for item in approvals)
        summary["groups"][name] = {
            "defined": True,
            "members": list(members),
            "satisfied": satisfied,
            "source": source,
        }
        if not satisfied:
            summary["missing_groups"].append(name)

    summary["missing_approvals"] = max(
        0, required_approvals - summary["approved_count"]
    )

    if latest and (latest.get("decision") or "approved") == "changes_requested":
        reviewer = reviewer_identity(latest) or "a reviewer"
        summary["reason"] = (
            f"{task['id']} has changes requested by {reviewer} — address them, "
            f"then run `aghub review {task['id']} --approve --note ...`"
        )
    elif latest and (latest.get("decision") or "approved") == "approved":
        summary["latest_stale"] = any(
            item["id"] == latest.get("id") for item in summary["stale"]
        )
        summary["latest_expired"] = any(
            item["id"] == latest.get("id") for item in summary["expired"]
        )
        summary["latest_ineligible"] = any(
            item["id"] == latest.get("id") for item in summary["ineligible"]
        )

    satisfied_reviewers = {item["reviewer"] for item in approvals}
    for entry in protected_view:
        if entry["triggered"] is not True:
            continue
        ok = True
        if entry["reviewers"]:
            ok = ok and any(name in satisfied_reviewers for name in entry["reviewers"])
        for name in entry["groups"]:
            group = summary["groups"].get(name)
            ok = ok and bool(group and group["satisfied"])
        if entry["approvals"]:
            ok = ok and summary["approved_count"] >= entry["approvals"]
        entry["satisfied"] = ok

    if summary["reason"] is None and summary["missing_groups"]:
        missing = []
        described = []
        for name in summary["missing_groups"]:
            group = summary["groups"][name]
            paths = group.get("paths") or []
            suffix = f" for {', '.join(paths)}" if paths else ""
            described.append(name + suffix)
            if group["defined"]:
                missing.append(
                    f"'{name}' (members: {', '.join(group['members'])}){suffix}"
                )
            else:
                missing.append(
                    f"'{name}' (undefined — add it with "
                    f"`aghub policy set --group {name}=agent1,agent2`){suffix}"
                )
        if any(
            summary["groups"][name].get("source") == "protected"
            for name in summary["missing_groups"]
        ):
            summary["reason"] = (
                f"{task['id']} touches protected paths and needs an approval "
                "from review group " + ", ".join(missing)
            )
        else:
            summary["reason"] = (
                f"{task['id']} needs an approval from review group "
                + ", ".join(missing)
            )
    if summary["reason"] is None:
        reviewer_unmet = [
            entry
            for entry in protected_view
            if entry["triggered"]
            and entry["reviewers"]
            and not any(name in satisfied_reviewers for name in entry["reviewers"])
        ]
        if reviewer_unmet:
            bits = [
                f"{' or '.join(entry['reviewers'])} "
                f"(protected paths: {', '.join(entry['matched'][:3])})"
                for entry in reviewer_unmet
            ]
            summary["reason"] = (
                f"{task['id']} touches protected paths and still needs an approval "
                "from " + "; ".join(bits)
            )
    if summary["reason"] is None and summary["approved_count"] < required_approvals:
        if not records and required_approvals == 1:
            extra = ""
            if summary["protected_paths_matched"]:
                paths = ", ".join(summary["protected_paths_matched"])
                groups = [
                    name
                    for name in summary["missing_groups"]
                    if summary["groups"].get(name, {}).get("source") == "protected"
                ]
                extra = f" for the protected paths it touches ({paths})"
                if groups:
                    extra += f", including review group {', '.join(groups)}"
            summary["reason"] = (
                f"{task['id']} requires a human approval{extra} — run "
                f"`aghub review {task['id']} --approve --note ...`"
            )
        elif summary["approved_count"] == 0 and reasons.get((latest or {}).get("id")):
            summary["reason"] = reasons[latest.get("id")]
        else:
            extra = ""
            if summary["ineligible"]:
                extra = (
                    f"; {len(summary['ineligible'])} review(s) not from an "
                    "eligible reviewer"
                )
            elif summary["expired"]:
                extra = f"; {len(summary['expired'])} review(s) expired"
            elif summary["stale"]:
                extra = f"; {len(summary['stale'])} review(s) target another head"
            protected = ""
            if summary["protected_paths_matched"]:
                paths = ", ".join(summary["protected_paths_matched"])
                protected = (
                    f"; the patch touches protected paths ({paths}) and needs "
                    f"{summary['required_approvals']} approvals"
                )
            summary["reason"] = (
                f"{task['id']} requires {required_approvals} approvals — "
                f"{summary['approved_count']} valid for the current head"
                f"{extra}{protected}; still need "
                f"{summary['missing_approvals']} more"
            )
    summary["satisfied"] = summary["reason"] is None

    _apply_review_sla(summary, state, task, head, policy)
    if summary["overdue"] and summary["reason"]:
        # Whatever is holding the review up, an overdue one says so.
        summary["reason"] += (
            f" — overdue: waiting {human_age(summary['pending_seconds'])}"
            f" (SLA {human_age(summary['sla_seconds'])})"
        )
    return summary


def approval_gate(state, task, head=None, changed_paths=None):
    """Return a human-readable gap when a task still needs an approval."""
    return approval_summary(
        state, task, head=head, changed_paths=changed_paths
    )["reason"]


def set_review_policy(state, updates, agent):
    """Merge a review-policy update into the board project and return it."""
    require_role(state, agent, "admin", "change review policy")
    updates = updates or {}
    if updates.get("clear"):
        raw = {}
    else:
        raw = (state.project or {}).get("review")
        raw = dict(raw) if isinstance(raw, dict) else {}

    if updates.get("approvals") is not None:
        raw["approvals"] = max(1, int(updates["approvals"]))
    if updates.get("clear_ttl"):
        raw.pop("ttl_seconds", None)
    elif updates.get("ttl_seconds") is not None:
        ttl = max(0, int(updates["ttl_seconds"]))
        if ttl:
            raw["ttl_seconds"] = ttl
        else:
            raw.pop("ttl_seconds", None)
    if updates.get("clear_sla"):
        raw.pop("sla_seconds", None)
    elif updates.get("sla_seconds") is not None:
        sla = max(0, int(updates["sla_seconds"]))
        if sla:
            raw["sla_seconds"] = sla
        else:
            raw.pop("sla_seconds", None)

    if updates.get("clear_reviewers"):
        raw.pop("reviewers", None)
    if updates.get("reviewers"):
        reviewers = _string_list(raw.get("reviewers"))
        for reviewer in updates["reviewers"]:
            reviewer = str(reviewer).strip()
            if reviewer and reviewer not in reviewers:
                reviewers.append(reviewer)
        if reviewers:
            raw["reviewers"] = reviewers

    groups = raw.get("groups")
    groups = dict(groups) if isinstance(groups, dict) else {}
    required = _string_list(raw.get("required_groups"))
    if updates.get("clear_groups"):
        groups = {}
        required = []
    for name, members in (updates.get("groups") or {}).items():
        name = str(name).strip()
        if not name:
            continue
        members = [str(member).strip() for member in members if str(member).strip()]
        if members:
            groups[name] = members
        else:
            groups.pop(name, None)
            required = [item for item in required if item != name]
    if updates.get("clear_required_groups"):
        required = []
    for name in updates.get("required_groups") or []:
        name = str(name).strip()
        if name and name not in required:
            required.append(name)

    if updates.get("clear_github"):
        raw.pop("github", None)
    if updates.get("github"):
        github = raw.get("github")
        github = dict(github) if isinstance(github, dict) else {}
        for login, target in updates["github"].items():
            login = str(login).strip().lower()
            target = str(target).strip()
            if login and target:
                github[login] = target
        if github:
            raw["github"] = github

    if updates.get("clear_protected"):
        raw.pop("protected", None)

    missing = [name for name in required if name not in groups]
    if missing:
        raise AghubError(
            "required review group(s) are not defined: "
            + ", ".join(missing)
            + " — add them with `aghub policy set --group NAME=agent1,agent2`"
        )
    reviewers = _string_list(raw.get("reviewers"))
    if reviewers:
        outside = sorted(
            {
                member
                for name, members in groups.items()
                for member in members
                if member not in reviewers
            }
        )
        if outside:
            raise AghubError(
                "review group members are not in the reviewer allowlist: "
                + ", ".join(outside)
                + " — add them with `aghub policy set --reviewer AGENT`"
            )
    protected = _normalize_protected(raw.get("protected_paths"))
    if updates.get("clear_protected"):
        protected = []
    elif updates.get("protected_paths") is not None:
        protected = _normalize_protected(updates["protected_paths"])
    for pattern in updates.get("unprotected") or []:
        pattern = str(pattern).strip().strip("/")
        if not pattern:
            continue
        protected = [
            rule for rule in protected if pattern not in (rule.get("paths") or [])
        ]
    undefined = sorted(
        {
            name
            for rule in protected
            for name in rule.get("groups") or []
            if name not in groups
        }
    )
    if undefined:
        raise AghubError(
            "protected-path rule references undefined group(s): "
            + ", ".join(undefined)
            + " — define them with `aghub policy set --group NAME=agent1,agent2`"
        )
    if reviewers:
        outside = sorted(
            {
                name
                for rule in protected
                for name in rule.get("reviewers") or []
                if name not in reviewers
            }
        )
        if outside:
            raise AghubError(
                "protected-path reviewer(s) are not in the reviewer allowlist: "
                + ", ".join(outside)
                + " — add them with `aghub policy set --reviewer AGENT`"
            )
    if protected:
        raw["protected_paths"] = protected
    else:
        raw.pop("protected_paths", None)

    if groups:
        raw["groups"] = groups
    else:
        raw.pop("groups", None)
    if required:
        raw["required_groups"] = required
    else:
        raw.pop("required_groups", None)
    raw["updated_at"] = now_iso()
    raw["updated_by"] = agent
    if raw:
        state.project["review"] = raw
    else:
        state.project.pop("review", None)
    state.emit(
        "project.review_policy",
        agent=agent,
        approvals=raw.get("approvals", 1),
        ttl_seconds=raw.get("ttl_seconds"),
        sla_seconds=raw.get("sla_seconds"),
        groups=sorted(groups),
        required_groups=required,
        reviewers=reviewers,
        protected_paths=len(_normalize_protected(raw.get("protected_paths"))),
        github=dict(raw.get("github") or {}),
    )
    return normalize_review_policy(state.project)


def _review_gate(state, task_id, agent, force, reviewer=None):
    task = state.tasks.get(task_id)
    if task is None:
        raise AghubError(f"unknown task {task_id}")
    if role_map(state) is not None:
        require_role(state, agent, "admin", "review tasks")
    identity = str(reviewer or agent)
    if task.get("owner") in (agent, identity) and not force:
        raise AghubError(
            f"{identity} owns {task_id}; review with a separate human identity "
            "(or pass --force)"
        )
    return task


def _review_record(
    state,
    task_id,
    agent,
    decision,
    note="",
    commit=None,
    private_key=None,
    public_key=None,
    force=False,
    source=None,
    external_id=None,
    reviewer=None,
    github_login=None,
):
    task = _review_gate(state, task_id, agent, force, reviewer=reviewer)
    identity = str(reviewer or agent)
    if (decision or "approved") == "approved":
        policy = review_policy_for(state, task)
        if policy["allowlist"] and identity not in policy["reviewers"]:
            allowed = ", ".join(policy["reviewers"]) or "none"
            raise AghubError(
                f"{identity} is not an eligible reviewer for {task_id} "
                f"(allowed: {allowed})"
            )
    record = {
        "id": new_ulid(),
        "at": now_iso(),
        "task": task_id,
        "agent": agent,
        "decision": decision,
        "note": note or "",
        "commit": commit,
    }
    if reviewer and str(reviewer) != str(agent):
        record["reviewer"] = str(reviewer)
    if github_login:
        record["github_login"] = str(github_login)
    if commit:
        record["binding"] = "commit"
    if source:
        record["source"] = source
    if external_id is not None:
        record["external_id"] = str(external_id)
    if private_key and public_key:
        register_agent_key(state, agent, public_key)
        attest_record(record, private_key, public_key, signer=agent)
    state.new_approvals.append(record)
    state.approvals.append(record)
    kind = "task.approved" if decision == "approved" else "task.changes_requested"
    state.emit(
        kind,
        agent=agent,
        reviewer=identity,
        task=task_id,
        review=record["id"],
        note=record["note"],
    )
    return record


def approve(
    state,
    task_id,
    agent,
    note="",
    commit=None,
    private_key=None,
    public_key=None,
    force=False,
    source=None,
    external_id=None,
    reviewer=None,
    github_login=None,
):
    return _review_record(
        state,
        task_id,
        agent,
        "approved",
        note,
        commit,
        private_key,
        public_key,
        force,
        source,
        external_id,
        reviewer=reviewer,
        github_login=github_login,
    )


def request_changes(
    state,
    task_id,
    agent,
    note="",
    commit=None,
    private_key=None,
    public_key=None,
    force=False,
    source=None,
    external_id=None,
    reviewer=None,
    github_login=None,
):
    return _review_record(
        state,
        task_id,
        agent,
        "changes_requested",
        note,
        commit,
        private_key,
        public_key,
        force,
        source,
        external_id,
        reviewer=reviewer,
        github_login=github_login,
    )


def dismiss_review(
    state,
    task_id,
    agent,
    external_id,
    note="",
    private_key=None,
    public_key=None,
    source="github",
):
    """Retract an earlier review by its external id (e.g. a dismissed GitHub review).

    This records an append-only `dismissed` meta-record rather than mutating the
    original approval. `approval_summary` then stops counting any approval whose
    `external_id` was dismissed, so a dismissed GitHub approval no longer
    satisfies the gate. Dismissing the same review twice is idempotent.
    """
    task = state.tasks.get(task_id)
    if task is None:
        raise AghubError(f"unknown task {task_id}")
    external_id = str(external_id)
    if external_id in dismissed_external_ids(state, task_id):
        return None
    record = {
        "id": new_ulid(),
        "at": now_iso(),
        "task": task_id,
        "agent": agent,
        "decision": "dismissed",
        "dismisses": external_id,
        "note": note or "",
    }
    if source:
        record["source"] = source
    if private_key and public_key:
        register_agent_key(state, agent, public_key)
        attest_record(record, private_key, public_key, signer=agent)
    state.new_approvals.append(record)
    state.approvals.append(record)
    state.emit(
        "task.review_dismissed",
        agent=agent,
        task=task_id,
        review=record["id"],
        dismisses=external_id,
    )
    return record


# -- queue ---------------------------------------------------------------


def submit(
    state,
    task_id,
    agent,
    branch=None,
    force=False,
    private_key=None,
    public_key=None,
    head=None,
):
    require_role(state, agent, "writer", "submit tasks")
    task = state.tasks.get(task_id)
    if task is None:
        raise AghubError(f"unknown task {task_id}")
    if task.get("status") == "done":
        raise AghubError(f"{task_id} is already done")
    _check_owner(task, agent, force, "submit it")
    branch = branch or task.get("branch")
    if not branch:
        raise AghubError(f"{task_id} has no branch — claim it first")
    for entry in state.queue:
        if entry.get("task") == task_id and entry.get("status") in ("waiting", "merging"):
            raise AghubError(f"{task_id} is already in the queue ({entry['status']})")
    missing = approval_gate(state, task, head=head)
    if missing and not force:
        raise AghubError(missing)
    entry_id = f"Q-{new_ulid()}"
    submission = {
        "id": entry_id,
        "at": now_iso(),
        "task": task_id,
        "agent": agent,
        "branch": branch,
    }
    if head:
        submission["commit"] = head
    if private_key and public_key:
        register_agent_key(state, agent, public_key)
        attest_record(submission, private_key, public_key, signer=agent)
    entry = {
        "id": entry_id,
        "task": task_id,
        "agent": agent,
        "branch": branch,
        "status": "waiting",
        "reason": None,
        "commit": None,
        "submitted_commit": head,
        "submitted_at": submission["at"],
        "started_at": None,
        "finished_at": None,
        "submission": submission,
    }
    state.queue.append(entry)
    state.emit("queue.submit", agent=agent, task=task_id, branch=branch)
    return entry, len(state.queue)


def queue_pick(state):
    for entry in state.queue:
        if entry.get("status") == "merging" and entry.get("started_at"):
            age = age_seconds(entry["started_at"]) or 0
            if age > 300:
                entry["status"] = "waiting"
                entry["reason"] = "recovered from an interrupted merge"
                entry["started_at"] = None
    if any(entry.get("status") == "merging" for entry in state.queue):
        return None
    for entry in state.queue:
        if entry.get("status") == "waiting":
            entry["status"] = "merging"
            entry["started_at"] = now_iso()
            state.emit("queue.pick", agent="queue", task=entry["task"])
            return dict(entry)
    return None


def queue_finalize(
    state,
    entry_id,
    status,
    reason=None,
    commit=None,
    trace=None,
    runner=None,
    private_key=None,
    public_key=None,
):
    entry = next((item for item in state.queue if item.get("id") == entry_id), None)
    if entry is None:
        raise AghubError("queue entry disappeared")
    entry["status"] = status
    entry["reason"] = reason
    entry["commit"] = commit
    entry["finished_at"] = now_iso()
    task = state.tasks.get(entry["task"])
    if task is None:
        return entry
    if status == "merged":
        task.update(
            {
                "status": "done",
                "commit": commit,
                "done_at": now_iso(),
                "updated_at": now_iso(),
            }
        )
        owner = task.get("owner")
        if owner and state.agents.get(owner, {}).get("current_task") == task["id"]:
            state.agents[owner]["current_task"] = None
            state.agents[owner]["branch"] = None
        _close_trace_tasks(state, task, commit)
        _resolve_source(state, task, commit)
    elif trace:
        trace = dict(trace)
        trace.setdefault("at", now_iso())
        trace["agent"] = runner or "queue"
        trace["reason"] = reason
        if private_key and public_key:
            attest_record(trace, private_key, public_key, signer=trace["agent"])
        task["trace"] = trace
        task["last_failure_at"] = trace["at"]
        task["updated_at"] = now_iso()
        fix = _trace_task(state, entry, task, reason, trace)
        if fix is not None:
            entry["fix_task"] = fix["id"]
    if private_key and public_key:
        signer = runner or "queue"
        register_agent_key(state, signer, public_key)
        attest_record(entry, private_key, public_key, signer=signer)
    state.emit("queue." + status, agent="queue", task=task["id"], reason=reason, commit=commit)
    return entry


def _trace_task(state, entry, task, reason, trace):
    """Open (or refresh) a fix task carrying the failing integration case."""
    if trace.get("kind") not in (
        "check",
        "paths",
        "merge",
        "timeout",
        "push",
        "sandbox",
        "approval",
        "scope",
    ):
        return None
    title = f"Fix {task['id']}: {_short_reason(reason)}"
    source = {
        "task": task["id"],
        "queue": entry["id"],
        "branch": entry["branch"],
        "agent": entry["agent"],
    }
    for item in state.tasks.values():
        if (item.get("source") or {}).get("task") == task["id"] and item.get("status") != "done":
            item.update(
                {
                    "title": title,
                    "source": source,
                    "trace": trace,
                    "assigned_to": task.get("owner") or item.get("assigned_to"),
                    "attempts": int(item.get("attempts", 1)) + 1,
                    "updated_at": now_iso(),
                }
            )
            state.emit(
                "task.retrace",
                agent="queue",
                task=item["id"],
                source=task["id"],
                failure=trace.get("kind"),
            )
            return item
    fix = add_task(
        state,
        title,
        task.get("paths") or [],
        task.get("contracts") or [],
        [],
        f"Make {entry['branch']} pass the rejected integration, then submit it.",
        "queue",
        task.get("evals") or [],
    )
    fix.update(
        {
            "source": source,
            "trace": trace,
            "assigned_to": task.get("owner"),
            "attempts": 1,
        }
    )
    state.emit(
        "task.trace",
        agent="queue",
        task=fix["id"],
        source=task["id"],
        failure=trace.get("kind"),
    )
    return fix


def _close_trace_tasks(state, task, commit):
    for item in state.tasks.values():
        if item.get("id") == task["id"]:
            continue
        if (item.get("source") or {}).get("task") != task["id"]:
            continue
        if item.get("status") == "done":
            continue
        item.update(
            {
                "status": "done",
                "commit": commit,
                "done_at": now_iso(),
                "resolution": f"source {task['id']} merged",
                "updated_at": now_iso(),
            }
        )


def _resolve_source(state, task, commit):
    """A merged fix task also resolves the task whose failure opened it."""
    source_id = (task.get("source") or {}).get("task")
    if not source_id:
        return
    source = state.tasks.get(source_id)
    if source is None or source.get("status") == "done":
        return
    owner = source.get("owner")
    source.update(
        {
            "status": "done",
            "commit": commit,
            "done_at": now_iso(),
            "resolved_by": task["id"],
            "updated_at": now_iso(),
        }
    )
    if owner and state.agents.get(owner, {}).get("current_task") == source_id:
        state.agents[owner]["current_task"] = None
        state.agents[owner]["branch"] = None
    _close_trace_tasks(state, source, commit)
    state.emit("task.resolved", agent="queue", task=source_id, by=task["id"])


def _short_reason(reason, limit=90):
    text = " ".join(str(reason or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def clip(text, limit=4000):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "...(truncated)...\n" + text[-limit:]


# -- registry + local evals ----------------------------------------------


def profile_agent(state, agent, model=None, skills=None, note=None):
    require_role(state, agent, "writer", "edit profiles")
    record = state.agents.get(agent)
    if record is None:
        record = {"id": agent, "current_task": None, "branch": None}
        state.agents[agent] = record
        record["registered_at"] = now_iso()
    if model:
        record["model"] = model
    if skills is not None:
        record["skills"] = [item for item in skills if item]
    if note is not None:
        record["note"] = note
    record["profile_updated_at"] = now_iso()
    state.touch(agent)
    state.emit(
        "agent.profile",
        agent=agent,
        model=record.get("model"),
        skills=record.get("skills") or [],
    )
    return record


def eval_fingerprint(record):
    """Stable content fingerprint: same code + command + timeout => same value.

    Dirty/untracked input is included when present; clean scorecards omit it so
    fingerprints written by older boards still compare equal.
    """
    body = {
        "suite": (record or {}).get("suite"),
        "command": (record or {}).get("command"),
        "commit": (record or {}).get("commit"),
        "tree": (record or {}).get("tree"),
        "timeout": (record or {}).get("timeout"),
    }
    worktree = (record or {}).get("worktree")
    if worktree:
        body["worktree"] = worktree
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def eval_record(
    agent,
    suite,
    command,
    exit_code,
    stdout="",
    stderr="",
    duration_ms=0,
    timeout=300,
    task=None,
    branch=None,
    commit=None,
    timed_out=False,
    machine=None,
    sandbox=None,
    tree=None,
    worktree=None,
):
    record = {
        "id": new_ulid(),
        "at": now_iso(),
        "agent": agent,
        "machine": machine or machine_name(),
        "suite": suite,
        "command": command,
        "exit_code": exit_code,
        "passed": exit_code == 0 and not timed_out,
        "timed_out": bool(timed_out),
        "duration_ms": int(duration_ms),
        "timeout": timeout,
        "task": task,
        "branch": branch,
        "commit": commit,
        "tree": tree,
        "worktree": worktree,
        "stdout": clip(stdout),
        "stderr": clip(stderr),
        "sandbox": dict(sandbox or {}),
    }
    record["fingerprint"] = eval_fingerprint(record)
    return record


def set_eval_ttl(state, seconds, agent):
    require_role(state, agent, "admin", "change eval policy")
    state.project["eval_ttl_seconds"] = int(seconds)
    state.emit("project.eval_ttl", agent=agent, seconds=int(seconds))
    return state.project["eval_ttl_seconds"]


def record_eval(state, record, private_key=None, public_key=None):
    require_role(state, record.get("agent"), "writer", "record evals")
    mode = eval_attestation_mode(state)
    if mode == EVAL_HMAC_SCHEME:
        ensure_eval_key(state)
        sign_eval(state, record)
    elif mode == EVAL_ED25519_SCHEME or private_key:
        if not private_key:
            raise AghubError(
                "this board requires per-agent eval keys — run `aghub agent --key` first"
            )
        if not public_key:
            public_key = keys.public_key(bytes.fromhex(private_key)).hex()
        register_agent_key(state, record["agent"], public_key)
        attest_eval(record, private_key, public_key)
    else:
        ensure_eval_key(state)
        sign_eval(state, record)
    state.evals.append(record)
    state.evals.sort(key=lambda item: item.get("id", ""))
    state.new_evals.append(record)
    state.emit(
        "eval.recorded",
        agent=record["agent"],
        eval=record["id"],
        suite=record["suite"],
        passed=record["passed"],
    )
    return record


def latest_eval(state, agent=None, suite=None):
    for record in reversed(state.evals):
        if agent and record.get("agent") != agent:
            continue
        if suite and record.get("suite") != suite:
            continue
        return record
    return None


def path_violations(claimed_paths, changed_paths):
    claimed = claimed_paths or []
    return [
        path for path in changed_paths if not any(matches(pattern, path) for pattern in claimed)
    ]


def remote_branch_head(board, branch):
    """Fetch one remote branch and return its full commit id, if available."""
    remote = board.remote
    if not remote or not branch:
        return None
    ref = f"refs/remotes/{remote}/{branch}"
    fetched = gitops.git(
        ["fetch", remote, f"+refs/heads/{branch}:{ref}"],
        cwd=board.repo,
        check=False,
    )
    if fetched.returncode != 0:
        return None
    resolved = gitops.git(["rev-parse", ref], cwd=board.repo, check=False)
    return resolved.stdout.strip() or None


def branch_changed_paths(board, task, project):
    """List the files a task branch touches against trunk, for the gate.

    Returns ``None`` when the diff cannot be computed (no remote, missing
    branch, no merge base); callers treat that as "diff unknown" and fall back
    to advisory handling of protected-path rules. The merge queue, which has
    already fetched the branch, gets the real list and enforces the rules.
    """
    remote = board.remote
    branch = (task or {}).get("branch")
    if not remote or not branch:
        return None
    trunk = (project or {}).get("trunk", "main")
    agent_ref = f"refs/remotes/{remote}/{branch}"
    trunk_ref = f"refs/remotes/{remote}/{trunk}"
    gitops.git(
        ["fetch", remote, f"+refs/heads/{branch}:{agent_ref}"],
        cwd=board.repo,
        check=False,
    )
    gitops.git(
        ["fetch", remote, f"+refs/heads/{trunk}:{trunk_ref}"],
        cwd=board.repo,
        check=False,
    )
    if not gitops.ref_exists(board.repo, agent_ref) or not gitops.ref_exists(
        board.repo, trunk_ref
    ):
        return None
    base = gitops.git(
        ["merge-base", trunk_ref, agent_ref], cwd=board.repo, check=False
    ).stdout.strip()
    if not base:
        return None
    changed = gitops.git(
        ["diff", "--name-only", base, agent_ref], cwd=board.repo, check=False
    )
    return [line for line in changed.stdout.splitlines() if line.strip()]


def integrate(board, entry, task, project, expected_head=None):
    """Rehearse the merge on a detached worktree, check it, then push trunk."""
    remote = board.remote
    if not remote:
        return {"status": "rejected", "reason": "no shared remote; integration needs an origin"}

    trunk = project.get("trunk", "main")
    branch = entry["branch"]
    gitops.git(
        ["fetch", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"], cwd=board.repo
    )
    gitops.git(
        ["fetch", remote, f"+refs/heads/{trunk}:refs/remotes/{remote}/{trunk}"], cwd=board.repo
    )
    agent_ref = f"refs/remotes/{remote}/{branch}"
    trunk_ref = f"refs/remotes/{remote}/{trunk}"

    if expected_head:
        current = gitops.git(["rev-parse", agent_ref], cwd=board.repo).stdout.strip()
        if current != expected_head:
            return {
                "status": "rejected",
                "reason": (
                    f"branch moved after review: approved head {str(expected_head)[:7]}, "
                    f"remote head {str(current)[:7]} — re-approve the current head"
                ),
                "trace": {
                    "kind": "approval",
                    "approved": expected_head,
                    "head": current,
                    "branch": branch,
                },
            }

    if gitops.is_ancestor(board.repo, agent_ref, trunk_ref):
        return {"status": "merged", "commit": gitops.short(board.repo, trunk_ref), "reason": "already merged"}

    base = gitops.git(["merge-base", trunk_ref, agent_ref], cwd=board.repo).stdout.strip()
    changed = [
        line
        for line in gitops.git(["diff", "--name-only", base, agent_ref], cwd=board.repo)
        .stdout.splitlines()
        if line.strip()
    ]
    if not changed:
        return {"status": "rejected", "reason": "branch has no changes against trunk"}
    scope = str(project.get("scope") or "").strip("/")
    if scope:
        outside = [
            path for path in changed if not (path == scope or path.startswith(scope + "/"))
        ]
        if outside:
            return {
                "status": "rejected",
                "reason": f"touched outside project scope {scope}: "
                + ", ".join(outside[:5]),
                "trace": {
                    "kind": "scope",
                    "scope": scope,
                    "changed": changed,
                    "outside": outside,
                },
            }
    violations = path_violations(task.get("paths") or [], changed)
    if violations:
        return {
            "status": "rejected",
            "reason": "touched outside claim: " + ", ".join(violations[:5]),
            "trace": {
                "kind": "paths",
                "claimed": task.get("paths") or [],
                "changed": changed,
                "violations": violations,
            },
        }

    worktree = os.path.join(board.common, f"aghub-merge-{new_ulid()[:10]}")
    gitops.git(["worktree", "add", "--detach", worktree, trunk_ref], cwd=board.repo)
    try:
        merge = gitops.git(
            [
                "merge",
                "--no-ff",
                agent_ref,
                "-m",
                f"merge {task['id']}: {task.get('title', '')} ({entry['agent']})",
            ],
            cwd=worktree,
            check=False,
        )
        if merge.returncode != 0:
            conflicted = [
                line
                for line in gitops.git(
                    ["diff", "--name-only", "--diff-filter=U"], cwd=worktree, check=False
                ).stdout.splitlines()
                if line.strip()
            ]
            gitops.git(["merge", "--abort"], cwd=worktree, check=False)
            return {
                "status": "rejected",
                "reason": "merge conflict with trunk",
                "trace": {
                    "kind": "merge",
                    "trunk": trunk,
                    "branch": branch,
                    "conflicted": conflicted,
                },
            }

        check_command = project.get("check")
        if check_command:
            timeout = float(project.get("check_timeout_seconds", 300))
            mode = project.get("check_sandbox") or "auto"
            if task.get("require_sandbox"):
                mode = "require"
            try:
                checked = sandbox.run(
                    check_command, worktree, timeout=timeout, mode=mode
                )
            except AghubError as exc:
                return {
                    "status": "rejected",
                    "reason": f"check sandbox refused: {exc}",
                    "trace": {
                        "kind": "sandbox",
                        "command": check_command,
                        "mode": mode,
                        "error": str(exc),
                        "changed": changed,
                    },
                }
            if checked.timed_out:
                return {
                    "status": "rejected",
                    "reason": f"check timed out: {check_command}",
                    "trace": {
                        "kind": "timeout",
                        "command": check_command,
                        "timeout": timeout,
                        "sandbox": checked.sandbox,
                        "changed": changed,
                    },
                }
            if checked.exit_code != 0:
                output = (checked.stdout + "\n" + checked.stderr).strip().splitlines()
                tail = " | ".join(output[-6:]) if output else f"exit {checked.exit_code}"
                return {
                    "status": "rejected",
                    "reason": "check failed: " + tail,
                    "trace": {
                        "kind": "check",
                        "command": check_command,
                        "exit_code": checked.exit_code,
                        "stdout": clip(checked.stdout),
                        "stderr": clip(checked.stderr),
                        "sandbox": checked.sandbox,
                        "changed": changed,
                    },
                }

        commit = gitops.git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
        push = gitops.git(
            ["push", remote, f"HEAD:refs/heads/{trunk}"], cwd=worktree, check=False
        )
        if push.returncode != 0:
            detail = (push.stderr or push.stdout or "").strip().splitlines()
            return {
                "status": "rejected",
                "reason": "could not push trunk: " + (" | ".join(detail[-4:]) if detail else "unknown"),
                "trace": {
                    "kind": "push",
                    "detail": "\n".join(detail[-10:]),
                    "changed": changed,
                },
            }
        return {"status": "merged", "commit": commit[:7]}
    finally:
        gitops.git(["worktree", "remove", "--force", worktree], cwd=board.repo, check=False)
        gitops.git(["worktree", "prune"], cwd=board.repo, check=False)


def run_queue(board, agent, printer=print, private_key=None, public_key=None):
    require_role(board.load(), agent, "writer", "run the merge queue")
    processed = 0
    while True:
        entry = board.transaction("queue pick", queue_pick, agent=agent)
        if entry is None:
            if processed == 0:
                printer("queue: nothing waiting")
            return processed
        state = board.load()
        task = state.tasks.get(entry["task"], {})
        printer(f"→ {entry['task']} {task.get('title', '')} ({entry['agent']}) on {entry['branch']}")
        head = remote_branch_head(board, entry["branch"]) if task.get("requires_approval") else None
        changed_paths = (
            branch_changed_paths(board, task, state.project)
            if task.get("requires_approval")
            else None
        )
        missing = approval_gate(state, task, head=head, changed_paths=changed_paths)
        if missing:
            result = {
                "status": "rejected",
                "reason": missing,
                "trace": {"kind": "approval", "task": task.get("id")},
            }
        elif task.get("requires_approval") and head is None:
            result = {
                "status": "rejected",
                "reason": (
                    f"cannot verify the approval for {task['id']}: branch "
                    f"{entry['branch']} is not available on {board.remote or 'the remote'}"
                ),
                "trace": {"kind": "approval", "task": task.get("id")},
            }
        else:
            try:
                result = integrate(board, entry, task, state.project, expected_head=head)
            except AghubError as exc:
                result = {"status": "rejected", "reason": str(exc)}
        entry = board.transaction(
            f"queue {result['status']} {entry['task']}",
            lambda current, entry_id=entry["id"], result=result: queue_finalize(
                current,
                entry_id,
                result["status"],
                result.get("reason"),
                result.get("commit"),
                result.get("trace"),
                runner=agent,
                private_key=private_key,
                public_key=public_key,
            ),
            agent=agent,
        )
        if result["status"] == "merged":
            printer(f"   merged {result.get('commit')}")
        else:
            printer(f"   REJECTED: {result.get('reason')}")
            if entry.get("fix_task"):
                owner = task.get("owner") or "anyone"
                printer(f"   trace {entry['fix_task']} assigned to {owner}")
        processed += 1


# -- helpers -------------------------------------------------------------


def next_ready(state, agent=None):
    ttl = int(state.project.get("ttl_seconds", 600))
    ready = []
    for task in state.tasks.values():
        if effective_status(state, task) != "ready":
            continue
        if task.get("evals") and (agent is None or eval_gate(state, task, agent)):
            continue
        assignee = task.get("assigned_to")
        if assignee and assignee != agent:
            age = age_seconds(state.agents.get(assignee, {}).get("last_seen"))
            if age is not None and age <= ttl:
                continue
        ready.append(task)
    ready.sort(key=lambda task: (0 if task.get("trace") else 1, task["id"]))
    return ready[0] if ready else None
