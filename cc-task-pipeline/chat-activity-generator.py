#!/usr/bin/env python3
"""Chat activity → task generator.

Runs periodically (typically hourly via cron). Reads recent Claude Code session
jsonl files, asks Sonnet to extract concrete actionable follow-ups, dedupes
against the live queue, and appends survivors.

Flow:
  1. Pre-flight against the tokenburn /recommend API.
     Skip if account is cool_down or headroom is below the configured floor.
  2. Scan ~/.claude/projects/ for jsonl files modified in the last N minutes.
  3. Extract user + assistant text turns (drop tool_use/tool_result noise).
  4. Compose compact corpus (per-session + total caps).
  5. Ask Sonnet via the local `claude` CLI for up to N tasks.
  6. Dedupe against live queue + last 7d completed (SequenceMatcher).
  7. Atomic append to queue.json.
  8. Push Telegram notification for owner=human tasks.

Kill-switch: touch $pipeline_root/.auto-gen-disabled
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # noqa: E402


CFG = pipeline_config.load()
ROOT = Path(CFG["pipeline_root"])
QUEUE_FILE = ROOT / "queue.json"
KILL_SWITCH = ROOT / ".auto-gen-disabled"
STATE_FILE = ROOT / ".chat-gen-state.json"
LOG = ROOT / "chat-activity-generator.log"

SESSIONS_ROOT = Path.home() / ".claude" / "projects"
RECOMMEND_URL = f"{CFG['tokenburn_url']}/recommend"
CLAUDE_BIN = CFG["claude_bin"]
LLM_MODEL = CFG["llm_model"]
LLM_BUDGET = CFG.get("llm_budget_usd", 0.50)
LLM_TIMEOUT_SEC = int(CFG.get("llm_timeout_sec", 300))

GEN = CFG["generator"]
LOOKBACK_MIN = GEN["lookback_min"]
MAX_SESSIONS_PER_RUN = GEN["max_sessions_per_run"]
MAX_CHARS_PER_SESSION = GEN["max_chars_per_session"]
MAX_TOTAL_CHARS = GEN["max_total_chars"]
MAX_TASKS_PER_RUN = GEN["max_tasks_per_run"]
MIN_HEADROOM_PCT = GEN["min_headroom_pct"]
DEDUPE_SIMILARITY = GEN["dedupe_similarity"]
SAFE_CATEGORIES = set(GEN["safe_categories"])

LABELS = CFG["owner_labels"]
HUMAN = LABELS.get("human", "human")
AGENT = LABELS.get("agent", "agent")


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


def recommend_preflight() -> dict | None:
    try:
        with urllib.request.urlopen(RECOMMEND_URL, timeout=5) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        log(f"preflight: /recommend unreachable ({e}); proceeding anyway")
        return None


def should_skip(rec: dict | None) -> tuple[bool, str]:
    """Generator is permissive — one cheap LLM call per run.

    Skips only on clear-halt signals or exhausted reserve. Tolerates
    routing-layer actions (route_worker, switch) since a single extraction
    run doesn't care which account ends up billed.
    """
    if not rec:
        return False, "api-unreachable (run permissive)"
    action = rec.get("action")
    SKIP = ("halt", "cool_down_all", "reduce", "worker_saturated",
            "protect_main", "unknown")
    if action in SKIP:
        return True, f"action={action}"
    RUN = ("use_more", "continue", "switch", "route_worker")
    if action not in RUN:
        return True, f"action={action!r} (unrecognized)"
    active = next((a for a in rec.get("accounts", []) if a.get("active")), None)
    if active and active.get("status") == "cool_down":
        return True, "active-status=cool_down"
    head = active.get("headroom_pct") if active else 100
    if head is not None and head < MIN_HEADROOM_PCT:
        return True, f"active-headroom={head}%"
    return False, f"action={action} headroom={head}%"


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


def recent_session_files(cutoff_epoch: float) -> list[Path]:
    files: list[Path] = []
    if not SESSIONS_ROOT.exists():
        return files
    for p in SESSIONS_ROOT.rglob("*.jsonl"):
        if "subagents" in p.parts or "tool-results" in p.parts:
            continue
        try:
            if p.stat().st_mtime >= cutoff_epoch:
                files.append(p)
        except OSError:
            continue
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return files[:MAX_SESSIONS_PER_RUN]


def extract_text_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(p for p in parts if p)
    return ""


def summarize_session(path: Path, since_epoch: float) -> str:
    out: list[str] = []
    try:
        with path.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = rec.get("timestamp")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                        if ts < since_epoch:
                            continue
                    except ValueError:
                        pass
                msg = rec.get("message") or {}
                role = msg.get("role") or rec.get("type")
                text = extract_text_from_content(msg.get("content"))
                if not text:
                    continue
                text = text.strip()
                if not text or len(text) < 12:
                    continue
                tag = "U" if role == "user" else "A"
                out.append(f"[{tag}] {text[:1200]}")
    except OSError as e:
        log(f"  read fail {path.name}: {e}")
        return ""
    joined = "\n".join(out)
    if len(joined) > MAX_CHARS_PER_SESSION:
        joined = joined[-MAX_CHARS_PER_SESSION:]
    return joined


def build_corpus(files: list[Path], since_epoch: float) -> tuple[str, int]:
    chunks = []
    total = 0
    used = 0
    for p in files:
        body = summarize_session(p, since_epoch)
        if not body:
            continue
        header = f"\n=== session {p.parent.name}/{p.stem[:8]} ===\n"
        piece = header + body
        if total + len(piece) > MAX_TOTAL_CHARS:
            break
        chunks.append(piece)
        total += len(piece)
        used += 1
    return "".join(chunks), used


EXTRACTION_PROMPT_TEMPLATE = """You analyze chat session excerpts and extract actionable follow-up TASKS.

STRICT RULES:
- Output ONLY a JSON array. No prose, no markdown fences.
- Each task object has these keys: task (string), owner ("__HUMAN__" or "__AGENT__"),
  priority ("low"|"med"|"high"), category (string), safe_for_auto (boolean),
  why (string).
- Max __MAX_TASKS__ tasks. Prefer quality over count. Output [] if nothing concrete.
- owner: "__HUMAN__" if a person must act (decision, external account, signature);
  "__AGENT__" if an automated agent can do it.
- safe_for_auto: true only if category is in: __SAFE_CATS__ — AND the task does NOT
  send external messages/emails, spend money, install software, modify prod config,
  or touch credentials. When in doubt: false.
- why: one short sentence of context from the excerpt.
- DO NOT fabricate. If the excerpt does not contain a clear actionable item, output [].
- DO NOT restate vague "continue working on X" items — only concrete next steps.
- DO NOT include anything about writing this prompt, analyzing sessions, or generating tasks.

CHAT EXCERPTS:
---
__CORPUS__
---

JSON array:"""


def call_llm(corpus: str) -> list[dict]:
    prompt = (EXTRACTION_PROMPT_TEMPLATE
              .replace("__MAX_TASKS__", str(MAX_TASKS_PER_RUN))
              .replace("__HUMAN__", HUMAN)
              .replace("__AGENT__", AGENT)
              .replace("__SAFE_CATS__", ", ".join(sorted(SAFE_CATEGORIES)))
              .replace("__CORPUS__", corpus))
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "--print", "--dangerously-skip-permissions",
             "--model", LLM_MODEL,
             "--max-budget-usd", str(LLM_BUDGET),
             "-p", prompt],
            capture_output=True, text=True, timeout=LLM_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        log("  LLM call timed out")
        return []
    except FileNotFoundError:
        log(f"  {CLAUDE_BIN} not found")
        return []
    if result.returncode != 0:
        log(f"  LLM exit {result.returncode}: {result.stderr[:300]}")
        return []
    out = (result.stdout or "").strip()
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        log(f"  LLM returned no JSON array. head={out[:200]!r}")
        return []
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        log(f"  JSON parse fail: {e}. head={m.group(0)[:200]!r}")
        return []
    if not isinstance(parsed, list):
        return []
    return parsed[:MAX_TASKS_PER_RUN]


def normalize_desc(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:120]


def load_dedupe_set() -> list[str]:
    if not QUEUE_FILE.exists():
        return []
    try:
        data = json.loads(QUEUE_FILE.read_text())
    except json.JSONDecodeError:
        return []
    out = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    for t in data.get("tasks", []):
        desc = t.get("task") or t.get("description") or ""
        if not desc:
            continue
        if t.get("status") == "done":
            completed = t.get("completed") or t.get("last_touched")
            if not completed:
                continue
            try:
                dt = datetime.fromisoformat(completed.replace("Z", "+00:00"))
                if dt < cutoff:
                    continue
            except ValueError:
                continue
        out.append(normalize_desc(desc))
    return out


def is_duplicate(desc: str, seen: list[str]) -> bool:
    n = normalize_desc(desc)
    for s in seen:
        if SequenceMatcher(None, n, s).ratio() >= DEDUPE_SIMILARITY:
            return True
    return False


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
             "-d", "parse_mode=HTML&disable_web_page_preview=true"],
            timeout=10, capture_output=True,
        )
    except Exception:
        pass


def append_tasks(new: list[dict]) -> tuple[int, list[dict]]:
    if not new:
        return 0, []
    ROOT.mkdir(parents=True, exist_ok=True)
    data = json.loads(QUEUE_FILE.read_text()) if QUEUE_FILE.exists() else {"version": 3, "tasks": []}
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    entries: list[dict] = []
    for t in new:
        task_desc = (t.get("task") or "").strip()
        if not task_desc:
            continue
        owner = t.get("owner") if t.get("owner") in (HUMAN, AGENT) else AGENT
        category = (t.get("category") or "").lower()
        safe = bool(t.get("safe_for_auto")) and category in SAFE_CATEGORIES and owner == AGENT
        entry = {
            "id": "t" + uuid.uuid4().hex[:6],
            "task": task_desc[:500],
            "status": "pending",
            "owner": owner,
            "priority": t.get("priority") if t.get("priority") in ("low", "med", "high") else "med",
            "category": category or "general",
            "safe_for_auto": safe,
            "source": "chat-activity-generator",
            "context": (t.get("why") or "")[:300],
            "created": now,
            "last_touched": now,
        }
        data["tasks"].append(entry)
        entries.append(entry)
        added += 1
    tmp = QUEUE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, QUEUE_FILE)
    return added, entries


def push_human_tasks(entries: list[dict]) -> None:
    items = [e for e in entries if e.get("owner") == HUMAN]
    if not items:
        return
    _, _, dashboard = pipeline_config.get_telegram_creds(CFG)
    prio_icon = {"high": "🔴", "med": "🟡", "low": "⚪"}
    lines = [f"<b>📋 {len(items)} new task{'s' if len(items) > 1 else ''} for you</b>\n"]
    for e in items:
        icon = prio_icon.get(e.get("priority"), "⚪")
        task = e.get("task", "")
        if len(task) > 180:
            task = task[:177] + "..."
        lines.append(f"{icon} <b>{e['id']}</b> [{e.get('category', '?')}]")
        lines.append(f"   {task}")
        if e.get("context"):
            ctx = e["context"]
            if len(ctx) > 160:
                ctx = ctx[:157] + "..."
            lines.append(f"   <i>{ctx}</i>")
        lines.append("")
    if dashboard:
        lines.append(f"→ {dashboard}")
    notify_tg("\n".join(lines))


def main() -> int:
    log("=== chat-activity-generator start ===")

    if KILL_SWITCH.exists():
        log("kill-switch present — exiting")
        return 0

    rec = recommend_preflight()
    skip, why = should_skip(rec)
    if skip:
        log(f"skipping: {why}")
        return 0

    state = load_state()
    now_epoch = time.time()
    last_run_epoch = state.get("last_run_epoch", 0)
    cutoff_epoch = max(now_epoch - LOOKBACK_MIN * 60, last_run_epoch - 300)

    files = recent_session_files(cutoff_epoch)
    if not files:
        log("no recent session files — nothing to do")
        state["last_run_epoch"] = now_epoch
        save_state(state)
        return 0

    corpus, used = build_corpus(files, cutoff_epoch)
    if not corpus.strip():
        log(f"scanned {len(files)} files, no extractable content")
        state["last_run_epoch"] = now_epoch
        save_state(state)
        return 0

    log(f"corpus: {len(corpus)} chars across {used}/{len(files)} sessions")

    candidates = call_llm(corpus)
    log(f"LLM returned {len(candidates)} candidate tasks")

    if not candidates:
        state["last_run_epoch"] = now_epoch
        save_state(state)
        return 0

    seen = load_dedupe_set()
    survivors = []
    for c in candidates:
        desc = (c.get("task") or "").strip()
        if not desc or len(desc) < 10:
            continue
        if is_duplicate(desc, seen):
            log(f"  dedup: {desc[:70]}")
            continue
        survivors.append(c)
        seen.append(normalize_desc(desc))

    added, entries = append_tasks(survivors)
    log(f"added {added} tasks (of {len(candidates)} candidates, {len(survivors)} post-dedup)")

    if added:
        push_human_tasks(entries)

    state["last_run_epoch"] = now_epoch
    state["last_added"] = added
    save_state(state)
    log("=== chat-activity-generator done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
