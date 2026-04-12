#!/usr/bin/env python3
"""
tokenburn (tb) — Claude Pro Max token usage dashboard

Monitors Claude Code session token consumption in real-time.
Pulls ground-truth usage data from Anthropic when available,
falls back to local JSONL parsing from ~/.claude/.

Data sources (in priority order):
  1. ~/.anthropic-usage/usage-clean.json       — Anthropic-reported session/weekly %
  2. ~/.claude/projects/**/<sessionId>.jsonl    — per-message model + token counts
  3. ~/.claude/stats-cache.json                 — daily aggregates
  4. System status cache (configurable)         — disk/load/network/services
  5. /proc/*                                    — live system metrics

Paths can be overridden via environment variables:
  TB_SYSTEM_CACHE  — path to system stats JSON (optional, for extended disk/network/service data)
  TB_USAGE_FILE    — path to Anthropic usage JSON (default: ~/.anthropic-usage/usage-clean.json)
  TB_SERVICES      — comma-separated extra systemd services to monitor

Run:  tb
Deps: pip install textual rich
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Static
from rich.text import Text

# ─────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────
CLAUDE_DIR    = Path.home() / ".claude"
STATS_CACHE   = CLAUDE_DIR / "stats-cache.json"
SESSIONS_DIR  = CLAUDE_DIR / "sessions"
PROJECTS_DIR  = CLAUDE_DIR / "projects"
HISTORY_FILE  = Path.home() / ".tokenburn_history.jsonl"
CONFIG_FILE   = Path.home() / ".tokenburn.json"
SYSTEM_CACHE  = Path(os.environ.get("TB_SYSTEM_CACHE",
                     str(Path.home() / ".tokenburn-system-stats.json")))
USAGE_CLEAN   = Path(os.environ.get("TB_USAGE_FILE",
                     str(Path.home() / ".anthropic-usage" / "usage-clean.json")))
USAGE_HISTORY = Path(os.environ.get("TB_USAGE_HISTORY",
                     str(Path.home() / ".anthropic-usage" / "usage-history.jsonl")))

# ─────────────────────────────────────────────────────────────
# Default config — multi-account ready
# ─────────────────────────────────────────────────────────────
DEFAULT_CFG: dict = {
    "active_account": "A",
    "accounts": [
        {
            "id":              "A",
            "name":            "Primary",
            "window_5h_limit": 900_000,   # input+output tokens (not cache)
            "window_7d_limit": 5_000_000,
            "target_pct_5h":   70,        # ideal utilisation %
        },
    ],
    "warn_pct":      60,
    "urgent_pct":    80,
    "refresh_secs":  20,
    "max_parallel":  10,
    "plan_name":     "Pro Max",
    # Sessions idle > this many minutes AND using > idle_mem_mb = flagged as orphan
    "orphan_idle_mins": 60,
    "orphan_mem_mb":    150,
}

MODEL_SHORT = {
    "claude-opus-4-6":           "opus:4",
    "claude-sonnet-4-6":         "sonnet:4",
    "claude-haiku-4-5-20251001": "haiku:4",
}

JSONL_CACHE_TTL = 45   # seconds — re-parse JSONL if mtime changed or this elapsed

# Additional services to check via systemctl (override with TB_SERVICES env var)
_svc_env = os.environ.get("TB_SERVICES", "")
EXTRA_SERVICES = [s.strip() for s in _svc_env.split(",") if s.strip()] if _svc_env else []


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def fmt_k(n: int | float) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def fmt_age(secs: float) -> str:
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def stars_str(n: int | float, total: int = 5) -> str:
    n = max(0, min(total, int(n)))
    return "★" * n + "☆" * (total - n)


def bar(pct: float, width: int,
        fill: str = "█", empty: str = "░",
        fill_style: str = "orange1", empty_style: str = "grey35") -> Text:
    pct = max(0.0, min(100.0, float(pct)))
    n   = max(0, min(width, int(round(pct / 100.0 * width))))
    t   = Text()
    if n:          t.append(fill  * n,          style=fill_style)
    if width - n:  t.append(empty * (width - n), style=empty_style)
    return t


def safe_div(a: float, b: float, fallback: float = 0.0) -> float:
    return a / b if b else fallback


def pct_color(pct: float, warn: float = 60, urgent: float = 80) -> str:
    return "red" if pct >= urgent else ("yellow" if pct >= warn else "green")


def _short_model(full: str) -> str:
    f = full.strip().lower()
    if "opus"  in f: return "opus"
    if "haiku" in f: return "haiku"
    return "sonnet"


# ─────────────────────────────────────────────────────────────
# JSONL parsing (the ground truth for per-session data)
# ─────────────────────────────────────────────────────────────
def _parse_jsonl(path: Path) -> dict:
    """
    Full parse of a session JSONL file. Returns:
      model       — most recently used model (short name)
      model_full  — full model string
      slug        — human-readable slug (last seen)
      input_toks  — total input tokens (all time)
      output_toks — total output tokens (all time)
      toks_5h     — input+output tokens in the last 5 hours
      last_ts     — float Unix timestamp of most recent message
      messages    — count of assistant messages parsed
    """
    cutoff_5h = time.time() - 5 * 3600
    result = {
        "model":            "sonnet",
        "model_full":       "",
        "slug":             "",
        "input_toks":       0,
        "output_toks":      0,
        "cache_read_toks":  0,
        "cache_create_toks": 0,
        "toks_5h":          0,
        "last_ts":          0.0,
        "messages":         0,
    }
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                if entry.get("type") != "assistant":
                    continue

                msg   = entry.get("message", {})
                usage = msg.get("usage", {})
                inp   = int(usage.get("input_tokens",  0))
                out   = int(usage.get("output_tokens", 0))
                cread = int(usage.get("cache_read_input_tokens", 0))
                ccrea = int(usage.get("cache_creation_input_tokens", 0))

                result["input_toks"]       += inp
                result["output_toks"]      += out
                result["cache_read_toks"]  += cread
                result["cache_create_toks"] += ccrea
                result["messages"]         += 1

                ts_str = entry.get("timestamp", "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")).timestamp()
                        if ts > result["last_ts"]:
                            result["last_ts"] = ts
                        if ts >= cutoff_5h:
                            result["toks_5h"] += inp + out
                    except Exception:
                        pass

                mfull = msg.get("model", "")
                if mfull:
                    result["model_full"] = mfull
                    result["model"]      = _short_model(mfull)

                slug = entry.get("slug", "")
                if slug:
                    result["slug"] = slug
    except Exception:
        pass
    return result


def _find_jsonl(session_id: str) -> Optional[Path]:
    """Locate a session's JSONL file across all project directories."""
    matches = list(PROJECTS_DIR.glob(f"**/{session_id}.jsonl"))
    return matches[0] if matches else None


# ─────────────────────────────────────────────────────────────
# Data layer
# ─────────────────────────────────────────────────────────────
class TokenData:

    def __init__(self):
        self.config    = self._load_config()
        self._acct     = self._active_account()
        # JSONL cache: session_id -> (mtime, parsed_result)
        self._jcache:  dict[str, tuple[float, dict]] = {}
        # Per-refresh-cycle cache — prevents 7x active_sessions() re-parse
        self._cycle:   dict[str, object] = {}
        self._take_snapshot()

    def _begin_refresh(self):
        """Call once at the start of each refresh cycle to clear per-cycle cache."""
        self._cycle.clear()

    # ── Anthropic usage (ground truth from browser scraper) ─
    def anthropic_usage(self) -> dict:
        """
        Read scraped Anthropic usage data from usage-clean.json.
        Returns dict with profile data or empty if unavailable/stale.
        Cache per cycle.
        """
        if "anthropic_usage" in self._cycle:
            return self._cycle["anthropic_usage"]

        result: dict = {"available": False, "stale": True, "profiles": {}}
        try:
            if not USAGE_CLEAN.exists():
                self._cycle["anthropic_usage"] = result
                return result
            raw     = json.loads(USAGE_CLEAN.read_text())
            last_ts = raw.get("lastCheck", "")
            if last_ts:
                from datetime import timezone
                last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                age_sec = (datetime.now(timezone.utc) - last_dt).total_seconds()
                result["stale"]    = age_sec > 7200   # >2h = stale
                result["age_mins"] = age_sec / 60
            result["available"] = True
            result["lastCheck"] = last_ts

            for pname, pdata in raw.get("profiles", {}).items():
                cs  = pdata.get("currentSession", {})
                wk  = pdata.get("weeklyLimits", {})
                wka = wk.get("allModels", {})
                wks = wk.get("sonnetOnly", {})
                ext = pdata.get("extraUsage", {})

                # Parse reset times
                session_reset = cs.get("resetsAt")
                weekly_reset  = wka.get("resetsAt")
                sonnet_reset  = wks.get("resetsAt")

                # Compute time-to-reset
                session_reset_mins = self._mins_until_reset(session_reset, 300)
                weekly_reset_mins  = self._mins_until_reset(weekly_reset, 10080)

                result["profiles"][pname] = {
                    "account":            pdata.get("account", "?"),
                    "plan":               pdata.get("plan", "?"),
                    "session_pct":        cs.get("percentUsed", 0),
                    "session_reset":      session_reset,
                    "session_reset_mins": session_reset_mins,
                    "weekly_all_pct":     wka.get("percentUsed", 0),
                    "weekly_all_reset":   weekly_reset,
                    "weekly_reset_mins":  weekly_reset_mins,
                    "weekly_sonnet_pct":  wks.get("percentUsed", 0),
                    "weekly_sonnet_reset": sonnet_reset,
                    "extra_spent":        ext.get("spent", 0),
                    "extra_limit":        ext.get("monthlyLimit", 0),
                    "extra_pct":          ext.get("percentUsed", 0),
                }
        except Exception:
            pass
        self._cycle["anthropic_usage"] = result
        return result

    @staticmethod
    def _mins_until_reset(iso_str, window_mins: float = 300) -> Optional[float]:
        """
        Minutes until the next reset. If the scraped reset time is in the past,
        roll forward by window_mins (default 300 = 5h) to estimate when the
        *current* window resets — the session window is rolling.
        """
        if not iso_str or iso_str == "unknown":
            return None
        try:
            from datetime import timezone
            reset_dt = datetime.fromisoformat(str(iso_str))
            if reset_dt.tzinfo is None:
                reset_dt = reset_dt.replace(tzinfo=timezone.utc)
            now  = datetime.now(timezone.utc)
            diff = (reset_dt - now).total_seconds() / 60.0
            if diff >= 0:
                return diff
            # Reset time is in the past — roll forward by window periods
            elapsed_past = -diff  # minutes since that reset
            periods_past = elapsed_past / window_mins
            # Next reset = original + ceil(periods_past) * window
            import math
            next_reset_mins = (math.ceil(periods_past) * window_mins) - elapsed_past
            return max(0.0, next_reset_mins)
        except Exception:
            return None

    # ── advisory engine (alerts + recommendations) ─────────
    def advisories(self) -> list[dict]:
        """
        Generate actionable alerts and recommendations based on real Anthropic usage.
        Each advisory: {level: "info"|"warn"|"urgent"|"action", msg: str}
        """
        if "advisories" in self._cycle:
            return self._cycle["advisories"]

        ads: list[dict] = []
        au  = self.anthropic_usage()
        if not au.get("available"):
            ads.append({"level": "warn",
                        "msg": "No Anthropic usage data — scraper may be down"})
            self._cycle["advisories"] = ads
            return ads

        if au.get("stale"):
            ads.append({"level": "warn",
                        "msg": f"Usage data stale ({au.get('age_mins', 0):.0f}m old)"})

        prof = au["profiles"].get("default")
        if not prof:
            self._cycle["advisories"] = ads
            return ads

        sp  = prof["session_pct"]
        wp  = prof["weekly_all_pct"]
        snp = prof["weekly_sonnet_pct"]
        sr  = prof.get("session_reset_mins")
        wr  = prof.get("weekly_reset_mins")

        # ── Session (5h window) alerts ─────────────────────
        if sp >= 90:
            ads.append({"level": "urgent",
                        "msg": f"Session {sp}% — near limit, pause heavy tasks"})
        elif sp >= 70:
            if sr and sr < 60:
                ads.append({"level": "info",
                            "msg": f"Session {sp}% — resets in {sr:.0f}m, coast it out"})
            else:
                ads.append({"level": "warn",
                            "msg": f"Session {sp}% — pace yourself, "
                                   f"resets in {sr:.0f}m" if sr else ""})

        # Session under-utilization
        if sp < 30 and sr and sr < 90:
            headroom = 100 - sp
            ads.append({"level": "action",
                        "msg": f"Session {sp}% with {sr:.0f}m left — "
                               f"{headroom}% available, spin up more work!"})

        # ── Weekly balance alerts ──────────────────────────
        if wr is not None and wr > 0:
            days_left    = wr / (60 * 24)
            ideal_daily  = (100 - wp) / max(0.5, days_left)
            current_pace = self._weekly_daily_pace(wp, wr)

            if wp >= 85:
                ads.append({"level": "urgent",
                            "msg": f"Weekly {wp}% — {days_left:.1f}d remain, "
                                   f"ration to ~{ideal_daily:.0f}%/day"})
            elif wp >= 65:
                ads.append({"level": "warn",
                            "msg": f"Weekly {wp}% — target ~{ideal_daily:.0f}%/day "
                                   f"over {days_left:.1f}d remaining"})
            elif wp < 40 and days_left < 4:
                ads.append({"level": "action",
                            "msg": f"Weekly only {wp}% with {days_left:.1f}d left — "
                                   f"burn ~{ideal_daily:.0f}%/day to use allocation"})
            elif wp < 20 and days_left < 5:
                ads.append({"level": "action",
                            "msg": f"Weekly {wp}% — lots of headroom, "
                                   f"ramp up to ~{ideal_daily:.0f}%/day"})

            # Pace check
            if current_pace is not None:
                if current_pace > ideal_daily * 1.5 and wp > 50:
                    ads.append({"level": "warn",
                                "msg": f"Burning {current_pace:.0f}%/day vs "
                                       f"ideal {ideal_daily:.0f}%/day — slow down"})
                elif current_pace < ideal_daily * 0.5 and wp < 60:
                    ads.append({"level": "info",
                                "msg": f"Burning {current_pace:.0f}%/day vs "
                                       f"ideal {ideal_daily:.0f}%/day — room to push harder"})

        # ── Sonnet-specific ────────────────────────────────
        if snp > wp + 20:
            ads.append({"level": "info",
                        "msg": f"Sonnet {snp}% vs all-model {wp}% — "
                               f"consider switching some work to Opus"})

        # ── Active sessions vs usage ───────────────────────
        sessions = self.active_sessions()
        if not sessions and sp < 80 and (sr is None or sr > 30):
            ads.append({"level": "action",
                        "msg": "No active sessions — tokens going unused!"})
        elif len(sessions) == 1 and sp < 40 and (sr is None or sr > 60):
            ads.append({"level": "info",
                        "msg": "Single session, low usage — spin up parallel work"})

        self._cycle["advisories"] = ads
        return ads

    @staticmethod
    def _weekly_daily_pace(current_pct: float, mins_left: float) -> Optional[float]:
        """Estimate current daily burn pace from weekly pct and time remaining."""
        if mins_left <= 0:
            return None
        total_mins = 7 * 24 * 60
        elapsed    = total_mins - mins_left
        if elapsed < 60:
            return None
        days_elapsed = elapsed / (60 * 24)
        return current_pct / days_elapsed if days_elapsed > 0.1 else None

    # ── config ─────────────────────────────────────────────
    def _load_config(self) -> dict:
        if CONFIG_FILE.exists():
            try:
                return {**DEFAULT_CFG, **json.loads(CONFIG_FILE.read_text())}
            except Exception:
                pass
        return json.loads(json.dumps(DEFAULT_CFG))

    def _active_account(self) -> dict:
        aid = self.config.get("active_account", "A")
        for a in self.config.get("accounts", []):
            if a.get("id") == aid:
                return a
        return self.config.get("accounts", [DEFAULT_CFG["accounts"][0]])[0]

    def switch_account(self, acct_id: str):
        self.config["active_account"] = acct_id
        self._acct = self._active_account()

    def all_accounts(self) -> list[dict]:
        return self.config.get("accounts", [])

    # ── JSONL access ───────────────────────────────────────
    def _jsonl_for(self, session_id: str) -> Optional[dict]:
        """Return parsed JSONL data for a session, with mtime caching."""
        path = _find_jsonl(session_id)
        if not path:
            return None
        try:
            mtime = path.stat().st_mtime
        except Exception:
            return None
        cached = self._jcache.get(session_id)
        if cached and cached[0] == mtime:
            return cached[1]
        data = _parse_jsonl(path)
        self._jcache[session_id] = (mtime, data)
        return data

    # ── snapshot history ───────────────────────────────────
    def _load_stats(self) -> dict:
        try:
            return json.loads(STATS_CACHE.read_text())
        except Exception:
            return {}

    def _model_totals(self) -> dict[str, int]:
        stats = self._load_stats()
        return {m: d.get("inputTokens", 0) + d.get("outputTokens", 0)
                for m, d in stats.get("modelUsage", {}).items()}

    def _grand_total(self) -> int:
        """Grand total from stats-cache + live JSONL 5h data (whichever is larger)."""
        cache_total = sum(self._model_totals().values())
        live_total  = self._live_5h_total()
        return max(cache_total, live_total)

    def _take_snapshot(self):
        snap = {"ts": time.time(), "grand": self._grand_total(),
                "by": self._model_totals()}
        try:
            with open(HISTORY_FILE, "a") as f:
                f.write(json.dumps(snap) + "\n")
            self._prune_history()
        except Exception:
            pass

    def _prune_history(self):
        cutoff = time.time() - 9 * 86400
        try:
            lines = HISTORY_FILE.read_text().strip().splitlines()
            kept  = [l for l in lines
                     if l.strip() and json.loads(l).get("ts", 0) >= cutoff]
            HISTORY_FILE.write_text("\n".join(kept) + ("\n" if kept else ""))
        except Exception:
            pass

    def snapshots(self) -> list:
        try:
            lines = HISTORY_FILE.read_text().strip().splitlines()
            return [json.loads(l) for l in lines if l.strip()]
        except Exception:
            return []

    # ── window calculations ────────────────────────────────
    def window_tokens(self, hours: float) -> int:
        cutoff = time.time() - hours * 3600
        snaps  = self.snapshots()
        if not snaps:
            return 0
        current  = snaps[-1]["grand"]
        before   = [s for s in snaps if s["ts"] < cutoff]
        baseline = before[-1]["grand"] if before else snaps[0]["grand"]
        return max(0, current - baseline)

    def window_limit(self, hours: float) -> int:
        return self._acct.get(f"window_{int(hours)}h_limit",
                              self.config.get(f"window_{int(hours)}h_limit", 0))

    def window_pct(self, hours: float) -> float:
        limit = self.window_limit(hours)
        return min(100.0, safe_div(self.window_tokens(hours), limit) * 100) if limit else 0.0

    def window_reset_str(self, hours: float) -> str:
        snaps  = self.snapshots()
        cutoff = time.time() - hours * 3600
        in_win = [s for s in snaps if s["ts"] >= cutoff]
        reset  = (in_win[0]["ts"] + hours * 3600) if in_win else (time.time() + hours * 3600)
        diff   = max(0.0, reset - time.time())
        h, m   = int(diff // 3600), int((diff % 3600) // 60)
        return f"{h}h{m:02d}m"

    def seven_day_reset_day(self) -> str:
        stats  = self._load_stats()
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        for e in sorted(stats.get("dailyModelTokens", []), key=lambda x: x.get("date", "")):
            if e.get("date", "") >= cutoff:
                try:
                    return (datetime.strptime(e["date"], "%Y-%m-%d")
                            + timedelta(days=7)).strftime("%a %b %d")
                except Exception:
                    pass
        return (datetime.now() + timedelta(days=7)).strftime("%a %b %d")

    # ── burn rate (live, from JSONL toks_5h) ─────────────
    def _live_5h_total(self) -> int:
        """Sum of toks_5h across all active sessions — live JSONL data."""
        return sum(s["toks_5h"] for s in self.active_sessions())

    def burn_rate_per_min(self, window_mins: int = 15) -> float:
        """
        Compute burn rate from snapshot deltas when stats-cache is fresh,
        else estimate from live 5h total / elapsed session time.
        """
        # Try snapshot-based first
        snaps  = self.snapshots()
        cutoff = time.time() - window_mins * 60
        recent = [s for s in snaps if s["ts"] >= cutoff]
        if len(recent) >= 2:
            dt    = (recent[-1]["ts"] - recent[0]["ts"]) / 60.0
            delta = recent[-1]["grand"] - recent[0]["grand"]
            if dt > 0.05 and delta > 0:
                return delta / dt

        # Fallback: live JSONL — total 5h tokens / avg session age (capped at 5h)
        sessions = self.active_sessions()
        if not sessions:
            return 0.0
        total_5h = sum(s["toks_5h"] for s in sessions)
        if total_5h == 0:
            return 0.0
        # Use the oldest active session's age (capped to 5h) as the time window
        max_age_mins = min(300.0, max(s["age_secs"] for s in sessions) / 60.0)
        return total_5h / max_age_mins if max_age_mins > 1 else 0.0

    def burn_pct_per_10min(self) -> float:
        """% of 5h window burned per 10 minutes, using Anthropic data when available."""
        au   = self.anthropic_usage()
        prof = au.get("profiles", {}).get("default")
        if prof and prof.get("session_reset_mins"):
            # Derive from real session pct / elapsed time
            sr_mins  = prof["session_reset_mins"]
            elapsed  = 300.0 - sr_mins  # 5h window = 300 min
            pct_used = prof["session_pct"]
            if elapsed > 1:
                return (pct_used / elapsed) * 10
        # Fallback to token rate
        limit = self.window_limit(5)
        return safe_div(self.burn_rate_per_min() * 10, limit) * 100 if limit else 0.0

    def mins_until_full(self) -> Optional[int]:
        au   = self.anthropic_usage()
        prof = au.get("profiles", {}).get("default")
        if prof:
            pct = prof["session_pct"]
        else:
            pct = self.window_pct(5)
        rate = self.burn_pct_per_10min() / 10  # pct/min
        return max(1, int((100.0 - pct) / rate)) if rate > 0 and pct < 100 else None

    def target_burn_pct(self) -> float:
        return float(self._acct.get("target_pct_5h", 70))

    def is_wasting(self) -> bool:
        au   = self.anthropic_usage()
        prof = au.get("profiles", {}).get("default")
        if prof:
            sr = prof.get("session_reset_mins")
            # Wasting = low session % with significant time elapsed in window
            if sr is not None and sr < 120 and prof["session_pct"] < 20:
                return True
            return False
        # Fallback to snapshot-based
        snaps  = self.snapshots()
        cutoff = time.time() - 3600
        if not [s for s in snaps if s["ts"] < cutoff]:
            return False
        return self.window_pct(5) < self.target_burn_pct() * 0.3

    # ── daily + 7d ─────────────────────────────────────────
    def today_stats(self) -> dict:
        stats  = self._load_stats()
        today  = datetime.now().strftime("%Y-%m-%d")
        result: dict = {"total": 0, "by_model": {}, "messages": 0, "sessions": 0}
        for e in stats.get("dailyModelTokens", []):
            if e.get("date") == today:
                for m, t in e.get("tokensByModel", {}).items():
                    result["by_model"][m] = t
                    result["total"]      += t
                break
        for e in stats.get("dailyActivity", []):
            if e.get("date") == today:
                result["messages"] = e.get("messageCount", 0)
                result["sessions"] = e.get("sessionCount", 0)
                break

        # If stats-cache is stale, derive from live JSONL
        if result["total"] == 0:
            sessions = self.active_sessions()
            by_model: dict[str, int] = {}
            for s in sessions:
                m = s.get("model_full") or s["model"]
                by_model[m] = by_model.get(m, 0) + s["toks_5h"]
            if any(v > 0 for v in by_model.values()):
                result["by_model"] = by_model
                result["total"]    = sum(by_model.values())
                result["sessions"] = len(sessions)
                result["messages"] = sum(1 for s in sessions if s["toks_5h"] > 0)
        return result

    def seven_day_total(self) -> int:
        stats  = self._load_stats()
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        return sum(sum(e.get("tokensByModel", {}).values())
                   for e in stats.get("dailyModelTokens", [])
                   if e.get("date", "") >= cutoff)

    def daily_history(self, days: int = 7) -> list[tuple[str, int]]:
        stats   = self._load_stats()
        entries = {e["date"]: sum(e.get("tokensByModel", {}).values())
                   for e in stats.get("dailyModelTokens", [])}
        return [(
            (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")[5:],
            entries.get((datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d"), 0),
        ) for i in range(days - 1, -1, -1)]

    def burndown_slots(self, hours: int = 5) -> list[tuple[str, int]]:
        snaps = self.snapshots()
        now   = time.time()
        slots = []
        for h in range(hours - 1, -1, -1):
            s_start = now - (h + 1) * 3600
            s_end   = now - h * 3600
            in_slot = [s for s in snaps if s_start <= s["ts"] < s_end]
            before  = [s for s in snaps if s["ts"] < s_start]
            if in_slot and before:
                delta = in_slot[-1]["grand"] - before[-1]["grand"]
            elif len(in_slot) >= 2:
                delta = in_slot[-1]["grand"] - in_slot[0]["grand"]
            elif in_slot and not before and len(snaps) >= 2:
                delta = in_slot[-1]["grand"] - snaps[0]["grand"]
            else:
                delta = 0
            slots.append((datetime.fromtimestamp(s_start).strftime("%H:%M"),
                          max(0, delta)))
        return slots

    # ── sessions + JSONL truth ─────────────────────────────
    def active_sessions(self) -> list[dict]:
        """
        Returns active sessions enriched with JSONL data.
        Cached per refresh cycle — safe to call multiple times per tick.
        """
        if "active_sessions" in self._cycle:
            return self._cycle["active_sessions"]
        result = self._active_sessions_inner()
        self._cycle["active_sessions"] = result
        return result

    def _active_sessions_inner(self) -> list[dict]:
        result = []
        if not SESSIONS_DIR.exists():
            return result

        for f in sorted(SESSIONS_DIR.glob("*.json"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                data = json.loads(f.read_text())
                pid  = data.get("pid")
                if not pid:
                    continue
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError):
                    continue

                started_sec = data.get("startedAt", 0) / 1000.0
                age_secs    = max(0.0, time.time() - started_sec)
                session_id  = data.get("sessionId", "")

                # Memory
                mem_mb = 0
                try:
                    with open(f"/proc/{pid}/status") as pf:
                        for line in pf:
                            if line.startswith("VmRSS:"):
                                mem_mb = int(line.split()[1]) // 1024
                                break
                except Exception:
                    pass

                # Process name
                proc_name = "claude"
                try:
                    with open(f"/proc/{pid}/comm") as pf:
                        proc_name = pf.read().strip()
                except Exception:
                    pass

                # JSONL truth — model, tokens, slug, last activity
                jdata = self._jsonl_for(session_id) or {}

                model      = jdata.get("model", "sonnet")
                model_full = jdata.get("model_full", "")
                slug       = jdata.get("slug", "")
                total_toks = jdata.get("input_toks", 0) + jdata.get("output_toks", 0)
                toks_5h    = jdata.get("toks_5h", 0)
                last_ts    = jdata.get("last_ts", started_sec)
                idle_secs  = max(0.0, time.time() - last_ts) if last_ts else age_secs

                # Cache efficiency — cache_read / total_context (input + cache_read)
                # input_tokens is fresh (non-cached) input; cache_read is reused input
                cache_read    = jdata.get("cache_read_toks", 0)
                cache_create  = jdata.get("cache_create_toks", 0)
                fresh_input   = jdata.get("input_toks", 0)
                total_context = fresh_input + cache_read
                cache_pct     = safe_div(cache_read, total_context) * 100 if total_context else 0.0

                # Cwd + directive
                raw_cwd   = data.get("cwd", "?")
                short_cwd = raw_cwd.replace(str(Path.home()), "~", 1)
                project   = Path(short_cwd).name or short_cwd
                directive = data.get("name", "") or slug or short_cwd
                if len(directive) > 38:
                    directive = "…" + directive[-37:]

                result.append({
                    "session_id":   session_id[:8],
                    "full_sid":     session_id,
                    "pid":          pid,
                    "proc_name":    proc_name,
                    "name":         data.get("name", ""),
                    "slug":         slug,
                    "cwd":          short_cwd,
                    "project":      project,
                    "directive":    directive,
                    "started_at":   started_sec,
                    "age_secs":     age_secs,
                    "idle_secs":    idle_secs,
                    "mem_mb":       mem_mb,
                    "model":        model,
                    "model_full":   model_full,
                    "total_toks":   total_toks,
                    "toks_5h":      toks_5h,
                    "cache_read":   cache_read,
                    "cache_create": cache_create,
                    "cache_pct":    cache_pct,
                    "kind":         data.get("entrypoint", "cli"),
                    "acct":         self.config.get("active_account", "A"),
                })
            except Exception:
                pass
        return result

    # ── orphan detection ───────────────────────────────────
    def orphan_info(self) -> dict:
        """
        Returns dict with:
          stale_files  — .json session files whose PID is dead
          idle_heavy   — alive sessions idle > orphan_idle_mins AND mem > orphan_mem_mb
          zombies      — processes in Z state (all users, not just claude)
        """
        idle_thresh  = self.config.get("orphan_idle_mins", 60) * 60
        mem_thresh   = self.config.get("orphan_mem_mb", 150)
        stale_files  = []
        idle_heavy   = []

        if SESSIONS_DIR.exists():
            for f in SESSIONS_DIR.glob("*.json"):
                try:
                    data = json.loads(f.read_text())
                    pid  = data.get("pid")
                    if not pid:
                        continue

                    alive = True
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        alive = False
                    except PermissionError:
                        alive = True

                    sid      = data.get("sessionId", "")
                    raw_cwd  = data.get("cwd", "?")
                    short_cwd = raw_cwd.replace(str(Path.home()), "~", 1)

                    if not alive:
                        started = data.get("startedAt", 0) / 1000
                        stale_files.append({
                            "session_id": sid[:8],
                            "cwd":        short_cwd,
                            "age_str":    fmt_age(max(0, time.time() - started)),
                        })
                    else:
                        # Check memory
                        mem_mb = 0
                        try:
                            with open(f"/proc/{pid}/status") as pf:
                                for line in pf:
                                    if line.startswith("VmRSS:"):
                                        mem_mb = int(line.split()[1]) // 1024
                                        break
                        except Exception:
                            pass

                        if mem_mb >= mem_thresh:
                            # Determine idle time from JSONL
                            jdata    = self._jsonl_for(sid) or {}
                            last_ts  = jdata.get("last_ts", 0.0)
                            idle_sec = max(0.0, time.time() - last_ts) if last_ts else 0.0

                            if idle_sec >= idle_thresh:
                                idle_heavy.append({
                                    "session_id": sid[:8],
                                    "pid":        pid,
                                    "mem_mb":     mem_mb,
                                    "idle_secs":  idle_sec,
                                    "cwd":        short_cwd,
                                    "model":      jdata.get("model", "?"),
                                    "slug":       jdata.get("slug", ""),
                                })
                except Exception:
                    pass

        # Zombie processes from /proc
        zombies: list[dict] = []
        try:
            for pid_dir in Path("/proc").iterdir():
                if not pid_dir.name.isdigit():
                    continue
                try:
                    stat = (pid_dir / "stat").read_text()
                    # Format: pid (comm) state ...
                    rp = stat.split(")")
                    if len(rp) >= 2 and rp[1].split()[0] == "Z":
                        comm = rp[0].split("(", 1)[1] if "(" in rp[0] else "?"
                        zombies.append({"pid": int(pid_dir.name), "name": comm})
                except Exception:
                    pass
        except Exception:
            pass

        reclaimable_mb = sum(o["mem_mb"] for o in idle_heavy)
        return {
            "stale_files": stale_files, "idle_heavy": idle_heavy,
            "zombies": zombies, "reclaimable_mb": reclaimable_mb,
        }

    # ── memory pressure ───────────────────────────────────
    def memory_pressure(self) -> dict:
        """
        Claude-specific memory pressure:
          claude_rss_mb — total RSS of all claude processes
          avail_mb      — system available memory
          pressure_pct  — claude_rss / avail * 100 (>100 = critical)
        """
        if "memory_pressure" in self._cycle:
            return self._cycle["memory_pressure"]

        sessions   = self.active_sessions()
        claude_rss = sum(s["mem_mb"] for s in sessions)

        avail_mb = 0
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        avail_mb = int(line.split()[1]) // 1024
                        break
        except Exception:
            pass

        pressure = safe_div(claude_rss, avail_mb) * 100 if avail_mb else 0.0
        result = {
            "claude_rss_mb": claude_rss,
            "avail_mb":      avail_mb,
            "pressure_pct":  pressure,
        }
        self._cycle["memory_pressure"] = result
        return result

    # ── system metrics ─────────────────────────────────────
    def system_metrics(self) -> dict:
        """Base /proc system metrics."""
        mu = mt = 0.0
        try:
            kv: dict[str, int] = {}
            with open("/proc/meminfo") as fh:
                for line in fh:
                    p = line.split()
                    if len(p) >= 2:
                        kv[p[0].rstrip(":")] = int(p[1])
            mt = kv.get("MemTotal", 0) / 1_048_576
            mu = (kv.get("MemTotal", 0) - kv.get("MemAvailable", 0)) / 1_048_576
        except Exception:
            pass

        cpu = 0.0
        try:
            def _rc():
                with open("/proc/stat") as fh:
                    p = fh.readline().split()
                v = [int(x) for x in p[1:8]]
                return sum(v), v[3]
            t1, i1 = _rc()
            time.sleep(0.08)
            t2, i2 = _rc()
            if (dt := t2 - t1):
                cpu = 100.0 * (1.0 - safe_div(i2 - i1, dt))
        except Exception:
            pass

        return {"mem_used_gb": mu, "mem_total_gb": mt, "cpu_pct": cpu}

    def system_extended(self) -> dict:
        """
        Full system metrics: base /proc + disk + load + network + uptime + services.
        Reads system status cache when fresh (<5min old), else falls back to /proc.
        """
        base   = self.system_metrics()
        result = dict(base)
        result.setdefault("disk_used",    "?")
        result.setdefault("disk_total",   "?")
        result.setdefault("disk_pct",     "?%")
        result.setdefault("load_1m",      0.0)
        result.setdefault("load_5m",      0.0)
        result.setdefault("load_15m",     0.0)
        result.setdefault("uptime_hours", 0.0)
        result.setdefault("net_rx",       "?")
        result.setdefault("net_tx",       "?")
        result.setdefault("services",     [])

        # 1. System status cache (freshest data, <5 min)
        if SYSTEM_CACHE.exists():
            try:
                cache_age = time.time() - SYSTEM_CACHE.stat().st_mtime
                if cache_age < 300:
                    d = json.loads(SYSTEM_CACHE.read_text())
                    result["disk_used"]    = d.get("disk", {}).get("used",    "?")
                    result["disk_total"]   = d.get("disk", {}).get("size",    "?")
                    result["disk_pct"]     = d.get("disk", {}).get("pct",     "?%")
                    result["load_1m"]      = d.get("cpu",  {}).get("load_1m",  0.0)
                    result["load_5m"]      = d.get("cpu",  {}).get("load_5m",  0.0)
                    result["load_15m"]     = d.get("cpu",  {}).get("load_15m", 0.0)
                    result["uptime_hours"] = d.get("uptime_hours", 0.0)
                    result["net_rx"]       = str(d.get("network", {}).get("rx_gb", "?"))
                    result["net_tx"]       = str(d.get("network", {}).get("tx_gb", "?"))
                    result["services"]     = d.get("services", [])
                    self._check_extra_services(result)
                    return result
            except Exception:
                pass

        # 2. /proc fallbacks
        try:
            p = open("/proc/loadavg").read().split()
            result["load_1m"], result["load_5m"], result["load_15m"] = (
                float(p[0]), float(p[1]), float(p[2]))
        except Exception:
            pass
        try:
            result["uptime_hours"] = float(open("/proc/uptime").read().split()[0]) / 3600
        except Exception:
            pass
        try:
            with open("/proc/net/dev") as fh:
                for line in fh:
                    for iface in ("eth0", "ens", "enp"):
                        if iface in line:
                            p = line.split()
                            result["net_rx"] = f"{int(p[1]) / 1e9:.1f}"
                            result["net_tx"] = f"{int(p[9]) / 1e9:.1f}"
                            break
        except Exception:
            pass

        # Check additional services not in status cache
        self._check_extra_services(result)
        return result

    def _check_extra_services(self, result: dict):
        """Append extra service health via systemctl --user is-active."""
        import subprocess
        known_names = {s.get("name", "") for s in result.get("services", [])}
        for svc in EXTRA_SERVICES:
            if svc in known_names:
                continue
            try:
                r = subprocess.run(
                    ["systemctl", "--user", "is-active", svc],
                    capture_output=True, text=True, timeout=2)
                active = r.stdout.strip() == "active"
                result.setdefault("services", []).append({"name": svc, "active": active})
            except Exception:
                result.setdefault("services", []).append({"name": svc, "active": False})

    # ── score ──────────────────────────────────────────────
    def score(self) -> dict:
        sessions = self.active_sessions()
        rate     = self.burn_rate_per_min()

        # Use Anthropic session pct for burn score
        au   = self.anthropic_usage()
        prof = au.get("profiles", {}).get("default")
        pct5 = prof["session_pct"] if prof else self.window_pct(5)

        # Ship score: use live 5h JSONL tokens since stats-cache may be stale
        live_5h  = self._live_5h_total()
        # Model breadth: count distinct models across active sessions
        models   = {s["model"] for s in sessions if s["model"]}

        burn     = int(min(5, max(0, pct5 / 20.0)))
        parallel = int(min(5, len(sessions)))
        ship     = int(min(5, live_5h // 40_000))
        breadth  = int(min(5, len(models) + 1))
        velocity = int(min(5, rate / 300.0))
        total    = (burn + parallel + ship + breadth + velocity) / 5.0

        return {"burn": burn, "parallel": parallel, "ship": ship,
                "breadth": breadth, "velocity": velocity,
                "total": round(total, 1), "stars": int(round(total))}

    # ── allocation breakdown (JSONL-based) ─────────────────
    def session_token_share(self) -> list[tuple[dict, float]]:
        """
        (session, pct_of_5h_total) — uses actual JSONL token counts.
        Falls back to memory ratio if JSONL has no 5h data.
        """
        sessions   = self.active_sessions()
        total_5h   = sum(s["toks_5h"]  for s in sessions)
        total_mem  = sum(s["mem_mb"]    for s in sessions) or 1

        if total_5h > 0:
            return [(s, safe_div(s["toks_5h"], total_5h) * 100) for s in sessions]
        # Fallback: memory proxy
        return [(s, safe_div(s["mem_mb"], total_mem) * 100) for s in sessions]


# ─────────────────────────────────────────────────────────────
# Widgets
# ─────────────────────────────────────────────────────────────

class HeaderBar(Static):
    DEFAULT_CSS = """
    HeaderBar {
        height: 2;
        background: #111120;
        padding: 0 1;
    }
    """
    def update_bar(self, data: TokenData):
        sessions = data.active_sessions()
        sc       = data.score()
        rate     = data.burn_rate_per_min()
        max_p    = data.config.get("max_parallel", 10)
        plan     = data.config.get("plan_name", "Pro Max")
        warn     = data.config.get("warn_pct", 60)
        urg      = data.config.get("urgent_pct", 80)

        # Real Anthropic data (preferred), fallback to local estimates
        au   = data.anthropic_usage()
        prof = au.get("profiles", {}).get("default")

        if prof:
            pct5  = prof["session_pct"]
            pct7  = prof["weekly_all_pct"]
            sn_p  = prof["weekly_sonnet_pct"]
            sr_m  = prof.get("session_reset_mins")
            reset5 = f"{int(sr_m // 60)}h{int(sr_m % 60):02d}m" if sr_m is not None else "?"
            src    = "●" if not au.get("stale") else "○"
        else:
            pct5   = data.window_pct(5)
            pct7   = safe_div(data.seven_day_total(), data.window_limit(7)) * 100
            sn_p   = 0
            reset5 = data.window_reset_str(5)
            src    = "×"

        t = Text()
        t.append(" TB ", style="bold white on #1e1e38")
        t.append(f" {reset5} ", style="cyan")
        t.append(f" P{len(sessions)}/{max_p}  ", style="bold white")

        c5 = pct_color(pct5, warn, urg)
        t.append("5h ", style="dim"); t.append(bar(pct5, 8, fill_style=c5))
        t.append(f" {pct5:.0f}%  ", style=f"bold {c5}")

        c7 = pct_color(pct7, 50, 75)
        t.append("7d ", style="dim"); t.append(bar(pct7, 8, fill_style=c7))
        t.append(f" {pct7:.0f}%  ", style=f"bold {c7}")

        if sn_p:
            cs = pct_color(sn_p, 50, 75)
            t.append("sn ", style="dim"); t.append(bar(sn_p, 6, fill_style=cs))
            t.append(f" {sn_p:.0f}%  ", style=f"dim {cs}")

        t.append(f"rate:{rate:.0f}/min  ", style="dim white")
        t.append(f"{plan} ", style="dim")
        t.append(f"{src}", style="green" if src == "●" else "red")
        if data.is_wasting():
            t.append(f"  ⚠ WASTING", style="bold yellow")
        t.append("\n")

        t.append(f" {stars_str(sc['stars'])}  ", style="yellow")
        t.append(f"B:{sc['burn']} P:{sc['parallel']} "
                 f"S:{sc['ship']} Br:{sc['breadth']} V:{sc['velocity']}",
                 style="dim white")

        today = data.today_stats()
        if today["by_model"]:
            t.append("    ")
            for mk, toks in sorted(today["by_model"].items(), key=lambda x: -x[1])[:3]:
                short = MODEL_SHORT.get(mk, mk.split("-")[1])
                t.append(f"{short}:{fmt_k(toks)}  ", style="dim cyan")

        self.update(t)


class BurndownPanel(Static):
    DEFAULT_CSS = """
    BurndownPanel {
        height: 15;
        border: round #2a2a44;
        background: #0a0a14;
        padding: 0 1;
    }
    """
    def update_chart(self, data: TokenData):
        self.update(self._build(data))

    def _build(self, data: TokenData) -> Text:
        slots    = data.burndown_slots(5)
        limit    = data.window_limit(5)
        has_data = any(v > 0 for _, v in slots)
        warn     = data.config.get("warn_pct", 60)
        urg      = data.config.get("urgent_pct", 80)

        t = Text()
        t.append(" Token Burndown", style="bold cyan")
        t.append("  (5h window, hourly)\n", style="dim")

        if not has_data:
            t.append("\n  Building hourly data… Showing 7-day daily:\n\n", style="dim yellow")
            daily   = data.daily_history(7)
            max_val = max((v for _, v in daily), default=1) or 1
            for label, val in daily:
                pct  = safe_div(val, max_val) * 100
                lpct = safe_div(val, limit or max_val) * 100
                fc   = pct_color(lpct, warn, urg) if val else "grey30"
                t.append(f"  {label} ", style="grey50")
                t.append(bar(pct, 42, fill_style=fc))
                t.append(f"  {fmt_k(val)}\n", style="dim white")
            return t

        max_val = max((v for _, v in slots), default=1) or 1
        t.append("\n")
        for label, val in slots:
            pct_of_max = safe_div(val, max_val) * 100
            lim_pct    = safe_div(val, limit) * 100 if limit else 0
            fc = pct_color(lim_pct, warn, urg) if val else "grey30"
            t.append(f"  {label} ", style="grey50")
            t.append(bar(pct_of_max, 42, fill_style=fc))
            lim_str = f" {lim_pct:.1f}%" if limit else ""
            t.append(f"  {fmt_k(val)}{lim_str}\n", style="dim white")

        pct5  = data.window_pct(5)
        rate  = data.burn_rate_per_min()
        p10   = data.burn_pct_per_10min()
        mins  = data.mins_until_full()
        tgt   = data.target_burn_pct()
        t.append(f"\n  budget:{tgt:.0f}%  actual:{pct5:.1f}%  "
                 f"{p10:.2f}%/10m  {rate:.0f} tok/min", style="dim")
        if mins is not None:
            t.append(f"  →100% ~{mins}m",
                     style="bold red" if mins < 20 else ("yellow" if mins < 60 else "dim"))
        return t


class StatsPanel(Static):
    DEFAULT_CSS = """
    StatsPanel {
        width: 34;
        height: 15;
        border: round #2a2a44;
        background: #0a0a14;
        padding: 0 1;
    }
    """
    def update_stats(self, data: TokenData):
        self.update(self._build(data))

    def _build(self, data: TokenData) -> Text:
        au       = data.anthropic_usage()
        prof     = au.get("profiles", {}).get("default")
        rate     = data.burn_rate_per_min()
        mins     = data.mins_until_full()
        sc       = data.score()
        today    = data.today_stats()
        warn     = data.config.get("warn_pct", 60)
        urg      = data.config.get("urgent_pct", 80)
        max_p    = data.config.get("max_parallel", 10)
        sessions = data.active_sessions()

        # Use Anthropic data when available, else fallback
        if prof:
            pct5   = prof["session_pct"]
            pct7   = prof["weekly_all_pct"]
            snp    = prof["weekly_sonnet_pct"]
            sr_m   = prof.get("session_reset_mins")
            wr_m   = prof.get("weekly_reset_mins")
            reset5 = f"{int(sr_m//60)}h{int(sr_m%60):02d}m" if sr_m is not None else "?"
            wr_d   = wr_m / (60*24) if wr_m else 0
            reset7 = f"{wr_d:.1f}d" if wr_d else "?"
        else:
            pct5   = data.window_pct(5)
            pct7   = safe_div(data.seven_day_total(), data.window_limit(7)) * 100
            snp    = 0
            reset5 = data.window_reset_str(5)
            reset7 = data.seven_day_reset_day()
            wr_d   = 0

        left5 = max(0.0, 100.0 - pct5)
        left7 = max(0.0, 100.0 - pct7)

        t = Text()
        c5 = pct_color(pct5, warn, urg)
        t.append(f"\n  {pct5:.0f}%", style=f"bold {c5}")
        t.append(" Used  ", style="dim white")
        t.append(f"{left5:.0f}%", style="bold green")
        t.append(" Left\n\n", style="dim white")

        c5b  = pct_color(pct5, warn, urg)
        cool5 = "COOL" if pct5 < warn else ("WARN" if pct5 < urg else "HOT!")
        t.append("  5h  ", style="dim"); t.append(bar(pct5, 12, fill_style=c5b))
        t.append(f"  {pct5:.0f}%\n", style="dim white")
        t.append(f"       resets {reset5}  ", style="dim")
        t.append(cool5, style=c5b)
        t.append("\n")

        c7b  = pct_color(pct7, 50, 75)
        cool7 = "COOL" if pct7 < 50 else ("WARM" if pct7 < 75 else "HOT!")
        t.append("  7d  ", style="dim"); t.append(bar(pct7, 12, fill_style=c7b))
        t.append(f"  {pct7:.0f}%\n", style="dim white")
        t.append(f"       resets {reset7}  ", style="dim")
        t.append(cool7, style=c7b)
        t.append(f"  left:{left7:.0f}%\n", style="dim")

        if snp:
            cs = pct_color(snp, 50, 75)
            t.append("  sn  ", style="dim"); t.append(bar(snp, 12, fill_style=cs))
            t.append(f"  {snp:.0f}%\n", style="dim white")

        # Weekly budget advisor
        if prof and wr_d > 0:
            ideal_daily = left7 / max(0.5, wr_d)
            t.append(f"\n  Budget: ~{ideal_daily:.0f}%/day\n", style="dim cyan")

        t.append(f"  P: {len(sessions)}/{max_p}\n", style="cyan")

        if today["by_model"]:
            for m, toks in sorted(today["by_model"].items(), key=lambda x: -x[1])[:3]:
                short = MODEL_SHORT.get(m, m.split("-")[1])
                t.append(f"  {short:<10}", style="cyan")
                t.append(f" {fmt_k(toks)}\n", style="white")

        if data.is_wasting():
            t.append(f"\n  ⚠ WASTING ~{left5:.0f}%\n", style="bold yellow")
        p10 = data.burn_pct_per_10min()
        t.append(f"\n  {p10:.2f}%/10m  {rate:.0f} tok/min\n", style="dim yellow")
        if mins is not None:
            t.append(f"  100% in ~{mins}m\n",
                     style="bold red" if mins < 20 else ("yellow" if mins < 60 else "dim"))

        t.append(f"\n  {stars_str(sc['stars'])} {sc['total']:.1f}  ", style="yellow")
        t.append(f"B:{sc['burn']} P:{sc['parallel']} "
                 f"S:{sc['ship']} Br:{sc['breadth']} V:{sc['velocity']}\n", style="dim")
        return t


class AllocationPanel(Static):
    """
    'Who Ate My X%?' — shows real per-session token breakdown from JSONL.
    Top multicolor bar = proportional token share of 5h window.
    Each row = one session with actual toks_5h or total_toks.
    """
    DEFAULT_CSS = """
    AllocationPanel {
        height: 9;
        border: round #2a2a44;
        background: #0a0a14;
        padding: 0 1;
    }
    """
    COLORS = ["orange1", "green", "cyan", "magenta", "yellow",
              "blue", "red", "white", "chartreuse3", "deep_sky_blue1"]

    def update_alloc(self, data: TokenData):
        self.update(self._build(data))

    def _build(self, data: TokenData) -> Text:
        # Use real Anthropic session % for the title
        au   = data.anthropic_usage()
        prof = au.get("profiles", {}).get("default")
        pct5 = prof["session_pct"] if prof else data.window_pct(5)

        pairs = data.session_token_share()
        has_real_toks = any(s["toks_5h"] > 0 for s, _ in pairs)
        label = "5h tokens" if has_real_toks else "mem proxy"

        # Sort by share descending
        pairs.sort(key=lambda x: -x[1])

        t = Text()
        t.append(f" Who Ate My {pct5:.0f}%?", style="bold cyan")
        t.append(f"  ({label})", style="dim")

        # Show total context processed (cache is always ~99% for Claude, not useful as %)
        sessions  = data.active_sessions()
        total_ctx = sum(s.get("cache_read", 0) + s.get("total_toks", 0)
                        for s in sessions)
        if total_ctx > 0:
            t.append(f"  Ctx: {fmt_k(total_ctx)}", style="dim cyan")
        t.append("\n\n")

        if not pairs:
            t.append("  No active sessions\n", style="dim grey50")
            return t

        # Multicolor composite bar — only sessions with actual share
        bar_w  = 50
        pos    = 0
        active_pairs = [(s, p) for s, p in pairs if p > 0.5]
        t.append("  ")
        for i, (sess, pct) in enumerate(active_pairs[:8]):
            n = max(0, min(bar_w - pos, int(round(pct / 100.0 * bar_w))))
            if n:
                t.append("█" * n, style=self.COLORS[i % len(self.COLORS)])
                pos += n
        if pos < bar_w:
            t.append("░" * (bar_w - pos), style="grey35")
        t.append("\n\n")

        # Per-session rows (top 5)
        for i, (sess, pct) in enumerate(pairs[:5]):
            sid   = sess["session_id"]
            c     = self.COLORS[i % len(self.COLORS)]
            model = sess["model"]
            mem   = sess["mem_mb"]
            toks  = sess["toks_5h"] if has_real_toks else sess["total_toks"]
            tok_s = fmt_k(toks) + (" 5h" if has_real_toks else "")
            # Show cache context volume (more useful than uniform 99.9%)
            ctx   = sess.get("cache_read", 0) + sess.get("total_toks", 0)
            ctx_s = fmt_k(ctx) if ctx > 0 else ""
            name  = sess["slug"] or sess["name"] or sess["cwd"]
            if len(name) > 26:
                name = "…" + name[-25:]

            t.append(f"  {sid:<10}", style=f"bold {c}")
            n = max(0, min(20, int(round(pct / 100.0 * 20))))
            t.append("█" * n, style=c)
            t.append("░" * (20 - n), style="grey35")
            t.append(f"  {pct:.1f}%  {model}  {mem}MB  {tok_s}\n",
                     style="dim white")
            t.append(f"    {name}", style="dim grey70")
            if ctx_s:
                t.append(f"  ctx:{ctx_s}", style="dim grey50")
            t.append("\n")
        return t


class UrgentPanel(Static):
    DEFAULT_CSS = """
    UrgentPanel {
        height: 3;
        border: round #ff4081;
        background: #200010;
        padding: 0 1;
        display: none;
    }
    UrgentPanel.urgent { display: block; }
    """
    def update_urgent(self, data: TokenData):
        au       = data.anthropic_usage()
        prof     = au.get("profiles", {}).get("default")
        pct5     = prof["session_pct"] if prof else data.window_pct(5)
        rate     = data.burn_rate_per_min()
        urg_pct  = data.config.get("urgent_pct", 80)
        mins     = data.mins_until_full()
        sessions = data.active_sessions()

        top_name = ""
        if sessions:
            heaviest = max(sessions, key=lambda s: s["toks_5h"] or s["mem_mb"])
            top_name = heaviest["slug"] or heaviest["name"] or heaviest["session_id"]

        t   = Text()
        show = False
        if pct5 >= urg_pct:
            show = True
            t.append(f"\n  ⚠ RUNAWAY — {pct5:.0f}% consumed", style="bold #ff4081")
            if top_name:
                t.append(f"  Top: {top_name}", style="#ff4081")
            if mins is not None:
                t.append(f"  · full in ~{mins}m at {rate:.0f}/min", style="bold #ff4081")
        elif rate > 4000:
            show = True
            t.append(f"\n  ⚠ HIGH BURN — {rate:.0f} tok/min  "
                     f"{data.burn_pct_per_10min():.2f}%/10m  ·  {pct5:.0f}% used",
                     style="bold yellow")

        self.update(t)
        self.add_class("urgent") if show else self.remove_class("urgent")


class AdvisoryPanel(Static):
    """
    Smart recommendations based on Anthropic usage data.
    Shows color-coded advisories for token optimization.
    """
    DEFAULT_CSS = """
    AdvisoryPanel {
        height: auto;
        max-height: 7;
        border: round #2a6a44;
        background: #0a140a;
        padding: 0 1;
        display: none;
    }
    AdvisoryPanel.has-advice { display: block; }
    """
    LEVEL_STYLE = {
        "urgent": "bold red",
        "warn":   "bold yellow",
        "action": "bold green",
        "info":   "dim cyan",
    }
    LEVEL_ICON = {
        "urgent": "🔴",
        "warn":   "🟡",
        "action": "🟢",
        "info":   "💡",
    }

    def update_advisory(self, data: TokenData):
        ads = data.advisories()
        t   = Text()
        if ads:
            t.append(" Advisor\n", style="bold cyan")
            for a in ads[:4]:
                icon  = self.LEVEL_ICON.get(a["level"], "·")
                style = self.LEVEL_STYLE.get(a["level"], "dim")
                t.append(f"  {icon} {a['msg']}\n", style=style)
        self.update(t)
        if ads:
            self.add_class("has-advice")
        else:
            self.remove_class("has-advice")


class SystemPanel(Static):
    """
    Expanded system status:
      Row 1: MEM bar + CPU bar
      Row 2: Disk + Load averages + Uptime
      Row 3: Network I/O
      Row 4: Service health dots
      Row 5-7: Orphan / idle-heavy session alerts
    """
    DEFAULT_CSS = """
    SystemPanel {
        height: 13;
        border: round #2a2a44;
        background: #0a0a14;
        padding: 0 1;
    }
    """
    def update_sys(self, data: TokenData):
        self.update(self._build(data))

    def _build(self, data: TokenData) -> Text:
        m        = data.system_extended()
        sessions = data.active_sessions()
        orphans  = data.orphan_info()
        mp       = data.memory_pressure()
        mu       = m["mem_used_gb"]
        mt       = m["mem_total_gb"]
        cpu      = m["cpu_pct"]
        mem_pct  = safe_div(mu, mt) * 100
        t        = Text()

        t.append(" System Status", style="bold cyan")

        # Memory pressure alert inline with title
        pp = mp["pressure_pct"]
        if pp >= 100:
            t.append(f"  MEMORY CRITICAL {pp:.0f}%", style="bold red")
        elif pp >= 70:
            t.append(f"  MEM PRESSURE {pp:.0f}%", style="bold yellow")
        t.append("\n\n")

        # ── Row 1: MEM + CPU ──────────────────────────────
        mc  = pct_color(mem_pct, 70, 88)
        cc  = pct_color(cpu,     70, 88)
        mst = "COOL" if mem_pct < 70 else ("WARM" if mem_pct < 88 else "HOT!")
        cst = "COOL" if cpu     < 70 else ("BUSY" if cpu     < 88 else "HOT!")

        t.append("  MEM ", style="dim")
        t.append(bar(mem_pct, 16, fill_style=mc))
        t.append(f"  {mu:.1f}/{mt:.0f}GB  ", style="dim white")
        t.append(mst, style=mc)
        t.append("    CPU ", style="dim")
        t.append(bar(cpu, 10, fill_style=cc))
        t.append(f"  {cpu:.0f}%  ", style="dim white")
        t.append(cst, style=cc)
        t.append(f"    Sessions: ", style="dim")
        t.append(str(len(sessions)), style="bold cyan")
        t.append("\n")

        # ── Row 2: Claude memory + pressure ───────────────
        crss    = mp["claude_rss_mb"]
        avail   = mp["avail_mb"]
        pc      = "red" if pp >= 100 else ("yellow" if pp >= 70 else "green")
        t.append(f"  Claude: {crss}MB RSS", style="dim white")
        t.append(f"  Avail: {avail}MB", style="dim white")
        t.append(f"  Pressure: ", style="dim")
        t.append(f"{pp:.0f}%", style=f"bold {pc}")
        reclaim = orphans.get("reclaimable_mb", 0)
        if reclaim > 0:
            t.append(f"  ({reclaim}MB reclaimable)", style="yellow")
        t.append("\n")

        # ── Row 3: Disk + Load + Uptime ───────────────────
        dused = m.get("disk_used",    "?")
        dtot  = m.get("disk_total",   "?")
        dpct  = m.get("disk_pct",     "?%")
        l1    = m.get("load_1m",      0.0)
        l5    = m.get("load_5m",      0.0)
        up    = m.get("uptime_hours", 0.0)

        try:
            dpct_val = float(str(dpct).rstrip("%"))
            dc = pct_color(dpct_val, 75, 90)
        except Exception:
            dc = "dim"

        t.append(f"  Disk ", style="dim")
        t.append(f"{dused}/{dtot} ", style="dim white")
        t.append(f"({dpct})", style=dc)
        t.append(f"    Load: ", style="dim")
        lc1 = "red" if l1 > 4 else ("yellow" if l1 > 2 else "green")
        t.append(f"{l1:.2f}  {l5:.2f}", style=lc1)
        t.append(f"    Up: {up:.0f}h\n", style="dim")

        # ── Row 4: Network ────────────────────────────────
        rx    = m.get("net_rx", "?")
        tx    = m.get("net_tx", "?")
        t.append(f"  Net RX:{rx}GB  TX:{tx}GB\n", style="dim white")

        # ── Row 5: Services ───────────────────────────────
        services = m.get("services", [])
        if services:
            t.append("  Svc ", style="dim")
            for svc in services:
                name   = svc.get("name", "?")
                active = svc.get("active", False)
                dot    = "●" if active else "○"
                style  = "green" if active else "red"
                # Shorten common service name prefixes for display
                short = name
                for prefix, abbr in [("openclaw-", "oc-"), ("telegram-", "tg-")]:
                    short = short.replace(prefix, abbr)
                t.append(f"{dot}{short}  ", style=style)
            t.append("\n")

        # ── Rows 6+: Orphan alerts ────────────────────────
        idle_heavy = orphans.get("idle_heavy", [])
        stale      = orphans.get("stale_files", [])
        zombies    = orphans.get("zombies", [])

        if idle_heavy:
            t.append("  ⚠ Idle+heavy  ", style="bold yellow")
            for o in idle_heavy[:3]:
                idle_h = o["idle_secs"] / 3600
                t.append(
                    f"→ {o['session_id']} "
                    f"{o['mem_mb']}MB  idle {idle_h:.1f}h  "
                    f"{o['model']}  {o['cwd'][:22]}\n",
                    style="yellow")
        if stale:
            t.append(f"  ⚠ Stale PID files: {len(stale)}", style="dim red")
            for s in stale[:2]:
                t.append(f"  {s['session_id']} ({s['age_str']})", style="dim")
            t.append("\n")
        if zombies:
            t.append(f"  ⚠ Zombies: {len(zombies)} — "
                     + "  ".join(f"{z['name']}({z['pid']})" for z in zombies[:3]) + "\n",
                     style="red")

        return t


class EngineTable(DataTable):
    DEFAULT_CSS = """
    EngineTable {
        height: 1fr;
        min-height: 7;
        border: round #2a2a44;
        background: #0a0a14;
    }
    """
    COLS = [
        ("When",    8),
        ("Session", 10),
        ("Acct",    5),
        ("Src",     5),
        ("Model",   8),
        ("Mem",     6),
        ("Age",     9),
        ("Idle",    9),
        ("Toks(5h)",9),
        ("Ctx",     8),
        ("Directive", 21),
    ]

    def on_mount(self):
        for name, width in self.COLS:
            self.add_column(name, width=width)
        self.cursor_type = "row"

    def update_table(self, data: TokenData):
        self.clear()
        sessions = data.active_sessions()
        for s in sessions:
            when     = datetime.fromtimestamp(s["started_at"]).strftime("%H:%M:%S")
            sid      = s["session_id"]
            acct     = s["acct"]
            src      = s["kind"][:4]
            model    = s["model"]
            mem      = f"{s['mem_mb']}MB" if s["mem_mb"] else "?"
            age      = fmt_age(s["age_secs"])
            idle     = fmt_age(s["idle_secs"]) if s["idle_secs"] < s["age_secs"] - 30 else "active"
            toks5h   = fmt_k(s["toks_5h"]) if s["toks_5h"] else "—"
            ctx      = s.get("cache_read", 0) + s.get("total_toks", 0)
            ctx_s    = fmt_k(ctx) if ctx > 0 else "—"
            direct   = s["directive"]
            self.add_row(when, sid, acct, src, model, mem, age, idle, toks5h, ctx_s, direct)

        self.border_title = f" Engine Management (live) — {len(sessions)} "


# ─────────────────────────────────────────────────────────────
# Main App
# ─────────────────────────────────────────────────────────────
class TokenBurnApp(App):
    TITLE = "tokenburn — Claude Pro Max"
    CSS = """
    Screen       { background: #0a0a14; }
    #top-row     { height: 15; }
    #burndown    { width: 1fr; }
    #stats       { width: 36; }
    """
    BINDINGS = [
        Binding("q",            "quit",           "Quit",       show=True),
        Binding("r",            "refresh",        "Refresh",    show=True),
        Binding("e",            "export_csv",     "Export",     show=True),
        Binding("h",            "show_history",   "History",    show=True),
        Binding("c",            "show_config",    "Config",     show=True),
        Binding("s",            "show_score",     "Score",      show=True),
        Binding("a",            "switch_account", "Acct",       show=True),
        Binding("o",            "show_orphans",   "Orphans",    show=False),
        Binding("k",            "kill_orphans",   "Kill idle",  show=False),
        Binding("p",            "show_health",    "Health",     show=False),
        Binding("question_mark","show_help",      "Help",       show=False),
    ]

    def __init__(self):
        super().__init__()
        self._data = TokenData()

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header")
        with Horizontal(id="top-row"):
            yield BurndownPanel(id="burndown")
            yield StatsPanel(id="stats")
        yield AllocationPanel(id="alloc")
        yield UrgentPanel(id="urgent")
        yield AdvisoryPanel(id="advisor")
        yield SystemPanel(id="sysstat")
        yield EngineTable(id="engine")
        yield Footer()

    def on_mount(self):
        self.refresh_all()
        interval = self._data.config.get("refresh_secs", 20)
        self.set_interval(interval, self.refresh_all)

    def refresh_all(self):
        try:
            self._data._begin_refresh()
            self._data._take_snapshot()
            self.query_one("#header",   HeaderBar).update_bar(self._data)
            self.query_one("#burndown", BurndownPanel).update_chart(self._data)
            self.query_one("#stats",    StatsPanel).update_stats(self._data)
            self.query_one("#alloc",    AllocationPanel).update_alloc(self._data)
            self.query_one("#urgent",   UrgentPanel).update_urgent(self._data)
            self.query_one("#advisor",  AdvisoryPanel).update_advisory(self._data)
            self.query_one("#sysstat",  SystemPanel).update_sys(self._data)
            self.query_one("#engine",   EngineTable).update_table(self._data)
        except Exception as exc:
            self.notify(f"Refresh error: {exc}", title="Error", severity="error")

    # ── Actions ────────────────────────────────────────────
    def action_refresh(self):
        self.refresh_all()
        self.notify("Refreshed.", timeout=1)

    def action_export_csv(self):
        snaps = self._data.snapshots()
        out   = Path.home() / f"tokenburn_{datetime.now():%Y%m%d_%H%M%S}.csv"
        try:
            with open(out, "w") as f:
                f.write("timestamp,iso,grand_total\n")
                for s in snaps:
                    f.write(f"{int(s['ts'])},{datetime.fromtimestamp(s['ts']).isoformat()},{s['grand']}\n")
            self.notify(f"→ {out}", title="Exported", timeout=5)
        except Exception as exc:
            self.notify(str(exc), title="Export failed", severity="error")

    def action_show_history(self):
        daily = self._data.daily_history(7)
        lines = ["7-day token history\n"]
        for date, toks in daily:
            lines.append(f"  {date}  {fmt_k(toks):>7}  {'█' * min(20, toks // 20_000)}")
        self.notify("\n".join(lines), title="History", timeout=10)

    def action_show_config(self):
        a = self._data._acct
        lines = [
            f"Config: {CONFIG_FILE}",
            f"Account: {a.get('id')}  {a.get('name')}",
            f"  5h limit: {fmt_k(a.get('window_5h_limit', 0))}",
            f"  7d limit: {fmt_k(a.get('window_7d_limit', 0))}",
            f"  target:   {a.get('target_pct_5h', 70)}%",
            f"warn:{self._data.config.get('warn_pct')}%  "
            f"urgent:{self._data.config.get('urgent_pct')}%  "
            f"refresh:{self._data.config.get('refresh_secs')}s",
            "",
            "Edit ~/.tokenburn.json to adjust.",
        ]
        self.notify("\n".join(lines), title="Config", timeout=10)

    def action_show_score(self):
        s = self._data.score()
        lines = [
            "Optimization Score\n",
            f"  Burn     {'█'*s['burn']+'░'*(5-s['burn'])}  {s['burn']}/5",
            f"  Parallel {'█'*s['parallel']+'░'*(5-s['parallel'])}  {s['parallel']}/5",
            f"  Ship     {'█'*s['ship']+'░'*(5-s['ship'])}  {s['ship']}/5",
            f"  Breadth  {'█'*s['breadth']+'░'*(5-s['breadth'])}  {s['breadth']}/5",
            f"  Velocity {'█'*s['velocity']+'░'*(5-s['velocity'])}  {s['velocity']}/5",
            "",
            f"  Total    {stars_str(s['stars'])}  {s['total']:.1f}/5.0",
        ]
        self.notify("\n".join(lines), title="Score", timeout=10)

    def action_switch_account(self):
        accounts = self._data.all_accounts()
        if len(accounts) <= 1:
            self.notify("Only one account configured.\nAdd more in ~/.tokenburn.json",
                        title="Accounts", timeout=5)
            return
        cur  = self._data.config.get("active_account", "A")
        ids  = [a["id"] for a in accounts]
        next_id = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
        self._data.switch_account(next_id)
        self.refresh_all()
        self.notify(f"Switched to account {next_id}", timeout=2)

    def action_show_orphans(self):
        orphans = self._data.orphan_info()
        lines   = ["Orphan / Idle-Heavy Report\n"]
        ih = orphans["idle_heavy"]
        if ih:
            lines.append(f"  Idle+heavy ({len(ih)}):")
            for o in ih:
                lines.append(f"    {o['session_id']}  {o['mem_mb']}MB  "
                             f"idle {o['idle_secs']/3600:.1f}h  {o['model']}  {o['cwd']}")
        sf = orphans["stale_files"]
        if sf:
            lines.append(f"\n  Stale PID files ({len(sf)}):")
            for s in sf:
                lines.append(f"    {s['session_id']}  age:{s['age_str']}  {s['cwd']}")
        zb = orphans["zombies"]
        if zb:
            lines.append(f"\n  Zombie processes ({len(zb)}):")
            for z in zb:
                lines.append(f"    {z['name']} (pid {z['pid']})")
        if not ih and not sf and not zb:
            lines.append("  All clear — no orphans detected.")
        self.notify("\n".join(lines), title="Orphans", timeout=12)

    def action_kill_orphans(self):
        import signal
        orphans = self._data.orphan_info()
        ih      = orphans["idle_heavy"]
        if not ih:
            self.notify("No idle+heavy sessions to kill.", timeout=3)
            return
        killed = []
        for o in ih:
            try:
                os.kill(o["pid"], signal.SIGTERM)
                killed.append(f"{o['session_id']} ({o['mem_mb']}MB)")
            except Exception as exc:
                killed.append(f"{o['session_id']} FAIL: {exc}")
        # Clean stale PID files
        stale = orphans["stale_files"]
        cleaned = 0
        for s in stale:
            for f in SESSIONS_DIR.glob("*.json"):
                try:
                    d = json.loads(f.read_text())
                    if d.get("sessionId", "")[:8] == s["session_id"]:
                        f.unlink()
                        cleaned += 1
                except Exception:
                    pass
        lines = ["Killed idle+heavy sessions:"] + [f"  {k}" for k in killed]
        if cleaned:
            lines.append(f"\nCleaned {cleaned} stale PID files.")
        self.notify("\n".join(lines), title="Kill Orphans", timeout=8)
        self.refresh_all()

    def action_show_health(self):
        sys_m  = self._data.system_extended()
        snaps  = self._data.snapshots()
        lines  = [
            "Health Check\n",
            f"  Snapshots: {len(snaps)}",
            f"  Grand total: {fmt_k(self._data._grand_total())}",
            f"  Active sessions: {len(self._data.active_sessions())}",
            f"  Mem: {sys_m['mem_used_gb']:.1f}/{sys_m['mem_total_gb']:.0f}GB",
            f"  CPU: {sys_m['cpu_pct']:.0f}%",
            f"  Load: {sys_m.get('load_1m',0):.2f}  {sys_m.get('load_5m',0):.2f}",
            f"  Disk: {sys_m.get('disk_used','?')}/{sys_m.get('disk_total','?')} ({sys_m.get('disk_pct','?')})",
            f"  Uptime: {sys_m.get('uptime_hours',0):.1f}h",
            f"  Net RX:{sys_m.get('net_rx','?')}GB TX:{sys_m.get('net_tx','?')}GB",
            f"  History: {HISTORY_FILE.name}",
            f"  Stats date: {self._data._load_stats().get('lastComputedDate','?')}",
            f"  sys cache: {'found' if SYSTEM_CACHE.exists() else 'not found'}",
        ]
        self.notify("\n".join(lines), title="Health", timeout=12)

    def action_show_help(self):
        self.notify(
            "r  Refresh\ne  Export snapshot CSV\n"
            "h  7-day token history\nc  Config\n"
            "s  Score breakdown\na  Switch account\n"
            "o  Orphan/idle report\nk  Kill idle+heavy\n"
            "p  Health check\nq  Quit",
            title="Help", timeout=8)


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────
def main():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CFG, indent=2) + "\n")
        print(f"Created config: {CONFIG_FILE}")
    TokenBurnApp().run()


if __name__ == "__main__":
    main()
