# KIS Mock Scalping Execution-Evidence Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `confirm_fill()` `None` stub with an authoritative, bounded KIS-mock fill-evidence gate driven by the daily order-execution inquiry, fail-closed by category, so confirmed mock scalping can observe real fills.

**Architecture:** A pure `fill_evidence` classifier maps KIS daily order-execution rows → a `FillEvidence` verdict + fail-closed category. `KisMockBroker.confirm_fill()` extracts the order number from the submit response, polls `inquire_daily_order_domestic(is_mock=True)` (read-only) once per call, runs the classifier, and returns a `Fill` only when fully filled (the executor's `_await_fill` provides the bounded retry loop and degrades any non-fill to an anomaly). A read-only smoke proves the path against the real mock API and validates the actual field names. No order submission, no scheduler, no live, no migration.

**Tech Stack:** Python 3.13, asyncio, `Decimal`, pytest (`pytest-asyncio`, `pytest-mock`), KIS REST client (`app.services.brokers.kis.KISClient`).

**Spec:** `docs/superpowers/specs/2026-05-27-rob334-kis-mock-fill-evidence-gate-design.md`

---

## File Structure

| Path | New/Edit | Responsibility |
|---|---|---|
| `app/services/brokers/kis/mock_scalping_exec/fill_evidence.py` | **new** | Pure classifier: rows + order_no → `FillEvidence`. stdlib-only (no broker/DB/network import). |
| `tests/brokers/kis/mock_scalping_exec/test_fill_evidence.py` | **new** | Classifier verdict × category matrix. |
| `app/services/brokers/kis/mock_scalping_exec/adapters.py` | **edit** | `_poll_fill_evidence()` + rewritten `confirm_fill()` + cached mock client. |
| `tests/brokers/kis/mock_scalping_exec/test_adapters.py` | **edit** | Update `confirm_fill` test; add poll/translation tests. |
| `scripts/kis_mock_fill_evidence_smoke.py` | **new** | Read-only daily-execution inquiry + classifier; default-disabled; no secret output. |
| `tests/brokers/kis/mock_scalping_exec/test_fill_evidence_smoke_cli.py` | **new** | Arg-parse + disabled no-op + classify-path (mocked client). |
| `docs/runbooks/kis-mock-scalping-smoke.md` | **edit** | Confirmed-run prerequisite, command shape, failure categories, no-op/rollback, 4-way separation. |

Reference facts (verified in code, cite when implementing):
- `inquire_daily_order_domestic(start_date, end_date, stock_code, side, order_number, is_mock=True)` returns raw `output1` rows; mock TR is `DOMESTIC_DAILY_ORDER_TR_MOCK` (`app/services/brokers/kis/domestic_orders.py:518-684`). Raises `RuntimeError` on `rt_cd != "0"`.
- `inquire_korea_orders(is_mock=True)` is **live-only** — raises immediately (`domestic_orders.py:111-115`). Do NOT use it.
- Mock submit response keys include `odno` and `order_no` (`app/mcp_server/tooling/kis_mock_ledger.py:336-358`).
- Mock client factory: `_create_kis_client(is_mock=True)` → `KISClient(is_mock=True)` (`app/mcp_server/tooling/order_execution.py:59-62`); `from app.services.brokers.kis import KISClient`.
- Executor consumes `confirm_fill` via the bounded `_await_fill` loop (`executor.py:302-308`, `max_fill_polls=10`, `poll_interval_seconds=1.0`) and degrades `None` to an `entry_unfilled`/`exit_unconfirmed` anomaly (`executor.py:194-203, 226-238`).
- Enable gate: `settings.kis_mock_scalping_ws_enabled`.

Field-name caveat: the `inquire_daily_order_domestic` docstring lists approximate keys (`ccld_qty`/`ccld_unpr`); real KIS daily-execution `output1` keys are more likely `tot_ccld_qty`/`avg_prvs`. The classifier therefore resolves **candidate keys** case-insensitively, and the smoke (Task 3) dumps the real row keys so we can confirm/tighten them.

---

### Task 1: Pure `fill_evidence` classifier

**Files:**
- Create: `app/services/brokers/kis/mock_scalping_exec/fill_evidence.py`
- Test: `tests/brokers/kis/mock_scalping_exec/test_fill_evidence.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/brokers/kis/mock_scalping_exec/test_fill_evidence.py`:

```python
"""ROB-334 — pure fill-evidence classifier tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    EvidenceCategory,
    FillVerdict,
    classify_fill_evidence,
)


def _row(**kw):
    base = {"odno": "0000123456", "pdno": "005930", "ord_qty": "1"}
    base.update(kw)
    return base


@pytest.mark.unit
def test_fully_filled_uses_avg_price() -> None:
    rows = [_row(tot_ccld_qty="1", avg_prvs="70000")]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.FILLED
    assert ev.filled_qty == Decimal("1")
    assert ev.avg_price == Decimal("70000")
    assert ev.category is None


@pytest.mark.unit
def test_filled_falls_back_to_amount_over_qty() -> None:
    rows = [_row(ord_qty="2", tot_ccld_qty="2", tot_ccld_amt="140600")]
    ev = classify_fill_evidence(order_no="0000123456", rows=rows)
    assert ev.verdict is FillVerdict.FILLED
    assert ev.avg_price == Decimal("70300")


@pytest.mark.unit
def test_zero_filled_is_pending() -> None:
    rows = [_row(tot_ccld_qty="0")]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.PENDING
    assert ev.category is None


@pytest.mark.unit
def test_partial_fill_is_partial_not_filled() -> None:
    rows = [_row(ord_qty="3", tot_ccld_qty="1", avg_prvs="70000")]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.PARTIAL


@pytest.mark.unit
def test_no_matching_row_is_none_data_precondition() -> None:
    rows = [_row(odno="999", tot_ccld_qty="1", avg_prvs="70000")]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.NONE
    assert ev.category is EvidenceCategory.DATA_PRECONDITION


@pytest.mark.unit
def test_filled_without_price_is_none_code() -> None:
    rows = [_row(tot_ccld_qty="1")]  # filled but no price/amount
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.NONE
    assert ev.category is EvidenceCategory.CODE


@pytest.mark.unit
def test_unparseable_qty_is_none_code() -> None:
    rows = [_row(tot_ccld_qty="abc", avg_prvs="70000")]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.NONE
    assert ev.category is EvidenceCategory.CODE


@pytest.mark.unit
def test_leading_zero_order_no_matches() -> None:
    rows = [_row(odno="0000123456", tot_ccld_qty="1", avg_prvs="70000")]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.FILLED


@pytest.mark.unit
def test_split_fill_rows_aggregate() -> None:
    rows = [
        _row(odno="123456", ord_qty="2", tot_ccld_qty="1", tot_ccld_amt="70000"),
        _row(odno="123456", ord_qty="2", tot_ccld_qty="1", tot_ccld_amt="70200"),
    ]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.FILLED
    assert ev.filled_qty == Decimal("2")
    assert ev.avg_price == Decimal("70100")  # 140200 / 2


@pytest.mark.unit
def test_uppercase_keys_resolved() -> None:
    rows = [{"ODNO": "123456", "ORD_QTY": "1", "TOT_CCLD_QTY": "1", "AVG_PRVS": "70000"}]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.FILLED
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/brokers/kis/mock_scalping_exec/test_fill_evidence.py -v`
Expected: FAIL — `ModuleNotFoundError: ... fill_evidence`.

- [ ] **Step 3: Implement the classifier**

Create `app/services/brokers/kis/mock_scalping_exec/fill_evidence.py`:

```python
"""ROB-334 — pure KIS-mock fill-evidence classifier.

Maps KIS daily order-execution rows (``inquire_daily_order_domestic`` raw
``output1``) for a given order number into a ``FillEvidence`` verdict plus a
fail-closed category. stdlib-only: no broker / DB / network import, so the gate
logic is unit-tested in isolation and cannot fabricate a fill.

The daily-execution field names differ across KIS surfaces, so each value is
resolved against ordered candidate keys, case-insensitively. The read-only
smoke (scripts/kis_mock_fill_evidence_smoke.py) confirms the real keys.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any


class FillVerdict(str, Enum):
    FILLED = "filled"
    PARTIAL = "partial"
    PENDING = "pending"
    NONE = "none"
    UNSUPPORTED = "unsupported"


class EvidenceCategory(str, Enum):
    """Issue ROB-334 fail-closed failure categories."""

    CODE = "code"
    ENV_CONFIG = "env/config"
    DATA_PRECONDITION = "data-precondition"
    UNSUPPORTED_MOCK_API = "unsupported mock API"
    OPERATOR_APPROVAL_NEEDED = "operator approval needed"


@dataclass(frozen=True)
class FillEvidence:
    verdict: FillVerdict
    filled_qty: Decimal | None
    avg_price: Decimal | None
    category: EvidenceCategory | None  # populated only for fail-closed verdicts
    reason_code: str
    detail: str


_ORDER_NO_KEYS = ("odno", "ord_no")
_ORD_QTY_KEYS = ("ord_qty",)
_FILLED_QTY_KEYS = ("tot_ccld_qty", "ccld_qty")
_AVG_PRICE_KEYS = ("avg_prvs", "ccld_unpr", "ccld_avg_unpr")
_FILLED_AMT_KEYS = ("tot_ccld_amt", "ccld_amt")


def _lower_keys(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in row.items()}


def _get_field(lowered: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    for c in candidates:
        if c in lowered and lowered[c] not in (None, ""):
            return lowered[c]
    return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _sum_decimals(values: Iterable[Any]) -> Decimal | None:
    total = Decimal("0")
    saw_any = False
    for v in values:
        d = _to_decimal(v)
        if d is None:
            return None
        total += d
        saw_any = True
    return total if saw_any else None


def _order_no_matches(target: str, candidate: Any) -> bool:
    cand = str(candidate).strip()
    if not cand:
        return False
    return cand == target or cand.lstrip("0") == target.lstrip("0")


def classify_fill_evidence(
    *, order_no: str | None, rows: list[dict[str, Any]]
) -> FillEvidence:
    """Classify fill evidence for ``order_no`` from daily-execution rows."""
    target = (order_no or "").strip()
    if not target:
        return FillEvidence(
            FillVerdict.NONE, None, None, EvidenceCategory.DATA_PRECONDITION,
            "order_no_missing", "no order number to match",
        )

    matched = [
        _lower_keys(r)
        for r in rows
        if _order_no_matches(target, _get_field(_lower_keys(r), _ORDER_NO_KEYS))
    ]
    if not matched:
        return FillEvidence(
            FillVerdict.NONE, None, None, EvidenceCategory.DATA_PRECONDITION,
            "no_matching_order", f"no daily-execution row for odno={target}",
        )

    ord_qty = _to_decimal(_get_field(matched[0], _ORD_QTY_KEYS))
    filled_qty = _sum_decimals(_get_field(m, _FILLED_QTY_KEYS) for m in matched)
    if ord_qty is None or filled_qty is None:
        return FillEvidence(
            FillVerdict.NONE, None, None, EvidenceCategory.CODE,
            "unparseable_qty", "could not parse ord_qty / filled_qty",
        )

    if filled_qty <= 0:
        return FillEvidence(
            FillVerdict.PENDING, Decimal("0"), None, None,
            "pending", f"order {target} accepted, no fill yet",
        )

    avg_price = _resolve_avg_price(matched, filled_qty)
    if avg_price is None or avg_price <= 0:
        return FillEvidence(
            FillVerdict.NONE, filled_qty, None, EvidenceCategory.CODE,
            "missing_fill_price", "filled qty present but no usable fill price",
        )

    if ord_qty > 0 and filled_qty >= ord_qty:
        return FillEvidence(
            FillVerdict.FILLED, filled_qty, avg_price, None,
            "filled", f"order {target} filled {filled_qty}@{avg_price}",
        )
    return FillEvidence(
        FillVerdict.PARTIAL, filled_qty, avg_price, None,
        "partial_fill", f"order {target} partial {filled_qty}/{ord_qty}",
    )


def _resolve_avg_price(
    matched: list[dict[str, Any]], filled_qty: Decimal
) -> Decimal | None:
    for m in matched:
        p = _to_decimal(_get_field(m, _AVG_PRICE_KEYS))
        if p is not None and p > 0:
            return p
    amt = _sum_decimals(_get_field(m, _FILLED_AMT_KEYS) for m in matched)
    if amt is not None and amt > 0 and filled_qty > 0:
        return amt / filled_qty
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/brokers/kis/mock_scalping_exec/test_fill_evidence.py -v`
Expected: PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/kis/mock_scalping_exec/fill_evidence.py \
        tests/brokers/kis/mock_scalping_exec/test_fill_evidence.py
git commit -m "feat(rob-334): pure KIS-mock fill-evidence classifier

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: Wire the bounded poll + classifier into `confirm_fill()`

**Files:**
- Modify: `app/services/brokers/kis/mock_scalping_exec/adapters.py:104-112` (and `__init__`, imports)
- Test: `tests/brokers/kis/mock_scalping_exec/test_adapters.py` (update one test, add four)

- [ ] **Step 1: Write/Update the failing tests**

In `tests/brokers/kis/mock_scalping_exec/test_adapters.py`, **replace** the existing `test_confirm_fill_returns_none_pending_validation` (lines 81-85) with the block below, and add the new imports at the top (`from decimal import Decimal` already present):

```python
from app.services.brokers.kis.mock_scalping_exec.executor import Fill, Quote


def _daily_rows(**kw):
    base = {"odno": "0000123456", "pdno": "005930", "ord_qty": "1"}
    base.update(kw)
    return [base]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fill_returns_none_when_no_odno() -> None:
    # No odno in the submit response -> data-precondition, no network call.
    broker = KisMockBroker(get_state=lambda s: None)
    assert await broker.confirm_fill({"any": "result"}) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fill_returns_fill_when_filled(mocker) -> None:
    broker = KisMockBroker(get_state=lambda s: None)
    fake_client = mocker.MagicMock()
    fake_client.domestic_orders.inquire_daily_order_domestic = AsyncMock(
        return_value=_daily_rows(tot_ccld_qty="1", avg_prvs="70000")
    )
    mocker.patch.object(broker, "_get_mock_client", return_value=fake_client)
    fill = await broker.confirm_fill({"odno": "0000123456"})
    assert fill == Fill(price=Decimal("70000"), quantity=Decimal("1"))
    # Bounded read-only inquiry: is_mock pinned True, filtered by order number.
    kw = fake_client.domestic_orders.inquire_daily_order_domestic.await_args.kwargs
    assert kw["is_mock"] is True
    assert kw["order_number"] == "0000123456"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fill_none_when_pending(mocker) -> None:
    broker = KisMockBroker(get_state=lambda s: None)
    fake_client = mocker.MagicMock()
    fake_client.domestic_orders.inquire_daily_order_domestic = AsyncMock(
        return_value=_daily_rows(tot_ccld_qty="0")
    )
    mocker.patch.object(broker, "_get_mock_client", return_value=fake_client)
    assert await broker.confirm_fill({"odno": "0000123456"}) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fill_none_on_unsupported_mock_api(mocker) -> None:
    broker = KisMockBroker(get_state=lambda s: None)
    fake_client = mocker.MagicMock()
    fake_client.domestic_orders.inquire_daily_order_domestic = AsyncMock(
        side_effect=RuntimeError("TR is not available in mock mode.")
    )
    mocker.patch.object(broker, "_get_mock_client", return_value=fake_client)
    assert await broker.confirm_fill({"odno": "0000123456"}) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_poll_fill_evidence_maps_unsupported_category(mocker) -> None:
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        EvidenceCategory,
        FillVerdict,
    )

    broker = KisMockBroker(get_state=lambda s: None)
    fake_client = mocker.MagicMock()
    fake_client.domestic_orders.inquire_daily_order_domestic = AsyncMock(
        side_effect=RuntimeError("VTTC8001R not available in mock")
    )
    mocker.patch.object(broker, "_get_mock_client", return_value=fake_client)
    ev = await broker._poll_fill_evidence({"odno": "123456"})
    assert ev.verdict is FillVerdict.UNSUPPORTED
    assert ev.category is EvidenceCategory.UNSUPPORTED_MOCK_API
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/brokers/kis/mock_scalping_exec/test_adapters.py -v`
Expected: FAIL — `_get_mock_client` / `_poll_fill_evidence` not defined; `confirm_fill` returns `None` unconditionally so `test_confirm_fill_returns_fill_when_filled` fails.

- [ ] **Step 3: Implement the wiring in `adapters.py`**

Add imports near the top of `adapters.py` (after the existing imports):

```python
import datetime

from app.mcp_server.tooling.order_execution import _create_kis_client
from app.services.brokers.kis import KISClient
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    EvidenceCategory,
    FillEvidence,
    FillVerdict,
    classify_fill_evidence,
)
```

In `KisMockBroker.__init__`, add the cached-client field:

```python
    def __init__(self, *, get_state: StateProvider, strategy_id: str = "kis-mock-v1"):
        self._get_state = get_state
        self._strategy_id = strategy_id
        self._mock_client: KISClient | None = None
```

Replace `confirm_fill` (lines 104-112) with:

```python
    def _get_mock_client(self) -> KISClient:
        # Mock-host client (live singleton would 401/EGW02005); cached so the
        # executor's bounded _await_fill loop does not re-construct per poll.
        if self._mock_client is None:
            self._mock_client = _create_kis_client(is_mock=True)
        return self._mock_client

    async def confirm_fill(self, submit_result: dict[str, Any]) -> Fill | None:
        # ROB-334: authoritative fill evidence from the KIS daily order-execution
        # inquiry. Returns a Fill only when fully filled; every other outcome is
        # fail-closed (None -> executor records entry_unfilled/exit_unconfirmed
        # anomaly). Never fabricates a fill.
        evidence = await self._poll_fill_evidence(submit_result)
        if (
            evidence.verdict is FillVerdict.FILLED
            and evidence.avg_price is not None
            and evidence.filled_qty is not None
        ):
            return Fill(price=evidence.avg_price, quantity=evidence.filled_qty)
        logger.info(
            "kis-mock fill unconfirmed verdict=%s category=%s reason=%s",
            evidence.verdict.value,
            evidence.category.value if evidence.category else "-",
            evidence.reason_code,
        )
        return None

    async def _poll_fill_evidence(
        self, submit_result: dict[str, Any]
    ) -> FillEvidence:
        order_no = submit_result.get("odno") or submit_result.get("order_no")
        if not order_no:
            return FillEvidence(
                FillVerdict.NONE, None, None,
                EvidenceCategory.DATA_PRECONDITION,
                "order_no_missing", "submit response carried no odno",
            )
        today = datetime.datetime.now().strftime("%Y%m%d")
        try:
            client = self._get_mock_client()
            rows = await client.domestic_orders.inquire_daily_order_domestic(
                start_date=today,
                end_date=today,
                stock_code="",
                order_number=str(order_no),
                is_mock=True,
            )
        except RuntimeError as exc:
            msg = str(exc)
            if _is_mock_unsupported(msg):
                return FillEvidence(
                    FillVerdict.UNSUPPORTED, None, None,
                    EvidenceCategory.UNSUPPORTED_MOCK_API,
                    "inquiry_unsupported", msg[:200],
                )
            return FillEvidence(
                FillVerdict.NONE, None, None, EvidenceCategory.CODE,
                "inquiry_error", msg[:200],
            )
        except Exception as exc:  # noqa: BLE001 - fail closed on any inquiry fault
            return FillEvidence(
                FillVerdict.NONE, None, None, EvidenceCategory.CODE,
                "inquiry_exception", str(exc)[:200],
            )
        return classify_fill_evidence(order_no=str(order_no), rows=rows)
```

Add this module-level helper (after the `_to_decimal` helper):

```python
def _is_mock_unsupported(message: str) -> bool:
    low = message.lower()
    return "mock" in low and (
        "unsupported" in low or "not available" in low or "아닙니다" in message
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/brokers/kis/mock_scalping_exec/test_adapters.py -v`
Expected: PASS (existing + 5 new). Also run the executor suite to confirm no regression in the `_await_fill` contract:
Run: `uv run pytest tests/brokers/kis/mock_scalping_exec/test_executor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/kis/mock_scalping_exec/adapters.py \
        tests/brokers/kis/mock_scalping_exec/test_adapters.py
git commit -m "feat(rob-334): wire bounded daily-order fill-evidence poll into confirm_fill

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: Read-only fill-evidence smoke

**Files:**
- Create: `scripts/kis_mock_fill_evidence_smoke.py`
- Test: `tests/brokers/kis/mock_scalping_exec/test_fill_evidence_smoke_cli.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/brokers/kis/mock_scalping_exec/test_fill_evidence_smoke_cli.py`:

```python
"""ROB-334 — read-only fill-evidence smoke CLI tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from scripts import kis_mock_fill_evidence_smoke as smoke


@pytest.mark.unit
def test_parse_args_defaults() -> None:
    args = smoke._parse_args([])
    assert args.order_no is None
    assert args.symbol is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disabled_is_noop(mocker) -> None:
    mocker.patch.object(smoke.settings, "kis_mock_scalping_ws_enabled", False)
    rc = await smoke.run_smoke(smoke._parse_args([]))
    assert rc == 4  # disabled / not configured -> env/config no-op


@pytest.mark.unit
@pytest.mark.asyncio
async def test_classifies_when_order_no_given(mocker) -> None:
    mocker.patch.object(smoke.settings, "kis_mock_scalping_ws_enabled", True)
    fake_client = mocker.MagicMock()
    fake_client.domestic_orders.inquire_daily_order_domestic = AsyncMock(
        return_value=[{"odno": "123456", "ord_qty": "1", "tot_ccld_qty": "1",
                       "avg_prvs": "70000"}]
    )
    mocker.patch.object(smoke, "_create_kis_client", return_value=fake_client)
    rc = await smoke.run_smoke(smoke._parse_args(["--order-no", "123456"]))
    assert rc == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inquiry_error_returns_2(mocker) -> None:
    mocker.patch.object(smoke.settings, "kis_mock_scalping_ws_enabled", True)
    fake_client = mocker.MagicMock()
    fake_client.domestic_orders.inquire_daily_order_domestic = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    mocker.patch.object(smoke, "_create_kis_client", return_value=fake_client)
    rc = await smoke.run_smoke(smoke._parse_args(["--order-no", "123456"]))
    assert rc == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/brokers/kis/mock_scalping_exec/test_fill_evidence_smoke_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.kis_mock_fill_evidence_smoke`.

- [ ] **Step 3: Implement the smoke script**

Create `scripts/kis_mock_fill_evidence_smoke.py`:

```python
#!/usr/bin/env python3
"""KIS mock fill-evidence read-only smoke (ROB-334).

Read-only: queries the KIS **mock** daily order-execution inquiry
(inquire_daily_order_domestic, is_mock=True) and runs the fill-evidence
classifier. Never submits, modifies, or cancels an order. Default-disabled —
requires KIS_MOCK_SCALPING_WS_ENABLED=true and KIS mock config. Prints only the
verdict/category and non-sensitive row keys; never prints secrets.

This smoke ALSO validates the real KIS daily-execution field names (it prints
the observed row keys) so the classifier candidate-key lists can be tightened.

Exit codes:
    0  - success (classified, or rows listed)
    1  - unexpected exception
    2  - inquiry error / unsupported mock API
    4  - disabled or KIS mock not configured (env/config no-op)

Usage:
    KIS_MOCK_SCALPING_WS_ENABLED=true uv run python -m scripts.kis_mock_fill_evidence_smoke \
        --order-no 0000123456 --symbol 005930
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import sys

from app.core.config import settings
from app.mcp_server.tooling.order_execution import _create_kis_client
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    classify_fill_evidence,
)

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KIS mock fill-evidence read-only smoke")
    today = datetime.datetime.now().strftime("%Y%m%d")
    parser.add_argument("--from-date", default=today, help="YYYYMMDD (default: today)")
    parser.add_argument("--to-date", default=today, help="YYYYMMDD (default: today)")
    parser.add_argument("--symbol", default=None, help="KR stock code filter (optional)")
    parser.add_argument("--order-no", default=None, help="Order number to classify (optional)")
    parser.add_argument("--max-rows", type=int, default=20)
    return parser.parse_args(argv)


async def run_smoke(args: argparse.Namespace) -> int:
    if not settings.kis_mock_scalping_ws_enabled:
        logger.info(
            "KIS_MOCK_SCALPING_WS_ENABLED is not set; fill-evidence smoke disabled (no-op)."
        )
        return 4
    if not (settings.kis_mock_app_key and settings.kis_mock_app_secret
            and settings.kis_mock_account_no):
        logger.error(
            "KIS mock not configured. Set: KIS_MOCK_APP_KEY, KIS_MOCK_APP_SECRET, "
            "KIS_MOCK_ACCOUNT_NO (names only — values not read here)."
        )
        return 4

    client = _create_kis_client(is_mock=True)
    try:
        rows = await client.domestic_orders.inquire_daily_order_domestic(
            start_date=args.from_date,
            end_date=args.to_date,
            stock_code=args.symbol or "",
            order_number=args.order_no or "",
            is_mock=True,
        )
    except Exception as exc:  # noqa: BLE001 - read-only smoke, classify the fault
        logger.error("daily order-execution inquiry failed: %s", str(exc)[:300])
        return 2

    logger.info("rows=%d (showing up to %d)", len(rows), args.max_rows)
    for row in rows[: args.max_rows]:
        logger.info("row keys: %s", sorted(str(k) for k in row.keys()))

    if args.order_no:
        ev = classify_fill_evidence(order_no=args.order_no, rows=rows)
        logger.info(
            "verdict=%s category=%s reason=%s filled_qty=%s avg_price=%s",
            ev.verdict.value,
            ev.category.value if ev.category else "-",
            ev.reason_code,
            ev.filled_qty,
            ev.avg_price,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        return asyncio.run(run_smoke(_parse_args(argv)))
    except KeyboardInterrupt:
        return 1
    except Exception:  # noqa: BLE001
        logger.exception("unexpected error in fill-evidence smoke")
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

> If `settings` lacks `kis_mock_app_key`/`kis_mock_app_secret`/`kis_mock_account_no`, grep `app/core/config.py` for the actual KIS-mock config attribute names and substitute them in the config-check above (keep it a names-only check — never log values).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/brokers/kis/mock_scalping_exec/test_fill_evidence_smoke_cli.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/kis_mock_fill_evidence_smoke.py \
        tests/brokers/kis/mock_scalping_exec/test_fill_evidence_smoke_cli.py
git commit -m "feat(rob-334): read-only KIS-mock fill-evidence smoke

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: Runbook — confirmed-run prerequisite + operator checklist

**Files:**
- Modify: `docs/runbooks/kis-mock-scalping-smoke.md`

- [ ] **Step 1: Read the current runbook**

Run: `sed -n '1,80p' docs/runbooks/kis-mock-scalping-smoke.md`
Note its existing section headers so the additions match the style.

- [ ] **Step 2: Append the fill-evidence-gate section**

Append the following section (adjust heading depth to match the file):

```markdown
## Execution-evidence gate (ROB-334)

Before any confirmed mock scalping run (`KIS_MOCK_SCALPING_WS_CONFIRM=true`), the
fill-evidence path below must be available; otherwise the executor fails closed
(no fabricated fill) and records an `entry_unfilled` / `exit_unconfirmed` anomaly.

**Authoritative source:** KIS daily order-execution inquiry
`inquire_daily_order_domestic(is_mock=True)`. Holdings/cash delta (ROB-102)
remains secondary. `inquire_korea_orders` (TTTC8036R, pending inquiry) is
**live-only** and is never used in mock.

**Deferred gap:** the execution-notice WebSocket `H0STCNI9` (실시간 체결통보) is
NOT implemented (requires an AES-CBC-decrypted, HTS-ID handshake frame path). It
is a fail-closed, documented gap and a candidate follow-up issue.

### Read-only preflight (no order submission)

```bash
KIS_MOCK_SCALPING_WS_ENABLED=true uv run python -m scripts.kis_mock_fill_evidence_smoke \
    --order-no <ODNO> --symbol <KR_CODE>
```

Required env (names only — never echo values): `KIS_MOCK_APP_KEY`,
`KIS_MOCK_APP_SECRET`, `KIS_MOCK_ACCOUNT_NO`.

Expected success signal: exit `0`, a printed `verdict=...` line, and the
observed `row keys: [...]` (use these to confirm/tighten the classifier's
candidate field names).

### Failure categories

| category | meaning | operator action |
|---|---|---|
| `code` | parse/classifier fault, unexpected response | file a bug with the redacted detail |
| `env/config` | mock creds/account missing or gate off | set the named env vars; do not commit secrets |
| `data-precondition` | not regular session / no matching order / no odno | run during KRX session after a real mock order |
| `unsupported mock API` | the daily-execution inquiry is rejected in mock | stop; the authoritative path is unavailable |
| `operator approval needed` | confirmed run attempted without approval | obtain explicit operator approval first |

Exit codes: `0` ok · `2` inquiry error / unsupported · `4` disabled or not
configured · `1` unexpected.

### Confirmed one-off mock smoke (operator-gated, NOT run by this change)

The operator-approved bounded confirmed mock smoke (one minimal KRX limit order
round-trip) is a **separate, operator-gated step**. It is deferred here:
this change ships code + runbook + tests + the read-only preflight only.

Rollback / no-op: all additions are read-only or fail-closed. Reverting the PR
restores the prior `confirm_fill` stub (always-unfilled). No migration, no
scheduler, no env mutation.
```

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/kis-mock-scalping-smoke.md
git commit -m "docs(rob-334): KIS-mock fill-evidence gate runbook + operator checklist

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Lint**

Run: `uv run ruff check app/ tests/ scripts/`
Expected: no new errors. Fix any reported in the files touched above (`uv run ruff format` if needed).

- [ ] **Step 2: Import-guard / package-boundary tests**

Run: `uv run pytest tests/ -v -k "import_guard or host_allowlist or mock_scalping"`
Expected: PASS. `fill_evidence.py` is stdlib-only; if a guard test enumerates the `mock_scalping_exec` package, confirm it still passes (the new module imports nothing from `app.*`).

- [ ] **Step 3: Full targeted suite**

Run: `uv run pytest tests/brokers/kis/mock_scalping_exec/ -v`
Expected: PASS (classifier + adapters + executor + smoke CLI + existing).

- [ ] **Step 4: Confirm no live/scheduler/secret surfaces touched**

Run: `git diff --stat origin/main...HEAD`
Confirm only the 7 files in the File Structure table changed; no migration, no scheduler/Prefect/TaskIQ/cron file, no `.env*`.

- [ ] **Step 5: Open the PR**

```bash
git push -u origin rob-334
gh pr create --base main --title "feat(rob-334): KIS mock scalping execution-evidence gate" \
  --body "$(cat <<'EOF'
Implements the KIS official mock execution-evidence gate (ROB-334), a follow-up
to ROB-321 / PR #981.

## What
- Pure `fill_evidence` classifier: daily order-execution rows + order number →
  verdict (filled/partial/pending/none/unsupported) + fail-closed category.
- `confirm_fill()` now polls `inquire_daily_order_domestic(is_mock=True)`
  (read-only, bounded by the executor's `_await_fill` loop) and returns a `Fill`
  only when fully filled; every other outcome fails closed to an anomaly.
- Read-only `scripts/kis_mock_fill_evidence_smoke.py` proves the path against the
  real mock API and validates the actual field names.
- Runbook: confirmed-run prerequisite, command shape, failure categories,
  deferred `H0STCNI9` gap.

## Safety
- No KIS live (mock host only).
- No confirmed order submitted — the operator-approved one-off confirmed mock
  smoke is left as a separate operator-gated step.
- No scheduler / Prefect / TaskIQ / cron / launchd activation.
- No persistent `KIS_MOCK_SCALPING_WS_CONFIRM=true`.
- No production env/secret change or logging; no live broker/order/watch/
  order-intent mutation. No DB migration.

## Remaining unsupported gap
- Execution-notice WS `H0STCNI9` (real-time 체결통보) not implemented
  (AES-CBC + HTS-ID handshake) — documented fail-closed; candidate follow-up.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

> Per the project's pre-merge gate: confirm the Test workflow is green before any merge; branch protection does not gate lint/test.

---

## Self-Review

- **Spec coverage:** §2.1 source-of-truth → Task 1+2; §3 components → Tasks 1-4; §4 data flow → Task 2; §5 taxonomy → Task 1 enum + Task 2 mapping + Task 4 table; §6 tests/safety → Tasks 1-3, 5; §7 single PR/no migration → Task 5; §8 acceptance → all tasks. No gaps.
- **Placeholder scan:** no TBD/TODO; every code step has full code. The one conditional (`settings` config-attr names in Task 3 Step 3) is an explicit grep-and-substitute instruction, not a silent placeholder.
- **Type consistency:** `FillEvidence` / `FillVerdict` / `EvidenceCategory` / `classify_fill_evidence` names identical across Tasks 1-3; `Fill(price=, quantity=)` matches `executor.Fill` (`executor.py:48-52`); `_get_mock_client` / `_poll_fill_evidence` names identical between adapter impl (Task 2 Step 3) and tests (Task 2 Step 1).
- **Spec deviation (intentional):** spec §4 said "no odno → unsupported mock API"; this plan classifies "no odno" as `data-precondition` (`order_no_missing`) and reserves `unsupported mock API` for an actually-rejected inquiry — the more accurate mapping. Spec §4 to be aligned.
```
