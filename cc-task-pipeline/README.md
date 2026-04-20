# cc-task-pipeline

An autonomous task pipeline for Claude Code: reads your recent session
transcripts, extracts concrete follow-ups with an LLM, and can execute safe
ones on its own — gated on local token budget.

One generator, one executor trigger, one executor runner, one queue. No parallel
systems. Add new task sources → modify the generator. Add new trigger logic →
modify the auto-executor.

## Architecture

```
chat-activity-generator.py (hourly)
    │ scans ~/.claude/projects/*.jsonl
    │ asks Sonnet for concrete next steps
    ▼
  queue.json ──┐
               │ notifies Telegram for owner=human tasks
               │
auto-executor.py (every 15 min)
    │ queries tokenburn /recommend + /trends
    │ per-account reserve + weekly cap + trend-spike gates
    ▼
task-executor.sh (only when gates pass)
    │ picks top safe_for_auto task
    │ spawns `claude --print --dangerously-skip-permissions`
    ▼
  task marks itself done/blocked; result to Telegram
```

## Dependencies

- Python 3.10+
- [`claude`](https://github.com/anthropics/claude-code) CLI (for the LLM calls + task execution)
- A local token-usage API exposing `/recommend`, `/trends`, `/health`.
  Designed for the `tb` tool in this repo, but any server matching the
  contract below works.
- `curl` for Telegram notifications (optional)

### tokenburn contract

```
GET /health    → {"status": "ok", ...}
GET /recommend → {
    "action": "use_more" | "continue" | "route_worker" | "worker_saturated"
            | "protect_main" | "switch" | "reduce" | "cool_down_all"
            | "halt" | "unknown",
    "message": "...",
    "route_to": "B",              // optional — present on route_worker
    "main_account": "A",          // optional
    "worker_account": "B",        // optional
    "accounts": [
        {"id": "A", "active": true,
         "role": "main" | "worker",
         "status": "healthy" | "warm" | "hot" | "cool_down",
         "session_pct": 0-100, "weekly_pct": 0-100, "headroom_pct": 0-100}
    ]
}
GET /trends?days=7 → {"daily": [{"date": "YYYY-MM-DD", "tokens": int}, ...]}
```

The router fields (`route_to`, `main_account`, `worker_account`, `role`) are
optional — older or simpler tokenburn implementations that only emit
`{use_more, continue, switch, reduce, cool_down_all, unknown}` still work
unchanged.

See [`../tb/`](../tb/) for a reference implementation.

## Install

```bash
bash install.sh
# edit config.json — at minimum set pipeline_root and (optionally) Telegram creds
```

`install.sh` creates `config.json` from the template, initializes an empty
`queue.json` in `pipeline_root`, and prints cron lines to paste.

## Configuration

Everything is in `config.json` (or `~/.config/cc-task-pipeline/config.json`,
or the path in `CC_TASK_PIPELINE_CONFIG`). Fields:

| Field | Default | Purpose |
|---|---|---|
| `pipeline_root` | **required** | Where `queue.json`, logs, state live |
| `tokenburn_url` | `http://127.0.0.1:18795` | Base URL of the /recommend API |
| `claude_bin` | `claude` | Path or name of the Claude CLI |
| `llm_model` | `claude-sonnet-4-6` | Model for extraction |
| `llm_budget_usd` | `0.50` | Per-generator-run cap |
| `executor_budget_usd` | `2.00` | Per-task cap |
| `generator.*` | see `config.example.json` | Extraction tuning |
| `executor.accounts` | `{}` | Per-account reserve policy (see below) |
| `owner_labels` | `{human, agent}` | Rename owner tags |
| `telegram.*` | empty | Optional push notifications |

### Per-account reserve policy

The executor's gates are **asymmetric per account** so you can burn some
accounts fully and reserve headroom on others for interactive use:

```json
"executor": {
  "accounts": {
    "A": {"min_headroom_pct": 10, "max_weekly_pct": 90, "trend_spike_guard": true},
    "B": {"min_headroom_pct": 0,  "max_weekly_pct": 100, "trend_spike_guard": false}
  },
  "default_min_headroom_pct": 20,
  "default_max_weekly_pct": 85,
  "trend_spike_multiplier": 2.0
}
```

Read: "Account A stays 10% below limit and has a trend-spike guard so an
anomalous day won't drain it. Account B is a burn target — use every last %."
Unknown IDs get the conservative defaults.

## Decision matrix

Auto-executor's `decide()`:

| `/recommend` action | per-account check | extra gate | result |
|---|---|---|---|
| `use_more` | active not hot/cool_down | reserve floor + weekly cap | **RUN** |
| `continue` | active not hot/cool_down | reserve floor + weekly cap + trend-spike (if enabled) | RUN |
| `route_worker` | active == `rec.route_to`, not hot/cool_down | reserve floor + weekly cap | RUN (else SKIP `route-mismatch`) |
| `worker_saturated` | — | — | SKIP (worker hot) |
| `protect_main` | — | — | SKIP (main over reserve) |
| `switch` | — | — | SKIP (legacy; let routing resolve) |
| `reduce` | — | — | SKIP (legacy all-hot) |
| `cool_down_all` / `halt` | — | — | SKIP (hard halt) |
| `unknown` / unrecognized | — | — | SKIP (defensive) |
| (api unreachable) | — | — | SKIP (fail closed) |

The `route_worker` check is asymmetric on purpose: the executor inherits the
active Claude account from the shell that invokes it, so it can't change
accounts — it can only decide whether to fire. On a mismatch we skip and
wait for whatever layer owns routing to swap accounts on the next tick.

Generator is more permissive (single cheap LLM call per run):

| `/recommend` action | result |
|---|---|
| `use_more` / `continue` / `switch` / `route_worker` | run (routing doesn't matter for a single extraction call) |
| `cool_down_all` / `reduce` / `worker_saturated` / `protect_main` / `halt` / `unknown` | skip |
| active status = `cool_down` | skip |
| active headroom < `min_headroom_pct` | skip |
| (api unreachable) | run permissive |

## Queue schema

`queue.json` (atomic writes only):

```json
{
  "version": 3,
  "tasks": [
    {
      "id": "t<6-hex>",
      "task": "human-readable description",
      "status": "pending|active|blocked|done|expired",
      "owner": "human|agent",
      "priority": "low|med|high",
      "category": "research|audit|...",
      "safe_for_auto": true,
      "source": "chat-activity-generator|manual|...",
      "context": "why this task exists",
      "created": "ISO-8601 UTC",
      "last_touched": "ISO-8601 UTC",
      "completed": "ISO-8601 UTC (if done)",
      "outcome": "what happened (if done/blocked)"
    }
  ]
}
```

A task is `safe_for_auto=true` only if the LLM flagged it AND category is in
the safe list (configurable) AND owner is `agent`. Anything touching
credentials, money, external messaging, installs, or prod config → `human`
owner, `safe_for_auto=false`.

## Commands

```bash
python3 taskrunner.py add "review the proposal" --owner human --priority high
python3 taskrunner.py list --owner human
python3 taskrunner.py next
python3 taskrunner.py done t3a9f21 "shipped v2"
python3 taskrunner.py block t3a9f21 "waiting on API key"
python3 taskrunner.py remind --owner human
python3 taskrunner.py stats
python3 taskrunner.py expire --days 14
```

## Kill-switches

```bash
touch $pipeline_root/.auto-gen-disabled     # stop generation
touch $pipeline_root/.auto-exec-disabled    # stop auto-execution
# remove to resume
```

## Safety notes

- The generator never executes anything — it only writes task rows. LLM
  runs under `--max-budget-usd`; typical cost ~$0.02–0.05 per run.
- The executor spawns `claude --dangerously-skip-permissions` with
  `--add-dir $pipeline_root`. Anything in that directory (and any
  `extra_add_dirs` you configure) is writable by the task. Keep it scoped.
- **Do not add credentials, source trees, or home directory to `extra_add_dirs`.**
- `safe_for_auto` gating is advisory. An LLM could still mislabel a task;
  keep `extra_add_dirs` small so the blast radius is bounded.
- If tokenburn is unreachable, the executor fails closed (SKIP) and the
  generator fails permissive (still runs — a single cheap call).

## Files

| Path | Purpose |
|---|---|
| `chat-activity-generator.py` | Stage 1 — scan + LLM extract + append |
| `auto-executor.py` | Stage 2 — gate against /recommend, call executor |
| `task-executor.sh` | Stage 3 — pick task, spawn Claude agent |
| `taskrunner.py` | Queue CRUD + lifecycle |
| `pipeline_config.py` | Shared config loader |
| `config.example.json` | Template |
| `install.sh` | One-shot setup |

## License

MIT — see repo root.
