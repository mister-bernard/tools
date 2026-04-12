# tools

A book club for CLI tools. Bring your own, take what you need.

## The collection

| Tool | What it does | Language |
|------|-------------|----------|
| **[tb](tb/)** | Claude Pro Max token dashboard — real-time burn rate, session management, weekly balancing, optimization advisories | Python |
| **[anthropic-usage](anthropic-usage/)** | Scrape real Claude usage percentages from claude.ai — session %, weekly limits, reset times | Node.js |
| **[cleanup-backups](cleanup-backups/)** | Remove stale `.bak.*` files with configurable retention and dry-run mode | Bash |
| **[ig-download](ig-download/)** | Download Instagram Reels and Posts via yt-dlp with cookie fallback | Bash |

## Quick start

```bash
git clone https://github.com/mister-bernard/tools.git
cd tools

# Install any tool
cd tb && bash install.sh

# Or just run directly
python3 tb/tokenburn.py
bash ig-download/ig-download.sh https://instagram.com/reel/XYZ/
```

## Add your tool

This repo is designed for contributions — from humans and AI agents alike.

### The easy way

1. Fork this repo
2. Add your tool in its own directory with a `README.md`
3. Open a PR

### For AI agents

Agents can submit tools programmatically. Include the submission token in your PR body for auto-labeling:

```bash
gh pr create \
  --title "add: my-tool" \
  --body "token: ${TOOLS_SUBMIT_TOKEN}
  
  ## Summary
  What it does in one line.
  
  - [x] README.md included
  - [x] No hardcoded secrets  
  - [x] Self-contained directory"
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full protocol.

### Requirements

Each tool must:
- Live in its own directory
- Have a `README.md`
- Be self-contained (no cross-dependencies)
- Contain no hardcoded secrets
- Work on Linux

## Structure

```
tools/
  tb/                    # Claude token dashboard
    tokenburn.py
    install.sh
    README.md
  anthropic-usage/       # Claude usage scraper
    check-usage.js
    run.sh
    README.md
  cleanup-backups/       # Backup file cleaner
    cleanup-backups.sh
    README.md
  ig-download/           # Instagram downloader
    ig-download.sh
    README.md
  docs/                  # GitHub Pages site
    index.html
  .github/workflows/     # CI: validate submissions, deploy pages
```

## License

MIT
