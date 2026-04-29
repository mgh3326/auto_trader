# ROB-26 — Prefect-style Research Run & Live-Refresh Schedule Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans
> (or superpowers:subagent-driven-development) to execute this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**AOE_STATUS:** plan_ready
**AOE_ISSUE:** ROB-26
**AOE_ROLE:** planner
**AOE_NEXT:** start_implementer_same_session

- **Linear issue:** ROB-26 — [Prefect] Schedule KR/NXT Research Run and live refresh deployments
- **Worktree:** `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-26-prefect-research-run-refresh`
- **Branch:** `feature/ROB-26-prefect-research-run-refresh`
- **Planner / reviewer:** Claude Opus (this session)
- **Implementer:** Claude Sonnet, exact command:
  `claude --model 'sonnet' --dangerously-skip-permissions`
- **Depends on (already merged on `main`):**
  - ROB-22 (`pending_reconciliation_service`)
  - ROB-23 (`nxt_classifier_service`)
  - ROB-24 (`research_run_service`, models, schemas, migration)
  - ROB-25 (`research_run_decision_session_service`,
    `research_run_live_refresh_service`, router
    `app/routers/research_run_decision_sessions.py`)
- **Does NOT depend on:** Prefect runtime/dependency, broker mutation paths,
  watch alert mutation, TradingAgents.

**Goal:** Wire scheduled, **read-only** entry points that drive the existing
ROB-25 research-run live-refresh / decision-session pipeline at the requested
KR / NXT KST timings. Each schedule resolves the latest research run for a
configured operator user, calls the existing read-only live-refresh provider,
persists a `TradingDecisionSession` (decision-ledger only — never an execution
authorization), and returns a structured summary suitable for logs/alerting.
**No order placement, watch mutation, or dry-run order side effects.**

---

## 0. Architectural decision — Prefect vs Taskiq

The Linear issue title says "[Prefect]" but `auto_trader` uses **Taskiq** for
all scheduled work (`app/core/taskiq_broker.py`, `app/core/scheduler.py`,
`app/tasks/*.py`). There is **no Prefect dependency** in `pyproject.toml`,
no `prefect/`, no `app/flows/`, no Prefect deployment infrastructure.
ROB-16 (Prefect intraday watch proximity monitor) explicitly chose Taskiq for
the same reason and the precedent is documented in
`docs/plans/ROB-16-prefect-intraday-watch-proximity-monitor-plan.md` §0.

**Decision for this PR:** implement the schedules as Taskiq cron tasks
(mirrors `app/tasks/intraday_order_review_tasks.py`,
`app/tasks/watch_proximity_tasks.py`). Adding Prefect as a brand-new runtime
and deployment surface for a single read-only schedule set would multiply
blast radius and is **out of scope** for ROB-26.

**Prefect compatibility note (external wrapper, NOT implemented here):**
Because the orchestrator (`run_research_run_refresh()`) is a plain async
function with no Taskiq imports inside it, a Prefect deployment can later
wrap it without code changes:

```python
# Sketch for a future repo / external wrapper (NOT implemented in ROB-26):
# from prefect import flow
# from prefect.client.schemas.schedules import CronSchedule
# from app.jobs.research_run_refresh_runner import run_research_run_refresh
#
# @flow(name="kr-preopen-research-refresh")
# async def kr_preopen_research_refresh_flow() -> dict:
#     return await run_research_run_refresh(
#         stage="preopen", market_scope="kr",
#     )
#
# # Deployment (CLI / serve()):
# kr_preopen_research_refresh_flow.serve(
#     name="kr-preopen-research-refresh",
#     cron="10 8 * * 1-5", timezone="Asia/Seoul",
# )
```

The Taskiq-side wrapper (`app/tasks/research_run_refresh_tasks.py`) is the
only scheduling surface this PR ships. Sonnet **MUST NOT** add a `prefect`
dependency, **MUST NOT** import `prefect`, and **MUST NOT** create a
`@flow`-decorated function in this repo. If product later requires a Prefect
runtime, that is a separate ticket scoped to deployment infra.

If a reviewer insists on a real Prefect dependency before merging, escalate
to the planner — **do not** silently add the dependency.

---

## 1. Scope check

ROB-26 is one subsystem (read-only schedule wiring). It does **not**:

- modify `research_run_service`, `research_run_live_refresh_service`,
  `research_run_decision_session_service`, or
  `app/routers/research_run_decision_sessions.py`,
- introduce a Prefect dependency or flow,
- create or mutate watch alerts,
- place orders, modify orders, or generate paper / dry-run order intents,
- iterate over multiple users (a single configured operator user_id is
  used; multi-user enumeration is a follow-up ticket),
- create research runs from scratch (the upstream "research run creation"
  pipeline is out of scope; this PR only **refreshes** the latest run that
  already exists).

The acceptance criteria are met by:

- a single orchestrator function `run_research_run_refresh()` that wraps the
  existing ROB-25 service with deterministic, read-only summary semantics
  (always returns a dict, never raises on "no run"/"empty run" conditions),
- 8 Taskiq cron tasks (KST) that call it with the right
  `(stage, market_scope)`,
- a manual-run script `scripts/run_research_run_refresh.py` (default
  `--dry-run` so smoke testing **never** writes to the DB),
- safety/import unit tests asserting the new modules cannot import any
  forbidden execution surface,
- a smoke test that runs the orchestrator end-to-end against a fake
  `AsyncSession` and verifies a `status="skipped"` (no operator
  configured / no run found) summary without DB mutation,
- env-var gates so the schedules are **off by default** in dev / CI.

## 2. In-scope vs Out-of-scope

| Area | In scope (this PR) | Deferred |
|---|---|---|
| `app/jobs/research_run_refresh_runner.py` (orchestrator) | ✅ | — |
| `app/tasks/research_run_refresh_tasks.py` (Taskiq cron tasks) | ✅ | — |
| `scripts/run_research_run_refresh.py` (manual entry) | ✅ | — |
| `app/core/config.py` settings additions (enabled / user_id / hours-only) | ✅ | — |
| `env.example` doc keys | ✅ | — |
| `app/tasks/__init__.py` registration in `TASKIQ_TASK_MODULES` | ✅ | — |
| Unit tests: orchestrator, task wrappers, manual runner, import-safety | ✅ | — |
| Smoke test (no-op `status="skipped"` path) | ✅ | — |
| Plan + deployment notes (this file + section §10) | ✅ | — |
| Prefect dependency / Prefect deployment | ❌ | external infra ticket |
| Multi-user fan-out (iterate every user with active run) | ❌ | follow-up |
| Creating research runs from screener / portfolio inputs | ❌ | upstream |
| TradingAgents advisory wiring (`include_tradingagents=True`) | ❌ | ROB-25 v2 |
| Modifying `research_run_*_service` business logic | ❌ | — |
| Adding/removing watch records | ❌ — **forbidden** | — |
| Live, paper, or `dry_run=False` order placement | ❌ — **forbidden** | — |
| Reading or echoing API keys / `.env` values / tokens / passwords | ❌ — **forbidden** | — |

## 3. Safety invariants this PR MUST enforce

1. **Forbidden imports** in
   `app/jobs/research_run_refresh_runner.py`,
   `app/tasks/research_run_refresh_tasks.py`,
   `scripts/run_research_run_refresh.py`:

   - `prefect` (any submodule)
   - `app.services.kis_trading_service`
   - `app.services.kis_trading_contracts`
   - `app.services.upbit_trading_service` *(if present)*
   - `app.services.order_service`
   - `app.services.orders` *(any submodule)*
   - `app.services.paper_trading_service`
   - `app.services.fill_notification`
   - `app.services.execution_event`
   - `app.services.crypto_trade_cooldown_service`
   - `app.services.kis_websocket`
   - `app.services.kis_websocket_internal`
   - `app.services.upbit_websocket`
   - `app.services.upbit_market_websocket`
   - `app.services.watch_alerts` *(write or registration paths;
     read-only listing also not needed for this PR — block the whole module)*
   - `app.services.screener_service`
   - `app.services.tradingagents_research_service`
   - `app.mcp_server.tooling.orders_registration`
   - `app.mcp_server.tooling.orders_modify_cancel`
   - `app.mcp_server.tooling.orders_history`
     *(the live-refresh provider already imports this internally —
     the orchestrator MUST go through `research_run_live_refresh_service` and
     not import `orders_history` directly)*
   - `app.mcp_server.tooling.paper_order_handler`
   - `app.mcp_server.tooling.watch_alerts_registration`

   **Allowed imports** (orchestrator + task module + script):
   - `app.services.research_run_decision_session_service`
   - `app.services.research_run_live_refresh_service`
   - `app.services.research_run_service` (read-only selectors only:
     `get_latest_research_run`, `get_research_run_by_uuid`)
   - `app.core.config`, `app.core.timezone`, `app.core.db`
   - `app.core.taskiq_broker` (in the task module only — NOT in
     `research_run_refresh_runner.py`, so the orchestrator stays
     scheduler-agnostic and Prefect-wrappable later)
   - `sqlalchemy`, `sqlalchemy.ext.asyncio`
   - stdlib (`asyncio`, `dataclasses`, `datetime`, `logging`, `uuid`)

   Enforced by Task 6 import-safety test.

2. **`include_tradingagents` is locked to `False`.** The orchestrator passes
   `include_tradingagents=False` to
   `ResearchRunDecisionSessionRequest`. The orchestrator MUST NOT accept an
   `include_tradingagents=True` argument on its public surface in this PR.
   (Forwarding `True` would raise `NotImplementedError` from the existing
   service — guard rail at the orchestrator level so we never even attempt.)

3. **No order, watch, fill, or execution mutation can be reached** from any
   call path in this PR. The `create_decision_session_from_research_run`
   call is the only side-effecting operation and it is **decision-ledger
   only** (writes `TradingDecisionSession` + `TradingDecisionProposal`).
   This is consistent with ROB-25 §2.

4. **Schedules are disabled by default.**
   `research_run_refresh_enabled` defaults to `False`. When `False`, every
   Taskiq task short-circuits to
   `{"status": "disabled", "reason": "research_run_refresh_disabled"}`
   **before** any DB or service call. Enforced by Task 4 unit tests.

5. **Operator user is opt-in.** `research_run_refresh_user_id` defaults to
   `None`. When `None`, the orchestrator returns
   `{"status": "skipped", "reason": "no_operator_user_configured"}` without
   touching the DB. Enforced by Task 3 unit tests.

6. **Trading-hours gate (default ON).**
   `research_run_refresh_market_hours_only` defaults to `True`. When `True`,
   the orchestrator + task wrapper consult `app.core.timezone.now_kst()` and
   skip with
   `{"status": "skipped", "reason": "outside_trading_hours"}` outside the
   target window. Allowed windows per stage:

   | Stage | Allowed KST window | Cron emitters |
   |---|---|---|
   | `preopen` (08:10) | 08:00–09:30 (Mon–Fri) | preopen, regular-open |
   | `nxt_aftermarket` | 15:30–20:30 (Mon–Fri) | five aftermarket + 19:55 final |

   Outside window → `status="skipped"`. The task **never** silently
   re-tries. Enforced by Task 4 unit tests with frozen-clock fakes.

7. **No secrets, tokens, account numbers, KIS app secrets, Telegram tokens,
   or `.env` values** are logged or returned in summaries. Logging follows
   the existing pattern in `app/services/research_run_decision_session_service.py`
   (no payload echoes). Summaries return only:
   `status`, `reason` (optional), `research_run_uuid` (UUID string),
   `session_uuid` (UUID string or `None`),
   `proposal_count` (int), `reconciliation_count` (int),
   `refreshed_at` (ISO 8601 string or `None`),
   `warnings` (list of token strings — already redacted upstream).
   Enforced by Task 3 + Task 5 tests asserting no secret-shaped values
   (any string starting with `sk-`, ending with `-secret`, or matching
   account-number regex `\d{8}-\d{2}`) appears in the returned dict.

8. **DB transaction policy** mirrors ROB-25: the orchestrator opens an
   `AsyncSession`, calls the service, and **commits exactly once** at the
   end of a successful `create_decision_session_from_research_run`. On any
   skip path (`disabled` / `no_operator_user_configured` /
   `outside_trading_hours` / `no_research_run` / `empty_research_run`), the
   orchestrator MUST NOT commit. The session is closed in a `finally`.
   Enforced by Task 3 unit tests with a fake `AsyncSession` recording
   commit/rollback calls.

9. **Idempotency / dedupe is NOT introduced in this PR.** Each cron firing
   creates a new `TradingDecisionSession` if a run is found. This matches
   ROB-25 router semantics and avoids inventing a new dedupe surface that
   would need its own migration. Operators can dedupe externally via the
   `research_run_uuid` already returned in the summary. (If dedupe is later
   required, file a follow-up; do not add a Redis dedupe key in this PR.)

10. **Manual-run script is dry-run by default.** Default `--dry-run=True`
    means: resolve latest run, build snapshot, **but do not** call
    `create_decision_session_from_research_run`. Returns a printed JSON
    summary with `status="dry_run"`, `would_create=True/False`, the
    candidate count, and warnings. Enforced by Task 7 unit tests.

11. **Idle / empty / missing run paths are status codes, not exceptions.**
    `EmptyResearchRunError` and `ResearchRunNotFound` raised by the existing
    service are caught by the orchestrator and translated to
    `status="skipped"` with `reason in {"empty_research_run","no_research_run"}`.
    The orchestrator NEVER lets these escape, so a Taskiq retry loop cannot
    spam Sentry. Enforced by Task 3 tests.

12. **`is_worker_process` / `is_scheduler_process` short-circuit.** The
    Taskiq tasks must remain importable inside `app/main.py`'s API process
    without triggering DB I/O at import time. The `@broker.task` decorator
    handles this; the task body must not run any setup at module import.
    Enforced by Task 4 import test.

## 4. Schedule matrix (single source of truth)

All cron strings are **Asia/Seoul (KST)**. `1-5` = Mon–Fri.

| Task name (`task_name=`) | Function name | Cron (KST) | Stage | Market scope | Notes |
|---|---|---|---|---|---|
| `research_run.kr_preopen_refresh` | `kr_preopen_research_refresh` | `10 8 * * 1-5` | `preopen` | `kr` | Pre-open prep refresh (~50min before KR open at 09:00). |
| `research_run.kr_regular_open_refresh` | `kr_regular_open_live_refresh` | `3 9 * * 1-5` | `preopen` | `kr` | First refresh **after** KR regular session opens (09:00). Re-uses preopen run. |
| `research_run.nxt_aftermarket_refresh_1545` | `nxt_aftermarket_refresh_1545` | `45 15 * * 1-5` | `nxt_aftermarket` | `kr` | NXT post-close transition. |
| `research_run.nxt_aftermarket_refresh_1630` | `nxt_aftermarket_refresh_1630` | `30 16 * * 1-5` | `nxt_aftermarket` | `kr` | |
| `research_run.nxt_aftermarket_refresh_1730` | `nxt_aftermarket_refresh_1730` | `30 17 * * 1-5` | `nxt_aftermarket` | `kr` | |
| `research_run.nxt_aftermarket_refresh_1830` | `nxt_aftermarket_refresh_1830` | `30 18 * * 1-5` | `nxt_aftermarket` | `kr` | |
| `research_run.nxt_aftermarket_refresh_1930` | `nxt_aftermarket_refresh_1930` | `30 19 * * 1-5` | `nxt_aftermarket` | `kr` | |
| `research_run.nxt_final_check_1955` | `nxt_final_check_1955` | `55 19 * * 1-5` | `nxt_aftermarket` | `kr` | Final check ~5min before NXT close (20:00). |

Each Taskiq task:

```python
@broker.task(
    task_name="research_run.<id>",
    schedule=[{"cron": "<KST-cron>", "cron_offset": "Asia/Seoul"}],
)
async def <function_name>() -> dict:
    return await run_research_run_refresh(
        stage="<stage>", market_scope="<market_scope>"
    )
```

The function body MUST be exactly that (no extra logic) so the safety
invariants live in one place: `run_research_run_refresh`.

## 5. Settings additions (`app/core/config.py`)

Add the following fields to the existing `Settings(BaseSettings)` class
(near the other `*_enabled` flags around line 175–214):

```python
# ROB-26 — research-run refresh schedules
research_run_refresh_enabled: bool = False
research_run_refresh_user_id: int | None = None
research_run_refresh_market_hours_only: bool = True
```

`env.example` additions (under a new `# Research Run Refresh (ROB-26)`
section):

```
# Research Run Refresh (ROB-26)
# All schedules are read-only; they only refresh existing research runs and
# persist a TradingDecisionSession (decision-ledger only — never executes
# orders). Disabled by default; set _ENABLED=true to enable in your env.
RESEARCH_RUN_REFRESH_ENABLED=false
# Required when ENABLED=true. Operator user the scheduled refresh runs as.
# Leave blank to no-op (status=skipped/no_operator_user_configured).
RESEARCH_RUN_REFRESH_USER_ID=
# When true (default), the schedules skip outside KR/NXT trading-hour windows.
RESEARCH_RUN_REFRESH_MARKET_HOURS_ONLY=true
```

## 6. File layout

```
app/jobs/research_run_refresh_runner.py       # NEW — orchestrator
app/tasks/research_run_refresh_tasks.py        # NEW — Taskiq schedules
app/tasks/__init__.py                          # EDIT — add new module to TASKIQ_TASK_MODULES
app/core/config.py                             # EDIT — settings additions
env.example                                    # EDIT — doc keys
scripts/run_research_run_refresh.py            # NEW — manual entry

tests/test_research_run_refresh_runner.py      # NEW — orchestrator unit tests
tests/test_research_run_refresh_tasks.py       # NEW — task wrappers + cron strings
tests/test_research_run_refresh_import_safety.py  # NEW — forbidden imports
tests/scripts/test_run_research_run_refresh_script.py  # NEW — manual runner unit tests
```

## 7. Module-by-module design

### 7.1 Orchestrator — `app/jobs/research_run_refresh_runner.py`

Public surface (single function, deterministic dict):

```python
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, TypedDict
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db_session  # see note below
from app.core.timezone import now_kst
from app.schemas.research_run_decision_session import (
    ResearchRunDecisionSessionRequest,
    ResearchRunSelector,
)
from app.services import (
    research_run_decision_session_service,
    research_run_live_refresh_service,
)

logger = logging.getLogger(__name__)

StageLiteral = Literal["preopen", "nxt_aftermarket"]
MarketScopeLiteral = Literal["kr"]
StatusLiteral = Literal[
    "completed",
    "disabled",
    "skipped",
    "error",
]


class ResearchRunRefreshSummary(TypedDict, total=False):
    status: StatusLiteral
    reason: str
    stage: str
    market_scope: str
    research_run_uuid: str | None
    session_uuid: str | None
    proposal_count: int
    reconciliation_count: int
    refreshed_at: str | None
    warnings: list[str]


_KR_PREOPEN_WINDOW = ((8, 0), (9, 30))
_KR_NXT_WINDOW = ((15, 30), (20, 30))


def _within_window(
    *, stage: StageLiteral, now: datetime
) -> bool:
    weekday = now.weekday()  # Mon=0..Sun=6
    if weekday >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    if stage == "preopen":
        start = _KR_PREOPEN_WINDOW[0][0] * 60 + _KR_PREOPEN_WINDOW[0][1]
        end = _KR_PREOPEN_WINDOW[1][0] * 60 + _KR_PREOPEN_WINDOW[1][1]
    elif stage == "nxt_aftermarket":
        start = _KR_NXT_WINDOW[0][0] * 60 + _KR_NXT_WINDOW[0][1]
        end = _KR_NXT_WINDOW[1][0] * 60 + _KR_NXT_WINDOW[1][1]
    else:
        return False
    return start <= minutes <= end


async def run_research_run_refresh(
    *,
    stage: StageLiteral,
    market_scope: MarketScopeLiteral = "kr",
    db_factory: Callable[[], AsyncSession] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    now_local: Callable[[], datetime] = now_kst,
) -> ResearchRunRefreshSummary:
    """Read-only refresh of the latest research run for the configured operator.

    Returns a structured summary; never raises on operational skip
    conditions (disabled, no operator, outside hours, no run, empty run).
    """
    base: ResearchRunRefreshSummary = {
        "stage": stage,
        "market_scope": market_scope,
        "research_run_uuid": None,
        "session_uuid": None,
        "proposal_count": 0,
        "reconciliation_count": 0,
        "refreshed_at": None,
        "warnings": [],
    }

    if not settings.research_run_refresh_enabled:
        logger.info("research_run_refresh disabled; skipping (%s/%s)", stage, market_scope)
        return {**base, "status": "disabled", "reason": "research_run_refresh_disabled"}

    user_id = settings.research_run_refresh_user_id
    if user_id is None:
        logger.info("research_run_refresh has no operator user; skipping (%s/%s)", stage, market_scope)
        return {**base, "status": "skipped", "reason": "no_operator_user_configured"}

    if settings.research_run_refresh_market_hours_only and not _within_window(
        stage=stage, now=now_local()
    ):
        logger.info("research_run_refresh outside trading hours; skipping (%s/%s)", stage, market_scope)
        return {**base, "status": "skipped", "reason": "outside_trading_hours"}

    db_factory = db_factory or get_db_session
    async with db_factory() as db:
        try:
            try:
                research_run = await research_run_decision_session_service.resolve_research_run(
                    db,
                    user_id=user_id,
                    selector=ResearchRunSelector(
                        market_scope=market_scope,
                        stage=stage,
                        status="open",
                    ),
                )
            except research_run_decision_session_service.ResearchRunNotFound:
                return {**base, "status": "skipped", "reason": "no_research_run"}

            snapshot = await research_run_live_refresh_service.build_live_refresh_snapshot(
                db, run=research_run
            )

            try:
                result = await research_run_decision_session_service.create_decision_session_from_research_run(
                    db,
                    user_id=user_id,
                    research_run=research_run,
                    snapshot=snapshot,
                    request=ResearchRunDecisionSessionRequest(
                        selector=ResearchRunSelector(
                            run_uuid=research_run.run_uuid,
                        ),
                        include_tradingagents=False,
                        notes=f"scheduled:{stage}",
                        generated_at=None,
                    ),
                    now=now,
                )
            except research_run_decision_session_service.EmptyResearchRunError:
                return {**base, "status": "skipped", "reason": "empty_research_run",
                        "research_run_uuid": str(research_run.run_uuid)}

            await db.commit()

            return {
                **base,
                "status": "completed",
                "research_run_uuid": str(result.research_run.run_uuid),
                "session_uuid": str(result.session.session_uuid),
                "proposal_count": result.proposal_count,
                "reconciliation_count": result.reconciliation_count,
                "refreshed_at": result.refreshed_at.isoformat(),
                "warnings": list(result.warnings),
            }
        except Exception:
            await db.rollback()
            logger.exception(
                "research_run_refresh failed (stage=%s market=%s)",
                stage,
                market_scope,
            )
            raise
```

> **`get_db_session` note for Sonnet:** the existing pattern is
> `app.core.db.get_db` (FastAPI dependency, async generator). Inspect
> `app/core/db.py` first; if a non-FastAPI async session-factory helper
> already exists (e.g. `AsyncSessionLocal`, `async_session_maker`), use it
> directly and import only that. If it does not exist, do **not** invent a
> new public surface — instead, define a private `_open_db_session()`
> async-context-manager inside the runner that uses `AsyncSessionLocal()`
> (or the equivalent) from `app/core/db.py`. The `db_factory` parameter
> stays so tests can inject a fake.

### 7.2 Taskiq tasks — `app/tasks/research_run_refresh_tasks.py`

Mirrors `app/tasks/watch_proximity_tasks.py` exactly:

```python
from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.research_run_refresh_runner import run_research_run_refresh

_KST = "Asia/Seoul"


@broker.task(
    task_name="research_run.kr_preopen_refresh",
    schedule=[{"cron": "10 8 * * 1-5", "cron_offset": _KST}],
)
async def kr_preopen_research_refresh() -> dict:
    return await run_research_run_refresh(stage="preopen", market_scope="kr")


@broker.task(
    task_name="research_run.kr_regular_open_refresh",
    schedule=[{"cron": "3 9 * * 1-5", "cron_offset": _KST}],
)
async def kr_regular_open_live_refresh() -> dict:
    return await run_research_run_refresh(stage="preopen", market_scope="kr")


@broker.task(
    task_name="research_run.nxt_aftermarket_refresh_1545",
    schedule=[{"cron": "45 15 * * 1-5", "cron_offset": _KST}],
)
async def nxt_aftermarket_refresh_1545() -> dict:
    return await run_research_run_refresh(
        stage="nxt_aftermarket", market_scope="kr"
    )


@broker.task(
    task_name="research_run.nxt_aftermarket_refresh_1630",
    schedule=[{"cron": "30 16 * * 1-5", "cron_offset": _KST}],
)
async def nxt_aftermarket_refresh_1630() -> dict:
    return await run_research_run_refresh(
        stage="nxt_aftermarket", market_scope="kr"
    )


@broker.task(
    task_name="research_run.nxt_aftermarket_refresh_1730",
    schedule=[{"cron": "30 17 * * 1-5", "cron_offset": _KST}],
)
async def nxt_aftermarket_refresh_1730() -> dict:
    return await run_research_run_refresh(
        stage="nxt_aftermarket", market_scope="kr"
    )


@broker.task(
    task_name="research_run.nxt_aftermarket_refresh_1830",
    schedule=[{"cron": "30 18 * * 1-5", "cron_offset": _KST}],
)
async def nxt_aftermarket_refresh_1830() -> dict:
    return await run_research_run_refresh(
        stage="nxt_aftermarket", market_scope="kr"
    )


@broker.task(
    task_name="research_run.nxt_aftermarket_refresh_1930",
    schedule=[{"cron": "30 19 * * 1-5", "cron_offset": _KST}],
)
async def nxt_aftermarket_refresh_1930() -> dict:
    return await run_research_run_refresh(
        stage="nxt_aftermarket", market_scope="kr"
    )


@broker.task(
    task_name="research_run.nxt_final_check_1955",
    schedule=[{"cron": "55 19 * * 1-5", "cron_offset": _KST}],
)
async def nxt_final_check_1955() -> dict:
    return await run_research_run_refresh(
        stage="nxt_aftermarket", market_scope="kr"
    )
```

Then register the module in `app/tasks/__init__.py`:

```python
from app.tasks import (
    daily_scan_tasks,
    intraday_order_review_tasks,
    kr_candles_tasks,
    kr_symbol_universe_tasks,
    research_run_refresh_tasks,           # ADD
    upbit_symbol_universe_tasks,
    us_candles_tasks,
    us_symbol_universe_tasks,
    watch_proximity_tasks,
    watch_scan_tasks,
)

TASKIQ_TASK_MODULES = (
    daily_scan_tasks,
    intraday_order_review_tasks,
    research_run_refresh_tasks,           # ADD
    watch_proximity_tasks,
    watch_scan_tasks,
    kr_candles_tasks,
    kr_symbol_universe_tasks,
    upbit_symbol_universe_tasks,
    us_candles_tasks,
    us_symbol_universe_tasks,
)
```

### 7.3 Manual-run script — `scripts/run_research_run_refresh.py`

Default `--dry-run=True`. If `--dry-run`, the script resolves the run and
builds the snapshot but does **not** call
`create_decision_session_from_research_run`. Prints a JSON summary to stdout.

```python
"""Manual entry point for ROB-26 research-run refresh.

Read-only by default (dry-run). Examples:

  uv run python scripts/run_research_run_refresh.py --stage preopen
  uv run python scripts/run_research_run_refresh.py --stage nxt_aftermarket --no-dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime

from app.core.config import settings
from app.core.db import get_db_session  # or AsyncSessionLocal — match runner
from app.jobs.research_run_refresh_runner import run_research_run_refresh
from app.schemas.research_run_decision_session import ResearchRunSelector
from app.services import (
    research_run_decision_session_service,
    research_run_live_refresh_service,
)

logger = logging.getLogger(__name__)


async def _dry_run(*, stage: str, market_scope: str) -> dict:
    user_id = settings.research_run_refresh_user_id
    if user_id is None:
        return {"status": "dry_run", "reason": "no_operator_user_configured"}
    async with get_db_session() as db:
        try:
            run = await research_run_decision_session_service.resolve_research_run(
                db,
                user_id=user_id,
                selector=ResearchRunSelector(
                    market_scope=market_scope, stage=stage, status="open"
                ),
            )
        except research_run_decision_session_service.ResearchRunNotFound:
            return {"status": "dry_run", "reason": "no_research_run",
                    "would_create": False}
        snapshot = await research_run_live_refresh_service.build_live_refresh_snapshot(
            db, run=run
        )
        return {
            "status": "dry_run",
            "would_create": True,
            "research_run_uuid": str(run.run_uuid),
            "candidate_count": len(run.candidates),
            "snapshot_warnings": list(snapshot.warnings),
            "refreshed_at": snapshot.refreshed_at.isoformat(),
        }


def main() -> None:
    parser = argparse.ArgumentParser(prog="run_research_run_refresh")
    parser.add_argument("--stage", choices=["preopen", "nxt_aftermarket"], required=True)
    parser.add_argument("--market-scope", default="kr", choices=["kr"])
    parser.add_argument(
        "--dry-run", dest="dry_run", default=True, action=argparse.BooleanOptionalAction
    )
    args = parser.parse_args()

    if args.dry_run:
        result = asyncio.run(
            _dry_run(stage=args.stage, market_scope=args.market_scope)
        )
    else:
        result = asyncio.run(
            run_research_run_refresh(
                stage=args.stage, market_scope=args.market_scope
            )
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

## 8. Tasks (TDD, bite-sized)

### Task 0 — Branch sanity

**Files:** none

- [ ] **Step 1:** Confirm working tree is clean and on `feature/ROB-26-prefect-research-run-refresh`.

```bash
git status --porcelain && git rev-parse --abbrev-ref HEAD
```
Expected: empty `git status` output, branch matches.

- [ ] **Step 2:** Verify deps already include sqlalchemy.ext.asyncio and Taskiq (no install needed).

```bash
uv run python -c "import sqlalchemy.ext.asyncio, taskiq; print('ok')"
```
Expected: `ok`.

### Task 1 — Add Settings fields + env.example

**Files:**
- Modify: `app/core/config.py`
- Modify: `env.example`

- [ ] **Step 1: Write failing settings test** (`tests/test_research_run_refresh_settings.py`):

```python
"""ROB-26 settings smoke test."""
from app.core.config import settings


def test_research_run_refresh_defaults():
    assert settings.research_run_refresh_enabled is False
    assert settings.research_run_refresh_user_id is None
    assert settings.research_run_refresh_market_hours_only is True
```

- [ ] **Step 2:** Run — expect `AttributeError`:

```bash
uv run pytest tests/test_research_run_refresh_settings.py -v
```
Expected: FAIL.

- [ ] **Step 3:** Add the three fields to `Settings` in `app/core/config.py`
  near the `*_enabled` cluster:

```python
    # ROB-26 — research-run refresh schedules
    research_run_refresh_enabled: bool = False
    research_run_refresh_user_id: int | None = None
    research_run_refresh_market_hours_only: bool = True
```

- [ ] **Step 4:** Append to `env.example`:

```
# Research Run Refresh (ROB-26) — read-only schedules; disabled by default.
RESEARCH_RUN_REFRESH_ENABLED=false
RESEARCH_RUN_REFRESH_USER_ID=
RESEARCH_RUN_REFRESH_MARKET_HOURS_ONLY=true
```

- [ ] **Step 5:** Run — expect PASS:

```bash
uv run pytest tests/test_research_run_refresh_settings.py -v
```
Expected: 1 passed.

- [ ] **Step 6:** Commit.

```bash
git add app/core/config.py env.example tests/test_research_run_refresh_settings.py
git commit -m "feat(ROB-26): add research-run refresh settings"
```

### Task 2 — Orchestrator scaffolding (window helper)

**Files:**
- Create: `app/jobs/research_run_refresh_runner.py`
- Create: `tests/test_research_run_refresh_runner.py`

- [ ] **Step 1: Write failing test for `_within_window`**:

```python
from datetime import datetime
from app.jobs.research_run_refresh_runner import _within_window


def test_preopen_window_includes_0810_weekday():
    # 2026-04-29 is a Wednesday
    assert _within_window(stage="preopen", now=datetime(2026, 4, 29, 8, 10)) is True


def test_preopen_window_excludes_weekend():
    # 2026-05-02 is a Saturday
    assert _within_window(stage="preopen", now=datetime(2026, 5, 2, 8, 10)) is False


def test_preopen_window_excludes_after_0930():
    assert _within_window(stage="preopen", now=datetime(2026, 4, 29, 9, 31)) is False


def test_nxt_window_includes_1545_and_1955():
    assert _within_window(stage="nxt_aftermarket", now=datetime(2026, 4, 29, 15, 45)) is True
    assert _within_window(stage="nxt_aftermarket", now=datetime(2026, 4, 29, 19, 55)) is True


def test_nxt_window_excludes_after_2030():
    assert _within_window(stage="nxt_aftermarket", now=datetime(2026, 4, 29, 20, 31)) is False
```

- [ ] **Step 2:** Run — expect ImportError:

```bash
uv run pytest tests/test_research_run_refresh_runner.py -v
```
Expected: FAIL on import.

- [ ] **Step 3:** Create `app/jobs/research_run_refresh_runner.py` with the
  imports, the two window constants, and the `_within_window` helper from §7.1
  (do **not** add `run_research_run_refresh` yet).

- [ ] **Step 4:** Run — expect PASS:

```bash
uv run pytest tests/test_research_run_refresh_runner.py -v
```
Expected: 5 passed.

- [ ] **Step 5:** Commit.

### Task 3 — Orchestrator skip paths

**Files:**
- Modify: `app/jobs/research_run_refresh_runner.py`
- Modify: `tests/test_research_run_refresh_runner.py`

- [ ] **Step 1: Write failing tests** for each skip path. The tests must
  use `monkeypatch.setattr(settings, ...)` to flip flags, and a fake
  `db_factory` that records calls. Example:

```python
import pytest
from contextlib import asynccontextmanager
from datetime import datetime, UTC
from unittest.mock import AsyncMock

from app.core.config import settings
from app.jobs.research_run_refresh_runner import run_research_run_refresh


class _FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
    async def commit(self): self.commits += 1
    async def rollback(self): self.rollbacks += 1
    async def close(self): self.closed = True


@asynccontextmanager
async def _fake_factory():
    session = _FakeSession()
    try:
        yield session
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_disabled_short_circuits(monkeypatch):
    monkeypatch.setattr(settings, "research_run_refresh_enabled", False, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_user_id", 123, raising=False)
    result = await run_research_run_refresh(
        stage="preopen", market_scope="kr", db_factory=_fake_factory
    )
    assert result["status"] == "disabled"
    assert result["reason"] == "research_run_refresh_disabled"


@pytest.mark.asyncio
async def test_no_operator_user_skips(monkeypatch):
    monkeypatch.setattr(settings, "research_run_refresh_enabled", True, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_user_id", None, raising=False)
    result = await run_research_run_refresh(
        stage="preopen", market_scope="kr", db_factory=_fake_factory
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "no_operator_user_configured"


@pytest.mark.asyncio
async def test_outside_hours_skips(monkeypatch):
    monkeypatch.setattr(settings, "research_run_refresh_enabled", True, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_user_id", 1, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_market_hours_only", True, raising=False)
    # Saturday 08:10 — outside window because weekend
    result = await run_research_run_refresh(
        stage="preopen", market_scope="kr",
        db_factory=_fake_factory,
        now_local=lambda: datetime(2026, 5, 2, 8, 10),
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "outside_trading_hours"


@pytest.mark.asyncio
async def test_no_run_returns_skipped(monkeypatch):
    from app.services import research_run_decision_session_service as svc
    monkeypatch.setattr(settings, "research_run_refresh_enabled", True, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_user_id", 1, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_market_hours_only", False, raising=False)
    monkeypatch.setattr(
        svc, "resolve_research_run",
        AsyncMock(side_effect=svc.ResearchRunNotFound("none")),
    )
    result = await run_research_run_refresh(
        stage="preopen", market_scope="kr", db_factory=_fake_factory,
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "no_research_run"
```

- [ ] **Step 2:** Run — expect failures:

```bash
uv run pytest tests/test_research_run_refresh_runner.py -v
```
Expected: 4 failures (function not exported yet).

- [ ] **Step 3:** Implement `run_research_run_refresh` per §7.1, with the
  full guard sequence: disabled → no-user → outside-hours → resolve-run →
  build-snapshot → create-session. Skip paths must NOT commit. Use the
  injected `db_factory`. **Do NOT** import `app.core.db.get_db` at module
  top — import lazily inside the function (or behind a `_default_db_factory`
  helper) so tests can avoid touching the real DB.

- [ ] **Step 4:** Run — expect PASS:

```bash
uv run pytest tests/test_research_run_refresh_runner.py -v
```
Expected: 9 passed (5 from Task 2 + 4 here).

- [ ] **Step 5:** Add a "happy path" test that fakes
  `resolve_research_run` to return a mock run, fakes
  `build_live_refresh_snapshot` to return a mock snapshot, and fakes
  `create_decision_session_from_research_run` to return a mock result with
  `proposal_count=2`, `reconciliation_count=1`, then asserts the returned
  summary has `status="completed"`, `proposal_count=2`,
  `session_uuid=<uuid str>`, and `db.commits == 1`.

- [ ] **Step 6:** Run — expect PASS, commit.

### Task 4 — Taskiq tasks + cron strings test

**Files:**
- Create: `app/tasks/research_run_refresh_tasks.py`
- Modify: `app/tasks/__init__.py`
- Create: `tests/test_research_run_refresh_tasks.py`

- [ ] **Step 1: Write failing test** asserting the cron schedule strings
  match the §4 matrix and each task body delegates to
  `run_research_run_refresh`:

```python
import inspect
import pytest
from unittest.mock import AsyncMock, patch

from app.tasks import research_run_refresh_tasks as mod


EXPECTED_SCHEDULES = {
    "kr_preopen_research_refresh": ("10 8 * * 1-5", "preopen"),
    "kr_regular_open_live_refresh": ("3 9 * * 1-5", "preopen"),
    "nxt_aftermarket_refresh_1545": ("45 15 * * 1-5", "nxt_aftermarket"),
    "nxt_aftermarket_refresh_1630": ("30 16 * * 1-5", "nxt_aftermarket"),
    "nxt_aftermarket_refresh_1730": ("30 17 * * 1-5", "nxt_aftermarket"),
    "nxt_aftermarket_refresh_1830": ("30 18 * * 1-5", "nxt_aftermarket"),
    "nxt_aftermarket_refresh_1930": ("30 19 * * 1-5", "nxt_aftermarket"),
    "nxt_final_check_1955": ("55 19 * * 1-5", "nxt_aftermarket"),
}


def test_all_expected_tasks_exist():
    for name in EXPECTED_SCHEDULES:
        assert hasattr(mod, name), f"missing task: {name}"


def test_cron_strings_match_schedule_matrix():
    for name, (cron, _stage) in EXPECTED_SCHEDULES.items():
        task = getattr(mod, name)
        labels = getattr(task, "labels", {})
        schedules = labels.get("schedule") or []
        assert any(s.get("cron") == cron and s.get("cron_offset") == "Asia/Seoul"
                   for s in schedules), (
            f"{name}: expected cron={cron!r} cron_offset='Asia/Seoul', got {schedules}"
        )


@pytest.mark.asyncio
async def test_each_task_delegates_to_runner():
    for name, (_cron, stage) in EXPECTED_SCHEDULES.items():
        task = getattr(mod, name)
        with patch(
            "app.tasks.research_run_refresh_tasks.run_research_run_refresh",
            AsyncMock(return_value={"status": "skipped", "reason": "test"}),
        ) as runner:
            result = await task()  # invoke task body directly
            assert result == {"status": "skipped", "reason": "test"}
            runner.assert_awaited_once_with(stage=stage, market_scope="kr")
```

> **Note for Sonnet:** Taskiq exposes schedule labels via the
> `AsyncTaskiqDecoratedTask.labels` dict, but the exact attribute name has
> historically differed. Inspect a sibling like
> `watch_proximity_tasks.run_watch_proximity_task` first to confirm the
> attribute. If the attribute differs (e.g. `_schedule` or `kicker.labels`),
> adjust the assertion **once** in the test file — DO NOT change the
> production code.

- [ ] **Step 2:** Run — expect FAIL (module missing).

- [ ] **Step 3:** Create `app/tasks/research_run_refresh_tasks.py` with the
  8 tasks per §7.2. **Direct invocation must be possible** — Taskiq tasks
  are awaitable when invoked as `await task()`; if the broker decorator
  hides the body, expose a helper inner function (`_run`) that the task
  delegates to and reference that in tests.

- [ ] **Step 4:** Add `research_run_refresh_tasks` to both the import block
  and `TASKIQ_TASK_MODULES` in `app/tasks/__init__.py` (§7.2).

- [ ] **Step 5:** Run — expect PASS, commit.

### Task 5 — Manual-run script

**Files:**
- Create: `scripts/run_research_run_refresh.py`
- Create: `tests/scripts/test_run_research_run_refresh_script.py`

- [ ] **Step 1: Write failing test** for the dry-run path:

```python
import json
import pytest
from unittest.mock import AsyncMock, patch

from scripts import run_research_run_refresh as mod


@pytest.mark.asyncio
async def test_dry_run_no_operator(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "research_run_refresh_user_id", None, raising=False)
    result = await mod._dry_run(stage="preopen", market_scope="kr")
    assert result == {"status": "dry_run", "reason": "no_operator_user_configured"}


def test_main_dry_run_default(monkeypatch, capsys):
    from app.core.config import settings
    monkeypatch.setattr(settings, "research_run_refresh_user_id", None, raising=False)
    monkeypatch.setattr("sys.argv", ["prog", "--stage", "preopen"])
    mod.main()
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == "dry_run"
```

> **Note:** because `scripts/` may not be a package, this test imports
> the module via `import scripts.run_research_run_refresh as mod`. If the
> import fails, add an empty `scripts/__init__.py` (or use
> `importlib.util.spec_from_file_location`) — match whatever the existing
> `tests/scripts/test_smoke_tradingagents_db_ingestion.py` does.

- [ ] **Step 2:** Run — expect FAIL.

- [ ] **Step 3:** Implement `scripts/run_research_run_refresh.py` per §7.3.

- [ ] **Step 4:** Run — expect PASS.

- [ ] **Step 5:** Commit.

### Task 6 — Import-safety test

**Files:**
- Create: `tests/test_research_run_refresh_import_safety.py`

- [ ] **Step 1: Write failing test** that imports the new modules and
  introspects their `__dict__` / `sys.modules` to assert no forbidden
  module is reachable. Use the same approach as
  `tests/test_research_run_decision_session_router_safety.py` (or
  `tests/test_trading_decisions_router_safety.py`); inspect that file
  first to mirror style.

```python
"""ROB-26 forbidden-import safety test."""
import importlib
import pytest

FORBIDDEN_PREFIXES = (
    "prefect",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.order_service",
    "app.services.orders",
    "app.services.paper_trading_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.crypto_trade_cooldown_service",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.services.upbit_websocket",
    "app.services.upbit_market_websocket",
    "app.services.watch_alerts",
    "app.services.screener_service",
    "app.services.tradingagents_research_service",
    "app.mcp_server.tooling.orders_registration",
    "app.mcp_server.tooling.orders_modify_cancel",
    "app.mcp_server.tooling.orders_history",
    "app.mcp_server.tooling.paper_order_handler",
    "app.mcp_server.tooling.watch_alerts_registration",
)

# IMPORTANT: these are imports of the NEW PR modules only — we walk the
# AST / source text of the file, not sys.modules (which the live-refresh
# provider may have transitively pulled in legitimately).
MODULES_UNDER_TEST = (
    "app.jobs.research_run_refresh_runner",
    "app.tasks.research_run_refresh_tasks",
)


@pytest.mark.parametrize("module_name", MODULES_UNDER_TEST)
def test_module_does_not_import_forbidden(module_name: str) -> None:
    module = importlib.import_module(module_name)
    src = open(module.__file__).read()
    for forbidden in FORBIDDEN_PREFIXES:
        assert f"import {forbidden}" not in src, f"{module_name} imports {forbidden}"
        assert f"from {forbidden}" not in src, f"{module_name} imports from {forbidden}"
```

- [ ] **Step 2:** Run — expect PASS (the modules already follow the rule).

- [ ] **Step 3:** Commit.

### Task 7 — Smoke test

**Files:**
- Create: `tests/test_research_run_refresh_smoke.py`

- [ ] **Step 1:** Write a single end-to-end smoke that:

  1. Sets `research_run_refresh_enabled=True`,
     `research_run_refresh_user_id=999`, `market_hours_only=False`.
  2. Calls `run_research_run_refresh(stage="preopen", market_scope="kr",
     db_factory=<fake>)` where the fake `db_factory` produces an
     `AsyncSession`-shaped fake.
  3. Patches `resolve_research_run` to raise `ResearchRunNotFound`.
  4. Asserts the returned summary is exactly:

     ```
     {"status": "skipped", "reason": "no_research_run", "stage": "preopen",
      "market_scope": "kr", "research_run_uuid": None, "session_uuid": None,
      "proposal_count": 0, "reconciliation_count": 0, "refreshed_at": None,
      "warnings": []}
     ```

  5. Asserts no `commit()` was called on the fake session.
  6. Asserts no value in the returned dict matches secret-shaped patterns
     (regex `(?i)(secret|token|password|sk-)` not found in any string value).

- [ ] **Step 2:** Run — expect PASS.

- [ ] **Step 3:** Commit.

### Task 8 — Verification + finishing checks

- [ ] **Step 1:** Run the full new-test set:

```bash
uv run pytest tests/test_research_run_refresh_runner.py \
              tests/test_research_run_refresh_tasks.py \
              tests/test_research_run_refresh_import_safety.py \
              tests/test_research_run_refresh_smoke.py \
              tests/test_research_run_refresh_settings.py \
              tests/scripts/test_run_research_run_refresh_script.py -v
```
Expected: all pass.

- [ ] **Step 2:** Run the existing tests that bracket this work to confirm
  no regression:

```bash
uv run pytest tests/test_research_run_decision_session_router.py \
              tests/test_research_run_decision_session_service.py \
              tests/test_research_run_decision_session_service_safety.py \
              tests/test_research_run_live_refresh_service.py -v
```
Expected: all pass, unchanged.

- [ ] **Step 3:** Lint + types:

```bash
make lint
make typecheck
```
Expected: clean (or no new findings).

- [ ] **Step 4:** Manual smoke (no DB write):

```bash
uv run python scripts/run_research_run_refresh.py --stage preopen --dry-run
uv run python scripts/run_research_run_refresh.py --stage nxt_aftermarket --dry-run
```
Expected: prints a JSON object containing `"status": "dry_run"`.
Confirm: nothing is written to the `trading_decision_sessions` table.

- [ ] **Step 5:** Final commit (if anything still pending), push branch,
  open PR with body that links ROB-26 and references this plan path.

## 9. Self-review checklist (planner — completed)

- ✅ Spec coverage: every acceptance criterion maps to a Task — manual run
  (Task 5/8 step 4), no side effects (Task 6 + invariant §3.3),
  deployment definitions documented (this file §4 + §10), smoke (Task 7).
- ✅ No placeholders, every code step is reproducible.
- ✅ Type/name consistency: `run_research_run_refresh`,
  `_within_window`, `ResearchRunRefreshSummary`, schedule task names —
  consistent across §4, §7, §8.
- ✅ Safety invariants are testable and each is mapped to a specific Task.

## 10. Deployment / operations notes

These are doc-only (no infra changes are part of this PR).

**How to enable in dev:**

```
# .env
RESEARCH_RUN_REFRESH_ENABLED=true
RESEARCH_RUN_REFRESH_USER_ID=<your-operator-user-id>
RESEARCH_RUN_REFRESH_MARKET_HOURS_ONLY=true   # default
```

Then start the Taskiq scheduler + worker as already documented for the
other schedules:

```bash
uv run taskiq scheduler app.core.scheduler:sched
uv run taskiq worker app.core.taskiq_broker:broker
```

**How to verify a run completed:**

- Worker logs: `research_run_refresh ... status=completed`.
- Redis result backend (TTL 1h): the JSON summary is the result of the
  Taskiq task and contains `session_uuid` for the just-created decision
  session.
- DB: a new `trading_decision_sessions` row with
  `source_profile='research_run'` and `notes` matching `scheduled:<stage>`.

**How to roll back:**

- Set `RESEARCH_RUN_REFRESH_ENABLED=false` and restart the scheduler. No
  DB migrations are introduced by this PR, so rollback is a config-only
  operation.

**Future Prefect migration (out of scope for ROB-26):**

When/if the team chooses to introduce Prefect, the migration is purely
additive: a separate repo / package can `import run_research_run_refresh`
from `app.jobs.research_run_refresh_runner` and wrap it in `@flow`
decorators with `CronSchedule` deployments mirroring §4. The Taskiq
schedules can then be disabled by removing the `schedule=[...]` argument
from each `@broker.task` (the function bodies remain valid Taskiq tasks
for manual invocation via `taskiq kick`).

## 11. Handoff prompt for Sonnet implementer

> Paste the prompt below into a fresh Sonnet session running in the SAME
> worktree:
> `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-26-prefect-research-run-refresh`
> Command: `claude --model 'sonnet' --dangerously-skip-permissions`

```
You are the Sonnet implementer for ROB-26.

Worktree: /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-26-prefect-research-run-refresh
Branch:   feature/ROB-26-prefect-research-run-refresh
Plan:     docs/plans/ROB-26-prefect-research-run-refresh-plan.md

Implement the plan exactly task-by-task using superpowers:executing-plans.
Use TDD as written. Commit after every Task. Do not modify production code
outside the file list in §6.

Hard constraints (do NOT violate, even if a tool suggests it):
- This issue is read-only / decision-support only.
- Do NOT add a `prefect` dependency, do NOT import `prefect`, do NOT add
  any `@flow` decorator.
- Do NOT call or add any code that calls: place_order, modify_order,
  cancel_order, manage_watch_alerts, broker order placement APIs, paper
  orders, dry-run orders, live orders, fill notifications.
- Do NOT import from any module listed under "Forbidden imports" in plan §3.1.
- Decision Session creation is allowed only via the existing
  `create_decision_session_from_research_run` (decision-ledger only).
- TradingAgents stays advisory-only; in this PR `include_tradingagents` is
  hard-coded to False — do not expose it on the orchestrator API.
- Do not print, store, paste, or commit secrets, API keys, account
  numbers, tokens, credentials, or connection strings. If you encounter
  any in stack traces, redact as `[REDACTED]` before logging.

Operational rules:
- Stay in this worktree. Do NOT create a new worktree or branch.
- Schedules are off by default (RESEARCH_RUN_REFRESH_ENABLED=false). Do
  NOT enable them as part of this PR.
- All cron strings are KST (`cron_offset: "Asia/Seoul"`). Do not change them.
- If a step fails, fix the root cause; do not bypass with `--no-verify`.
- If you discover a planning gap (e.g. `app/core/db.py` does not expose an
  async session factory under the name the plan assumes), STOP and report
  back to the planner — do not invent a new public surface.

When all 9 tasks (Task 0..Task 8) are checked and tests are green:
1. Push the branch (do NOT force-push).
2. Open a PR titled
   "feat(ROB-26): scheduled read-only research-run refresh (Taskiq)"
   with body referencing the plan path.
3. Reply with:

   AOE_STATUS: implemented
   AOE_ISSUE: ROB-26
   AOE_ROLE: sonnet-implementer
   AOE_PR_URL: <url>
   AOE_NEXT: handoff_to_reviewer

If anything blocks (missing dep, ambiguous test fixture, unexpected DB
schema), reply with:

   AOE_STATUS: blocked
   AOE_ISSUE: ROB-26
   AOE_ROLE: sonnet-implementer
   AOE_BLOCKER: <one-paragraph description>
   AOE_NEXT: planner_review
```

---

**End of plan.**
