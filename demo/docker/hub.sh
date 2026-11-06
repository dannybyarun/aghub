#!/usr/bin/env bash
#
# The live layer. Owns leases, presence, and the web board; never the history.
#
set -euo pipefail

mkdir -p /hubdata

exec python -m aghub hub serve \
  --host 0.0.0.0 \
  --port 8788 \
  --db /hubdata/hub.db \
  --token "${AGHUB_HUB_TOKEN:-demo}" \
  --rate-limit 0
