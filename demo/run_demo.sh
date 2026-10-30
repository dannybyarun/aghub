#!/usr/bin/env bash
#
# Two agents, two clones, one repository. Neither agent messages the other.
# Run: ./demo/run_demo.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGHUB="$ROOT/bin/aghub"
DEMO="${AGHUB_DEMO_DIR:-$(mktemp -d /tmp/aghub-demo.XXXXXX)}"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '\033[2m%s\033[0m\n' "$*"; }
cmd()  { printf '\033[2m$ %s\033[0m\n' "$*"; }

fe() { cmd "aghub $*"; ( cd "$DEMO/fe" && AGHUB_AGENT=fe-1 "$AGHUB" "$@" ); }
be() { cmd "aghub $*"; ( cd "$DEMO/be" && AGHUB_AGENT=be-1 "$AGHUB" "$@" ); }
ci() { cmd "aghub $*"; ( cd "$DEMO/ci" && AGHUB_AGENT=ci "$AGHUB" "$@" ); }
git_at() { local name="$1"; shift; git -C "$DEMO/$name" "$@"; }
eval_id_at() {
  ( cd "$DEMO/$1" && AGHUB_AGENT="$2" "$AGHUB" eval list --agent "$2" | tail -1 | awk '{print $1}' )
}

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
mkdir -p "$DEMO/seed"
cd "$DEMO/seed"
git init -q -b main
git config user.name seed
git config user.email seed@example.com
mkdir -p contracts

cat > contracts/api.yaml <<'YAML'
openapi: 3.0.0
info:
  title: Items API
  version: "1"
paths:
  /items:
    get:
      summary: List items
    post:
      summary: Create an item
  /items/{id}:
    get:
      summary: Get one item
YAML

cat > check.py <<'PY'
"""Contract check run inside every candidate merge.

Both lanes must agree with the frozen contract: every route in api.yaml has to
appear in the code, and no lane may invent a route the contract does not have.
"""
import pathlib
import re
import sys

root = pathlib.Path(__file__).parent
contract = (root / "contracts" / "api.yaml").read_text()
routes = re.findall(r"^\s{2}(/[A-Za-z0-9_{}/-]+):\s*$", contract, re.M)
sources = [
    path
    for folder in ("src/api", "src/web")
    if (root / folder).exists()
    for path in (root / folder).rglob("*")
    if path.is_file()
]
text = "\n".join(path.read_text(errors="ignore") for path in sources)
missing = [route for route in routes if route not in text]
extra = sorted({match for match in re.findall(r"['\"](/[a-z0-9_/{}-]+)['\"]", text) if match not in routes})
if missing:
    print("missing routes:", missing)
    sys.exit(1)
if extra:
    print("routes not in the frozen contract:", extra)
    sys.exit(1)
print("contract ok:", len(routes), "routes")
PY

cat > .gitignore <<'EOF'
generated/
EOF

git add -A
git commit -qm "seed: frozen contract and merge check"
git remote add origin "$DEMO/origin.git"
git push -qu origin main

for name in fe be ci; do
  git clone -q "$DEMO/origin.git" "$DEMO/$name"
  git -C "$DEMO/$name" config user.name "$name"
  git -C "$DEMO/$name" config user.email "$name@example.com"
done

note "demo repo: $DEMO"

# -------------------------------------------------------------- the story ---

bold "1 · One agent writes the interface down, then stops talking"
note "api@1 is frozen before either side starts. Frontend and backend depend"
note "on the contract, not on each other."
fe init --project acme-store --ttl 60 --check "python3 check.py"
fe contract freeze api 1 --from contracts/api.yaml
fe codegen api 1 --out generated
fe codegen api 1 --out generated/sql --target sql
fe task add "Items API" --paths 'src/api/**' --contract api@1
fe task add "Items UI" --paths 'src/web/**' --contract api@1
note "both lanes can generate from api@1; neither has to ask the other what it means"

bold "2 · A second agent joins from another clone"
be init --project acme-store
be status

bold "2b · The same board over MCP stdio"
note "an editor agent can inspect and claim work through the MCP server"
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"aghub_next","arguments":{}}}' \
  | ( cd "$DEMO/fe" && AGHUB_AGENT=fe-1 "$AGHUB" mcp serve --agent fe-1 )

bold "3 · Each takes its own lane without asking the other"
fe next
fe claim T-002
be claim T-001
expect_fail "a live claim cannot be taken" be claim T-002

bold "4 · Parallel work, documented by handoffs instead of messages"
git_at fe checkout -q agent/fe-1/T-002
mkdir -p "$DEMO/fe/src/web"
cat > "$DEMO/fe/src/web/api.ts" <<'TS'
// Frontend lane, written against the frozen contract api@1.
export const ROUTES = ["/items", "/items/{id}"];
TS
git_at fe add -A
git_at fe commit -qm "web: items client against api@1"
fe handoff T-002 --next "UI consumes /items and /items/{id}; nothing blocks backend"
fe submit T-002

git_at be checkout -q agent/be-1/T-001
mkdir -p "$DEMO/be/src/api"
cat > "$DEMO/be/src/api/items.py" <<'PY'
# Backend lane, written against the frozen contract api@1.
ROUTES = ["/items", "/items/{id}"]
PY
git_at be add -A
git_at be commit -qm "api: items handlers against api@1"
be handoff T-001 --next "API implements api@1; UI can merge independently"
be submit T-001

bold "5 · The serial merge queue is the only writer to trunk"
ci init --project acme-store
ci queue run
git_at ci fetch -q origin
git_at ci log --oneline -3 origin/main
ci registry profile --model demo-checker --skills python,git --note "integration lane"
git_at ci reset --hard -q origin/main
ci eval run contract --command "python3 check.py"
ci eval ttl
first_eval="$(eval_id_at ci ci)"
ci eval verify "$first_eval"
ci eval rerun "$first_eval"
ci eval diff "$first_eval" "$(eval_id_at ci ci)"
ci ci workflow --out .github/workflows/aghub.yml
ci ci workflow --github-review --out .github/workflows/aghub-review.yml
ci ci status --json
ci verify
note "the eval ran locally; the board stores a signed scorecard, not the run"

bold "6 · The board, not a conversation, is the state"
ci status

bold "7 · A dead agent releases its lease — no messages, no cleanup call"
be task add "Seed data" --paths 'src/db/**' --contract api@1 --eval contract
be eval run contract --command "python3 check.py"
be claim T-003
be debug expire be-1
ci status
expect_fail "an eval gate blocks the steal" fe claim T-003 --steal
note "the gate is per agent; fe must run the suite before taking the task"
fe eval run contract --command "python3 check.py"
fe claim T-003 --steal
fe release T-003

bold "8 · Claims are advisory until the merge queue checks them"
be task add "Sneaky edit" --paths 'src/api/**' --contract api@1
be claim T-004
git_at be checkout -q agent/be-1/T-004
mkdir -p "$DEMO/be/src/web"
echo '// this path belongs to the UI claim' > "$DEMO/be/src/web/evil.ts"
git_at be add -A
git_at be commit -qm "api: accidental cross-lane edit"
be submit T-004
ci queue run
note "the rejection opened T-005 with the changed files attached"
ci task list

bold "9 · The contract check catches semantic drift"
be release T-004
be task add "Health endpoint" --paths 'src/api/**' --contract api@1
be claim T-006
git_at be checkout -q agent/be-1/T-006
cat > "$DEMO/be/src/api/items.py" <<'PY'
# /health was added without touching the frozen contract.
ROUTES = ["/items", "/items/{id}", "/health"]
PY
git_at be add -A
git_at be commit -qm "api: add /health without changing api@1"
be submit T-006
ci queue run
note "T-007 carries the failing case: command, exit code, output, changed files"
ci task show T-007

bold "10 · A human review is a signed gate, not a message"
be task add "Billing deploy" --paths 'src/api/**' --requires-approval
be claim T-008
git_at be checkout -q agent/be-1/T-008
cat > "$DEMO/be/src/api/billing.py" <<'PY'
# Gated change: requires an approval from a different identity.
ROUTES = ["/items", "/items/{id}"]
PY
git_at be add -A
git_at be commit -qm "api: billing deploy behind an approval gate"
git_at be push -qu origin agent/be-1/T-008
expect_fail "the task owner cannot self-approve" be review T-008 --approve --note "self approval"
expect_fail "a task without approval cannot be submitted" be submit T-008
fe review T-008 --request-changes --note "add a rollback check"
fe inbox
expect_fail "requested changes still block submit" be submit T-008
fe review T-008 --approve --note "reviewed the billing rollout and rollback"
fe review T-008 --json
fe github sync --task T-008 --repo acme/store --dry-run
cat > "$DEMO/be/src/api/billing.py" <<'PY'
# Gated change: the update after approval must be re-reviewed.
ROUTES = ["/items", "/items/{id}"]
PY
git_at be add -A
git_at be commit -qm "api: adjust billing rollout after review"
git_at be push -qu origin agent/be-1/T-008
expect_fail "an approval for the old head is stale" be submit T-008
fe review T-008 --approve --note "re-reviewed the current head"
be submit T-008
ci queue run
ci verify

bold "11 · ACLs close the board to unknown writers"
ci acl init
ci acl set fe-1 writer
ci acl set be-1 writer
ci acl set reviewer admin
ci acl list
ci acl remove be-1
expect_fail "a reader cannot add tasks" be task add "Sneaky task" --paths 'src/api/**'
ci acl set be-1 writer

bold "12 · Final board"
ci status
note "Code reaches main only through the queue; the board lives on aghub/board."
note "History is hash-chained, records are signed, and roles are explicit."
note "Re-run any time. Demo repo: $DEMO"
