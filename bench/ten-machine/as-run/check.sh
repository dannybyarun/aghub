#!/usr/bin/env bash
# Optional wrapper around the merge gate. The board uses the cross-platform
# equivalent directly: node app/tests/run-tests.js && node app/tests/run-lanes.js
set -euo pipefail
node app/tests/run-tests.js
node app/tests/run-lanes.js
