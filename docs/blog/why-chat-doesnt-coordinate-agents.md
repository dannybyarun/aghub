# Why chat doesn't coordinate parallel coding agents

Put three coding agents on one repository and give them a shared chat channel. They will introduce themselves, agree on a plan, and then collide anyway. The failure is not that the models are bad at conversation. It is that conversation is the wrong data structure for coordination.

Parallel agents fail in three distinct ways, and each one needs a different mechanism. Chat addresses none of them, and at scale it makes the third one worse.

## Three kinds of collision

**Write collisions.** Two agents decide the new endpoint belongs in `app/api/routes.py`. Agent A appends a handler at the bottom. Agent B refactors the same file's error handling and rewrites the region around it. Whichever pushes second either gets a rejected push or resolves a conflict from memory, keeping its own version. A's handler disappears. The branch still builds, because A's test lives in a separate file and has not been merged yet. The damage surfaces later, in a merge commit nobody attributes to the deletion.

**Semantic collisions.** Agent A ships `GET /items` returning `{"items": [...]}`. Agent B, working from the shape it saw an hour ago, writes a client that treats the response as a bare array. The two agents touched no common file. Both branches are green in isolation. Both are correct against their own assumptions. The incompatibility only exists at the boundary between them, which neither agent owns. A merge queue catches this if the project's check exercises the boundary, and does not if the check is a unit suite that mocks it.

**Context collisions.** Agent C needs to know whether `api@2` is frozen and whether migration `0042` already exists. It asks the channel. It gets an answer that was true forty minutes ago, or an answer from an agent that has since changed its mind, or no answer at all. So it reads the repository and re-derives the state, duplicating work that another agent is doing right now. The expensive part is not the wrong answer. It is that the answer has no author, no version, and no expiry, so it cannot be trusted or checked.

These are not intelligence failures. A better model writes more code per minute, which increases the rate of all three.

## Why message passing does not fix it

A chat channel is nondeterministic. The same question gets a different answer depending on who is awake, what is in their context window, and what order the messages arrived in. There is no way to replay the decision later and see why it was made.

It is expensive in context. Every participant either reads the whole channel or risks missing the one message that mattered. Ten agents admit forty-five pairwise conversations; fifty agents admit 1,225. A single broadcast channel does not remove that fan-out, it concentrates it into one transcript that every agent must re-read. You pay for the same coordination tokens repeatedly, and you pay again on every retry and every new agent that joins.

It leaves no record. Chat scrolls, edits, deletes, and is not content-addressed. Nothing in it is attached to a commit, signed by the agent that said it, or verifiable after the fact. When a merge goes wrong, the transcript can tell you what people claimed, not what the repository contained.

And it has no enforcement. Even a perfect, permanent transcript does not stop the second agent from writing the same file. Coordination that consists entirely of asking is a synchronous dependency: an agent blocked on a reply is idle, and a quick question becomes a scheduling problem.

## The board model

`aghub` replaces the channel with four artifacts on a git branch.

The board is an orphan branch, `aghub/board`, checked out at `.git/aghub-board`. It is not on `main`, so coordination commits never appear in pull request diffs, and an agent that is mid-rebase can still publish status. Every event is a new file named by ULID, so concurrent appends do not collide on a shared counter. Events carry `prev` and `hash`, forming a hash chain, and are signed with the acting agent's Ed25519 key when one exists.

**Tasks and path claims.** A task declares the paths it owns:

```bash
aghub task add "Auth API" --paths 'src/api/**' --contract api@1
aghub claim T-001
```

`claim` rejects a task whose globs overlap another live claim. Globs support `*`, `**`, and `?`. The claim is not a permission system; the merge queue re-checks every changed file against it before merging.

**Frozen contracts.** A contract version is pinned before either side starts:

```bash
aghub contract freeze api 1 --from contracts/openapi.yaml
aghub codegen api 1 --out generated
```

Frozen versions are immutable. A task that declares `--contract api@1` cannot be claimed until that version exists. Both lanes generate from the same artifact and get the same bytes, so the boundary is settled before the work begins rather than discovered during integration.

**Written handoffs.** Instead of a status message, an agent leaves a structured delta:

```bash
aghub handoff T-002 --next "UI waits on api@2 for pagination" --note "mock covers all of api@1"
```

The handoff records what changed, what comes next, a diff summary, and a signature. A fresh agent reads the board and the last handoff and knows where to continue without asking anyone.

**A serial merge queue.** `aghub submit` pushes the branch and enqueues it. `aghub queue run` fetches one entry at a time, computes the diff against trunk, rejects files outside the claim or project scope, merges into a detached worktree to catch conflicts, runs the project's configured check, verifies approvals, and only then pushes to trunk. A rejection is not a log line: it opens a follow-up task carrying the failing command, exit code, output, and changed files.

## The architectural argument

Here is the part worth arguing about.

Git gives you a content-addressed history and a compare-and-swap on refs: a push either fast-forwards or it does not. Git does not give you a lease. A lease committed to a branch describes the state as of the last fetch. It cannot tell you who holds a claim right now, because "now" is not a git concept. Making a git-only lease authoritative would require the remote to reject a push based on a file that another push is concurrently mutating, which is a transaction across refs that git does not provide. Every git-only claim is therefore optimistic: read, decide, write, and retry if someone got there first. That catches most races. Under load it produces retry storms, which the swarm demo measured directly: 88 retried board writes at ten agents and 380 at twenty before the writer lease existed.

A hub can arbitrate a race that happens in the next two seconds because it is a single serializable store holding the lease table. `claim` asks the hub first. A 409 aborts the claim. A grant carries a fencing token that increases on every lease, and a stale token cannot publish a snapshot. Board writes take a project-scoped, TTL-bounded writer lease, so concurrent writers queue instead of rebuilding the same commit.

But the hub is not allowed to be the record. It is a live mirror: an ephemeral process with a SQLite file, a tunnel, a backup path, and audit rows that are not signed history. If it disappears, the branch is what remains. So the split is deliberate:

- **Claims are advisory in git and authoritative in the hub.** Without a hub they stop most races. With a hub, a live lease is a hard 409 at write time.
- **The merge queue is the backstop.** It re-derives the diff against trunk and enforces paths, scope, contracts, checks, and approvals at the only moment where all the facts are available.
- **Git is the durable record, not the lock.** The branch records what happened. It does not decide who may write next.

That is the whole design. Everything else is bookkeeping around it.

## Evidence

### A hub-only lease blocked a real claim

In the ten-machine run, one agent created a lease on the hub for a task that existed nowhere in git. While that lease was live, another machine tried to claim T-001, whose written path was the same file. The observed rejection:

```text
aghub: hub rejected the claim: path conflict: app/src/22_MoneySplit.gs overlaps app/src/22_MoneySplit.gs, leased by ghost-b on T-999
```

Git had nothing to find. `ghost-b` appeared in no board file; the board had exactly ten agent files. `T-999` appeared in no git history. The only `ghost` string in the trunk was a workflow matrix setting. The rejection could only have come from the hub's lease table. After the lease was released, the same claim succeeded.

### Ten simultaneous claims, one winner

All ten machines waited on a shared boundary and then called `aghub claim T-011` at the same instant. Machine `a5` won:

```text
claimed T-011 Shared winner race (a5)
  hub     lease granted
```

The nine losers received one of two authoritative errors, depending on whether they read the board after the winner had published:

```text
aghub: hub rejected the claim: T-011 is leased by a5 until 2026-11-06T16:54:22Z
aghub: T-011 is claimed by a5 (heartbeat Ns ago) - use --force to override
```

The hub audit contained exactly one T-011 lease claim, by `a5`, not ten. The winner wrote the shared module, and its test on trunk asserts the same value, so the task, artifact, and winner agree.

### Ten machines, a real project, a real test gate

Ten GitHub-hosted runner VMs across two GitHub accounts joined one board and completed 11 tasks against a verbatim copy of a real 414-file project (Apps Script plus Kotlin and Zig clients). The merge gate was the project's own suite plus every merged lane test:

```text
node app/tests/run-tests.js && node app/tests/run-lanes.js
```

Final results: **41 core tests and 79 lane tests passing, 0 failed**, 22 commits between baseline and final trunk, 67 board-lock cycles with strictly increasing fencing tokens, 12 lease claims, and 0 failed merges. All ten machines verified the same signed board: 77 events, 55 signed, 33 valid records, 0 invalid. A fresh clone of the final trunk reproduced 41 and 79 passing.

The work was pre-authored, so this measures coordination, merge-gate enforcement, and signing, not an LLM's coding quality. That distinction matters and is easy to lose in a headline.

### The local swarm curve

The swarm demo launches every claim at the same instant, then every submit, and merges all branches through the serial queue. From the README:

| agents | claims | submits | queue | total | lease waits | retries |
|---|---|---|---|---|---|---|
| 10 | 22.44s | 48.66s | 23.65s | 94.75s | 18 | 0 |
| 20 | 23.56s | 41.45s | 104.84s | 169.85s | 38 | 0 |
| 50 | 116.86s | 163.57s | 176.10s | 456.53s | 97 | 0 |

Before the writer lease, the same 10-agent demo produced 88 retried board writes; the 20-agent run produced 380. Both leased runs produced zero. Wall-clock time moves with host load, especially in the serial merge phase. The wait and retry counts are the stable signal, and the signal is that contention became queue time instead of duplicated work.

## What this does not solve

The limits are real, and softening them would make the rest less believable.

**One hub and one serialized board writer are the throughput ceiling.** Sixty-seven lock cycles for 11 tasks over roughly four minutes is fine at this size. It proves ten machines, not a hundred. Batched writes collapse many mutations into one transaction, and independent board shards give disjoint work its own ref, but neither makes one git ref parallel. Shards are separate worlds with no cross-shard ordering, so they only help work that genuinely does not need to integrate through one trunk.

**Security is guardrails, not a firewall.** ACLs stop accidental writes through the CLI. Anyone who can force-push `aghub/board` can rewrite it. Hash chains and signatures make tampering visible; they do not make the branch immutable. Hub tokens protect the HTTP API, not git. The Ed25519 helper is pure Python and not constant-time, which is fine for a trusted local repository and not fine for hostile co-tenants.

**Signatures bind records to published keys, not to people.** A board writer cannot mint a record for another agent without replacing that agent's published key, but the key is an identity, not a proof of who was at the keyboard.

**Machine identity on hosted runners is unreliable.** Ten VMs in the real run presented three distinct `/etc/machine-id` values and three hostnames, because GitHub's pool reuses image identity. `/etc/machine-id` is not usable there. `boot_id`, the runner id, and the `aghub` agent name are.

**Completed leases are left to TTL.** The final hub reported 11 live leases until their 600-second expiry. It caused no problem in that run because no task was re-claimed, but a swarm that tried to re-claim a finished task inside the TTL would be blocked by its own completed lease. Releasing on merge or shortening the TTL is the obvious fix, and it is not automatic today.

**The hub in that run was ephemeral.** SQLite behind a cloudflared quick tunnel with no uptime guarantee. The URL was dead after the session. Postgres support exists but was not exercised, because the integration DSN was unset; the run is evidence for the SQLite backend only.

## Try it

```bash
./demo/run_swarm.sh
SWARM_AGENTS=50 ./demo/run_swarm.sh
./demo/run_hub.sh
python3 -m unittest discover -s tests -v
```

`run_swarm.sh` creates one clone per agent, starts a local hub, fires every claim simultaneously, then every submit, and merges through the queue. `SWARM_HUB=0 ./demo/run_swarm.sh` runs the optimistic, hub-less path for comparison, which is where the retry counts come from. `run_hub.sh` starts a hub and two clones, grants a lease, and demonstrates the hub-only rejection shown above; it leaves the hub and web board running, and `--stop` ends them.

For the full protocol, see the [repository README](../../README.md). The longer write-up of the ten-machine run, including the audit tables, machine identities, and per-task merge order, is in [docs/REAL-PROJECT-10-MACHINE.md](../REAL-PROJECT-10-MACHINE.md).
