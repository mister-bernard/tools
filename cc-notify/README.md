# cc-notify

Desktop notifications for Claude Code that actually make it through tmux, mosh, and SSH.

Claude Code's native notification signalling relies on terminal escape sequences (OSC 9) that get silently swallowed by mosh and mangled by tmux. `cc-notify` sidesteps the whole terminal-escape stack and pushes an HTTP notification to [ntfy.sh](https://ntfy.sh) — the ntfy app on your phone or a browser tab on your laptop delivers native macOS/iOS/Android notifications.

## Why

You SSH from your laptop to a dev box and run Claude Code inside tmux. Maybe mosh too. Claude finishes a long task. You get nothing — the escape codes never reach your terminal. With cc-notify:

- `Stop` hook fires → notification titled with the **auto-generated session topic** (e.g. *"Refactor auth middleware"*), body *"task finished"*
- `Notification` hook fires → *"needs attention"* when Claude is waiting on you
- **Teammates are silent** — if you're running a multi-pane team session, only the orchestrator (not the `sdk-cli` spawns) pings you

Works over any number of tmux/mosh/ssh/jump-host layers because it's HTTP, not a terminal escape.

## Install

```bash
cd cc-notify
./install.sh
```

The installer:
- Copies `cc-notify` into `~/.local/bin`
- Scaffolds `~/.config/cc-notify/config` with a random ntfy topic (if one doesn't exist)
- Registers `Stop` and `Notification` hooks in `~/.claude/settings.json` (backup saved as `settings.json.bak.cc-notify`)

Then on your phone or laptop:

1. Install the ntfy app ([App Store](https://apps.apple.com/app/id1625396347) / [Play Store](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [Web](https://ntfy.sh))
2. Subscribe to the topic shown in `~/.config/cc-notify/config`

## Session labelling

When the hook fires, cc-notify resolves a session label in this order:

1. **tmux `pane_title`** — this captures the auto-generated topic that Claude Code sets via OSC title. It's the same string you see in your terminal tab.
2. **`name` field of `~/.claude/sessions/<pid>.json`** — set by `/rename`.
3. **basename of the session cwd** — last-ditch fallback.

So your notifications look like:

```
Title:  Refactor auth middleware
Body:   task finished
```

## Suppressing teammates

Claude Code team sessions spawn child Claudes with `entrypoint: "sdk-cli"` in their session file. cc-notify detects this and exits silently, so only the orchestrator fires. In-process `Agent` tool subagents use `SubagentStop` (a separate hook event), which cc-notify doesn't register by default — so subagent completions are silent too.

If you *want* teammates to notify, remove the `entrypoint == "sdk-cli"` skip in `bin/cc-notify`.

## Config

`~/.config/cc-notify/config` is a shell-sourceable file of `KEY=VALUE` lines. All keys are optional except the topic.

| Variable | Default | Purpose |
|---|---|---|
| `CC_NOTIFY_NTFY_TOPIC` | *(generated at install)* | Topic string. Use something unguessable if you care about privacy. |
| `CC_NOTIFY_NTFY_SERVER` | `https://ntfy.sh` | Change to your self-hosted ntfy instance. |
| `CC_NOTIFY_PRIORITY` | `default` | `min` / `low` / `default` / `high` / `urgent` — controls badge prominence. |
| `CC_NOTIFY_LOG` | `~/.cache/cc-notify.log` | Log path. Tail this when debugging. |

Environment variables with the same names override the config file.

## Security

ntfy.sh topics are public URLs — anyone who knows the topic can read and publish messages. The installer generates a cryptographically random 16-character suffix, which is effectively unguessable, but if you're sending sensitive content consider:

- Self-hosting ntfy with auth enabled
- Keeping notifications status-only (*"task finished"*, *"needs attention"*) — the default.

## Uninstall

```bash
rm ~/.local/bin/cc-notify
# Then edit ~/.claude/settings.json and remove the Stop + Notification
# entries, or restore settings.json.bak.cc-notify.
```

## Troubleshooting

```bash
tail -f ~/.cache/cc-notify.log              # watch what the hook does
cc-notify "Claude Code" "manual test"       # fire a test directly
cat ~/.config/cc-notify/config              # check topic
```

If the log shows `no topic configured`, the config file either doesn't exist or doesn't set `CC_NOTIFY_NTFY_TOPIC`.

If you see curl output with an `id` field in the log but no notification arrives, the topic is being published correctly — your subscription side is the problem. Make sure the ntfy app or browser tab is subscribed to the exact topic in your config.
