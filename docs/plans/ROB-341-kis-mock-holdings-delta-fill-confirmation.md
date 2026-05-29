# ROB-341 — KIS mock same-day fill confirmation via holdings/cash delta Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make KIS **mock** same-day scalping fill confirmation use a baseline-vs-post **holdings delta** (primary, load-bearing) + **cash delta** (corroboration + fill-price derivation) instead of `inquire_daily_order_domestic` (daily-ccld), which returns empty rows for same-day mock fills.

**Architecture:** Extract the existing ROB-102 reconciler's delta→verdict decision into a shared pure kernel (`classify_fill_by_delta`) reused by both the periodic reconciler and a new **synchronous** confirm path. `KisMockBroker` captures a baseline holdings+cash snapshot immediately before each submit, then `confirm_fill` polls a post-submit snapshot, computes the per-symbol delta, classifies it, derives a fill price from the cash delta (fallback = submitted limit price), and returns a `Fill` only when the delta unambiguously proves a fill — every other outcome is fail-closed (`None`). daily-ccld is demoted to a supplementary diagnostic that can never gate or override the holdings verdict.

**Tech Stack:** Python 3.13, asyncio, Decimal, pytest, KIS REST (`fetch_domestic_balance_snapshot` TR `VTTC8434R` mock), existing executor port architecture.

**Decided trade-off (surfaced for reviewer):** holdings delta proves *quantity* but not *price*. Price priority = (1) cash-delta-implied `|Δdnca_tot_amt| / qty` when cash moved and qty>0; (2) submitted limit price from `submit_result`. The chosen source is recorded in evidence. This keeps PnL telemetry meaningful while holdings remains the authoritative fill signal.

**Safety boundaries (hard):** KIS mock only (no live), limit orders only (no market), no scheduler / no persistent `KIS_MOCK_SCALPING_WS_CONFIRM=true`, no Prefect/TaskIQ/cron/launchd, no prod env/secret/DB-destructive change, no secret logging. Ambiguous evidence fails closed. Confirmed smoke only after read-only preflight + operator approval.

**STOP conditions (report with evidence, do not force):** (a) reconciler kernel reuse turns out infeasible; (b) the confirmed smoke shows mock holdings do **not** reflect a same-day fill within the bounded poll window.

---

## File Structure

- `app/services/kis_mock_holdings_reconciler.py` — **modify**: add pure `classify_fill_by_delta` kernel + `DeltaFillResult`; refactor `classify_orders` to delegate the delta→verdict decision to it (DRY, behavior-preserving).
- `app/services/brokers/kis/mock_scalping_exec/holdings_delta_confirm.py` — **create**: async snapshot fetch + `confirm_fill_from_holdings_delta` orchestration (baseline/post delta, cash-delta price derivation, fail-closed) — the broker-facing glue, separate from the pure kernel.
- `app/services/brokers/kis/mock_scalping_exec/adapters.py` — **modify**: `submit_buy`/`submit_exit_sell` capture a baseline snapshot before placing and stamp it (+ side/ordered_qty/intended_price) into the returned dict; `confirm_fill` rewired to the holdings-delta path; daily-ccld kept only as a logged supplementary diagnostic.
- `scripts/kis_mock_holdings_delta_smoke.py` — **create**: read-only preflight (snapshot read, no orders) + operator-gated bounded confirmed round-trip producing the ROB-341 evidence packet.
- `docs/runbooks/kis-mock-scalping-smoke.md` — **modify**: state daily-ccld is not primary same-day evidence; document new preflight + confirmed-smoke command shapes.
- `tests/test_kis_mock_holdings_delta_fill.py` — **create**: kernel + price-derivation + confirm fail-closed tests.
- `tests/test_kis_mock_holdings_reconciler.py` — **modify if present**: ensure refactor keeps reconciler behavior green.

---

## Task 1: Shared pure delta kernel

**Files:**
- Modify: `app/services/kis_mock_holdings_reconciler.py`
- Test: `tests/test_kis_mock_holdings_delta_fill.py` (create)

- [ ] **Step 1: Write the failing kernel test**

```python
# tests/test_kis_mock_holdings_delta_fill.py
from decimal import Decimal
import pytest
from app.services.kis_mock_holdings_reconciler import classify_fill_by_delta

@pytest.mark.unit
@pytest.mark.parametrize(
    "side,baseline,observed,ordered,verdict,filled",
    [
        ("buy", "0", "10", "10", "filled", "10"),
        ("buy", "0", "4", "10", "partial", "4"),
        ("buy", "0", "0", "10", "none", "0"),
        ("buy", "5", "15", "10", "filled", "10"),    # baseline position present
        ("buy", "5", "9", "10", "partial", "4"),     # delta below ordered
        ("buy", "5", "3", "10", "none", "0"),         # holdings DROPPED after a buy -> impossible -> none
        ("sell", "10", "0", "10", "filled", "10"),
        ("sell", "10", "6", "10", "partial", "4"),
        ("sell", "10", "10", "10", "none", "0"),
        ("sell", "10", "12", "10", "none", "0"),      # holdings ROSE after a sell -> impossible -> none
    ],
)
def test_classify_fill_by_delta(side, baseline, observed, ordered, verdict, filled):
    res = classify_fill_by_delta(
        side=side,
        ordered_qty=Decimal(ordered),
        baseline_qty=Decimal(baseline),
        observed_qty=Decimal(observed),
    )
    assert res.verdict == verdict
    assert res.filled_qty == Decimal(filled)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kis_mock_holdings_delta_fill.py -v`
Expected: FAIL with `ImportError: cannot import name 'classify_fill_by_delta'`

- [ ] **Step 3: Implement the kernel + refactor `classify_orders` to use it**

Add to `app/services/kis_mock_holdings_reconciler.py`:

```python
@dataclass(frozen=True, slots=True)
class DeltaFillResult:
    verdict: Literal["filled", "partial", "none"]
    filled_qty: Decimal  # signed-magnitude: always >= 0
    delta: Decimal


def classify_fill_by_delta(
    *,
    side: Literal["buy", "sell"],
    ordered_qty: Decimal,
    baseline_qty: Decimal,
    observed_qty: Decimal,
) -> DeltaFillResult:
    """Pure delta→fill decision shared by the periodic reconciler and the
    synchronous confirm path. ``filled_qty`` is the magnitude of the position
    change in the order's direction, clamped to ``ordered_qty``. A delta in the
    wrong direction (holdings dropped on a buy / rose on a sell) yields ``none``.
    """
    delta = observed_qty - baseline_qty
    directional = delta if side == "buy" else -delta
    if directional <= 0:
        return DeltaFillResult("none", Decimal("0"), delta)
    filled = directional if directional < ordered_qty else ordered_qty
    verdict = "filled" if directional >= ordered_qty else "partial"
    return DeltaFillResult(verdict, filled, delta)
```

Then refactor the `accepted / pending paths` block inside `classify_orders` (lines ~149-163) to delegate:

```python
        # accepted / pending paths — delegate the delta decision to the kernel.
        decision = classify_fill_by_delta(
            side=order.side,
            ordered_qty=order.ordered_qty,
            baseline_qty=order.holdings_baseline_qty,
            observed_qty=snapshot.quantity,
        )
        if decision.verdict == "filled":
            next_state, reason = "fill", "fill_detected"
        elif decision.verdict == "partial":
            next_state, reason = "fill", "partial_fill_detected"
        else:
            next_state, reason = _pending_or_stale(order, now, thresholds)
```

Add `"DeltaFillResult"` and `"classify_fill_by_delta"` to `__all__`.

- [ ] **Step 4: Run kernel + reconciler tests**

Run: `uv run pytest tests/test_kis_mock_holdings_delta_fill.py tests/test_kis_mock_holdings_reconciler.py -v`
Expected: PASS (reconciler tests unchanged — behavior preserved)

- [ ] **Step 5: Commit**

```bash
git add app/services/kis_mock_holdings_reconciler.py tests/test_kis_mock_holdings_delta_fill.py
git commit -m "feat(ROB-341): shared classify_fill_by_delta kernel reused by reconciler"
```

---

## Task 2: Cash-delta fill-price derivation (pure)

**Files:**
- Modify: `app/services/brokers/kis/mock_scalping_exec/holdings_delta_confirm.py` (create in this task)
- Test: `tests/test_kis_mock_holdings_delta_fill.py`

- [ ] **Step 1: Write the failing price-derivation test**

```python
from decimal import Decimal
import pytest
from app.services.brokers.kis.mock_scalping_exec.holdings_delta_confirm import (
    derive_fill_price,
)

@pytest.mark.unit
def test_price_from_cash_delta_buy():
    # cash dropped 100000 for 10 shares -> 10000/share
    price, source = derive_fill_price(
        side="buy", filled_qty=Decimal("10"),
        cash_baseline=Decimal("1000000"), cash_observed=Decimal("900000"),
        limit_price=Decimal("9999"),
    )
    assert price == Decimal("10000")
    assert source == "cash_delta"

@pytest.mark.unit
def test_price_falls_back_to_limit_when_cash_unmoved():
    price, source = derive_fill_price(
        side="buy", filled_qty=Decimal("10"),
        cash_baseline=Decimal("1000000"), cash_observed=Decimal("1000000"),
        limit_price=Decimal("9999"),
    )
    assert price == Decimal("9999")
    assert source == "limit_fallback"

@pytest.mark.unit
def test_price_falls_back_when_cash_unavailable():
    price, source = derive_fill_price(
        side="sell", filled_qty=Decimal("10"),
        cash_baseline=None, cash_observed=Decimal("900000"),
        limit_price=Decimal("8888"),
    )
    assert price == Decimal("8888")
    assert source == "limit_fallback"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kis_mock_holdings_delta_fill.py -k price -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create the module with `derive_fill_price`**

```python
# app/services/brokers/kis/mock_scalping_exec/holdings_delta_confirm.py
"""ROB-341 — synchronous holdings/cash-delta fill confirmation for KIS mock.

Primary same-day fill signal is the baseline-vs-post holdings delta (load
bearing). Cash delta is corroboration + the preferred fill-price source. This
module performs the broker-facing async snapshot reads and orchestration; the
delta→verdict decision lives in the pure ``classify_fill_by_delta`` kernel.
daily-ccld is never consulted here — empty same-day daily-ccld can neither
gate nor override this verdict.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal


def derive_fill_price(
    *,
    side: Literal["buy", "sell"],
    filled_qty: Decimal,
    cash_baseline: Decimal | None,
    cash_observed: Decimal | None,
    limit_price: Decimal,
) -> tuple[Decimal, str]:
    """Derive a fill price. Prefer the cash delta (``|Δcash| / filled_qty``);
    fall back to the submitted limit price when cash is unavailable, unmoved,
    or qty is zero. Returns ``(price, source)``."""
    if (
        cash_baseline is not None
        and cash_observed is not None
        and filled_qty > 0
    ):
        cash_delta = abs(cash_observed - cash_baseline)
        if cash_delta > 0:
            return cash_delta / filled_qty, "cash_delta"
    return limit_price, "limit_fallback"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kis_mock_holdings_delta_fill.py -k price -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/kis/mock_scalping_exec/holdings_delta_confirm.py tests/test_kis_mock_holdings_delta_fill.py
git commit -m "feat(ROB-341): cash-delta fill-price derivation with limit fallback"
```

---

## Task 3: Async confirm orchestration (snapshot fetch + fail-closed)

**Files:**
- Modify: `app/services/brokers/kis/mock_scalping_exec/holdings_delta_confirm.py`
- Test: `tests/test_kis_mock_holdings_delta_fill.py`

- [ ] **Step 1: Write the failing confirm test (with a fake snapshot provider)**

```python
from decimal import Decimal
import pytest
from app.services.brokers.kis.mock_scalping_exec.executor import Fill
from app.services.brokers.kis.mock_scalping_exec.holdings_delta_confirm import (
    BaselineSnapshot, confirm_fill_from_holdings_delta,
)

def _baseline(qty="0", cash="1000000"):
    return BaselineSnapshot(
        symbol="005930", side="buy", ordered_qty=Decimal("10"),
        limit_price=Decimal("70000"),
        holdings_qty=Decimal(qty),
        cash=(Decimal(cash) if cash is not None else None),
    )

@pytest.mark.unit
async def test_confirm_filled_returns_fill():
    async def post(symbol):  # observed holdings + cash
        return Decimal("10"), Decimal("300000")  # bought 10, cash dropped 700000
    fill = await confirm_fill_from_holdings_delta(_baseline(), fetch_post=post)
    assert isinstance(fill, Fill)
    assert fill.quantity == Decimal("10")
    assert fill.price == Decimal("70000")  # 700000/10 cash-delta

@pytest.mark.unit
async def test_confirm_no_delta_fails_closed():
    async def post(symbol):
        return Decimal("0"), Decimal("1000000")
    assert await confirm_fill_from_holdings_delta(_baseline(), fetch_post=post) is None

@pytest.mark.unit
async def test_confirm_baseline_missing_fails_closed():
    b = _baseline()
    object.__setattr__(b, "holdings_qty", None)
    async def post(symbol):
        return Decimal("10"), Decimal("300000")
    assert await confirm_fill_from_holdings_delta(b, fetch_post=post) is None

@pytest.mark.unit
async def test_confirm_snapshot_error_fails_closed():
    async def post(symbol):
        raise RuntimeError("VTS read failed")
    assert await confirm_fill_from_holdings_delta(_baseline(), fetch_post=post) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kis_mock_holdings_delta_fill.py -k confirm -v`
Expected: FAIL with `ImportError: cannot import name 'BaselineSnapshot'`

- [ ] **Step 3: Implement `BaselineSnapshot` + `confirm_fill_from_holdings_delta`**

Append to `holdings_delta_confirm.py`:

```python
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.services.brokers.kis.mock_scalping_exec.executor import Fill
from app.services.kis_mock_holdings_reconciler import classify_fill_by_delta

logger = logging.getLogger("rob341.kis_mock_holdings_delta")

# (symbol) -> (observed_holdings_qty, observed_cash | None)
PostFetch = Callable[[str], Awaitable[tuple[Decimal, Decimal | None]]]


@dataclass
class BaselineSnapshot:
    symbol: str
    side: Literal["buy", "sell"]
    ordered_qty: Decimal
    limit_price: Decimal
    holdings_qty: Decimal | None  # None => baseline read failed => fail closed
    cash: Decimal | None


async def confirm_fill_from_holdings_delta(
    baseline: BaselineSnapshot,
    *,
    fetch_post: PostFetch,
) -> Fill | None:
    """Return a ``Fill`` only when the post-submit holdings delta unambiguously
    proves a (full or partial) fill in the order's direction. Every other
    outcome — missing baseline, snapshot read failure, zero/wrong-direction
    delta — is fail-closed (``None``)."""
    if baseline.holdings_qty is None:
        logger.info("kis-mock holdings-delta confirm: baseline missing -> fail closed")
        return None
    try:
        observed_qty, observed_cash = await fetch_post(baseline.symbol)
    except Exception as exc:  # noqa: BLE001 - any read fault fails closed
        logger.info("kis-mock holdings-delta confirm: post-snapshot error %s", exc)
        return None

    decision = classify_fill_by_delta(
        side=baseline.side,
        ordered_qty=baseline.ordered_qty,
        baseline_qty=baseline.holdings_qty,
        observed_qty=observed_qty,
    )
    if decision.verdict == "none":
        logger.info(
            "kis-mock holdings-delta confirm: no fill delta sym=%s delta=%s",
            baseline.symbol, decision.delta,
        )
        return None

    price, price_source = derive_fill_price(
        side=baseline.side,
        filled_qty=decision.filled_qty,
        cash_baseline=baseline.cash,
        cash_observed=observed_cash,
        limit_price=baseline.limit_price,
    )
    logger.info(
        "kis-mock holdings-delta confirm: %s sym=%s qty=%s price=%s (%s)",
        decision.verdict, baseline.symbol, decision.filled_qty, price, price_source,
    )
    return Fill(price=price, quantity=decision.filled_qty)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kis_mock_holdings_delta_fill.py -k confirm -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/kis/mock_scalping_exec/holdings_delta_confirm.py tests/test_kis_mock_holdings_delta_fill.py
git commit -m "feat(ROB-341): fail-closed async holdings-delta confirm orchestration"
```

---

## Task 4: Wire baseline capture + holdings-delta confirm into `KisMockBroker`

**Files:**
- Modify: `app/services/brokers/kis/mock_scalping_exec/adapters.py`
- Test: `tests/test_kis_mock_scalping_adapters.py` (create if absent)

- [ ] **Step 1: Write the failing adapter test**

```python
# tests/test_kis_mock_scalping_adapters.py
from decimal import Decimal
import pytest
from app.services.brokers.kis.mock_scalping_exec.adapters import KisMockBroker
from app.services.brokers.kis.mock_scalping_exec.executor import Fill


@pytest.mark.unit
async def test_confirm_fill_uses_holdings_delta(monkeypatch):
    broker = KisMockBroker(get_state=lambda s: None)

    # Fake the snapshot reads: baseline 0 holdings / 1,000,000 cash;
    # post 10 holdings / 300,000 cash.
    calls = {"n": 0}
    async def fake_snapshot(symbol):
        calls["n"] += 1
        return (Decimal("0"), Decimal("1000000")) if calls["n"] == 1 \
            else (Decimal("10"), Decimal("300000"))
    monkeypatch.setattr(broker, "_read_snapshot", fake_snapshot)

    submit_result = {
        "odno": "0001",
        "_baseline": {
            "symbol": "005930", "side": "buy", "ordered_qty": "10",
            "limit_price": "70000", "holdings_qty": "0", "cash": "1000000",
        },
    }
    fill = await broker.confirm_fill(submit_result)
    assert isinstance(fill, Fill)
    assert fill.quantity == Decimal("10")


@pytest.mark.unit
async def test_confirm_fill_no_baseline_fails_closed():
    broker = KisMockBroker(get_state=lambda s: None)
    assert await broker.confirm_fill({"odno": "0001"}) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kis_mock_scalping_adapters.py -v`
Expected: FAIL (current `confirm_fill` reads daily-ccld, has no `_baseline`/`_read_snapshot`)

- [ ] **Step 3: Rewire `KisMockBroker`**

In `adapters.py`:

1. Add a snapshot reader that returns `(holdings_qty_for_symbol, cash)` from the mock domestic balance snapshot:

```python
    async def _read_snapshot(self, symbol: str) -> tuple[Decimal, Decimal | None]:
        from app.core.symbol import to_db_symbol
        client = self._get_mock_client()
        snap = await client.account.fetch_domestic_balance_snapshot(is_mock=True)
        target = to_db_symbol(symbol)
        qty = Decimal("0")
        for h in snap.get("holdings") or []:
            if to_db_symbol(str(h.get("pdno") or "")) == target:
                qty = _to_decimal(h.get("hldg_qty")) or Decimal("0")
                break
        cash = _to_decimal((snap.get("cash") or {}).get("dnca_tot_amt"))
        return qty, cash
```

2. Capture a baseline immediately before each submit and stamp it into the result. Wrap the existing `submit_buy`/`submit_exit_sell` bodies:

```python
    async def _capture_baseline(
        self, *, symbol: str, side: str, qty: Decimal, limit_price: Decimal
    ) -> dict[str, str | None]:
        try:
            h, cash = await self._read_snapshot(symbol)
            hq: str | None = str(h)
        except Exception:  # noqa: BLE001 - baseline read failure => confirm fails closed
            hq, cash = None, None
        return {
            "symbol": symbol, "side": side, "ordered_qty": str(qty),
            "limit_price": str(limit_price),
            "holdings_qty": hq, "cash": (str(cash) if cash is not None else None),
        }
```

In `submit_buy` (and `submit_exit_sell` with `side="sell"`), before the `_place_order_impl` call, capture the baseline; after, attach it:

```python
        baseline = await self._capture_baseline(
            symbol=symbol, side="buy", qty=quantity, limit_price=price
        )
        result = await _place_order_impl(... unchanged ...)
        if isinstance(result, dict):
            result["_baseline"] = baseline
        return result
```

3. Replace `confirm_fill` body to use the holdings-delta path (drop daily-ccld as the gate; keep it only as a logged diagnostic):

```python
    async def confirm_fill(self, submit_result: dict[str, Any]) -> Fill | None:
        raw = submit_result.get("_baseline")
        if not isinstance(raw, dict):
            logger.info("kis-mock confirm: no baseline snapshot -> fail closed")
            return None
        baseline = BaselineSnapshot(
            symbol=str(raw["symbol"]),
            side=raw["side"],
            ordered_qty=Decimal(str(raw["ordered_qty"])),
            limit_price=Decimal(str(raw["limit_price"])),
            holdings_qty=(Decimal(str(raw["holdings_qty"])) if raw.get("holdings_qty") is not None else None),
            cash=(Decimal(str(raw["cash"])) if raw.get("cash") is not None else None),
        )
        fill = await confirm_fill_from_holdings_delta(baseline, fetch_post=self._read_snapshot)
        # Supplementary, non-gating: log daily-ccld for post-settlement evidence only.
        await self._log_daily_ccld_diagnostic(submit_result)
        return fill
```

4. Rename the existing daily-ccld method to `_log_daily_ccld_diagnostic` and make it log-only (never returns a verdict that gates). It must classify empty same-day rows clearly (`pending`/`no_matching_order`) and log them, but its result MUST NOT influence the return of `confirm_fill`. Add imports: `from app.services.brokers.kis.mock_scalping_exec.holdings_delta_confirm import BaselineSnapshot, confirm_fill_from_holdings_delta`.

- [ ] **Step 4: Run adapter + full scalping-exec tests**

Run: `uv run pytest tests/test_kis_mock_scalping_adapters.py tests/ -k "scalping or fill_evidence" -v`
Expected: PASS (executor port contract unchanged; daily-ccll classifier tests still pass as a diagnostic)

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/kis/mock_scalping_exec/adapters.py tests/test_kis_mock_scalping_adapters.py
git commit -m "feat(ROB-341): KisMockBroker confirms fills via holdings/cash delta, daily-ccld demoted"
```

---

## Task 5: Read-only preflight + operator-gated confirmed smoke

**Files:**
- Create: `scripts/kis_mock_holdings_delta_smoke.py`
- Test: `tests/test_kis_mock_holdings_delta_smoke_cli.py` (create)

- [ ] **Step 1: Write the failing CLI-shape test (no secrets, no network)**

```python
# tests/test_kis_mock_holdings_delta_smoke_cli.py
import subprocess, sys
import pytest

@pytest.mark.unit
def test_smoke_help_runs_without_secrets():
    out = subprocess.run(
        [sys.executable, "-m", "scripts.kis_mock_holdings_delta_smoke", "--help"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0
    assert "--confirm" in out.stdout
    assert "--preflight" in out.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kis_mock_holdings_delta_smoke_cli.py -v`
Expected: FAIL (module missing)

- [ ] **Step 3: Implement the smoke CLI**

Create `scripts/kis_mock_holdings_delta_smoke.py`. Requirements:
- `argparse` with `--preflight` (read-only: fetch baseline snapshot, print holdings+cash for `--symbol`, exit 0; no orders), `--confirm` (place ONE small limit buy below market, poll holdings delta, derive fill, then cleanup-sell, print the full evidence packet), `--symbol`, `--notional-krw` (small cap, default 10000), `--max-poll`/`--poll-interval`.
- Lazy-import Settings-backed modules **only inside** the command body (per `feedback_operator_cli_lazy_settings_import` — `--help` must run without secrets).
- Default-disabled: require `KIS_MOCK_ENABLED=true` (or the existing scalping enable gate) and refuse confirm unless `--confirm` is explicitly passed.
- Never print secret values; print only missing env var **names**.
- Evidence packet (printed as JSON) MUST include: symbol, side(s), order id(s), baseline holdings/cash, post-submit holdings/cash, selected confirmation signal + price source, pending-order count, cleanup result, final position delta vs baseline.
- Always attempt cleanup-sell of any acquired position in a `finally` block; if cleanup cannot be confirmed, exit non-zero and label `anomaly`.

- [ ] **Step 4: Run CLI-shape test**

Run: `uv run pytest tests/test_kis_mock_holdings_delta_smoke_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/kis_mock_holdings_delta_smoke.py tests/test_kis_mock_holdings_delta_smoke_cli.py
git commit -m "feat(ROB-341): read-only preflight + operator-gated holdings-delta confirmed smoke"
```

---

## Task 6: Runbook update

**Files:**
- Modify: `docs/runbooks/kis-mock-scalping-smoke.md`

- [ ] **Step 1: Document the change**

Add a section stating:
- daily-ccld (`inquire_daily_order_domestic`) is **not** the primary same-day fill signal; its empty same-day result is supplementary/post-settlement only and never gates or overrides the holdings verdict.
- Same-day fills are confirmed via baseline-vs-post **holdings delta** (primary) + **cash delta** (corroboration + fill-price source); ambiguous/zero/wrong-direction deltas fail closed.
- `H0STCNI9` remains an unimplemented, documented gap and an explicit follow-up (out of ROB-341 scope).
- Read-only preflight command: `uv run python -m scripts.kis_mock_holdings_delta_smoke --preflight --symbol <code>`.
- Bounded confirmed-smoke command shape (operator-gated, secrets excluded): `KIS_MOCK_ENABLED=true uv run python -m scripts.kis_mock_holdings_delta_smoke --confirm --symbol <code> --notional-krw 10000`.

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/kis-mock-scalping-smoke.md
git commit -m "docs(ROB-341): daily-ccld demoted; holdings/cash-delta confirm runbook"
```

---

## Task 7: Verification, smoke, evidence

- [ ] **Step 1: Full lint + targeted tests**

Run: `uv run ruff check app/ tests/ scripts/ && uv run pytest tests/test_kis_mock_holdings_delta_fill.py tests/test_kis_mock_scalping_adapters.py tests/test_kis_mock_holdings_reconciler.py tests/test_kis_mock_holdings_delta_smoke_cli.py -v`
Expected: clean + all PASS

- [ ] **Step 2: Import guards** (binance/host grep guards that historically tripped red main)

Run: existing repo guard test(s), e.g. `uv run pytest tests/ -k "import_guard or host_allowlist" -v`
Expected: PASS

- [ ] **Step 3: Read-only preflight on host** (no orders)

Run: `uv run python -m scripts.kis_mock_holdings_delta_smoke --preflight --symbol <liquid KRX code>`
Expected: prints baseline holdings + cash; exit 0. If snapshot read fails → STOP, report.

- [ ] **Step 4: Operator-gated bounded confirmed smoke** (one small limit order + cleanup)

Run only after Step 3 passes and operator approves:
`KIS_MOCK_ENABLED=true uv run python -m scripts.kis_mock_holdings_delta_smoke --confirm --symbol <code> --notional-krw 10000`
Expected: holdings delta reflects the fill within the bounded poll; evidence packet printed; cleanup confirmed; no residual pending order / position delta.
**STOP condition:** if holdings do NOT reflect the fill within the bounded window → do not force; capture the evidence and report (the same-day-reflection assumption failed).

- [ ] **Step 5: Linear evidence comment**

Post a comment on ROB-341 with: the chosen primary signal (holdings delta + cash corroboration), the smoke evidence packet (symbol/side/order ids/baseline+post holdings+cash/selected signal+price source/pending count/cleanup/final delta), confirmation that daily-ccld is demoted, and the merged PR link. Do not mark Done on an open PR alone.

---

## Self-Review

- **Spec coverage:** §1 inspect (done in investigation) · §2 H0STCNI9 → documented-gap follow-up (Task 6) · §3 holdings/cash delta confirm (Tasks 1–4) · §4 ambiguity fail-closed (Task 1 wrong-direction `none`, Task 3 baseline-missing/snapshot-error/no-delta) · §5 runbook (Task 6) · §6 focused tests (Tasks 1–5) · §7 bounded confirmed smoke after preflight (Task 7). ✓
- **Acceptance:** primary source = holdings/cash delta with baseline + ledger correlation ✓; daily-ccld supplementary, empty classified ✓ (Task 4 diagnostic); unit tests cover delta success / baseline present / ambiguous-no-delta / stale-snapshot-error / cleanup ✓; runbook command shapes ✓; smoke evidence fields ✓.
- **Cash narrowing clause:** domestic mock cash is supported (verified), so cash is retained as corroboration + price source rather than dropped; the holdings-only fallback path still exists implicitly because `derive_fill_price` falls back to the limit price whenever cash is `None`/unmoved — so a future cash-unsupported regression degrades gracefully without breaking fill confirmation.
- **Type consistency:** `classify_fill_by_delta` / `DeltaFillResult` / `derive_fill_price` / `BaselineSnapshot` / `confirm_fill_from_holdings_delta` signatures match across Tasks 1–4. ✓
