# tb — tokenburn

Real-time terminal dashboard for Claude Pro Max token usage. Tracks session consumption, weekly limits, burn rate, and provides optimization advisories.

![tb screenshot](../docs/tb-screenshot.png)

## What it does

- **Live Anthropic usage** — reads scraped session/weekly percentages from `usage-clean.json` (if you run the [anthropic-usage](../anthropic-usage/) scraper)
- **Per-session breakdown** — parses `~/.claude/projects/**/*.jsonl` for actual token counts per message, model, and 5h window
- **Burn rate** — tokens/minute, %/10min, time-to-100% projections
- **Optimization score** — composite of burn, parallelism, throughput, model breadth, velocity
- **Weekly balancing** — budget advisor showing ideal %/day based on remaining allocation
- **Smart advisories** — automatic alerts when you're under-utilizing or approaching limits
- **System status** — memory, CPU, disk, load, network, service health, orphan detection
- **Session management** — engine table with model, idle time, context volume per session

## Install

```bash
pip install textual rich
cp tokenburn.py ~/.local/bin/tb
chmod +x ~/.local/bin/tb
```

Or symlink:
```bash
ln -s $(pwd)/tokenburn.py ~/.local/bin/tb
```

## Usage

```bash
tb                          # launch dashboard
```

### Keybindings

| Key | Action |
|-----|--------|
| `r` | Refresh |
| `s` | Score breakdown |
| `h` | 7-day history |
| `o` | Orphan/idle report |
| `k` | Kill idle+heavy sessions |
| `a` | Switch account |
| `e` | Export CSV |
| `c` | Config |
| `p` | Health check |
| `q` | Quit |

## Configuration

On first run, creates `~/.tokenburn.json`:

```json
{
  "active_account": "A",
  "accounts": [
    {
      "id": "A",
      "name": "Primary",
      "window_5h_limit": 900000,
      "window_7d_limit": 5000000,
      "target_pct_5h": 70
    }
  ],
  "warn_pct": 60,
  "urgent_pct": 80,
  "refresh_secs": 20,
  "max_parallel": 10,
  "plan_name": "Pro Max",
  "orphan_idle_mins": 60,
  "orphan_mem_mb": 150
}
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TB_SYSTEM_CACHE` | `~/.tokenburn-system-stats.json` | System stats JSON (optional, for extended disk/network/service data) |
| `TB_USAGE_FILE` | `~/.anthropic-usage/usage-clean.json` | Anthropic usage data path |
| `TB_SERVICES` | *(empty)* | Comma-separated systemd user services to monitor |

## Data sources

| Source | What | Refresh |
|--------|------|---------|
| `~/.anthropic-usage/usage-clean.json` | Real Anthropic session %, weekly %, reset times | Every 30m (via scraper cron) |
| `~/.claude/projects/**/*.jsonl` | Per-message tokens, model, timestamps | Live (on each dashboard refresh) |
| `~/.claude/stats-cache.json` | Daily aggregate token counts | Updated by Claude Code |
| `~/.claude/sessions/*.json` | Active session PIDs, start times | Live |
| `/proc/*` | Memory, CPU, load, uptime | Live |

## Requirements

- Python 3.10+
- `textual >= 0.40` and `rich >= 13`
- Linux (uses `/proc` for system metrics)
- Claude Code installed (`~/.claude/` directory)
- Optional: [anthropic-usage](../anthropic-usage/) scraper for real Anthropic percentages
