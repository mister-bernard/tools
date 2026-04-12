# ig-download

Download Instagram Reels and Posts from the command line.

Uses `yt-dlp` with automatic cookie fallback. When automated methods fail, suggests Telegram bot alternatives.

## Usage

```bash
./ig-download.sh https://www.instagram.com/reel/DWeOnaODdHP/

# Custom output directory
./ig-download.sh https://www.instagram.com/p/ABC123/ ~/downloads/
```

## Requirements

- `yt-dlp` — `pip install yt-dlp`
- Optional: browser with Instagram session for private content
