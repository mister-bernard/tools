# screenshot

CLI screenshot tool using Playwright. Captures any URL as PNG/JPG with viewport control, full-page mode, and mobile emulation.

## Why

Every screenshot tool is either a bloated Electron app or a 200-line wrapper with 15 dependencies. This is 60 lines, one dependency, and it handles the things you actually need: custom viewport, mobile emulation, full-page capture, and wait-for-render.

## Install

```bash
npm install playwright
# Then put screenshot.js somewhere in your PATH
```

Playwright will download Chromium on first run (~150MB).

## Usage

```bash
# Basic — saves to /tmp/screenshot.png
node screenshot.js https://example.com

# Custom output path
node screenshot.js https://example.com ~/shots/example.png

# Full page (scrolls entire page)
node screenshot.js https://example.com --full

# Mobile emulation (iPhone viewport, 3x DPR)
node screenshot.js https://example.com --mobile

# Custom viewport
node screenshot.js https://example.com --width=1920 --height=1080

# Wait longer for JS-heavy pages
node screenshot.js https://example.com --wait=5000

# JPEG output
node screenshot.js https://example.com shot.jpg
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--width=N` | 1280 | Viewport width |
| `--height=N` | 800 | Viewport height |
| `--full` | off | Capture full scrollable page |
| `--mobile` | off | iPhone viewport (390x844, 3x DPR) |
| `--wait=N` | 2000 | Wait N ms after DOM load before capture |

## Requirements

- Node.js 18+
- `playwright` npm package
