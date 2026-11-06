# Ten-machine as-run harness

This directory contains the scripts and evidence from the 2026-11-06 ten-machine run described in
[`docs/REAL-PROJECT-10-MACHINE.md`](../../docs/REAL-PROJECT-10-MACHINE.md).

It is published so the run can be inspected, not because it is a turnkey benchmark. Read the
scope note before drawing conclusions.

## What is here

| Path | What it is |
|---|---|
| `as-run/agent.sh` | The script each of the ten machines ran: join, ghost lease, ten-way race, lane work, queue, verify. |
| `as-run/tasks.yaml` | The eleven lane definitions imported by the leader. |
| `as-run/check.sh`, `as-run/check.py` | The merge checks used in earlier trials. |
| `as-run/account-a.workflow.yml` | The coordinator account's five-job workflow. |
| `as-run/account-b.workflow.yml` | The second account's five-job workflow (checks out account A's repo through a token). |
| `as-run/collect.py`, `machines.py`, `audit*.py` | Board, machine-identity, and hub-audit collectors used after the run. |
| `evidence/runA-evidence.txt`, `runB-evidence.txt` | Every `EVIDENCE|...` line from the two runner logs. |
| `evidence/audit-summary.txt` | Counts and fencing range from the 225 hub audit events. |

## What is not here

The run used a **private project**, and none of it is published:

- The 414-file project copied into `app/` for the run.
- The eleven lane payloads and lane tests under `swarm/lanes/` (they implement that project's features).
- Raw runner logs, workflow JSON, the hub SQLite database, hub tokens, and the Cloudflare tunnel URL.

`agent.sh` still refers to that project's paths (`app/src/22_*.gs`, `app/tests/lanes/*.js`). As
published it **cannot run end to end** without a project tree and lanes of the same shape. That is
deliberate: publishing the harness is not the same as publishing the private code, and a rewritten
"generic" harness would no longer be the thing that ran.

For a runnable swarm with this repository, use [`demo/run_swarm.sh`](../../demo/run_swarm.sh). It
exercises the same claim/lease/queue path locally at 10, 20, or 50 agents.

## Topology

| Piece | Configuration |
|---|---|
| Hub | `aghub hub` 0.10.0, `127.0.0.1:8788`, fresh SQLite, project `swarm-real` |
| Ingress | Cloudflare quick tunnel (ephemeral, session-only) |
| Canonical repo | Private, disposable; `main` plus the `aghub/board` ref |
| Second account | Private runner-only repo; checks out the canonical repo through a cross-repo token |
| Machines | 10 GitHub-hosted runners, five per account, `ubuntu-22.04` / `ubuntu-24.04` |
| Merge gate | `node app/tests/run-tests.js && node app/tests/run-lanes.js` |

## What the run did

1. The leader initialized the board, imported `tasks.yaml`, and connected to the hub.
2. One machine from the second account created a hub-only lease on a real lane path (`T-999`, a task
   that exists nowhere in git). The leader's claim on `T-001` was rejected by the hub.
3. All ten machines waited for one wall-clock boundary and claimed `T-011` at the same instant.
   Exactly one won; the other nine received an authoritative lease/claim error.
4. Each machine claimed its lane, copied its lane payload into `app/`, committed, wrote a handoff,
   and submitted to the queue.
5. The leader drained the serial queue. All eleven submissions merged; the project's own 41-test
   suite plus 79 lane tests passed on a fresh clone.
6. Every machine synced and ran `aghub verify` against the signed board.

## Publication changes

Only these changes were made:

- Private repository names in the workflows were replaced with `YOUR-ORG/...` placeholders.
- The tunnel URL, account names, and token literals were removed from the evidence extracts and
  moved to `AGHUB_HUB_URL`, `AGHUB_TOKEN`, and `AGHUB_PROJECT` in the collector scripts.
- `agent.sh` gained a header stating that the private `app/` tree is absent.

The `EVIDENCE` lines are otherwise verbatim. Nothing was added to make the numbers look better.

## Integrity notes

These are also in the public write-up:

- Ten VMs reported ten distinct runner IDs and `boot_id` values, but only three distinct
  `/etc/machine-id` values and three hostnames. Do not use `machine-id` as an identity on
  GitHub-hosted runners.
- The serial board writer is the ceiling: 67 lock cycles for 11 tasks over about four minutes.
  This demonstrates ten machines, not one hundred.
- Completed leases were left to TTL; only the ghost lease was explicitly released.
- The Postgres backend was not exercised.
- The lane work was pre-authored. The run measures coordination, lease authority, merge-gate
  enforcement, and signing — not an LLM's coding quality.
