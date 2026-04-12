# tb-reaper

Cooperative idle-session reaper for Claude Code. Finds sessions that have gone quiet for hours, checks they aren't in the middle of a tool call, negotiates a shutdown via filesystem sentinels, and only then kills them.

A sibling to [`tb`](../tb/) — `tb` shows you the burn; `tb-reaper` cleans up the embers.

## What it does

- Scans `~/.claude/sessions/` every hour (user systemd timer)
- A session is a **candidate** when all of these hold:
  - idle > `TB_REAPER_IDLE_HOURS` (default **6h**) — no JSONL writes, no hook pings
  - resident memory > `TB_REAPER_MIN_MEM_MB` (default **300 MB**)
  - no active task recorded via the PreToolUse hook (or the task entry is stale)
- **Negotiation protocol** before any kill:
  1. Reaper writes `~/.claude/reaper/pending-kill/<sid>`
  2. Waits `TB_REAPER_GRACE_SECS` (default **120s**)
  3. If session touches `~/.claude/reaper/keep-alive/<sid>` → abort (any tool call does this)
  4. Otherwise SIGTERM, wait 10s, SIGKILL
- Logs every decision to `~/.tokenburn-reaper.log`
- Weekly Telegram digest (Sundays 18:00) via `tb-reaper-digest`

## Install

```bash
cd tb-reaper
./install.sh
```

The installer:
- Symlinks `tb-reaper`, `tb-reaper-digest`, and the two hook scripts into `~/.local/bin`
- Drops systemd user units into `~/.config/systemd/user` and enables the hourly timer + weekly digest
- Registers `PreToolUse` and `PostToolUse` hooks in `~/.claude/settings.json` (backup saved as `settings.json.bak.tb-reaper`)

## Usage

```bash
tb-reaper                          # one-shot scan (normally run by timer)
TB_REAPER_DRY_RUN=1 tb-reaper      # see what would be reaped, kill nothing
tail -f ~/.tokenburn-reaper.log    # watch decisions
systemctl --user status tb-reaper.timer
```

## Configuration

Tune via environment vars in `~/.config/systemd/user/tb-reaper.service`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `TB_REAPER_IDLE_HOURS` | `6` | Minimum idle time before a session is a candidate |
| `TB_REAPER_MIN_MEM_MB` | `300` | Skip sessions lighter than this |
| `TB_REAPER_GRACE_SECS` | `120` | Negotiation window before SIGTERM |
| `TB_REAPER_TASK_STALE_HOURS` | `2` | Treat task registry entries older than this as stale |
| `TB_REAPER_DRY_RUN` | `0` | Log candidates only, never kill |

## How sessions opt out of a kill

Any tool call during the grace window touches `keep-alive/<sid>` via the PostToolUse hook — the reaper aborts instantly. A session that's genuinely in the middle of a long tool call (e.g., a multi-minute build) has a fresh task entry in `tasks/<sid>.json` and won't be flagged in the first place.

## Files

```
~/.claude/reaper/
  tasks/<sid>.json        # active task — cleared by PostToolUse
  last-active/<sid>       # mtime = last hook event
  pending-kill/<sid>      # reaper's notice of intent
  keep-alive/<sid>        # session's "I'm still here"
~/.tokenburn-reaper.log          # append-only decision log
~/.tokenburn-reaper-digest.json  # last 14 days of actions
```

## Uninstall

```bash
systemctl --user disable --now tb-reaper.timer tb-reaper-digest.timer
rm ~/.config/systemd/user/tb-reaper*.{service,timer}
rm ~/.local/bin/tb-reaper ~/.local/bin/tb-reaper-digest ~/.local/bin/tb-reaper-{pre,post}-hook
# restore the pre-install settings if you want:
mv ~/.claude/settings.json.bak.tb-reaper ~/.claude/settings.json
systemctl --user daemon-reload
```
