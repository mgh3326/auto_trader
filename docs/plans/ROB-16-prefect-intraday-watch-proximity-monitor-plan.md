# ROB-16 — Prefect Intraday Watch Proximity Monitor Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans
> (or superpowers:subagent-driven-development) to execute this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**AOE_STATUS:** implemented
**AOE_ISSUE:** ROB-16
**AOE_ROLE:** codex-implementer
**AOE_NEXT:** hand back to Opus reviewer for ROB-16 review
(`/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-16-prefect-intraday-watch-proximity-monitor`,
branch `feature/ROB-16-prefect-intraday-watch-proximity-monitor`).

- **Linear issue:** ROB-16 — Prefect intraday watch proximity monitor
- **Branch / worktree:** `feature/ROB-16-prefect-intraday-watch-proximity-monitor`
- **Status:** Implemented in scoped Taskiq/read-only form. Ready for review.
- **Planner / reviewer:** Claude Opus
- **Implementer:** Codex (`codex --yolo`), scoped to this worktree.
- **Depends on:** existing watch alert subsystem
  - `app/services/watch_alerts.py` — `WatchAlertService.list_watches()` /
    `get_watches_for_market()` (read-only listing).
  - `app/jobs/watch_scanner.py` — value-fetch dispatch (`_get_price`,
    `_get_index_price`, `_get_fx_price`, `_get_trade_value`, `_get_rsi`,
    `_get_current_value`).
  - `app/services/market_data` — `get_quote`, `get_ohlcv`.
  - `app/services/openclaw_client.py` — `WatchAlertDeliveryResult`,
    `OpenClawClient` (additive method `send_watch_proximity_alert_to_n8n`).
  - `app/core/taskiq_broker.py` — `broker` (existing scheduling primitive).

**Goal:** Add a read-only intraday watch **proximity monitor** that, during
market hours, computes how close current prices are to each registered watch
alert's trigger threshold and emits a Discord/n8n notification when proximity
crosses a configurable band (e.g. within 1%, within 0.5%, hit). The monitor
is visibility/alerting only: it never places orders, never registers/removes
watches, and never authorizes execution.

**Architecture:** Three pure modules (helpers, dedupe-key construction,
notification formatting) + one orchestrator + one task wrapper. The
orchestrator reads existing watches via `WatchAlertService.get_watches_for_market()`,
resolves current values through an **injected** `ValueResolver` callable
(production wires it to a thin adapter over `WatchScanner._get_current_value`,
tests inject fakes), classifies proximity into bands via pure helpers,
deduplicates per `(market, watch_field, band)` via Redis `SET NX EX`, and
delivers via an injected notifier (production: a new
`OpenClawClient.send_watch_proximity_alert_to_n8n` method using
`alert_type="watch_proximity"` over the existing N8N webhook). **No mutations
to watch records.**

**Tech Stack:** Python 3.13, Taskiq (existing scheduler), Redis (dedupe),
`exchange_calendars` (market hours), pytest (`unit`, `asyncio`), `fakeredis`.

---

## 0. Architectural decision — Prefect vs Taskiq

The Linear issue title says "Prefect" but auto_trader uses **Taskiq** for all
scheduled work (`app/core/taskiq_broker.py`, `app/tasks/*.py`,
`app/tasks/watch_scan_tasks.py`, `app/tasks/intraday_order_review_tasks.py`).
There is **no Prefect dependency** in `pyproject.toml`, no `app/flows/` or
`prefect/` directory, and no Prefect deployment infrastructure.

**Decision for this PR:** Implement the proximity monitor as a Taskiq-scheduled
service following the established pattern (mirrors `watch_scan_tasks.py`).
Adding Prefect as a brand-new dependency, runtime, and deployment surface for
a single read-only monitor would multiply blast radius and is **out of scope**
for ROB-16.

**Prefect compatibility note (external wrapper, not implemented here):**
Because the orchestrator is a plain async class
(`WatchProximityMonitor.run()`) with no Taskiq imports inside it, a Prefect
deployment can later wrap it without code changes:

```python
# Sketch for a future repo / external wrapper (NOT implemented in ROB-16):
# from prefect import flow
# from app.jobs.watch_proximity_monitor import WatchProximityMonitor
#
# @flow(name="watch-proximity-monitor")
# async def watch_proximity_flow() -> dict:
#     monitor = WatchProximityMonitor()
#     try:
#         return await monitor.run()
#     finally:
#         await monitor.close()
```

The Taskiq-side wrapper (`app/tasks/watch_proximity_tasks.py`) is the only
scheduling surface this PR ships. Codex MUST NOT add a `prefect` dependency,
MUST NOT import `prefect`, and MUST NOT create a `Prefect`/`flow` decorator
in this repo. If product later requires a Prefect runtime, that is a separate
ticket scoped to deployment infra.

If a reviewer insists on a Prefect dependency before merging, escalate to the
planner — do not silently add the dependency.

---

## 1. Scope check

ROB-16 is one subsystem (read-only proximity monitor). It does **not**:

- modify `app/jobs/watch_scanner.py`, `app/services/watch_alerts.py`, or any
  existing Taskiq task,
- modify `app/services/openclaw_client.py` beyond adding **one** new
  additive method `send_watch_proximity_alert_to_n8n` (see Task 6),
- introduce a Prefect dependency or flow,
- introduce a Linear API client (issue says "Discord/Linear"; Linear delivery
  is **out of scope** — handled later via the existing n8n webhook fan-out
  if/when n8n adds a Linear node),
- add or remove watch records,
- expose a UI page,
- modify ROB-1/ROB-9/ROB-13/ROB-14/ROB-15 modules.

The acceptance criteria are met by:

- a Taskiq-scheduled task that runs every 5 minutes (Asia/Seoul), gated
  per-market by `exchange_calendars` (KR/US) with crypto/fx always on,
- pure helpers for proximity math, band selection, dedupe-key construction,
  and message formatting (with the mandatory disclaimer),
- Redis-backed dedupe so the same `(market, watch_field, band)` does not
  re-notify within the configured TTL,
- unit tests for every pure helper plus orchestrator tests using fakes for
  Redis, value resolution, and notification,
- an explicit safety test that asserts the monitor module does not import
  forbidden execution surfaces.

## 2. In-scope vs Out-of-scope

| Area | In scope (this PR) | Deferred |
|---|---|---|
| `app/services/watch_proximity_helpers.py` (pure) | ✅ | — |
| `app/services/watch_proximity_dedupe.py` (Redis SET NX EX) | ✅ | — |
| `app/services/watch_proximity_notifier.py` (Protocol + N8N impl) | ✅ | — |
| `app/jobs/watch_proximity_monitor.py` (orchestrator) | ✅ | — |
| `app/tasks/watch_proximity_tasks.py` (Taskiq schedule) | ✅ | — |
| `app/core/config.py` settings additions (enabled flag, TTL, bands) | ✅ | — |
| `env.example` doc keys | ✅ | — |
| Additive `OpenClawClient.send_watch_proximity_alert_to_n8n` | ✅ | — |
| Tests: helpers, dedupe (fakeredis), orchestrator, task wrapper, import-safety | ✅ | — |
| Manual-run script `scripts/run_watch_proximity_monitor.py` | ✅ | — |
| Prefect dependency / Prefect deployment | ❌ | external infra ticket |
| Linear API delivery | ❌ | n8n fan-out follow-up |
| Modifying `WatchScanner` / `WatchAlertService` business logic | ❌ | — |
| Adding/removing watch records | ❌ — **forbidden** | — |
| Live, paper, or `dry_run=False` order placement | ❌ — **forbidden** | — |
| Reading or echoing API keys / `.env` values / tokens / passwords | ❌ — **forbidden** | — |

## 3. Safety invariants this PR MUST enforce

1. The new modules import **none** of:
   `app.services.kis_trading_service`, `app.services.kis_holdings_service`,
   `app.services.upbit_trading_service`, `app.services.order_service`,
   `app.services.orders`, `app.services.paper_trading_service`,
   `app.services.crypto_trade_cooldown_service`,
   `app.services.fill_notification`, `app.services.execution_event`,
   `app.services.screener_service`,
   `app.mcp_server.tooling.order_execution`,
   `app.mcp_server.tooling.watch_alerts_registration`,
   `app.services.tradingagents_research_service`,
   `app.services.trading_decision_service`,
   `app.services.trading_decision_synthesis*`,
   `prefect`.
   Allowed: `app.services.watch_alerts` (read-only methods only),
   `app.services.market_data`, `app.services.market_index_service`,
   `app.services.exchange_rate_service`, `app.services.openclaw_client`
   (only `OpenClawClient` and `WatchAlertDeliveryResult`),
   `app.core.config`, `app.core.timezone`, `app.core.taskiq_broker`
   (in the task wrapper module only), `app.jobs.watch_scanner`
   (only the `WatchScanner` class for the value-resolution adapter).
2. The orchestrator NEVER calls:
   `WatchAlertService.add_watch`, `WatchAlertService.remove_watch`,
   `WatchAlertService.trigger_and_remove`, `place_order`,
   `register_watch_alert*`, `create_order_intent`, `_place_order_impl`,
   anything that mutates broker or watch state. Enforced by the read-only
   assertion in Task 7's fake `WatchAlertService`.
3. The orchestrator's value resolution uses **read-only** helpers
   (`market_data.get_quote`, `market_data.get_ohlcv`,
   `market_index_service.get_kr_index_quote`,
   `exchange_rate_service.get_usd_krw_quote`, RSI computation). It does NOT
   issue any HTTP POST/PUT/DELETE itself.
4. Outside market hours (per `exchange_calendars` for KR/US; crypto/fx always
   open), the orchestrator returns a `status="skipped"` summary **without**
   calling the notifier. Configurable via
   `WATCH_PROXIMITY_MARKET_HOURS_ONLY=true` (default true).
5. Every notification body MUST include the literal disclaimer line:
   `"⚠️ Proximity alert only — final user approval is required for any order. No orders are placed automatically."`
   Enforced by Task 4 unit tests on `format_proximity_summary`.
6. Dedupe is required: per `(market, watch_field, band)` no second
   notification within `WATCH_PROXIMITY_DEDUPE_TTL_SECONDS` (default
   1800 = 30 min). Atomic via Redis `SET NX EX`. Enforced by Task 5
   fakeredis tests.
7. The monitor NEVER prints, logs, or persists raw env values, tokens, API
   keys, passwords, or connection strings. Logging follows existing
   conventions in `openclaw_client.py` (no payload echoes that contain
   secrets).
8. The monitor is **read-only** for watch records: a unit test asserts that
   after a full `run()`, the `WatchAlertService` fake observed zero calls to
   `add_watch`, `remove_watch`, or `trigger_and_remove` (Task 7).
9. TradingAgents outputs / order intents / sessions are not touched. The
   monitor does not import `app.services.trading_decision_service` or
   `app.services.trading_decision_synthesis*`. Enforced by Task 9 import-safety
   test.

## 4. Design

### 4.1 File layout

```
app/services/watch_proximity_helpers.py     # NEW — pure helpers (no I/O)
app/services/watch_proximity_dedupe.py      # NEW — Redis SET NX EX
app/services/watch_proximity_notifier.py    # NEW — injectable notifier
app/jobs/watch_proximity_monitor.py         # NEW — orchestrator
app/tasks/watch_proximity_tasks.py          # NEW — Taskiq schedule
scripts/run_watch_proximity_monitor.py      # NEW — manual entry
app/core/config.py                          # EDIT — settings additions
env.example                                 # EDIT — doc keys
app/services/openclaw_client.py             # EDIT — additive method only

tests/test_watch_proximity_helpers.py       # NEW
tests/test_watch_proximity_dedupe.py        # NEW
tests/test_watch_proximity_monitor.py       # NEW
tests/test_watch_proximity_tasks.py         # NEW
tests/test_watch_proximity_import_safety.py # NEW
```

### 4.2 Data shapes

```python
# app/services/watch_proximity_helpers.py
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class BandSpec:
    name: str                # "hit" | "very_near" | "near" | ...
    within_pct: float | None # None means "at or past threshold (hit)"

@dataclass(frozen=True, slots=True)
class ProximityResult:
    current: float
    threshold: float
    operator: str            # "above" | "below"
    distance_abs: float      # absolute distance to threshold
    distance_pct: float      # 100 * distance_abs / |threshold|, always >= 0
    hit: bool                # True iff condition is satisfied
    on_approach: bool        # True iff current is on the side that has not crossed
```

### 4.3 Pure helpers (`watch_proximity_helpers.py`)

Public surface:

```python
DEFAULT_BANDS: tuple[BandSpec, ...]
PROXIMITY_DISCLAIMER: str

def compute_proximity(
    *, current: float | None, threshold: float, operator: str
) -> ProximityResult | None: ...

def select_band(
    proximity: ProximityResult, bands: tuple[BandSpec, ...] = DEFAULT_BANDS
) -> str | None: ...

def build_dedupe_key(*, market: str, watch_field: str, band: str) -> str: ...

def format_proximity_summary(
    *, market: str, rows: list[dict[str, object]], as_of_iso: str
) -> str: ...

def is_market_open_for_proximity(
    *, market: str, target_kind: str, now_utc: pd.Timestamp | None = None
) -> bool: ...
```

Rules:

- **`compute_proximity`** returns `None` if `current is None`, threshold is
  approximately zero, operator is unknown. For RSI bounded in `[0,100]` the
  same formula applies (use `abs(threshold)` denominator; guard
  `abs(threshold) < 1e-9 → None`). For `operator="above"`:
  `distance_abs = |threshold - current|`, `on_approach = current < threshold`,
  `hit = current >= threshold`. For `operator="below"`:
  `distance_abs = |current - threshold|`, `on_approach = current > threshold`,
  `hit = current <= threshold`.
- **`select_band`** iterates bands in order; picks the **first** match:
  - `BandSpec(within_pct=None)` (i.e. "hit") matches iff `proximity.hit is True`.
  - `BandSpec(within_pct=x)` matches iff `proximity.on_approach is True` and
    `proximity.distance_pct <= x`.
  - Returns the band `name`, or `None` if no band matches.
- **`build_dedupe_key`**: `f"watch:proximity:dedupe:{market}:{watch_field}:{band}"`.
- **`format_proximity_summary`**: returns a multi-line string starting with
  `"Watch proximity ({market}) as of {as_of_iso}"` and ending with the
  literal disclaimer line from §3-(5). Each row line:
  `f"- {symbol} {condition_type} threshold={threshold:.4f} current={current:.4f} distance={distance_pct:.3f}% band={band}"`.
- **`is_market_open_for_proximity`** mirrors `WatchScanner._is_market_open`
  but is a pure function with `now_utc` injectable for tests. `target_kind`
  in `{"fx","crypto"}` short-circuits to `True`. For `"asset"` and `"index"`,
  use `xcals.get_calendar("XKRX"|"XNYS")` based on `market`. Crypto market is
  always open.

### 4.4 Dedupe (`watch_proximity_dedupe.py`)

```python
class WatchProximityDedupeStore:
    def __init__(self, redis_url: str | None = None) -> None: ...
    async def claim(self, key: str, *, ttl_seconds: int) -> bool:
        """Atomic SET NX EX. Returns True iff this caller is the first
        within the TTL window (i.e. we should send)."""
    async def close(self) -> None: ...  # idempotent
```

Implementation: `redis.asyncio.from_url(settings.get_redis_url(), decode_responses=True)`,
`await client.set(key, value="1", nx=True, ex=ttl_seconds)`. Return
`bool(result)`. Connection lazily created.

### 4.5 Notifier (`watch_proximity_notifier.py`)

```python
from typing import Protocol

class ProximityNotifier(Protocol):
    async def send(
        self, *, market, rows, message, as_of_iso, correlation_id,
    ) -> WatchAlertDeliveryResult: ...

class N8nProximityNotifier:
    def __init__(self, client: OpenClawClient | None = None) -> None: ...
    async def send(self, *, market, rows, message, as_of_iso, correlation_id):
        return await self._client.send_watch_proximity_alert_to_n8n(
            message=message, market=market, triggered=rows,
            as_of=as_of_iso, correlation_id=correlation_id,
        )
```

> **Why a new transport method?** `OpenClawClient.send_watch_alert_to_n8n`
> already sends `{"alert_type":"watch", ...}` — proximity is not the same
> semantic, and the n8n workflow downstream may have side effects (Discord
> formatting, "Mark Sent" dedup) tied to the existing alert_type. To avoid
> conflating semantics, Task 6 adds **one** new method
> `send_watch_proximity_alert_to_n8n` that mirrors the retrying transport
> with `"alert_type":"watch_proximity"` and a separate logger prefix.

### 4.6 Orchestrator (`watch_proximity_monitor.py`)

```python
class WatchProximityMonitor:
    def __init__(
        self,
        *,
        watch_service: WatchAlertService | None = None,
        dedupe: WatchProximityDedupeStore | None = None,
        notifier: ProximityNotifier | None = None,
        value_resolver: Callable[..., Awaitable[float | None]] | None = None,
        bands: tuple[BandSpec, ...] = DEFAULT_BANDS,
        market_hours_only: bool | None = None,
        dedupe_ttl_seconds: int | None = None,
        now_factory: Callable[[], pd.Timestamp] | None = None,
    ) -> None: ...

    async def scan_market(self, market: str) -> dict[str, object]: ...
    async def run(self) -> dict[str, dict[str, object]]: ...
    async def close(self) -> None: ...
```

`scan_market(market)` flow:

1. Read watches: `await self._watch_service.get_watches_for_market(market)`.
2. If `market_hours_only`, filter watches by
   `is_market_open_for_proximity(market=market, target_kind=target_kind, now_utc=now)`.
   If all watches were filtered out (and `watches` was non-empty), return
   `status="skipped", reason="market_closed", alerts_sent=0, details=[]`. If
   `watches` was empty, return `status="skipped", reason="no_watch_records"`.
3. For each remaining watch:
   - `metric, operator = condition_type.rsplit("_", 1)`.
   - `current = await value_resolver(target_kind=..., metric=..., symbol=..., market=...)`.
     Wrap in try/except — log + skip on resolver failure (no notification for
     that row).
   - `proximity = compute_proximity(current=current, threshold=threshold, operator=operator)`.
   - If `proximity is None`, skip.
   - `band = select_band(proximity, self._bands)`. If `None`, skip.
   - `dedupe_key = build_dedupe_key(market=market, watch_field=field, band=band)`.
   - `claimed = await self._dedupe.claim(dedupe_key, ttl_seconds=self._dedupe_ttl)`.
   - If not claimed, skip (already notified within TTL).
   - Append row: `{target_kind, symbol, condition_type, threshold, current,
     distance_pct, band, field}`.
4. If no rows: return `status="skipped", reason="no_proximity"`. **Do not call
   the notifier.**
5. Build message via `format_proximity_summary(...)` and call
   `notifier.send(...)`.
6. Return:
   ```python
   {
     "market": market,
     "status": result.status,        # success | skipped | failed
     "reason": result.reason,        # passthrough on non-success
     "request_id": result.request_id,
     "alerts_sent": len(rows) if result.status == "success" else 0,
     "details": [message],
   }
   ```

`run()` calls `scan_market` for `("crypto", "kr", "us")` sequentially and
returns `dict[str, dict]`.

`close()` closes the dedupe store, the watch service, and the owned
`WatchScanner` (when no `value_resolver` was injected). All `close()` calls
are wrapped in try/except + debug-log so a failure does not mask the outer
result. `WatchAlertService.close()` and `WatchProximityDedupeStore.close()`
are idempotent in their existing/new contracts.

**Production wiring** (defaults inside `__init__` when args are `None`):

- `watch_service = WatchAlertService()`
- `dedupe = WatchProximityDedupeStore()` (uses `settings.get_redis_url()`)
- `notifier = N8nProximityNotifier()`
- `value_resolver` adapter:
  ```python
  scanner = WatchScanner()
  async def _resolve(*, target_kind, metric, symbol, market):
      return await scanner._get_current_value(
          target_kind=target_kind, metric=metric,
          symbol=symbol, market=market,
      )
  ```
  Calling a private method on `WatchScanner` is acceptable here as a
  minimal-blast-radius reuse. Codex MUST NOT refactor `WatchScanner` in this
  PR. The adapter is encapsulated in the orchestrator's `__init__`. The
  scanner instance is owned by the monitor and closed in `close()`.
- `market_hours_only = settings.WATCH_PROXIMITY_MARKET_HOURS_ONLY`
- `dedupe_ttl_seconds = settings.WATCH_PROXIMITY_DEDUPE_TTL_SECONDS`

### 4.7 Settings additions (`app/core/config.py`)

Add (alongside existing watch-alert / N8N settings around line 310):

```python
    WATCH_PROXIMITY_ENABLED: bool = False
    WATCH_PROXIMITY_MARKET_HOURS_ONLY: bool = True
    WATCH_PROXIMITY_DEDUPE_TTL_SECONDS: int = 1800  # 30 minutes
    WATCH_PROXIMITY_BAND_NEAR_PCT: float = 1.0
    WATCH_PROXIMITY_BAND_VERY_NEAR_PCT: float = 0.5
```

`env.example` additions (documentation only, kept aligned with defaults):

```env
# ROB-16 watch proximity monitor (read-only alerts)
WATCH_PROXIMITY_ENABLED=false
WATCH_PROXIMITY_MARKET_HOURS_ONLY=true
WATCH_PROXIMITY_DEDUPE_TTL_SECONDS=1800
WATCH_PROXIMITY_BAND_NEAR_PCT=1.0
WATCH_PROXIMITY_BAND_VERY_NEAR_PCT=0.5
```

### 4.8 Task wrapper (`app/tasks/watch_proximity_tasks.py`)

```python
from __future__ import annotations

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.jobs.watch_proximity_monitor import WatchProximityMonitor


@broker.task(
    task_name="scan.watch_proximity",
    schedule=[{"cron": "*/5 * * * *", "cron_offset": "Asia/Seoul"}],
)
async def run_watch_proximity_monitor_task() -> dict:
    if not settings.WATCH_PROXIMITY_ENABLED:
        return {"status": "skipped", "reason": "feature_disabled"}
    monitor = WatchProximityMonitor()
    try:
        return await monitor.run()
    finally:
        await monitor.close()
```

### 4.9 Manual run (`scripts/run_watch_proximity_monitor.py`)

```python
#!/usr/bin/env python3
"""Manual entry point for ROB-16 watch proximity monitor (no scheduler)."""

from __future__ import annotations

import asyncio
import json
import sys

from app.jobs.watch_proximity_monitor import WatchProximityMonitor


async def _main() -> int:
    monitor = WatchProximityMonitor()
    try:
        result = await monitor.run()
    finally:
        await monitor.close()
    json.dump(result, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
```

---

## 5. Tasks

Each task is a TDD bite. Implement in order. Commit at the end of every task.
Use `Co-Authored-By: Paperclip <noreply@paperclip.ing>` per repo convention.

### Task 1 — Pure helpers: types and `compute_proximity`

**Files:**
- Create: `app/services/watch_proximity_helpers.py`
- Test:   `tests/test_watch_proximity_helpers.py`

- [ ] **Step 1.1: Write failing tests**

```python
# tests/test_watch_proximity_helpers.py
from __future__ import annotations

import pytest

from app.services.watch_proximity_helpers import (
    BandSpec,
    ProximityResult,
    compute_proximity,
)


def test_compute_proximity_above_on_approach() -> None:
    p = compute_proximity(current=99.0, threshold=100.0, operator="above")
    assert p is not None
    assert p.hit is False
    assert p.on_approach is True
    assert p.distance_abs == pytest.approx(1.0)
    assert p.distance_pct == pytest.approx(1.0)


def test_compute_proximity_above_hit() -> None:
    p = compute_proximity(current=101.0, threshold=100.0, operator="above")
    assert p is not None
    assert p.hit is True
    assert p.on_approach is False


def test_compute_proximity_below_on_approach() -> None:
    p = compute_proximity(current=101.0, threshold=100.0, operator="below")
    assert p is not None
    assert p.hit is False
    assert p.on_approach is True
    assert p.distance_pct == pytest.approx(1.0)


def test_compute_proximity_below_hit() -> None:
    p = compute_proximity(current=99.0, threshold=100.0, operator="below")
    assert p is not None
    assert p.hit is True


def test_compute_proximity_returns_none_for_unknown_operator() -> None:
    assert compute_proximity(current=1.0, threshold=2.0, operator="equal") is None


def test_compute_proximity_returns_none_for_none_current() -> None:
    assert compute_proximity(current=None, threshold=2.0, operator="above") is None


def test_compute_proximity_returns_none_for_zero_threshold() -> None:
    assert compute_proximity(current=1.0, threshold=0.0, operator="above") is None


def test_compute_proximity_uses_absolute_denominator_for_negative_threshold() -> None:
    p = compute_proximity(current=-99.0, threshold=-100.0, operator="above")
    assert p is not None
    # With a negative threshold and "above", current=-99 IS above -100, so hit=True.
    assert p.hit is True


def test_band_spec_is_frozen() -> None:
    spec = BandSpec(name="hit", within_pct=None)
    with pytest.raises(Exception):
        spec.name = "other"  # type: ignore[misc]


def test_proximity_result_is_frozen() -> None:
    r = ProximityResult(
        current=99.0, threshold=100.0, operator="above",
        distance_abs=1.0, distance_pct=1.0, hit=False, on_approach=True,
    )
    with pytest.raises(Exception):
        r.current = 0.0  # type: ignore[misc]
```

- [ ] **Step 1.2: Run — expect import error**

`uv run pytest tests/test_watch_proximity_helpers.py -v`
Expected: collection error or `ImportError`.

- [ ] **Step 1.3: Implement `app/services/watch_proximity_helpers.py` (types + `compute_proximity` only)**

```python
"""Pure helpers for ROB-16 watch proximity monitor.

This module performs no I/O and imports nothing from app.services.* to keep
it dependency-light and trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BandSpec:
    name: str
    within_pct: float | None


@dataclass(frozen=True, slots=True)
class ProximityResult:
    current: float
    threshold: float
    operator: str
    distance_abs: float
    distance_pct: float
    hit: bool
    on_approach: bool


def compute_proximity(
    *, current: float | None, threshold: float, operator: str
) -> ProximityResult | None:
    if current is None:
        return None
    if operator not in {"above", "below"}:
        return None
    threshold_f = float(threshold)
    if abs(threshold_f) < 1e-9:
        return None
    current_f = float(current)
    if operator == "above":
        distance_abs = abs(threshold_f - current_f)
        on_approach = current_f < threshold_f
        hit = current_f >= threshold_f
    else:  # below
        distance_abs = abs(current_f - threshold_f)
        on_approach = current_f > threshold_f
        hit = current_f <= threshold_f
    distance_pct = 100.0 * distance_abs / abs(threshold_f)
    return ProximityResult(
        current=current_f,
        threshold=threshold_f,
        operator=operator,
        distance_abs=distance_abs,
        distance_pct=distance_pct,
        hit=hit,
        on_approach=on_approach,
    )
```

- [ ] **Step 1.4: Run — expect pass**

`uv run pytest tests/test_watch_proximity_helpers.py -v`

- [ ] **Step 1.5: Commit**

```bash
git add app/services/watch_proximity_helpers.py tests/test_watch_proximity_helpers.py
git commit -m "$(cat <<'EOF'
feat(rob-16): add proximity types and compute_proximity helper

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

### Task 2 — Pure helpers: `select_band`, `build_dedupe_key`

- [ ] **Step 2.1: Add failing tests**

Append to `tests/test_watch_proximity_helpers.py`:

```python
from app.services.watch_proximity_helpers import (
    DEFAULT_BANDS,
    build_dedupe_key,
    select_band,
)


def test_select_band_picks_hit_first() -> None:
    p = compute_proximity(current=101.0, threshold=100.0, operator="above")
    assert p is not None
    assert select_band(p) == "hit"


def test_select_band_picks_very_near_when_within_half_pct() -> None:
    p = compute_proximity(current=99.6, threshold=100.0, operator="above")
    assert p is not None
    assert select_band(p) == "very_near"


def test_select_band_picks_near_when_within_one_pct() -> None:
    p = compute_proximity(current=99.2, threshold=100.0, operator="above")
    assert p is not None
    assert select_band(p) == "near"


def test_select_band_returns_none_when_far() -> None:
    p = compute_proximity(current=90.0, threshold=100.0, operator="above")
    assert p is not None
    assert select_band(p) is None


def test_select_band_below_operator_very_near() -> None:
    p = compute_proximity(current=100.4, threshold=100.0, operator="below")
    assert p is not None
    assert select_band(p) == "very_near"


def test_select_band_returns_none_when_hit_disabled() -> None:
    custom = (
        BandSpec(name="very_near", within_pct=0.5),
        BandSpec(name="near", within_pct=1.0),
    )
    p = compute_proximity(current=99.0, threshold=100.0, operator="below")
    assert p is not None
    assert p.hit is True
    # Custom bands have no "hit" entry and the row is not on approach,
    # so no band matches.
    assert select_band(p, custom) is None


def test_build_dedupe_key_format() -> None:
    key = build_dedupe_key(
        market="crypto", watch_field="asset:KRW-BTC:price_above:100000000",
        band="near",
    )
    assert key == (
        "watch:proximity:dedupe:crypto:asset:KRW-BTC:price_above:100000000:near"
    )


def test_default_bands_order() -> None:
    names = tuple(b.name for b in DEFAULT_BANDS)
    assert names == ("hit", "very_near", "near")
```

- [ ] **Step 2.2: Run — expect failures**

`uv run pytest tests/test_watch_proximity_helpers.py -v`

- [ ] **Step 2.3: Extend `app/services/watch_proximity_helpers.py`**

Append:

```python
DEFAULT_BANDS: tuple[BandSpec, ...] = (
    BandSpec(name="hit", within_pct=None),
    BandSpec(name="very_near", within_pct=0.5),
    BandSpec(name="near", within_pct=1.0),
)


def select_band(
    proximity: ProximityResult,
    bands: tuple[BandSpec, ...] = DEFAULT_BANDS,
) -> str | None:
    for spec in bands:
        if spec.within_pct is None:
            if proximity.hit:
                return spec.name
            continue
        if proximity.on_approach and proximity.distance_pct <= spec.within_pct:
            return spec.name
    return None


def build_dedupe_key(*, market: str, watch_field: str, band: str) -> str:
    return f"watch:proximity:dedupe:{market}:{watch_field}:{band}"
```

- [ ] **Step 2.4: Run — expect pass**

`uv run pytest tests/test_watch_proximity_helpers.py -v`

- [ ] **Step 2.5: Commit**

```bash
git add app/services/watch_proximity_helpers.py tests/test_watch_proximity_helpers.py
git commit -m "$(cat <<'EOF'
feat(rob-16): add select_band and build_dedupe_key helpers

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

### Task 3 — Market-hours gating helper

- [ ] **Step 3.1: Add failing tests**

Append to `tests/test_watch_proximity_helpers.py`:

```python
import pandas as pd

from app.services.watch_proximity_helpers import is_market_open_for_proximity


def test_is_market_open_crypto_always_true() -> None:
    assert is_market_open_for_proximity(
        market="crypto", target_kind="asset",
        now_utc=pd.Timestamp("2026-01-01 00:00:00", tz="UTC"),
    ) is True


def test_is_market_open_kr_fx_always_true_even_on_weekend() -> None:
    # 2026-01-03 is a Saturday
    assert is_market_open_for_proximity(
        market="kr", target_kind="fx",
        now_utc=pd.Timestamp("2026-01-03 12:00:00", tz="UTC"),
    ) is True


def test_is_market_open_kr_asset_weekend_false() -> None:
    # Saturday 10:00 KST → 01:00 UTC
    assert is_market_open_for_proximity(
        market="kr", target_kind="asset",
        now_utc=pd.Timestamp("2026-01-03 01:00:00", tz="UTC"),
    ) is False


def test_is_market_open_kr_asset_weekday_open() -> None:
    # Monday 10:00 KST → 01:00 UTC (use a non-holiday Monday in 2026)
    assert is_market_open_for_proximity(
        market="kr", target_kind="asset",
        now_utc=pd.Timestamp("2026-01-05 01:00:00", tz="UTC"),
    ) is True


def test_is_market_open_us_asset_weekend_false() -> None:
    assert is_market_open_for_proximity(
        market="us", target_kind="asset",
        now_utc=pd.Timestamp("2026-01-03 18:00:00", tz="UTC"),
    ) is False
```

- [ ] **Step 3.2: Run — expect failures**

`uv run pytest tests/test_watch_proximity_helpers.py -v -k market_open`

- [ ] **Step 3.3: Implement**

Append to `app/services/watch_proximity_helpers.py`:

```python
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd


@lru_cache(maxsize=2)
def _calendar(market: str):
    if market == "kr":
        return xcals.get_calendar("XKRX")
    if market == "us":
        return xcals.get_calendar("XNYS")
    return None


def is_market_open_for_proximity(
    *,
    market: str,
    target_kind: str,
    now_utc: pd.Timestamp | None = None,
) -> bool:
    if market == "crypto":
        return True
    if target_kind in {"fx", "crypto"}:
        return True
    cal = _calendar(market)
    if cal is None:
        return False
    ts = now_utc if now_utc is not None else pd.Timestamp.now("UTC")
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    return bool(cal.is_trading_minute(ts.tz_convert(cal.tz)))
```

- [ ] **Step 3.4: Run — expect pass**

`uv run pytest tests/test_watch_proximity_helpers.py -v`

- [ ] **Step 3.5: Commit**

```bash
git add app/services/watch_proximity_helpers.py tests/test_watch_proximity_helpers.py
git commit -m "$(cat <<'EOF'
feat(rob-16): add market-hours gating helper for proximity monitor

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

### Task 4 — `format_proximity_summary` (with mandatory disclaimer)

- [ ] **Step 4.1: Add failing tests**

Append to `tests/test_watch_proximity_helpers.py`:

```python
from app.services.watch_proximity_helpers import (
    PROXIMITY_DISCLAIMER,
    format_proximity_summary,
)


def test_disclaimer_text_is_exact() -> None:
    assert PROXIMITY_DISCLAIMER == (
        "⚠️ Proximity alert only — final user approval is required for any order. "
        "No orders are placed automatically."
    )


def test_format_proximity_summary_includes_disclaimer_and_header() -> None:
    text = format_proximity_summary(
        market="crypto",
        rows=[
            {
                "symbol": "KRW-BTC",
                "condition_type": "price_above",
                "threshold": 100_000_000.0,
                "current": 99_500_000.0,
                "distance_pct": 0.5,
                "band": "very_near",
            }
        ],
        as_of_iso="2026-04-28T01:00:00+00:00",
    )
    assert "Watch proximity (crypto) as of 2026-04-28T01:00:00+00:00" in text
    assert "KRW-BTC" in text and "price_above" in text
    assert "band=very_near" in text
    assert PROXIMITY_DISCLAIMER in text
    assert text.endswith(PROXIMITY_DISCLAIMER)


def test_format_proximity_summary_multi_row_order_preserved() -> None:
    text = format_proximity_summary(
        market="kr",
        rows=[
            {"symbol": "005930", "condition_type": "price_below", "threshold": 70000.0,
             "current": 70250.0, "distance_pct": 0.357, "band": "near"},
            {"symbol": "035720", "condition_type": "price_above", "threshold": 50000.0,
             "current": 50050.0, "distance_pct": 0.1, "band": "very_near"},
        ],
        as_of_iso="2026-04-28T05:01:00+00:00",
    )
    lines = text.splitlines()
    assert any("005930" in line and "near" in line for line in lines)
    assert any("035720" in line and "very_near" in line for line in lines)
    assert lines[-1] == PROXIMITY_DISCLAIMER


def test_format_proximity_summary_empty_rows_still_includes_disclaimer() -> None:
    text = format_proximity_summary(
        market="us", rows=[], as_of_iso="2026-04-28T13:35:00+00:00",
    )
    assert "Watch proximity (us)" in text
    assert text.endswith(PROXIMITY_DISCLAIMER)
```

- [ ] **Step 4.2: Run — expect failure**

`uv run pytest tests/test_watch_proximity_helpers.py -v -k summary`

- [ ] **Step 4.3: Implement**

Append to `app/services/watch_proximity_helpers.py`:

```python
PROXIMITY_DISCLAIMER = (
    "⚠️ Proximity alert only — final user approval is required for any order. "
    "No orders are placed automatically."
)


def format_proximity_summary(
    *,
    market: str,
    rows: list[dict[str, object]],
    as_of_iso: str,
) -> str:
    lines: list[str] = [f"Watch proximity ({market}) as of {as_of_iso}"]
    for row in rows:
        symbol = str(row["symbol"])
        condition_type = str(row["condition_type"])
        threshold = float(row["threshold"])  # type: ignore[arg-type]
        current = float(row["current"])  # type: ignore[arg-type]
        distance_pct = float(row["distance_pct"])  # type: ignore[arg-type]
        band = str(row["band"])
        lines.append(
            f"- {symbol} {condition_type} threshold={threshold:.4f} "
            f"current={current:.4f} distance={distance_pct:.3f}% band={band}"
        )
    lines.append(PROXIMITY_DISCLAIMER)
    return "\n".join(lines)
```

- [ ] **Step 4.4: Run — expect pass**

`uv run pytest tests/test_watch_proximity_helpers.py -v`

- [ ] **Step 4.5: Commit**

```bash
git add app/services/watch_proximity_helpers.py tests/test_watch_proximity_helpers.py
git commit -m "$(cat <<'EOF'
feat(rob-16): add proximity summary formatter with mandatory disclaimer

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

### Task 5 — Redis dedupe (`WatchProximityDedupeStore`)

**Files:**
- Create: `app/services/watch_proximity_dedupe.py`
- Test:   `tests/test_watch_proximity_dedupe.py`

- [ ] **Step 5.1: Add failing tests with `fakeredis`**

```python
# tests/test_watch_proximity_dedupe.py
from __future__ import annotations

import pytest
import fakeredis.aioredis as fakeredis_async


@pytest.mark.asyncio
async def test_first_claim_succeeds_second_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = fakeredis_async.FakeRedis(decode_responses=True)

    import app.services.watch_proximity_dedupe as mod
    monkeypatch.setattr(
        mod.redis, "from_url", lambda *a, **kw: fake, raising=True,
    )

    from app.services.watch_proximity_dedupe import WatchProximityDedupeStore

    store = WatchProximityDedupeStore(redis_url="redis://test")
    try:
        first = await store.claim(
            "watch:proximity:dedupe:crypto:foo:near", ttl_seconds=60,
        )
        second = await store.claim(
            "watch:proximity:dedupe:crypto:foo:near", ttl_seconds=60,
        )
        assert first is True
        assert second is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fakeredis_async.FakeRedis(decode_responses=True)
    import app.services.watch_proximity_dedupe as mod
    monkeypatch.setattr(
        mod.redis, "from_url", lambda *a, **kw: fake, raising=True,
    )

    from app.services.watch_proximity_dedupe import WatchProximityDedupeStore
    store = WatchProximityDedupeStore(redis_url="redis://test")
    await store.claim("k1", ttl_seconds=60)
    await store.close()
    await store.close()  # must not raise


@pytest.mark.asyncio
async def test_distinct_keys_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fakeredis_async.FakeRedis(decode_responses=True)
    import app.services.watch_proximity_dedupe as mod
    monkeypatch.setattr(
        mod.redis, "from_url", lambda *a, **kw: fake, raising=True,
    )

    from app.services.watch_proximity_dedupe import WatchProximityDedupeStore
    store = WatchProximityDedupeStore(redis_url="redis://test")
    try:
        a = await store.claim("k:a", ttl_seconds=60)
        b = await store.claim("k:b", ttl_seconds=60)
        assert a is True and b is True
    finally:
        await store.close()
```

- [ ] **Step 5.2: Run — expect failures**

`uv run pytest tests/test_watch_proximity_dedupe.py -v`

- [ ] **Step 5.3: Implement `app/services/watch_proximity_dedupe.py`**

```python
"""Redis-backed dedupe store for ROB-16 watch proximity notifications.

Uses atomic SET NX EX so that the first caller within the TTL window claims
the slot and subsequent callers are told to skip.
"""

from __future__ import annotations

import logging

import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger(__name__)


class WatchProximityDedupeStore:
    def __init__(self, redis_url: str | None = None) -> None:
        self._redis_url = redis_url or settings.get_redis_url()
        self._redis: redis.Redis | None = None

    async def _get(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(
                self._redis_url,
                max_connections=settings.redis_max_connections,
                socket_timeout=settings.redis_socket_timeout,
                socket_connect_timeout=settings.redis_socket_connect_timeout,
                decode_responses=True,
            )
        return self._redis

    async def claim(self, key: str, *, ttl_seconds: int) -> bool:
        client = await self._get()
        result = await client.set(key, value="1", nx=True, ex=ttl_seconds)
        return bool(result)

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.close()
            except Exception as exc:
                logger.debug("dedupe close error: %s", exc)
            self._redis = None
```

- [ ] **Step 5.4: Run — expect pass**

`uv run pytest tests/test_watch_proximity_dedupe.py -v`

- [ ] **Step 5.5: Commit**

```bash
git add app/services/watch_proximity_dedupe.py tests/test_watch_proximity_dedupe.py
git commit -m "$(cat <<'EOF'
feat(rob-16): add redis-backed proximity dedupe store

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

### Task 6 — Add proximity-alert transport on `OpenClawClient` (additive)

**Files:**
- Modify: `app/services/openclaw_client.py` — append **one** new method
  `send_watch_proximity_alert_to_n8n` mirroring `send_watch_alert_to_n8n` but
  with `"alert_type": "watch_proximity"` and a separate logger prefix.

- [ ] **Step 6.1: Append the new method on `OpenClawClient`**

Open `app/services/openclaw_client.py`. Locate the end of the existing
`OpenClawClient.send_watch_alert_to_n8n` method (around line 472, after the
last `return WatchAlertDeliveryResult(status="failed", reason="request_failed")`).
Add **immediately after** that method, inside the `OpenClawClient` class
body:

```python
    async def send_watch_proximity_alert_to_n8n(
        self,
        *,
        message: str,
        market: str,
        triggered: list[dict[str, Any]],
        as_of: str,
        correlation_id: str | None = None,
    ) -> WatchAlertDeliveryResult:
        request_id = str(uuid4())
        n8n_webhook_url = settings.N8N_WATCH_ALERT_WEBHOOK_URL.strip()

        if not n8n_webhook_url:
            logger.debug(
                "N8N watch proximity skipped: correlation_id=%s market=%s reason=n8n_webhook_not_configured",
                correlation_id,
                market,
            )
            return WatchAlertDeliveryResult(
                status="skipped",
                reason="n8n_webhook_not_configured",
            )

        payload = {
            "alert_type": "watch_proximity",
            "correlation_id": correlation_id,
            "as_of": as_of,
            "market": market,
            "triggered": triggered,
            "message": message,
        }
        headers = {"Content-Type": "application/json"}

        try:
            async for attempt in _build_openclaw_retrying():
                attempt_number = attempt.retry_state.attempt_number
                with attempt:
                    logger.info(
                        "N8N watch proximity send start: correlation_id=%s request_id=%s market=%s attempt=%s",
                        correlation_id, request_id, market, attempt_number,
                    )
                    try:
                        async with httpx.AsyncClient(timeout=10) as cli:
                            res = await cli.post(
                                n8n_webhook_url, json=payload, headers=headers,
                            )
                            _ = res.raise_for_status()
                    except Exception as exc:
                        logger.warning(
                            "N8N watch proximity attempt failed: correlation_id=%s request_id=%s market=%s attempt=%s error=%s",
                            correlation_id, request_id, market,
                            attempt_number, exc,
                        )
                        raise
                    logger.info(
                        "N8N watch proximity sent: correlation_id=%s request_id=%s market=%s attempt=%s status=%s",
                        correlation_id, request_id, market,
                        attempt_number, res.status_code,
                    )
                    return WatchAlertDeliveryResult(
                        status="success",
                        request_id=request_id,
                    )
        except RetryError as exc:
            logger.error(
                "N8N watch proximity failed after retries: correlation_id=%s request_id=%s market=%s error=%s",
                correlation_id, request_id, market, exc,
            )
        except Exception as exc:
            logger.error(
                "N8N watch proximity error: correlation_id=%s request_id=%s market=%s error=%s",
                correlation_id, request_id, market, exc,
            )

        return WatchAlertDeliveryResult(status="failed", reason="request_failed")
```

- [ ] **Step 6.2: Confirm existing tests still pass (no regression)**

```bash
uv run pytest tests/test_watch_scanner.py tests/test_watch_scan_tasks.py tests/test_watch_alerts.py -v
```

Expected: all green.

- [ ] **Step 6.3: Commit**

```bash
git add app/services/openclaw_client.py
git commit -m "$(cat <<'EOF'
feat(rob-16): add additive watch_proximity transport on openclaw client

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

### Task 7 — Notifier wrapper + orchestrator (`WatchProximityMonitor`)

**Files:**
- Create: `app/services/watch_proximity_notifier.py`
- Create: `app/jobs/watch_proximity_monitor.py`
- Test:   `tests/test_watch_proximity_monitor.py`

- [ ] **Step 7.1: Add failing orchestrator tests**

```python
# tests/test_watch_proximity_monitor.py
from __future__ import annotations

from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.jobs.watch_proximity_monitor import WatchProximityMonitor
from app.services.openclaw_client import WatchAlertDeliveryResult


class _FakeWatchService:
    def __init__(
        self, rows_by_market: dict[str, list[dict[str, object]]]
    ) -> None:
        self._rows_by_market = rows_by_market
        self.add_calls: list = []
        self.remove_calls: list = []
        self.trigger_remove_calls: list = []
        self.closed = False

    async def get_watches_for_market(
        self, market: str
    ) -> list[dict[str, object]]:
        return list(self._rows_by_market.get(market, []))

    async def add_watch(self, *a, **kw):
        self.add_calls.append((a, kw))

    async def remove_watch(self, *a, **kw):
        self.remove_calls.append((a, kw))

    async def trigger_and_remove(self, *a, **kw):
        self.trigger_remove_calls.append((a, kw))

    async def close(self) -> None:
        self.closed = True


class _FakeDedupe:
    def __init__(self, claimed_keys: set[str] | None = None) -> None:
        self._claimed = set(claimed_keys or set())
        self.claim_calls: list[tuple[str, int]] = []
        self.closed = False

    async def claim(self, key: str, *, ttl_seconds: int) -> bool:
        self.claim_calls.append((key, ttl_seconds))
        if key in self._claimed:
            return False
        self._claimed.add(key)
        return True

    async def close(self) -> None:
        self.closed = True


class _FakeNotifier:
    def __init__(self, status: str = "success") -> None:
        self.status = status
        self.calls: list[dict] = []

    async def send(
        self, *, market, rows, message, as_of_iso, correlation_id,
    ) -> WatchAlertDeliveryResult:
        self.calls.append({
            "market": market, "rows": list(rows), "message": message,
            "as_of_iso": as_of_iso, "correlation_id": correlation_id,
        })
        if self.status == "success":
            return WatchAlertDeliveryResult(
                status="success", request_id="prox-1",
            )
        if self.status == "skipped":
            return WatchAlertDeliveryResult(
                status="skipped", reason="n8n_webhook_not_configured",
            )
        return WatchAlertDeliveryResult(
            status="failed", reason="request_failed",
        )


@pytest.mark.asyncio
async def test_scan_market_emits_when_band_matches_and_dedupe_claims() -> None:
    watches = _FakeWatchService(rows_by_market={
        "crypto": [
            {
                "target_kind": "asset",
                "symbol": "KRW-BTC",
                "condition_type": "price_above",
                "threshold": 100_000_000.0,
                "field": "asset:KRW-BTC:price_above:100000000",
            },
        ],
        "kr": [], "us": [],
    })
    dedupe = _FakeDedupe()
    notifier = _FakeNotifier(status="success")
    resolver = AsyncMock(return_value=99_500_000.0)  # 0.5% away → very_near

    monitor = WatchProximityMonitor(
        watch_service=watches, dedupe=dedupe, notifier=notifier,
        value_resolver=resolver, market_hours_only=False,
        dedupe_ttl_seconds=60,
        now_factory=lambda: pd.Timestamp("2026-04-28T01:00:00", tz="UTC"),
    )
    try:
        result = await monitor.scan_market("crypto")
    finally:
        await monitor.close()

    assert result["status"] == "success"
    assert result["alerts_sent"] == 1
    assert len(notifier.calls) == 1
    sent_message = notifier.calls[0]["message"]
    assert "very_near" in sent_message
    assert "final user approval is required" in sent_message
    assert dedupe.claim_calls
    assert dedupe.claim_calls[0][0].startswith(
        "watch:proximity:dedupe:crypto:asset:KRW-BTC:price_above:100000000:"
    )
    # Read-only invariant: never mutate watches
    assert watches.add_calls == []
    assert watches.remove_calls == []
    assert watches.trigger_remove_calls == []


@pytest.mark.asyncio
async def test_scan_market_skips_when_dedupe_already_claimed() -> None:
    watches = _FakeWatchService(rows_by_market={
        "crypto": [
            {
                "target_kind": "asset",
                "symbol": "KRW-BTC",
                "condition_type": "price_above",
                "threshold": 100_000_000.0,
                "field": "asset:KRW-BTC:price_above:100000000",
            },
        ], "kr": [], "us": [],
    })
    dedupe = _FakeDedupe(claimed_keys={
        "watch:proximity:dedupe:crypto:"
        "asset:KRW-BTC:price_above:100000000:very_near"
    })
    notifier = _FakeNotifier(status="success")
    resolver = AsyncMock(return_value=99_500_000.0)

    monitor = WatchProximityMonitor(
        watch_service=watches, dedupe=dedupe, notifier=notifier,
        value_resolver=resolver, market_hours_only=False,
        dedupe_ttl_seconds=60,
    )
    try:
        result = await monitor.scan_market("crypto")
    finally:
        await monitor.close()

    assert result["status"] == "skipped"
    assert result["alerts_sent"] == 0
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_scan_market_skips_when_market_closed() -> None:
    watches = _FakeWatchService(rows_by_market={
        "us": [
            {
                "target_kind": "asset", "symbol": "AAPL",
                "condition_type": "price_below", "threshold": 200.0,
                "field": "asset:AAPL:price_below:200",
            },
        ], "crypto": [], "kr": [],
    })
    dedupe = _FakeDedupe()
    notifier = _FakeNotifier()
    resolver = AsyncMock(return_value=199.0)

    # Saturday 18:00 UTC → US closed
    monitor = WatchProximityMonitor(
        watch_service=watches, dedupe=dedupe, notifier=notifier,
        value_resolver=resolver, market_hours_only=True,
        dedupe_ttl_seconds=60,
        now_factory=lambda: pd.Timestamp("2026-01-03T18:00:00", tz="UTC"),
    )
    try:
        result = await monitor.scan_market("us")
    finally:
        await monitor.close()

    assert result["status"] == "skipped"
    assert result["reason"] == "market_closed"
    assert resolver.await_count == 0
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_scan_market_skips_when_no_band_matches() -> None:
    watches = _FakeWatchService(rows_by_market={
        "crypto": [
            {
                "target_kind": "asset", "symbol": "KRW-BTC",
                "condition_type": "price_above", "threshold": 100_000_000.0,
                "field": "asset:KRW-BTC:price_above:100000000",
            },
        ], "kr": [], "us": [],
    })
    dedupe = _FakeDedupe()
    notifier = _FakeNotifier()
    resolver = AsyncMock(return_value=80_000_000.0)  # 20% away → no band

    monitor = WatchProximityMonitor(
        watch_service=watches, dedupe=dedupe, notifier=notifier,
        value_resolver=resolver, market_hours_only=False,
        dedupe_ttl_seconds=60,
    )
    try:
        result = await monitor.scan_market("crypto")
    finally:
        await monitor.close()

    assert result["status"] == "skipped"
    assert result["reason"] == "no_proximity"
    # Don't burn dedupe slots when nothing to send
    assert dedupe.claim_calls == []
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_scan_market_handles_notifier_failure() -> None:
    watches = _FakeWatchService(rows_by_market={
        "crypto": [
            {
                "target_kind": "asset", "symbol": "KRW-BTC",
                "condition_type": "price_above", "threshold": 100_000_000.0,
                "field": "asset:KRW-BTC:price_above:100000000",
            },
        ], "kr": [], "us": [],
    })
    dedupe = _FakeDedupe()
    notifier = _FakeNotifier(status="failed")
    resolver = AsyncMock(return_value=99_900_000.0)

    monitor = WatchProximityMonitor(
        watch_service=watches, dedupe=dedupe, notifier=notifier,
        value_resolver=resolver, market_hours_only=False,
        dedupe_ttl_seconds=60,
    )
    try:
        result = await monitor.scan_market("crypto")
    finally:
        await monitor.close()

    assert result["status"] == "failed"
    assert result["alerts_sent"] == 0


@pytest.mark.asyncio
async def test_run_iterates_all_three_markets_and_returns_dict() -> None:
    watches = _FakeWatchService(
        rows_by_market={"crypto": [], "kr": [], "us": []},
    )
    dedupe = _FakeDedupe()
    notifier = _FakeNotifier()
    resolver = AsyncMock(return_value=None)

    monitor = WatchProximityMonitor(
        watch_service=watches, dedupe=dedupe, notifier=notifier,
        value_resolver=resolver, market_hours_only=False,
        dedupe_ttl_seconds=60,
    )
    try:
        result = await monitor.run()
    finally:
        await monitor.close()

    assert set(result.keys()) == {"crypto", "kr", "us"}
    for v in result.values():
        assert v["alerts_sent"] == 0


@pytest.mark.asyncio
async def test_close_propagates_to_dependencies() -> None:
    watches = _FakeWatchService(
        rows_by_market={"crypto": [], "kr": [], "us": []},
    )
    dedupe = _FakeDedupe()
    notifier = _FakeNotifier()
    resolver = AsyncMock(return_value=None)

    monitor = WatchProximityMonitor(
        watch_service=watches, dedupe=dedupe, notifier=notifier,
        value_resolver=resolver, market_hours_only=False,
        dedupe_ttl_seconds=60,
    )
    await monitor.close()
    assert watches.closed is True
    assert dedupe.closed is True
```

- [ ] **Step 7.2: Run — expect failures**

`uv run pytest tests/test_watch_proximity_monitor.py -v`

- [ ] **Step 7.3: Implement `app/services/watch_proximity_notifier.py`**

```python
"""Injectable notifier protocol + default n8n implementation for ROB-16."""

from __future__ import annotations

from typing import Any, Protocol

from app.services.openclaw_client import OpenClawClient, WatchAlertDeliveryResult


class ProximityNotifier(Protocol):
    async def send(
        self,
        *,
        market: str,
        rows: list[dict[str, Any]],
        message: str,
        as_of_iso: str,
        correlation_id: str,
    ) -> WatchAlertDeliveryResult: ...


class N8nProximityNotifier:
    def __init__(self, client: OpenClawClient | None = None) -> None:
        self._client = client or OpenClawClient()

    async def send(
        self,
        *,
        market: str,
        rows: list[dict[str, Any]],
        message: str,
        as_of_iso: str,
        correlation_id: str,
    ) -> WatchAlertDeliveryResult:
        return await self._client.send_watch_proximity_alert_to_n8n(
            message=message,
            market=market,
            triggered=rows,
            as_of=as_of_iso,
            correlation_id=correlation_id,
        )
```

- [ ] **Step 7.4: Implement `app/jobs/watch_proximity_monitor.py`**

```python
"""ROB-16 read-only watch proximity monitor.

Read-only with respect to watch records. NEVER calls
WatchAlertService.add_watch / remove_watch / trigger_and_remove. NEVER places
orders. NEVER touches broker side effects.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import pandas as pd

from app.core.config import settings
from app.jobs.watch_scanner import WatchScanner
from app.services.openclaw_client import WatchAlertDeliveryResult
from app.services.watch_alerts import WatchAlertService
from app.services.watch_proximity_dedupe import WatchProximityDedupeStore
from app.services.watch_proximity_helpers import (
    DEFAULT_BANDS,
    BandSpec,
    build_dedupe_key,
    compute_proximity,
    format_proximity_summary,
    is_market_open_for_proximity,
    select_band,
)
from app.services.watch_proximity_notifier import (
    N8nProximityNotifier,
    ProximityNotifier,
)

logger = logging.getLogger(__name__)

ValueResolver = Callable[..., Awaitable[float | None]]


class WatchProximityMonitor:
    def __init__(
        self,
        *,
        watch_service: WatchAlertService | None = None,
        dedupe: WatchProximityDedupeStore | None = None,
        notifier: ProximityNotifier | None = None,
        value_resolver: ValueResolver | None = None,
        bands: tuple[BandSpec, ...] = DEFAULT_BANDS,
        market_hours_only: bool | None = None,
        dedupe_ttl_seconds: int | None = None,
        now_factory: Callable[[], pd.Timestamp] | None = None,
    ) -> None:
        self._watch_service = watch_service or WatchAlertService()
        self._dedupe = dedupe or WatchProximityDedupeStore()
        self._notifier = notifier or N8nProximityNotifier()
        self._bands = bands
        self._market_hours_only = (
            settings.WATCH_PROXIMITY_MARKET_HOURS_ONLY
            if market_hours_only is None
            else market_hours_only
        )
        self._dedupe_ttl_seconds = (
            settings.WATCH_PROXIMITY_DEDUPE_TTL_SECONDS
            if dedupe_ttl_seconds is None
            else dedupe_ttl_seconds
        )
        self._now_factory = now_factory or (lambda: pd.Timestamp.now("UTC"))
        self._scanner: WatchScanner | None = None
        if value_resolver is None:
            self._scanner = WatchScanner()
            scanner = self._scanner

            async def _resolve(
                *, target_kind: str, metric: str, symbol: str, market: str,
            ) -> float | None:
                return await scanner._get_current_value(
                    target_kind=target_kind, metric=metric,
                    symbol=symbol, market=market,
                )

            self._value_resolver: ValueResolver = _resolve
        else:
            self._value_resolver = value_resolver

    async def scan_market(self, market: str) -> dict[str, Any]:
        normalized = str(market).strip().lower()
        watches = await self._watch_service.get_watches_for_market(normalized)
        now = self._now_factory()

        gated_watches: list[dict[str, Any]] = []
        for watch in watches:
            target_kind = str(
                watch.get("target_kind") or "asset"
            ).strip().lower()
            if self._market_hours_only and not is_market_open_for_proximity(
                market=normalized, target_kind=target_kind, now_utc=now,
            ):
                continue
            gated_watches.append(watch)

        if not gated_watches:
            reason = (
                "market_closed"
                if self._market_hours_only and watches
                else "no_watch_records"
            )
            return {
                "market": normalized,
                "status": "skipped",
                "reason": reason,
                "alerts_sent": 0,
                "details": [],
            }

        rows: list[dict[str, Any]] = []
        for watch in gated_watches:
            target_kind = str(
                watch.get("target_kind") or "asset"
            ).strip().lower()
            symbol = str(watch.get("symbol") or "").strip().upper()
            condition_type = str(
                watch.get("condition_type") or ""
            ).strip().lower()
            field = str(watch.get("field") or "")
            threshold_raw = watch.get("threshold")
            try:
                threshold = float(threshold_raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if not symbol or not condition_type or not field:
                continue
            try:
                metric, operator = condition_type.rsplit("_", 1)
            except ValueError:
                continue

            try:
                current = await self._value_resolver(
                    target_kind=target_kind, metric=metric,
                    symbol=symbol, market=normalized,
                )
            except Exception as exc:
                logger.warning(
                    "proximity value_resolver failed: market=%s symbol=%s metric=%s err=%s",
                    normalized, symbol, metric, exc,
                )
                continue

            proximity = compute_proximity(
                current=current, threshold=threshold, operator=operator,
            )
            if proximity is None:
                continue
            band = select_band(proximity, self._bands)
            if band is None:
                continue

            dedupe_key = build_dedupe_key(
                market=normalized, watch_field=field, band=band,
            )
            claimed = await self._dedupe.claim(
                dedupe_key, ttl_seconds=self._dedupe_ttl_seconds,
            )
            if not claimed:
                continue

            rows.append({
                "target_kind": target_kind,
                "symbol": symbol,
                "condition_type": condition_type,
                "threshold": threshold,
                # current is non-None here because compute_proximity guards it.
                "current": float(current),  # type: ignore[arg-type]
                "distance_pct": proximity.distance_pct,
                "band": band,
                "field": field,
            })

        if not rows:
            return {
                "market": normalized,
                "status": "skipped",
                "reason": "no_proximity",
                "alerts_sent": 0,
                "details": [],
            }

        as_of_iso = now.isoformat()
        message = format_proximity_summary(
            market=normalized, rows=rows, as_of_iso=as_of_iso,
        )
        correlation_id = str(uuid4())
        result: WatchAlertDeliveryResult = await self._notifier.send(
            market=normalized, rows=rows, message=message,
            as_of_iso=as_of_iso, correlation_id=correlation_id,
        )

        return {
            "market": normalized,
            "status": result.status,
            "reason": result.reason,
            "request_id": result.request_id,
            "alerts_sent": (
                len(rows) if result.status == "success" else 0
            ),
            "details": [message],
        }

    async def run(self) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for market in ("crypto", "kr", "us"):
            results[market] = dict(await self.scan_market(market))
        return results

    async def close(self) -> None:
        try:
            await self._watch_service.close()
        except Exception as exc:
            logger.debug("watch_service close error: %s", exc)
        try:
            await self._dedupe.close()
        except Exception as exc:
            logger.debug("dedupe close error: %s", exc)
        if self._scanner is not None:
            try:
                await self._scanner.close()
            except Exception as exc:
                logger.debug("scanner close error: %s", exc)
```

- [ ] **Step 7.5: Run — expect pass**

```bash
uv run pytest \
  tests/test_watch_proximity_helpers.py \
  tests/test_watch_proximity_dedupe.py \
  tests/test_watch_proximity_monitor.py \
  -v
```

- [ ] **Step 7.6: Commit**

```bash
git add app/services/watch_proximity_notifier.py app/jobs/watch_proximity_monitor.py tests/test_watch_proximity_monitor.py
git commit -m "$(cat <<'EOF'
feat(rob-16): add read-only watch proximity monitor orchestrator

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

### Task 8 — Settings additions

- [ ] **Step 8.1: Add settings to `app/core/config.py`**

Locate the `N8N_WATCH_ALERT_WEBHOOK_URL` line (currently around line 310 in
the file). Append the new fields **immediately after** it, with the same
indentation:

```python
    WATCH_PROXIMITY_ENABLED: bool = False
    WATCH_PROXIMITY_MARKET_HOURS_ONLY: bool = True
    WATCH_PROXIMITY_DEDUPE_TTL_SECONDS: int = 1800
    WATCH_PROXIMITY_BAND_NEAR_PCT: float = 1.0
    WATCH_PROXIMITY_BAND_VERY_NEAR_PCT: float = 0.5
```

> Note: defaults are off / conservative. The bands constants are documentary
> only for ROB-16 (the orchestrator currently uses `DEFAULT_BANDS`); a future
> ticket can wire `WATCH_PROXIMITY_BAND_*_PCT` into a settings-derived bands
> tuple if customization becomes necessary.

- [ ] **Step 8.2: Add `env.example` doc keys**

Append a new section to `env.example`:

```env
# ROB-16 watch proximity monitor (read-only alerts)
WATCH_PROXIMITY_ENABLED=false
WATCH_PROXIMITY_MARKET_HOURS_ONLY=true
WATCH_PROXIMITY_DEDUPE_TTL_SECONDS=1800
WATCH_PROXIMITY_BAND_NEAR_PCT=1.0
WATCH_PROXIMITY_BAND_VERY_NEAR_PCT=0.5
```

- [ ] **Step 8.3: Run tests to ensure config import is still clean**

```bash
uv run pytest \
  tests/test_watch_proximity_helpers.py \
  tests/test_watch_proximity_monitor.py \
  tests/test_watch_proximity_dedupe.py \
  -v
```

- [ ] **Step 8.4: Commit**

```bash
git add app/core/config.py env.example
git commit -m "$(cat <<'EOF'
feat(rob-16): add proximity monitor settings and env documentation

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

### Task 9 — Import-safety test

- [ ] **Step 9.1: Add the test**

```python
# tests/test_watch_proximity_import_safety.py
from __future__ import annotations

import importlib
import sys

FORBIDDEN = (
    "app.services.kis_trading_service",
    "app.services.kis_holdings_service",
    "app.services.upbit_trading_service",
    "app.services.order_service",
    "app.services.orders",
    "app.services.paper_trading_service",
    "app.services.crypto_trade_cooldown_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.screener_service",
    "app.mcp_server.tooling.order_execution",
    "app.mcp_server.tooling.watch_alerts_registration",
    "app.services.tradingagents_research_service",
    "app.services.trading_decision_service",
    "prefect",
)

PROXIMITY_MODULES = (
    "app.services.watch_proximity_helpers",
    "app.services.watch_proximity_dedupe",
    "app.services.watch_proximity_notifier",
    "app.jobs.watch_proximity_monitor",
    "app.tasks.watch_proximity_tasks",
)


def test_proximity_modules_do_not_pull_forbidden_surfaces() -> None:
    before = set(sys.modules.keys())
    for name in PROXIMITY_MODULES:
        importlib.import_module(name)
    after = set(sys.modules.keys())
    pulled = after - before
    leaked = sorted(
        m for m in pulled
        if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)
    )
    assert leaked == [], (
        f"forbidden imports leaked via proximity modules: {leaked}"
    )


def test_orchestrator_class_does_not_expose_mutation_methods() -> None:
    from app.jobs.watch_proximity_monitor import WatchProximityMonitor

    methods = {m for m in dir(WatchProximityMonitor) if not m.startswith("_")}
    forbidden_methods = {
        "add_watch", "remove_watch", "trigger_and_remove",
        "place_order", "register_watch_alert",
    }
    assert methods.isdisjoint(forbidden_methods)
```

> Note: `test_proximity_modules_do_not_pull_forbidden_surfaces` measures the
> diff `after - before`. If a forbidden module was already imported by an
> earlier test in the run, it will not appear in the diff (it was loaded
> before the test started). That is acceptable: the goal is to catch
> direct/transitive imports introduced *by the proximity modules
> themselves*, which is what this test verifies under any test order.

- [ ] **Step 9.2: Run**

`uv run pytest tests/test_watch_proximity_import_safety.py -v`

- [ ] **Step 9.3: Commit**

```bash
git add tests/test_watch_proximity_import_safety.py
git commit -m "$(cat <<'EOF'
test(rob-16): assert proximity modules do not pull forbidden surfaces

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

### Task 10 — Taskiq schedule wrapper + manual script

**Files:**
- Create: `app/tasks/watch_proximity_tasks.py`
- Create: `scripts/run_watch_proximity_monitor.py`
- Test:   `tests/test_watch_proximity_tasks.py`

- [ ] **Step 10.1: Add failing wrapper test**

```python
# tests/test_watch_proximity_tasks.py
from __future__ import annotations

import pytest


class _FakeMonitor:
    def __init__(self, result: dict[str, object]) -> None:
        self._result = result
        self.closed = False

    async def run(self) -> dict[str, object]:
        return self._result

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_run_watch_proximity_task_returns_skipped_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.tasks.watch_proximity_tasks as mod

    monkeypatch.setattr(
        mod.settings, "WATCH_PROXIMITY_ENABLED", False, raising=True,
    )
    result = await mod.run_watch_proximity_monitor_task()
    assert result == {"status": "skipped", "reason": "feature_disabled"}


@pytest.mark.asyncio
async def test_run_watch_proximity_task_invokes_monitor_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.tasks.watch_proximity_tasks as mod

    monitor = _FakeMonitor({
        "crypto": {"alerts_sent": 0},
        "kr": {"alerts_sent": 0},
        "us": {"alerts_sent": 0},
    })
    monkeypatch.setattr(
        mod.settings, "WATCH_PROXIMITY_ENABLED", True, raising=True,
    )
    monkeypatch.setattr(mod, "WatchProximityMonitor", lambda: monitor)

    result = await mod.run_watch_proximity_monitor_task()
    assert "crypto" in result
    assert monitor.closed is True
```

- [ ] **Step 10.2: Run — expect failure**

`uv run pytest tests/test_watch_proximity_tasks.py -v`

- [ ] **Step 10.3: Implement task wrapper**

```python
# app/tasks/watch_proximity_tasks.py
from __future__ import annotations

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.jobs.watch_proximity_monitor import WatchProximityMonitor


@broker.task(
    task_name="scan.watch_proximity",
    schedule=[{"cron": "*/5 * * * *", "cron_offset": "Asia/Seoul"}],
)
async def run_watch_proximity_monitor_task() -> dict:
    if not settings.WATCH_PROXIMITY_ENABLED:
        return {"status": "skipped", "reason": "feature_disabled"}
    monitor = WatchProximityMonitor()
    try:
        return await monitor.run()
    finally:
        await monitor.close()
```

- [ ] **Step 10.4: Implement manual script**

```python
# scripts/run_watch_proximity_monitor.py
#!/usr/bin/env python3
"""Manual entry point for ROB-16 watch proximity monitor (no scheduler)."""

from __future__ import annotations

import asyncio
import json
import sys

from app.jobs.watch_proximity_monitor import WatchProximityMonitor


async def _main() -> int:
    monitor = WatchProximityMonitor()
    try:
        result = await monitor.run()
    finally:
        await monitor.close()
    json.dump(result, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
```

```bash
chmod +x scripts/run_watch_proximity_monitor.py
```

- [ ] **Step 10.5: Run — expect pass**

`uv run pytest tests/test_watch_proximity_tasks.py -v`

- [ ] **Step 10.6: Commit**

```bash
git add app/tasks/watch_proximity_tasks.py scripts/run_watch_proximity_monitor.py tests/test_watch_proximity_tasks.py
git commit -m "$(cat <<'EOF'
feat(rob-16): wire taskiq schedule and manual runner for proximity monitor

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

### Task 11 — Final verification & full-suite green

- [ ] **Step 11.1: Run lint + typecheck**

```bash
make lint
make typecheck
```

If `ruff` or `ty` reports issues in the new files, fix them in place. Do
**not** introduce broad `# noqa` or `# type: ignore` unless unavoidable; if
needed, add a one-line comment that names the specific constraint.

- [ ] **Step 11.2: Run the targeted suite**

```bash
uv run pytest \
  tests/test_watch_proximity_helpers.py \
  tests/test_watch_proximity_dedupe.py \
  tests/test_watch_proximity_monitor.py \
  tests/test_watch_proximity_tasks.py \
  tests/test_watch_proximity_import_safety.py \
  -v
```

Expected: all green.

- [ ] **Step 11.3: Run regression on existing watch suite (must remain green)**

```bash
uv run pytest \
  tests/test_watch_alerts.py \
  tests/test_watch_scanner.py \
  tests/test_watch_scan_tasks.py \
  -v
```

Expected: all green (no behavior change to existing scanner/alerts modules).

- [ ] **Step 11.4: Smoke-run the manual script with the feature disabled**

```bash
WATCH_PROXIMITY_ENABLED=false uv run python scripts/run_watch_proximity_monitor.py || true
```

> The script imports the orchestrator unconditionally. If Redis is not
> running locally, the dedupe will fail at first `claim()`. That is
> acceptable for a smoke check — the goal is to confirm the module imports
> cleanly. If Redis is running, expect a JSON dict with three markets and
> `alerts_sent: 0` (no proximity yet).

- [ ] **Step 11.5: Final commit (only if lint/typecheck modified files)**

```bash
git add -A
git commit -m "$(cat <<'EOF'
chore(rob-16): satisfy lint/typecheck for proximity monitor

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## 6. Spec coverage check

| Issue requirement | Where covered |
|---|---|
| Read active watch alerts | Task 7 — `WatchAlertService.get_watches_for_market` (read-only) |
| Determine market-hours gating for KR/US | Task 3 — `is_market_open_for_proximity` (XKRX/XNYS via `exchange_calendars`); Task 7 — gate in `scan_market` |
| Fetch latest quotes | Task 7 — `value_resolver` adapter wraps `WatchScanner._get_current_value` (price/index/fx/trade_value/rsi) |
| Compute distance to threshold (abs and pct) | Task 1 — `compute_proximity` returns `distance_abs` + `distance_pct` |
| Notify when proximity crosses configurable bands (1%, 0.5%, hit) | Tasks 2, 4, 7 — `select_band` with `DEFAULT_BANDS`; emission via `format_proximity_summary` + notifier |
| Dedupe to avoid spam | Task 5 — `WatchProximityDedupeStore` (Redis SET NX EX); Task 7 — claim before append |
| Manual + scheduled run | Task 10 — Taskiq schedule (`*/5 * * * *` Asia/Seoul) + `scripts/run_watch_proximity_monitor.py` |
| Outside market hours: skip / non-actionable summary | Task 7 — returns `status="skipped"`, `reason="market_closed"` (no notifier call) |
| Notification text says final user approval is required | Task 4 — `format_proximity_summary` always appends `PROXIMITY_DISCLAIMER` (asserted in tests) |
| No live orders / no `place_order(dry_run=False)` | §3 invariants 1–2; Task 9 import-safety test |
| No watch registration | §3 invariant 2; Task 7 read-only assertion in `_FakeWatchService` |
| No order intent creation | §3 invariant 1 (forbidden imports); Task 9 import-safety test |
| TradingAgents stays advisory_only | §3 invariant 9; Task 9 forbids `tradingagents_research_service` import |
| No secrets / env values printed or persisted | §3 invariant 7; existing logger patterns reused |
| Feature flag (off by default) | Task 8 — `WATCH_PROXIMITY_ENABLED=False`; Task 10 — task short-circuits when disabled |
| Prefect-compatible (without adding dep) | §0 — orchestrator is a plain async class; future Prefect wrapper sketch is informational only |

## 7. Handoff notes for Codex

- Implement Tasks 1–11 sequentially. Each task ends with a green test run
  and a commit. Do not collapse tasks into a single commit.
- Do **not** modify `app/jobs/watch_scanner.py`, `app/services/watch_alerts.py`,
  `app/tasks/watch_scan_tasks.py`, or any TradingAgents/decision-session
  module. The only edits to existing code are: (a) the additive method on
  `OpenClawClient` (Task 6) and (b) the additive settings + env keys
  (Task 8).
- Do **not** add a `prefect` dependency. Do **not** import `prefect` anywhere.
  See §0 for the rationale and the future-Prefect wrapper sketch
  (informational only — **not** to be committed).
- Do **not** introduce a Linear API client. The notifier delivers via the
  existing n8n webhook, which can later fan out to Linear inside n8n.
- Do **not** mutate watch records. The orchestrator must remain read-only
  with respect to Redis watch keys (`watch:alerts:*`).
- If `make lint` / `make typecheck` flag a real issue, fix it. Do not silence
  with broad `# noqa` / `# type: ignore`.
- If a required dev dep (e.g. `fakeredis`) is missing, install via
  `uv add --group test <pkg>` rather than vendoring.
- Open the PR against `main`. Title: `ROB-16: read-only watch proximity
  monitor (Taskiq)`. Body: one paragraph + the §6 coverage table.

---

## 8. Codex implementation status

Implemented the scoped ROB-16 handoff requested for this worktree:

- [x] Pure proximity helper module and tests.
- [x] Safe read-only monitor job and tests with injected dependencies.
- [x] Dedupe by watch proximity band using an injectable store and Redis default.
- [x] Taskiq task wrapper and task-discovery wiring.
- [x] Safety regression tests for advisory decision/session paths.
- [x] Ruff lint and format checks for new proximity files.
- [x] Plan status updated.

Notes:

- No live or paper orders are placed.
- The proximity monitor does not register, trigger, remove, or otherwise mutate
  active watches.
- No order intents are created.
- Notification text includes: "This is an informational alert only; any order
  requires final user approval."
- No optional smoke script was added; the safe callable entrypoint is
  `WatchProximityMonitor.run()` plus the scheduled Taskiq wrapper.
- The older expanded plan text above mentions separate config, notifier, and
  smoke-script tasks. The current handoff scope was intentionally narrower and
  required no config migration, no hard Prefect dependency, and no manual script.

Verification run by Codex:

```bash
uv sync --group test --group dev
uv run pytest tests/services/test_watch_proximity.py tests/jobs/test_watch_proximity_monitor.py tests/tasks/test_watch_proximity_tasks.py -q
uv run pytest tests/test_trading_decisions_router_safety.py tests/services/test_trading_decision_synthesis_safety.py tests/services/test_operator_decision_session_safety.py -q
uv run ruff check app/services/watch_proximity.py app/jobs/watch_proximity_monitor.py app/tasks/watch_proximity_tasks.py tests/services/test_watch_proximity.py tests/jobs/test_watch_proximity_monitor.py tests/tasks/test_watch_proximity_tasks.py
uv run ruff format --check app/services/watch_proximity.py app/jobs/watch_proximity_monitor.py app/tasks/watch_proximity_tasks.py tests/services/test_watch_proximity.py tests/jobs/test_watch_proximity_monitor.py tests/tasks/test_watch_proximity_tasks.py
```

---

**AOE_STATUS:** implemented
**AOE_ISSUE:** ROB-16
**AOE_ROLE:** codex-implementer
**AOE_NEXT:** hand back to Opus reviewer for ROB-16 review.
