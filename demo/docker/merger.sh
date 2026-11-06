#!/usr/bin/env bash
#
# The serial merge queue. One authority integrates all agent branches into
# trunk, running the board's configured check on each merge.
#
set -euo pipefail

PROJECT="${AGHUB_PROJECT:-demo}"
HUB_URL="${HUB_URL:-http://hub:8788}"
HUB_TOKEN="${AGHUB_HUB_TOKEN:-demo}"

export HOME="${HOME:-/root}"
export AGHUB_AGENT=merger

git config --global user.name "merger"
git config --global user.email "merger@aghub.local"
git config --global init.defaultBranch main
git config --global --add safe.directory '*'

WORK=/work/merger
mkdir -p /work
rm -rf "$WORK"
git clone -q /repos/origin.git "$WORK"
cd "$WORK"

python -m aghub init --project "$PROJECT" >/dev/null
python -m aghub hub connect "$HUB_URL" --token "$HUB_TOKEN" >/dev/null

echo "[merger] draining the queue"
python -m aghub queue run

git fetch -q origin
echo "[merger] lanes on origin/main:"
git ls-tree -r --name-only origin/main lanes

echo "[merger] board"
python -m aghub status
