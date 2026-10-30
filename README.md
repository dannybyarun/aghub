# aghub

**A shared board so parallel coding agents can work on one repo without talking to each other.**

Two agents, two clones, one project. One takes the backend, one takes the frontend. Neither sends a message. They read a board, claim a lane, build against a frozen contract, and hand off in writing.

This is a working vertical slice of that protocol: the board, claims, contracts, handoffs, codegen from frozen contracts, a serial merge queue that turns failures into trace-carrying tasks, locally signed and sandboxed evals, hash-chained board history, project ACLs, human review inboxes and approval gates, a GitHub pull-request bridge, an MCP server and Python SDK, and a hub that arbitrates live leases and serves a web board.

```
$ aghub status
acme-store · board 1319e55 · trunk main bdf7644 · ttl 60s
contracts  api@1 frozen

AGENTS
  be-1       T-003    src/db/**                    seen 1m  STALE
  fe-1       —                                     seen 3s

TASKS
  stale    T-003 Seed data                        src/db/**                be-1, 1m STALE
  done     2

QUEUE  empty
```

## Why this exists

Agents working in parallel fail in three ways:

1. **Write collisions** — two agents touch the same files.
2. **Semantic collisions** — different files, incompatible assumptions (frontend expects `{items: []}`, backend returns `[...]`).
3. **Context collisions** — everything is fine except nobody knows what anyone else is doing, so every agent re-derives the world by asking.

Chat does not fix this. It is nondeterministic, expensive in context, and leaves no record. A written board does:

- **Tasks + claims** make ownership explicit and machine-readable.
- **Frozen contracts** let both sides build without coordinating, because the boundary is pinned before work starts.
- **Handoffs** replace conversation with a structured delta: what changed, what is next.
- **A merge queue** is the single authority that turns parallel branches into a serial trunk.
- **Signed history + reviews** let humans and other agents verify what happened, request changes, and stop high-risk merges.

Claims are advisory at write time (fast, works offline, catches most races). The queue is authoritative at merge time (path check + contract check + serial merge). That split is deliberate: you cannot make git leases truly authoritative across machines without a leader, so don't pretend — enforce at integration. With a hub, live leases become authoritative at write time too.

## Quick start

Requires Python 3.10+ and `git`.

```bash
python3 -m pip install -r requirements.txt
export PATH="$PWD/bin:$PATH"

# in a repo with a shared remote
aghub init --project my-app --check "npm test"
aghub agent --key
aghub contract freeze api 1 --from contracts/openapi.yaml
aghub codegen api 1 --out generated
aghub task add "Auth API" --paths 'src/api/**' --contract api@1

aghub status
aghub next
aghub claim T-001
```

An agent loop calls `aghub heartbeat` every `ttl/2` seconds while it works. If it dies, its claim goes stale and another agent can take the task with `--steal`.

Optional agent-native entry points:

```bash
aghub mcp serve --agent planner    # newline-delimited MCP JSON-RPC over stdio
aghub mcp tools                    # print the tool catalog
```

## Demo

```bash
./demo/run_demo.sh
```

Creates a bare origin, three clones (`fe`, `be`, `ci`), and walks through:

1. Freezing `api@1`, generating TypeScript, Python, a mock server, and a SQL migration, then reading the board over MCP stdio.
2. A second agent joining from another clone and reading the board.
3. Two agents claiming separate lanes; a third claim is rejected.
4. Parallel work with signed handoffs instead of messages.
5. Serial merge queue, a reproducibility check (`eval rerun` / `eval diff`), and the CI bridge.
6. A local eval gate, a simulated dead agent, lease expiry, and `--steal`.
7. An approval-gated task where the owner cannot self-approve: request changes, see the review inbox, approve, preview the GitHub review comment and check, then watch a new commit invalidate the approval until it is re-reviewed.
8. A cross-lane edit rejected by the queue; a fix task is opened with the changed files attached.
9. A semantic drift (`/health` not in the contract) rejected by the check; the fix task carries the failing command and output.
10. Signed board verification, then ACLs locking writes to known roles.

For the live layer:

```bash
./demo/run_hub.sh            # leaves the hub and web board running
./demo/run_hub.sh --stop     # stop the hub when the demo ends
```

It starts a hub, connects two clones, shows a lease being granted, and proves
that a hub-only lease can reject a claim the git board knows nothing about.
Open the printed URL to watch the board.

## Concepts

### The board is an orphan branch

All coordination state lives on `aghub/board`, checked out in `.git/aghub-board`, never on `main`.

- Board commits never pollute PR diffs or code history.
- An agent mid-rebase or mid-merge can still publish status.
- Every agent writes only its own files, and each event is a new ULID-named file — no shared counter, so concurrent appends do not collide.
- Events are hash-chained: every event carries `prev` and `hash`, and is signed with the acting agent's key when one exists.

```
aghub/board
  project.yaml
  tasks/T-001.yaml
  agents/fe-1.yaml
  evals/01J9...yaml
  approvals/01J9...yaml
  contracts/api/1/meta.yaml
  contracts/api/1/artifact.yaml
  handoffs/01J9...yaml
  events/01J9...yaml
  queue.yaml
```

### Signatures, verification, and keys

Every durable action that can be attributed to an agent is signed with a per-agent Ed25519 key:

- eval scorecards
- handoffs
- queue submissions and queue results
- failure traces
- human reviews
- board events (when a local key exists)

Private keys live in `.git/aghub.local.json` (mode `0600`) and never leave the clone. Only the public key is published on `agents/<id>.yaml`. Key rotation keeps the previous public key so old scorecards still verify; revocation makes old and future signatures from that key fail.

```bash
aghub agent --key
aghub agent --rotate-key
aghub agent --revoke-key
aghub acl revoke-key fe-1 --key-id <id>
aghub verify                    # event chain + all signed records
aghub verify --strict           # also fail on unsigned records
aghub board verify
aghub queue verify              # all queue entries, or one by id
aghub queue show Q-...
```

`verify` always fails on a broken hash chain or an invalid signature. It reports unsigned legacy records and events separately so old boards remain readable. A board writer can still rewrite git history; signatures make tampering visible, while ACLs and hub tokens make it harder to happen in the first place.

### The hub is the live authority

Git cannot arbitrate a race that happens in the next two seconds. The hub can.

```bash
aghub hub serve --port 8787 --db ~/.aghub/hub.db   # once, anywhere reachable
aghub hub connect http://hub-a:8787 http://hub-b:8787   # per clone; first is primary
aghub hub status
```

- **Leases are authoritative at write time.** `claim` asks the hub first. A 409 aborts the claim; a grant carries a fencing token that increases on every lease. Heartbeats extend the lease.
- **Presence is live.** Every board transaction publishes a snapshot and a heartbeat. `status` overlays hub presence and prints `· hub live` or `· hub offline`.
- **The web board never touches git.** `GET /` is a self-contained page: ready / in-flight / blocked lanes, agents, live leases, evals, dependency/fix graph, contracts and their latest diff, merge queue, reviews, handoffs, a replay scrubber, and a multi-project overview. It consumes `/api/events` over server-sent events, resumes with `Last-Event-ID`, and falls back to polling `/api/board` every 3s if the stream drops.
- **An outage degrades, it does not block.** The first unreachable hub produces one warning, then claims continue git-only. A reachable hub that returns 409 is always obeyed.
- **Failover is conservative.** Clients try the configured URLs in order on connection failures and `502`/`503`/`504`. Any other HTTP response — especially a lease `409` or a quota `429` — is authoritative and is never replayed against another hub.
- **Tenant quotas are shared.** A token can set `requests_per_minute` and `max_request_bytes`. Counters live in the hub store, so several hubs sharing one SQLite file or Postgres database enforce one budget per tenant.
- **Audit export is per tenant.** `/api/audit/export` streams JSONL or CSV. Admins can export any actor; non-admin tokens are pinned to their own actor label.
- **The hub is not the durable record.** Snapshots and leases are a live mirror. Tasks, contracts, handoffs and merges still live on `aghub/board`.

Default bind is `127.0.0.1:8787`. The server refuses a non-loopback address without a token. For hosted use:

```bash
aghub hub serve --host 0.0.0.0 --port 8787 \
  --token "$ADMIN_TOKEN" \
  --read-token "$READ_TOKEN" \
  --tokens-file hub-tokens.json \
  --rate-limit 600
```

`hub-tokens.json` accepts either a token → role map or per-token scopes, audit labels, and quotas:

```json
{
  "tokens": {
    "writer-token": {
      "role": "writer",
      "projects": ["acme-store"],
      "name": "frontend-ci",
      "quota": {"requests_per_minute": 120, "max_request_bytes": 1048576}
    },
    "ci-token": {
      "role": "admin",
      "projects": ["acme-store", "acme-infra"],
      "name": "release-bot"
    }
  }
}
```

Roles are `reader`, `writer`, and `admin`. Project scopes reject reads and writes outside the listed project ids, and an empty scope is rejected rather than falling back to the latest global board. `name` is the label used in the audit log and the key for quota accounting; when it is omitted, the hub derives `token-<sha256[:10]>` so tokens stay distinguishable without storing the secret. Give each independent tenant a unique name: tokens that share one share both their quota budget and their export scope. Quotas are optional and enforced per actor in the shared store: `requests_per_minute` applies a fixed-window budget, and `max_request_bytes` caps request bodies below the global 16 MiB ceiling. A zero limit disables that quota.

For per-token audit export:

```bash
aghub hub audit --format jsonl --limit 1000 --out audit.jsonl   # admin: every actor
curl -H "Authorization: Bearer $TOKEN" \
  'http://127.0.0.1:8787/api/audit/export?format=csv&actor=frontend-ci'
```

Non-admin tokens can export only their own actor; asking for another actor returns 403. Admin tokens may export any actor, project, or the whole hub. Use `after=SEQ` to page. `/api/health` is always open; it returns `200` when the store responds and `503` with `{"ok": false}` when it does not, so a load balancer can distinguish a live process from a broken backend. Other endpoints are rate-limited per IP, and request bodies are capped at 16 MiB. Hosted deployments can use Postgres instead of SQLite by installing `psycopg[binary]` and passing `--postgres postgresql://…` (the core install remains dependency-free). Postgres stores the quota counters too, so every hub process sharing that database enforces the same budget.

| Endpoint | Purpose |
|---|---|
| `GET /` | web board |
| `GET /api/health` | open liveness/version probe |
| `GET /api/projects` | published projects, latest first |
| `GET /api/overview` | aggregate task/queue/eval counts across projects |
| `GET /api/board?project=NAME` | snapshot + live agents + active leases |
| `GET /api/events?project=NAME` | server-sent board updates (SSE; `Last-Event-ID` replays) |
| `GET /api/history?project=NAME&after=SEQ&limit=N` | bounded board history (max 100) |
| `GET /api/audit?project=NAME&after=SEQ&limit=N&actor=NAME` | hub operation audit log (admin token) |
| `GET /api/audit/export?format=jsonl\|csv&actor=NAME&project=NAME&after=SEQ&limit=N` | per-token audit export (JSONL/CSV) |
| `POST /api/snapshot` | publish a board snapshot |
| `POST /api/heartbeat` | presence + lease extension |
| `POST /api/claim` | acquire a lease (409 on conflict) |
| `POST /api/release` | release a lease |
| `POST /api/debug/expire` | simulate a dead agent |

### ACLs and reviews

ACLs are opt-in. Until a board runs `aghub acl init`, every participant is a writer, preserving compatibility with existing boards.

```bash
aghub acl init
aghub acl set fe-1 writer
aghub acl set be-1 writer
aghub acl set reviewer admin
aghub acl remove be-1
aghub acl list
```

- `reader` can inspect the board but cannot mutate it.
- `writer` can add tasks, claim/release, hand off, submit, run evals, record profiles, edit contracts, and run the queue.
- `admin` additionally manages roles, approval decisions, eval policy, and revocation of another agent's key.
- `aghub acl remove AGENT` revokes membership entirely; `aghub acl set AGENT reader` keeps a visible read-only member.
- The last admin cannot demote or remove themselves; appoint another admin first.

Approval gates are per task:

```bash
aghub task add "Deploy billing" --paths 'src/billing/**' --requires-approval
aghub review T-007 --request-changes --note "add a rollback check"
aghub review T-007 --approve --note "reviewed migration and rollout plan"
```

`submit` and `queue run` refuse a gated task until the **latest valid review**
is an approval **for the current branch head**. A new commit invalidates the
approval; re-approve the current head with another `aghub review --approve`.
A request for changes also blocks submission until a later approval. A reviewer
can inspect the whole packet first with `aghub review T-007 --diff`. The task
owner cannot review their own work unless `--force` is passed, and with ACLs
enabled only an admin can review. Reviews are signed records on the board,
shown in the web task detail, and the legacy `aghub approve` command still
records an approval for compatibility.

Commit binding is recorded as `commit` plus `binding: commit`. Reviews require a
pushed branch (or an explicit `--commit SHA`); if the branch cannot be resolved,
the review command asks you to push it. Approvals recorded by older aghub
versions have no binding and remain accepted, so upgrading a board does not
silently invalidate its history.

`aghub inbox` is the human queue: pending approvals, requested changes, rejected
queue entries, and stale leases, each with the next command to run. Use
`aghub inbox --check` in a cron or agent loop when you only need a nonzero exit
status.

```bash
aghub inbox
aghub inbox --json
aghub review T-007                 # task, contracts, evals, queue, reviews, diffstat
aghub review T-007 --diff          # include the patch
```

### Contracts are the anti-conversation device

```
aghub contract draft api 2 --from openapi.yaml
aghub contract freeze api 2
```

- Frozen versions are immutable.
- A task declares `--contract api@1`; it cannot be claimed until that version is frozen.
- Frontend generates types/mocks from the artifact; backend implements against it.
- A new version flags dependents as `re-verify` (see the renderer): the board shows which tasks pinned an older version.

See what changed before regenerating or re-verifying:

```bash
aghub contract diff api            # previous frozen version → latest
aghub contract diff api 1 2
aghub contract diff api --json     # machine-readable for agents and CI
```

The diff reports routes, operations (with changed fields), and schema/property changes. The live board renders the latest pair and also shows dependency and fix-task edges in a graph panel.

### Codegen makes the contract executable

```bash
aghub codegen api 1 --out generated
aghub codegen api 1 --out generated --target sql
aghub codegen api 1 --out generated --check   # CI gate: fail on drift
```

Generation is deterministic and only runs against a frozen version, so both
lanes get the same bytes for the same `name@version`.

| File | Contents |
|---|---|
| `<name>.ts` | route literals, operation keys, schema types, and a typed `ContractClient` |
| `<name>.py` | route tuple, operation map, dataclasses, raw schema map, and a stdlib `Client` |
| `<name>_mock.py` | standalone stdlib mock server with CORS and example responses |
| `<name>.sql` | starting Postgres migration inferred from component schemas |
| `manifest.json` | contract id, sha256, routes, operations, generated files |

```bash
python3 generated/api_mock.py --port 4010
```

The generator covers the pragmatic OpenAPI subset this protocol needs: `paths`, HTTP methods, `operationId`, `components.schemas` / Swagger `definitions`, `$ref`, arrays, enums, unions, examples, and the common scalar formats. It is deliberately not a full OpenAPI compiler, and the SQL file is a reviewable starting point — not a live schema diff.

### The registry is the board; evals run locally

```bash
aghub registry profile --model my-model --skills python,api --note "backend lane"
aghub registry list
aghub eval run smoke --command "pytest -q" --task T-001
aghub eval verify <eval-id>
aghub eval rerun <eval-id>
aghub eval diff <left-id> <right-id>
aghub eval ttl 86400
aghub eval list
```

- Profiles live on `agents/<id>.yaml`; eval scorecards live in `evals/<ulid>.yaml`.
- `eval run` executes the command against a snapshot of your checkout, records the exit code, duration, branch/commit, git tree, content fingerprint, task, machine, sandbox policy, and tail-bounded output, then publishes the scorecard.
- Eval execution is local and BYOK: the platform never runs inference or pays for eval compute; only the scorecard is published.
- With Bubblewrap installed, evals run with no network, a read-only host filesystem, masked home directories, a writable `/tmp`, and a scrubbed environment. If Bubblewrap is unavailable, `--sandbox auto` still uses a snapshot plus resource limits but cannot hide the host filesystem or network; `--sandbox require` refuses that fallback and `--sandbox off` runs live in the checkout. Toolchains installed under `$HOME` need `--sandbox off`.
- `--require-eval-sandbox` on `init` and `--requires-sandbox` on a task make Bubblewrap mandatory for the relevant eval gates and merge checks. An `auto`-mode eval on such a task is escalated to `require` automatically; an explicit `--sandbox off` still opts out and will not satisfy the gate.
- Scorecards are signed with a per-agent Ed25519 key. `eval run` generates a private key in `.git/aghub.local.json` (mode `0600`) on first use, publishes only the public key on `agents/<id>.yaml`, and signs the scorecard. `eval verify` checks the signature against that published key; `eval ttl` reads or sets how long a pass stays valid (default 7 days, `0` disables expiry).
- `aghub agent --key` shows or creates the local key; `aghub agent --rotate-key` rotates it and keeps the previous public key for old scorecards. Boards created before per-agent keys still verify their legacy HMAC scorecards, and `init --attestation hmac-sha256` keeps that mode explicitly.
- Reproducibility is content-addressed: the fingerprint covers suite, command, commit, git tree, timeout, and the content of uncommitted/untracked (non-ignored) input when the worktree is dirty. `eval rerun` repeats the stored command and prints whether the fingerprint matches; `eval diff` shows exactly which fields or sandbox properties changed.
- A failed eval is still recorded. `eval run` exits nonzero, so CI can gate on it.
- `status`, `status --for AGENT`, `status --json`, and the live hub board all show the latest scorecard per agent, including stale and unsigned markers.

A task can require an eval:

```bash
aghub task add "Payments API" --paths 'src/api/**' --eval smoke
aghub eval run smoke --command "pytest -q"
aghub claim T-004
```

`claim` refuses until that agent's latest `smoke` scorecard passed, is inside the
freshness window, verifies against that agent's published key, and satisfies any
sandbox policy. `next` skips gated tasks the agent cannot claim yet, and fix tasks
inherit the gate. Re-run the suite before a stale pass blocks you; `eval ttl`
changes the window for the whole board.

### Path claims and monorepos

```
aghub task add "Items UI" --paths 'src/web/**' --contract api@1
```

`claim` rejects a task whose paths overlap another live claim. Globs support `*`, `**`, `?`. The merge queue re-checks every changed file against the claim, so a claim is not a permission system — it is a collision detector with teeth at integration.

For a board that owns one subtree of a larger repository:

```bash
aghub init --project web --scope packages/web
aghub task add "Settings page"          # defaults to packages/web/**
```

Task paths must stay inside `project.scope`, and the queue rejects any merged change outside it. Scope is a path prefix, not a separate git repository or workspace graph.

Two agents can share one clone without sharing a working tree:

```bash
aghub claim T-001 --worktree              # ../<repo>-<agent>-<task>
aghub claim T-002 --worktree ../fe-lane   # explicit path
```

If the checkout cannot be created, the claim is released again rather than
leaving a task stranded.

### Handoffs

```
aghub handoff T-002 --next "UI waits on api@2 for pagination" --note "mock covers all of api@1"
```

Written to `handoffs/<ulid>.yaml`, including a diff summary and a signature when
the board uses per-agent keys. A fresh agent reads the board and the last handoff
and knows where to continue. `--done` closes the task. `aghub verify` checks the
signature alongside the rest of the board.

### The merge queue

```
aghub submit T-002        # pushes agent/fe-1/T-002, enqueues it
aghub queue run           # on any machine; merges one at a time
aghub queue show Q-...    # full entry, including signatures and trace
```

For each entry the queue:

1. Fetches the branch and computes the diff against trunk.
2. Rejects files outside the project scope or the claim.
3. Merges into a detached worktree (catches conflicts).
4. Runs `project.yaml: check` (your contract and integration tests; evals are separate local scorecards) under the sandbox policy.
5. Checks any required human approval.
6. Pushes to trunk, then marks the task done.

Failed entries stay claimed so the owner can fix and resubmit. The queue serializes, so two branches that are green alone but incompatible together cannot both land. The queue result is signed by the runner.

### Failures become trace-carrying tasks

A rejection is not just a log line. The queue attaches the failing case to the
task and opens a follow-up:

```
→ T-006 Health endpoint (be-1) on agent/be-1/T-006
   REJECTED: check failed: routes not in the frozen contract: ['/health']
   trace T-007 assigned to be-1
```

`aghub task show T-007` carries:

- `source`: the failed task, queue entry, branch, and agent.
- `trace.kind`: `check`, `paths`, `merge`, `timeout`, `push`, `sandbox`, `approval`, or `scope`.
- `trace.command`, `exit_code`, `stdout`, `stderr`: the actual failing case (tail-bounded).
- `trace.changed` / `trace.conflicted` / `trace.violations` / `trace.outside`: the files involved.
- `attempts`: incremented when the same task fails again instead of piling up tasks.
- An Ed25519 signature from the queue runner.

The fix task inherits the original paths and contracts and is assigned to the
owner. It stays out of other agents' `next` while the assignee is live; after the
lease goes stale, anyone can take it with `--steal`. Claiming clears
`assigned_to`. A successful resubmit closes the fix task, and merging a fix task
also resolves its source task.

### CI and automation

```bash
aghub ci workflow --out .github/workflows/aghub.yml
aghub ci workflow --contract api --codegen-out generated   # add a drift gate
aghub ci status --json
```

The workflow template installs aghub's one runtime dependency (`PyYAML`), sets a
git identity for board commits and queue merges, attaches the shared board,
prints board status, verifies signatures and history, runs the merge queue, and
emits a machine-readable CI bridge. Pass `--contract` to add a hard
`codegen --check` drift gate; it is omitted by default because generated output
paths are project-specific. Set `--trunk` if the board's trunk is not `main`.
`ci status` returns nonzero when a queue entry is rejected and still unresolved,
so a job can fail on it.

CI runners are ephemeral, so their key would otherwise change on every run.
Store a 32-byte hex Ed25519 seed in the `AGHUB_AGENT_KEY` secret (generate one
with `python3 -c 'import secrets; print(secrets.token_hex(32))'`, or any
Ed25519 seed generator) to keep the `ci` identity stable. When it is absent, the
runner generates a fresh local key as usual. The template assumes `bin/aghub`
is present in the repository (as it is here); replace that path if your project
installs or vendors the CLI differently.

### Review inbox and the GitHub bridge

The review layer has two entry points. `aghub inbox` is local and read-only;
`aghub github sync` publishes the same review packet onto a pull request.

```bash
aghub ci workflow --github-review --out .github/workflows/aghub-review.yml
```

The generated workflow:

- runs on pull-request updates and submitted pull-request reviews;
- checks out the **base** commit and only fetches the pull-request head for diffing,
  so no untrusted pull-request code is executed;
- attaches the shared board, records trusted `APPROVED` / `CHANGES_REQUESTED`
  reviews, then updates one sticky PR comment and one `aghub/review` check run;
- skips fork pull requests by default: they receive a read-only token and no
  secrets, so they cannot publish to the board or GitHub.

Set `AGHUB_AGENT_KEY` to a stable 32-byte hex Ed25519 seed for the workflow
identity (default `github-review`); the board review is signed by that identity
and the note records the actual GitHub login. If ACLs are enabled, give that
identity an admin role so it can record reviews. `AGHUB_HUB_TOKEN` is optional
and only mirrors board changes to a hub. The workflow needs `contents: write`,
`pull-requests: write`, and `checks: write`.

Task resolution is intentionally boring: the PR branch must equal the task
branch, or the title/body must contain `aghub-task: T-003` (or a plain `T-003`).
The bridge only records reviews for gated tasks, only from
`OWNER,MEMBER,COLLABORATOR` by default, and deduplicates by GitHub review id.
Change the trust set with `--allow-association`; fork approval recording is
refused unless `--allow-fork-reviews` is passed explicitly.

For local or one-off syncs:

```bash
aghub github sync --task T-007 --dry-run --json  # inspect the comment and check
aghub github sync --record-reviews           # use GITHUB_EVENT_PATH + GITHUB_TOKEN
aghub github sync --task T-007 --no-comment --check
```

The check is `success` when merged or ready for the queue, `failure` when the
queue rejected the branch, `action_required` for a pending approval or requested
changes, and `neutral` while work is in progress. If a GitHub approval was
recorded for an earlier head SHA, the comment and check mark it as stale and ask
for a fresh approval; `submit` and `queue run` now reject that stale approval as
well. The board verifies the branch head again immediately before merging, so a
push that lands between queue selection and integration is rejected instead of
merged against an old approval. The comment marker
`<!-- aghub:review -->` is what makes it sticky across updates.

### MCP and the Python SDK

`aghub mcp serve` exposes the board over newline-delimited JSON-RPC 2.0 on stdio,
the transport used by Claude Desktop, Cursor, and editor agents. Every tool wraps
the same CLI and identity rules as a human or script.

```json
{
  "mcpServers": {
    "aghub": {
      "command": "/path/to/aghub/bin/aghub",
      "args": ["mcp", "serve", "--agent", "planner"]
    }
  }
}
```

Tools include board status, next task, task list/show/add, claim, release,
handoff, submit, heartbeat, approve, verify, eval run/list, queue list, and
agent key. Resources `aghub://board/status` and `aghub://board/tasks` expose
read-only views.

For Python agents:

```python
from aghub.sdk import Agent

with Agent("planner") as agent:
    print(agent.status())
    agent.claim("T-001", worktree="../lane")
    agent.add_task("Docs", paths=["docs/**"], requires_approval=True)
    agent.eval_run("smoke", "python3 -m unittest")
    agent.handoff("T-001", next_step="review docs", done=False)
    assert agent.verify()["chain"]["ok"]
```

The SDK shells through the CLI so leases, ACLs, approvals, and signing are never
reimplemented or bypassed.

## Commands

| Command | Purpose |
|---|---|
| `aghub init --project NAME [--scope SUBDIR] [--ttl 600] [--eval-ttl 604800] [--check CMD] [--check-sandbox auto\|off\|require] [--check-timeout N] [--require-eval-sandbox] [--attestation ed25519\|hmac-sha256]` | create / join the board and set policy |
| `aghub status [--json] [--for AGENT]` | the digest, safe to paste into a prompt |
| `aghub next` | first ready task, or the exact blockers |
| `aghub agent [ID] [--key] [--rotate-key] [--revoke-key] [--key-id ID]` | set identity and manage this clone's eval key |
| `aghub acl init\|list\|set\|remove\|revoke-key` | manage roles and key revocation |
| `aghub task add TITLE --paths GLOB [--contract api@1] [--eval smoke] [--depends T-001] [--requires-approval] [--requires-sandbox]` | define work |
| `aghub task show TASK` | full task YAML, including any failure trace |
| `aghub approve TASK [--note TEXT] [--commit SHA] [--force]` | legacy alias for `review --approve` |
| `aghub inbox [--json] [--check]` | pending approvals, requested changes, failed queue entries, stale leases |
| `aghub review TASK [--diff] [--json] [--approve\|--request-changes] [--note TEXT] [--commit SHA] [--force]` | inspect one task and record a signed, commit-bound human review |
| `aghub verify [--strict] [--json]` / `board verify` | verify the event chain and signed records |
| `aghub registry profile [--model M] [--skills a,b] [--note TEXT]` | publish this agent's profile |
| `aghub registry show [AGENT]` / `registry list` | inspect the registry |
| `aghub eval run SUITE --command CMD [--task T] [--timeout N] [--sandbox auto\|off\|require]` | run an eval locally and publish the scorecard |
| `aghub eval verify ID` / `eval rerun ID` / `eval diff LEFT RIGHT` | check or compare scorecards |
| `aghub eval ttl [SECONDS]` | show or set the scorecard freshness window |
| `aghub eval list [--agent ID] [--suite NAME]` / `eval show ID` | inspect scorecards |
| `aghub contract draft/freeze/show/list` | pin interfaces |
| `aghub contract diff NAME [OLD] [NEW] [--json]` | compare two frozen versions |
| `aghub codegen NAME [VERSION] [--out DIR] [--target all\|ts\|py\|mock\|sql] [--check]` | generate from a frozen contract |
| `aghub claim TASK [--steal] [--force] [--worktree [PATH]]` | take task + path lease + branch (+ isolated checkout) |
| `aghub heartbeat` | refresh the lease |
| `aghub handoff TASK --next TEXT [--done]` | write the signed async delta |
| `aghub release TASK` | give work back |
| `aghub submit TASK` | push branch and enqueue |
| `aghub queue run\|list\|show [ID]\|verify [ID]` | integration and queue inspection |
| `aghub ci workflow [--out PATH] [--trunk BRANCH] [--contract NAME] [--codegen-out DIR] [--github-review]` / `ci status [--json]` | CI template, review bridge, and gate |
| `aghub github sync [--task TASK] [--event PATH] [--repo OWNER/NAME] [--record-reviews] [--dry-run] [--json]` | publish review comment/check and record trusted PR reviews |
| `aghub mcp serve [--agent ID]` / `mcp tools` | MCP stdio server and tool catalog |
| `aghub sync` | fetch board + trunk |
| `aghub hub serve [--host H] [--port P] [--db PATH] [--token T] [--read-token T] [--tokens-file F] [--rate-limit N] [--postgres DSN]` | run the lease/presence hub + web board |
| `aghub hub connect URL [URL ...] [--token T]` | point this clone at a hub, with optional failover URLs |
| `aghub hub audit [--format jsonl\|csv] [--actor ID] [--project NAME] [--after SEQ] [--limit N] [--out PATH]` | export the hub operation log |
| `aghub hub status` | probe every configured hub and the published project |
| `aghub hub disconnect` | fall back to git-only state |
| `aghub debug expire AGENT` | simulate a dead agent (demo/test) |

Agent identity: `--agent`, `$AGHUB_AGENT`, or the clone-local `.git/aghub.local.json` (`aghub agent fe-1`).

## Layout

```
bin/aghub              entrypoint
src/aghub/             board, service, review, github, codegen, render, sandbox, hub, client, MCP, SDK, CI, CLI
demo/run_demo.sh       the two-agent story
demo/run_hub.sh        the live layer: leases, presence, web board
tests/                 protocol, trust, codegen, hub, MCP, sandbox, and end-to-end tests
```

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

The Postgres backend has optional integration tests. They are skipped unless
`AGHUB_TEST_POSTGRES_DSN` points at a disposable database and `psycopg` is
installed:

```bash
AGHUB_TEST_POSTGRES_DSN='postgresql://postgres@127.0.0.1:5432/aghub_test' \
  python3 -m unittest tests.test_postgres -v
```

They cover store and HTTP behavior, a two-connection claim race, and concurrent
first startup against an empty schema. The tests use uniquely named projects
and delete their own rows; still, point them at a scratch database.

## Known limits (on purpose)

- **Claims are advisory in git, authoritative in the hub.** Without a hub they stop most races, not all; the queue is the backstop. With a hub, a live lease is a hard 409.
- **Integration needs a shared remote.** `submit`/`queue run` require `origin`; local-only mode supports the board but not merging.
- **Board authorization is guardrails, not a git firewall.** ACLs stop accidental reader writes through the CLI, but anyone who can force-push `aghub/board` can rewrite it. Event hashes and signatures make tampering visible; they do not make the branch immutable. Hub tokens protect the HTTP API, not git.
- **Signatures bind records to published keys, not to people.** Private keys live outside the board, so a board writer cannot read them or mint a valid record for another agent without replacing that agent's published key. Old records stay valid until their key is revoked. The Ed25519 helper is pure Python, not constant-time, and is intended for local trusted-repo use rather than hostile co-tenants.
- **Latest review wins by board time.** Review order is the signed record's `at` timestamp, with its ULID as the tie-breaker; clock skew between reviewers can therefore reorder reviews. New reviews are commit-bound: a new branch head invalidates an approval, and `submit` plus `queue run` reject the mismatch. There are still no required-reviewer counts, review expiry, or diff-conditioned approvals. Legacy reviews without a binding remain accepted for compatibility.
- **The GitHub bridge is a workflow, not an App.** It runs only on the events in the generated workflow, needs a same-repository PR by default, publishes one sticky comment and one `aghub/review` check, and records only GitHub `APPROVED` / `CHANGES_REQUESTED` reviews. Fork PRs are skipped (read-only token, no secrets) and fork approvals are refused unless explicitly allowed. Dismissed or commented reviews are not treated as decisions, and the workflow identity must be an admin when ACLs are enabled.
- **A review packet needs the task branch locally.** `aghub review --diff` fetches the branch from the board's remote when possible; if the owner has not pushed it yet, the packet shows the task and gate state but not a patch.
- **Legacy and pre-key events may be unsigned.** Records created before a key exists are reported as unsigned; `verify --strict` fails on unsigned records, while chain errors and invalid signatures always fail.
- **Live updates send full snapshots.** SSE pushes the whole board on change and keeps the last 100 per project for `Last-Event-ID` replay; there are no per-task deltas or websocket fan-out yet. A reconnect older than the window gets the oldest retained snapshot and then the current board.
- **Postgres is optional.** The default hub is a single SQLite file. `--postgres` requires `psycopg[binary]` in the runtime environment, uses per-project advisory transaction locks for lease decisions, and serializes concurrent first startup with a schema lock; the default suite skips Postgres integration tests unless `AGHUB_TEST_POSTGRES_DSN` is set.
- **Failover needs one authority.** A failover set must point at hubs backed by the same store (Postgres, or the same SQLite file with one writer). Two independent databases are two independent worlds: failover provides availability, not consensus.
- **Quotas are coarse fixed windows.** `requests_per_minute` uses a per-actor counter in the shared store, not a token bucket, so a tenant can burst at a window boundary. `max_request_bytes` is checked from `Content-Length`; chunked request bodies are not accepted. A quota `429` is authoritative and is never retried against another hub.
- **Audit export is not the signed git history.** `/api/audit/export` records hub requests and lease operations with a token actor label. Tamper evidence for tasks, contracts, handoffs, and merges still comes from `aghub verify` over the hash-chained board.
- **The merge check is sandboxed by default but is still not a security boundary.** `submit`/`queue run` execute `project.check` through the same sandbox runner as evals (`auto` uses Bubblewrap, otherwise a scrubbed snapshot). `--check-sandbox off` and `--sandbox off` intentionally opt out. Traces are signed evidence, not proof of isolation; re-run the command to reproduce.
- **Codegen covers an OpenAPI subset.** Paths, methods, `operationId`, component schemas, `$ref`, arrays, enums, unions, examples, common scalar formats, and a starting SQL migration. Security schemes, callbacks, discriminators, and parameter validation are not generated.
- **Eval fingerprints identify an input, not a whole environment.** They cover command, suite, commit, tree, dirty/untracked input, and timeout. `eval rerun` compares fingerprints but does not pin toolchain or container images, and ignored files are not hashed.
- **The eval sandbox is pragmatic.** The snapshot omits ignored files such as `.venv` or `node_modules`, so a command that needs them must install them inside the eval or use `--sandbox off`. Bubblewrap is optional and detected at runtime; without it, the fallback hides the environment but not the host filesystem or network.
- **Evals are still local and BYOK.** The platform records the command, sandbox policy, scorecard, and freshness; it does not verify the machine or reproduce the environment.
- **Scope is a path prefix.** `--scope packages/web` keeps tasks and merged changes inside that subtree. It does not model nested projects, independent remotes, or workspace-level dependency graphs.
- **Agents must commit.** The queue merges branches; it does not snapshot working trees.

## Next

1. Web review workspace: a real reviewer queue with inline diff review and signed approve / request-changes actions.
2. Policy: required reviewers, expiring approvals, and approval conditions tied to diffs.
3. GitHub App path: OAuth/webhooks, fork-safe review recording, richer check annotations, and multi-repository boards.
4. Deeper codegen: parameter validation, auth schemes, and dialect-aware migrations.
