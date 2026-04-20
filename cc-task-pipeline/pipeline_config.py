"""Shared config loader for cc-task-pipeline.

Resolution order:
  1. CC_TASK_PIPELINE_CONFIG env var (explicit path)
  2. ./config.json next to this file
  3. ~/.config/cc-task-pipeline/config.json
  4. Hard-coded defaults (minimal — requires pipeline_root from caller)

Paths with ~ and $VAR are expanded. Config values may be overridden by env vars
(see get_telegram_creds).
"""
from __future__ import annotations

import json
import os
from pathlib import Path


DEFAULTS = {
    "tokenburn_url": "http://127.0.0.1:18795",
    "claude_bin": "claude",
    "llm_model": "claude-sonnet-4-6",
    "llm_budget_usd": 0.50,
    "executor_budget_usd": 2.00,
    "executor_timeout_sec": 900,
    "generator": {
        "lookback_min": 70,
        "max_sessions_per_run": 15,
        "max_chars_per_session": 4000,
        "max_total_chars": 40000,
        "max_tasks_per_run": 4,
        "min_headroom_pct": 10,
        "dedupe_similarity": 0.78,
        "safe_categories": [
            "research", "audit", "scan", "reporting", "documentation",
            "cleanup", "dedup", "code-review", "verification", "analysis",
        ],
    },
    "executor": {
        "accounts": {},
        "default_min_headroom_pct": 20,
        "default_max_weekly_pct": 85,
        "trend_spike_multiplier": 2.0,
        "extra_add_dirs": [],
    },
    "owner_labels": {"human": "human", "agent": "agent"},
    "telegram": {
        "bot_token_env": "TELEGRAM_BOT_TOKEN",
        "chat_id_env": "TELEGRAM_CHAT_ID",
        "bot_token": "",
        "chat_id": "",
        "dashboard_url": "",
    },
}


def _expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _candidate_paths() -> list[Path]:
    here = Path(__file__).resolve().parent
    paths = []
    env = os.environ.get("CC_TASK_PIPELINE_CONFIG")
    if env:
        paths.append(Path(_expand(env)))
    paths.append(here / "config.json")
    paths.append(Path.home() / ".config" / "cc-task-pipeline" / "config.json")
    return paths


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    for p in _candidate_paths():
        if p.exists():
            try:
                over = json.loads(p.read_text())
                cfg = _merge(cfg, over)
                cfg["_config_path"] = str(p)
                break
            except json.JSONDecodeError as e:
                raise SystemExit(f"cc-task-pipeline: bad config JSON at {p}: {e}")
    root = cfg.get("pipeline_root")
    if not root:
        raise SystemExit(
            "cc-task-pipeline: pipeline_root is required. Set it in config.json "
            "or export CC_TASK_PIPELINE_ROOT."
        )
    cfg["pipeline_root"] = _expand(root)
    return cfg


def pipeline_path(cfg: dict, *parts: str) -> Path:
    return Path(cfg["pipeline_root"]).joinpath(*parts)


def get_telegram_creds(cfg: dict) -> tuple[str, str, str]:
    tg = cfg.get("telegram") or {}
    token = os.environ.get(tg.get("bot_token_env") or "TELEGRAM_BOT_TOKEN", "") or tg.get("bot_token") or ""
    chat_id = os.environ.get(tg.get("chat_id_env") or "TELEGRAM_CHAT_ID", "") or tg.get("chat_id") or ""
    dashboard = tg.get("dashboard_url") or ""
    return token, chat_id, dashboard
