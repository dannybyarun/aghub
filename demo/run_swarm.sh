#!/usr/bin/env bash
#
# A swarm on one board: many agents claim disjoint lanes at the same time.
# Run: ./demo/run_swarm.sh
#
# The interesting number is not "how many agents" in the abstract; it is how
# many concurrent board writers the git-backed board can absorb. This demo
# launches every claim and submit together, reports the writer-lease waits and
# board retries each phase needed, and merges the resulting branches through
# the serial queue.
#
# A hub coordinates the board write lease by default. Set SWARM_HUB=0 to see
# the optimistic, hub-less retry path for comparison. SWARM_LOCK_WAIT overrides
# how long a writer waits for the lease before falling back (default 300s here).
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGHUB="$ROOT/bin/aghub"
DEMO="${AGHUB_SWARM_DIR:-$(mktemp -d /tmp/aghub-swarm.XXXXXX)}"
COUNT="${SWARM_AGENTS:-10}"
HUB_TOKEN="swarm"
HUB_MODE="${SWARM_HUB:-1}"
export AGHUB_BOARD_LOCK_WAIT="${SWARM_LOCK_WAIT:-300}"

if ! [[ "$COUNT" =~ ^[0-9]+$ ]] || (( COUNT < 2 )); then
  echo "SWARM_AGENTS must be an integer >= 2" >&2
  exit 2
fi

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '\033[2m%s\033[0m\n' "$*"; }
cmd()  { printf '\033[2m$ %s\033[0m\n' "$*"; }
now_ms() { date +%s%3N; }
seconds() { awk -v ms="$1" 'BEGIN { printf "%.2fs", ms / 1000 }'; }

agent_name() { printf 'w%02d' "$1"; }
task_id() { printf 'T-%03d' "$1"; }
lane_name() { printf '%02d' "$1"; }

mkdir -p "$DEMO/logs"
note "swarm repo: $DEMO"
note "agents: $COUNT · every claim is launched at the same instant"

# ---------------------------------------------------------------- setup ---

git init -q --bare -b main "$DEMO/origin.git"
mkdir -p "$DEMO/seed"
cd "$DEMO/seed"
git init -q -b main
git config user.name seed
git config user.email seed@example.com
cat > README.md <<'EOF'
# swarm

One board, many agents, disjoint lanes.
EOF
git add -A
git commit -qm "seed: empty swarm repository"
git remote add origin "$DEMO/origin.git"
git push -qu origin main

for i in $(seq 1 "$COUNT"); do
  name="$(agent_name "$i")"
  git clone -q "$DEMO/origin.git" "$DEMO/$name"
  git -C "$DEMO/$name" config user.name "$name"
  git -C "$DEMO/$name" config user.email "$name@example.com"
done

# The first clone creates the board and its lanes. Joins are sequential so the
# setup itself is not the measurement.
( cd "$DEMO/$(agent_name 1)" && AGHUB_AGENT="$(agent_name 1)" \
  "$AGHUB" init --project swarm --ttl 120 >/dev/null )
for i in $(seq 2 "$COUNT"); do
  name="$(agent_name "$i")"
  ( cd "$DEMO/$name" && AGHUB_AGENT="$name" \
    "$AGHUB" init --project swarm >/dev/null )
done
for i in $(seq 1 "$COUNT"); do
  ( cd "$DEMO/$(agent_name 1)" && AGHUB_AGENT="$(agent_name 1)" \
    "$AGHUB" task add "Lane $(lane_name "$i")" \
      --paths "lanes/$(lane_name "$i")/**" >/dev/null )
done

# ---------------------------------------------------------------- the hub ---

HUB_URL=""
if [[ "$HUB_MODE" != "0" ]]; then
  PORT="$(python3 - <<'PY'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
)"
  HUB_URL="http://127.0.0.1:$PORT"
  "$AGHUB" hub serve --port "$PORT" --db "$DEMO/hub.db" --token "$HUB_TOKEN" \
    --rate-limit 0 >"$DEMO/hub.log" 2>&1 &
  HUB_PID=$!
  cleanup() {
    kill "$HUB_PID" 2>/dev/null || true
    wait "$HUB_PID" 2>/dev/null || true
  }
  trap cleanup EXIT
  trap 'exit 143' TERM INT
  python3 - "$HUB_URL" <<'PY'
import sys
import time
import urllib.request

url = sys.argv[1] + "/api/health"
for _ in range(100):
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            if response.status == 200:
                break
    except Exception:
        time.sleep(0.1)
else:
    raise SystemExit("hub did not start (see hub.log)")
PY
  for i in $(seq 1 "$COUNT"); do
    name="$(agent_name "$i")"
    ( cd "$DEMO/$name" && AGHUB_AGENT="$name" \
      "$AGHUB" hub connect "$HUB_URL" --token "$HUB_TOKEN" >/dev/null )
  done
  note "hub: $HUB_URL · writer lease on · rate limit off"
else
  note "hub: off · optimistic retries only"
fi

bold "1 · $COUNT agents claim $COUNT disjoint lanes at once"
if [[ -n "$HUB_URL" ]]; then
  note "one project-scoped writer lease at a time; the board ref stays the backstop"
else
  note "no central scheduler: the board ref is the lock, and retries are jittered"
fi
claim_start="$(now_ms)"
pids=()
for i in $(seq 1 "$COUNT"); do
  name="$(agent_name "$i")"
  task="$(task_id "$i")"
  (
    cd "$DEMO/$name"
    AGHUB_AGENT="$name" AGHUB_TRACE_BOARD=1 \
      "$AGHUB" claim "$task" >"$DEMO/logs/claim-$i.log" 2>&1
  ) &
  pids+=("$!")
done
claim_fail=0
for pid in "${pids[@]}"; do
  if ! wait "$pid"; then claim_fail=$((claim_fail + 1)); fi
done
claim_ms=$(( $(now_ms) - claim_start ))
if (( claim_fail > 0 )); then
  for i in $(seq 1 "$COUNT"); do
    if grep -q "could not update the shared board" "$DEMO/logs/claim-$i.log" 2>/dev/null; then
      printf 'claim %s failed:\n' "$i"
      sed 's/^/  /' "$DEMO/logs/claim-$i.log"
    fi
  done
  echo "swarm failed: $claim_fail claim(s) did not complete" >&2
  exit 1
fi
note "claims: $COUNT/$COUNT in $(seconds "$claim_ms")"
claim_locks="$({ grep -h 'board lock busy' "$DEMO"/logs/claim-*.log || true; } | wc -l)"
note "writer-lease waits during claims: $claim_locks"
note "board retries during claims: $({ grep -h 'board retry' "$DEMO"/logs/claim-*.log || true; } | wc -l)"

bold "2 · Every agent writes its own lane, then submits at once"
commit_pids=()
for i in $(seq 1 "$COUNT"); do
  name="$(agent_name "$i")"
  task="$(task_id "$i")"
  lane="$(lane_name "$i")"
  (
    cd "$DEMO/$name"
    git checkout -q "agent/$name/$task"
    mkdir -p "lanes/$lane"
    printf '%s wrote lane %s\n' "$name" "$lane" > "lanes/$lane/agent.txt"
    git add -A
    git commit -qm "$name: lane $lane"
  ) &
  commit_pids+=("$!")
done
for pid in "${commit_pids[@]}"; do wait "$pid"; done

rm -f "$DEMO/go-submit"
submit_start="$(now_ms)"
pids=()
for i in $(seq 1 "$COUNT"); do
  name="$(agent_name "$i")"
  task="$(task_id "$i")"
  (
    while [[ ! -e "$DEMO/go-submit" ]]; do sleep 0.01; done
    cd "$DEMO/$name"
    AGHUB_AGENT="$name" AGHUB_TRACE_BOARD=1 \
      "$AGHUB" submit "$task" >"$DEMO/logs/submit-$i.log" 2>&1
  ) &
  pids+=("$!")
done
touch "$DEMO/go-submit"
submit_fail=0
for pid in "${pids[@]}"; do
  if ! wait "$pid"; then submit_fail=$((submit_fail + 1)); fi
done
submit_ms=$(( $(now_ms) - submit_start ))
if (( submit_fail > 0 )); then
  for i in $(seq 1 "$COUNT"); do
    if grep -q "could not update the shared board" "$DEMO/logs/submit-$i.log" 2>/dev/null; then
      printf 'submit %s failed:\n' "$i"
      sed 's/^/  /' "$DEMO/logs/submit-$i.log"
    fi
  done
  echo "swarm failed: $submit_fail submit(s) did not complete" >&2
  exit 1
fi
note "submits: $COUNT/$COUNT in $(seconds "$submit_ms")"
submit_locks="$({ grep -h 'board lock busy' "$DEMO"/logs/submit-*.log || true; } | wc -l)"
note "writer-lease waits during submits: $submit_locks"
note "board retries during submits: $({ grep -h 'board retry' "$DEMO"/logs/submit-*.log || true; } | wc -l)"

bold "3 · One serial merge queue integrates all $COUNT lanes"
queue_start="$(now_ms)"
( cd "$DEMO/$(agent_name 1)" && AGHUB_AGENT="$(agent_name 1)" \
  "$AGHUB" queue run )
queue_ms=$(( $(now_ms) - queue_start ))
note "queue: $COUNT branches in $(seconds "$queue_ms")"

bold "4 · Prove every lane reached trunk"
git -C "$DEMO/$(agent_name 1)" fetch -q origin
merged="$(git -C "$DEMO/$(agent_name 1)" ls-tree -r --name-only origin/main lanes | wc -l)"
( cd "$DEMO/$(agent_name 1)" && AGHUB_AGENT="$(agent_name 1)" \
  "$AGHUB" status --json >"$DEMO/status.json" )

python3 - "$DEMO/status.json" "$COUNT" "$merged" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1]))
tasks = data["tasks"]
done = sum(1 for task in tasks if task.get("effective_status") == "done")
claimed = sum(1 for task in tasks if task.get("effective_status") == "claimed")
ready = sum(1 for task in tasks if task.get("effective_status") == "ready")
agents = len(data.get("agents") or [])
print(f"  agents          {agents}")
print(f"  tasks           {len(tasks)}")
print(f"  done            {done}")
print(f"  claimed         {claimed}")
print(f"  ready           {ready}")
print(f"  merged lanes    {sys.argv[3]}/{sys.argv[2]}")
PY

retries="$({ grep -h 'board retry' "$DEMO"/logs/*.log || true; } | wc -l)"
total_ms=$(( claim_ms + submit_ms + queue_ms ))
printf '\n  claims    %s\n' "$(seconds "$claim_ms")"
printf '  submits   %s\n' "$(seconds "$submit_ms")"
printf '  queue     %s\n' "$(seconds "$queue_ms")"
printf '  total     %s\n' "$(seconds "$total_ms")"
printf '  lockwaits %s\n' "$((claim_locks + submit_locks))"
printf '  retries   %s\n' "$retries"
printf '  board     %s\n' "$(git -C "$DEMO/$(agent_name 1)/.git/aghub-board" rev-parse --short HEAD)"
if [[ -n "$HUB_URL" ]]; then
  note "the writer lease turned contention into a short queue; retries are the fallback"
  note "run SWARM_HUB=0 to compare the optimistic path"
else
  note "retries are normal without a hub; the board rebuilt each rejected write"
fi
note "set AGHUB_TRACE_BOARD=1 to see individual waits and retries in logs"
note "re-run with SWARM_AGENTS=20 or SWARM_AGENTS=50 to see the curve"
note "repo: $DEMO"
