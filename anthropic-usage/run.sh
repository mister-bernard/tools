#!/bin/bash
# Run anthropic-usage scraper with Xvfb cleanup
set -euo pipefail

LOG="${USAGE_LOG:-/tmp/anthropic-usage.log}"
export DISPLAY="${DISPLAY:-:99}"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG"; }
log "Running usage check"

cd "$(dirname "$0")"

# Snapshot Xvfb PIDs before run to clean up playwright orphans after
XVFB_BEFORE=$(pgrep -f 'Xvfb -br' 2>/dev/null | sort || true)

cleanup() {
  pkill -P $NODE_PID 2>/dev/null || true
  wait $NODE_PID 2>/dev/null || true

  # Kill orphan Xvfb processes spawned by playwright during this run
  XVFB_AFTER=$(pgrep -f 'Xvfb -br' 2>/dev/null | sort || true)
  NEW_XVFB=$(comm -13 <(echo "$XVFB_BEFORE") <(echo "$XVFB_AFTER") 2>/dev/null || true)
  if [ -n "$NEW_XVFB" ]; then
    log "Cleaning up orphan Xvfb: ${NEW_XVFB//$'\n'/ }"
    echo "$NEW_XVFB" | xargs -r kill 2>/dev/null || true
  fi
}

node check-usage.js >> "$LOG" 2>&1 &
NODE_PID=$!
wait $NODE_PID 2>/dev/null
cleanup

log "Usage check completed"
