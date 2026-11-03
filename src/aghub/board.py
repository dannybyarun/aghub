import hashlib
import json
import os
import random
import secrets
import sys
import time

import yaml

from . import gitops, keys
from .errors import AghubError
from .ulid import new_ulid
from .util import age_seconds, now_iso, slug

BOARD_BRANCH = "aghub/board"
BOARD_ATTEMPTS = max(
    1, int(os.environ.get("AGHUB_BOARD_ATTEMPTS", "24") or 24)
)


def read_yaml(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return default if data is None else data


def write_yaml(path, data):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True, width=100)
    os.replace(tmp, path)


def order_events(events):
    """Return events in hash-chain order, independent of ULID collisions.

    ULIDs are only time-ordered to the millisecond, so several events emitted in
    one transaction can sort lexically out of emission order. The chain's
    ``prev`` links are the source of truth; IDs are only a deterministic
    tie-breaker for forks and legacy events.
    """
    hashed = [event for event in events or [] if event.get("hash")]
    by_hash = {}
    for event in hashed:
        by_hash.setdefault(event["hash"], event)
    children = {}
    indegree = {}
    for event in by_hash.values():
        prev = event.get("prev")
        indegree[event["hash"]] = 0
        if prev and prev in by_hash and prev != event["hash"]:
            indegree[event["hash"]] = 1
            children.setdefault(prev, []).append(event)
    ready = sorted(
        (event for event in by_hash.values() if indegree[event["hash"]] == 0),
        key=lambda item: item.get("id", ""),
    )
    ordered = []
    seen = set()
    while ready:
        event = ready.pop(0)
        if event["hash"] in seen:
            continue
        seen.add(event["hash"])
        ordered.append(event)
        for child in sorted(
            children.get(event["hash"], []), key=lambda item: item.get("id", "")
        ):
            indegree[child["hash"]] -= 1
            if indegree[child["hash"]] == 0:
                ready.append(child)
                ready.sort(key=lambda item: item.get("id", ""))
    # A cycle or duplicate is broken, but keep every event visible to verify.
    for event in sorted(hashed, key=lambda item: item.get("id", "")):
        if event["hash"] not in seen:
            ordered.append(event)
    return ordered


def event_tip(events):
    ordered = order_events(events)
    return ordered[-1].get("hash") if ordered else None


def local_config_path(common):
    return os.path.join(common, "aghub.local.json")


def read_local_config(common):
    path = local_config_path(common)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_local_config(common, config):
    path = local_config_path(common)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, sort_keys=True)
        handle.write("\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def event_payload(event):
    """Canonical event bytes excluding the hash and signature envelopes."""
    body = {
        key: value
        for key, value in (event or {}).items()
        if key not in ("hash", "signature")
    }
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def verify_event_chain(events, agents):
    """Verify the event hash chain and every available Ed25519 signature."""
    errors = []
    signed = 0
    unsigned = 0
    legacy = sum(1 for event in (events or []) if not event.get("hash"))
    previous_hash = None
    for event in order_events(events):
        event_id = event.get("id") or "?"
        digest = event.get("hash")
        if not digest:
            legacy += 1
            continue
        expected = hashlib.sha256(event_payload(event)).hexdigest()
        if digest != expected:
            errors.append(f"{event_id}: hash mismatch")
            continue
        if event.get("prev") and event["prev"] != previous_hash:
            errors.append(f"{event_id}: broken chain link")
        signature = event.get("signature") or {}
        if signature:
            signer = signature.get("signer") or event.get("agent") or ""
            profile = (agents or {}).get(signer) or {}
            public_key = str(signature.get("public_key") or "")
            revoked = {
                str(item.get("key_id") if isinstance(item, dict) else item)
                for item in profile.get("revoked_key_ids") or []
            }
            allowed = [str(profile.get("public_key") or "")] + [
                str(item) for item in profile.get("previous_public_keys") or []
            ]
            if not public_key or public_key not in allowed:
                errors.append(f"{event_id}: signer key is not published for {signer or '?'}")
            elif signature.get("key_id") in revoked:
                errors.append(f"{event_id}: signer key was revoked")
            elif signature.get("key_id") != keys.key_id(public_key):
                errors.append(f"{event_id}: signer key id mismatch")
            elif not keys.verify(public_key, bytes.fromhex(digest), str(signature.get("signature") or "")):
                errors.append(f"{event_id}: signature invalid")
            else:
                signed += 1
        else:
            unsigned += 1
        previous_hash = digest
    return {
        "ok": not errors,
        "events": len(events or []),
        "signed": signed,
        "unsigned": unsigned,
        "legacy": legacy,
        "errors": errors,
        "head": previous_hash,
    }


def local_agent_key(common, agent):
    config = read_local_config(common) or {}
    return (config.get("agent_keys") or {}).get(agent)


def ensure_local_agent_key(common, agent, rotate=False):
    """Load this clone's private key for an agent, generating one if needed.

    ``AGHUB_AGENT_KEY`` accepts a 32-byte hex Ed25519 seed so CI runners and
    ephemeral containers can keep a stable identity from a secret instead of
    publishing a new key on every run. Explicit environment keys win over the
    clone-local cache; rotating them must happen in the secret store.
    """
    config = read_local_config(common) or {}
    records = config.setdefault("agent_keys", {})
    entry = records.get(agent)
    env_key = (os.environ.get("AGHUB_AGENT_KEY") or "").strip()
    if env_key:
        if rotate:
            raise AghubError(
                "AGHUB_AGENT_KEY is set; rotate the key in the secret store instead"
            )
        try:
            seed = bytes.fromhex(env_key)
        except ValueError:
            seed = b""
        if len(seed) != 32:
            raise AghubError("AGHUB_AGENT_KEY must be a 32-byte hex Ed25519 seed")
        public_key = keys.public_key(seed).hex()
        if (
            not entry
            or entry.get("private_key") != env_key
            or entry.get("public_key") != public_key
        ):
            entry = {
                "alg": "ed25519",
                "private_key": env_key,
                "public_key": public_key,
                "created_at": now_iso(),
                "source": "env",
            }
            records[agent] = entry
            write_local_config(common, config)
        return entry
    if (
        rotate
        or not entry
        or not entry.get("private_key")
        or not entry.get("public_key")
    ):
        private_key, public_key = keys.generate()
        entry = {
            "alg": "ed25519",
            "private_key": private_key,
            "public_key": public_key,
            "created_at": now_iso(),
        }
        records[agent] = entry
        write_local_config(common, config)
    return entry


class State:
    """In-memory view of the board at one revision."""

    def __init__(self, board):
        self.board = board
        self.project = {}
        self.tasks = {}
        self.agents = {}
        self.contracts = {}
        self.queue = []
        self.handoffs = []
        self.evals = []
        self.approvals = []
        self.events = []
        self.new_events = []
        self.new_handoffs = []
        self.new_evals = []
        self.new_approvals = []
        self.new_artifacts = {}
        self.last_event_hash = None

    # -- loading ---------------------------------------------------------

    def load(self):
        base = self.board.dir
        self.project = read_yaml(os.path.join(base, "project.yaml"), {}) or {}

        tasks_dir = os.path.join(base, "tasks")
        if os.path.isdir(tasks_dir):
            for name in sorted(os.listdir(tasks_dir)):
                if name.endswith(".yaml"):
                    task = read_yaml(os.path.join(tasks_dir, name), {})
                    if task.get("id"):
                        self.tasks[task["id"]] = task

        agents_dir = os.path.join(base, "agents")
        if os.path.isdir(agents_dir):
            for name in sorted(os.listdir(agents_dir)):
                if name.endswith(".yaml"):
                    agent = read_yaml(os.path.join(agents_dir, name), {})
                    if agent.get("id"):
                        self.agents[agent["id"]] = agent

        contracts_dir = os.path.join(base, "contracts")
        if os.path.isdir(contracts_dir):
            for name in sorted(os.listdir(contracts_dir)):
                versions_dir = os.path.join(contracts_dir, name)
                if not os.path.isdir(versions_dir):
                    continue
                for version in sorted(os.listdir(versions_dir)):
                    meta_path = os.path.join(versions_dir, version, "meta.yaml")
                    meta = read_yaml(meta_path)
                    if meta:
                        self.contracts.setdefault(name, {})[int(meta["version"])] = meta

        queue = read_yaml(os.path.join(base, "queue.yaml"), {"entries": []}) or {}
        self.queue = queue.get("entries") or []

        handoffs_dir = os.path.join(base, "handoffs")
        if os.path.isdir(handoffs_dir):
            for name in sorted(os.listdir(handoffs_dir)):
                if name.endswith(".yaml"):
                    record = read_yaml(os.path.join(handoffs_dir, name), {})
                    if record:
                        self.handoffs.append(record)
        self.handoffs.sort(key=lambda item: item.get("id", ""))

        evals_dir = os.path.join(base, "evals")
        if os.path.isdir(evals_dir):
            for name in sorted(os.listdir(evals_dir)):
                if name.endswith(".yaml"):
                    record = read_yaml(os.path.join(evals_dir, name), {})
                    if record.get("id"):
                        self.evals.append(record)
        self.evals.sort(key=lambda item: item.get("id", ""))

        approvals_dir = os.path.join(base, "approvals")
        if os.path.isdir(approvals_dir):
            for name in sorted(os.listdir(approvals_dir)):
                if name.endswith(".yaml"):
                    record = read_yaml(os.path.join(approvals_dir, name), {})
                    if record.get("id"):
                        self.approvals.append(record)
        self.approvals.sort(key=lambda item: item.get("id", ""))

        events_dir = os.path.join(base, "events")
        if os.path.isdir(events_dir):
            for name in sorted(os.listdir(events_dir)):
                if name.endswith(".yaml"):
                    event = read_yaml(os.path.join(events_dir, name), {})
                    if event.get("id"):
                        self.events.append(event)
        self.events.sort(key=lambda item: item.get("id", ""))
        self.last_event_hash = event_tip(self.events)

        return self

    # -- writing ---------------------------------------------------------

    def save(self):
        base = self.board.dir
        write_yaml(os.path.join(base, "project.yaml"), self.project)

        for task_id in sorted(self.tasks):
            write_yaml(os.path.join(base, "tasks", f"{task_id}.yaml"), self.tasks[task_id])

        for agent_id in sorted(self.agents):
            write_yaml(os.path.join(base, "agents", f"{agent_id}.yaml"), self.agents[agent_id])

        for name in sorted(self.contracts):
            for version in sorted(self.contracts[name]):
                meta = self.contracts[name][version]
                write_yaml(
                    os.path.join(base, "contracts", name, str(version), "meta.yaml"), meta
                )

        write_yaml(os.path.join(base, "queue.yaml"), {"entries": self.queue})

        for record in self.new_handoffs:
            write_yaml(os.path.join(base, "handoffs", f"{record['id']}.yaml"), record)

        for record in self.new_evals:
            write_yaml(os.path.join(base, "evals", f"{record['id']}.yaml"), record)

        for record in self.new_approvals:
            write_yaml(os.path.join(base, "approvals", f"{record['id']}.yaml"), record)

        for event in self.new_events:
            write_yaml(os.path.join(base, "events", f"{event['id']}.yaml"), event)

        for relative, blob in self.new_artifacts.items():
            path = os.path.join(base, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(blob)

    # -- helpers ---------------------------------------------------------

    def emit(self, kind, agent=None, task=None, **extra):
        event = {
            "id": new_ulid(),
            "at": now_iso(),
            "type": kind,
            "agent": agent,
            "task": task,
        }
        event.update(extra)
        event["prev"] = self.last_event_hash
        digest = hashlib.sha256(event_payload(event)).hexdigest()
        event["hash"] = digest
        self.last_event_hash = digest
        self.events.append(event)
        self.new_events.append(event)
        return event

    def touch(self, agent_id, min_interval=15):
        if not agent_id:
            return
        agent = self.agents.get(agent_id)
        if agent is None:
            agent = {"id": agent_id, "current_task": None, "branch": None}
            self.agents[agent_id] = agent
        age = age_seconds(agent.get("last_seen"))
        if age is None or age >= min_interval:
            agent["last_seen"] = now_iso()
        agent["machine"] = os.uname().nodename
        return agent


class Board:
    """The board branch, checked out in its own worktree under .git."""

    def __init__(
        self, repo, common, worktree, remote, hub=None, hub_token=None,
        hub_failover=None, branch=BOARD_BRANCH,
    ):
        self.repo = repo
        self.common = common
        self.dir = worktree
        self.remote = remote
        self.hub = hub
        self.hub_token = hub_token
        if isinstance(hub_failover, str):
            hub_failover = [hub_failover]
        self.hub_failover = hub_failover or []
        self.branch = branch or BOARD_BRANCH
        self.retries = 0
        self.lock_waits = 0

    @property
    def hub_urls(self):
        urls = []
        for url in [self.hub, *(self.hub_failover or [])]:
            if url and url not in urls:
                urls.append(url)
        return urls

    def project_name(self):
        project = read_yaml(os.path.join(self.dir, "project.yaml"), {}) or {}
        return project.get("name") or "default"

    @classmethod
    def discover(cls, cwd=None, shard=None):
        repo = gitops.repo_root(cwd)
        common = gitops.common_dir(repo)
        config = read_local_config(common)
        if not config:
            raise AghubError("aghub is not initialized here — run `aghub init --project <name>`")
        shard = shard if shard is not None else (os.environ.get("AGHUB_SHARD") or None)
        if shard:
            shard = slug(shard)
            entry = (config.get("boards") or {}).get(shard)
            if not entry:
                raise AghubError(
                    f"unknown board shard '{shard}' — create it with "
                    f"`aghub --shard {shard} init --project <name>`"
                )
            worktree_name = entry.get("worktree") or f"aghub-board-{shard}"
            branch = entry.get("branch") or f"{BOARD_BRANCH}-{shard}"
        else:
            worktree_name = config.get("worktree", "aghub-board")
            branch = config.get("branch") or BOARD_BRANCH
        worktree = os.path.join(common, worktree_name)
        if not os.path.isdir(worktree):
            raise AghubError("the board worktree is missing — run `aghub init` again")
        return cls(
            repo,
            common,
            worktree,
            config.get("remote"),
            config.get("hub"),
            config.get("hub_token"),
            config.get("hub_failover"),
            branch=branch,
        )

    @property
    def head(self):
        return gitops.short(self.dir, "HEAD")

    def load(self):
        return State(self).load()

    def project_config(self):
        """Just `project.yaml`: a field without the cost of a full board load."""
        return read_yaml(os.path.join(self.dir, "project.yaml"), {}) or {}

    def _remote_ready(self):
        return bool(self.remote) and gitops.remote_branch_exists(self.repo, self.remote, self.branch)

    def sync(self):
        if not self._remote_ready():
            return False
        gitops.git(
            [
                "fetch",
                self.remote,
                f"+refs/heads/{self.branch}:refs/remotes/{self.remote}/{self.branch}",
            ],
            cwd=self.dir,
        )
        gitops.git(["reset", "--hard", f"refs/remotes/{self.remote}/{self.branch}"], cwd=self.dir)
        return True

    def _dirty(self):
        return bool(gitops.out(["status", "--porcelain"], self.dir))

    def _commit(self, label):
        gitops.git(["add", "-A"], cwd=self.dir)
        gitops.git(["commit", "-m", f"aghub: {label}"], cwd=self.dir)

    def _board_lock_settings(self):
        try:
            ttl = max(5, int(os.environ.get("AGHUB_BOARD_LOCK_TTL", "60") or 60))
        except (TypeError, ValueError):
            ttl = 60
        try:
            wait = max(0.0, float(os.environ.get("AGHUB_BOARD_LOCK_WAIT", "180") or 0))
        except (TypeError, ValueError):
            wait = 180.0
        return ttl, wait

    def _acquire_board_lock(self, label, agent):
        """Ask the hub for the project-scoped board write lease.

        The lease is an optimization, not a correctness boundary: git still
        arbitrates the ref, and an unreachable or absent hub immediately falls
        back to optimistic retries. A reachable hub that says the lock is held
        is obeyed until its TTL expires or the wait deadline passes.
        """
        if not self.hub_urls or os.environ.get(
            "AGHUB_BOARD_LOCK", "1"
        ).strip().lower() in (
            "0",
            "false",
            "off",
            "no",
        ):
            return None
        from . import hubclient

        project = self.project_name()
        holder = f"{agent or 'board'}@{os.uname().nodename}:{os.getpid()}"
        ttl, wait = self._board_lock_settings()
        deadline = time.monotonic() + wait
        delay = 0.05
        warned = False
        while True:
            try:
                result = hubclient.board_lock(
                    self.hub_urls, project, holder, ttl, token=self.hub_token
                )
                return {
                    "project": project,
                    "holder": holder,
                    "fencing": result.get("fencing"),
                }
            except hubclient.HubConflict as exc:
                self.lock_waits += 1
                if not warned:
                    warned = True
                    if os.environ.get("AGHUB_TRACE_BOARD"):
                        holder_info = (exc.payload or {}).get("holder") or {}
                        print(
                            f"aghub: board lock busy for {label} "
                            f"(holder {holder_info.get('holder')}); waiting",
                            file=sys.stderr,
                        )
                if time.monotonic() >= deadline:
                    hubclient.warn_once(
                        "could not acquire the board writer lock before the wait "
                        "deadline; continuing with optimistic board writes"
                    )
                    return None
                time.sleep(min(0.75, delay) * (0.5 + random.random()))
                delay = min(1.0, delay * 1.6)
            except hubclient.HubError as exc:
                if getattr(exc, "status", None) == 429:
                    if time.monotonic() >= deadline:
                        hubclient.warn_once(
                            "hub is rate limiting board lock requests; continuing "
                            "with optimistic board writes"
                        )
                        return None
                    time.sleep(min(2.0, delay + 0.5))
                    delay = min(1.0, delay * 1.6)
                    continue
                if getattr(exc, "timed_out", False):
                    if time.monotonic() >= deadline:
                        hubclient.warn_once(
                            "hub timed out while granting the board writer lock; "
                            "continuing with optimistic board writes"
                        )
                        return None
                    time.sleep(min(1.0, delay) * (0.5 + random.random()))
                    delay = min(1.0, delay * 1.6)
                    continue
                hubclient.warn_once(
                    f"hub at {self.hub} is unreachable ({exc}); continuing with "
                    "optimistic board writes"
                )
                return None

    def _release_board_lock(self, lock):
        if not lock:
            return
        from . import hubclient

        try:
            hubclient.board_unlock(
                self.hub_urls,
                lock["project"],
                lock["holder"],
                fencing=lock.get("fencing"),
                token=self.hub_token,
            )
        except hubclient.HubError as exc:
            hubclient.warn_once(f"could not release the board writer lock ({exc})")

    def transaction(self, label, mutate, agent=None):
        """Read the freshest board, apply `mutate`, commit and push.

        On a rejected push we discard the local commit, re-read the remote board
        and run the mutation again. Mutations must therefore be functions of the
        state passed to them, not of earlier reads.

        Concurrent writers collide on the board ref, so retries are staggered
        with randomized exponential backoff. Without the jitter every agent
        retries in lockstep and a large swarm can exhaust the attempt budget
        even though each individual write is valid.

        When a hub is configured, the whole read -> mutate -> push -> publish
        sequence holds a project-scoped writer lease. That turns a swarm of
        colliding writers into a short queue, while the optimistic retry loop
        remains the fallback whenever the hub is absent or offline.
        """
        return self.batch(label, [mutate], agent=agent)[0]

    def batch(self, label, mutations, agent=None):
        """Apply several mutations in one board transaction.

        Each mutation is a function of the loaded state, applied in order. The
        whole batch shares a single writer-lease acquisition, one fetch, one
        commit, and one push — so seeding N tasks costs one round trip instead
        of N. A rejected push re-reads the board and replays every mutation,
        exactly like a single transaction, so mutations must stay pure
        functions of the state passed to them. Returns the list of per-mutation
        results in order.
        """
        mutations = list(mutations)
        lock = self._acquire_board_lock(label, agent)
        try:
            last_error = None
            for attempt in range(BOARD_ATTEMPTS):
                self.sync()
                state = State(self).load()
                state.touch(agent)
                results = [mutate(state) for mutate in mutations]
                self._sign_events(state, agent)
                state.save()
                fencing = lock.get("fencing") if lock else None
                if not self._dirty():
                    self._publish(state, agent, fencing)
                    return results
                self._commit(label)
                if not self.remote:
                    self._publish(state, agent, fencing)
                    return results
                push = gitops.git(
                    ["push", self.remote, f"HEAD:refs/heads/{self.branch}"],
                    cwd=self.dir,
                    check=False,
                )
                if push.returncode == 0:
                    self._publish(state, agent, fencing)
                    return results
                self.retries += 1
                last_error = (push.stderr or push.stdout or "").strip()
                if os.environ.get("AGHUB_TRACE_BOARD"):
                    print(
                        f"aghub: board retry {attempt + 1}/{BOARD_ATTEMPTS} "
                        f"for {label}",
                        file=sys.stderr,
                    )
                if attempt + 1 < BOARD_ATTEMPTS:
                    delay = min(0.12, 0.005 * (2 ** min(attempt, 5)))
                    time.sleep(delay * (0.5 + random.random()))
            raise AghubError(
                f"could not update the shared board after {BOARD_ATTEMPTS} attempts"
                + (f": {last_error}" if last_error else "")
            )
        finally:
            self._release_board_lock(lock)

    def _sign_events(self, state, agent):
        """Sign new chained events with the acting agent's local key, if any."""
        if not agent:
            return
        entry = local_agent_key(self.common, agent)
        if not entry or not entry.get("private_key") or not entry.get("public_key"):
            return
        for event in state.new_events:
            if event.get("signature") or not event.get("hash"):
                continue
            public_key = entry["public_key"]
            event["signature"] = {
                "scheme": "ed25519",
                "signer": agent,
                "key_id": keys.key_id(public_key),
                "public_key": public_key,
                "signature": keys.sign(entry["private_key"], bytes.fromhex(event["hash"])),
            }

    def _publish(self, state, agent, fencing=None):
        """Push the live snapshot + heartbeat to the hub, best effort.

        The hub is a live mirror and lease authority, never the durable record,
        so an outage degrades to git-only behaviour instead of blocking work.
        """
        if not self.hub:
            return
        from . import hubclient, render

        project = state.project.get("name") or "default"
        try:
            hubclient.publish(
                self.hub_urls,
                project,
                render.snapshot(state, self),
                self.hub_token,
                fencing=fencing,
            )
            if agent:
                record = state.agents.get(agent) or {}
                hubclient.heartbeat(
                    self.hub_urls,
                    project,
                    agent,
                    record.get("current_task"),
                    record.get("branch"),
                    int(state.project.get("ttl_seconds", 600)),
                    self.hub_token,
                    machine=os.uname().nodename,
                )
        except hubclient.HubConflict as exc:
            hubclient.warn_once(
                f"hub rejected the board snapshot for {project}: {exc}"
            )
        except hubclient.HubError as exc:
            hubclient.warn_once(
                f"hub at {self.hub} is unreachable ({exc}); continuing with git-only state"
            )


def board_entries(common):
    """List the boards configured in this clone: the default plus any shards."""
    config = read_local_config(common) or {}
    entries = [
        {
            "name": "default",
            "worktree": config.get("worktree", "aghub-board"),
            "branch": config.get("branch") or BOARD_BRANCH,
            "default": True,
        }
    ]
    for name in sorted(config.get("boards") or {}):
        meta = config["boards"][name] or {}
        entries.append(
            {
                "name": name,
                "worktree": meta.get("worktree") or f"aghub-board-{name}",
                "branch": meta.get("branch") or f"{BOARD_BRANCH}-{name}",
                "default": False,
            }
        )
    return entries


def init_board(
    repo,
    project,
    trunk="main",
    ttl=600,
    remote=None,
    worktree="aghub-board",
    check=None,
    eval_ttl=7 * 24 * 60 * 60,
    attestation="ed25519",
    check_sandbox="auto",
    check_timeout=300,
    require_eval_sandbox=False,
    scope=None,
    shard=None,
):
    common = gitops.common_dir(repo)
    existing = read_local_config(common)
    shard_slug = slug(shard) if shard else None
    if shard_slug:
        if not existing:
            raise AghubError(
                "initialize the default board first: `aghub init --project <name>`"
            )
        if shard_slug in (existing.get("boards") or {}):
            raise AghubError(f"board shard '{shard_slug}' already exists")
        branch = f"{BOARD_BRANCH}-{shard_slug}"
        worktree = f"aghub-board-{shard_slug}"
        if remote is None:
            remote = existing.get("remote")
    else:
        if existing:
            raise AghubError("aghub is already initialized in this repository")
        branch = BOARD_BRANCH
    if remote and not gitops.has_remote(repo, remote):
        raise AghubError(f"remote '{remote}' does not exist")

    target = os.path.join(common, worktree)
    if os.path.exists(target):
        raise AghubError(f"{target} already exists")

    remote_ready = bool(remote) and gitops.remote_branch_exists(repo, remote, branch)
    if remote_ready:
        gitops.git(
            ["fetch", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"], cwd=repo
        )
        gitops.git(
            ["worktree", "add", "-b", branch, target, f"refs/remotes/{remote}/{branch}"], cwd=repo
        )
    else:
        tree = gitops.git(["mktree"], cwd=repo, input="").stdout.strip()
        commit = gitops.git(
            ["commit-tree", tree, "-m", "aghub: initialize board"], cwd=repo
        ).stdout.strip()
        gitops.git(["update-ref", f"refs/heads/{branch}", commit], cwd=repo)
        gitops.git(["worktree", "add", target, branch], cwd=repo)

    if shard_slug:
        existing.setdefault("boards", {})[shard_slug] = {
            "worktree": worktree,
            "branch": branch,
        }
        write_local_config(common, existing)
        board = Board(
            repo,
            common,
            target,
            remote,
            existing.get("hub"),
            existing.get("hub_token"),
            existing.get("hub_failover"),
            branch=branch,
        )
    else:
        write_local_config(
            common,
            {
                "worktree": worktree,
                "remote": remote,
                "agent": None,
                "branch": branch,
                "hub": None,
                "hub_token": None,
                "hub_failover": [],
                "boards": {},
            },
        )
        board = Board(repo, common, target, remote, branch=branch)

    def ensure(state):
        if not state.project:
            state.project = {
                "name": project,
                "trunk": trunk,
                "ttl_seconds": int(ttl),
                "remote": remote,
                "check": check,
                "check_sandbox": check_sandbox,
                "check_timeout_seconds": int(check_timeout),
                "eval_ttl_seconds": int(eval_ttl),
                "eval_attestation": attestation,
                "require_eval_sandbox": bool(require_eval_sandbox),
                "scope": scope.strip("/") if scope else None,
                "created_at": now_iso(),
                "created_by": None,
            }
            if attestation == "hmac-sha256":
                state.project["eval_secret"] = secrets.token_hex(24)
            state.emit("board.init", project=project)
        else:
            if "eval_attestation" not in state.project:
                state.project["eval_attestation"] = (
                    "hmac-sha256" if state.project.get("eval_secret") else attestation
                )
            if (
                state.project["eval_attestation"] == "hmac-sha256"
                and not state.project.get("eval_secret")
            ):
                state.project["eval_secret"] = secrets.token_hex(24)
            if "eval_ttl_seconds" not in state.project:
                state.project["eval_ttl_seconds"] = int(eval_ttl)
            state.project.setdefault("check_sandbox", check_sandbox)
            state.project.setdefault("check_timeout_seconds", int(check_timeout))
            state.project.setdefault("require_eval_sandbox", bool(require_eval_sandbox))
        return dict(state.project)

    board.transaction("initialize board", ensure)
    return board
