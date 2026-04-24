#!/usr/bin/env python3
"""
tb-reaper — cooperative idle-session reaper for Claude Code.

Identifies Claude sessions that are idle (no tool activity for > IDLE_HOURS)
and not in the middle of a long-running task, then negotiates a cooperative
shutdown via filesystem sentinels before falling back to SIGTERM/SIGKILL.

Negotiation flow:
  1. Reaper finds idle candidate PID+SID
  2. Writes ~/.claude/reaper/pending-kill/<sid>
  3. Waits GRACE_SECS for session to either:
       (a) touch ~/.claude/reaper/keep-alive/<sid>  → abort
       (b) exit on its own
  4. SIGTERM, wait 10s, SIGKILL if still running

Runs as a user systemd timer. Logs every decision to ~/.tokenburn-reaper.log.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"
REAPER_DIR = CLAUDE_DIR / "reaper"
TASK_DIR = REAPER_DIR / "tasks"
PENDING_DIR = REAPER_DIR / "pending-kill"
KEEPALIVE_DIR = REAPER_DIR / "keep-alive"
LAST_ACTIVE_DIR = REAPER_DIR / "last-active"
PINNED_DIR = REAPER_DIR / "pinned"
LOG_FILE = HOME / ".tokenburn-reaper.log"
DIGEST_FILE = HOME / ".tokenburn-reaper-digest.json"

# Telegram notification (best-effort, fire-and-forget)
TG_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "39172309")

# Thresholds (overridable via env)
IDLE_HOURS = float(os.environ.get("TB_REAPER_IDLE_HOURS", "10"))
MIN_MEM_MB = float(os.environ.get("TB_REAPER_MIN_MEM_MB", "300"))
GRACE_SECS = int(os.environ.get("TB_REAPER_GRACE_SECS", "120"))
TASK_STALE_HOURS = float(os.environ.get("TB_REAPER_TASK_STALE_HOURS", "2"))
DRY_RUN = os.environ.get("TB_REAPER_DRY_RUN", "0") == "1"

# Overnight quiet window — reaper skips reaping during these local hours.
# Read G's current timezone from env (set in ~/.openclaw/.env, synced to
# systemd service via EnvironmentFile). Update G_TIMEZONE whenever G moves.
# Format: IANA timezone name, e.g. "Europe/Berlin" or "America/Los_Angeles".
_tz_name = os.environ.get("G_TIMEZONE", "UTC")
try:
    G_TZ = ZoneInfo(_tz_name)
except ZoneInfoNotFoundError:
    G_TZ = ZoneInfo("UTC")

# Quiet window in G's local time: 23:00 → 07:00. Sessions are NOT reaped
# during this window regardless of idle time — they stay alive overnight.
OVERNIGHT_START = int(os.environ.get("TB_REAPER_OVERNIGHT_START", "23"))
OVERNIGHT_END = int(os.environ.get("TB_REAPER_OVERNIGHT_END", "7"))


def in_overnight_quiet_window() -> bool:
    """Return True if G's local time is in [OVERNIGHT_START, 24) ∪ [0, OVERNIGHT_END)."""
    local_hour = datetime.now(G_TZ).hour
    if OVERNIGHT_START < OVERNIGHT_END:
        # e.g. 01:00–06:00
        return OVERNIGHT_START <= local_hour < OVERNIGHT_END
    else:
        # wraps midnight: e.g. 23:00–07:00
        return local_hour >= OVERNIGHT_START or local_hour < OVERNIGHT_END


def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    try:
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def ensure_dirs() -> None:
    for d in (REAPER_DIR, TASK_DIR, PENDING_DIR, KEEPALIVE_DIR, LAST_ACTIVE_DIR,
              PINNED_DIR):
        d.mkdir(parents=True, exist_ok=True)


def is_pinned(sid: str) -> bool:
    """Return True if a pinned/<sid> sentinel exists (optionally with expiry epoch)."""
    p = PINNED_DIR / sid
    if not p.exists():
        return False
    # Optional expiry: sentinel contains unix epoch; if in the past, treat as unpinned.
    try:
        txt = p.read_text().strip()
        if txt:
            expiry = float(txt)
            if expiry < time.time():
                try:
                    p.unlink()
                except OSError:
                    pass
                return False
    except (OSError, ValueError):
        pass
    return True


def notify_telegram(msg: str) -> None:
    """Best-effort Telegram ping. Silent on any failure."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode({
            "chat_id": TG_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown",
            "disable_notification": "false",
        }).encode()
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass  # notifications must never break reaping


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def pid_rss_mb(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def pid_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def jsonl_last_mtime(sid: str) -> float:
    """Most recent JSONL mtime for this session across all projects."""
    latest = 0.0
    for p in PROJECTS_DIR.glob(f"**/{sid}.jsonl"):
        try:
            m = p.stat().st_mtime
            if m > latest:
                latest = m
        except OSError:
            continue
    return latest


def session_last_active(sid: str) -> float:
    """Last-active timestamp: max of hook-touched file and JSONL mtime."""
    hook_ts = 0.0
    hp = LAST_ACTIVE_DIR / sid
    if hp.exists():
        try:
            hook_ts = hp.stat().st_mtime
        except OSError:
            pass
    return max(hook_ts, jsonl_last_mtime(sid))


def has_active_task(sid: str) -> tuple[bool, str]:
    """Check task registry. Stale entries (> TASK_STALE_HOURS) are ignored."""
    tp = TASK_DIR / f"{sid}.json"
    if not tp.exists():
        return False, ""
    try:
        age = time.time() - tp.stat().st_mtime
        if age > TASK_STALE_HOURS * 3600:
            return False, "stale"
        data = json.loads(tp.read_text())
        return True, data.get("tool", "unknown")
    except (OSError, json.JSONDecodeError):
        return False, ""


def iter_sessions():
    """Yield (sid, pid, session_json) for every session file."""
    if not SESSIONS_DIR.exists():
        return
    for sf in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(sf.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid = data.get("pid") or data.get("processId")
        sid = data.get("sessionId") or data.get("id") or sf.stem
        if not pid:
            # fall back to filename if it's numeric
            try:
                pid = int(sf.stem)
            except ValueError:
                continue
        yield sid, int(pid), data


def negotiate_kill(sid: str, pid: int) -> str:
    """Run the negotiation protocol. Returns: 'aborted', 'exited', 'killed'."""
    pending = PENDING_DIR / sid
    keep = KEEPALIVE_DIR / sid
    # Clear any stale keepalive
    try:
        keep.unlink()
    except FileNotFoundError:
        pass
    pending.write_text(str(int(time.time())))
    deadline = time.time() + GRACE_SECS
    while time.time() < deadline:
        if keep.exists():
            try:
                pending.unlink()
            except FileNotFoundError:
                pass
            return "aborted"
        if not pid_alive(pid):
            try:
                pending.unlink()
            except FileNotFoundError:
                pass
            return "exited"
        time.sleep(5)

    # Grace expired: SIGTERM
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        log(f"  SIGTERM failed for {pid}: {e}")
        return "killed"
    # Wait 10s for graceful exit
    for _ in range(10):
        time.sleep(1)
        if not pid_alive(pid):
            try:
                pending.unlink()
            except FileNotFoundError:
                pass
            return "killed"
    # SIGKILL
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        pending.unlink()
    except FileNotFoundError:
        pass
    return "killed"


def update_digest(entry: dict) -> None:
    data = []
    if DIGEST_FILE.exists():
        try:
            data = json.loads(DIGEST_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            data = []
    data.append(entry)
    # Keep last 14 days
    cutoff = time.time() - 14 * 86400
    data = [d for d in data if d.get("ts", 0) > cutoff]
    try:
        DIGEST_FILE.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def self_pid() -> int:
    """Return the PID of the session running this reaper, if any (to skip)."""
    return int(os.environ.get("CLAUDE_SESSION_PID", "0") or 0)


def main() -> int:
    ensure_dirs()
    skip_pid = self_pid()
    now = time.time()
    idle_cutoff = now - IDLE_HOURS * 3600

    local_now = datetime.now(G_TZ).strftime("%H:%M %Z")
    log(f"reaper run: idle_hours={IDLE_HOURS} min_mem_mb={MIN_MEM_MB} "
        f"grace={GRACE_SECS}s dry_run={DRY_RUN} g_local={local_now} tz={_tz_name}")

    if in_overnight_quiet_window():
        log(f"reaper skip: overnight quiet window active "
            f"({OVERNIGHT_START}:00–{OVERNIGHT_END}:00 {_tz_name})")
        return 0

    seen = 0
    acted = 0
    for sid, pid, sdata in iter_sessions():
        seen += 1
        if pid == skip_pid or pid == os.getpid():
            continue
        if not pid_alive(pid):
            continue
        cmd = pid_cmdline(pid)
        if "claude" not in cmd.lower() and "node" not in cmd.lower():
            continue
        last = session_last_active(sid)
        idle_h = (now - last) / 3600.0 if last > 0 else 999.0
        rss = pid_rss_mb(pid)

        if last == 0 or last >= idle_cutoff:
            continue  # not idle enough
        if rss < MIN_MEM_MB:
            continue  # small, leave it

        if is_pinned(sid):
            log(f"SKIP sid={sid[:8]} pid={pid} idle={idle_h:.1f}h rss={rss:.0f}MB "
                f"(pinned)")
            continue

        active, tool = has_active_task(sid)
        if active:
            log(f"SKIP sid={sid[:8]} pid={pid} idle={idle_h:.1f}h rss={rss:.0f}MB "
                f"(active task: {tool})")
            continue

        log(f"CANDIDATE sid={sid[:8]} pid={pid} idle={idle_h:.1f}h rss={rss:.0f}MB")
        if DRY_RUN:
            log(f"  dry-run: would reap")
            continue
        outcome = negotiate_kill(sid, pid)
        acted += 1
        log(f"  outcome={outcome}")
        update_digest({
            "ts": int(now),
            "sid": sid,
            "pid": pid,
            "idle_hours": round(idle_h, 2),
            "rss_mb": round(rss, 1),
            "outcome": outcome,
        })
        if outcome == "killed":
            notify_telegram(
                f"🪦 *tb-reaper killed a session*\n"
                f"sid `{sid[:8]}` · pid {pid}\n"
                f"idle {idle_h:.1f}h · {rss:.0f}MB\n"
                f"pin next time: `touch ~/.claude/reaper/pinned/{sid}`"
            )
        elif outcome == "aborted":
            log(f"  session defended itself via keep-alive sentinel")

    log(f"reaper done: scanned={seen} acted={acted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
