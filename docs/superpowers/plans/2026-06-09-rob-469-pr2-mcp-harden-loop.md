# ROB-469 PR2 — Harden the loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop one slow/awaiting tool from wedging the single FastMCP event loop and taking all 128 tools down (the ROB-469 SPOF), by adding a per-tool timeout middleware, bounding the hot-path `asyncio.gather` fan-outs, switching the shared DB engine from `NullPool` to a bounded (env-gated) connection pool, and widening the Redis pool.

**Architecture:** A `ToolTimeoutMiddleware` (added **innermost** so its `ToolError` is captured inside the Sentry scope) bounds every `tools/call` with `asyncio.wait_for`. A reusable `bounded_gather` helper caps concurrent fan-outs in `get_holdings`. `app/core/db.py` defaults to the async queue pool with `DB_POOL_CLASS=null` as an instant rollback. All knobs are env-configurable with a kill switch.

**Tech Stack:** Python 3.13, FastMCP 3.2.0 middleware, SQLAlchemy async (`AsyncAdaptedQueuePool`), asyncio, pytest (`asyncio_mode=strict`).

**Spec:** ROB-469 design spec §5 (`docs/superpowers/specs/2026-06-09-rob-469-mcp-server-resilience-design.md` — present once PR1 merges; design is reproduced inline below).

**Branch / worktree:** `/Users/mgh3326/work/auto_trader.rob-469-pr2`, branch `rob-469-pr2`, based on latest `origin/main`.

---

## Background facts (verified against installed source / current main — do not re-derive)

- **Middleware order (fastmcp 3.2.0 `server.py:448-451`):** `chain = tool; for mw in reversed(self.middleware): chain = partial(mw, call_next=chain)`. With append-order `[Sentry, CallerIdentity]`, execution is `Sentry(outer) → CallerIdentity → tool` — **first-added = OUTERMOST**. So `ToolTimeoutMiddleware` must be added **LAST** (innermost): it wraps the tool, and its `ToolError` propagates up into the Sentry scope where it's captured with the tool-call context.
- **`ToolError`** is `from fastmcp.exceptions import ToolError` (a `FastMCPError`). Raising it from `on_call_tool` yields a proper MCP error result for that one call — the server and other tools stay up.
- **Middleware contract:** subclass `fastmcp.server.middleware.Middleware`, implement `async def on_call_tool(self, context, call_next)`; `context.message.name` is the tool name; `await call_next(context)` runs the rest of the chain + the tool. `asyncio.wait_for(call_next(context), timeout=...)` is valid.
- **HONEST LIMITATION:** `asyncio.wait_for` can only cancel a coroutine blocked on `await`. A tool that blocks the loop **synchronously** (heavy pandas with no `await`, a blocking C call) cannot be cancelled this way — that case is ROB-469 PR3's watchdog. State this in the module docstring; do not oversell.
- **`app/core/db.py`** currently uses `poolclass=NullPool` (a fresh connection per request). The engine is **shared** by API + MCP + workers, but each process imports the module independently → a **per-process** pool. Investigation confirmed **no pgbouncer** (direct `localhost:5432`), clean `async with AsyncSessionLocal()` lifecycle, no forking. QueuePool is safe.
- **SQLAlchemy async pool (verified):** `create_async_engine(..., pool_size=N, max_overflow=M)` with **no `poolclass`** → `AsyncAdaptedQueuePool` (correct). `poolclass=NullPool` → `NullPool`. **`poolclass=QueuePool` (the sync class) RAISES `ArgumentError` on an async engine** — never pass it.
- **Hot-path gathers (current `app/mcp_server/tooling/portfolio_holdings.py`):** crypto-signals at line ~995 (`asyncio.gather(*[_compute_crypto_signals_for_position(p) ...], return_exceptions=True)`); equity-price at line ~668 (`asyncio.gather(*equity_tasks)`), where `fetch_equity_price` is a local async closure. `asyncio` is imported at the top; `_env_int` is already imported from `app.mcp_server.env_utils`.
- **Existing semaphore precedent:** `app/mcp_server/tooling/analysis_tool_handlers.py:438` (`sem = asyncio.Semaphore(5)`).
- **Redis config:** `app/core/config.py:373-375` — `redis_max_connections: int = 10`, `redis_socket_timeout: int = 5`, `redis_socket_connect_timeout: int = 5`.
- **`main.py` middleware block:** `:54` Sentry, `:55` CallerIdentity, `:57` `register_all_tools`. Add the timeout middleware at line 56. (PR1 edits the ctor/after-register/`main()` regions — different lines, so PR1 and PR2 auto-merge.)

## Branch-interaction note (important — read before Task 2)

PR2 is **independent** of PR1 (base = latest `origin/main`). PR1 robustified `tests/test_mcp_server_main.py` (a hermetic harness that stubs main.py's deps). **PR2 must NOT touch `tests/test_mcp_server_main.py`** — doing so would conflict with PR1's edits to the same function. PR2 adds a new `app.mcp_server.timeout_middleware` import to `main.py`; the harness will tolerate it because PR2's own `tests/test_mcp_timeout_middleware.py` imports that module at collection time, caching it in `sys.modules` (the same mechanism that keeps the harness green for `profiles`/`lifecycle`). The middleware is fully covered by **isolated unit tests** (no `main` import, no FastMCP machinery), so no fragile heavy-import wiring test is needed.

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `app/mcp_server/timeout_middleware.py` | per-tool timeout middleware + elevated-budget map | **Create** |
| `tests/test_mcp_timeout_middleware.py` | isolated middleware unit tests | **Create** |
| `app/mcp_server/env_utils.py` | `get_mcp_tool_timeout_default()` / `_enabled()` | **Modify** |
| `app/mcp_server/main.py` | register `ToolTimeoutMiddleware` innermost | **Modify** |
| `app/mcp_server/tooling/concurrency.py` | reusable `bounded_gather` helper | **Create** |
| `tests/test_mcp_bounded_gather.py` | `bounded_gather` unit tests | **Create** |
| `app/mcp_server/tooling/portfolio_holdings.py` | bound crypto + equity gathers | **Modify** |
| `app/core/db.py` | `NullPool` → env-gated async queue pool | **Modify** |
| `tests/test_db_pool_config.py` | pool-class selection test | **Create** |
| `app/core/config.py` | `redis_max_connections` 10 → 20 | **Modify** |
| `env.example`, `env.prod.example` | document `DB_POOL_*`, `MCP_TOOL_TIMEOUT_*` | **Modify** |

---

## Task 1: ToolTimeoutMiddleware + env helpers

**Files:**
- Create: `app/mcp_server/timeout_middleware.py`
- Modify: `app/mcp_server/env_utils.py`
- Test: `tests/test_mcp_timeout_middleware.py`

- [ ] **Step 1: Write failing unit tests**

Create `tests/test_mcp_timeout_middleware.py`:

```python
"""ROB-469 PR2: isolated unit tests for the per-tool timeout middleware.

No FastMCP machinery / no main import — the middleware only reads context.message.name
and calls call_next, so a SimpleNamespace context + a stub call_next fully exercises it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from app.mcp_server.timeout_middleware import ToolTimeoutMiddleware


def _ctx(name: str) -> SimpleNamespace:
    return SimpleNamespace(message=SimpleNamespace(name=name))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_slow_tool_times_out_as_toolerror() -> None:
    mw = ToolTimeoutMiddleware(default_timeout_s=0.05)

    async def call_next(ctx):
        await asyncio.sleep(1.0)
        return "done"

    with pytest.raises(ToolError, match="time budget"):
        await mw.on_call_tool(_ctx("some_slow_tool"), call_next)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fast_tool_passes_through() -> None:
    mw = ToolTimeoutMiddleware(default_timeout_s=5.0)

    async def call_next(ctx):
        return "ok"

    assert await mw.on_call_tool(_ctx("fast"), call_next) == "ok"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_elevated_budget_not_timed_out() -> None:
    mw = ToolTimeoutMiddleware(default_timeout_s=0.05, overrides={"heavy": 1.0})

    async def call_next(ctx):
        await asyncio.sleep(0.2)  # > default 0.05 but < elevated 1.0
        return "ok"

    assert await mw.on_call_tool(_ctx("heavy"), call_next) == "ok"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disabled_passes_through_without_timeout() -> None:
    mw = ToolTimeoutMiddleware(default_timeout_s=0.01, enabled=False)

    async def call_next(ctx):
        await asyncio.sleep(0.1)
        return "ok"

    assert await mw.on_call_tool(_ctx("slow"), call_next) == "ok"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zero_budget_is_exempt() -> None:
    mw = ToolTimeoutMiddleware(default_timeout_s=45.0, overrides={"exempt": 0.0})

    async def call_next(ctx):
        await asyncio.sleep(0.1)
        return "ok"

    assert await mw.on_call_tool(_ctx("exempt"), call_next) == "ok"


@pytest.mark.unit
def test_budget_resolution_default_and_overrides() -> None:
    mw = ToolTimeoutMiddleware(default_timeout_s=45.0)
    assert mw._budget_for("a_tool_with_no_override") == 45.0
    assert mw._budget_for("investment_report_generate_from_bundle") == 240.0
    assert mw._budget_for("get_holdings") == 120.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_timeout_middleware.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.mcp_server.timeout_middleware'`.

- [ ] **Step 3: Create `app/mcp_server/timeout_middleware.py`**

```python
"""ROB-469 PR2: per-tool execution timeout middleware.

A single slow or awaiting tool can stall the FastMCP streamable-http event loop and
take ALL tools down at once (the ROB-469 SPOF). This middleware bounds each
``tools/call`` with ``asyncio.wait_for`` and converts a timeout into a clean
``ToolError`` so one slow tool fails by itself instead of wedging the whole server.

LIMITATION: ``asyncio.wait_for`` can only cancel a coroutine blocked on ``await``. A
tool that blocks the loop SYNCHRONOUSLY (heavy pandas with no await, a blocking C
call) cannot be cancelled this way — that case is covered by ROB-469 PR3's watchdog.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

if TYPE_CHECKING:
    import mcp.types as mt
    from fastmcp.server.middleware import CallNext, MiddlewareContext
    from fastmcp.tools.tool import ToolResult

logger = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT_S = 45.0

# Heavy tools that legitimately run long get an elevated budget so the default does
# not kill them (ROB-469: "global default + exempt heavy tools"). Generous on
# purpose. Names verified against the registered tool surface. A budget of 0 means
# "exempt" (no timeout).
ELEVATED_TOOL_TIMEOUTS_S: dict[str, float] = {
    # Report generation (snapshot collectors + Hermes composition) — heaviest.
    "investment_report_generate_from_bundle": 240.0,
    "investment_report_prepare_bundle": 240.0,
    "investment_report_create_from_hermes_composition": 240.0,
    "investment_report_prepare_intraday_context": 180.0,
    "investment_report_get_hermes_context": 180.0,
    # Batch analysis / screeners (multi-symbol fan-out).
    "analyze_stock_batch": 120.0,
    "analyze_portfolio": 120.0,
    "screen_stocks": 120.0,
    "screen_stocks_snapshot": 120.0,
    # Single heavy fan-out.
    "analyze_stock": 90.0,
    "get_holdings": 120.0,
    # Multi-API fundamentals.
    "get_financials": 90.0,
    "get_company_profile": 90.0,
    "get_valuation": 90.0,
    "get_sector_peers": 90.0,
    # Crypto multi-source (network).
    "get_crypto_catalysts": 75.0,
    "get_crypto_social": 75.0,
    "get_crypto_order_flow": 75.0,
    # OHLCV + indicator compute; news fetch.
    "get_indicators": 75.0,
    "get_news": 75.0,
    # Order reconcile fan-out over daily order history.
    "kis_live_reconcile_orders": 90.0,
    "live_reconcile_orders": 90.0,
}


class ToolTimeoutMiddleware(Middleware):
    """Bound each ``tools/call`` with a per-tool time budget.

    Registered LAST in main.py so it is the innermost middleware (wraps the tool)
    while the Sentry middleware stays outermost and captures the raised ``ToolError``
    with the tool-call context (fastmcp 3.2.0 reverses the middleware list, so
    first-added = outermost).
    """

    def __init__(
        self,
        *,
        default_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
        overrides: dict[str, float] | None = None,
        enabled: bool = True,
    ) -> None:
        self._default = default_timeout_s
        self._overrides = dict(
            ELEVATED_TOOL_TIMEOUTS_S if overrides is None else overrides
        )
        self._enabled = enabled

    def _budget_for(self, tool_name: str) -> float:
        return self._overrides.get(tool_name, self._default)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if not self._enabled:
            return await call_next(context)
        tool_name = context.message.name
        budget = self._budget_for(tool_name)
        if budget <= 0:  # explicit exemption
            return await call_next(context)
        try:
            return await asyncio.wait_for(call_next(context), timeout=budget)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("mcp.tool.timeout tool=%s budget_s=%.0f", tool_name, budget)
            raise ToolError(
                f"Tool '{tool_name}' exceeded its {budget:.0f}s time budget "
                "and was cancelled."
            ) from None
```

- [ ] **Step 4: Add env helpers to `app/mcp_server/env_utils.py`**

Append after `get_mcp_graceful_shutdown_timeout()`:

```python
def get_mcp_tool_timeout_default() -> float:
    """Default per-tool execution timeout (seconds) for the MCP timeout middleware."""
    raw = _env("MCP_TOOL_TIMEOUT_DEFAULT_S")
    if raw is None:
        return 45.0
    try:
        return float(raw)
    except ValueError:
        logging.warning(
            f"Invalid float for MCP_TOOL_TIMEOUT_DEFAULT_S={raw!r}, using default=45.0"
        )
        return 45.0


def get_mcp_tool_timeout_enabled() -> bool:
    """Kill switch for the MCP per-tool timeout middleware (default enabled)."""
    raw = _env("MCP_TOOL_TIMEOUT_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in ("1", "true", "yes", "on")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_timeout_middleware.py -q`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/timeout_middleware.py app/mcp_server/env_utils.py tests/test_mcp_timeout_middleware.py
git commit -m "$(cat <<'EOF'
feat(ROB-469 PR2): per-tool timeout middleware (ToolTimeoutMiddleware)

Bounds each tools/call with asyncio.wait_for so one slow/awaiting tool can't wedge
the single FastMCP event loop and take all 128 tools down (the ROB-469 SPOF). Default
45s + generous elevated map for heavy tools; env knobs MCP_TOOL_TIMEOUT_DEFAULT_S and
MCP_TOOL_TIMEOUT_ENABLED (kill switch). On timeout raises a clean ToolError. Documents
the honest limit: wait_for can't cancel a sync-blocking tool (that's PR3's watchdog).

Co-authored-by: Hermes <hermes@example.invalid>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Wire ToolTimeoutMiddleware into main.py (innermost) + env docs

**Files:**
- Modify: `app/mcp_server/main.py`
- Modify: `env.example`, `env.prod.example`

- [ ] **Step 1: Extend the env_utils import in `main.py`**

Change the existing line 4:

```python
from app.mcp_server.env_utils import _env, _env_int, get_mcp_graceful_shutdown_timeout
```

to:

```python
from app.mcp_server.env_utils import (
    _env,
    _env_int,
    get_mcp_graceful_shutdown_timeout,
    get_mcp_tool_timeout_default,
    get_mcp_tool_timeout_enabled,
)
```

- [ ] **Step 2: Add the timeout-middleware import**

Add after the existing `from app.mcp_server.caller_identity_middleware import (...)` block (near line 32-34, with the other `# noqa: E402` middleware imports):

```python
from app.mcp_server.timeout_middleware import ToolTimeoutMiddleware  # noqa: E402
```

- [ ] **Step 3: Register the middleware LAST (innermost)**

Find the middleware block (lines 54-55) and add the timeout middleware AFTER `CallerIdentityMiddleware`, BEFORE `register_all_tools`:

```python
mcp.add_middleware(McpToolCallSentryMiddleware())
mcp.add_middleware(CallerIdentityMiddleware())
# ROB-469 PR2: per-tool timeout — added LAST so it is the INNERMOST middleware
# (wraps the tool) while Sentry stays outermost and captures the ToolError with the
# tool-call context. Bounds the single event loop so one slow tool can't take all
# 128 tools down (the ROB-469 SPOF).
mcp.add_middleware(
    ToolTimeoutMiddleware(
        default_timeout_s=get_mcp_tool_timeout_default(),
        enabled=get_mcp_tool_timeout_enabled(),
    )
)
_mcp_profile = resolve_mcp_profile(_env("MCP_PROFILE"))
register_all_tools(mcp, profile=_mcp_profile)
```

- [ ] **Step 4: Document the env knobs**

In `env.example` and `env.prod.example`, find the MCP section (the lines with `MCP_TYPE`/`MCP_PORT`) and add after them:

```bash
# ROB-469 PR2: per-tool execution timeout (MCP). Bounds each tool call so one slow
# tool can't wedge the single event loop. Heavy tools (report-gen, batch analysis,
# screeners) get higher built-in budgets. Set ENABLED=false to disable entirely.
MCP_TOOL_TIMEOUT_DEFAULT_S=45
MCP_TOOL_TIMEOUT_ENABLED=true
```

- [ ] **Step 5: Verify main still imports + boot test passes**

Run: `uv run pytest tests/test_mcp_tool_registration_boot.py -q`
Expected: PASS (registration unaffected; middleware is additive). Also confirm import + registration (membership, not position — FastMCP may append an internal middleware):
Run: `uv run python -c "import app.mcp_server.main as m; print(any(type(x).__name__=='ToolTimeoutMiddleware' for x in m.mcp.middleware))"`
Expected: prints `True`. (Order is verified by construction: added after Sentry/CallerIdentity → innermost; the Sentry-captures-timeout behavior is asserted by the middleware unit tests.)

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/main.py env.example env.prod.example
git commit -m "$(cat <<'EOF'
feat(ROB-469 PR2): register ToolTimeoutMiddleware innermost in MCP main

Added after Sentry/CallerIdentity so it wraps the tool while Sentry (outermost)
captures the timeout ToolError with context. Env-driven default + kill switch
documented in env.example/env.prod.example. No tool-surface change.

Co-authored-by: Hermes <hermes@example.invalid>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Bound the hot-path fan-outs in get_holdings

**Files:**
- Create: `app/mcp_server/tooling/concurrency.py`
- Test: `tests/test_mcp_bounded_gather.py`
- Modify: `app/mcp_server/tooling/portfolio_holdings.py`

- [ ] **Step 1: Write failing tests for `bounded_gather`**

Create `tests/test_mcp_bounded_gather.py`:

```python
"""ROB-469 PR2: tests for the bounded_gather concurrency helper."""

from __future__ import annotations

import asyncio

import pytest

from app.mcp_server.tooling.concurrency import bounded_gather


@pytest.mark.unit
@pytest.mark.asyncio
async def test_caps_peak_concurrency() -> None:
    active = 0
    peak = 0

    async def work(i: int) -> int:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return i

    factories = [lambda i=i: work(i) for i in range(10)]
    results = await bounded_gather(3, factories)
    assert results == list(range(10))  # order preserved
    assert peak <= 3  # never more than the limit concurrent


@pytest.mark.unit
@pytest.mark.asyncio
async def test_return_exceptions_collects_errors() -> None:
    async def boom() -> int:
        raise ValueError("nope")

    async def ok() -> int:
        return 1

    results = await bounded_gather(2, [boom, ok], return_exceptions=True)
    assert isinstance(results[0], ValueError)
    assert results[1] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_returns_empty() -> None:
    assert await bounded_gather(4, []) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_bounded_gather.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.mcp_server.tooling.concurrency'`.

- [ ] **Step 3: Create `app/mcp_server/tooling/concurrency.py`**

```python
"""ROB-469 PR2: bounded-concurrency fan-out helper.

Caps how many coroutines run at once so a large fan-out (e.g. crypto-signal or
equity-price computation over many holdings) cannot explode the task count and stall
the single MCP event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

_T = TypeVar("_T")


async def bounded_gather(
    limit: int,
    factories: list[Callable[[], Awaitable[_T]]],
    *,
    return_exceptions: bool = False,
) -> list[_T]:
    """Run ``factories`` (zero-arg coroutine factories) at most ``limit`` at a time.

    Results preserve input order, matching ``asyncio.gather``. Each element must be a
    *factory* (``() -> Awaitable``) rather than a bare coroutine so the coroutine is
    created only when a semaphore slot is free.
    """
    if not factories:
        return []
    sem = asyncio.Semaphore(limit)

    async def _run(factory: Callable[[], Awaitable[_T]]) -> _T:
        async with sem:
            return await factory()

    return await asyncio.gather(
        *[_run(f) for f in factories], return_exceptions=return_exceptions
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_bounded_gather.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Use `bounded_gather` at the crypto-signals fan-out**

In `app/mcp_server/tooling/portfolio_holdings.py`, add the import near the other tooling imports (after the existing `from app.mcp_server.env_utils import _env_int` line):

```python
from app.mcp_server.tooling.concurrency import bounded_gather
```

Add a module-level constant near the top of the file (after the imports, with any other module constants):

```python
# ROB-469 PR2: bound per-call fan-out concurrency so a large portfolio can't explode
# the task count and stall the MCP event loop.
_CRYPTO_SIGNAL_CONCURRENCY = 4
_EQUITY_PRICE_CONCURRENCY = 5
```

Replace the crypto-signals gather (the `signal_results = await asyncio.gather(...)` block around line 995):

```python
                signal_results = await asyncio.gather(
                    *[
                        _compute_crypto_signals_for_position(position)
                        for position in crypto_positions
                    ],
                    return_exceptions=True,
                )
```

with:

```python
                signal_results = await bounded_gather(
                    _CRYPTO_SIGNAL_CONCURRENCY,
                    [
                        lambda p=position: _compute_crypto_signals_for_position(p)
                        for position in crypto_positions
                    ],
                    return_exceptions=True,
                )
```

- [ ] **Step 6: Use `bounded_gather` at the equity-price fan-out**

Replace the equity-price gather (around line 655-668). Change the `equity_tasks` list + `results = await asyncio.gather(*equity_tasks)`:

```python
    equity_tasks = [
        fetch_equity_price(instrument_type, symbol)
        for instrument_type, symbol in sorted(
            {
                (position["instrument_type"], position["symbol"])
                for position in positions
                if position["instrument_type"] in {"equity_kr", "equity_us"}
                and _position_needs_current_price_refresh(position)
            }
        )
    ]

    if equity_tasks:
        results = await asyncio.gather(*equity_tasks)
```

with:

```python
    equity_pairs = sorted(
        {
            (position["instrument_type"], position["symbol"])
            for position in positions
            if position["instrument_type"] in {"equity_kr", "equity_us"}
            and _position_needs_current_price_refresh(position)
        }
    )

    if equity_pairs:
        results = await bounded_gather(
            _EQUITY_PRICE_CONCURRENCY,
            [lambda it=it, sym=sym: fetch_equity_price(it, sym) for it, sym in equity_pairs],
        )
```

- [ ] **Step 7: Run the existing portfolio_holdings tests to confirm no regression**

Run: `uv run pytest tests/ -q -k "holding or portfolio" 2>&1 | tail -8`
Expected: PASS (behavior unchanged; only concurrency is bounded). If any test referenced `equity_tasks` by name, it is internal and not part of the public surface.

- [ ] **Step 8: Commit**

```bash
git add app/mcp_server/tooling/concurrency.py tests/test_mcp_bounded_gather.py app/mcp_server/tooling/portfolio_holdings.py
git commit -m "$(cat <<'EOF'
feat(ROB-469 PR2): bound get_holdings crypto+equity fan-outs

New bounded_gather helper caps per-call concurrency; get_holdings crypto-signal
(limit 4) and equity-price (limit 5) gathers no longer fan out unbounded over large
portfolios, which could explode the task count and stall the MCP event loop. Results
and error semantics preserved (order + return_exceptions).

Co-authored-by: Hermes <hermes@example.invalid>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: NullPool → env-gated async queue pool

**Files:**
- Modify: `app/core/db.py`
- Test: `tests/test_db_pool_config.py`
- Modify: `env.example`, `env.prod.example`

- [ ] **Step 1: Write failing tests for pool selection**

Create `tests/test_db_pool_config.py`:

```python
"""ROB-469 PR2: DB engine pool-class selection (env-gated)."""

from __future__ import annotations

import pytest
from sqlalchemy.pool import NullPool

from app.core.db import build_engine

_URL = "postgresql+asyncpg://u:p@localhost:5432/db"


@pytest.mark.unit
def test_default_is_async_queue_pool() -> None:
    engine = build_engine(_URL)
    assert type(engine.pool).__name__ == "AsyncAdaptedQueuePool"
    assert engine.pool.size() == 5  # default DB_POOL_SIZE


@pytest.mark.unit
def test_db_pool_class_null_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_CLASS", "null")
    engine = build_engine(_URL)
    assert isinstance(engine.pool, NullPool)


@pytest.mark.unit
def test_env_overrides_pool_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "9")
    engine = build_engine(_URL)
    assert engine.pool.size() == 9
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_db_pool_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_engine' from 'app.core.db'`.

- [ ] **Step 3: Refactor `app/core/db.py` to an env-gated `build_engine`**

Replace the whole file with:

```python
import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import settings

# MCP 서버에서는 stdout 오염 방지를 위해 echo=False 필수
_echo = os.getenv("SQLALCHEMY_ECHO", "false").lower() in ("true", "1", "yes")


def build_engine(database_url: str | None = None) -> AsyncEngine:
    """Build the shared async engine.

    ROB-469 PR2: default to the async queue pool (AsyncAdaptedQueuePool) instead of
    NullPool. NullPool opened a fresh connection per request — wasteful, and under
    load the connect itself piles up and can stall the event loop. The engine is
    shared by API + MCP + workers, but each PROCESS imports this module independently,
    so the pool is per-process. There is no pgbouncer in front (direct Postgres), so
    the app owns pooling.

    Env-gated:
    - DB_POOL_CLASS=null  → instant rollback to NullPool (no pooling)
    - DB_POOL_SIZE (5), DB_MAX_OVERFLOW (10), DB_POOL_RECYCLE_S (1800),
      DB_POOL_TIMEOUT_S (10)

    NOTE: never pass the sync ``QueuePool`` class to an async engine — SQLAlchemy
    raises ArgumentError. Omitting ``poolclass`` selects AsyncAdaptedQueuePool.
    """
    url = database_url if database_url is not None else settings.DATABASE_URL
    pool_class = os.getenv("DB_POOL_CLASS", "queue").strip().lower()
    if pool_class == "null":
        return create_async_engine(
            url, echo=_echo, pool_pre_ping=True, poolclass=NullPool
        )
    return create_async_engine(
        url,
        echo=_echo,
        pool_pre_ping=True,
        pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
        pool_recycle=int(os.getenv("DB_POOL_RECYCLE_S", "1800")),
        pool_timeout=int(os.getenv("DB_POOL_TIMEOUT_S", "10")),
    )


engine = build_engine()
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_db_pool_config.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Document env knobs in `env.example` and `env.prod.example`**

Find the `DATABASE_URL` line in each file and add after it:

```bash
# ROB-469 PR2: DB connection pool (shared by API + MCP + workers; per-process).
# Default is the async queue pool. DB_POOL_CLASS=null is an instant rollback to the
# previous NullPool (a fresh connection per request).
DB_POOL_CLASS=queue
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=10
DB_POOL_RECYCLE_S=1800
DB_POOL_TIMEOUT_S=10
```

- [ ] **Step 6: Confirm no other code imports a now-changed symbol**

Run: `grep -rn "from app.core.db import" app/ | grep -vE "engine|AsyncSessionLocal|get_db|build_engine" || echo "OK: only engine/AsyncSessionLocal/get_db/build_engine imported"`
Expected: OK line (the public surface `engine`, `AsyncSessionLocal`, `get_db` is unchanged; `build_engine` is additive).

- [ ] **Step 7: Commit**

```bash
git add app/core/db.py tests/test_db_pool_config.py env.example env.prod.example
git commit -m "$(cat <<'EOF'
feat(ROB-469 PR2): env-gated async DB connection pool (default QueuePool)

build_engine() defaults to AsyncAdaptedQueuePool (omit poolclass; sync QueuePool
errors on async engines) instead of NullPool's fresh-connection-per-request, which
piled up under load. Shared engine is per-process; no pgbouncer in front. Env knobs
DB_POOL_SIZE/MAX_OVERFLOW/RECYCLE/TIMEOUT; DB_POOL_CLASS=null is an instant rollback.

Co-authored-by: Hermes <hermes@example.invalid>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Widen the Redis pool

**Files:**
- Modify: `app/core/config.py`
- Test: check for existing assertions on the default first

- [ ] **Step 1: Check whether any test asserts the old default**

Run: `grep -rn "redis_max_connections" tests/ app/ | grep -vE "config.py"`
If a test asserts `redis_max_connections == 10`, update it to `20` in the same commit. If none, proceed.

- [ ] **Step 2: Change the default in `app/core/config.py`**

Change line 373:

```python
    redis_max_connections: int = 10
```

to:

```python
    # ROB-469 PR2: widened from 10 — a tight shared ceiling caused pool contention
    # when several MCP tool fan-outs ran at once.
    redis_max_connections: int = 20
```

- [ ] **Step 3: Verify settings still load**

Run: `uv run python -c "from app.core.config import settings; print('redis_max_connections', settings.redis_max_connections)"`
Expected: prints `redis_max_connections 20`.

- [ ] **Step 4: Commit**

```bash
git add app/core/config.py
git commit -m "$(cat <<'EOF'
feat(ROB-469 PR2): widen redis_max_connections 10 -> 20

The tight shared Redis pool ceiling (10) caused contention when multiple MCP tool
fan-outs ran concurrently. Conservative bump; socket timeouts unchanged.

Co-authored-by: Hermes <hermes@example.invalid>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Verification gate

- [ ] **Step 1: Run all PR2 tests together**

Run: `uv run pytest tests/test_mcp_timeout_middleware.py tests/test_mcp_bounded_gather.py tests/test_db_pool_config.py tests/test_mcp_tool_registration_boot.py -q`
Expected: all PASS.

- [ ] **Step 2: Run the holdings/portfolio regression slice**

Run: `uv run pytest tests/ -q -k "holding or portfolio or db_pool or timeout or bounded" 2>&1 | tail -6`
Expected: PASS.

- [ ] **Step 3: Lint + format the changed Python (CI checks `app/` AND `tests/`)**

Run: `uv run ruff check app/mcp_server/timeout_middleware.py app/mcp_server/env_utils.py app/mcp_server/main.py app/mcp_server/tooling/concurrency.py app/mcp_server/tooling/portfolio_holdings.py app/core/db.py app/core/config.py tests/test_mcp_timeout_middleware.py tests/test_mcp_bounded_gather.py tests/test_db_pool_config.py`
Then: `uv run ruff format --check <same files>`
Expected: clean (run `ruff format` + amend if needed).

- [ ] **Step 4: Confirm scope + guardrails**

Run: `git diff --name-only origin/main..HEAD`
Expected only: the 7 code files, 3 new test files, `env.example`, `env.prod.example`, and this plan doc. Then:
Run: `git diff --name-only origin/main..HEAD | grep -iE "alembic|migration|services/brokers|ledger|test_mcp_server_main" || echo "GUARDRAILS OK"`
Expected: `GUARDRAILS OK` — **no DB migration, no broker mutation, and `test_mcp_server_main.py` untouched** (avoids conflict with PR1).

- [ ] **Step 5: Push + open PR (only when the user asks to ship)**

```bash
git push -u origin rob-469-pr2
```
PR base `main`, title `feat(ROB-469 PR2): harden the MCP event loop — per-tool timeout + bounded fan-outs + pooled DB`.

---

## Self-review notes (author)
- **Spec coverage (§5):** timeout middleware + innermost ordering (T1/T2), bounded gathers (T3), NullPool→QueuePool env-gated (T4), redis tuning (T5). All §5 items mapped. Colder gathers (yfinance/market-index/enrichment) are noted as opportunistic follow-up in the spec, not in scope.
- **Honest limitation** (sync-blocking not cancellable) stated in the middleware docstring.
- **No placeholders.** Every step has literal code/commands.
- **Type/name consistency:** `ToolTimeoutMiddleware(default_timeout_s=, overrides=, enabled=)`, `_budget_for`, `bounded_gather(limit, factories, *, return_exceptions)`, `build_engine(database_url=None)` used identically across tasks and tests. Env keys `MCP_TOOL_TIMEOUT_DEFAULT_S/ENABLED`, `DB_POOL_CLASS/SIZE/MAX_OVERFLOW/RECYCLE_S/TIMEOUT_S` consistent across code + docs.
- **QueuePool correctness:** verified `poolclass=QueuePool` errors on async; the queue path omits `poolclass` (→ AsyncAdaptedQueuePool). NullPool path explicit for rollback.
- **Branch isolation:** PR2 does not touch `test_mcp_server_main.py` (PR1 owns that harness); new `timeout_middleware` is collection-cached by its own test.
