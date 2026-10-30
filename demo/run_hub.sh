#!/usr/bin/env bash
#
# The live layer: one hub, two clones, authoritative leases, and a web board.
# Run: ./demo/run_hub.sh [--stop]
#
# By default the hub is left running so the printed URL stays reachable.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGHUB="$ROOT/bin/aghub"
DEMO="${AGHUB_HUB_DEMO_DIR:-$(mktemp -d /tmp/aghub-hub-demo.XXXXXX)}"
TOKEN="demo"
PROJECT="acme-store"
STOP_AT_END=0
[[ "${1:-}" == "--stop" ]] && STOP_AT_END=1

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '\033[2m%s\033[0m\n' "$*"; }
cmd()  { printf '\033[2m$ %s\033[0m\n' "$*"; }

fe() { cmd "aghub $*"; ( cd "$DEMO/fe" && AGHUB_AGENT=fe-1 "$AGHUB" "$@" ); }
be() { cmd "aghub $*"; ( cd "$DEMO/be" && AGHUB_AGENT=be-1 "$AGHUB" "$@" ); }

expect_fail() {
  local label="$1" fn="$2"
  shift 2
  cmd "aghub $*"
  if "$fn" "$@" >"$DEMO/expected-failure.log" 2>&1; then
    printf '\033[31mUNEXPECTED SUCCESS: %s\033[0m\n' "$label"
    exit 1
  fi
  printf '\033[31m  ✗ %s\033[0m\n' "$label"
  sed 's/^/  /' "$DEMO/expected-failure.log"
}

# ---------------------------------------------------------------- setup ---

mkdir -p "$DEMO"
git init -q --bare -b main "$DEMO/origin.git"
mkdir -p "$DEMO/seed/contracts"
cd "$DEMO/seed"
git init -q -b main
git config user.name seed
git config user.email seed@example.com
cat > contracts/api.yaml <<'YAML'
openapi: 3.0.0
info:
  title: Items API
  version: "1"
paths:
  /items:
    get:
      summary: List items
  /items/{id}:
    get:
      summary: Get one item
YAML
git add -A
git commit -qm "seed: contract"
git remote add origin "$DEMO/origin.git"
git push -qu origin main

for name in fe be; do
  git clone -q "$DEMO/origin.git" "$DEMO/$name"
  git -C "$DEMO/$name" config user.name "$name"
  git -C "$DEMO/$name" config user.email "$name@example.com"
done

note "demo repo: $DEMO"

# Two agents define lanes before the hub is involved. Git remains the record.
fe init --project "$PROJECT" --ttl 60 >/dev/null
fe contract freeze api 1 --from contracts/api.yaml >/dev/null
fe task add "Items API" --paths 'src/api/**' --contract api@1 >/dev/null
fe task add "Items UI" --paths 'src/web/**' --contract api@1 >/dev/null
fe task add "Seed data" --paths 'src/db/**' --contract api@1 >/dev/null
be init --project "$PROJECT" >/dev/null

# --------------------------------------------------------------- the hub ---

PORT="$(python3 - <<'PY'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
)"
URL="http://127.0.0.1:$PORT"

bold "1 · Start one hub; it owns leases and presence, not the durable log"
"$AGHUB" hub serve --port "$PORT" --db "$DEMO/hub.db" --token "$TOKEN" \
  >"$DEMO/hub.log" 2>&1 &
HUB_PID=$!

cleanup() {
  local status=$?
  if [[ $status -ne 0 || "$STOP_AT_END" == "1" ]]; then
    kill "$HUB_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

python3 - "$URL" <<'PY'
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
note "hub listening on $URL (db $DEMO/hub.db, token $TOKEN)"

bold "2 · Both clones point at the same hub and publish live presence"
fe hub connect "$URL" --token "$TOKEN"
be hub connect "$URL" --token "$TOKEN"
fe hub status

bold "3 · A claim becomes a fencing lease before it becomes a git write"
fe claim T-001
note "the lease has a fencing token; every later grant increments it"

bold "4 · The hub is authoritative, even for state git has never seen"
PYTHONPATH="$ROOT/src" python3 - "$URL" <<'PY'
import sys
from aghub import hubclient

# Fault injection: a live lease for a task that does not exist on the board.
result = hubclient.claim(
    sys.argv[1], "acme-store", "T-999", "ghost", ["src/db/**"], 600, token="demo"
)
print(f"  ghost lease: T-999 by ghost, fencing={result['fencing']}, paths=src/db/**")
PY
expect_fail "a hub-only lease blocks an overlapping claim" be claim T-003
note "the git board had no conflict — the hub was the authority that said no"

bold "5 · Live view: who is here, what is leased, what the web board sees"
be status
PYTHONPATH="$ROOT/src" python3 - "$URL" <<'PY'
import sys
from aghub import hubclient

data = hubclient.board(sys.argv[1], "acme-store", token="demo")
print("  leases")
for lease in data["leases"]:
    print(
        f"    {lease['task']:<7} {lease['agent']:<7} "
        f"fencing={lease['fencing']:<3} paths={','.join(lease['paths'])}"
    )
print(
    f"  board: {len(data['snapshot'].get('tasks', []))} tasks, "
    f"{len(data['agents'])} agents, updated {data['updated_at']}"
)
PY

bold "6 · Open the board"
note "git is the durable log · the hub is the live authority · live stream with polling fallback"
printf '\n  \033[36m%s/?token=%s\033[0m\n' "$URL" "$TOKEN"

if [[ "$STOP_AT_END" == "1" ]]; then
  kill "$HUB_PID"
  wait "$HUB_PID" 2>/dev/null || true
  note "hub stopped"
else
  printf '\n'
  note "hub is still running: pid $HUB_PID"
  note "stop it with: kill $HUB_PID"
fi
