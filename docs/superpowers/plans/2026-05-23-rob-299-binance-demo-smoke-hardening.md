# ROB-299 — Binance Demo Smoke Hardening + Futures Env Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Binance Spot Demo close path fee-aware (sell free balance, not the original buy qty), classify sub-min-notional residue as dust instead of anomaly, add a no-secret Futures Demo env readiness check, and emit a structured smoke report event — all demo-only, no-secret, no-scheduler.

**Architecture:** Three small, independently-testable units. (1) A narrow signed read-side method `get_asset_balance` on the Spot Demo execution client + pure sizing/classification helpers in `sizing.py` drive a fee-aware close. (2) A pure `evaluate_futures_demo_env_readiness()` reflector + a `--readiness` smoke mode report Futures env state without secrets or HTTP. (3) A pure report builder threads structured fields into a single `spot_demo_smoke_report` evidence event. No new state-machine states: dust is a clean `reconciled` row with a metadata note; anomaly keeps its existing branch plus a remediation hint.

**Tech Stack:** Python 3.13, `decimal.Decimal`, `pytest` + `pytest-asyncio` + `pytest-httpx` (`httpx_mock`), `monkeypatch` for env. Existing Binance Demo backend (ROB-298).

---

## File Structure

**Modify:**
- `app/services/brokers/binance/spot_demo/dto.py` — add `SpotDemoAssetBalance` DTO.
- `app/services/brokers/binance/spot_demo/execution_client.py` — add `get_asset_balance` signed read-side method (+ `_ACCOUNT_PATH`).
- `app/services/brokers/binance/spot_demo/sizing.py` — add `compute_close_qty` + `classify_close_residual` pure helpers and their result types.
- `scripts/binance_spot_demo_smoke.py` — rewire `_close_with_sell` to be fee-aware, route dust to `reconciled`-with-note, add remediation hint to anomalies, emit `spot_demo_smoke_report`.
- `scripts/binance_futures_demo_smoke.py` — add `--readiness` mode.
- `docs/runbooks/binance-spot-demo-smoke.md` — fee-aware close + dust semantics + report event.
- `docs/runbooks/binance-futures-demo-smoke.md` — `--readiness` mode + env names (placeholders only).

**Create:**
- `app/services/brokers/binance/futures_demo/readiness.py` — pure `FuturesDemoEnvReadiness` DTO + `evaluate_futures_demo_env_readiness()`.
- `tests/services/brokers/binance/spot_demo/test_close_sizing.py`
- `tests/services/brokers/binance/spot_demo/test_get_asset_balance.py`
- `tests/services/brokers/binance/futures_demo/test_env_readiness.py`
- `tests/scripts/test_spot_smoke_report.py` (pure report builder)

**No change needed:** `env.example` already documents `BINANCE_FUTURES_DEMO_{ENABLED,API_KEY,API_SECRET,BASE_URL}` (lines 387-390) with the independence note.

---

## Task 1: `SpotDemoAssetBalance` DTO

**Files:**
- Modify: `app/services/brokers/binance/spot_demo/dto.py`

- [ ] **Step 1: Add the DTO** (append after `SpotDemoOpenOrdersResult`)

```python
@dataclass(frozen=True)
class SpotDemoAssetBalance:
    """Free/locked amounts for a SINGLE asset.

    Deliberately narrow: ``get_asset_balance`` returns only the one asset
    the caller asked about so the full account payload (every balance row +
    account-level flags) never enters logs or evidence. ``free`` is the
    amount sellable right now (post-commission); ``locked`` is reserved by
    open orders.
    """

    asset: str
    free: Decimal
    locked: Decimal
```

- [ ] **Step 2: Commit**

```bash
git add app/services/brokers/binance/spot_demo/dto.py
git commit -m "feat(rob-299): add SpotDemoAssetBalance DTO"
```

---

## Task 2: `get_asset_balance` signed read-side method

**Files:**
- Modify: `app/services/brokers/binance/spot_demo/execution_client.py`
- Test: `tests/services/brokers/binance/spot_demo/test_get_asset_balance.py`

- [ ] **Step 1: Write the failing tests**

```python
"""ROB-299 — get_asset_balance narrow signed read-side method."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.spot_demo.dto import SpotDemoAssetBalance
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)

_BASE = "https://demo-api.binance.com"

_ACCOUNT_JSON = {
    "canTrade": True,
    "accountType": "SPOT",
    "balances": [
        {"asset": "XRP", "free": "12.34000000", "locked": "0.00000000"},
        {"asset": "USDT", "free": "500.00000000", "locked": "0.00000000"},
    ],
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> BinanceSpotDemoExecutionClient:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "DUMMY_SECRET")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", _BASE)
    return BinanceSpotDemoExecutionClient.from_env()


@pytest.mark.asyncio
async def test_get_asset_balance_returns_only_requested_asset(client, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/account\?.*$"),
        json=_ACCOUNT_JSON,
    )
    bal = await client.get_asset_balance(asset="XRP")
    assert isinstance(bal, SpotDemoAssetBalance)
    assert bal.asset == "XRP"
    assert bal.free == Decimal("12.34000000")
    assert bal.locked == Decimal("0")


@pytest.mark.asyncio
async def test_get_asset_balance_absent_asset_returns_zero(client, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/account\?.*$"),
        json=_ACCOUNT_JSON,
    )
    bal = await client.get_asset_balance(asset="DOGE")
    assert bal.asset == "DOGE"
    assert bal.free == Decimal("0")
    assert bal.locked == Decimal("0")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/brokers/binance/spot_demo/test_get_asset_balance.py -v`
Expected: FAIL — `AttributeError: 'BinanceSpotDemoExecutionClient' object has no attribute 'get_asset_balance'`

- [ ] **Step 3: Implement the method**

Add module constant near the other `_*_PATH` finals:

```python
_ACCOUNT_PATH: Final[str] = "/api/v3/account"
```

Add `SpotDemoAssetBalance` to the dto import block, then add the method after `get_order_status`:

```python
    async def get_asset_balance(self, *, asset: str) -> SpotDemoAssetBalance:
        """Signed ``GET /api/v3/account``; return only ``asset``'s free/locked.

        Narrow by design: every other balance row and all account-level
        flags are dropped here so the full account payload never reaches a
        caller, log line, or evidence file. If the account holds none of
        ``asset``, returns zero free/locked (absence == zero, not an error).
        Read-side only — no mutation, no operator gate.
        """
        params = {"recvWindow": str(BINANCE_SPOT_DEMO_RECV_WINDOW_MS)}
        signed = _sign_request_params(params=params, api_secret=self._api_secret)
        resp = await self._client.get(_ACCOUNT_PATH, params=signed)
        resp.raise_for_status()
        body = resp.json()
        for entry in body.get("balances") or []:
            if entry.get("asset") == asset:
                return SpotDemoAssetBalance(
                    asset=asset,
                    free=Decimal(str(entry.get("free", "0"))),
                    locked=Decimal(str(entry.get("locked", "0"))),
                )
        return SpotDemoAssetBalance(asset=asset, free=Decimal("0"), locked=Decimal("0"))
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/brokers/binance/spot_demo/test_get_asset_balance.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/spot_demo/execution_client.py tests/services/brokers/binance/spot_demo/test_get_asset_balance.py
git commit -m "feat(rob-299): narrow get_asset_balance read-side method on Spot Demo client"
```

---

## Task 3: Fee-aware close-qty + dust classification helpers (pure)

**Files:**
- Modify: `app/services/brokers/binance/spot_demo/sizing.py`
- Test: `tests/services/brokers/binance/spot_demo/test_close_sizing.py`

- [ ] **Step 1: Write the failing tests**

```python
"""ROB-299 — fee-aware close qty + residual dust classification."""

from __future__ import annotations

from decimal import Decimal

from app.services.brokers.binance.spot_demo.sizing import (
    CloseQtyDust,
    CloseQtyResult,
    classify_close_residual,
    compute_close_qty,
)

_STEP = Decimal("0.1")
_MIN_NOTIONAL = Decimal("5")
_PRICE = Decimal("2.0")  # 1 unit = 2 USDT


def test_compute_close_qty_uses_free_balance_not_buy_qty():
    # Bought 6 units, but only 5.93 free after commission.
    res = compute_close_qty(
        free_balance=Decimal("5.93"),
        price=_PRICE,
        min_notional=_MIN_NOTIONAL,
        step_size=_STEP,
    )
    assert isinstance(res, CloseQtyResult)
    assert res.qty == Decimal("5.9")  # floored to step, NOT 6
    assert res.notional_usdt == Decimal("11.8")


def test_compute_close_qty_fee_reduced_free_balance_still_sellable():
    res = compute_close_qty(
        free_balance=Decimal("3.001"),
        price=_PRICE,
        min_notional=_MIN_NOTIONAL,
        step_size=_STEP,
    )
    assert isinstance(res, CloseQtyResult)
    assert res.qty == Decimal("3.0")
    assert res.notional_usdt == Decimal("6.0")


def test_compute_close_qty_residual_below_min_notional_is_dust():
    # 2.0 units * 2.0 = 4.0 USDT < min_notional 5.
    res = compute_close_qty(
        free_balance=Decimal("2.0"),
        price=_PRICE,
        min_notional=_MIN_NOTIONAL,
        step_size=_STEP,
    )
    assert isinstance(res, CloseQtyDust)
    assert res.free == Decimal("2.0")
    assert res.notional_usdt == Decimal("4.0")


def test_compute_close_qty_sub_step_free_is_dust():
    res = compute_close_qty(
        free_balance=Decimal("0.05"),
        price=_PRICE,
        min_notional=_MIN_NOTIONAL,
        step_size=_STEP,
    )
    assert isinstance(res, CloseQtyDust)
    assert res.free == Decimal("0.05")


def test_classify_residual_below_min_notional_is_dust():
    outcome = classify_close_residual(
        free_after=Decimal("0.5"),
        price=_PRICE,
        min_notional=_MIN_NOTIONAL,
        step_size=_STEP,
        open_orders_empty=True,
    )
    assert outcome.kind == "dust"
    assert outcome.remediation_hint is None


def test_classify_residual_sellable_remainder_with_clean_book_is_anomaly():
    # 3.0 * 2.0 = 6.0 >= min_notional: a sellable chunk was left behind.
    outcome = classify_close_residual(
        free_after=Decimal("3.0"),
        price=_PRICE,
        min_notional=_MIN_NOTIONAL,
        step_size=_STEP,
        open_orders_empty=True,
    )
    assert outcome.kind == "anomaly"
    assert outcome.remediation_hint  # operator-readable, non-empty


def test_classify_residual_dirty_book_is_anomaly():
    outcome = classify_close_residual(
        free_after=Decimal("0.5"),
        price=_PRICE,
        min_notional=_MIN_NOTIONAL,
        step_size=_STEP,
        open_orders_empty=False,
    )
    assert outcome.kind == "anomaly"
    assert outcome.remediation_hint
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/brokers/binance/spot_demo/test_close_sizing.py -v`
Expected: FAIL — `ImportError: cannot import name 'compute_close_qty'`

- [ ] **Step 3: Implement the helpers** (append to `sizing.py`)

```python
@dataclass(frozen=True)
class CloseQtyResult:
    """A sellable close quantity: step-floored, notional >= min_notional."""

    qty: Decimal
    notional_usdt: Decimal


@dataclass(frozen=True)
class CloseQtyDust:
    """Free balance is non-zero but too small to place a min-notional SELL."""

    free: Decimal
    notional_usdt: Decimal
    reason: str


@dataclass(frozen=True)
class CloseResidualOutcome:
    kind: str  # "dust" | "anomaly"
    remediation_hint: str | None = None


def compute_close_qty(
    *,
    free_balance: Decimal,
    price: Decimal,
    min_notional: Decimal,
    step_size: Decimal,
) -> CloseQtyResult | CloseQtyDust:
    """Largest step-floored qty of the FREE balance whose notional clears
    ``min_notional``. Never reuses the original BUY qty; never rounds up."""
    if price <= 0:
        raise ValueError("price must be > 0")
    if step_size <= 0:
        raise ValueError("step_size must be > 0")
    floored = (free_balance / step_size).quantize(
        Decimal("1"), rounding=ROUND_DOWN
    ) * step_size
    notional = floored * price
    if floored <= 0:
        return CloseQtyDust(
            free=free_balance,
            notional_usdt=free_balance * price,
            reason=f"free={free_balance} below step_size={step_size}",
        )
    if notional < min_notional:
        return CloseQtyDust(
            free=free_balance,
            notional_usdt=notional,
            reason=f"closeable notional={notional} < MIN_NOTIONAL={min_notional}",
        )
    return CloseQtyResult(qty=floored, notional_usdt=notional)


def classify_close_residual(
    *,
    free_after: Decimal,
    price: Decimal,
    min_notional: Decimal,
    step_size: Decimal,
    open_orders_empty: bool,
) -> CloseResidualOutcome:
    """Decide whether what is left after a close is benign dust or an anomaly.

    Dust (benign, -> ``reconciled`` with note) requires BOTH: the order book
    is clean AND no sellable (>= min_notional) chunk remains. Anything else
    is an anomaly carrying an operator-readable remediation hint."""
    leftover = compute_close_qty(
        free_balance=free_after,
        price=price,
        min_notional=min_notional,
        step_size=step_size,
    )
    if open_orders_empty and isinstance(leftover, CloseQtyDust):
        return CloseResidualOutcome(kind="dust")
    if not open_orders_empty:
        hint = (
            "Open orders remain after close. Cancel residual open orders, "
            "then re-run --confirm or remediate manually."
        )
    else:
        hint = (
            f"Sellable residual ~{leftover.notional_usdt} USDT (>= MIN_NOTIONAL "
            f"{min_notional}) left after close. Place a fee-adjusted MARKET SELL "
            "of the free base asset to flatten, then re-reconcile."
        )
    return CloseResidualOutcome(kind="anomaly", remediation_hint=hint)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/brokers/binance/spot_demo/test_close_sizing.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/spot_demo/sizing.py tests/services/brokers/binance/spot_demo/test_close_sizing.py
git commit -m "feat(rob-299): fee-aware close qty + residual dust classification helpers"
```

---

## Task 4: Wire fee-aware close + dust into the Spot smoke flow

**Files:**
- Modify: `scripts/binance_spot_demo_smoke.py`

No new unit test here (logic is covered by Task 3's pure helpers + Task 2's method); the existing `tests/scripts/test_binance_spot_demo_smoke.py` must still pass, and Task 6's report test exercises the accumulator.

- [ ] **Step 1: Import the new helpers** — extend the existing `sizing` import:

```python
from app.services.brokers.binance.spot_demo.sizing import (
    CloseQtyDust,
    CloseQtyResult,
    SizingBlocked,
    SizingResult,
    classify_close_residual,
    compute_close_qty,
    compute_demo_order_qty,
)
```

- [ ] **Step 2: Replace the `qty=qty` SELL in `_close_with_sell`** (currently `scripts/binance_spot_demo_smoke.py:553-648`). Before recording `planned`, derive the fee-aware close qty from the live free balance. `_close_with_sell` already receives `symbol`, `qty` (buy qty — kept only for reference), `notional`, plus `execution`, `ledger`, `session`. Add params `step_size: Decimal`, `min_notional: Decimal`, `ref_price: Decimal`, `report: dict[str, Any]` (threaded from Task 6) and replace the body's qty derivation:

```python
    base_asset = symbol.removesuffix("USDT")
    balance = await execution.get_asset_balance(asset=base_asset)
    close_sizing = compute_close_qty(
        free_balance=balance.free,
        price=ref_price,
        min_notional=min_notional,
        step_size=step_size,
    )
    if isinstance(close_sizing, CloseQtyDust):
        # Nothing sellable at min-notional; the BUY left only dust. Confirm a
        # clean book, then reconcile with a dust note (NOT anomaly).
        report["close_qty"] = "0"
        report["residual_dust_amount"] = str(close_sizing.free)
        report["residual_dust_notional"] = str(close_sizing.notional_usdt)
        await ledger.record_closed(client_order_id=buy_cid, now=_now_utc())
        await session.commit()
        _trace(
            f"close skipped dust base={base_asset} free={close_sizing.free} "
            f"notional={close_sizing.notional_usdt} reason={close_sizing.reason}"
        )
        return await _reconcile(
            execution=execution,
            ledger=ledger,
            session=session,
            buy_cid=buy_cid,
            close_cid=None,
            symbol=symbol,
            sell_was_filled=None,
            dust_note=close_sizing.reason,
            report=report,
            step_size=step_size,
            min_notional=min_notional,
            ref_price=ref_price,
        )
    assert isinstance(close_sizing, CloseQtyResult)
    close_qty = close_sizing.qty
    report["close_qty"] = str(close_qty)
```

Then use `close_qty` (not `qty`) for `record_planned(... qty=close_qty ...)` and `submit_order(... qty=close_qty ...)`. Pass `report`, `step_size`, `min_notional`, `ref_price` through to `_reconcile`.

- [ ] **Step 3: Extend `_reconcile`** (`scripts/binance_spot_demo_smoke.py:690`) to take `dust_note: str | None = None`, `report`, `step_size`, `min_notional`, `ref_price`, and after confirming open orders are empty, classify any residual base asset:

```python
    base_asset = symbol.removesuffix("USDT")
    balance = await execution.get_asset_balance(asset=base_asset)
    outcome = classify_close_residual(
        free_after=balance.free,
        price=ref_price,
        min_notional=min_notional,
        step_size=step_size,
        open_orders_empty=is_empty,
    )
    report["open_orders_count"] = len(open_orders.orders)
    if outcome.kind == "anomaly":
        report["reconciliation_status"] = "anomaly"
        report["blockers"].append(outcome.remediation_hint or "residual_after_close")
        report["remediation_hint"] = outcome.remediation_hint
        await ledger.record_anomaly(
            client_order_id=buy_cid,
            reason=f"residual_after_close: {outcome.remediation_hint}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={buy_cid} reason=residual_after_close")
        return 2
    # dust or clean: reconcile with a note recording residual size.
    report["reconciliation_status"] = "dust" if balance.free > 0 else "reconciled"
    report["residual_dust_amount"] = str(balance.free)
    report["residual_dust_notional"] = str(balance.free * ref_price)
    await ledger.record_reconciled(
        client_order_id=buy_cid,
        now=_now_utc(),
        extra_metadata_merge={
            "residual_dust": {
                "asset": base_asset,
                "free": str(balance.free),
                "notional_usdt": str(balance.free * ref_price),
                "note": dust_note or "post-close residual within dust threshold",
            }
        }
        if balance.free > 0
        else None,
    )
```

(Keep the existing close-row reconcile + final commit + `spot_demo_confirm_reconciled` evidence emit.) The `is_empty == False` early-return anomaly branch already present stays as the first guard; the classification above runs only on the clean-book path.

- [ ] **Step 4: Run the existing smoke regression tests**

Run: `uv run pytest tests/scripts/test_binance_spot_demo_smoke.py -v`
Expected: PASS (adjust any test that asserted the old `qty=qty` SELL; if a test mocked the SELL without an `/api/v3/account` response, add a mocked account response with sufficient free balance).

- [ ] **Step 5: Commit**

```bash
git add scripts/binance_spot_demo_smoke.py tests/scripts/test_binance_spot_demo_smoke.py
git commit -m "feat(rob-299): fee-aware Spot Demo close + dust-vs-anomaly reconcile"
```

---

## Task 5: Futures Demo env readiness (no-secret, no-HTTP)

**Files:**
- Create: `app/services/brokers/binance/futures_demo/readiness.py`
- Test: `tests/services/brokers/binance/futures_demo/test_env_readiness.py`

- [ ] **Step 1: Write the failing tests**

```python
"""ROB-299 — Futures Demo no-secret env readiness."""

from __future__ import annotations

import pytest

from app.services.brokers.binance.futures_demo.readiness import (
    evaluate_futures_demo_env_readiness,
)


def test_reports_all_missing_at_once_without_raising(monkeypatch):
    for k in (
        "BINANCE_FUTURES_DEMO_ENABLED",
        "BINANCE_FUTURES_DEMO_API_KEY",
        "BINANCE_FUTURES_DEMO_API_SECRET",
        "BINANCE_FUTURES_DEMO_BASE_URL",
    ):
        monkeypatch.delenv(k, raising=False)
    r = evaluate_futures_demo_env_readiness()
    assert r.ready is False
    assert "BINANCE_FUTURES_DEMO_API_KEY" in r.missing
    assert "BINANCE_FUTURES_DEMO_API_SECRET" in r.missing


def test_no_secret_values_in_evidence(monkeypatch):
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "SUPER_SECRET_KEY_VALUE")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "SUPER_SECRET_SECRET_VALUE")
    monkeypatch.setenv(
        "BINANCE_FUTURES_DEMO_BASE_URL", "https://demo-fapi.binance.com"
    )
    ev = evaluate_futures_demo_env_readiness().to_evidence_dict()
    blob = repr(ev)
    assert "SUPER_SECRET_KEY_VALUE" not in blob
    assert "SUPER_SECRET_SECRET_VALUE" not in blob
    assert ev["api_key_present"] is True
    assert ev["base_url_host_allowed"] is True
    assert ev["ready"] is True


def test_ignores_spot_and_testnet_env(monkeypatch):
    for k in (
        "BINANCE_FUTURES_DEMO_ENABLED",
        "BINANCE_FUTURES_DEMO_API_KEY",
        "BINANCE_FUTURES_DEMO_API_SECRET",
        "BINANCE_FUTURES_DEMO_BASE_URL",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "spot-key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "spot-secret")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "testnet-key")
    r = evaluate_futures_demo_env_readiness()
    assert r.ready is False
    assert r.api_key_present is False
    assert r.api_secret_present is False


def test_host_judgment_rejects_non_demo_host_without_raising(monkeypatch):
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "k")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "s")
    monkeypatch.setenv(
        "BINANCE_FUTURES_DEMO_BASE_URL", "https://testnet.binancefuture.com"
    )
    r = evaluate_futures_demo_env_readiness()
    assert r.base_url_host_allowed is False
    assert r.ready is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/brokers/binance/futures_demo/test_env_readiness.py -v`
Expected: FAIL — `ModuleNotFoundError: ...futures_demo.readiness`

- [ ] **Step 3: Implement the module**

```python
"""ROB-299 — Futures Demo no-secret env readiness reflector.

Reports presence/absence + truthiness + host-allowlist judgment for the
``BINANCE_FUTURES_DEMO_*`` env quartet WITHOUT raising and WITHOUT echoing
any value. Independent from Spot Demo and legacy testnet env: this module
reads only the four ``BINANCE_FUTURES_DEMO_*`` keys.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from app.services.brokers.binance.futures_demo.host_allowlist import FUTURES_DEMO_HOSTS

_DEFAULT_BASE_URL = "https://demo-fapi.binance.com"
_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FuturesDemoEnvReadiness:
    enabled_present: bool
    enabled_truthy: bool
    api_key_present: bool
    api_secret_present: bool
    base_url_present: bool
    base_url_host: str | None
    base_url_host_allowed: bool
    missing: list[str] = field(default_factory=list)
    ready: bool = False

    def to_evidence_dict(self) -> dict[str, Any]:
        # Presence/judgment ONLY — never a value.
        return {
            "source": "futures_demo",
            "venue": "binance",
            "product": "usdm_futures",
            "enabled_present": self.enabled_present,
            "enabled_truthy": self.enabled_truthy,
            "api_key_present": self.api_key_present,
            "api_secret_present": self.api_secret_present,
            "base_url_present": self.base_url_present,
            "base_url_host": self.base_url_host,
            "base_url_host_allowed": self.base_url_host_allowed,
            "missing": list(self.missing),
            "ready": self.ready,
        }


def evaluate_futures_demo_env_readiness(
    env: Mapping[str, str] | None = None,
) -> FuturesDemoEnvReadiness:
    src = env if env is not None else os.environ
    enabled_raw = src.get("BINANCE_FUTURES_DEMO_ENABLED")
    api_key = src.get("BINANCE_FUTURES_DEMO_API_KEY") or ""
    api_secret = src.get("BINANCE_FUTURES_DEMO_API_SECRET") or ""
    base_url_raw = src.get("BINANCE_FUTURES_DEMO_BASE_URL")

    enabled_present = enabled_raw is not None
    enabled_truthy = bool(enabled_raw) and enabled_raw.strip().lower() in _TRUTHY
    api_key_present = bool(api_key)
    api_secret_present = bool(api_secret)
    base_url_present = bool(base_url_raw)

    effective_base = base_url_raw or _DEFAULT_BASE_URL
    host: str | None = httpx.URL(effective_base).host or None
    host_allowed = host in FUTURES_DEMO_HOSTS

    missing: list[str] = []
    if not enabled_truthy:
        missing.append("BINANCE_FUTURES_DEMO_ENABLED")
    if not api_key_present:
        missing.append("BINANCE_FUTURES_DEMO_API_KEY")
    if not api_secret_present:
        missing.append("BINANCE_FUTURES_DEMO_API_SECRET")

    ready = not missing and host_allowed
    return FuturesDemoEnvReadiness(
        enabled_present=enabled_present,
        enabled_truthy=enabled_truthy,
        api_key_present=api_key_present,
        api_secret_present=api_secret_present,
        base_url_present=base_url_present,
        base_url_host=host,
        base_url_host_allowed=host_allowed,
        missing=missing,
        ready=ready,
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/brokers/binance/futures_demo/test_env_readiness.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Add `--readiness` mode to the Futures smoke CLI**

In `scripts/binance_futures_demo_smoke.py`: add `mode.add_argument("--readiness", action="store_true", help="No-secret env readiness report. No HTTP, no credentials required.")`. In `main()`, **before** the `BINANCE_FUTURES_DEMO_ENABLED` disabled-gate early-return (so readiness runs even when disabled), branch:

```python
    if getattr(args, "readiness", False):
        from app.services.brokers.binance.futures_demo.readiness import (
            evaluate_futures_demo_env_readiness,
        )

        readiness = evaluate_futures_demo_env_readiness()
        _evidence({"event": "futures_demo_env_readiness", **readiness.to_evidence_dict()})
        return 0 if readiness.ready else 1
```

- [ ] **Step 6: Run the Futures smoke regression tests**

Run: `uv run pytest tests/scripts/test_binance_futures_demo_smoke.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/services/brokers/binance/futures_demo/readiness.py tests/services/brokers/binance/futures_demo/test_env_readiness.py scripts/binance_futures_demo_smoke.py
git commit -m "feat(rob-299): no-secret Futures Demo env readiness check + --readiness mode"
```

---

## Task 6: Structured smoke report event (minimal)

**Files:**
- Modify: `scripts/binance_spot_demo_smoke.py`
- Test: `tests/scripts/test_spot_smoke_report.py`

The report is a plain mutable `dict` accumulator created in `_run_confirm`, threaded through the lifecycle (Task 4 already writes `close_qty`, `open_orders_count`, `residual_dust_*`, `reconciliation_status`, `blockers`, `remediation_hint` into it), and finalized + emitted once via a pure builder.

- [ ] **Step 1: Write the failing test for the pure builder**

```python
"""ROB-299 — spot_demo_smoke_report builder shape."""

from __future__ import annotations

from scripts.binance_spot_demo_smoke import build_spot_smoke_report


def test_report_shape_clean_reconcile():
    report = {
        "deployed_sha": "abc1234",
        "env_enabled": True,
        "env_credentials_present": True,
        "buy_qty": "6.0",
        "buy_status": "FILLED",
        "close_qty": "5.9",
        "close_status": "FILLED",
        "open_orders_count": 0,
        "reconciliation_status": "reconciled",
        "blockers": [],
    }
    out = build_spot_smoke_report(report)
    assert out["event"] == "spot_demo_smoke_report"
    assert out["deployed_sha"] == "abc1234"
    assert out["reconciliation_status"] == "reconciled"
    assert out["residual_dust"] is None
    assert out["blockers"] == []


def test_report_shape_dust_includes_residual():
    report = {
        "deployed_sha": "abc1234",
        "env_enabled": True,
        "env_credentials_present": True,
        "close_qty": "0",
        "residual_dust_amount": "0.0925",
        "residual_dust_notional": "0.18",
        "reconciliation_status": "dust",
        "blockers": [],
    }
    out = build_spot_smoke_report(report)
    assert out["residual_dust"]["amount"] == "0.0925"
    assert out["residual_dust"]["notional_usdt"] == "0.18"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/scripts/test_spot_smoke_report.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_spot_smoke_report'`

- [ ] **Step 3: Implement the builder + thread the accumulator**

Add a `_deployed_sha()` best-effort helper and the pure builder:

```python
def _deployed_sha() -> str:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("DEPLOYED_SHA", "unknown")


def build_spot_smoke_report(report: dict[str, Any]) -> dict[str, Any]:
    """Pure: shape the accumulated fields into the final evidence event.

    Contains no secrets — only the operator-facing run summary."""
    dust_amount = report.get("residual_dust_amount")
    residual = (
        {
            "amount": dust_amount,
            "notional_usdt": report.get("residual_dust_notional"),
        }
        if dust_amount is not None
        else None
    )
    return {
        "event": "spot_demo_smoke_report",
        "deployed_sha": report.get("deployed_sha", "unknown"),
        "env_enabled": report.get("env_enabled"),
        "env_credentials_present": report.get("env_credentials_present"),
        "buy_qty": report.get("buy_qty"),
        "buy_status": report.get("buy_status"),
        "close_qty": report.get("close_qty"),
        "close_status": report.get("close_status"),
        "open_orders_count": report.get("open_orders_count"),
        "residual_dust": residual,
        "reconciliation_status": report.get("reconciliation_status"),
        "blockers": list(report.get("blockers", [])),
        "remediation_hint": report.get("remediation_hint"),
    }
```

In `_run_confirm`: create `report = {"deployed_sha": _deployed_sha(), "env_enabled": True, "env_credentials_present": True, "blockers": []}` after the client is built, pass it into `_execute_confirm_lifecycle` (which forwards it to `_close_with_sell`/`_reconcile`), set `report["buy_qty"]`/`report["buy_status"]` at the BUY submit site and `report["close_status"]` at the SELL submit site, and emit once in the `finally`:

```python
    finally:
        _evidence(build_spot_smoke_report(report))
        await execution.aclose()
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/scripts/test_spot_smoke_report.py tests/scripts/test_binance_spot_demo_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/binance_spot_demo_smoke.py tests/scripts/test_spot_smoke_report.py
git commit -m "feat(rob-299): structured spot_demo_smoke_report evidence event"
```

---

## Task 7: Runbook updates (no secrets)

**Files:**
- Modify: `docs/runbooks/binance-spot-demo-smoke.md`
- Modify: `docs/runbooks/binance-futures-demo-smoke.md`

- [ ] **Step 1: Spot runbook** — document fee-aware close (close qty derived from free base-asset balance, step-floored, min-notional gated), dust-vs-anomaly semantics (dust = clean book + sub-min-notional residue → `reconciled` with `residual_dust` note; anomaly = dirty book OR sellable residue → remediation hint), and the `spot_demo_smoke_report` event with its field list. Placeholder env names only.

- [ ] **Step 2: Futures runbook** — document `--readiness` as a no-secret, no-HTTP mode; show the exact command and a sample redacted `futures_demo_env_readiness` event (present/missing/host only). State explicitly that `BINANCE_SPOT_DEMO_*` and `BINANCE_TESTNET_*` are NOT consulted. Placeholder env names only.

Exact command to document:

```bash
uv run python -m scripts.binance_futures_demo_smoke --readiness
```

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/binance-spot-demo-smoke.md docs/runbooks/binance-futures-demo-smoke.md
git commit -m "docs(rob-299): runbook updates for fee-aware close, dust, futures readiness"
```

---

## Task 8: Full gate + final verification

- [ ] **Step 1: Ruff check**

Run: `uv run ruff check app/ tests/ scripts/binance_spot_demo_smoke.py scripts/binance_futures_demo_smoke.py`
Expected: PASS (no errors)

- [ ] **Step 2: Ruff format check**

Run: `uv run ruff format --check app/ tests/ scripts/binance_spot_demo_smoke.py scripts/binance_futures_demo_smoke.py`
Expected: PASS

- [ ] **Step 3: Targeted pytest (Binance Demo smoke + ledger)**

Run:
```bash
uv run pytest tests/services/brokers/binance/spot_demo tests/services/brokers/binance/futures_demo tests/services/brokers/binance/demo tests/scripts/test_binance_spot_demo_smoke.py tests/scripts/test_binance_futures_demo_smoke.py tests/scripts/test_spot_smoke_report.py -v
```
Expected: PASS (all)

- [ ] **Step 4: Confirm no live/scheduler/testnet drift**

Run: `uv run pytest tests/services/brokers/binance/spot_demo/test_testnet_env_does_not_activate_demo.py tests/services/brokers/binance/futures_demo/test_testnet_env_does_not_activate_demo.py tests/services/brokers/binance/futures_demo/test_spot_demo_env_does_not_activate_futures.py -v`
Expected: PASS (existing invariants untouched)

---

## Safety Boundaries (must hold across every task)

- **No-secret:** New surfaces emit only fingerprints, present/missing booleans, host-allowlist judgments, and single-asset / close / dust amounts. Never the raw api_key, api_secret, or the full `/api/v3/account` payload (all other balance rows are dropped in `get_asset_balance`). Readiness output carries zero values.
- **No-live / no-testnet:** No host allowlist changes; `demo-api.binance.com` / `demo-fapi.binance.com` only. SELL stays operator-gated (`confirm=True`). Readiness and the report builder dispatch zero HTTP. The deprecated-testnet deny-lists are untouched.
- **No-scheduler:** No TaskIQ / Prefect / cron / Hermes wiring added. CLI-only entry points.
- **Futures independence:** `evaluate_futures_demo_env_readiness` reads only `BINANCE_FUTURES_DEMO_*`. A regression test asserts Spot/testnet env never flips Futures readiness to ready.
- **Ledger writes via service only:** dust → `record_reconciled` (with `residual_dust` metadata note); residual/dirty → `record_anomaly` (with remediation hint). No new state-machine states; no direct SQL.

---

## Self-Review

- **Spec coverage:** Scope 1 (fee-aware close, free-balance/step/min-notional, dust-not-anomaly, ledger state + remediation hint) → Tasks 2-4. Scope 2 (no-secret futures readiness, independence, runbook/env) → Tasks 5, 7 (env.example already complete). Scope 3 (concise report: SHA, env readiness, mode results, buy/close qty + status, open orders, residual dust, reconciliation, blockers) → Task 6. All acceptance-criteria tests → Tasks 2/3/5/6; ruff + format + targeted pytest → Task 8. ✅
- **Type consistency:** `compute_close_qty` → `CloseQtyResult | CloseQtyDust`; `classify_close_residual` → `CloseResidualOutcome(kind, remediation_hint)`; `get_asset_balance` → `SpotDemoAssetBalance(asset, free, locked)`; `evaluate_futures_demo_env_readiness` → `FuturesDemoEnvReadiness`; `build_spot_smoke_report(report: dict) -> dict`. Names used consistently across tasks. ✅
- **Placeholder scan:** No TODO/TBD; every code step shows complete code; commands have expected output. ✅
