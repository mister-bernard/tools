#!/usr/bin/env python3
"""taskrunner — CRUD + lifecycle helpers for queue.json.

Usage:
    taskrunner.py list [--all] [--owner human|agent]
    taskrunner.py add "task text" [--priority low|med|high] [--category cat]
                                  [--owner human|agent] [--context "..."] [--safe]
    taskrunner.py done <id> ["outcome"]
    taskrunner.py block <id> ["reason"]
    taskrunner.py unblock <id>
    taskrunner.py reassign <id> --owner human|agent
    taskrunner.py safe <id> [--off]
    taskrunner.py next           # Pick highest-priority pending and mark active
    taskrunner.py expire [--days 14]
    taskrunner.py stats
    taskrunner.py remind [--owner human]
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # noqa: E402


CFG = pipeline_config.load()
ROOT = Path(CFG["pipeline_root"])
QUEUE_FILE = ROOT / "queue.json"
SCHEMA_VERSION = 3

LABELS = CFG["owner_labels"]
HUMAN = LABELS.get("human", "human")
AGENT = LABELS.get("agent", "agent")
VALID_OWNERS = {HUMAN, AGENT}

PRIORITY_RANK = {"high": 3, "med": 2, "medium": 2, "low": 1}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_queue() -> dict:
    if not QUEUE_FILE.exists():
        return {"version": SCHEMA_VERSION, "tasks": []}
    return json.loads(QUEUE_FILE.read_text())


def save_queue(data: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    data["version"] = SCHEMA_VERSION
    tmp = QUEUE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    import os
    os.replace(tmp, QUEUE_FILE)


def _score(t: dict) -> int:
    return PRIORITY_RANK.get(t.get("priority", "med"), 2)


def _owner_badge(o: str) -> str:
    return "👤" if o == HUMAN else "🤖"


# --- Commands ---------------------------------------------------------------

def cmd_list(show_all: bool = False, owner_filter: str | None = None) -> None:
    q = load_queue()
    tasks = q["tasks"]
    if not show_all:
        tasks = [t for t in tasks if t.get("status") not in ("done", "expired")]
    if owner_filter:
        tasks = [t for t in tasks if t.get("owner", AGENT) == owner_filter]
    if not tasks:
        print("Queue empty.")
        return
    tasks.sort(key=_score, reverse=True)
    for t in tasks:
        status = t.get("status", "?")
        owner = t.get("owner", AGENT)
        pri = t.get("priority", "med")
        cat = (t.get("category", "?") or "?")[:10]
        print(f"  [{status:7}] {_owner_badge(owner)} {pri:4} {t['id']} {cat:10} — {t['task'][:70]}")


def cmd_add(task_text: str, priority: str = "med", category: str = "general",
            owner: str = None, context: str = "", safe_for_auto: bool = False) -> None:
    if owner is None:
        owner = AGENT
    if owner not in VALID_OWNERS:
        print(f"Invalid owner: {owner!r}. Must be {HUMAN!r} or {AGENT!r}.")
        sys.exit(2)
    q = load_queue()
    if any(t["task"] == task_text for t in q["tasks"]):
        print(f"Already exists: {task_text[:50]}")
        return
    tid = "t" + hashlib.md5((task_text + _now_iso()).encode()).hexdigest()[:6]
    task = {
        "id": tid,
        "task": task_text,
        "status": "pending",
        "owner": owner,
        "priority": priority,
        "category": category,
        "safe_for_auto": bool(safe_for_auto),
        "source": "manual",
        "context": context,
        "created": _now_iso(),
        "last_touched": _now_iso(),
    }
    q["tasks"].append(task)
    save_queue(q)
    print(f"Added {_owner_badge(owner)} [{priority}] {tid} ({category}): {task_text[:60]}")


def cmd_done(task_id: str, outcome: str | None = None) -> None:
    q = load_queue()
    for t in q["tasks"]:
        if t["id"] == task_id:
            t["status"] = "done"
            t["completed"] = _now_iso()
            t["last_touched"] = _now_iso()
            if outcome:
                t["outcome"] = outcome
            save_queue(q)
            print(f"Done: {t['task'][:60]}")
            if outcome:
                print(f"  Outcome: {outcome[:80]}")
            return
    print(f"Not found: {task_id}")


def cmd_block(task_id: str, reason: str = "") -> None:
    q = load_queue()
    for t in q["tasks"]:
        if t["id"] == task_id:
            t["status"] = "blocked"
            t["last_touched"] = _now_iso()
            if reason:
                t["context"] = f"{t.get('context', '')} | BLOCKED: {reason}".strip(" |")
            save_queue(q)
            print(f"Blocked: {t['task'][:60]}")
            return
    print(f"Not found: {task_id}")


def cmd_unblock(task_id: str) -> None:
    q = load_queue()
    for t in q["tasks"]:
        if t["id"] == task_id:
            t["status"] = "pending"
            t["last_touched"] = _now_iso()
            save_queue(q)
            print(f"Unblocked: {t['task'][:60]}")
            return
    print(f"Not found: {task_id}")


def cmd_reassign(task_id: str, new_owner: str) -> None:
    if new_owner not in VALID_OWNERS:
        print(f"Invalid owner: {new_owner!r}. Must be {HUMAN!r} or {AGENT!r}.")
        sys.exit(2)
    q = load_queue()
    for t in q["tasks"]:
        if t["id"] == task_id:
            old = t.get("owner", AGENT)
            t["owner"] = new_owner
            t["last_touched"] = _now_iso()
            save_queue(q)
            print(f"Reassigned {task_id}: {old} → {new_owner}")
            return
    print(f"Not found: {task_id}")


def cmd_safe(task_id: str, value: bool = True) -> None:
    q = load_queue()
    for t in q["tasks"]:
        if t["id"] == task_id:
            t["safe_for_auto"] = value
            t["last_touched"] = _now_iso()
            save_queue(q)
            flag = "✅ safe_for_auto=True" if value else "🔒 safe_for_auto=False"
            print(f"{flag}: {t['task'][:60]}")
            return
    print(f"Not found: {task_id}")


def cmd_next() -> None:
    q = load_queue()
    pending = [t for t in q["tasks"] if t.get("status") == "pending"]
    if not pending:
        print("No pending tasks.")
        return
    pending.sort(key=_score, reverse=True)
    top = pending[0]
    for t in q["tasks"]:
        if t["id"] == top["id"]:
            t["status"] = "active"
            t["last_touched"] = _now_iso()
            break
    save_queue(q)
    print(f"NEXT [{top['id']}] {_owner_badge(top.get('owner', AGENT))} pri={top.get('priority')}")
    print(f"  Category: {top.get('category', '?')}")
    print(f"  Task:     {top['task']}")
    if top.get("context"):
        print(f"  Context:  {top['context']}")


def cmd_expire(days: int = 14) -> None:
    q = load_queue()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    expired = 0
    now = _now_iso()
    for t in q["tasks"]:
        if t.get("status") != "pending":
            continue
        created = t.get("created")
        if not created:
            continue
        try:
            if datetime.fromisoformat(created.replace("Z", "+00:00")) < cutoff:
                t["status"] = "expired"
                t["completed"] = now
                t["outcome"] = f"auto-expired (>{days}d stale)"
                expired += 1
        except ValueError:
            pass
    if expired:
        save_queue(q)
    print(f"Expired {expired} task(s).")


def cmd_stats() -> None:
    q = load_queue()
    tasks = q["tasks"]
    by_status: dict[str, int] = {}
    by_owner: dict[str, int] = {}
    for t in tasks:
        by_status[t.get("status", "?")] = by_status.get(t.get("status", "?"), 0) + 1
        by_owner[t.get("owner", AGENT)] = by_owner.get(t.get("owner", AGENT), 0) + 1
    print("=== Queue Health ===")
    for s in ("pending", "active", "blocked", "done", "expired"):
        if s in by_status:
            print(f"  {s:8}: {by_status[s]}")
    print("\nBy owner:")
    for o, c in by_owner.items():
        print(f"  {_owner_badge(o)} {o:8}: {c}")


def cmd_remind(owner: str = None) -> None:
    if owner is None:
        owner = HUMAN
    q = load_queue()
    tasks = [t for t in q["tasks"]
             if t.get("status") in ("pending", "active", "blocked")
             and t.get("owner", AGENT) == owner]
    if not tasks:
        print(f"✅ All clear — no items waiting on {owner}")
        return
    by_pri: dict[str, list] = {"high": [], "med": [], "low": []}
    for t in tasks:
        pri = t.get("priority", "med")
        by_pri.setdefault(pri, []).append(t)
    total = len(tasks)
    print(f"📋 {total} item{'s' if total != 1 else ''} waiting on {owner}:\n")
    emoji = {"high": "🔴", "med": "🟡", "low": "🟢"}
    for pri in ("high", "med", "low"):
        items = by_pri.get(pri, [])
        if not items:
            continue
        print(f"{emoji[pri]} {pri.upper()} ({len(items)}):")
        for t in items:
            print(f"  • #{t['id']} {t['task'][:80]}")
        print()


# --- CLI --------------------------------------------------------------------

def _flag_value(args: list[str], flag: str) -> str | None:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    cmd = args[0]
    rest = args[1:]

    if cmd == "list":
        cmd_list("--all" in rest, _flag_value(rest, "--owner"))
    elif cmd == "add":
        parts = []
        priority = "med"
        category = "general"
        owner = None
        context = ""
        safe = False
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--priority" and i + 1 < len(rest):
                priority = rest[i + 1]; i += 2
            elif a == "--category" and i + 1 < len(rest):
                category = rest[i + 1]; i += 2
            elif a == "--owner" and i + 1 < len(rest):
                owner = rest[i + 1]; i += 2
            elif a == "--context" and i + 1 < len(rest):
                context = rest[i + 1]; i += 2
            elif a == "--safe":
                safe = True; i += 1
            else:
                parts.append(a); i += 1
        if not parts:
            print("Usage: taskrunner.py add \"task description\" [flags]")
            return 2
        cmd_add(" ".join(parts), priority, category, owner, context, safe)
    elif cmd == "done":
        if not rest:
            print("Usage: taskrunner.py done <id> [outcome]")
            return 2
        cmd_done(rest[0], " ".join(rest[1:]) if len(rest) > 1 else None)
    elif cmd == "block":
        if not rest:
            print("Usage: taskrunner.py block <id> [reason]")
            return 2
        cmd_block(rest[0], " ".join(rest[1:]) if len(rest) > 1 else "")
    elif cmd == "unblock":
        if not rest:
            print("Usage: taskrunner.py unblock <id>")
            return 2
        cmd_unblock(rest[0])
    elif cmd == "reassign":
        owner = _flag_value(rest, "--owner")
        if not rest or not owner:
            print("Usage: taskrunner.py reassign <id> --owner human|agent")
            return 2
        cmd_reassign(rest[0], owner)
    elif cmd == "safe":
        if not rest:
            print("Usage: taskrunner.py safe <id> [--off]")
            return 2
        cmd_safe(rest[0], value=("--off" not in rest))
    elif cmd == "next":
        cmd_next()
    elif cmd == "expire":
        days = _flag_value(rest, "--days")
        cmd_expire(int(days) if days else 14)
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "remind":
        cmd_remind(_flag_value(rest, "--owner"))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
