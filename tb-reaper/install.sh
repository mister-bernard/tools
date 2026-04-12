#!/usr/bin/env bash
# Install tb-reaper: CLI symlinks, Claude hooks, systemd user timers.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
BIN="${HOME}/.local/bin"
UNIT="${HOME}/.config/systemd/user"
CLAUDE="${HOME}/.claude"

mkdir -p "$BIN" "$UNIT" "$CLAUDE/reaper"/{tasks,pending-kill,keep-alive,last-active}

# CLI entry points
install -m 0755 "$SRC/reaper.py" "$BIN/tb-reaper"
install -m 0755 "$SRC/digest.py" "$BIN/tb-reaper-digest"
install -m 0755 "$SRC/hooks/pre-tool.sh" "$BIN/tb-reaper-pre-hook"
install -m 0755 "$SRC/hooks/post-tool.sh" "$BIN/tb-reaper-post-hook"

# Systemd user units
install -m 0644 "$SRC/systemd/"*.service "$UNIT/"
install -m 0644 "$SRC/systemd/"*.timer "$UNIT/"
systemctl --user daemon-reload
systemctl --user enable --now tb-reaper.timer
systemctl --user enable --now tb-reaper-digest.timer

# Claude hook registration (idempotent)
python3 - <<'PY'
import json, os
from pathlib import Path

settings = Path.home() / ".claude" / "settings.json"
data = json.loads(settings.read_text()) if settings.exists() else {}
hooks = data.setdefault("hooks", {})

def ensure(event, cmd):
    entries = hooks.setdefault(event, [])
    # entries is a list of {"matcher": ..., "hooks": [{"type":"command","command":...}]}
    for e in entries:
        for h in e.get("hooks", []):
            if h.get("command") == cmd:
                return
    entries.append({"matcher": "*", "hooks": [{"type": "command", "command": cmd}]})

bin_dir = str(Path.home() / ".local" / "bin")
ensure("PreToolUse",  f"{bin_dir}/tb-reaper-pre-hook")
ensure("PostToolUse", f"{bin_dir}/tb-reaper-post-hook")

backup = settings.with_suffix(".json.bak.tb-reaper")
if settings.exists() and not backup.exists():
    backup.write_text(settings.read_text())
settings.write_text(json.dumps(data, indent=2))
print(f"✓ hooks registered in {settings}")
PY

echo "✓ tb-reaper installed"
echo "  timer:   systemctl --user status tb-reaper.timer"
echo "  dry run: TB_REAPER_DRY_RUN=1 tb-reaper"
echo "  log:     tail -f ~/.tokenburn-reaper.log"
