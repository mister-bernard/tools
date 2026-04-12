#!/bin/bash
# cleanup-backups — Remove .bak.* files older than N days
# Usage: cleanup-backups.sh [--dry-run] [--days 7] [dir1 dir2 ...]

set -euo pipefail

DRY_RUN=false
DAYS=7
DIRS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --days)    DAYS="$2"; shift 2 ;;
    *)         DIRS+=("$1"); shift ;;
  esac
done

# Default: current directory
[[ ${#DIRS[@]} -eq 0 ]] && DIRS=(".")

TOTAL=0
FREED=0

for dir in "${DIRS[@]}"; do
  [ -d "$dir" ] || { echo "skip: $dir (not a directory)"; continue; }
  while IFS= read -r -d '' f; do
    size=$(stat -c%s "$f" 2>/dev/null || echo 0)
    FREED=$((FREED + size))
    TOTAL=$((TOTAL + 1))
    if $DRY_RUN; then
      echo "[dry-run] would delete: $f ($(numfmt --to=iec "$size" 2>/dev/null || echo "${size}B"))"
    else
      rm "$f"
    fi
  done < <(find "$dir" -name "*.bak.*" -mtime +"$DAYS" -type f -print0 2>/dev/null)
done

FREED_HR=$(numfmt --to=iec "$FREED" 2>/dev/null || echo "${FREED}B")
if $DRY_RUN; then
  echo "Would delete $TOTAL files, freeing $FREED_HR"
else
  echo "Deleted $TOTAL backup files, freed $FREED_HR"
fi
