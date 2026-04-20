#!/usr/bin/env node
/**
 * Headless browser screenshot tool.
 *
 * Usage:
 *   node screenshot.js <url> [output.png] [--width=1280] [--height=800] [--full] [--wait=2000] [--mobile]
 *
 * Examples:
 *   node screenshot.js https://example.com
 *   node screenshot.js https://example.com /tmp/shot.png --full
 *   node screenshot.js https://example.com --mobile --width=390
 */

const { chromium } = require('playwright');

async function main() {
  const args = process.argv.slice(2);

  const url = args.find(a => a.startsWith('http'));
  if (!url) {
    console.error('Usage: screenshot.js <url> [output.png] [--width=1280] [--height=800] [--full] [--wait=ms] [--mobile]');
    process.exit(1);
  }

  const output = args.find(a => a.endsWith('.png') || a.endsWith('.jpg')) || '/tmp/screenshot.png';
  const width = parseInt((args.find(a => a.startsWith('--width=')) || '--width=1280').split('=')[1]);
  const height = parseInt((args.find(a => a.startsWith('--height=')) || '--height=800').split('=')[1]);
  const fullPage = args.includes('--full');
  const waitMs = parseInt((args.find(a => a.startsWith('--wait=')) || '--wait=2000').split('=')[1]);
  const mobile = args.includes('--mobile');

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: mobile ? 390 : width, height: mobile ? 844 : height },
    deviceScaleFactor: mobile ? 3 : 2,
    isMobile: mobile,
    userAgent: mobile
      ? 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15'
      : undefined,
  });

  const page = await context.newPage();

  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    if (waitMs > 0) await page.waitForTimeout(waitMs);

    await page.screenshot({
      path: output,
      fullPage,
      type: output.endsWith('.jpg') ? 'jpeg' : 'png',
    });

    console.log(output);
  } catch (e) {
    console.error('Error:', e.message);
    process.exit(1);
  } finally {
    await browser.close();
  }
}

main();
