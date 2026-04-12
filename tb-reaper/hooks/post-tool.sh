#!/usr/bin/env bash
# Claude Code PostToolUse hook — clears the active task registry entry.
set -euo pipefail

REAPER_DIR="${HOME}/.claude/reaper"
TASK_DIR="${REAPER_DIR}/tasks"
LAST_ACTIVE_DIR="${REAPER_DIR}/last-active"
mkdir -p "$TASK_DIR" "$LAST_ACTIVE_DIR"

payload="$(cat || true)"
sid="$(printf '%s' "$payload" | python3 -c 'import json,sys;d=json.load(sys.stdin) if sys.stdin.isatty()==False else {};print(d.get("session_id",""))' 2>/dev/null || true)"

[ -z "$sid" ] && exit 0

rm -f "${TASK_DIR}/${sid}.json"
touch "${LAST_ACTIVE_DIR}/${sid}"

exit 0
