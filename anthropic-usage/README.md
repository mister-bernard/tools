# anthropic-usage

Scrapes your real Claude Pro/Team/Max usage percentages from claude.ai. Produces a clean JSON file that other tools (like [tb](../tb/)) can consume.

## What it reports

- **Session (5h window)** — current utilization %, reset time
- **Weekly all-models** — utilization %, reset time
- **Weekly Sonnet-only** — utilization %, reset time
- **Extra usage** — credit spend, monthly limit (if enabled)

Supports multiple profiles (e.g., primary + backup accounts).

## How it works

1. Uses Playwright with stealth plugin to pass Cloudflare
2. Injects your `sessionKey` cookie
3. Queries `claude.ai` internal APIs from the browser context
4. Writes structured JSON to `~/.anthropic-usage/usage-clean.json`
5. Appends history to `usage-history.jsonl`

## Setup

```bash
cd anthropic-usage
npm install
```

### Get your session cookie

1. Open `claude.ai` in Chrome
2. DevTools → Application → Cookies → `claude.ai`
3. Copy the `sessionKey` value
4. Save it:

```bash
mkdir -p ~/.anthropic-usage
echo "YOUR_SESSION_KEY" > ~/.anthropic-usage/cookies-default.txt
```

For multiple accounts, create `cookies-backup.txt` etc.

### Xvfb (headless Linux)

The scraper needs a display for Cloudflare bypass:

```bash
sudo apt install xvfb
Xvfb :99 -screen 0 1280x800x24 &
export DISPLAY=:99
```

## Usage

```bash
# One-shot check
bash run.sh

# Cron (every 30 minutes)
*/30 * * * * bash /path/to/anthropic-usage/run.sh >> /tmp/anthropic-usage.log 2>&1
```

## Output format

`~/.anthropic-usage/usage-clean.json`:

```json
{
  "lastCheck": "2026-04-12T12:00:19.150Z",
  "profiles": {
    "default": {
      "timestamp": "2026-04-12T12:00:17.392Z",
      "account": "you@example.com's Organization",
      "plan": "default_claude_max_20x",
      "currentSession": {
        "resetsAt": "2026-04-12T17:00:01+00:00",
        "percentUsed": 48
      },
      "weeklyLimits": {
        "allModels": {
          "resetsAt": "2026-04-17T03:00:00+00:00",
          "percentUsed": 26
        },
        "sonnetOnly": {
          "resetsAt": "2026-04-17T05:00:00+00:00",
          "percentUsed": 11
        }
      },
      "extraUsage": {}
    }
  }
}
```

## Requirements

- Node.js 18+
- `playwright-extra`, `puppeteer-extra-plugin-stealth`
- Xvfb (for headless Linux servers)
- A valid claude.ai session cookie
