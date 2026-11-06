#!/usr/bin/env bash
#
# One-shot setup: create the shared bare origin, seed the trunk, and create a
# board with three disjoint lanes. Runs once before the agents start.
#
set -euo pipefail

ORIGIN=/repos/origin.git
PROJECT="${AGHUB_PROJECT:-demo}"

export HOME="${HOME:-/root}"
export AGHUB_AGENT=seed

git config --global user.name "aghub seed"
git config --global user.email "seed@aghub.local"
git config --global init.defaultBranch main
git config --global --add safe.directory '*'

mkdir -p /repos
if [ ! -d "$ORIGIN" ]; then
  git init -q --bare -b main "$ORIGIN"
fi

WORK="$(mktemp -d)"
git clone -q "$ORIGIN" "$WORK/seed"
cd "$WORK/seed"

if ! git rev-parse --verify -q refs/heads/main >/dev/null; then
  cat > README.md <<'EOF'
# aghub docker demo

Three agents, one board, one serial merge queue.
EOF
  git add -A
  git commit -qm "seed: repository"
  git push -qu origin main
fi

# Adopt the board if the remote already has one, otherwise create it.
python -m aghub init --project "$PROJECT" --ttl 120 --check "test -f README.md" >/dev/null

if python -m aghub status --json | python -c "import json, sys; data = json.load(sys.stdin); sys.exit(0 if any(t.get('id') == 'T-001' for t in data.get('tasks', [])) else 1)"; then
  echo "seed: board already has lanes"
else
  echo "seed: creating three lanes"
  python -m aghub task add "Lane A" --paths 'lanes/a/**' >/dev/null
  python -m aghub task add "Lane B" --paths 'lanes/b/**' >/dev/null
  python -m aghub task add "Lane C" --paths 'lanes/c/**' >/dev/null
fi

python -m aghub status
