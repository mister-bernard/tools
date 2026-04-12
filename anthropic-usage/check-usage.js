/**
 * anthropic-usage — scrape real Claude usage from claude.ai
 *
 * Launches a headed browser on Xvfb to pass Cloudflare,
 * injects session cookies, queries internal APIs.
 *
 * Output: ~/.anthropic-usage/usage-clean.json
 */

const { chromium } = require("playwright-extra");
const stealth = require("puppeteer-extra-plugin-stealth")();
chromium.use(stealth);
const fs = require("fs");
const path = require("path");

const BASE = process.env.USAGE_DIR || path.join(require("os").homedir(), ".anthropic-usage");
const CLEAN_FILE = path.join(BASE, "usage-clean.json");
const HISTORY_FILE = path.join(BASE, "usage-history.jsonl");

// Profiles to check — each needs a cookies-{name}.txt file
const PROFILES = (process.env.USAGE_PROFILES || "default").split(",").map(s => s.trim());

async function fetchProfileUsage(ctx, page, name) {
  const cookieFile = path.join(BASE, `cookies-${name}.txt`);
  if (!fs.existsSync(cookieFile)) return { error: `no_cookies (${cookieFile})` };

  const sessionKey = fs.readFileSync(cookieFile, "utf8").trim().replace("sessionKey=", "");

  await ctx.addCookies([{
    name: "sessionKey",
    value: sessionKey,
    domain: ".claude.ai",
    path: "/",
    httpOnly: true,
    secure: true,
  }]);

  const result = await page.evaluate(async () => {
    try {
      const orgResp = await fetch("/api/organizations");
      if (orgResp.status !== 200) return { error: `org_${orgResp.status}` };
      const orgs = await orgResp.json();
      if (!orgs[0]?.uuid) return { error: "no_org" };

      const org = orgs[0];
      const usageResp = await fetch(`/api/organizations/${org.uuid}/usage`);
      if (usageResp.status !== 200) return { error: `usage_${usageResp.status}` };
      const usage = await usageResp.json();

      const rateResp = await fetch(`/api/organizations/${org.uuid}/rate_limits`);
      const rates = rateResp.status === 200 ? await rateResp.json() : {};

      return { org: org.name, orgId: org.uuid, billing: org.billing_type, plan: rates.rate_limit_tier, usage };
    } catch (e) { return { error: e.message }; }
  });

  if (result.error) return { error: result.error };

  const u = result.usage;
  return {
    timestamp: new Date().toISOString(),
    account: result.org,
    plan: result.plan || "",
    currentSession: {
      resetsAt: u.five_hour?.resets_at || null,
      percentUsed: Math.round(u.five_hour?.utilization || 0),
    },
    weeklyLimits: {
      allModels: {
        resetsAt: u.seven_day?.resets_at || "unknown",
        percentUsed: Math.round(u.seven_day?.utilization || 0),
      },
      sonnetOnly: {
        resetsAt: u.seven_day_sonnet?.resets_at || "unknown",
        percentUsed: Math.round(u.seven_day_sonnet?.utilization || 0),
      },
    },
    extraUsage: u.extra_usage?.is_enabled ? {
      spent: u.extra_usage.used_credits || 0,
      monthlyLimit: u.extra_usage.monthly_limit || 0,
      percentUsed: Math.round(u.extra_usage.utilization || 0),
    } : {},
  };
}

async function main() {
  fs.mkdirSync(BASE, { recursive: true });
  const tmpDir = `/tmp/anthropic-browser-${Date.now()}`;

  try {
    const ctx = await chromium.launchPersistentContext(tmpDir, {
      headless: false,
      args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
      viewport: { width: 1280, height: 900 },
    });

    const page = ctx.pages()[0] || await ctx.newPage();

    // Pass Cloudflare challenge
    await page.goto("https://claude.ai", { waitUntil: "domcontentloaded", timeout: 30000 });
    for (let i = 0; i < 8; i++) {
      await page.waitForTimeout(3000);
      const title = await page.title();
      if (!title.includes("Just a moment")) break;
      if (i === 7) { console.error("CF challenge not resolved"); await ctx.close(); process.exit(1); }
    }

    // Load existing data for fallback
    let existing = {};
    if (fs.existsSync(CLEAN_FILE)) {
      try { existing = JSON.parse(fs.readFileSync(CLEAN_FILE, "utf8")).profiles || {}; } catch (e) { /* */ }
    }

    const profiles = {};
    for (const name of PROFILES) {
      const result = await fetchProfileUsage(ctx, page, name);
      if (result.error) {
        console.error(`${name}: ERROR - ${result.error}`);
        if (existing[name]) { profiles[name] = existing[name]; profiles[name]._stale = true; }
      } else {
        profiles[name] = result;
        delete profiles[name]._stale;
        console.log(`${name}: session=${result.currentSession.percentUsed}% weekly=${result.weeklyLimits.allModels.percentUsed}%`);
      }
      await ctx.clearCookies();
    }

    const clean = { lastCheck: new Date().toISOString(), profiles };
    fs.writeFileSync(CLEAN_FILE, JSON.stringify(clean, null, 2));

    // Append history
    const historyEntry = { ts: clean.lastCheck };
    for (const [k, v] of Object.entries(profiles)) {
      if (!v._stale) {
        historyEntry[k] = {
          weekly: v.weeklyLimits?.allModels?.percentUsed,
          session: v.currentSession?.percentUsed,
        };
      }
    }
    fs.appendFileSync(HISTORY_FILE, JSON.stringify(historyEntry) + "\n");

    await ctx.close();
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

main().then(() => process.exit(0)).catch(e => {
  console.error(e);
  process.exit(1);
});

// Safety timeout
setTimeout(() => {
  console.error("TIMEOUT: Force exiting after 120s");
  process.exit(2);
}, 120000).unref();
