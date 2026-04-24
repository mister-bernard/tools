# telegraph

Telegram and Signal router that dispatches incoming messages to CLI agent sessions via an OpenAI-compatible HTTP bridge, then delivers the response back to the originating chat.

Built for operators running multiple CLI-driven agents (Claude Code, Codex, etc.) behind a bridge and wanting to talk to them from Telegram or Signal without exposing the terminal. Drop in any bridge that speaks `POST /v1/chat/completions`.

## What it does

- **Long-polls Telegram** (multiple bot accounts) and, optionally, **Signal** (via [signal-cli](https://github.com/AsamK/signal-cli) JSON-RPC).
- **Routes messages** through a static bindings table (`account + chat-type + peer → session`), with wildcard fallbacks.
- **Origin routing** — remembers which agent *sent* a message, so when a human replies to that message, the reply goes back to the same agent instead of the statically-routed default session.
- **Bridges to a CLI agent** via OpenAI-compatible `/v1/chat/completions`. The session ID is passed as the `model` field; anything that speaks that shape works.
- **Delivers replies** with HTML formatting, chunking, attachment extraction, and per-chat threading.
- **Built-in commands** — `/help`, `/ping`, `/health`, `/sessions`, `/context`, `/clear`, `/model`, `/effort`.

## Architecture

```
 ┌──────────────┐         ┌──────────────┐
 │  Telegram    │────┐    │   Signal     │
 │  bots (N)    │    │    │  (signal-cli) │
 └──────────────┘    │    └──────────────┘
        │            │             │
        ▼            ▼             ▼
 ┌──────────────────────────────────────┐
 │         telegraph poller             │
 │   ┌──────────────────────────────┐   │
 │   │  Router (bindings table)     │   │
 │   │  Origin Store (reply memory) │   │
 │   │  Context Tracker (optional)  │   │
 │   └──────────────────────────────┘   │
 └──────────────────────────────────────┘
                 │
                 ▼
      POST /v1/chat/completions
      (model = session id)
                 │
                 ▼
      ┌───────────────────────┐
      │   your agent bridge   │
      │   (any OpenAI-compat) │
      └───────────────────────┘
```

## Install

```bash
git clone https://github.com/mister-bernard/tools.git
cd tools/telegraph
cp telegraph.example.json telegraph.json
cp .env.example .env
# edit both — put your bot tokens in .env and your routing rules in telegraph.json
```

No npm dependencies. Node 18+ only.

## Configure

### `.env`

```
TELEGRAM_BOT_TOKEN=123456:your_token
TELEGRAM_SECONDARY_BOT_TOKEN=789012:second_token
BRIDGE_PROVIDER_KEY=your-bridge-api-key
TELEGRAPH_PORT=18903
TELEGRAPH_BIND=127.0.0.1
TELEGRAPH_ATTACHMENT_ROOT=/home/you   # optional — restricts file-path extraction
```

### `telegraph.json`

The `bindings` array is evaluated top-to-bottom, first match wins. A `peer` of `"*"` matches anything.

```json
{
  "accounts": {
    "default": { "botToken": "${TELEGRAM_BOT_TOKEN}", "pollTimeout": 30 }
  },
  "bridge": {
    "baseUrl": "http://127.0.0.1:18901/v1",
    "bearer": "${BRIDGE_PROVIDER_KEY}"
  },
  "bindings": [
    { "account": "default", "chat": "dm",    "peer": "10000001",  "session": "session-primary" },
    { "account": "default", "chat": "group", "peer": "*",         "session": "session-default" }
  ]
}
```

Environment variables referenced as `${VAR}` are expanded at load.

### Signal (optional)

Run `signal-cli` in JSON-RPC mode on `127.0.0.1:7583`, add an account binding named `"signal"`, and telegraph will poll it alongside Telegram.

## Run

```bash
node --env-file=.env src/main.js
```

Or as a systemd user unit — see `deploy/telegraph.service`.

Health check:

```bash
curl 127.0.0.1:18903/healthz
```

## Commands

Send any of these to a bot telegraph is polling:

| Command | Effect |
|---------|--------|
| `/help` | List commands |
| `/ping` | Uptime + message counts |
| `/health` | Bridge reachability + version |
| `/sessions` | List configured sessions |
| `/context` | Dump shared-context file (if configured) |
| `/clear` | Print the phrase to paste to clear the agent's context |
| `/model <session> <alias>` | Change the session's model (requires bridge `.env` path) |
| `/effort <session> <low\|medium\|high>` | Change the session's reasoning effort |

The `/model` and `/effort` commands are wired for bridges whose `.env` holds per-session JSON maps (`BRIDGE_SESSION_MODELS`, `BRIDGE_SESSION_EXTRA_ARGS`); delete `src/commands.js::readBridgeEnv`/`writeBridgeEnvField` or rewrite to your backend if that shape doesn't fit.

## Origin routing

When an agent posts into Telegram through telegraph, telegraph records `{chatId, messageId} → origin` in `state/origins.jsonl`. Later, when a human quote-replies to that message, telegraph looks up the origin and dispatches the reply back to the same agent instead of the statically-routed session. Falls back to the static route (with a provenance note in the prompt) if the origin agent is unreachable.

Disable via `"originStore": { "enabled": false }` in `telegraph.json`.

## Testing

```bash
node --test tests/*.test.js
```

72 tests, zero dependencies.

## License

MIT.
