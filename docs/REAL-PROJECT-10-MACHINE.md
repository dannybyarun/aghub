# Ten machines, one board: a real-project swarm run

On 2026-11-06, ten GitHub-hosted runner VMs across two GitHub accounts joined a single `aghub` board and worked on a copy of a real project. This is what was tested, what the hub enforced that git could not, and where the design still has limits.

## 1. What this is

The subject was `milk-center-ledger`, an Apps Script backend with Kotlin and Zig clients. At commit `ab2fedd` it holds 414 tracked files and its own suite reports 41 passed, 0 failed. A `git archive` of that commit was copied verbatim into `app/` of a disposable repository, roughly 4.5 MB, so every lane would import the project's real helpers instead of a mock. Ten machines then claimed ten disjoint lanes plus one shared race, wrote an Apps Script module and a lane test each, handed off in writing, and submitted to a serial merge queue whose gate was the project's own test suite. No agent sent a message to another agent. The shared state was the board, the queue, and one trunk.

The result: ten of ten jobs succeeded, eleven tasks merged, all eleven merges passed the gate, and all ten machines independently verified the same signed board history. The original checkout was never modified; after the run it remained clean at `ab2fedd` with its 41 tests passing.

## 2. Topology

| Piece | Configuration |
|---|---|
| Hub | `aghub hub` v0.10.0, bound to `127.0.0.1:8788`, fresh SQLite `hub.db`, project `swarm-real` |
| Ingress | Cloudflare quick tunnel, ephemeral and session-only |
| Canonical repo | `aghub-swarm-live`, private and disposable; `main` plus the `aghub/board` ref |
| Second-account repo | `aghub-swarm-runners`, private, runner-only |
| Account B checkout | `actions/checkout` of the canonical repo using a cross-repo token |
| Account A matrix | `a1`-`a5`, Ubuntu 22.04 and 24.04, max-parallel 5 |
| Account B matrix | `b1`-`b5`, Ubuntu 22.04 and 24.04, max-parallel 5 |

An `aghub` board is tied to one git remote, so the second account did not get a second board. Its five runners checked out the canonical repo and pushed back to it. Both workflows received the same hub URL, hub token, and project name, which is what made ten independent jobs write to one board and one trunk.

The board branch records exactly ten agents, `agents/a1.yaml` through `agents/b5.yaml`.

## 3. The run

| Metric | Result |
|---|---|
| Jobs | 10/10 success |
| Tasks merged | 11 |
| Commits between baseline `5656e48` and final trunk `f0bfe03` | 22 (11 submissions + 11 merges) |
| Files changed / insertions | 22 files, 1,149 insertions |
| Core suite on final trunk | 41 passed, 0 failed |
| Lane suite on final trunk | 79 passed, 0 failed |
| Wall time | 4m13s (account A), 4m10s (account B) |
| Retries | 0 |
| Error lines | 0 |

Account A's jobs started at 16:43:52Z and account B's at 16:44:01Z; the last machine joined the board at 16:44:34Z, and the last merge landed at 16:47:42Z. The queue's first submission to last merge was 2m51s. Both workflows ran with `fail-fast: false`, so no failure was hidden by cancellation.

## 4. Why the hub mattered: the ghost lease

Git can tell you that two branches changed the same path. It cannot tell you that an agent is holding a path *right now*. That distinction was tested deliberately.

Account B's `b1` created a lease on the hub for a task that existed nowhere in git:

```text
EVIDENCE|ghost|lease_created|fencing=3 paths=app/src/22_MoneySplit.gs
```

Twenty-five seconds later it released the lease:

```text
EVIDENCE|ghost|lease_released|T-999
```

While that lease was live, account A's leader `a1` tried to claim T-001, whose declared write path is exactly `app/src/22_MoneySplit.gs`. The hub refused:

```text
aghub: hub rejected the claim: path conflict: app/src/22_MoneySplit.gs overlaps app/src/22_MoneySplit.gs, leased by ghost-b on T-999
EVIDENCE|a1|hub_only_lease|blocked claim T-001 exit=1
```

There was nothing for git to find. `ghost-b` appears in no board file, T-999 appears in no git history, and the only `ghost` string in the trunk is a workflow matrix setting. The rejection could only have come from the hub's lease table. Once the lease was released, `a1` claimed T-001 successfully and eventually merged it as the final commit. A hub-only fact stopped a real write.

## 5. One winner from ten simultaneous claims

For the shared race, all ten machines called `wait_boundary 10` and then `aghub claim T-011` at the same wall-clock boundary. One won:

```text
# a5
claimed T-011 Shared winner race (a5)
  hub     lease granted
EVIDENCE|a5|race|won T-011
```

The other nine, `a1` through `a4` and `b1` through `b5`, were rejected in one of two authoritative forms: `T-011 is leased by a5 until 2026-11-06T16:54:22Z`, or, on machines that read the board after the winner published, `T-011 is claimed by a5 (heartbeat Ns ago) - use --force to override`. The hub audit contains exactly one T-011 lease claim, by `a5`, not ten.

The winner then wrote the artifact the task asked for:

```javascript
function sharedWinner_() {
  return 'a5';
}
```

The test merged on the trunk asserts the same value. Task, artifact, and winner agree.

## 6. Serial merge queue

The leader `a1` ran `aghub queue run` in a loop while the other nine machines submitted. All eleven entries merged in submission order, with no rejection and no conflict:

| # | Task | Agent | Merged commit | Submitted | Finished |
|---|---|---|---|---|---|
| 1 | T-011 | a5 | `b51fb3d` | 16:44:51Z | 16:46:21Z |
| 2 | T-009 | b4 | `d74231f` | 16:44:56Z | 16:46:30Z |
| 3 | T-004 | a4 | `634ead4` | 16:45:04Z | 16:46:40Z |
| 4 | T-005 | a5 | `d4eb961` | 16:45:13Z | 16:46:48Z |
| 5 | T-006 | b1 | `9f0eda2` | 16:45:31Z | 16:46:55Z |
| 6 | T-002 | a2 | `4834aea` | 16:45:44Z | 16:47:03Z |
| 7 | T-008 | b3 | `b7b8633` | 16:45:49Z | 16:47:11Z |
| 8 | T-003 | a3 | `eca899b` | 16:45:53Z | 16:47:18Z |
| 9 | T-010 | b5 | `d6961d5` | 16:46:02Z | 16:47:26Z |
| 10 | T-007 | b2 | `23066ca` | 16:46:06Z | 16:47:34Z |
| 11 | T-001 | a1 | `f0bfe03` | 16:46:09Z | 16:47:42Z |

Every entry in `queue.yaml` ended with status `merged` and `reason: null`. The merge gate was:

```text
node app/tests/run-tests.js && node app/tests/run-lanes.js
```

The first command is the project's original 41-test suite. The second runs every lane test merged so far, so the gate tightened as the trunk grew. The queue advanced `main` by one merge commit per task; the final board commit is `4b77296`.

## 7. Verification

Independent verification used `aghub verify` on every machine. All ten produced identical values:

```text
event chain  ok - 77 events - 55 signed - 22 unsigned - 0 legacy
  head  7717d3c63412031a
records      33 ok - 0 invalid - 0 unsigned
EVIDENCE|<agent>|verify|signed board verified
```

Ten of ten also reported that all ten lane modules plus the shared winner were present on `origin/main`, and ten of ten reached the final `done` marker.

The counts reconcile exactly. The 55 signed events are 10 `agent.key`, 11 `task.handoff`, 11 `queue.submit`, 11 `queue.pick`, 11 `queue.merged`, and 1 signed `task.claim`. The 22 unsigned events are 1 `board.init`, 11 `task.add`, and 10 `task.claim`. The `33 ok` records are the 11 handoffs, 11 queue submissions, and 11 queue merge attestations. No record was invalid and no record was unsigned.

## 8. Independent trunk check

The workflows' final display step printed `Lane tests: 0 file(s)`. That is a harness bug, not a failed gate: the step ran against the job's pre-merge local `main`, while verification had inspected `origin/main` through `git cat-file` rather than checking it out. To settle it, the final trunk was cloned fresh after the run:

```text
git clone --branch main <canonical-repo>
node app/tests/run-tests.js   ->  41 passed, 0 failed
node app/tests/run-lanes.js   ->  79 passed, 0 failed
```

The clone is at `f0bfe03` and contains all eleven `22_*.gs` modules, including `22_SharedWinner.gs`, and all eleven lane test files. The published trunk is correct on its own terms.

## 9. Findings and limits

These are part of the result.

- **`/etc/machine-id` is not a machine identity on GitHub-hosted runners.** The ten VMs had ten distinct runner IDs and ten distinct `boot_id` values, but only three distinct `/etc/machine-id` values and three hostnames across the pool. Anything that keys identity or leases off `machine-id` will merge distinct workers into one. Use `boot_id`, the runner ID, or the `aghub` agent name, as this run did.
- **The serial board writer is the scaling ceiling.** The hub logged 67 board-lock cycles for 11 tasks over about four minutes, with fencing increasing strictly from 1 to 67. That is comfortable here, but every board write is serialized through one hub and one git ref. This run demonstrates ten machines, not one hundred.
- **Completed leases are left to TTL.** Only the ghost lease was explicitly released. The other eleven were left to expire on their 600-second TTL, and the final hub health payload still reports `live_leases: 11`. Nothing broke because no task was reclaimed, but a longer swarm should release on merge or completion, or use a shorter TTL.
- **The final display step read a stale tree.** The per-merge gate and the fresh clone were both correct; the last workflow step was not. It should check out `origin/main` before running the lane suite.
- **The hub was ephemeral.** SQLite plus a Cloudflare quick tunnel, with no uptime guarantee and a URL that dies with the session. This is adequate for a trial and not a production control plane.
- **The Postgres backend was not exercised.** `AGHUB_TEST_POSTGRES_DSN` was unset, so every result here is SQLite-only.
- **The lane work was pre-authored.** The run measures coordination, lease authority, merge-gate enforcement, and signing. It does not measure an LLM's coding quality, and it should not be cited as evidence of that.

## 10. Reproduce the accessible version

Ten cloud VMs from two GitHub accounts are not reproducible on demand. The coordination behavior is, locally:

```bash
./demo/run_swarm.sh                  # 10 local agents
SWARM_AGENTS=20 ./demo/run_swarm.sh  # 20 agents
SWARM_AGENTS=50 ./demo/run_swarm.sh  # 50 agents
./demo/run_hub.sh                    # hub plus web board, with a hub-only lease rejection
```

`run_swarm.sh` creates one clone per agent, starts a local hub, fires every claim at the same instant, then every submit, and merges all branches through the serial queue. `run_hub.sh` starts a hub and two clones and reproduces the ghost-lease case: a live hub lease rejecting a claim the git board knows nothing about. The cloud run is the evidence; the local demo is the part you can run yourself.

The as-run scripts and the `EVIDENCE` extracts from both runner logs are published in
[`bench/ten-machine/`](../bench/ten-machine/README.md). The private project and its lane payloads are
not included, so `agent.sh` is there to be inspected rather than run end to end.
