#!/usr/bin/env bash
# Install cc-notify: copies the script to ~/.local/bin, scaffolds a
# config file if one doesn't exist, and wires Stop + Notification hooks
# into ~/.claude/settings.json.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
BIN="${HOME}/.local/bin"
CONFIG_DIR="${HOME}/.config/cc-notify"
CONFIG_FILE="$CONFIG_DIR/config"
SETTINGS="${HOME}/.claude/settings.json"

mkdir -p "$BIN" "$CONFIG_DIR"

install -m 0755 "$SRC/bin/cc-notify" "$BIN/cc-notify"
echo "✓ installed cc-notify → $BIN/cc-notify"

# Scaffold config if missing. Generate a fresh random topic — the user
# can edit or replace later.
if [ ! -f "$CONFIG_FILE" ]; then
  TOPIC="cc-notify-$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  cat > "$CONFIG_FILE" <<EOF
# cc-notify config — one KEY=VALUE per line, shell-sourceable.
# Generated $(date -Iseconds).
#
# Subscribe to this topic on your laptop/phone in the ntfy app or via
# https://ntfy.sh/$TOPIC to receive notifications.
CC_NOTIFY_NTFY_TOPIC=$TOPIC
# CC_NOTIFY_NTFY_SERVER=https://ntfy.sh       # self-hosted? change this
# CC_NOTIFY_PRIORITY=default                  # min|low|default|high|urgent
EOF
  chmod 600 "$CONFIG_FILE"
  echo "✓ wrote default config → $CONFIG_FILE"
  echo "  topic: $TOPIC"
else
  echo "• config exists at $CONFIG_FILE — left alone"
fi

# Register Stop + Notification hooks (idempotent merge).
python3 - "$BIN/cc-notify" "$SETTINGS" <<'PY'
import json, os, sys
from pathlib import Path

bin_cmd, settings_path = sys.argv[1], sys.argv[2]
settings = Path(settings_path)
data = json.loads(settings.read_text()) if settings.exists() else {}
hooks = data.setdefault("hooks", {})

def ensure(event, title_prefix, message):
    cmd = f'{bin_cmd} "{title_prefix}" "{message}"'
    entries = hooks.setdefault(event, [])
    for e in entries:
        for h in e.get("hooks", []):
            if h.get("command") == cmd:
                return False
    entries.append({"hooks": [{"type": "command", "command": cmd}]})
    return True

added = []
if ensure("Stop", "Claude Code", "task finished"):
    added.append("Stop")
if ensure("Notification", "Claude Code", "needs attention"):
    added.append("Notification")

if added:
    settings.parent.mkdir(parents=True, exist_ok=True)
    # Backup first edit only.
    backup = settings.with_suffix(".json.bak.cc-notify")
    if settings.exists() and not backup.exists():
        backup.write_text(settings.read_text())
    settings.write_text(json.dumps(data, indent=2))
    print(f"✓ registered hooks: {', '.join(added)}")
else:
    print("• hooks already registered — nothing to change")
PY

echo ""
echo "Next steps:"
echo "  1. Open your ntfy app (or https://ntfy.sh) and subscribe to the"
echo "     topic in $CONFIG_FILE."
echo "  2. Send a test:  cc-notify \"Claude Code\" \"test\""
echo "  3. Logs:         tail -f ~/.cache/cc-notify.log"
