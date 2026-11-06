#!/usr/bin/env bash
#
# One machine in the real-project aghub swarm.
#
# This is the script that ran. The app/ tree it expects was a verbatim copy
# of a private project and is NOT included in this repository, so the script
# cannot run end to end as published. It is included as evidence: each machine
# owned one lane (a pair of files), and the merge gate ran the project's own
# tests plus every lane test. See ../README.md.

set -euo pipefail

: "${AGENT_NAME:?AGENT_NAME is required}"
: "${ROLE:?ROLE is required}"
: "${LANE:?LANE is required}"
: "${HUB_URL:?HUB_URL is required}"
: "${HUB_TOKEN:?HUB_TOKEN is required}"
: "${PROJECT:?PROJECT is required}"
: "${GHOST:=0}"

PY=python3
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export AGHUB_AGENT="$AGENT_NAME"
export AGHUB_BOARD_LOCK_WAIT="${AGHUB_BOARD_LOCK_WAIT:-600}"

# Use the module directly: a branch switch must not take the CLI away.
aghub() { "$PY" -m aghub "$@"; }

log() { printf '\n=== [%s] %s ===\n' "$AGENT_NAME" "$*"; }
evidence() { printf 'EVIDENCE|%s|%s|%s\n' "$AGENT_NAME" "$1" "$2"; }
fail() { printf '\nFAIL [%s]: %s\n' "$AGENT_NAME" "$*" >&2; exit 1; }

wait_boundary() {
  "$PY" - "$1" <<'PY'
import sys
import time

period = int(sys.argv[1])
now = time.time()
time.sleep((period - (now % period)) + 0.25)
PY
}

TASKS=(T-001 T-002 T-003 T-004 T-005 T-006 T-007 T-008 T-009 T-010 T-011)
ALL_TASKS="T-001,T-002,T-003,T-004,T-005,T-006,T-007,T-008,T-009,T-010,T-011"

git config user.name "$AGENT_NAME"
git config user.email "$AGENT_NAME@aghub.invalid"
evidence "machine" "$(hostname) $(uname -sr)"
evidence "machine_id" "$(cat /etc/machine-id 2>/dev/null || echo unknown)"
evidence "boot_id" "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown)"
evidence "runner" "${RUNNER_NAME:-local}"
evidence "account_repo" "${GITHUB_REPOSITORY:-local}"
evidence "hub" "$HUB_URL"

# ---------------------------------------------------------------- board ---

log "init or join board $PROJECT"
if [ "$ROLE" = "leader" ]; then
  aghub init --project "$PROJECT" --ttl 600 --check "node app/tests/run-tests.js && node app/tests/run-lanes.js"
else
  appeared=0
  for _ in $(seq 1 240); do
    if git ls-remote --exit-code origin refs/heads/aghub/board >/dev/null 2>&1; then
      appeared=1
      break
    fi
    sleep 2
  done
  [ "$appeared" = 1 ] || fail "board branch aghub/board never appeared on origin"

  joined=0
  for _ in $(seq 1 60); do
    if aghub init --project "$PROJECT" >/tmp/join.log 2>&1; then
      joined=1
      break
    fi
    cat /tmp/join.log || true
    sleep 3
  done
  [ "$joined" = 1 ] || fail "could not join the board: $(cat /tmp/join.log 2>/dev/null || true)"
fi

log "connect to the hub on the coordinator machine"
aghub hub connect "$HUB_URL" --token "$HUB_TOKEN"
aghub hub status || true
evidence "joined" "board=$PROJECT"

# ---------------------------------------------------------------- lanes ---

if [ "$ROLE" = "leader" ]; then
  log "leader imports all real-project lanes in one board write"
  aghub task import swarm/tasks.yaml
else
  log "follower waits for all eleven lanes to reach the hub"
  "$PY" - "$HUB_URL" "$PROJECT" "$HUB_TOKEN" <<'PY'
import sys
import time

from aghub import hubclient

url, project, token = sys.argv[1:4]
wanted = {f"T-{n:03d}" for n in range(1, 12)}
deadline = time.time() + 600
while time.time() < deadline:
    try:
        snapshot = hubclient.board(url, project, token=token)["snapshot"]
    except Exception:
        time.sleep(2)
        continue
    have = {task["id"] for task in snapshot.get("tasks", [])}
    if wanted <= have:
        print("all lanes visible on the hub:", ",".join(sorted(have)))
        raise SystemExit(0)
    time.sleep(2)
raise SystemExit("lanes never appeared on the hub")
PY
  aghub sync
fi

evidence "tasks" "$(aghub status --json | "$PY" -c 'import json,sys; print(",".join(t["id"] for t in json.load(sys.stdin)["tasks"]))')"

# ------------------------------------------------- hub-only authority ---

if [ "$GHOST" = "1" ]; then
  log "create a hub-only lease on a real lane path"
  "$PY" - "$HUB_URL" "$PROJECT" "$HUB_TOKEN" <<'PY'
import sys

from aghub import hubclient

url, project, token = sys.argv[1:4]
lease = hubclient.claim(
    url, project, "T-999", "ghost-b", ["app/src/22_MoneySplit.gs"], 300, token=token
)
print(f"EVIDENCE|ghost|lease_created|fencing={lease['fencing']} paths=app/src/22_MoneySplit.gs")
PY
  sleep 25
  "$PY" - "$HUB_URL" "$PROJECT" "$HUB_TOKEN" <<'PY'
import sys

from aghub import hubclient

url, project, token = sys.argv[1:4]
hubclient.release(url, project, "T-999", "ghost-b", token=token)
print("EVIDENCE|ghost|lease_released|T-999")
PY
elif [ "$ROLE" = "leader" ]; then
  log "prove the hub-only lease blocks a claim even though git has no conflict"
  "$PY" - "$HUB_URL" "$PROJECT" "$HUB_TOKEN" <<'PY'
import sys
import time

from aghub import hubclient

url, project, token = sys.argv[1:4]
deadline = time.time() + 600
while time.time() < deadline:
    try:
        leases = hubclient.board(url, project, token=token)["leases"]
    except Exception:
        time.sleep(2)
        continue
    if any(lease["task"] == "T-999" for lease in leases):
        print("ghost lease from the other account is visible here")
        raise SystemExit(0)
    time.sleep(2)
raise SystemExit("the ghost lease never appeared on the hub")
PY

  set +e
  ghost_out="$(aghub claim T-001 2>&1)"
  ghost_rc=$?
  set -e
  printf '%s\n' "$ghost_out"
  [ "$ghost_rc" -ne 0 ] || fail "T-001 claim succeeded despite a hub-only lease"
  printf '%s\n' "$ghost_out" | grep -qiE 'hub rejected|conflict|lease' \
    || fail "unexpected error while a hub-only lease was active"
  evidence "hub_only_lease" "blocked claim T-001 exit=$ghost_rc"

  "$PY" - "$HUB_URL" "$PROJECT" "$HUB_TOKEN" <<'PY'
import sys
import time

from aghub import hubclient

url, project, token = sys.argv[1:4]
deadline = time.time() + 600
while time.time() < deadline:
    try:
        leases = hubclient.board(url, project, token=token)["leases"]
    except Exception:
        time.sleep(2)
        continue
    if not any(lease["task"] == "T-999" for lease in leases):
        print("hub-only lease released by the other account")
        raise SystemExit(0)
    time.sleep(1)
raise SystemExit("the ghost lease was never released")
PY
fi

# ---------------------------------------------------------------- race ---

log "ten machines race for T-011 at one wall-clock boundary"
wait_boundary 10
set +e
race_out="$(aghub claim T-011 2>&1)"
race_rc=$?
set -e
printf '%s\n' "$race_out"
if [ "$race_rc" -eq 0 ]; then
  won=1
  evidence "race" "won T-011"
else
  won=0
  printf '%s\n' "$race_out" | grep -qiE 'hub rejected|conflict|claimed|already|lease' \
    || fail "race was lost for an unexpected reason"
  evidence "race" "lost T-011 exit=$race_rc"
fi

# ---------------------------------------------------------------- work ---

commit_and_submit() {
  local task="$1" message="$2"
  git add -A
  git commit -qm "$message"
  aghub handoff "$task" --next "$message"
  aghub submit "$task"
  evidence "submitted" "$task"
}

work_shared() {
  log "won the race: write the shared module and its test"
  git checkout -q "agent/$AGENT_NAME/T-011"
  mkdir -p app/tests/lanes
  cat >app/src/22_SharedWinner.gs <<EOF
function sharedWinner_() {
  return '$AGENT_NAME';
}
EOF
  cat >app/tests/lanes/shared-winner.test.js <<EOF
const { app, test, eq } = require('../lane-harness');

test('shared winner is $AGENT_NAME', () => {
  eq(app.sharedWinner_(), '$AGENT_NAME');
});
EOF
  commit_and_submit T-011 "shared: $AGENT_NAME won the T-011 race"
  git fetch -q origin
  git checkout -q -B main origin/main
}

work_lane() {
  local task="$1"
  local branch="agent/$AGENT_NAME/$task"

  log "claim and work $task"
  aghub claim "$task"
  git checkout -q "$branch"
  CP_SRC="swarm/lanes/$task"
  [ -d "$CP_SRC" ] || fail "no lane content for $task"
  mkdir -p app/src app/tests/lanes
  cp -R "$CP_SRC"/src/. app/src/
  cp -R "$CP_SRC"/tests/. app/tests/lanes/
  commit_and_submit "$task" "lane: $AGENT_NAME completed $task"
  git fetch -q origin
  git checkout -q -B main origin/main
}

if [ "$won" = 1 ]; then
  work_shared
fi
work_lane "$LANE"

# ----------------------------------------------------------- integrate ---

if [ "$ROLE" = "leader" ]; then
  log "serial merge queue: merge every submitted lane"
  complete=0
  for _ in $(seq 1 120); do
    aghub queue run >/tmp/queue-run.log 2>&1 || true
    aghub sync >/tmp/sync.log 2>&1 || true
    aghub status --json >/tmp/status.json 2>/dev/null || true
    if [ -s /tmp/status.json ] && "$PY" - "$ALL_TASKS" <<'PY'
import json
import sys

wanted = set(sys.argv[1].split(","))
state = json.load(open("/tmp/status.json"))
done = {task["id"] for task in state["tasks"] if task.get("status") == "done"}
missing = sorted(wanted - done)
if missing:
    print("waiting for", missing)
    raise SystemExit(1)
print("all eleven tasks are done")
PY
    then
      complete=1
      break
    fi
    sleep 10
  done
  tail -n 60 /tmp/queue-run.log 2>/dev/null || true
  [ "$complete" = 1 ] || fail "the queue never completed every lane"
  evidence "queue" "merged $ALL_TASKS"
else
  log "wait for the integrator to merge every lane"
  complete=0
  for _ in $(seq 1 150); do
    aghub sync >/dev/null 2>&1 || true
    aghub status --json >/tmp/status.json 2>/dev/null || true
    if [ -s /tmp/status.json ] && "$PY" - "$ALL_TASKS" <<'PY'
import json
import sys

wanted = set(sys.argv[1].split(","))
state = json.load(open("/tmp/status.json"))
done = {task["id"] for task in state["tasks"] if task.get("status") == "done"}
missing = sorted(wanted - done)
if missing:
    print("waiting for", missing)
    raise SystemExit(1)
print("every lane is done")
PY
    then
      complete=1
      break
    fi
    sleep 10
  done
  [ "$complete" = 1 ] || fail "never saw every lane done"
fi

# ------------------------------------------------------------- verify ---

log "verify trunk and the signed board on this machine"
aghub sync
git fetch -q origin main
for file in \
  app/src/22_MoneySplit.gs \
  app/src/22_BsSpan.gs \
  app/src/22_Aging.gs \
  app/src/22_StockHealth.gs \
  app/src/22_QualityVerdict.gs \
  app/src/22_PaymentWaterfall.gs \
  app/src/22_Rollup.gs \
  app/src/22_BranchScope.gs \
  app/src/22_RatePick.gs \
  app/src/22_CsvMerge.gs \
  app/src/22_SharedWinner.gs; do
  git cat-file -e "origin/main:$file" || fail "trunk is missing $file"
done
evidence "trunk" "all 10 lane modules + shared winner present on origin/main"

aghub verify
evidence "verify" "signed board verified"

aghub status --json >/tmp/final-status.json
"$PY" - <<'PY'
import json

state = json.load(open("/tmp/final-status.json"))
for task in state["tasks"]:
    print(f"  {task['id']:<6} {task['status']:<8} {task.get('owner')} {task.get('title')}")
print("  queue:", [(entry.get("task"), entry.get("status")) for entry in state["queue"]])
print("  agents:", sorted(agent.get("id", "?") for agent in state["agents"]))
PY

evidence "done" "real-project multi-machine trial complete"
