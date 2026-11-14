#!/usr/bin/env bash
# Case opaque-exit: fails without saying why.
set -euo pipefail
echo "running checks"
exit 3
