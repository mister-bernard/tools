#!/usr/bin/env python3
"""Auto-executor — headroom-gated task runner.

Consumes /recommend and /trends from a local tokenburn API and decides whether
to auto-execute a queued task via task-executor.sh.

Policy (strict — spawns a full Claude agent, expensive):
  - action in {halt, cool_down_all, reduce, switch, worker_saturated,
               protect_main, unknown}                        → SKIP
  - action == route_worker                                   → RUN only if
                                                               active account
                                                               already matches
                                                               rec.route_to
  - action in {use_more, continue}                           → RUN if gates pass
  - active account status in {hot, cool_down}                → SKIP
  - per-account reserve floor on headroom                    → SKIP if violated
  - per-account weekly cap                                   → SKIP if violated
  - trend-spike guard (today > N× 7d median)                 → SKIP if guard enabled
                                                               for the active account

Also:
  - Records a one-line decision log per run
  - Respects kill-switch at $pipeline_root/.auto-exec-disabled
  - At most 1 task per run (the executor itself is rate-limited)
  - Pings Telegram on halt-like state transitions (see ALERT_ACTIONS below)
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # noqa: E402


CFG = pipeline_config.load()
ROOT = Path(CFG["pipeline_root"])
KILL_SWITCH = ROOT / ".auto-exec-disabled"
STATE_FILE = ROOT / ".auto-exec-state.json"
LOG = ROOT / "auto-executor.log"
EXECUTOR = Path(__file__).resolve().parent / "task-executor.sh"

TOKENBURN = CFG["tokenburn_url"]
RECOMMEND_URL = f"{TOKENBURN}/recommend"
TRENDS_URL = f"{TOKENBURN}/trends"
HEALTH_URL = f"{TOKENBURN}/health"

EXEC_CFG = CFG["executor"]
ACCOUNTS = EXEC_CFG.get("accounts") or {}
DEFAULT_MIN_HEADROOM = EXEC_CFG.get("default_min_headroom_pct", 20)
DEFAULT_MAX_WEEKLY = EXEC_CFG.get("default_max_weekly_pct", 85)
TREND_SPIKE_MULTIPLIER = EXEC_CFG.get("trend_spike_multiplier", 2.0)


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fetch_json(url: str, timeout: float = 5.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        log(f"fetch fail {url}: {e}")
        return None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(tmp, STATE_FILE)


def notify_tg(msg: str) -> None:
    token, chat_id, _ = pipeline_config.get_telegram_creds(CFG)
    if not token or not chat_id:
        return
    try:
        subprocess.run(
            ["curl", "-s", "-X", "POST",
             f"https://api.telegram.org/bot{token}/sendMessage",
             "--data-urlencode", f"chat_id={chat_id}",
             "--data-urlencode", f"text={msg}",
             "-d", "parse_mode=HTML"],
            timeout=10, capture_output=True,
        )
    except Exception:
        pass


def _account_policy(aid: str) -> tuple[int, int, bool]:
    a = ACCOUNTS.get(aid) or {}
    return (
        int(a.get("min_headroom_pct", DEFAULT_MIN_HEADROOM)),
        int(a.get("max_weekly_pct", DEFAULT_MAX_WEEKLY)),
        bool(a.get("trend_spike_guard", False)),
    )


SKIP_ACTIONS = (
    "halt", "cool_down_all", "reduce", "switch",
    "worker_saturated", "protect_main", "unknown",
)
RUN_ACTIONS = ("use_more", "continue", "route_worker")


def decide(rec: dict, trends: dict | None) -> tuple[bool, str]:
    """Return (run, reason). See module docstring for policy."""
    action = rec.get("action")
    if action in SKIP_ACTIONS:
        return False, f"action={action}"
    if action not in RUN_ACTIONS:
        return False, f"action={action!r} (unrecognized)"

    active = next((a for a in rec.get("accounts", []) if a.get("active")), None)
    if not active:
        return False, "no-active-account"

    aid = active.get("id", "?")
    route_to = rec.get("route_to")
    # For route_worker, the executor can only honor the routing if the active
    # account IS the recommended target — we can't change the active account
    # from here, so skip and let whatever controls routing resolve it.
    if action == "route_worker" and route_to and route_to != aid:
        return False, f"route-mismatch active={aid} route_to={route_to}"

    astatus = active.get("status")
    if astatus in ("hot", "cool_down"):
        return False, f"active-status={astatus}"

    head = active.get("headroom_pct")
    wk = active.get("weekly_pct", 0)
    min_head, max_wk, spike_guard = _account_policy(aid)

    if head is None or head < min_head:
        return False, f"reserve-floor acct={aid} head={head}% min={min_head}%"
    if wk > max_wk:
        return False, f"weekly acct={aid} wk={wk}% max={max_wk}%"

    if action in ("use_more", "route_worker"):
        return True, f"action={action} acct={aid} head={head}%"

    # action == "continue": optional trend-spike guard
    if spike_guard and trends and trends.get("daily"):
        daily = trends["daily"]
        if len(daily) >= 3:
            today = daily[-1].get("tokens", 0)
            prior = [d.get("tokens", 0) for d in daily[:-1]]
            median = statistics.median(prior) if prior else 0
            if median > 0 and today > median * TREND_SPIKE_MULTIPLIER:
                return False, f"trend-spike acct={aid} today={today} median={int(median)}"
    return True, f"action=continue acct={aid} head={head}% wk={wk}%"


def run_executor() -> tuple[int, str]:
    if not EXECUTOR.exists():
        return 127, f"executor-missing {EXECUTOR}"
    try:
        proc = subprocess.run(
            ["bash", str(EXECUTOR), "--safe-only"],
            capture_output=True, text=True,
            timeout=int(CFG.get("executor_timeout_sec", 900)),
        )
        return proc.returncode, (proc.stdout + proc.stderr)[-500:]
    except subprocess.TimeoutExpired:
        return 124, "executor-timeout"


def main() -> int:
    if KILL_SWITCH.exists():
        log("kill-switch — exiting")
        return 0

    health = fetch_json(HEALTH_URL)
    if not health or health.get("status") != "ok":
        log(f"health not ok: {health}")
        return 0

    rec = fetch_json(RECOMMEND_URL)
    if not rec:
        log("no /recommend — exit")
        return 0

    trends = fetch_json(TRENDS_URL)
    state = load_state()
    prev_action = state.get("last_action")
    cur_action = rec.get("action")

    ALERT_ACTIONS = ("cool_down_all", "reduce", "switch", "protect_main", "worker_saturated")
    if prev_action != cur_action and cur_action in ALERT_ACTIONS:
        notify_tg(f"⚠️ tokenburn /recommend: <b>{cur_action}</b>\n{rec.get('message', '')}")

    ok, reason = decide(rec, trends)
    log(f"decision: {'RUN' if ok else 'SKIP'} — {reason}")

    if ok:
        code, tail = run_executor()
        log(f"executor exit={code} tail={tail[-200:]!r}")
        state["last_run_ts"] = datetime.now(timezone.utc).isoformat()
        state["last_exit"] = code

    state["last_action"] = cur_action
    state["last_reason"] = reason
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
