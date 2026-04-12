#!/bin/bash
# ig-download — Instagram Reel/Post downloader
# Usage: ig-download.sh <instagram_url> [output_dir]

set -euo pipefail

URL="${1:-}"
OUTPUT_DIR="${2:-/tmp/ig-reels}"

if [[ -z "$URL" ]]; then
    echo "Usage: $0 <instagram_url> [output_dir]"
    echo "Example: $0 https://www.instagram.com/reel/DWeOnaODdHP/"
    exit 1
fi

REEL_ID=$(echo "$URL" | grep -oP '(?<=reel/|p/)[A-Za-z0-9_-]+' | head -1)

if [[ -z "$REEL_ID" ]]; then
    echo "Error: Could not extract reel/post ID from URL"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"
OUTPUT_FILE="$OUTPUT_DIR/$REEL_ID.mp4"

echo "Downloading: $REEL_ID"
echo "Output: $OUTPUT_FILE"
echo ""

# Method 1: yt-dlp (primary)
echo "[1/2] Trying yt-dlp..."
if command -v yt-dlp &>/dev/null; then
    if yt-dlp -o "$OUTPUT_FILE" "$URL" 2>&1; then
        if [[ -f "$OUTPUT_FILE" ]]; then
            echo "Downloaded: $OUTPUT_FILE"
            ls -lh "$OUTPUT_FILE"
            exit 0
        fi
    fi
else
    echo "yt-dlp not found. Install: pip install yt-dlp"
fi

# Method 2: yt-dlp with browser cookies
echo "[2/2] Trying yt-dlp with browser cookies..."
for browser in chromium chrome firefox; do
    if yt-dlp --cookies-from-browser "$browser" -o "$OUTPUT_FILE" "$URL" 2>&1; then
        if [[ -f "$OUTPUT_FILE" ]]; then
            echo "Downloaded (with cookies): $OUTPUT_FILE"
            ls -lh "$OUTPUT_FILE"
            exit 0
        fi
    fi
done 2>/dev/null

echo ""
echo "Automated download failed. Try manually:"
echo "  1. Open a Telegram bot like @SaveAsBot"
echo "  2. Send: $URL"
echo "  3. Save the returned video"
exit 1
