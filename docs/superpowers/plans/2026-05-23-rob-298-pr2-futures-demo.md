# ROB-298 PR 2 — USD-M Futures Demo Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Binance USD-M Futures Demo (`demo-fapi.binance.com`) as the canonical futures mock-trading backend with mutation-capable order execution, position/leverage/reduceOnly safety guards, and full 5-mode smoke CLI. Reuses the unified `binance_demo_order_ledger` table from PR 1 by writing `product="usdm_futures"` rows. Spot Demo (PR 1) and Futures Demo are deliberately separate adapters with independent env namespaces and disjoint host allowlists.

**Architecture:** New `app/services/brokers/binance/futures_demo/` package mirrors `spot_demo/`'s structure (errors → host_allowlist → signing → transport → preflight → dto → sizing → execution_client). Distinct env namespace (`BINANCE_FUTURES_DEMO_*`) and host allowlist (`demo-fapi.binance.com`). Cross-allowlist guard rejects requests to Spot Demo, live, or deprecated testnet hosts. The unified `binance_demo_order_ledger` table already supports `product="usdm_futures"` (PR 1 migration). Futures-specific concerns: 1x leverage forced, One-way mode required (Hedge mode fail-closed), `reduceOnly` required on close orders (not on open), post-close reconciliation must show position flat AND open orders empty.

**Tech Stack:** Python 3.13, SQLAlchemy 2.x async (reuses PR 1 ledger), httpx 0.27.x, pytest-asyncio, Pydantic v2. Worktree: `/Users/mgh3326/work/auto_trader.rob-298` (branch `rob-298` — same branch as PR 1; PR 2 builds on top once PR 1 merges) OR a new worktree `auto_trader.rob-298-pr2` rebased onto main after PR 1 lands.

**Out of scope (deferred):**
- Spot Demo (PR 1, already landed)
- TaskIQ / Prefect / scheduler activation (ROB-292)
- Hermes/Discord integration
- Binance live/mainnet `fapi.binance.com` (structurally fail-closed at transport)
- Futures-specific orderbook/feed integrations (this PR is execution + ledger + smoke only)
- Multi-symbol scalping logic (the smoke is one symbol BUY → CLOSE per invocation; no portfolio logic)

**Operator approval scope (ROB-298 issue, same as PR 1):** Actual buy/sell smoke validation on `demo-fapi.binance.com` only, capped at `10 USDT` notional, 1x leverage, with explicit CLI flag.

**Reference comment with all locked decisions:** ROB-298 comment id `d258c471-3202-444b-901b-c127f3ee44af`.

### Locked decisions for Futures Demo (from comment d258c471)

1. **Default symbol: `XRPUSDT`**
   - `BTCUSDT` excluded: MIN_NOTIONAL=50 USDT > 10 USDT cap → always blocked
   - `XRPUSDT`: TRADING, MIN_NOTIONAL=5 USDT, LOT_SIZE minQty=0.1 → fits in 10 USDT cap
   - Fallback allowlist: `DOGEUSDT`, `SOLUSDT`
   - operator CLI override allowed but exchange filter + cap must not be bypassed

2. **Max notional cap: 10 USDT** (no separate higher cap for futures)

3. **Sizing rule (same as Spot Demo)**
   - order notional ≥ exchange MIN_NOTIONAL
   - order notional ≤ 10 USDT cap
   - quantity = floor(target / price / step_size) × step_size
   - floor result < MIN_NOTIONAL → `SizingBlocked` (no round-up)

4. **Safety constraints**
   - Leverage: `1x` forced (set via `POST /fapi/v1/leverage` before open, verify echo)
   - Position mode: One-way only; Hedge mode → fail-closed (`BinanceFuturesDemoHedgeModeBlocked`)
   - `reduceOnly`: never on open; required on close (use `reduceOnly=true` to prevent reversal)
   - Post-close reconciliation: BOTH open orders empty AND position flat (size = 0). Non-flat position → record anomaly + exit code 2.

5. **Host/env namespace (strict separation)**
   - `BINANCE_FUTURES_DEMO_*` only (no shared with Spot Demo, no aliasing)
   - Base URL: `https://demo-fapi.binance.com`
   - Live `fapi.binance.com` + testnet `testnet.binancefuture.com` deny-listed at transport layer

---

## Foundation already in place (from PR 1)

- ✅ Unified `binance_demo_order_ledger` table with `product` discriminator (CHECK constraint allows `'spot' | 'usdm_futures'`)
- ✅ `BinanceDemoOrderLedger` ORM model with `venue_host` evidence column
- ✅ `BinanceDemoLedgerService` with 9-state machine + 8 `record_*` methods + product validation
- ✅ `BinanceDemoLedgerRepository` (service-internal, AST-guarded)
- ✅ Demo ledger errors module (cross-product: `BinanceDemoInvalidStateTransition`, `BinanceDemoInvalidProduct`, `BinanceDemoDuplicateClientOrderId`)
- ✅ Static AST import guard for testnet (still active; PR 2 must also avoid testnet imports)
- ✅ Audit allowlist for the demo model file
- ✅ Sizing helper precedent: `app/services/brokers/binance/spot_demo/sizing.py` (PR 2 may reuse the same shape; consider moving to `app/services/brokers/binance/demo/sizing.py` if generic, or duplicate per-product)

**No alembic migration needed** for PR 2 — the ledger table is already shaped for futures rows.

---

## File Structure

### Created

- `app/services/brokers/binance/futures_demo/__init__.py` — package marker + re-exports
- `app/services/brokers/binance/futures_demo/errors.py` — `BinanceFuturesDemoDisabled`, `BinanceFuturesDemoMissingCredentials`, `BinanceFuturesDemoCrossAllowlistViolation`, `BinanceFuturesDemoHedgeModeBlocked`, `BinanceFuturesDemoLeverageMismatch`, `BinanceFuturesDemoReduceOnlyRequired`, `BinanceFuturesDemoUnsupportedSymbol`
- `app/services/brokers/binance/futures_demo/host_allowlist.py` — `FUTURES_DEMO_HOSTS = frozenset({"demo-fapi.binance.com"})`, `_DEPRECATED_FUTURES_TESTNET_HOSTS` deny-list (`testnet.binancefuture.com`), `assert_futures_demo_host`
- `app/services/brokers/binance/futures_demo/signing.py` — HMAC-SHA256 signer + `RECV_WINDOW_MS=5000` (same shape as spot_demo/signing.py)
- `app/services/brokers/binance/futures_demo/transport.py` — `build_futures_demo_client()` factory (httpx.AsyncClient pointed at demo-fapi with on-request event hook enforcing allowlist + cross-package deny)
- `app/services/brokers/binance/futures_demo/preflight.py` — signed GET `/fapi/v1/account` for credential validation
- `app/services/brokers/binance/futures_demo/dto.py` — `FuturesDemoOrderSubmitResult`, `FuturesDemoOrderTestResult`, `FuturesDemoCancelResult`, `FuturesDemoOpenOrdersResult`, `FuturesDemoPositionResult`, `FuturesDemoLeverageResult`, `FuturesDemoPositionModeResult`
- `app/services/brokers/binance/futures_demo/sizing.py` — futures-specific sizing (same floor-only semantics, plus symbol allowlist enforcement)
- `app/services/brokers/binance/futures_demo/execution_client.py` — `BinanceFuturesDemoExecutionClient` with:
  - `from_env()` classmethod
  - `preview_submit(...)` → `FuturesDemoDryRunResult`
  - `submit_order(*, symbol, side, order_type, qty, client_order_id, confirm, reduce_only=False, price=None, time_in_force=None)` — POST `/fapi/v1/order`
  - `order_test(...)` — POST `/fapi/v1/order/test`
  - `cancel_order(*, symbol, client_order_id)` — DELETE `/fapi/v1/order`
  - `get_open_orders(*, symbol)` — GET `/fapi/v1/openOrders`
  - `get_position(*, symbol)` — GET `/fapi/v1/positionRisk`
  - `get_position_mode()` — GET `/fapi/v1/positionSide/dual` (returns Hedge mode flag)
  - `set_leverage(*, symbol, leverage)` — POST `/fapi/v1/leverage`
  - `aclose()` — close httpx client
- `scripts/binance_futures_demo_smoke.py` — 5-mode CLI (default-disabled / --plan-only / --preflight / --order-test / --confirm) with open + close + reconciliation
- `tests/services/brokers/binance/futures_demo/__init__.py` — empty package marker
- `tests/services/brokers/binance/futures_demo/test_host_allowlist.py`
- `tests/services/brokers/binance/futures_demo/test_signing.py`
- `tests/services/brokers/binance/futures_demo/test_transport.py`
- `tests/services/brokers/binance/futures_demo/test_path_construction.py`
- `tests/services/brokers/binance/futures_demo/test_secret_redaction.py`
- `tests/services/brokers/binance/futures_demo/test_audit_no_live_host.py`
- `tests/services/brokers/binance/futures_demo/test_cross_environment_leakage.py` — Futures Demo never reaches Spot Demo / live / testnet hosts; Spot Demo client never reaches futures host
- `tests/services/brokers/binance/futures_demo/test_preflight.py`
- `tests/services/brokers/binance/futures_demo/test_sizing.py` — XRPUSDT happy path; BTCUSDT blocked at 10 USDT cap; symbol allowlist enforced
- `tests/services/brokers/binance/futures_demo/test_execution_client_fail_closed.py`
- `tests/services/brokers/binance/futures_demo/test_execution_client_submit_cancel.py`
- `tests/services/brokers/binance/futures_demo/test_execution_client_order_test.py`
- `tests/services/brokers/binance/futures_demo/test_execution_client_leverage.py` — verifies `set_leverage(leverage=1)` is called before open + echo verified; non-1x leverage in response → `BinanceFuturesDemoLeverageMismatch`
- `tests/services/brokers/binance/futures_demo/test_execution_client_position_mode.py` — Hedge mode response → `BinanceFuturesDemoHedgeModeBlocked` before any submit
- `tests/services/brokers/binance/futures_demo/test_execution_client_reduce_only.py` — open without reduceOnly OK; close with reduceOnly=true OK; close without reduceOnly → reject (defense in depth)
- `tests/services/brokers/binance/futures_demo/test_testnet_env_does_not_activate_demo.py` — `BINANCE_TESTNET_*` cannot enable Futures Demo
- `tests/services/brokers/binance/futures_demo/test_spot_demo_env_does_not_activate_futures.py` — `BINANCE_SPOT_DEMO_*` set without `BINANCE_FUTURES_DEMO_*` → futures fails closed
- `tests/services/brokers/binance/demo/test_ledger_futures_product.py` — `record_planned(product="usdm_futures", venue_host="demo-fapi.binance.com")` stores correctly + state transitions work identically to spot rows
- `tests/scripts/test_binance_futures_demo_smoke.py` — CLI mode tests
- `docs/runbooks/binance-futures-demo-smoke.md`
- `docs/superpowers/plans/2026-05-23-rob-298-pr2-futures-demo.md` — this file

### Modified

- `app/services/brokers/binance/futures_demo/__init__.py` — exports
- `env.example` — add `BINANCE_FUTURES_DEMO_*` block (4 vars) below the existing `BINANCE_SPOT_DEMO_*` block
- `docs/runbooks/binance-spot-demo-smoke.md` — light update referencing Futures Demo as now-available sibling
- `CLAUDE.md` — extend the "Binance Demo Order Ledger (ROB-298)" section to mention the Futures Demo adapter + its safety constraints (leverage, position mode, reduceOnly)
- `tests/services/brokers/binance/demo/test_no_testnet_imports.py` — already scans all of app/scripts/tests; no changes needed unless the AST guard needs to also scan futures_demo for cross-package leakage (probably yes — add a sibling test that the futures_demo package doesn't import spot_demo)
- `tests/services/brokers/binance/test_audit_no_signed_endpoints.py` — extend `ALLOWED_PACKAGE_PATHS` to include `app/services/brokers/binance/futures_demo`

### Not changed

- `app/models/binance_demo_order_ledger.py` (PR 1 already supports `product="usdm_futures"`)
- `app/services/brokers/binance/demo/ledger/` (service/repository unchanged — they're product-agnostic)
- Alembic — no new migration; the existing CHECK constraint covers both products

---

## Pre-flight

- [ ] **Step P1: Verify branch state**

```bash
cd /Users/mgh3326/work/auto_trader.rob-298
git status --short  # clean
git log --oneline origin/main..HEAD | head -3
```
Expected: clean, ~19 commits ahead of main (PR 1).

Decision: continue on the same `rob-298` branch (PR 1 not yet merged), or start a new branch off main after PR 1 lands? Both viable. If PR 1 has merged: rebase a new branch off main. If PR 1 still open: build on top of rob-298 (PR 2 will rebase before push).

- [ ] **Step P2: Verify foundation imports**

```bash
cd /Users/mgh3326/work/auto_trader.rob-298
uv run python -c "from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger; from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService; print('PR 1 foundation ok')"
```
Expected: `PR 1 foundation ok`

- [ ] **Step P3: Verify Spot Demo tests still pass**

```bash
cd /Users/mgh3326/work/auto_trader.rob-298
uv run pytest tests/services/brokers/binance/spot_demo/ tests/services/brokers/binance/demo/ -v --tb=short 2>&1 | tail -10
```
Expected: all pass (or pre-existing environmental Postgres errors only).

---

## Task 1: Futures Demo errors module

**Files:**
- Create: `app/services/brokers/binance/futures_demo/__init__.py`
- Create: `app/services/brokers/binance/futures_demo/errors.py`

- [ ] **Step 1: Create package `__init__.py`**

```python
"""ROB-298 PR 2 — Binance USD-M Futures Demo execution domain.

Sibling of ``spot_demo``. Independent env namespace
(``BINANCE_FUTURES_DEMO_*``), independent host allowlist
(``demo-fapi.binance.com`` only), independent transport. Shares only the
unified ``binance_demo_order_ledger`` table via ``BinanceDemoLedgerService``
(writes ``product='usdm_futures'`` rows).
"""
```

- [ ] **Step 2: Create errors module**

```python
"""ROB-298 PR 2 — Futures Demo adapter error vocabulary."""
from __future__ import annotations

from app.services.brokers.binance.errors import BinanceAdapterError


class BinanceFuturesDemoDisabled(BinanceAdapterError):
    """Raised when BINANCE_FUTURES_DEMO_ENABLED is not 'true'."""


class BinanceFuturesDemoMissingCredentials(BinanceAdapterError):
    """Raised when API key/secret env vars are empty."""


class BinanceFuturesDemoCrossAllowlistViolation(BinanceAdapterError):
    """Raised when a signed request would route to a non-Futures-Demo host."""


class BinanceFuturesDemoHedgeModeBlocked(BinanceAdapterError):
    """Raised when the Demo account is in Hedge mode.

    ROB-298 PR 2 only supports One-way mode. Hedge mode would require
    explicit positionSide on every order, which is out of scope.
    """


class BinanceFuturesDemoLeverageMismatch(BinanceAdapterError):
    """Raised when the post-set_leverage echo from Binance is not 1x.

    The smoke contract enforces 1x leverage exactly. Any other leverage
    indicates either a Binance-side bug or an env tampering attempt.
    """


class BinanceFuturesDemoReduceOnlyRequired(BinanceAdapterError):
    """Raised when a close-side order is submitted without reduceOnly=true.

    Defense in depth: a close without reduceOnly could flip the position
    (open opposite side). PR 2 close path always sets reduceOnly=true.
    """


class BinanceFuturesDemoUnsupportedSymbol(BinanceAdapterError):
    """Raised when a symbol is not in the configured allowlist.

    Default allowlist: XRPUSDT (primary), DOGEUSDT, SOLUSDT.
    BTCUSDT is explicitly excluded due to MIN_NOTIONAL=50 USDT > 10 USDT cap.
    Operator CLI override extends the list but the cap is never bypassed.
    """
```

- [ ] **Step 3: Verify imports**

```bash
cd /Users/mgh3326/work/auto_trader.rob-298
uv run python -c "from app.services.brokers.binance.futures_demo.errors import BinanceFuturesDemoDisabled, BinanceFuturesDemoMissingCredentials, BinanceFuturesDemoCrossAllowlistViolation, BinanceFuturesDemoHedgeModeBlocked, BinanceFuturesDemoLeverageMismatch, BinanceFuturesDemoReduceOnlyRequired, BinanceFuturesDemoUnsupportedSymbol; print('ok')"
```
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add app/services/brokers/binance/futures_demo/__init__.py app/services/brokers/binance/futures_demo/errors.py
git commit -m "feat(rob-298 pr2): Futures Demo error vocabulary"
```

---

## Task 2: Futures Demo host allowlist + cross-allowlist guard

**Files:**
- Create: `app/services/brokers/binance/futures_demo/host_allowlist.py`
- Test: `tests/services/brokers/binance/futures_demo/__init__.py` (empty)
- Test: `tests/services/brokers/binance/futures_demo/test_host_allowlist.py`

- [ ] **Step 1: Write the failing test**

```python
"""ROB-298 PR 2 — Futures Demo host allowlist + disjointness guard."""
from __future__ import annotations

import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.futures_demo.host_allowlist import (
    FUTURES_DEMO_HOSTS,
    _DEPRECATED_FUTURES_TESTNET_HOSTS,
    assert_futures_demo_host,
)
from app.services.brokers.binance.spot_demo.host_allowlist import (
    SPOT_DEMO_HOSTS,
    _DEPRECATED_TESTNET_HOSTS,
)
from app.services.brokers.binance.host_allowlist import PUBLIC_HOSTS


def test_futures_demo_hosts_only_demo_fapi() -> None:
    assert FUTURES_DEMO_HOSTS == frozenset({"demo-fapi.binance.com"})


def test_disjoint_from_spot_demo() -> None:
    assert FUTURES_DEMO_HOSTS.isdisjoint(SPOT_DEMO_HOSTS)


def test_disjoint_from_public() -> None:
    assert FUTURES_DEMO_HOSTS.isdisjoint(PUBLIC_HOSTS)


def test_disjoint_from_deprecated_testnet() -> None:
    assert FUTURES_DEMO_HOSTS.isdisjoint(_DEPRECATED_TESTNET_HOSTS)
    assert FUTURES_DEMO_HOSTS.isdisjoint(_DEPRECATED_FUTURES_TESTNET_HOSTS)


def test_assert_passes_for_demo_fapi() -> None:
    assert_futures_demo_host("demo-fapi.binance.com")  # no raise


@pytest.mark.parametrize(
    "host",
    [
        "fapi.binance.com",  # live futures
        "api.binance.com",  # live spot
        "demo-api.binance.com",  # spot demo
        "testnet.binance.vision",  # deprecated spot testnet
        "testnet.binancefuture.com",  # deprecated futures testnet
        "demo-fapi.binance.com.evil.example",  # spoofed subdomain
    ],
)
def test_assert_rejects_non_demo_fapi(host: str) -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        assert_futures_demo_host(host)
```

- [ ] **Step 2: Run failing test**

```bash
uv run pytest tests/services/brokers/binance/futures_demo/test_host_allowlist.py -v
```
Expected: FAIL (module not yet present).

- [ ] **Step 3: Implement host_allowlist.py**

```python
"""ROB-298 PR 2 — Futures Demo host allowlist (frozen, disjoint).

Two active demo host sets:

  * Spot Demo signed adapter      → ``SPOT_DEMO_HOSTS``
  * Futures Demo signed adapter   → ``FUTURES_DEMO_HOSTS`` (this file)

Plus the unsigned Public adapter (``PUBLIC_HOSTS``).

A host appearing in two sets would let a signed request leak across
environments. Pairwise disjointness is enforced by tests.

Historical (ROB-298):
  * Spot Testnet → deprecated, hosts in ``spot_demo._DEPRECATED_TESTNET_HOSTS``
  * Futures Testnet → never had an active adapter; hosts deny-listed below
    for defense in depth in case anyone ever sets BINANCE_FUTURES_DEMO_BASE_URL
    to testnet.binancefuture.com.
"""
from __future__ import annotations

from app.services.brokers.binance.errors import BinanceLiveHostBlocked


FUTURES_DEMO_HOSTS: frozenset[str] = frozenset(
    {
        "demo-fapi.binance.com",
    }
)


_DEPRECATED_FUTURES_TESTNET_HOSTS: frozenset[str] = frozenset(
    {
        "testnet.binancefuture.com",
    }
)


def assert_futures_demo_host(host: str) -> None:
    """Raise ``BinanceLiveHostBlocked`` if host is not in ``FUTURES_DEMO_HOSTS``.

    Strict equality match. Subdomain spoofs rejected.
    """
    if host not in FUTURES_DEMO_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Futures Demo signed request blocked: {host!r} not in {sorted(FUTURES_DEMO_HOSTS)}"
        )
```

- [ ] **Step 4: Run tests, verify pass**

```bash
uv run pytest tests/services/brokers/binance/futures_demo/test_host_allowlist.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/futures_demo/host_allowlist.py tests/services/brokers/binance/futures_demo/
git commit -m "feat(rob-298 pr2): Futures Demo host allowlist + disjointness tests"
```

---

## Task 3: Futures Demo signing + transport

**Files:**
- Create: `app/services/brokers/binance/futures_demo/signing.py`
- Create: `app/services/brokers/binance/futures_demo/transport.py`
- Test: `tests/services/brokers/binance/futures_demo/test_signing.py`
- Test: `tests/services/brokers/binance/futures_demo/test_transport.py`

Mirror `app/services/brokers/binance/spot_demo/signing.py` and `spot_demo/transport.py` exactly. Key differences:
- Use `FUTURES_DEMO_HOSTS` + `_DEPRECATED_FUTURES_TESTNET_HOSTS` in the host check
- Cross-package deny: a futures transport must also reject Spot Demo hosts (`SPOT_DEMO_HOSTS`) and live spot host (`api.binance.com`). Either reject any host not in `FUTURES_DEMO_HOSTS` (already covered by `assert_futures_demo_host`) OR explicitly check disjointness.

- [ ] **Step 1: Read spot_demo precedents**

```bash
cd /Users/mgh3326/work/auto_trader.rob-298
cat app/services/brokers/binance/spot_demo/signing.py
cat app/services/brokers/binance/spot_demo/transport.py
```

- [ ] **Step 2: Write signing tests + module**

`tests/services/brokers/binance/futures_demo/test_signing.py`:

```python
"""ROB-298 PR 2 — Futures Demo HMAC signing."""
from __future__ import annotations

from app.services.brokers.binance.futures_demo.signing import (
    RECV_WINDOW_MS,
    _sign_request_params,
)


def test_recv_window_matches_binance_default() -> None:
    assert RECV_WINDOW_MS == 5000


def test_signed_params_include_timestamp_and_signature() -> None:
    signed = _sign_request_params(
        params={"symbol": "XRPUSDT", "side": "BUY"},
        api_secret="test-secret",
        timestamp_ms=1700000000000,
    )
    assert "timestamp" in signed
    assert signed["timestamp"] == "1700000000000"
    assert "recvWindow" in signed
    assert signed["recvWindow"] == str(RECV_WINDOW_MS)
    assert "signature" in signed
    assert len(signed["signature"]) == 64  # sha256 hex
```

`app/services/brokers/binance/futures_demo/signing.py`:

Mirror spot_demo/signing.py verbatim except the module docstring (call out Futures Demo). The HMAC-SHA256 chokepoint is the only signer surface.

- [ ] **Step 3: Write transport tests + module**

`tests/services/brokers/binance/futures_demo/test_transport.py`:

```python
"""ROB-298 PR 2 — Futures Demo transport factory."""
from __future__ import annotations

import httpx
import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.futures_demo.transport import (
    build_futures_demo_client,
)


def test_factory_rejects_non_demo_fapi_base_url() -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        build_futures_demo_client(
            base_url="https://fapi.binance.com",
            api_key="key",
            api_secret="secret",
        )


def test_factory_rejects_spot_demo_base_url() -> None:
    """Cross-allowlist: futures transport must reject spot demo host."""
    with pytest.raises(BinanceLiveHostBlocked):
        build_futures_demo_client(
            base_url="https://demo-api.binance.com",
            api_key="key",
            api_secret="secret",
        )


def test_factory_accepts_demo_fapi() -> None:
    client = build_futures_demo_client(
        base_url="https://demo-fapi.binance.com",
        api_key="key",
        api_secret="secret",
    )
    assert isinstance(client, httpx.AsyncClient)


async def test_request_hook_rejects_runtime_redirect_to_live(
    httpx_mock,
) -> None:
    client = build_futures_demo_client(
        base_url="https://demo-fapi.binance.com",
        api_key="key",
        api_secret="secret",
    )
    # Simulate a code path that tries to GET a live URL
    with pytest.raises(BinanceLiveHostBlocked):
        await client.get("https://fapi.binance.com/fapi/v1/ping")
    await client.aclose()
```

`app/services/brokers/binance/futures_demo/transport.py`:

Mirror spot_demo/transport.py:
- Factory function `build_futures_demo_client(base_url, api_key, api_secret) -> httpx.AsyncClient`
- Pre-flight `assert_futures_demo_host(parsed_host)` at factory time
- `event_hooks={"request": [_on_request]}` where `_on_request` checks every outgoing request URL host
- Cross-package deny: explicitly check that host is in `FUTURES_DEMO_HOSTS`, raise if it appears in `_DEPRECATED_FUTURES_TESTNET_HOSTS`, `SPOT_DEMO_HOSTS`, or any other known set
- `X-MBX-APIKEY` header set globally
- `timeout=httpx.Timeout(...)` matching spot_demo
- `aclose()` discipline

- [ ] **Step 4: Run all tests**

```bash
uv run pytest tests/services/brokers/binance/futures_demo/test_signing.py tests/services/brokers/binance/futures_demo/test_transport.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/futures_demo/signing.py app/services/brokers/binance/futures_demo/transport.py tests/services/brokers/binance/futures_demo/test_signing.py tests/services/brokers/binance/futures_demo/test_transport.py
git commit -m "feat(rob-298 pr2): Futures Demo HMAC signing + transport with cross-allowlist guard"
```

---

## Task 4: Futures Demo preflight (signed account read)

**Files:**
- Create: `app/services/brokers/binance/futures_demo/preflight.py`
- Test: `tests/services/brokers/binance/futures_demo/test_preflight.py`

Mirror `spot_demo/preflight.py`. Differences:
- Path: `/fapi/v1/account` (not `/api/v3/account`)
- Uses `build_futures_demo_client` + `_sign_request_params`
- Returns a redacted account summary dataclass

- [ ] **Step 1: Tests + implementation**

(Follow spot_demo/preflight.py shape — read it first. Tests should cover: from_env disabled / missing creds / wrong base URL / signed-get-account success with mocked httpx response / secret redaction in log capture.)

- [ ] **Step 2: Commit**

```bash
git add app/services/brokers/binance/futures_demo/preflight.py tests/services/brokers/binance/futures_demo/test_preflight.py
git commit -m "feat(rob-298 pr2): Futures Demo preflight (signed GET /fapi/v1/account)"
```

---

## Task 5: Futures Demo DTOs

**Files:**
- Create: `app/services/brokers/binance/futures_demo/dto.py`

```python
"""ROB-298 PR 2 — DTOs for Futures Demo execution backend responses."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class FuturesDemoOrderSubmitResult:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    order_type: str
    qty: Decimal
    executed_qty: Decimal
    avg_price: Decimal
    status: str  # FILLED / PARTIALLY_FILLED / NEW / ...
    reduce_only: bool
    raw_response_redacted: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FuturesDemoOrderTestResult:
    """``/fapi/v1/order/test`` returned 200 with empty body."""

    symbol: str
    side: str
    order_type: str
    qty: Decimal


@dataclass(frozen=True)
class FuturesDemoCancelResult:
    client_order_id: str
    broker_order_id: str
    symbol: str
    status: str
    raw_response_redacted: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FuturesDemoOpenOrder:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    qty: Decimal
    status: str
    reduce_only: bool


@dataclass(frozen=True)
class FuturesDemoOpenOrdersResult:
    orders: list[FuturesDemoOpenOrder]


@dataclass(frozen=True)
class FuturesDemoPositionResult:
    """Single-symbol position snapshot from ``/fapi/v1/positionRisk``."""

    symbol: str
    position_amt: Decimal  # signed; positive=long, negative=short, 0=flat
    entry_price: Decimal
    leverage: int
    is_flat: bool


@dataclass(frozen=True)
class FuturesDemoLeverageResult:
    symbol: str
    leverage: int  # echoed by Binance after set_leverage
    max_notional_value: Decimal


@dataclass(frozen=True)
class FuturesDemoPositionModeResult:
    is_hedge_mode: bool  # True = dual-side, False = One-way (required for PR 2)
```

- [ ] **Step 1: Write file, verify imports, commit**

```bash
uv run python -c "from app.services.brokers.binance.futures_demo.dto import FuturesDemoOrderSubmitResult, FuturesDemoOrderTestResult, FuturesDemoCancelResult, FuturesDemoOpenOrdersResult, FuturesDemoPositionResult, FuturesDemoLeverageResult, FuturesDemoPositionModeResult; print('ok')"
git add app/services/brokers/binance/futures_demo/dto.py
git commit -m "feat(rob-298 pr2): Futures Demo execution DTOs"
```

---

## Task 6: Futures Demo sizing helper (with symbol allowlist)

**Files:**
- Create: `app/services/brokers/binance/futures_demo/sizing.py`
- Test: `tests/services/brokers/binance/futures_demo/test_sizing.py`

The futures sizing reuses the Spot Demo floor logic but adds:
- A configurable symbol allowlist enforced at the helper level: `XRPUSDT` (default), `DOGEUSDT`, `SOLUSDT` (fallback), `BTCUSDT` explicitly **excluded**
- `BinanceFuturesDemoUnsupportedSymbol` raised for any symbol not in the allowlist (unless operator passes `--allow-symbol` override at CLI level — but the helper itself doesn't take overrides)

Implementation sketch:

```python
"""ROB-298 PR 2 — Futures Demo order sizing.

Reuses the floor-only LOT_SIZE / MIN_NOTIONAL semantics from Spot Demo
plus an explicit symbol allowlist for futures (BTCUSDT excluded because
its MIN_NOTIONAL=50 USDT exceeds the 10 USDT cap).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoUnsupportedSymbol,
)


FUTURES_DEMO_DEFAULT_SYMBOL = "XRPUSDT"
FUTURES_DEMO_FALLBACK_SYMBOLS: frozenset[str] = frozenset(
    {"XRPUSDT", "DOGEUSDT", "SOLUSDT"}
)
FUTURES_DEMO_EXCLUDED_SYMBOLS: frozenset[str] = frozenset({"BTCUSDT"})


@dataclass(frozen=True)
class FuturesSizingResult:
    qty: Decimal
    notional_usdt: Decimal


@dataclass(frozen=True)
class FuturesSizingBlocked:
    reason: str


def assert_symbol_allowed(symbol: str, *, allowlist_override: frozenset[str] | None = None) -> None:
    """Raise ``BinanceFuturesDemoUnsupportedSymbol`` for excluded/non-allowlisted symbols."""
    if symbol in FUTURES_DEMO_EXCLUDED_SYMBOLS:
        raise BinanceFuturesDemoUnsupportedSymbol(
            f"{symbol} is explicitly excluded (MIN_NOTIONAL > 10 USDT cap)"
        )
    allowlist = allowlist_override if allowlist_override is not None else FUTURES_DEMO_FALLBACK_SYMBOLS
    if symbol not in allowlist:
        raise BinanceFuturesDemoUnsupportedSymbol(
            f"{symbol} not in allowlist {sorted(allowlist)}"
        )


def compute_futures_demo_order_qty(
    *,
    symbol: str,
    target_notional_usdt: Decimal,
    price: Decimal,
    min_notional: Decimal,
    step_size: Decimal,
    cap_usdt: Decimal,
    symbol_allowlist_override: frozenset[str] | None = None,
) -> FuturesSizingResult | FuturesSizingBlocked:
    assert_symbol_allowed(symbol, allowlist_override=symbol_allowlist_override)
    # ... rest mirrors Spot Demo compute_demo_order_qty
```

Tests:
- Happy path on `XRPUSDT` at 10 USDT
- `BTCUSDT` raises `BinanceFuturesDemoUnsupportedSymbol` before any math
- Symbol allowlist override (test that operator-level expansion works without bypassing cap)
- Floor + MIN_NOTIONAL behaviors same as Spot Demo

- [ ] **Step 1-4: TDD same as Spot Demo Task 7**

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/futures_demo/sizing.py tests/services/brokers/binance/futures_demo/test_sizing.py
git commit -m "feat(rob-298 pr2): Futures Demo sizing with symbol allowlist"
```

---

## Task 7: Futures Demo execution client

**Files:**
- Create: `app/services/brokers/binance/futures_demo/execution_client.py`
- Update: `app/services/brokers/binance/futures_demo/__init__.py` (exports)
- Tests:
  - `test_execution_client_fail_closed.py`
  - `test_execution_client_submit_cancel.py`
  - `test_execution_client_order_test.py`
  - `test_execution_client_leverage.py`
  - `test_execution_client_position_mode.py`
  - `test_execution_client_reduce_only.py`

This is the largest task. Mirror `spot_demo/execution_client.py` structure but add futures-specific surfaces:

### Surface

- `from_env()` — reads `BINANCE_FUTURES_DEMO_*` env vars only
- `preview_submit(...)` → `FuturesDemoDryRunResult` (no HTTP)
- `submit_order(*, symbol, side, order_type, qty, client_order_id, confirm, reduce_only=False, price=None, time_in_force=None)` → `FuturesDemoOrderSubmitResult | FuturesDemoDryRunResult`
  - POST `/fapi/v1/order`
  - `reduceOnly` param mirrors `reduce_only` arg
  - For LIMIT orders: requires `price` + `time_in_force`
- `order_test(...)` → `FuturesDemoOrderTestResult`
  - POST `/fapi/v1/order/test`
- `cancel_order(*, symbol, client_order_id)` → `FuturesDemoCancelResult`
  - DELETE `/fapi/v1/order`
- `get_open_orders(*, symbol)` → `FuturesDemoOpenOrdersResult`
  - GET `/fapi/v1/openOrders`
- `get_position(*, symbol)` → `FuturesDemoPositionResult`
  - GET `/fapi/v1/positionRisk?symbol=...`
  - Parses `positionAmt` (signed Decimal), `entryPrice`, `leverage`
  - `is_flat = (position_amt == 0)`
- `get_position_mode()` → `FuturesDemoPositionModeResult`
  - GET `/fapi/v1/positionSide/dual`
  - `is_hedge_mode = response["dualSidePosition"]` (True = Hedge mode → caller must refuse before submitting)
- `set_leverage(*, symbol, leverage)` → `FuturesDemoLeverageResult`
  - POST `/fapi/v1/leverage` with `symbol`, `leverage` params
  - Echo back from Binance must match the requested leverage; otherwise raise `BinanceFuturesDemoLeverageMismatch`
- `aclose()` — close httpx client

### Safety invariants (encoded in execution_client)

1. `from_env()` rejects non-Demo base URL via `assert_futures_demo_host`
2. `submit_order(..., confirm=False)` returns DryRun without HTTP
3. `submit_order(..., confirm=True, reduce_only=True)` — close-side; OK
4. `submit_order(..., confirm=True, reduce_only=False)` — open-side; OK on first call (no existing position check at client layer — that's CLI's job)
5. Secret hygiene: `__repr__` shows api_key fingerprint only; `_api_secret` never logged
6. Cross-allowlist: transport's on-request hook rejects any non-demo-fapi host

### Tests (separate files for focus)

**`test_execution_client_fail_closed.py`** (~5 tests):
- `BINANCE_FUTURES_DEMO_ENABLED=false` → `BinanceFuturesDemoDisabled`
- env unset → `BinanceFuturesDemoDisabled`
- enabled but missing key → `BinanceFuturesDemoMissingCredentials`
- enabled but missing secret → `BinanceFuturesDemoMissingCredentials`
- enabled with live `fapi.binance.com` base URL → `BinanceLiveHostBlocked` (or `BinanceFuturesDemoCrossAllowlistViolation`)
- secret never appears in `__repr__`

**`test_execution_client_submit_cancel.py`** (~5 tests):
- preview_submit returns DryRun, zero HTTP
- submit_order(confirm=False) → DryRun
- submit_order(confirm=True) → signed POST /fapi/v1/order with X-MBX-APIKEY + signature
- cancel_order → signed DELETE /fapi/v1/order
- secret not in caplog on submit failure

**`test_execution_client_order_test.py`** (~3 tests):
- order_test hits /fapi/v1/order/test, not /fapi/v1/order
- LIMIT params (price, timeInForce) included
- Signed header + signature param present

**`test_execution_client_leverage.py`** (~4 tests):
- set_leverage(symbol=XRPUSDT, leverage=1) hits POST /fapi/v1/leverage
- Response echo with `leverage=1` → returns `FuturesDemoLeverageResult(leverage=1)`
- Response echo with `leverage=5` (any non-1) → raises `BinanceFuturesDemoLeverageMismatch`
- The test simulates Binance returning a different leverage to confirm the guard fires

**`test_execution_client_position_mode.py`** (~3 tests):
- get_position_mode hits GET /fapi/v1/positionSide/dual
- Response `dualSidePosition=false` → returns `FuturesDemoPositionModeResult(is_hedge_mode=False)`
- Response `dualSidePosition=true` → returns `FuturesDemoPositionModeResult(is_hedge_mode=True)` (caller is responsible for refusing; the client doesn't refuse — CLI does)

**`test_execution_client_reduce_only.py`** (~2 tests):
- submit_order(reduce_only=True) sends `reduceOnly=true` in params
- submit_order(reduce_only=False) does NOT send reduceOnly (or sends `reduceOnly=false`)

- [ ] **Steps**: read spot_demo precedent, write tests (TDD), implement, run, commit

```bash
git add app/services/brokers/binance/futures_demo/execution_client.py \
        app/services/brokers/binance/futures_demo/__init__.py \
        tests/services/brokers/binance/futures_demo/test_execution_client_*.py
git commit -m "feat(rob-298 pr2): Futures Demo execution client (submit/test/cancel/leverage/position/mode)"
```

---

## Task 8: Cross-environment isolation tests

**Files:**
- Test: `tests/services/brokers/binance/futures_demo/test_testnet_env_does_not_activate_demo.py`
- Test: `tests/services/brokers/binance/futures_demo/test_spot_demo_env_does_not_activate_futures.py`
- Test: `tests/services/brokers/binance/futures_demo/test_cross_environment_leakage.py`

Mirror the cross-environment safety tests from Spot Demo PR 1.

**`test_testnet_env_does_not_activate_demo.py`**:
```python
def test_only_testnet_env_does_not_enable_futures(monkeypatch):
    # BINANCE_TESTNET_* set, BINANCE_FUTURES_DEMO_* unset → Disabled
    ...


def test_testnet_creds_do_not_substitute_for_futures_demo(monkeypatch):
    # FUTURES_DEMO_ENABLED=true, missing FUTURES_DEMO keys, TESTNET keys set → MissingCredentials
    ...
```

**`test_spot_demo_env_does_not_activate_futures.py`**:
```python
def test_spot_demo_env_does_not_enable_futures(monkeypatch):
    # BINANCE_SPOT_DEMO_* set + ENABLED=true, BINANCE_FUTURES_DEMO_* unset → Disabled
    ...


def test_spot_demo_creds_do_not_substitute_for_futures_demo(monkeypatch):
    # FUTURES_DEMO_ENABLED=true, missing FUTURES_DEMO keys, SPOT_DEMO keys set → MissingCredentials
    ...
```

**`test_cross_environment_leakage.py`**:
- Futures Demo transport rejects spot demo host
- Futures Demo transport rejects live spot host
- Futures Demo transport rejects deprecated testnet hosts (both spot and futures testnet)
- Spot Demo transport rejects futures demo host (regression test — should already pass from PR 1)

- [ ] **Steps**: TDD, implement, run, commit

```bash
git add tests/services/brokers/binance/futures_demo/test_testnet_env_does_not_activate_demo.py \
        tests/services/brokers/binance/futures_demo/test_spot_demo_env_does_not_activate_futures.py \
        tests/services/brokers/binance/futures_demo/test_cross_environment_leakage.py
git commit -m "test(rob-298 pr2): Futures Demo cross-environment isolation"
```

---

## Task 9: Demo ledger product extension test

**Files:**
- Test: `tests/services/brokers/binance/demo/test_ledger_futures_product.py`

Verify the unified ledger correctly stores `product="usdm_futures"` rows and that all state transitions work identically to spot rows.

```python
"""ROB-298 PR 2 — Demo ledger correctly handles usdm_futures product rows."""
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService

pytestmark = pytest.mark.asyncio


async def test_record_planned_usdm_futures(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_xrp_id: int,
) -> None:
    now = dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=dt.UTC)
    cid = "test-futures-XRPUSDT"
    await demo_ledger_service.record_planned(
        instrument_id=crypto_instrument_xrp_id,
        product="usdm_futures",
        venue_host="demo-fapi.binance.com",
        client_order_id=cid,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("3.0"),
        price=None,
        now=now,
    )
    row = await demo_ledger_service.get_by_client_order_id(cid)
    assert row.product == "usdm_futures"
    assert row.venue_host == "demo-fapi.binance.com"


async def test_futures_state_transitions_identical_to_spot(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_xrp_id: int,
) -> None:
    # planned → previewed → validated → submitted → filled → closed → reconciled
    # All 7 transitions should succeed on a usdm_futures row
    ...
```

(Add the `crypto_instrument_xrp_id` fixture — mirror the BTC fixture from PR 1's `test_ledger_service.py`.)

- [ ] **Step 1: Write tests, run (should pass without any service changes since PR 1's service is product-agnostic)**

- [ ] **Step 2: Commit**

```bash
git add tests/services/brokers/binance/demo/test_ledger_futures_product.py
git commit -m "test(rob-298 pr2): demo ledger usdm_futures product rows"
```

---

## Task 10: Futures Demo smoke CLI (5-mode)

**Files:**
- Create: `scripts/binance_futures_demo_smoke.py`
- Test: `tests/scripts/test_binance_futures_demo_smoke.py`

Mirror `scripts/binance_spot_demo_smoke.py` structure. Differences:

### Argparse

```python
parser.add_argument("--symbol", default="XRPUSDT")
parser.add_argument("--cap-usdt", type=Decimal, default=Decimal("10"))
parser.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
parser.add_argument("--leverage", type=int, default=1)
parser.add_argument("--allow-symbol", action="append", default=None,
                    help="Extend the symbol allowlist (e.g., --allow-symbol DOGEUSDT). Cap is never bypassed.")
```

### Mode behavior

1. **default-disabled**: `BINANCE_FUTURES_DEMO_ENABLED != "true"` → print disabled, exit 0
2. **--plan-only**: print JSON plan with symbol/qty/leverage/cap; no HTTP
3. **--preflight**: signed GET /fapi/v1/account; print redacted balance summary
4. **--order-test**: get exchange info → sizing → signed POST /fapi/v1/order/test; print redacted response
5. **--confirm**: full lifecycle below

### --confirm lifecycle

1. Symbol allowlist check (`assert_symbol_allowed` with operator override merged in)
2. Public GET /fapi/v1/exchangeInfo to fetch MIN_NOTIONAL + LOT_SIZE.stepSize
3. Compute qty via `compute_futures_demo_order_qty`. If blocked → exit 1 with redacted reason
4. **Position mode check**: `get_position_mode()`. If Hedge → raise `BinanceFuturesDemoHedgeModeBlocked` + record anomaly + exit 2
5. **Leverage set**: `set_leverage(symbol, leverage=1)`. Verify echo leverage == 1; mismatch → `BinanceFuturesDemoLeverageMismatch` + exit 2
6. Open DB session, find-or-create `CryptoInstrument` row for the symbol (venue=binance, product=usdm_futures, venue_symbol=symbol)
7. Generate `client_order_id` (`rob-298-fut-<uuid4hex>`)
8. **`ledger.record_planned(product="usdm_futures", venue_host="demo-fapi.binance.com", ...)`**
9. `ledger.record_previewed(...)`
10. `execution_client.order_test(...)` → success → `record_validated(...)`
11. `execution_client.submit_order(..., confirm=True, reduce_only=False)` → success → `record_submitted(broker_order_id, ...)`. If status == FILLED → `record_filled(...)`. (Open MARKET order on demo typically fills immediately.)
12. **Pre-close position check**: `get_position(symbol)`. Confirm position is non-zero (i.e., open succeeded). If flat → anomaly (open didn't take effect) + exit 2
13. **Close side**:
   - `--close-with SELL`: `submit_order(side=opposite, confirm=True, reduce_only=True, qty=abs(position_amt))` → expects FILLED
   - `--close-with CANCEL`: only valid for LIMIT orders that didn't fill (skip for MARKET)
14. `record_closed(...)` on the BUY row
15. **Reconciliation**: `get_open_orders(symbol)` MUST be empty AND `get_position(symbol).is_flat` MUST be True
   - If both clean → `record_reconciled(...)`, print evidence, exit 0
   - If either dirty → `record_anomaly(reason=...)` with details, exit 2

### Print output (machine-greppable)

```
[rob-298-fut] planned cid=<uuid> product=usdm_futures symbol=XRPUSDT side=BUY qty=3.0 venue=demo-fapi.binance.com
[rob-298-fut] previewed cid=<uuid>
[rob-298-fut] position_mode is_hedge=false
[rob-298-fut] leverage_set symbol=XRPUSDT leverage=1
[rob-298-fut] order_test_ok symbol=XRPUSDT
[rob-298-fut] validated cid=<uuid>
[rob-298-fut] submitted cid=<uuid> broker_order_id=<id> status=FILLED reduce_only=false
[rob-298-fut] filled cid=<uuid>
[rob-298-fut] position_check symbol=XRPUSDT amt=3.0
[rob-298-fut] submitted cid=<close-uuid> broker_order_id=<close-id> status=FILLED reduce_only=true
[rob-298-fut] closed cid=<uuid>
[rob-298-fut] open_orders_check empty=true
[rob-298-fut] position_check symbol=XRPUSDT amt=0 is_flat=true
[rob-298-fut] reconciled cid=<uuid>
```

### Tests

`tests/scripts/test_binance_futures_demo_smoke.py`:

- default-disabled exits 0 with disabled message
- --plan-only emits JSON without HTTP (httpx-bomb mock)
- --plan-only with BTCUSDT → exits 1 with `BinanceFuturesDemoUnsupportedSymbol` reason
- --plan-only with `--allow-symbol BTCUSDT` → still rejected (excluded list overrides override)
- Modes mutually exclusive (argparse-level)
- --confirm without credentials → refuses cleanly with exit 1
- (Optional, with full httpx mock) successful BUY+SELL lifecycle emits expected evidence lines

- [ ] **Steps**: read spot_demo precedent, implement, test, commit

```bash
git add scripts/binance_futures_demo_smoke.py tests/scripts/test_binance_futures_demo_smoke.py
git commit -m "feat(rob-298 pr2): Futures Demo smoke CLI — 5 modes with leverage/position/reduceOnly guards"
```

---

## Task 11: Static import guard extension

**File:** Update `tests/services/brokers/binance/demo/test_no_testnet_imports.py` to also ensure `futures_demo/` doesn't import `spot_demo/` directly (the only allowed cross-import is through shared `binance.errors` and the unified `binance.demo.ledger`).

- [ ] **Step 1: Extend the test**

Add a new test function:

```python
def test_futures_demo_does_not_import_spot_demo() -> None:
    """ROB-298 PR 2 — Futures Demo and Spot Demo are independent adapters.

    They share only the unified ledger (binance.demo.ledger) and base
    errors (binance.errors). Any direct import between the two adapter
    packages is forbidden.
    """
    futures_demo_root = pathlib.Path("app/services/brokers/binance/futures_demo")
    offenders: list[str] = []
    for py in futures_demo_root.rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "binance.spot_demo" in node.module:
                    offenders.append(f"{py}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "binance.spot_demo" in alias.name:
                        offenders.append(f"{py}: import {alias.name}")
    assert not offenders, (
        f"futures_demo must not import from spot_demo. Offenders: {offenders}"
    )
```

Add symmetric guard: `test_spot_demo_does_not_import_futures_demo`.

- [ ] **Step 2: Run + commit**

```bash
uv run pytest tests/services/brokers/binance/demo/test_no_testnet_imports.py -v
git add tests/services/brokers/binance/demo/test_no_testnet_imports.py
git commit -m "test(rob-298 pr2): static import guard — spot_demo and futures_demo isolated"
```

---

## Task 12: env.example + docs

**Files:**
- Modify: `env.example` (add `BINANCE_FUTURES_DEMO_*` block)
- Modify: `docs/runbooks/binance-spot-demo-smoke.md` (mention futures sibling)
- Modify: `CLAUDE.md` (extend Demo section)
- Create: `docs/runbooks/binance-futures-demo-smoke.md`

### env.example block (insert below existing Spot Demo block)

```
# -----------------------------------------------------------------------------
# Binance USD-M Futures Demo (canonical futures mock-trading, ROB-298 PR 2)
# -----------------------------------------------------------------------------
# Independent namespace from BINANCE_SPOT_DEMO_*. Same Demo credential may
# auth against both demo-api and demo-fapi, but each namespace must be set
# independently — they are not aliased.
#
# Live futures host (fapi.binance.com) and deprecated futures testnet
# (testnet.binancefuture.com) are fail-closed at the transport layer.

BINANCE_FUTURES_DEMO_ENABLED=false
BINANCE_FUTURES_DEMO_API_KEY=
BINANCE_FUTURES_DEMO_API_SECRET=
BINANCE_FUTURES_DEMO_BASE_URL=https://demo-fapi.binance.com
```

### Futures Demo runbook (`docs/runbooks/binance-futures-demo-smoke.md`)

Sections:
1. Header / overview — Binance USD-M Futures Demo is the canonical futures mock-trading backend (ROB-298 PR 2). Reference comment d258c471 for locked decisions.
2. Lane boundaries — `demo-fapi.binance.com` only; live + futures testnet fail-closed; sibling `Spot Demo` lane covered by separate runbook.
3. Env setup — `BINANCE_FUTURES_DEMO_*` (4 vars). `BINANCE_SPOT_DEMO_*` and `BINANCE_TESTNET_*` do not activate futures.
4. Pre-conditions and safety guarantees:
   - Default-disabled
   - Per-call operator gate (`--confirm`)
   - Host allowlist + cross-allowlist (deny live, spot demo, testnet)
   - 10 USDT cap
   - 1x leverage forced (mismatch → `BinanceFuturesDemoLeverageMismatch`)
   - One-way mode required (Hedge → `BinanceFuturesDemoHedgeModeBlocked`)
   - reduceOnly: never on open, required on close
   - Symbol allowlist: XRPUSDT default, DOGEUSDT/SOLUSDT fallback, BTCUSDT excluded
   - Post-close reconciliation: open orders empty AND position flat
5. 5 CLI modes with example invocations
6. Confirmed smoke runbook step-by-step:
   - Set env vars
   - Set position mode to One-way via Binance Demo console if not already
   - Run --plan-only (verify intent)
   - Run --preflight (verify creds)
   - Run --order-test (verify exchange filters)
   - Run --confirm --symbol XRPUSDT --cap-usdt 10 --side BUY --leverage 1
   - Verify ledger in DB (sample SQL filtered on product='usdm_futures')
   - Verify position flat + open orders empty post-run
7. Expected redacted evidence shape (full lifecycle output above)
8. Rollback / manual reconciliation:
   - Check `binance_demo_order_ledger` WHERE product='usdm_futures'
   - Use `BinanceFuturesDemoExecutionClient.cancel_order(...)` for stale orders
   - Use `get_position(...)` to confirm flat
   - Manual close-position order with reduce_only=true if position dirty
   - Record cancelled/anomaly via `BinanceDemoLedgerService`
9. Linked decisions — comment d258c471
10. Out-of-scope — multi-symbol scalping, scheduler activation, live hosts

### CLAUDE.md extension

Append to the existing "Binance Demo Order Ledger (ROB-298)" section:

```markdown
**USD-M Futures Demo (ROB-298 PR 2)**:
- **실행 어댑터**: `app/services/brokers/binance/futures_demo/execution_client.BinanceFuturesDemoExecutionClient` — `demo-fapi.binance.com` only; mutation은 `submit_order(..., confirm=True)`만; close 주문에는 `reduce_only=True` 필수
- **호스트 분리**: `FUTURES_DEMO_HOSTS = {demo-fapi.binance.com}`, Spot Demo (`demo-api.binance.com`)와 disjoint; live/testnet futures (`fapi.binance.com`, `testnet.binancefuture.com`) 차단
- **env namespace**: `BINANCE_FUTURES_DEMO_*` 전용 (Spot Demo와 비공유)
- **Leverage**: `1x` 강제 (`set_leverage` echo로 검증; mismatch → `BinanceFuturesDemoLeverageMismatch`)
- **Position mode**: One-way only (Hedge → `BinanceFuturesDemoHedgeModeBlocked`)
- **Symbol allowlist**: `XRPUSDT` (default), `DOGEUSDT`, `SOLUSDT` (fallback). `BTCUSDT` 제외 (MIN_NOTIONAL=50 > cap=10)
- **Reconcile gate**: 클로즈 후 open orders empty AND position flat 둘 다 만족해야 `reconciled`. 둘 중 하나라도 dirty면 `anomaly` 기록
- **CLI**: `scripts/binance_futures_demo_smoke.py` (default-disabled, 5 modes)
- **런북**: `docs/runbooks/binance-futures-demo-smoke.md`
```

- [ ] **Steps**: edit files, verify markdown renders, commit

```bash
git add env.example docs/runbooks/binance-futures-demo-smoke.md docs/runbooks/binance-spot-demo-smoke.md CLAUDE.md
git commit -m "docs(rob-298 pr2): env.example + Futures Demo runbook + CLAUDE.md"
```

---

## Task 13: Audit allowlist + full test sweep

**Files:**
- Modify: `tests/services/brokers/binance/test_audit_no_signed_endpoints.py` (allowlist `app/services/brokers/binance/futures_demo/`)

- [ ] **Step 1: Extend audit allowlist**

Read the test's `ALLOWED_PACKAGE_PATHS` and add the futures_demo path.

- [ ] **Step 2: Full sweep**

```bash
cd /Users/mgh3326/work/auto_trader.rob-298
uv run pytest tests/ -v --tb=short 2>&1 | tail -30
uv run ruff check app/ scripts/ tests/
uv run ruff format --check app/ tests/
uv run ty check app/ --error-on-warning
```

Expected: 0 ROB-298 PR 2 failures. Pre-existing environmental failures (Postgres unavailable, etc.) ignored.

If ruff format reports drift on new files, run `uv run ruff format app/ tests/` and commit.

- [ ] **Step 3: Commit**

```bash
git add tests/services/brokers/binance/test_audit_no_signed_endpoints.py
git commit -m "chore(rob-298 pr2): audit allowlist + full sweep"
```

---

## Task 14: Confirmed Demo smoke (operator-gated)

This requires real `BINANCE_FUTURES_DEMO_API_KEY` / `_API_SECRET` in env. If not available, mark BLOCKED in PR handoff.

- [ ] **Step 1: Plan-only sanity**

```bash
BINANCE_FUTURES_DEMO_ENABLED=true uv run python scripts/binance_futures_demo_smoke.py --plan-only --symbol XRPUSDT
```

- [ ] **Step 2: Preflight**

```bash
BINANCE_FUTURES_DEMO_ENABLED=true uv run python scripts/binance_futures_demo_smoke.py --preflight
```

Capture output to `/tmp/rob-298-pr2-preflight-evidence.txt`.

- [ ] **Step 3: Order-test**

```bash
BINANCE_FUTURES_DEMO_ENABLED=true uv run python scripts/binance_futures_demo_smoke.py --order-test --symbol XRPUSDT --cap-usdt 10
```

Capture to `/tmp/rob-298-pr2-ordertest-evidence.txt`.

- [ ] **Step 4: Confirmed BUY + SELL**

```bash
BINANCE_FUTURES_DEMO_ENABLED=true uv run python scripts/binance_futures_demo_smoke.py \
    --confirm --symbol XRPUSDT --cap-usdt 10 --side BUY --leverage 1
```

Expected: full lifecycle output; exit 0; ledger row with `product='usdm_futures'`, `lifecycle_state='reconciled'`; open orders empty; position flat.

Capture to `/tmp/rob-298-pr2-confirmed-evidence.txt`.

- [ ] **Step 5: Verify in DB**

```sql
SELECT client_order_id, product, lifecycle_state, side, qty, broker_order_id, notional_usdt
FROM binance_demo_order_ledger
WHERE product = 'usdm_futures'
ORDER BY created_at DESC LIMIT 4;
```

Expected: 2 rows (BUY + SELL), both `reconciled`.

- [ ] **Step 6: Verify post-run cleanliness**

```bash
BINANCE_FUTURES_DEMO_ENABLED=true uv run python -c "
import asyncio
from app.services.brokers.binance.futures_demo.execution_client import BinanceFuturesDemoExecutionClient
async def main():
    c = BinanceFuturesDemoExecutionClient.from_env()
    open_orders = await c.get_open_orders(symbol='XRPUSDT')
    position = await c.get_position(symbol='XRPUSDT')
    print('open orders:', open_orders.orders)
    print('position is_flat:', position.is_flat, 'amt:', position.position_amt)
    await c.aclose()
asyncio.run(main())
"
```

Expected: `open orders: []` and `is_flat: True, amt: 0`.

If any stale state remains, run cancel + close manually (see runbook §8 Rollback).

---

## Task 15: Push branch, create PR, update Linear

- [ ] **Step 1: Final status**

```bash
cd /Users/mgh3326/work/auto_trader.rob-298
git status
git log --oneline origin/rob-298..HEAD | head -20
```

- [ ] **Step 2: Push**

If PR 1 still open and PR 2 is on the same branch: push as additional commits to `rob-298`. If PR 1 already merged to main: rebase onto main first, push as `rob-298-pr2` or similar.

```bash
git push origin <branch>
```

- [ ] **Step 3: Create PR**

PR title: `feat(rob-298): Binance USD-M Futures Demo backend (PR 2 of 2)`

PR body template:

```markdown
## Summary

PR 2 of ROB-298: adds Binance USD-M Futures Demo as the canonical futures mock-trading backend with mutation-capable order execution, position/leverage/reduceOnly safety guards, and full 5-mode smoke CLI. Reuses the unified `binance_demo_order_ledger` table from PR 1 by writing `product="usdm_futures"` rows. Closes the futures gap left open by PR 1.

Locked design decisions: ROB-298 comment `d258c471-3202-444b-901b-c127f3ee44af`.

## Scope

- ✅ New `app/services/brokers/binance/futures_demo/` package (errors, host_allowlist, signing, transport, preflight, dto, sizing, execution_client) — independent of `spot_demo/`
- ✅ `BinanceFuturesDemoExecutionClient` with submit/order_test/cancel/get_open_orders/get_position/get_position_mode/set_leverage
- ✅ Symbol allowlist: XRPUSDT (default), DOGEUSDT, SOLUSDT. BTCUSDT excluded (MIN_NOTIONAL > 10 USDT cap)
- ✅ 1x leverage forced (verified via set_leverage echo)
- ✅ One-way mode required (Hedge mode → fail-closed)
- ✅ `reduceOnly` required on close, never on open
- ✅ Post-close reconciliation: open orders empty AND position flat
- ✅ Cross-allowlist guard: futures transport rejects spot demo, live, deprecated testnet hosts
- ✅ Cross-import guard: futures_demo never imports spot_demo (AST-checked)
- ✅ Demo ledger reuse: writes `product="usdm_futures"` rows (no new migration)
- ✅ 5-mode smoke CLI (`scripts/binance_futures_demo_smoke.py`)
- ✅ env.example + runbook + CLAUDE.md updates

## Out of scope

- Multi-symbol scalping logic (single-symbol BUY → CLOSE per invocation)
- TaskIQ / Prefect / scheduler activation — ROB-292
- Hermes/Discord integration
- Live `fapi.binance.com` — structurally fail-closed

## Tests run

- `uv run pytest tests/services/brokers/binance/futures_demo/ tests/services/brokers/binance/demo/test_ledger_futures_product.py tests/scripts/test_binance_futures_demo_smoke.py -v` → all PASS
- Full test sweep → 0 ROB-298 PR 2 failures
- `uv run ruff check + ruff format --check + ty check` → clean
- Static AST guards: futures_demo and spot_demo are isolated; neither imports testnet

## Confirmed Demo smoke evidence

[Fill from /tmp/rob-298-pr2-*-evidence.txt or mark BLOCKED]

- `--plan-only`: ✅
- `--preflight`: ✅
- `--order-test`: ✅
- `--confirm` (BUY + SELL, XRPUSDT, 10 USDT, 1x): ✅ lifecycle reconciled clean

## Safety boundary verification

- ✅ `BINANCE_FUTURES_DEMO_ENABLED=false` → `BinanceFuturesDemoDisabled`
- ✅ Base URL `fapi.binance.com` → `BinanceLiveHostBlocked` at construction
- ✅ Base URL `demo-api.binance.com` → `BinanceLiveHostBlocked` (cross-allowlist)
- ✅ Hedge mode response → `BinanceFuturesDemoHedgeModeBlocked` before any submit
- ✅ Non-1x leverage echo → `BinanceFuturesDemoLeverageMismatch`
- ✅ `BTCUSDT` → `BinanceFuturesDemoUnsupportedSymbol` (even with `--allow-symbol BTCUSDT` — excluded list wins)
- ✅ Close order without `reduce_only=true` → smoke records anomaly + exit 2
- ✅ Post-close non-flat position → smoke records anomaly + exit 2

## Test plan

- [ ] `uv run alembic upgrade head` (no new migration; PR 1's covers both products)
- [ ] Operator confirms position mode is One-way on Demo Futures account
- [ ] Operator runs `--preflight` against Demo creds
- [ ] Operator runs `--order-test` and confirms no order on Demo account
- [ ] Operator runs `--confirm` smoke and confirms reconciled
- [ ] Close ROB-291 as `Canceled / Superseded by ROB-298`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

- [ ] **Step 4: Update Linear**

Post a comment on ROB-298 with the PR 2 URL + reconciliation evidence summary. Reference PR 1's comment and the locked decisions comment for continuity.

- [ ] **Step 5: Close ROB-291**

Once PR 2 lands and reconciled smoke is verified, comment on ROB-291: `Canceled / Superseded by ROB-298`. Use the Linear MCP to change status to Cancelled.

---

## Self-Review Checklist

After completing all tasks, the reviewer verifies:

### Acceptance criteria from ROB-298 (futures-specific items)

- [x] Futures Demo mutation-capable backend with `demo-fapi.binance.com` allowlist — Task 7
- [x] Actual Demo buy/sell smoke with small caps + explicit flags — Tasks 10, 14
- [x] Futures Demo order-test path — Task 7, 10
- [x] Confirmed Demo smoke ends without stale state — Task 10, 14
- [x] Tests prove live/mainnet Binance hosts fail-closed — Tasks 2, 3, 7, 8
- [x] Docs describe Demo as canonical — Task 12

### Locked decisions from comment d258c471

- [x] Env namespace: `BINANCE_FUTURES_DEMO_*` only — Task 7
- [x] Default symbol XRPUSDT; BTCUSDT excluded — Task 6
- [x] Fallback allowlist DOGEUSDT, SOLUSDT — Task 6
- [x] 10 USDT cap — Task 6
- [x] 1x leverage forced — Task 7 (execution_client.set_leverage + leverage_mismatch test)
- [x] One-way mode required — Task 7 (get_position_mode + hedge_mode_blocked test)
- [x] reduceOnly: open=False, close=True — Task 7, 10
- [x] Post-close reconcile: open orders empty + position flat — Task 10

### Safety isolation

- [x] futures_demo never imports spot_demo (AST guard) — Task 11
- [x] spot_demo never imports futures_demo (AST guard) — Task 11
- [x] Cross-allowlist disjointness tested — Task 2
- [x] Cross-environment env vars do not activate futures — Task 8

### Discipline

- [x] No alembic migration (PR 1 covers product enum) — confirmed in Foundation section
- [x] No scope creep into multi-symbol scalping, scheduler, live hosts
- [x] No placeholders ("TBD", "implement later", etc.) in plan
- [x] All locked decisions traceable to comment d258c471
