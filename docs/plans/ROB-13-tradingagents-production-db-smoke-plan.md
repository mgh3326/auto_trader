# ROB-13 — TradingAgents Production Advisory-Only DB Smoke Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or
> superpowers:subagent-driven-development) to execute this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

- **Linear issue:** ROB-13 — Complete TradingAgents production advisory-only DB smoke
- **Branch / worktree:** `feature/ROB-13-tradingagents-production-db-smoke`
  (`/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-13-tradingagents-production-db-smoke`)
- **Status:** Plan only. **No code or smoke run begins until this plan is reviewed.**
- **Planner / reviewer:** Claude Opus
- **Implementer:** Codex (`codex --yolo`), scoped to this worktree
- **Depends on:** ROB-9 (PR #601, merged 55ecdb6e). The advisory-only invariants
  enforced there are preconditions, not changes here.

**Goal:** Add a thin, advisory-only smoke harness and run it against the deployed
auto_trader runtime so a real `TradingAgentsResearchService` subprocess invocation
persists one `TradingDecisionSession` + one `TradingDecisionProposal` against the
production DB, with zero broker / watch / order-intent / dry-run side effects, and
ship a Linear-/Discord-attachable smoke report.

**Architecture:** Add `scripts/smoke_tradingagents_db_ingestion.py` (advisory-only;
no broker imports) plus a focused unit test. Run it from the worktree using the
deployed env file (`ENV_FILE=/Users/mgh3326/services/auto_trader/shared/.env.prod.native`)
with two inline env overrides (`TRADINGAGENTS_REPO_PATH`, `TRADINGAGENTS_PYTHON`)
that point at the verified TradingAgents wrapper. The script never modifies the
prod env file, never registers watches, never calls broker APIs, and never sets
`dry_run=False`. After the smoke succeeds, verify the persisted rows in SQL,
write a redacted smoke report, and open the PR.

**Tech Stack:** Python 3.13, asyncio, pydantic-settings, SQLAlchemy async,
PostgreSQL, the existing `app.services.tradingagents_research_service.ingest_tradingagents_research`
entrypoint, and the local OpenAI-compatible shim at `http://127.0.0.1:8796/v1`
(model `gpt-5.5`).

---

## 1. Pre-flight (already verified by planner, read-only)

| Check | Result |
|---|---|
| `git status` | clean, on `feature/ROB-13-tradingagents-production-db-smoke`, up to date with `origin/main` |
| `git log --oneline -1` | `ed9e49d7 Improve trading decision proposal card display (#603)` |
| `~/services/auto_trader/current` symlink | `→ ~/services/auto_trader/releases/ed9e49d7932801f6811f08155f903f98e6796d65` (matches HEAD) |
| TradingAgents wrapper | `/Users/mgh3326/work/TradingAgents/.venv/bin/python-tradingagents-wrapper` exists, mode `0755`, two-line zsh wrapper that `exec`s the venv `python` |
| Wrapper imports `tradingagents` | ✅ `python-tradingagents-wrapper -c "import tradingagents"` exits 0 |
| TradingAgents runner script | `/Users/mgh3326/work/TradingAgents/scripts/run_auto_trader_research.py` exists |
| Local shim listening | `lsof -nPi :8796` shows `python3.1` PID listening on `127.0.0.1:8796`; `GET /v1/models` returns 200 |
| Production env file | `/Users/mgh3326/services/auto_trader/shared/.env.prod.native` exists (mode 0600). **Contents are NOT read.** |
| ROB-9 service surface | `app/services/tradingagents_research_service.py` exposes `ingest_tradingagents_research` and the three exception types verbatim per ROB-9 plan. |

No changes to ROB-9 source are needed.

---

## 2. In-scope vs Out-of-scope

| Area | In scope (this PR) | Deferred |
|---|---|---|
| Add `scripts/smoke_tradingagents_db_ingestion.py` | ✅ | — |
| Unit test for smoke harness assertion logic (stubbed runner) | ✅ `tests/scripts/test_smoke_tradingagents_db_ingestion.py` | — |
| Run smoke against deployed runtime + prod DB | ✅ documented + executed by planner after Codex finishes | — |
| Write smoke evidence report to `docs/plans/ROB-13-smoke-report.md` | ✅ | — |
| Linear / Discord progress messages | ✅ planner posts | — |
| Modify `app/services/tradingagents_research_service.py` | ❌ | not needed — ROB-9 already complete |
| Modify `~/services/auto_trader/shared/.env.prod.native` | ❌ — **forbidden in this PR** | follow-up: add `TRADINGAGENTS_*` keys to deploy infra after smoke proves the path |
| Add a FastAPI route, scheduler, Discord push, watch registration, broker call | ❌ — **forbidden** | tracked separately (ROB-10/11 ladder) |
| Create `TradingDecisionAction` / `Counterfactual` / `Outcome` rows | ❌ — **forbidden** | product decision required |
| Set `dry_run=False`, place live or paper orders | ❌ — **forbidden** | — |
| Read or echo any value from `.env.prod.native` | ❌ — **forbidden** | — |

---

## 3. Safety invariants the smoke harness MUST enforce

The harness exists because we want a single command that proves these invariants
hold against the *production* runtime, not just unit tests. Any failure of any
invariant must `db.rollback()` (or fail before commit) and exit non-zero.

1. `session.source_profile == "tradingagents"`
2. `session.market_scope == "kr"` (for the KR smoke input)
3. `session.market_brief["advisory_only"] is True`
4. `session.market_brief["execution_allowed"] is False`
5. Exactly one proposal row exists for the new session
6. `proposal.proposal_kind == ProposalKind.other`
7. `proposal.side == "none"`
8. `proposal.original_payload["advisory_only"] is True`
9. `proposal.original_payload["execution_allowed"] is False`
10. `proposal.user_response == UserResponse.pending` and all `user_*` fields `None`
11. **Zero** rows in `trading_decision_actions`, `trading_decision_counterfactuals`,
    `trading_decision_outcomes` whose `proposal_id` belongs to this session
12. The harness module's `sys.modules` after import must NOT contain any of:
    - `app.services.kis*`, `app.services.upbit*`, `app.services.brokers*`,
      `app.services.order_service`, `app.services.watch_alerts`,
      `app.services.paper_trading_service`, `app.services.openclaw_client`,
      `app.services.crypto_trade_cooldown_service`,
      `app.services.fill_notification`, `app.services.execution_event`,
      `app.services.redis_token_manager`, `app.services.kis_websocket*`,
      `app.tasks*`
13. The harness must not call `place_order`, `manage_watch_alerts`, or any
    `dry_run=False` code path
14. The harness must never log a value from `os.environ` whose key matches
    `(KEY|SECRET|TOKEN|PASSWORD|URL)$` (case-insensitive), nor echo `stderr`
    from the runner subprocess at INFO/ERROR level

(1)–(10) are re-checked post-commit by re-querying the DB. (11) is enforced by
SQL `COUNT(*)` queries against the three child tables. (12) is enforced by the
unit test described in §5.2. (13) is enforced by the same `_FORBIDDEN_PREFIXES`
list check at runtime + by *not* importing those modules at all. (14) is
enforced by the redaction helper described in §5.1.

---

## 4. Smoke input (fixed)

| Field | Value |
|---|---|
| Symbol | `005930.KS` (Samsung Electronics, KOSPI) |
| `instrument_type` | `InstrumentType.equity_kr` |
| `as_of_date` | `2025-01-15` |
| `analysts` | `["market"]` (default) |
| Expected `market_scope` | `"kr"` |
| `tradingagents_base_url` | `http://127.0.0.1:8796/v1` |
| `tradingagents_model` | `gpt-5.5` |
| `tradingagents_python` | `/Users/mgh3326/work/TradingAgents/.venv/bin/python-tradingagents-wrapper` |
| `tradingagents_repo_path` | `/Users/mgh3326/work/TradingAgents` |
| `tradingagents_subprocess_timeout_sec` | `1200` (override; GPT-5.5 first-token latency on the local shim has been observed >5min) |

The smoke uses a historical date so the runner does not need live market data
beyond what TradingAgents fetches itself; it does not authorize any trade.

---

## 5. Files

### 5.1 New file: `scripts/smoke_tradingagents_db_ingestion.py`

Responsibilities (no other behavior allowed in this file):

- Argparse:
  - `--symbol` (required, validated by `re.fullmatch(r"^[A-Za-z0-9._/-]{1,32}$", ...)`)
  - `--as-of` (required, `YYYY-MM-DD`, parsed via `date.fromisoformat`)
  - `--instrument-type` (choice: `equity_kr` / `equity_us` / `crypto`,
    default `equity_kr`)
  - `--user-id` (required, positive int — the operator looks this up via
    `python manage_users.py list`)
  - `--analysts` (comma-separated; default `market`)
  - `--keep-on-success` / `--delete-on-success` (mutually exclusive; default
    `--keep-on-success`. `--delete-on-success` deletes the session row created
    by this run, **not** the user.)
- Refuse to start if any of these strings appear in `sys.argv`:
  `--dry-run=False`, `--place-order`, `--register-watch`, `--order-intent`,
  `--no-advisory`, `--execute`. (Defense-in-depth: argparse never defines them,
  but a pre-parse string check makes the refusal explicit.)
- After importing `app.services.tradingagents_research_service`, scan
  `sys.modules` for any forbidden prefix (§3 invariant 12). If any is found,
  print the offending module name and exit 70 **without** invoking the runner.
- Build `Settings()` (which reads `ENV_FILE` from `os.environ`) and confirm
  `tradingagents_repo_path`, `tradingagents_python`, and
  `tradingagents_runner_path` resolve to existing files. If
  `tradingagents_python` is unset in env, exit 78 with the message
  `"TRADINGAGENTS_PYTHON is required for the production smoke"`. Same for
  `tradingagents_repo_path`.
- Open one `AsyncSession` via `app.core.db.AsyncSessionLocal()`.
- Call `ingest_tradingagents_research(db, user_id=..., symbol=..., instrument_type=..., as_of_date=..., analysts=...)`.
- Inside the same session, **before commit**, re-query the row pair plus the
  three child-table counts and assert all 11 invariants from §3.
- If any assertion fails: `await db.rollback()`, print a structured failure
  report (no env values, no stderr text from the subprocess), exit 1.
- If all assertions pass: `await db.commit()`. Then run a final cross-session
  re-query to print the post-commit JSON report.
- The JSON report printed to stdout has these keys only (no env values, no
  stderr, no API keys):
  ```json
  {
    "ok": true,
    "session": {
      "id": <int>,
      "session_uuid": "<uuid>",
      "source_profile": "tradingagents",
      "market_scope": "kr",
      "advisory_only": true,
      "execution_allowed": false,
      "generated_at": "<iso>"
    },
    "proposal": {
      "id": <int>,
      "symbol": "005930.KS",
      "instrument_type": "equity_kr",
      "proposal_kind": "other",
      "side": "none",
      "user_response": "pending",
      "original_payload_advisory_only": true,
      "original_payload_execution_allowed": false
    },
    "side_effect_counts": {
      "actions": 0,
      "counterfactuals": 0,
      "outcomes": 0
    }
  }
  ```
- Logging policy: `logging.getLogger("smoke_tradingagents")` only. No
  `print()` of `os.environ`. No `print()` of `stderr` content. The redaction
  rule for env values: `re.search(r"(KEY|SECRET|TOKEN|PASSWORD|URL)$", key, re.I)`
  → mask value as `"<redacted>"` if ever logged.
- Module-level `_FORBIDDEN_PREFIXES` extends ROB-9's safety list verbatim:
  ```python
  _FORBIDDEN_PREFIXES = (
      "app.services.kis",
      "app.services.upbit",
      "app.services.brokers",
      "app.services.order_service",
      "app.services.watch_alerts",
      "app.services.paper_trading_service",
      "app.services.openclaw_client",
      "app.services.crypto_trade_cooldown_service",
      "app.services.fill_notification",
      "app.services.execution_event",
      "app.services.redis_token_manager",
      "app.services.kis_websocket",
      "app.tasks",
  )
  ```

### 5.2 New file: `tests/scripts/test_smoke_tradingagents_db_ingestion.py`

Marker: default unit (no DB, no subprocess). Stubs both
`run_tradingagents_research` and the `ingest_tradingagents_research` DB calls,
verifying the harness's argument validation, env override checks, forbidden-arg
refusal, and `_FORBIDDEN_PREFIXES` enforcement at module-import time.

| Test | Assertion |
|---|---|
| `test_argv_rejects_dry_run_false` | argparse never reaches; harness exits 64 with code-only stderr message |
| `test_argv_rejects_place_order_flag` | exits 64 |
| `test_argv_rejects_register_watch_flag` | exits 64 |
| `test_module_import_does_not_load_forbidden_prefixes` | reuses the ROB-9 subprocess clean-import pattern: importing `scripts.smoke_tradingagents_db_ingestion` in a fresh interpreter must not put any `_FORBIDDEN_PREFIXES` entry into `sys.modules` |
| `test_settings_missing_tradingagents_python_exits_78` | with `TRADINGAGENTS_PYTHON` unset and `tradingagents_python` setting `None`, harness exits 78 |
| `test_invariant_violation_rolls_back` | stub `ingest_tradingagents_research` to return a session whose `market_brief["execution_allowed"]` is `True`; harness must call `db.rollback`, not `commit`, and exit 1 |
| `test_success_path_prints_redacted_json_report` | stub returns a session+proposal with all invariants holding; harness prints valid JSON, exits 0, and the JSON does not contain any of the strings `OPENAI_API_KEY`, `KIS_`, `UPBIT_`, `GOOGLE_API_KEY`, `DATABASE_URL`, `TELEGRAM_TOKEN`, `OPENDART_API_KEY` |

### 5.3 Files NOT modified

| File | Reason |
|---|---|
| `app/services/tradingagents_research_service.py` | ROB-9 already final |
| `app/services/tradingagents_research_service*` schemas | ROB-9 already final |
| `app/core/config.py` | settings already accept `TRADINGAGENTS_*` envs |
| `~/services/auto_trader/shared/.env.prod.native` | smoke uses inline env overrides only; no prod env file edit |
| Any `app/services/kis*`, `upbit*`, `brokers*`, `order_service`, `watch_alerts`, `paper_trading_service` | **forbidden** to import or modify in this PR |

### 5.4 New file (after smoke runs): `docs/plans/ROB-13-smoke-report.md`

Captures the redacted JSON report, plus the SQL re-verification queries and
their counts. No env values, no API keys, no stderr text.

---

## 6. Smoke invocation (planner runs after Codex finishes)

The smoke is launched **from the worktree** so the new harness is on disk, but
the runtime config (DB, Redis, etc.) and the TradingAgents subprocess use the
*deployed* settings via `ENV_FILE=...`. Two inline env overrides supply the
TradingAgents paths without modifying the prod env file.

```bash
cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-13-tradingagents-production-db-smoke
ENV_FILE=/Users/mgh3326/services/auto_trader/shared/.env.prod.native \
TRADINGAGENTS_REPO_PATH=/Users/mgh3326/work/TradingAgents \
TRADINGAGENTS_PYTHON=/Users/mgh3326/work/TradingAgents/.venv/bin/python-tradingagents-wrapper \
TRADINGAGENTS_SUBPROCESS_TIMEOUT_SEC=1200 \
  uv run python scripts/smoke_tradingagents_db_ingestion.py \
    --symbol 005930.KS \
    --as-of 2025-01-15 \
    --instrument-type equity_kr \
    --user-id <USER_ID_FROM_manage_users.py_list> \
    --keep-on-success
```

`USER_ID_FROM_manage_users.py_list` is resolved by the planner via:

```bash
ENV_FILE=/Users/mgh3326/services/auto_trader/shared/.env.prod.native \
  uv run python manage_users.py list
```

The planner picks an existing low-privilege user (e.g. role `viewer`) and
records the chosen user_id in the smoke report. The smoke does **not** create
or delete users.

Expected runtime: 5–20 min (TradingAgents debate + GPT-5.5 latency on the local
shim). The harness times out at `tradingagents_subprocess_timeout_sec` (1200).

---

## 7. Post-smoke SQL verification (planner runs)

Using the same `ENV_FILE` so we hit the same DB:

```sql
-- Inputs from harness JSON:
-- :session_id, :proposal_id, :session_uuid

-- (a) one session
SELECT id, source_profile, market_scope,
       (market_brief->>'advisory_only')::bool        AS advisory_only,
       (market_brief->>'execution_allowed')::bool    AS execution_allowed
FROM trading_decision_sessions
WHERE id = :session_id;

-- (b) exactly one proposal
SELECT id, symbol, instrument_type, proposal_kind, side, user_response,
       (original_payload->>'advisory_only')::bool      AS advisory_only,
       (original_payload->>'execution_allowed')::bool  AS execution_allowed
FROM trading_decision_proposals
WHERE session_id = :session_id;

-- (c) zero side-effect rows
SELECT
  (SELECT COUNT(*) FROM trading_decision_actions a
     JOIN trading_decision_proposals p ON a.proposal_id = p.id
    WHERE p.session_id = :session_id)         AS action_count,
  (SELECT COUNT(*) FROM trading_decision_counterfactuals c
     JOIN trading_decision_proposals p ON c.proposal_id = p.id
    WHERE p.session_id = :session_id)         AS counterfactual_count,
  (SELECT COUNT(*) FROM trading_decision_outcomes o
     JOIN trading_decision_proposals p ON o.proposal_id = p.id
    WHERE p.session_id = :session_id)         AS outcome_count;
```

Acceptance: (a) returns one row with `advisory_only=t`, `execution_allowed=f`,
`market_scope='kr'`, `source_profile='tradingagents'`. (b) returns exactly one
row with `proposal_kind='other'`, `side='none'`, `user_response='pending'`,
`advisory_only=t`, `execution_allowed=f`. (c) returns
`action_count=0, counterfactual_count=0, outcome_count=0`.

---

## 8. Implementation order (TDD-first, bite-sized)

Each numbered step is a single commit. Run `make lint` after step 5 and
`uv run pytest tests/scripts/ tests/services/test_tradingagents_research_service*.py -v`
after step 6.

### Task 1: Confirm worktree parity with deployed release

**Files:**
- Read only: `git log`, `~/services/auto_trader/current` symlink

- [ ] **Step 1: Confirm release SHA matches HEAD**

Run:
```bash
readlink /Users/mgh3326/services/auto_trader/current | awk -F/ '{print $NF}'
git -C /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-13-tradingagents-production-db-smoke rev-parse origin/main
```
Expected: both print the same SHA prefix (`ed9e49d7…`). If not, stop and
escalate — the smoke would be testing different code from prod.

- [ ] **Step 2: Confirm wrapper + shim**

```bash
/Users/mgh3326/work/TradingAgents/.venv/bin/python-tradingagents-wrapper -c "import tradingagents; print('ok')"
curl -sS -m 2 -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8796/v1/models
```
Expected: `ok` and `200`.

### Task 2: Write failing unit tests for the smoke harness

**Files:**
- Create: `tests/scripts/__init__.py`
- Create: `tests/scripts/test_smoke_tradingagents_db_ingestion.py`

- [ ] **Step 1: Add the seven tests listed in §5.2.** Use `subprocess.run` for
      the clean-import test (mirror pattern from
      `tests/services/test_tradingagents_research_service_safety.py`). For the
      remaining tests, use `pytest.MonkeyPatch` to stub
      `app.services.tradingagents_research_service.run_tradingagents_research`
      and `ingest_tradingagents_research`, plus
      `app.core.db.AsyncSessionLocal` (use a fake context manager whose
      `commit`/`rollback` are `AsyncMock`s so we can assert which one ran).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/scripts/test_smoke_tradingagents_db_ingestion.py -v`
Expected: all 7 fail with `ModuleNotFoundError: No module named 'scripts.smoke_tradingagents_db_ingestion'`
(or equivalent — the module does not exist yet).

- [ ] **Step 3: Commit**
```bash
git add tests/scripts/
git commit -m "test(rob-13): add unit tests for tradingagents smoke harness (failing)"
```

### Task 3: Implement the smoke harness module

**Files:**
- Create: `scripts/smoke_tradingagents_db_ingestion.py`

- [ ] **Step 1: Implement per §5.1.** Module shape (in this exact order so the
      `_FORBIDDEN_PREFIXES` import-time check catches any accidental leakage):

```python
"""ROB-13 advisory-only TradingAgents production DB smoke harness.

This module imports ONLY tradingagents_research_service + DB session +
trading_decision models. It MUST NOT import broker / watch_alerts / order /
paper trading / kis trading / upbit trading / openclaw / paperclip modules.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date

_FORBIDDEN_PREFIXES = (
    "app.services.kis",
    "app.services.upbit",
    "app.services.brokers",
    "app.services.order_service",
    "app.services.watch_alerts",
    "app.services.paper_trading_service",
    "app.services.openclaw_client",
    "app.services.crypto_trade_cooldown_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.redis_token_manager",
    "app.services.kis_websocket",
    "app.tasks",
)

_FORBIDDEN_ARGV = (
    "--dry-run=False", "--place-order", "--register-watch",
    "--order-intent", "--no-advisory", "--execute",
)

_SECRET_KEY_RE = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|URL)$", re.I)
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._/-]{1,32}$")

logger = logging.getLogger("smoke_tradingagents")


def _refuse_forbidden_argv(argv: list[str]) -> None:
    for token in argv:
        for forbidden in _FORBIDDEN_ARGV:
            if forbidden in token:
                print(
                    f"smoke refused: forbidden argv token {forbidden!r} present",
                    file=sys.stderr,
                )
                raise SystemExit(64)


def _refuse_forbidden_modules() -> None:
    for name in list(sys.modules):
        for prefix in _FORBIDDEN_PREFIXES:
            if name == prefix or name.startswith(prefix + "."):
                print(
                    f"smoke refused: forbidden module loaded: {name}",
                    file=sys.stderr,
                )
                raise SystemExit(70)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ROB-13 advisory-only TradingAgents → DB ingestion smoke",
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--as-of", required=True, dest="as_of")
    parser.add_argument(
        "--instrument-type",
        choices=("equity_kr", "equity_us", "crypto"),
        default="equity_kr",
    )
    parser.add_argument("--user-id", required=True, type=int)
    parser.add_argument("--analysts", default="market")
    keep_group = parser.add_mutually_exclusive_group()
    keep_group.add_argument(
        "--keep-on-success", dest="keep_on_success",
        action="store_true", default=True,
    )
    keep_group.add_argument(
        "--delete-on-success", dest="keep_on_success", action="store_false",
    )
    args = parser.parse_args(argv)
    if not _SYMBOL_RE.fullmatch(args.symbol):
        parser.error("--symbol contains unsupported characters")
    args.as_of = date.fromisoformat(args.as_of)
    args.analysts = [a.strip() for a in args.analysts.split(",") if a.strip()]
    if args.user_id <= 0:
        parser.error("--user-id must be positive")
    return args


async def _run(args: argparse.Namespace) -> int:
    # imports kept inside to keep module-level surface minimal
    from app.core.db import AsyncSessionLocal
    from app.models.trading import InstrumentType
    from app.models.trading_decision import (
        ProposalKind, TradingDecisionProposal, TradingDecisionSession,
        UserResponse,
    )
    from app.services import tradingagents_research_service as svc
    from sqlalchemy import func, select

    _refuse_forbidden_modules()  # tradingagents_research_service must be clean

    instrument = InstrumentType(args.instrument_type)
    async with AsyncSessionLocal() as db:
        try:
            session_obj, proposal = await svc.ingest_tradingagents_research(
                db,
                user_id=args.user_id,
                symbol=args.symbol,
                instrument_type=instrument,
                as_of_date=args.as_of,
                analysts=args.analysts,
            )
        except Exception:
            await db.rollback()
            logger.exception("ingest failed")
            return 1

        problems: list[str] = []
        sb = session_obj.market_brief or {}
        op = proposal.original_payload or {}
        if session_obj.source_profile != "tradingagents":
            problems.append("source_profile != tradingagents")
        if instrument == InstrumentType.equity_kr and session_obj.market_scope != "kr":
            problems.append(f"market_scope != kr ({session_obj.market_scope!r})")
        if sb.get("advisory_only") is not True:
            problems.append("session.market_brief.advisory_only is not True")
        if sb.get("execution_allowed") is not False:
            problems.append("session.market_brief.execution_allowed is not False")
        if proposal.proposal_kind != ProposalKind.other:
            problems.append("proposal_kind != other")
        if proposal.side != "none":
            problems.append("side != none")
        if op.get("advisory_only") is not True:
            problems.append("proposal.original_payload.advisory_only is not True")
        if op.get("execution_allowed") is not False:
            problems.append("proposal.original_payload.execution_allowed is not False")
        if proposal.user_response != UserResponse.pending:
            problems.append("user_response != pending")

        proposal_count = (await db.execute(
            select(func.count(TradingDecisionProposal.id))
            .where(TradingDecisionProposal.session_id == session_obj.id)
        )).scalar_one()
        if proposal_count != 1:
            problems.append(f"proposal_count != 1 ({proposal_count})")

        # zero side-effect rows
        for label, model in (
            ("actions", "trading_decision_actions"),
            ("counterfactuals", "trading_decision_counterfactuals"),
            ("outcomes", "trading_decision_outcomes"),
        ):
            ...  # use raw text() for these counts

        if problems:
            await db.rollback()
            logger.error("invariant failures: %s", problems)
            return 1

        if args.keep_on_success:
            await db.commit()
        else:
            await db.rollback()

    # Re-query post-commit and emit redacted JSON report.
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    _refuse_forbidden_argv(raw)
    args = _parse_args(raw)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

(The implementer fills in the post-commit re-query + the `text()` counts +
the redacted JSON report shape from §5.1.)

- [ ] **Step 2: Run unit tests to verify they pass**

Run: `uv run pytest tests/scripts/test_smoke_tradingagents_db_ingestion.py -v`
Expected: all 7 PASS.

- [ ] **Step 3: Commit**
```bash
git add scripts/smoke_tradingagents_db_ingestion.py
git commit -m "feat(rob-13): add advisory-only tradingagents production smoke harness"
```

### Task 4: Lint, typecheck, and full ROB-9/13 test sweep

- [ ] **Step 1:** `uv run ruff format scripts/ tests/scripts/` then
      `uv run ruff check scripts/ tests/scripts/`
- [ ] **Step 2:** `make lint` (project-wide)
- [ ] **Step 3:** `uv run pytest tests/scripts/ tests/services/test_tradingagents_research_service.py tests/services/test_tradingagents_research_service_safety.py -v`
      Expected: 18 (ROB-9) + 7 (ROB-13) = 25 passed.

If anything fails, fix in place before continuing. **No commits with red tests.**

### Task 5: Operator picks a smoke user_id

- [ ] **Step 1: Planner-side action**

Run, recording the chosen `id` (no echo to logs):
```bash
ENV_FILE=/Users/mgh3326/services/auto_trader/shared/.env.prod.native \
  uv run python manage_users.py list
```

Pick a low-privilege user (`role=viewer`, `is_active=true`). Record only the
numeric id in the smoke report — no email, no username, no other PII.

### Task 6: Run the smoke against the deployed runtime

- [ ] **Step 1:** Run the command from §6 with `--keep-on-success`. Capture
      stdout to `/tmp/ROB-13-smoke-stdout.json` and stderr to
      `/tmp/ROB-13-smoke-stderr.log`. **Do not paste stderr verbatim into
      Linear/Discord** — it can contain runner debug output. Stdout is the
      redacted JSON report and is safe to attach.

- [ ] **Step 2:** Confirm exit code is 0 and the JSON report has
      `"ok": true` and all flags as listed in §5.1.

- [ ] **Step 3:** If exit non-zero, capture failure type from stdout (the
      harness only emits non-secret failure descriptors), DO NOT post stderr,
      and write a fix-plan note. Iterate via Codex if the failure is in the
      harness; if it is in the runner subprocess, escalate to the user
      (TradingAgents fork issue, not auto_trader).

### Task 7: Post-smoke SQL verification

- [ ] **Step 1:** Open a psql connected to the prod DB via the same
      `ENV_FILE`-resolved DATABASE_URL (the planner uses `psql "$DATABASE_URL"`
      after sourcing only `DATABASE_URL` from the env file with
      `set -a; source <(grep -E '^DATABASE_URL=' "$ENV_FILE"); set +a`).
      The DATABASE_URL value is **never** echoed.
- [ ] **Step 2:** Run the three queries from §7 with the session_id from
      the harness JSON. Expected counts and flags as in §7.

### Task 8: Write the smoke report

**Files:**
- Create: `docs/plans/ROB-13-smoke-report.md`

- [ ] **Step 1:** Capture:
      - Verdict (PASS/FAIL)
      - Release SHA (`ed9e49d7…`)
      - Timestamp (UTC)
      - Smoke input (symbol, as_of, instrument_type, analysts)
      - The redacted JSON report from `/tmp/ROB-13-smoke-stdout.json`
      - SQL re-verification counts (one row session, one row proposal, 0 / 0 / 0
        action / counterfactual / outcome)
      - Confirmation that no broker / watch / order-intent / dry-run code path
        was touched (cite §3 invariants 12 + 13)

- [ ] **Step 2:** Commit
```bash
git add docs/plans/ROB-13-smoke-report.md
git commit -m "docs(rob-13): production advisory-only smoke evidence"
```

### Task 9: Open the PR

- [ ] **Step 1:** `git push -u origin feature/ROB-13-tradingagents-production-db-smoke`

- [ ] **Step 2:** `gh pr create --base main --title "feat(rob-13): tradingagents production advisory-only DB smoke"`
      with body that explicitly includes the phrase **"advisory-only, no
      execution path"** and links Linear ROB-13.

- [ ] **Step 3:** Post a Linear comment on ROB-13 with: PR URL, smoke verdict,
      session_uuid, "no broker/watch/order/dry-run side effects". Post a
      Discord progress message in the same thread/channel used for ROB-9.
      No env values, no API keys, no DATABASE_URL.

---

## 9. Self-review checklist (planner runs before merge)

- [ ] `git diff origin/main` touches only:
      `scripts/smoke_tradingagents_db_ingestion.py`,
      `tests/scripts/test_smoke_tradingagents_db_ingestion.py`,
      `tests/scripts/__init__.py`,
      `docs/plans/ROB-13-tradingagents-production-db-smoke-plan.md`,
      `docs/plans/ROB-13-smoke-report.md`.
      No `app/`, no `~/services/auto_trader/`, no `.env*`.
- [ ] No imports of `app.services.kis*`, `upbit*`, `brokers*`, `order_service`,
      `watch_alerts`, `paper_trading_service`, `openclaw_client`,
      `crypto_trade_cooldown_service`, `fill_notification`, `execution_event`,
      `redis_token_manager`, `kis_websocket*`, `app.tasks` anywhere in the
      diff.
- [ ] No `place_order`, `manage_watch_alerts`, `dry_run=False`, broker
      construction, watch registration, order intent call.
- [ ] `_FORBIDDEN_PREFIXES` in the harness matches the ROB-9 list verbatim.
- [ ] Harness never touches `os.environ` values whose key matches
      `(KEY|SECRET|TOKEN|PASSWORD|URL)$` (case-insensitive) for log/print.
- [ ] Smoke command does not write to `~/services/auto_trader/shared/.env.prod.native`.
- [ ] Smoke report contains zero env values / API keys / DATABASE_URL.
- [ ] All §5.2 unit tests pass; all ROB-9 tests still pass.
- [ ] DB SQL counts: 1 session, 1 proposal, 0 action, 0 counterfactual,
      0 outcome.
- [ ] PR description explicitly states **"advisory-only, no execution path"**.

---

**End of plan.** Implementation begins under Codex (`codex --yolo`) only after
this plan is reviewed; the planner then runs §6/§7/§8/§9 personally.
