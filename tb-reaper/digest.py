#!/usr/bin/env python3
"""Weekly digest of reaper activity → Telegram (or stdout if no token)."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

DIGEST_FILE = Path.home() / ".tokenburn-reaper-digest.json"


def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def build_report() -> str:
    if not DIGEST_FILE.exists():
        return "tb-reaper: no activity recorded this week."
    try:
        entries = json.loads(DIGEST_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return "tb-reaper: digest file unreadable."

    cutoff = time.time() - 7 * 86400
    week = [e for e in entries if e.get("ts", 0) > cutoff]
    if not week:
        return "tb-reaper: quiet week — no sessions needed reaping."

    killed = sum(1 for e in week if e.get("outcome") == "killed")
    exited = sum(1 for e in week if e.get("outcome") == "exited")
    aborted = sum(1 for e in week if e.get("outcome") == "aborted")
    total_mb = sum(e.get("rss_mb", 0) for e in week if e.get("outcome") in ("killed", "exited"))

    lines = [
        "🔍 tb-reaper weekly digest",
        f"Actions: {len(week)} (killed={killed}, exited={exited}, aborted={aborted})",
        f"RAM reclaimed: ~{total_mb:.0f} MB",
        "",
        "Recent reaps:",
    ]
    for e in week[-8:]:
        ts = time.strftime("%m-%d %H:%M", time.localtime(e.get("ts", 0)))
        lines.append(
            f"  {ts}  pid={e.get('pid')}  idle={e.get('idle_hours')}h  "
            f"rss={e.get('rss_mb')}MB  → {e.get('outcome')}"
        )
    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    env = load_env(Path.home() / ".openclaw" / ".env")
    token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = env.get("G_TELEGRAM_ID") or os.environ.get("G_TELEGRAM_ID") or "39172309"
    if not token:
        return False
    try:
        subprocess.run(
            [
                "curl", "-s", "-X", "POST",
                f"https://api.telegram.org/bot{token}/sendMessage",
                "-d", f"chat_id={chat}",
                "--data-urlencode", f"text={text}",
            ],
            check=True, timeout=15, capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


def main() -> int:
    report = build_report()
    if not send_telegram(report):
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
