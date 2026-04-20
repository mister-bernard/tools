#!/usr/bin/env bash
# task-executor.sh — Picks top ready task, notifies Telegram, executes via claude CLI.
# Triggered by auto-executor.py (not directly cron'd). Safe-first: only runs
# tasks flagged safe_for_auto=true when available.
#
# Usage:
#   bash task-executor.sh            # Normal run
#   bash task-executor.sh --dry-run  # Print task but don't execute
#   bash task-executor.sh --safe-only # Only run safe_for_auto=true tasks

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=false
SAFE_ONLY=false

for arg in "$@"; do
    case "$arg" in
        --dry-run)  DRY_RUN=true ;;
        --safe-only) SAFE_ONLY=true ;;
    esac
done

# Resolve config + roots via Python (single source of truth)
eval "$(python3 - "$SCRIPT_DIR" <<'PYEOF'
import sys, os
sys.path.insert(0, sys.argv[1])
import pipeline_config
cfg = pipeline_config.load()
token, chat_id, _ = pipeline_config.get_telegram_creds(cfg)
root = cfg["pipeline_root"]
print(f'PIPELINE_ROOT={root!r}')
print(f'CLAUDE_BIN={cfg["claude_bin"]!r}')
print(f'MAX_BUDGET={cfg.get("executor_budget_usd", 2.00)!r}')
print(f'TG_TOKEN={token!r}')
print(f'TG_CHAT={chat_id!r}')
extra = cfg.get("executor", {}).get("extra_add_dirs", []) or []
print(f'EXTRA_DIRS=({" ".join(repr(os.path.expanduser(d)) for d in extra)})')
PYEOF
)"

QUEUE="$PIPELINE_ROOT/queue.json"
READY_QUEUE="$PIPELINE_ROOT/ready-queue.json"
LOG="$PIPELINE_ROOT/task-executor.log"

mkdir -p "$PIPELINE_ROOT"
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG"; }

tg_send() {
    [ -z "$TG_TOKEN" ] || [ -z "$TG_CHAT" ] && return 0
    curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=$TG_CHAT" \
        --data-urlencode "text=$1" \
        -d "parse_mode=HTML&disable_web_page_preview=true" > /dev/null
}

# Pick top task — prefer safe_for_auto=true; fall back only if --safe-only absent
TASK_JSON=$(SAFE_ONLY="$SAFE_ONLY" python3 - "$QUEUE" "$READY_QUEUE" <<'PYEOF'
import json, os, sys

queue_file, ready_file = sys.argv[1], sys.argv[2]
safe_only = os.environ.get("SAFE_ONLY") == "true"

try:
    ready = json.loads(open(ready_file).read())
    ready_ids = {t["id"] for t in ready.get("tasks", [])}
except Exception:
    ready_ids = set()

try:
    queue = json.loads(open(queue_file).read())
    tasks = queue.get("tasks", [])
except Exception:
    sys.exit(0)

# If no ready-queue, fall through to using queue directly (pending tasks)
if ready_ids:
    candidates = [t for t in tasks if t.get("id") in ready_ids and t.get("status") == "pending"]
else:
    candidates = [t for t in tasks if t.get("status") == "pending"]

safe = [t for t in candidates if t.get("safe_for_auto") is True]
pool = safe if safe else ([] if safe_only else candidates)
if not pool:
    sys.exit(0)
pool.sort(key=lambda t: t.get("roi_score", 0), reverse=True)
print(json.dumps(pool[0]))
PYEOF
)

if [ -z "$TASK_JSON" ]; then
    log "No ready tasks to execute."
    exit 0
fi

TASK_ID=$(echo "$TASK_JSON"     | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
TASK_DESC=$(echo "$TASK_JSON"   | python3 -c "import json,sys; print(json.load(sys.stdin)['task'])")
TASK_PROJ=$(echo "$TASK_JSON"   | python3 -c "import json,sys; print(json.load(sys.stdin).get('project','?'))")
TASK_CTX=$(echo "$TASK_JSON"    | python3 -c "import json,sys; print(json.load(sys.stdin).get('context',''))")
IS_SAFE=$(echo "$TASK_JSON"     | python3 -c "import json,sys; print(json.load(sys.stdin).get('safe_for_auto', False))")

log "Selected $TASK_ID [$TASK_PROJ] safe=$IS_SAFE: $TASK_DESC"

if [ "$DRY_RUN" = true ]; then
    log "DRY RUN — skipping execution."
    exit 0
fi

tg_send "🤖 <b>Auto-executing task</b>

<code>$TASK_ID</code> [$TASK_PROJ]
$TASK_DESC"

TASKRUNNER="$SCRIPT_DIR/taskrunner.py"
PROMPT="You are executing a queued task autonomously. Be efficient and direct. No preamble.

TASK ID: $TASK_ID
PROJECT: $TASK_PROJ
TASK: $TASK_DESC
CONTEXT: ${TASK_CTX:-none}

Instructions:
1. Execute this task to completion using available tools.
2. When done, mark it complete:
   python3 $TASKRUNNER done $TASK_ID \"<one-line outcome>\"
3. If the task cannot be completed autonomously (needs human input, unclear, risky), mark it blocked:
   python3 $TASKRUNNER block $TASK_ID \"reason\"

Do not ask for confirmation. Do not explain your plan. Execute and report."

ADD_DIR_ARGS=(--add-dir "$PIPELINE_ROOT")
for d in "${EXTRA_DIRS[@]}"; do
    ADD_DIR_ARGS+=(--add-dir "$d")
done

STATUS=0
"$CLAUDE_BIN" \
    --print \
    --dangerously-skip-permissions \
    "${ADD_DIR_ARGS[@]}" \
    --max-budget-usd "$MAX_BUDGET" \
    -p "$PROMPT" >> "$LOG" 2>&1 || STATUS=$?

log "Task $TASK_ID executor exit code: $STATUS"
