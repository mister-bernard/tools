# cleanup-backups

Removes `.bak.*` files older than N days from specified directories.

Useful when your workflow (or your AI agent) creates timestamped backups like `config.json.bak.20260412-143022` before edits.

## Usage

```bash
# Preview what would be deleted
./cleanup-backups.sh --dry-run ~/projects ~/configs

# Delete backups older than 7 days (default)
./cleanup-backups.sh ~/projects ~/configs

# Custom retention period
./cleanup-backups.sh --days 14 ~/projects

# Current directory
./cleanup-backups.sh --dry-run
```

## Install

```bash
cp cleanup-backups.sh ~/.local/bin/cleanup-backups
chmod +x ~/.local/bin/cleanup-backups
```

Or add to cron:
```bash
0 3 * * * bash /path/to/cleanup-backups.sh --days 7 ~/projects ~/configs
```
