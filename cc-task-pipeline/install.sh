#!/usr/bin/env bash
# cc-task-pipeline — one-shot setup.
#
# Copies config.example.json → config.json (if missing), creates pipeline_root,
# prints cron lines to install, and checks that `claude` and `python3` resolve.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is required." >&2
    exit 1
fi

if [ ! -f config.json ]; then
    cp config.example.json config.json
    echo "→ created config.json (edit it before running)."
else
    echo "→ config.json already exists, not overwriting."
fi

ROOT=$(python3 - <<'PYEOF'
import sys
sys.path.insert(0, ".")
import pipeline_config
print(pipeline_config.load()["pipeline_root"])
PYEOF
)

mkdir -p "$ROOT"
echo "→ pipeline_root: $ROOT"

if ! command -v claude >/dev/null 2>&1; then
    echo "WARN: 'claude' CLI not on PATH. Set claude_bin in config.json or install it."
fi

if [ ! -f "$ROOT/queue.json" ]; then
    echo '{"version":3,"tasks":[]}' > "$ROOT/queue.json"
    echo "→ initialized empty queue at $ROOT/queue.json"
fi

cat <<EOF

Cron suggestion (copy into \`crontab -e\`):

    # Each script writes its own timestamped log inside \$pipeline_root.
    # We drop cron stdout (would duplicate every line) and send stderr to a
    # separate .err file so crash tracebacks are still captured.

    # hourly task generation from recent Claude Code sessions
    23 * * * * cd $SCRIPT_DIR && python3 chat-activity-generator.py >/dev/null 2>>$ROOT/chat-activity-generator.err
    # every 15 min: check /recommend, execute a queued task if safe
    */15 * * * * cd $SCRIPT_DIR && python3 auto-executor.py >/dev/null 2>>$ROOT/auto-executor.err
    # daily expiry sweep
    0 8 * * * cd $SCRIPT_DIR && python3 taskrunner.py expire >/dev/null 2>>$ROOT/taskrunner.err

Quick commands:

    python3 taskrunner.py add "review the proposal" --owner human --priority high
    python3 taskrunner.py list
    python3 taskrunner.py stats

Kill-switches (touch to pause, rm to resume):

    touch $ROOT/.auto-gen-disabled     # stop generation
    touch $ROOT/.auto-exec-disabled    # stop auto-execution
EOF
