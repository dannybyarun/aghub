#!/usr/bin/env bash
#
# One agent: clone, join the board, claim one lane, write it, submit it.
# Every agent runs this in its own container and touches only its own paths.
#
set -euo pipefail

: "${AGENT_NAME:?AGENT_NAME is required}"
: "${TASK_ID:?TASK_ID is required}"
: "${LANE:?LANE is required}"

PROJECT="${AGHUB_PROJECT:-demo}"
HUB_URL="${HUB_URL:-http://hub:8788}"
HUB_TOKEN="${AGHUB_HUB_TOKEN:-demo}"

export HOME="${HOME:-/root}"
export AGHUB_AGENT="$AGENT_NAME"

git config --global user.name "$AGENT_NAME"
git config --global user.email "$AGENT_NAME@aghub.local"
git config --global init.defaultBranch main
git config --global --add safe.directory '*'

WORK="/work/$AGENT_NAME"
mkdir -p /work
rm -rf "$WORK"
git clone -q /repos/origin.git "$WORK"
cd "$WORK"

python -m aghub init --project "$PROJECT" >/dev/null
python -m aghub hub connect "$HUB_URL" --token "$HUB_TOKEN" >/dev/null

echo "[$AGENT_NAME] claiming $TASK_ID"
python -m aghub claim "$TASK_ID"

git checkout -q "agent/$AGENT_NAME/$TASK_ID"
mkdir -p "lanes/$LANE"
printf '%s wrote lane %s\n' "$AGENT_NAME" "$LANE" > "lanes/$LANE/agent.txt"
git add -A
git commit -qm "$AGENT_NAME: lane $LANE"

echo "[$AGENT_NAME] submitting $TASK_ID"
python -m aghub submit "$TASK_ID"
echo "[$AGENT_NAME] done"
