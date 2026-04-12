#!/usr/bin/env bash
# Claude Code PreToolUse hook — registers an active task for this session.
# Input JSON on stdin: {"session_id": "...", "tool_name": "...", ...}
set -euo pipefail

REAPER_DIR="${HOME}/.claude/reaper"
TASK_DIR="${REAPER_DIR}/tasks"
LAST_ACTIVE_DIR="${REAPER_DIR}/last-active"
KEEPALIVE_DIR="${REAPER_DIR}/keep-alive"
mkdir -p "$TASK_DIR" "$LAST_ACTIVE_DIR" "$KEEPALIVE_DIR"

payload="$(cat || true)"
sid="$(printf '%s' "$payload" | python3 -c 'import json,sys;d=json.load(sys.stdin) if sys.stdin.isatty()==False else {};print(d.get("session_id",""))' 2>/dev/null || true)"
tool="$(printf '%s' "$payload" | python3 -c 'import json,sys;d=json.load(sys.stdin) if sys.stdin.isatty()==False else {};print(d.get("tool_name",""))' 2>/dev/null || true)"

[ -z "$sid" ] && exit 0

cat > "${TASK_DIR}/${sid}.json" <<EOF
{"tool":"${tool}","started":$(date +%s)}
EOF

touch "${LAST_ACTIVE_DIR}/${sid}"
# Abort any pending reaper decision for us — we are demonstrably alive
touch "${KEEPALIVE_DIR}/${sid}"

exit 0
