# ROB-715 /invest Item-Level Learning-Loop Render — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render each /invest report item's forecast-resolution status + retrospective link, its stored-but-unrendered fields (`trigger_checklist`, `max_action`, `structured_evidence` summary, `decision_bucket` badge), and a plan-vs-actual view — so an operator reaches "이 판단의 결과(체결→forecast 해소→회고)" within 2 clicks.

**Architecture:** Thin read-only backend attaches two new item-keyed maps (`forecasts_by_item_uuid`, `retrospectives_by_item_uuid`) to the existing investment-reports bundle response, mirroring the established `linked_orders_by_item_uuid` / `decisions_by_item_uuid` pattern — one batched query per table, exact join on `report_item_uuid`. The frontend `ItemRow` component renders the new maps plus already-normalized raw fields. Migration 0.

**Tech Stack:** FastAPI, SQLAlchemy async (PostgreSQL, `review` schema), Pydantic v2, pytest/pytest-asyncio; React + TypeScript + Vitest (`frontend/invest`).

## Global Constraints

- **Migration 0.** No alembic migration. All backend schema additions are Pydantic *response-model* fields (additive, default-empty — legacy-safe). No DB DDL.
- **Read-only.** No broker / order / watch / order-intent mutation reachable from any new code. All new code is deterministic reads.
- **ROB-501.** No in-process LLM provider import anywhere under `app/**`. The static guard `tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py` must stay green.
- **Ownership boundary.** Do NOT edit `app/services/decision_history.py` or `app/services/trade_journal/aggregates.py` — those belong to ROB-717. This work queries `trade_forecasts` / `trade_retrospectives` directly by `report_item_uuid`.
- **Join key.** The item's `item_uuid` is stored in downstream tables' `report_item_uuid` column. On `TradeForecast` / `TradeRetrospective`, `report_item_uuid` is a **Text** column (not `PG_UUID`), so pass `str(item_uuid)` values in the `IN (...)` filter and key result dicts by that string.
- **No N+1.** Exactly one batched `SELECT ... WHERE report_item_uuid IN (...)` per table.
- **`decision_bucket` badge is row-level auxiliary only** — must not duplicate the ROB-308/322 section projection.
- **Test-DB discipline (bit ROB-711/713/705 three times):** seed rows with per-test unique `report_item_uuid = str(uuid4())`; `await db.flush()` only — never `commit()`; do not add an autouse global-DELETE fixture.
- **Lint gate:** `make lint` runs `ruff format --check` AND `ruff check`. Always run `ruff format` (not just `ruff check`) before committing, or CI goes red on unformatted-but-lint-clean code.

**Spec:** `docs/superpowers/specs/2026-07-05-rob-715-invest-item-loop-render-design.md`

---

### Task 1: Backend batch loaders + projection schema models

**Files:**
- Create: `app/services/investment_reports/item_loop_links.py`
- Modify: `app/schemas/investment_reports.py` (add two projection models near `LinkedOrderView`)
- Test: `tests/test_investment_reports_item_loop_links.py`

**Interfaces:**
- Produces:
  - `ForecastLinkResponse` (Pydantic): `forecast_id: str`, `status: str`, `outcome: bool | None`, `review_date: str | None`, `direction: str | None`, `target_price: float | None`, `probability: float`, `brier_score: float | None`, `resolution_source: str | None`.
  - `RetrospectiveLinkResponse` (Pydantic): `retrospective_id: int`, `outcome: str`, `lesson: str | None`, `result_summary: str | None`, `root_cause_class: str | None`, `trigger_type: str | None`, `pnl_pct: float | None`, `created_at: str | None`.
  - `async def list_forecasts_for_item_uuids(db: AsyncSession, item_uuids: Sequence[UUID]) -> dict[str, list[ForecastLinkResponse]]`
  - `async def list_retrospectives_for_item_uuids(db: AsyncSession, item_uuids: Sequence[UUID]) -> dict[str, list[RetrospectiveLinkResponse]]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_investment_reports_item_loop_links.py`:

```python
"""ROB-715 — exact-join batch loaders for item→forecast/retrospective links."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeForecast, TradeRetrospective
from app.services.investment_reports.item_loop_links import (
    list_forecasts_for_item_uuids,
    list_retrospectives_for_item_uuids,
)


def _forecast(item_uuid: str, **kw) -> TradeForecast:
    base = dict(
        created_by="claude",
        symbol="000660",
        instrument_type="equity_kr",
        forecast_target={"kind": "price_target", "direction": "at_or_above",
                         "target_price": 200000},
        probability=Decimal("0.6"),
        review_date=date(2026, 7, 20),
        status="open",
        outcome=None,
        report_item_uuid=item_uuid,
    )
    base.update(kw)
    return TradeForecast(**base)


def _retro(item_uuid: str, **kw) -> TradeRetrospective:
    base = dict(
        symbol="000660",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="loss",
        lesson="cut the position too late",
        report_item_uuid=item_uuid,
    )
    base.update(kw)
    return TradeRetrospective(**base)


@pytest.mark.asyncio
async def test_forecasts_grouped_by_item_uuid_exact_join(db_session: AsyncSession):
    item_a = uuid4()
    item_b = uuid4()
    db_session.add(_forecast(str(item_a), status="closed", outcome=True,
                             brier_score=Decimal("0.09")))
    db_session.add(_forecast(str(item_b)))
    await db_session.flush()

    result = await list_forecasts_for_item_uuids(db_session, [item_a, item_b])

    assert set(result) == {str(item_a), str(item_b)}
    a = result[str(item_a)][0]
    assert a.status == "closed"
    assert a.outcome is True
    assert a.direction == "at_or_above"
    assert a.target_price == 200000.0
    assert a.brier_score == pytest.approx(0.09)


@pytest.mark.asyncio
async def test_forecasts_absent_item_uuid_not_in_dict(db_session: AsyncSession):
    item_a = uuid4()
    unlinked = uuid4()
    db_session.add(_forecast(str(item_a)))
    await db_session.flush()

    result = await list_forecasts_for_item_uuids(db_session, [item_a, unlinked])

    assert str(item_a) in result
    assert str(unlinked) not in result


@pytest.mark.asyncio
async def test_retrospectives_grouped_by_item_uuid(db_session: AsyncSession):
    item_a = uuid4()
    db_session.add(_retro(str(item_a), pnl_pct=Decimal("-3.5"),
                          root_cause_class="thesis_wrong"))
    await db_session.flush()

    result = await list_retrospectives_for_item_uuids(db_session, [item_a])

    row = result[str(item_a)][0]
    assert row.outcome == "loss"
    assert row.lesson == "cut the position too late"
    assert row.pnl_pct == pytest.approx(-3.5)
    assert row.root_cause_class == "thesis_wrong"


@pytest.mark.asyncio
async def test_empty_input_returns_empty_dict(db_session: AsyncSession):
    assert await list_forecasts_for_item_uuids(db_session, []) == {}
    assert await list_retrospectives_for_item_uuids(db_session, []) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_investment_reports_item_loop_links.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.investment_reports.item_loop_links'`

- [ ] **Step 3: Add the projection schema models**

In `app/schemas/investment_reports.py`, near `LinkedOrderView`, add:

```python
class ForecastLinkResponse(BaseModel):
    """ROB-715 — an item's own forecast, projected for the audit surface."""

    forecast_id: str
    status: str
    outcome: bool | None = None
    review_date: str | None = None
    direction: str | None = None
    target_price: float | None = None
    probability: float
    brier_score: float | None = None
    resolution_source: str | None = None


class RetrospectiveLinkResponse(BaseModel):
    """ROB-715 — an item's own retrospective, projected for the audit surface."""

    retrospective_id: int
    outcome: str
    lesson: str | None = None
    result_summary: str | None = None
    root_cause_class: str | None = None
    trigger_type: str | None = None
    pnl_pct: float | None = None
    created_at: str | None = None
```

- [ ] **Step 4: Write the loader implementation**

Create `app/services/investment_reports/item_loop_links.py`:

```python
"""ROB-715 — item→forecast/retrospective exact-join batch loaders.

Given report-item UUIDs, return each item's own ``trade_forecasts`` and
``trade_retrospectives`` rows (exact join on the ``report_item_uuid`` Text
column), projected for the /invest audit surface. One batched query per table;
items with no rows are absent from the returned dict. Read-only — no broker,
order, watch, or order-intent mutation is reachable. This module deliberately
does NOT import ``app.services.decision_history`` (ROB-717 ownership).
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeForecast, TradeRetrospective
from app.schemas.investment_reports import (
    ForecastLinkResponse,
    RetrospectiveLinkResponse,
)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _project_forecast(row: TradeForecast) -> ForecastLinkResponse:
    target = row.forecast_target or {}
    price = target.get("target_price")
    return ForecastLinkResponse(
        forecast_id=str(row.forecast_id),
        status=row.status,
        outcome=row.outcome,
        review_date=_iso(row.review_date),
        direction=target.get("direction"),
        target_price=float(price) if price is not None else None,
        probability=float(row.probability),
        brier_score=(
            float(row.brier_score) if row.brier_score is not None else None
        ),
        resolution_source=row.resolution_source,
    )


def _project_retrospective(row: TradeRetrospective) -> RetrospectiveLinkResponse:
    return RetrospectiveLinkResponse(
        retrospective_id=row.id,
        outcome=row.outcome,
        lesson=row.lesson,
        result_summary=row.result_summary,
        root_cause_class=row.root_cause_class,
        trigger_type=row.trigger_type,
        pnl_pct=float(row.pnl_pct) if row.pnl_pct is not None else None,
        created_at=_iso(row.created_at),
    )


async def list_forecasts_for_item_uuids(
    db: AsyncSession, item_uuids: Sequence[UUID]
) -> dict[str, list[ForecastLinkResponse]]:
    keys = [str(u) for u in item_uuids]
    grouped: dict[str, list[ForecastLinkResponse]] = {}
    if not keys:
        return grouped
    rows = (
        (
            await db.execute(
                select(TradeForecast)
                .where(TradeForecast.report_item_uuid.in_(keys))
                .order_by(TradeForecast.id.desc())
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        grouped.setdefault(row.report_item_uuid, []).append(
            _project_forecast(row)
        )
    return grouped


async def list_retrospectives_for_item_uuids(
    db: AsyncSession, item_uuids: Sequence[UUID]
) -> dict[str, list[RetrospectiveLinkResponse]]:
    keys = [str(u) for u in item_uuids]
    grouped: dict[str, list[RetrospectiveLinkResponse]] = {}
    if not keys:
        return grouped
    rows = (
        (
            await db.execute(
                select(TradeRetrospective)
                .where(TradeRetrospective.report_item_uuid.in_(keys))
                .order_by(TradeRetrospective.id.desc())
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        grouped.setdefault(row.report_item_uuid, []).append(
            _project_retrospective(row)
        )
    return grouped
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_investment_reports_item_loop_links.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Lint + commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-715
uv run ruff format app/services/investment_reports/item_loop_links.py app/schemas/investment_reports.py tests/test_investment_reports_item_loop_links.py
uv run ruff check app/services/investment_reports/item_loop_links.py app/schemas/investment_reports.py
git add app/services/investment_reports/item_loop_links.py app/schemas/investment_reports.py tests/test_investment_reports_item_loop_links.py
git commit -m "feat(ROB-715): item->forecast/retrospective exact-join batch loaders"
```

---

### Task 2: Wire loaders into the bundle query service

**Files:**
- Modify: `app/services/investment_reports/query_service.py` (the `get_bundle` builder, around lines 152-178)
- Test: `tests/test_investment_reports_query_service.py` (add one test)

**Interfaces:**
- Consumes: `list_forecasts_for_item_uuids`, `list_retrospectives_for_item_uuids` from Task 1.
- Produces: bundle dict gains keys `forecasts_by_item_uuid` and `retrospectives_by_item_uuid` (`dict[str, list[...Response]]`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_investment_reports_query_service.py` (reuse the file's existing `session` fixture and its report/item seeding helpers — grep the file for how `test_get_bundle_returns_nested_shapes` seeds a report + item, and copy that seeding into this test):

```python
@pytest.mark.asyncio
async def test_get_bundle_attaches_forecast_and_retrospective_maps(
    session: AsyncSession,
) -> None:
    from decimal import Decimal
    from datetime import date
    from app.models.review import TradeForecast, TradeRetrospective

    # Seed one report with one item (mirror test_get_bundle_returns_nested_shapes).
    report, item = await _seed_single_item_report(session)  # existing helper pattern

    session.add(
        TradeForecast(
            created_by="claude", symbol=item.symbol, instrument_type="equity_kr",
            forecast_target={"direction": "at_or_above", "target_price": 100},
            probability=Decimal("0.6"), review_date=date(2026, 7, 20),
            status="open", report_item_uuid=str(item.item_uuid),
        )
    )
    session.add(
        TradeRetrospective(
            symbol=item.symbol, instrument_type="equity_kr",
            account_mode="kis_live", outcome="win",
            report_item_uuid=str(item.item_uuid),
        )
    )
    await session.flush()

    svc = InvestmentReportQueryService(session)
    bundle = await svc.get_bundle(report.report_uuid)

    key = str(item.item_uuid)
    assert bundle["forecasts_by_item_uuid"][key][0].status == "open"
    assert bundle["retrospectives_by_item_uuid"][key][0].outcome == "win"
```

> If `_seed_single_item_report` does not already exist in the test file, inline the same report+item insert that `test_get_bundle_returns_nested_shapes` uses. Use `report_item_uuid=str(item.item_uuid)` so the exact join matches.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_investment_reports_query_service.py::test_get_bundle_attaches_forecast_and_retrospective_maps -v`
Expected: FAIL with `KeyError: 'forecasts_by_item_uuid'`

- [ ] **Step 3: Wire the loaders**

In `app/services/investment_reports/query_service.py`, add imports at the top with the other service imports:

```python
from app.services.investment_reports.item_loop_links import (
    list_forecasts_for_item_uuids,
    list_retrospectives_for_item_uuids,
)
```

In `get_bundle`, right after the `linked_orders_by_item_uuid = await list_linked_orders_for_item_uuids(...)` call, add:

```python
        item_uuids = [it.item_uuid for it in items]
        forecasts_by_item_uuid = await list_forecasts_for_item_uuids(
            self._session, item_uuids
        )
        retrospectives_by_item_uuid = await list_retrospectives_for_item_uuids(
            self._session, item_uuids
        )
```

In the `return {...}` dict at the end of `get_bundle`, add two keys:

```python
            "forecasts_by_item_uuid": forecasts_by_item_uuid,
            "retrospectives_by_item_uuid": retrospectives_by_item_uuid,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_investment_reports_query_service.py::test_get_bundle_attaches_forecast_and_retrospective_maps -v`
Expected: PASS

- [ ] **Step 5: Run the whole query-service file (no regression)**

Run: `uv run pytest tests/test_investment_reports_query_service.py -v`
Expected: all PASS

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff format app/services/investment_reports/query_service.py tests/test_investment_reports_query_service.py
uv run ruff check app/services/investment_reports/query_service.py
git add app/services/investment_reports/query_service.py tests/test_investment_reports_query_service.py
git commit -m "feat(ROB-715): attach forecast/retrospective maps to report bundle"
```

---

### Task 3: Expose the maps on the bundle response + router folding

**Files:**
- Modify: `app/schemas/investment_reports.py` (`InvestmentReportBundle`, ~line 1076-1090)
- Modify: `app/routers/investment_reports.py` (`_serialise_bundle`, lines 56-115)
- Test: `tests/test_investment_reports_schemas.py` OR a new `tests/test_investment_reports_bundle_loop_maps.py`

**Interfaces:**
- Consumes: bundle dict keys from Task 2; `ForecastLinkResponse` / `RetrospectiveLinkResponse` from Task 1.
- Produces: `InvestmentReportBundle.forecasts_by_item_uuid` and `.retrospectives_by_item_uuid` (`dict[str, list[...]]`, default empty).

- [ ] **Step 1: Write the failing test**

Create `tests/test_investment_reports_bundle_loop_maps.py`:

```python
"""ROB-715 — _serialise_bundle folds forecast/retrospective maps onto response."""

from __future__ import annotations

import pytest

from app.routers.investment_reports import _serialise_bundle
from app.schemas.investment_reports import (
    ForecastLinkResponse,
    RetrospectiveLinkResponse,
)


def test_serialise_bundle_carries_loop_maps(minimal_bundle_dict):
    # minimal_bundle_dict: a dict shaped like query_service.get_bundle output
    # with one report + one item (item_uuid == UUID(int=1)). See fixture below.
    key = str(minimal_bundle_dict["items"][0].item_uuid)
    minimal_bundle_dict["forecasts_by_item_uuid"] = {
        key: [ForecastLinkResponse(forecast_id="f1", status="open", probability=0.6)]
    }
    minimal_bundle_dict["retrospectives_by_item_uuid"] = {
        key: [RetrospectiveLinkResponse(retrospective_id=1, outcome="win")]
    }

    bundle = _serialise_bundle(minimal_bundle_dict)

    assert bundle.forecasts_by_item_uuid[key][0].status == "open"
    assert bundle.retrospectives_by_item_uuid[key][0].outcome == "win"


def test_serialise_bundle_defaults_empty_when_maps_absent(minimal_bundle_dict):
    # Legacy bundle dict without the new keys → empty dicts, no crash.
    bundle = _serialise_bundle(minimal_bundle_dict)
    assert bundle.forecasts_by_item_uuid == {}
    assert bundle.retrospectives_by_item_uuid == {}
```

> Build `minimal_bundle_dict` as a local pytest fixture in this file: reuse the seeding from `tests/test_investment_reports_query_service.py::test_get_bundle_returns_nested_shapes`, calling `InvestmentReportQueryService(session).get_bundle(...)` to get a real dict, then strip the two new keys for the "absent" test. If constructing a real dict is heavy, instead assemble the minimum keys `_serialise_bundle` reads: `report`, `items`, `decisions_by_item`, `alerts`, `events`, `news_citations`, `linked_orders_by_item_uuid` (see `_serialise_bundle` body, investment_reports.py:56-115).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_investment_reports_bundle_loop_maps.py -v`
Expected: FAIL — `AttributeError: 'InvestmentReportBundle' object has no attribute 'forecasts_by_item_uuid'`

- [ ] **Step 3: Add schema fields**

In `app/schemas/investment_reports.py`, in `class InvestmentReportBundle`, after the `decisions_by_item_uuid` field, add:

```python
    forecasts_by_item_uuid: dict[str, list[ForecastLinkResponse]] = Field(
        default_factory=dict
    )
    retrospectives_by_item_uuid: dict[str, list[RetrospectiveLinkResponse]] = Field(
        default_factory=dict
    )
```

(Ensure `Field` is imported — it already is in this module.)

- [ ] **Step 4: Fold maps in the router**

In `app/routers/investment_reports.py::_serialise_bundle`, read the two maps from the bundle dict (default empty) and pass them to the `InvestmentReportBundle(...)` constructor:

```python
    forecasts_by_item_uuid = bundle.get("forecasts_by_item_uuid", {})
    retrospectives_by_item_uuid = bundle.get("retrospectives_by_item_uuid", {})
```

Add to the `return InvestmentReportBundle(` call:

```python
        forecasts_by_item_uuid=forecasts_by_item_uuid,
        retrospectives_by_item_uuid=retrospectives_by_item_uuid,
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_investment_reports_bundle_loop_maps.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff format app/schemas/investment_reports.py app/routers/investment_reports.py tests/test_investment_reports_bundle_loop_maps.py
uv run ruff check app/schemas/investment_reports.py app/routers/investment_reports.py
git add app/schemas/investment_reports.py app/routers/investment_reports.py tests/test_investment_reports_bundle_loop_maps.py
git commit -m "feat(ROB-715): expose forecast/retrospective loop maps on bundle response"
```

---

### Task 4: `structured_evidence_summary` helper + item-response field

**Files:**
- Create: `app/services/investment_reports/structured_evidence_summary.py`
- Modify: `app/schemas/investment_reports.py` (`InvestmentReportItemResponse`, ~line 807-849)
- Modify: `app/routers/investment_reports.py` (`_serialise_bundle`, item loop lines 61-65)
- Test: `tests/test_structured_evidence_summary.py`

**Interfaces:**
- Produces: `def summarize_structured_evidence(evidence_snapshot: dict) -> str | None`; new field `InvestmentReportItemResponse.structured_evidence_summary: str | None`.

**Context:** `structured_evidence` is persisted at `evidence_snapshot["structured_evidence"]` (schema comment, investment_reports.py:241-242, 326-327) — there is no separate column. The summary is derived on the backend so the frontend renders a string without parsing the nested structure ("백엔드 전용 — 프론트 미파싱").

- [ ] **Step 1: Write the failing test**

Create `tests/test_structured_evidence_summary.py`:

```python
"""ROB-715 — deterministic one-line summary of evidence_snapshot.structured_evidence."""

from app.services.investment_reports.structured_evidence_summary import (
    summarize_structured_evidence,
)


def test_none_when_no_structured_evidence():
    assert summarize_structured_evidence({}) is None
    assert summarize_structured_evidence({"structured_evidence": None}) is None
    assert summarize_structured_evidence({"structured_evidence": {}}) is None


def test_summarizes_top_level_keys():
    snap = {"structured_evidence": {"valuation": "cheap", "momentum": "up",
                                    "risk": "low"}}
    out = summarize_structured_evidence(snap)
    assert out is not None
    # Deterministic: sorted keys, count-prefixed.
    assert "3" in out
    assert "momentum" in out and "risk" in out and "valuation" in out


def test_stable_ordering():
    snap = {"structured_evidence": {"b": 1, "a": 2}}
    assert summarize_structured_evidence(snap) == summarize_structured_evidence(
        {"structured_evidence": {"a": 2, "b": 1}}
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_structured_evidence_summary.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the helper**

Create `app/services/investment_reports/structured_evidence_summary.py`:

```python
"""ROB-715 — backend-derived summary of ``evidence_snapshot['structured_evidence']``.

The frontend renders the returned string verbatim (no client-side parsing of the
nested structure). Deterministic: keys are sorted so the output is stable across
requests and safe to snapshot-test.
"""

from __future__ import annotations

from typing import Any


def summarize_structured_evidence(evidence_snapshot: dict[str, Any]) -> str | None:
    se = (evidence_snapshot or {}).get("structured_evidence")
    if not isinstance(se, dict) or not se:
        return None
    keys = sorted(str(k) for k in se)
    return f"{len(keys)} evidence fields: " + ", ".join(keys)
```

- [ ] **Step 4: Run helper test to verify it passes**

Run: `uv run pytest tests/test_structured_evidence_summary.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Add the item-response field + wire it in the router**

In `app/schemas/investment_reports.py`, in `class InvestmentReportItemResponse`, add (near `linked_orders`):

```python
    structured_evidence_summary: str | None = None
```

In `app/routers/investment_reports.py::_serialise_bundle`, in the item loop that already sets `resp.linked_orders`, add the summary derivation. Add the import at the top of the file:

```python
from app.services.investment_reports.structured_evidence_summary import (
    summarize_structured_evidence,
)
```

Then inside the `for it in items:` loop:

```python
        resp.structured_evidence_summary = summarize_structured_evidence(
            resp.evidence_snapshot
        )
```

- [ ] **Step 6: Write a wiring test**

Add to `tests/test_investment_reports_bundle_loop_maps.py`:

```python
def test_serialise_bundle_sets_structured_evidence_summary(minimal_bundle_dict):
    item = minimal_bundle_dict["items"][0]
    item.evidence_snapshot = {"structured_evidence": {"valuation": "cheap"}}
    bundle = _serialise_bundle(minimal_bundle_dict)
    assert bundle.items[0].structured_evidence_summary == "1 evidence fields: valuation"
```

> If the ORM `item.evidence_snapshot` is read-only in your seeding path, set the summary expectation against whatever the seeded item's `evidence_snapshot` already contains, or seed the item with `evidence_snapshot={"structured_evidence": {...}}` up front.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_structured_evidence_summary.py tests/test_investment_reports_bundle_loop_maps.py -v`
Expected: all PASS

- [ ] **Step 8: Lint + commit**

```bash
uv run ruff format app/services/investment_reports/structured_evidence_summary.py app/schemas/investment_reports.py app/routers/investment_reports.py tests/test_structured_evidence_summary.py tests/test_investment_reports_bundle_loop_maps.py
uv run ruff check app/services/investment_reports/structured_evidence_summary.py app/schemas/investment_reports.py app/routers/investment_reports.py
git add app/services/investment_reports/structured_evidence_summary.py app/schemas/investment_reports.py app/routers/investment_reports.py tests/test_structured_evidence_summary.py tests/test_investment_reports_bundle_loop_maps.py
git commit -m "feat(ROB-715): backend structured_evidence summary on report items"
```

---

### Task 5: Frontend types + normalizer for the loop maps

**Files:**
- Modify: `frontend/invest/src/types/investmentReports.ts`
- Modify: `frontend/invest/src/api/investmentReports.ts`
- Test: `frontend/invest/src/__tests__/investmentReports.loopMaps.test.ts` (create)

**Interfaces:**
- Produces TS types `ForecastLink`, `RetrospectiveLink`; `InvestmentReportBundle.forecastsByItemUuid` / `.retrospectivesByItemUuid`; `InvestmentReportItem.structuredEvidenceSummary`.

- [ ] **Step 1: Write the failing test**

Create `frontend/invest/src/__tests__/investmentReports.loopMaps.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import { normalizeForecastLink, normalizeRetrospectiveLink } from "../api/investmentReports";

describe("ROB-715 loop-map normalizers", () => {
  it("normalizes a forecast link snake→camel", () => {
    const out = normalizeForecastLink({
      forecast_id: "f1", status: "closed", outcome: true,
      review_date: "2026-07-20", direction: "at_or_above",
      target_price: 200000, probability: 0.6, brier_score: 0.09,
      resolution_source: "ohlcv_day",
    });
    expect(out.forecastId).toBe("f1");
    expect(out.status).toBe("closed");
    expect(out.outcome).toBe(true);
    expect(out.targetPrice).toBe(200000);
    expect(out.brierScore).toBe(0.09);
  });

  it("normalizes a retrospective link", () => {
    const out = normalizeRetrospectiveLink({
      retrospective_id: 1, outcome: "loss", lesson: "cut late",
      root_cause_class: "thesis_wrong", pnl_pct: -3.5,
    });
    expect(out.outcome).toBe("loss");
    expect(out.lesson).toBe("cut late");
    expect(out.pnlPct).toBe(-3.5);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend/invest && npx vitest run src/__tests__/investmentReports.loopMaps.test.ts`
Expected: FAIL — `normalizeForecastLink is not exported`

- [ ] **Step 3: Add the TS types**

In `frontend/invest/src/types/investmentReports.ts`, add:

```typescript
export interface ForecastLink {
  forecastId: string;
  status: string;
  outcome: boolean | null;
  reviewDate: string | null;
  direction: string | null;
  targetPrice: number | null;
  probability: number;
  brierScore: number | null;
  resolutionSource: string | null;
}

export interface RetrospectiveLink {
  retrospectiveId: number;
  outcome: string;
  lesson: string | null;
  resultSummary: string | null;
  rootCauseClass: string | null;
  triggerType: string | null;
  pnlPct: number | null;
  createdAt: string | null;
}
```

Add to `interface InvestmentReportBundle`:

```typescript
  forecastsByItemUuid: Record<string, ForecastLink[]>;
  retrospectivesByItemUuid: Record<string, RetrospectiveLink[]>;
```

Add to `interface InvestmentReportItem` (near `linkedOrders`):

```typescript
  structuredEvidenceSummary?: string | null;
```

- [ ] **Step 4: Add the normalizers + wire into the bundle reader**

In `frontend/invest/src/api/investmentReports.ts`, add exported normalizers (use the same `asOptionalString` / `asArray` helpers the file already uses; grep for their definitions):

```typescript
export function normalizeForecastLink(raw: ApiItem): ForecastLink {
  return {
    forecastId: String(raw.forecast_id ?? ""),
    status: String(raw.status ?? ""),
    outcome: raw.outcome === null || raw.outcome === undefined ? null : Boolean(raw.outcome),
    reviewDate: asOptionalString(raw.review_date) ?? null,
    direction: asOptionalString(raw.direction) ?? null,
    targetPrice: raw.target_price == null ? null : Number(raw.target_price),
    probability: Number(raw.probability ?? 0),
    brierScore: raw.brier_score == null ? null : Number(raw.brier_score),
    resolutionSource: asOptionalString(raw.resolution_source) ?? null,
  };
}

export function normalizeRetrospectiveLink(raw: ApiItem): RetrospectiveLink {
  return {
    retrospectiveId: Number(raw.retrospective_id ?? 0),
    outcome: String(raw.outcome ?? ""),
    lesson: asOptionalString(raw.lesson) ?? null,
    resultSummary: asOptionalString(raw.result_summary) ?? null,
    rootCauseClass: asOptionalString(raw.root_cause_class) ?? null,
    triggerType: asOptionalString(raw.trigger_type) ?? null,
    pnlPct: raw.pnl_pct == null ? null : Number(raw.pnl_pct),
    createdAt: asOptionalString(raw.created_at) ?? null,
  };
}
```

In the bundle reader (where `decisions_by_item_uuid` is normalized, ~line 398-412), add parsing of the two new maps and `structured_evidence_summary` on each item, and include `forecastsByItemUuid` / `retrospectivesByItemUuid` in the returned bundle object. Mirror the existing `decisionsByItemUuid` loop:

```typescript
  const forecastsRaw = raw.forecasts_by_item_uuid ?? {};
  const forecastsByItemUuid: Record<string, ForecastLink[]> = {};
  for (const [k, v] of Object.entries(forecastsRaw)) {
    forecastsByItemUuid[k] = asArray<ApiItem>(v).map(normalizeForecastLink);
  }
  const retrospectivesRaw = raw.retrospectives_by_item_uuid ?? {};
  const retrospectivesByItemUuid: Record<string, RetrospectiveLink[]> = {};
  for (const [k, v] of Object.entries(retrospectivesRaw)) {
    retrospectivesByItemUuid[k] = asArray<ApiItem>(v).map(normalizeRetrospectiveLink);
  }
```

Also add `structuredEvidenceSummary: asOptionalString(raw.structured_evidence_summary) ?? null,` to the item normalizer (near the `decisionBucket` line, ~208), and add the two maps to the bundle-type read struct (the inline `readJson<{...}>` type near line 398) and to the returned bundle literal.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd frontend/invest && npx vitest run src/__tests__/investmentReports.loopMaps.test.ts`
Expected: PASS

- [ ] **Step 6: Typecheck + commit**

```bash
cd frontend/invest && npx tsc --noEmit
cd /Users/mgh3326/work/auto_trader.rob-715
git add frontend/invest/src/types/investmentReports.ts frontend/invest/src/api/investmentReports.ts frontend/invest/src/__tests__/investmentReports.loopMaps.test.ts
git commit -m "feat(ROB-715): frontend types + normalizers for item loop maps"
```

---

### Task 6: Render the loop section (a) + empty state on `ItemRow`

**Files:**
- Modify: `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx` (`ItemRow`, def at line 276; call sites 629/647/791 must pass the maps down)
- Test: `frontend/invest/src/__tests__/InvestmentReportBundleContent.loopSection.test.tsx` (create)

**Interfaces:**
- Consumes: `ForecastLink[]` / `RetrospectiveLink[]` for the item, from Task 5 types. `ItemRow` gains props `forecastLinks?: ForecastLink[]` and `retrospectiveLinks?: RetrospectiveLink[]` (looked up by the parent from `bundle.forecastsByItemUuid[item.itemUuid]`).

- [ ] **Step 1: Write the failing test**

Create `frontend/invest/src/__tests__/InvestmentReportBundleContent.loopSection.test.tsx`. Model it on the existing `InvestmentReportBundleContent.linkedOrders.test.tsx` (copy its render harness + bundle-building helper):

```tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { InvestmentReportBundleContent } from "../components/investment-reports/InvestmentReportBundleContent";
import { buildBundleFixture } from "./InvestmentReportBundleContent.linkedOrders.test"; // or inline a local helper

describe("ROB-715 item loop section", () => {
  it("renders forecast status and retrospective outcome for an item", () => {
    const bundle = buildBundleFixture(); // one item with itemUuid "u1"
    bundle.forecastsByItemUuid = {
      u1: [{ forecastId: "f1", status: "closed", outcome: true, reviewDate: "2026-07-20",
             direction: "at_or_above", targetPrice: 200000, probability: 0.6,
             brierScore: 0.09, resolutionSource: "ohlcv_day" }],
    };
    bundle.retrospectivesByItemUuid = {
      u1: [{ retrospectiveId: 1, outcome: "win", lesson: "held to target",
             resultSummary: null, rootCauseClass: null, triggerType: null,
             pnlPct: 4.2, createdAt: null }],
    };
    render(<InvestmentReportBundleContent bundle={bundle} />);
    expect(screen.getByTestId("item-loop-forecast-f1")).toBeInTheDocument();
    expect(screen.getByText(/held to target/)).toBeInTheDocument();
  });

  it("renders the empty state when an item has no forecast or retrospective", () => {
    const bundle = buildBundleFixture();
    bundle.forecastsByItemUuid = {};
    bundle.retrospectivesByItemUuid = {};
    render(<InvestmentReportBundleContent bundle={bundle} />);
    expect(screen.getByText("해소 대기 / 미연결")).toBeInTheDocument();
  });
});
```

> If `buildBundleFixture` is not exported by the linkedOrders test, inline a minimal bundle builder in this file (copy the shape the linkedOrders test constructs). Ensure the single item's `itemUuid` is `"u1"`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend/invest && npx vitest run src/__tests__/InvestmentReportBundleContent.loopSection.test.tsx`
Expected: FAIL — testid `item-loop-forecast-f1` not found / empty-state text absent.

- [ ] **Step 3: Thread the maps to `ItemRow` and render the section**

In `InvestmentReportBundleContent.tsx`, at each `ItemRow` call site (lines ~629, 647, 791), pass:

```tsx
  forecastLinks={bundle.forecastsByItemUuid[it.itemUuid] ?? []}
  retrospectiveLinks={bundle.retrospectivesByItemUuid[it.itemUuid] ?? []}
```

Add the two props to `ItemRow`'s prop type. Inside `ItemRow`, after the `linkedOrders` block (~line 386), add:

```tsx
      <div className="item-loop-section" data-testid={`item-loop-${item.itemUuid}`}>
        {forecastLinks.length === 0 && retrospectiveLinks.length === 0 ? (
          <span className="item-loop-empty muted">해소 대기 / 미연결</span>
        ) : (
          <>
            {forecastLinks.map((f) => (
              <div key={f.forecastId} data-testid={`item-loop-forecast-${f.forecastId}`}>
                <span>{f.status === "closed"
                  ? (f.outcome ? "적중" : "빗나감")
                  : "해소 대기"}</span>
                {f.brierScore != null && <span> · Brier {f.brierScore.toFixed(2)}</span>}
                {f.reviewDate && <span> · {f.reviewDate}</span>}
              </div>
            ))}
            {retrospectiveLinks.map((r) => (
              <div key={r.retrospectiveId} data-testid={`item-loop-retro-${r.retrospectiveId}`}>
                <span>회고: {r.outcome}</span>
                {r.lesson && <span> — {r.lesson}</span>}
              </div>
            ))}
          </>
        )}
      </div>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend/invest && npx vitest run src/__tests__/InvestmentReportBundleContent.loopSection.test.tsx`
Expected: PASS (2 tests)

- [ ] **Step 5: Typecheck + commit**

```bash
cd frontend/invest && npx tsc --noEmit
cd /Users/mgh3326/work/auto_trader.rob-715
git add frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx frontend/invest/src/__tests__/InvestmentReportBundleContent.loopSection.test.tsx
git commit -m "feat(ROB-715): render item forecast-resolution + retrospective loop section"
```

---

### Task 7: Render raw fields (b) on `ItemRow`

**Files:**
- Modify: `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx` (`ItemRow`)
- Test: `frontend/invest/src/__tests__/InvestmentReportBundleContent.rawFields.test.tsx` (create)

**Interfaces:**
- Consumes: `item.triggerChecklist`, `item.maxAction`, `item.decisionBucket`, `item.structuredEvidenceSummary` (all already normalized; `structuredEvidenceSummary` from Task 5).

- [ ] **Step 1: Write the failing test**

Create `frontend/invest/src/__tests__/InvestmentReportBundleContent.rawFields.test.tsx` (same harness as Task 6):

```tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { InvestmentReportBundleContent } from "../components/investment-reports/InvestmentReportBundleContent";
import { buildBundleFixture } from "./InvestmentReportBundleContent.linkedOrders.test";

describe("ROB-715 raw item fields", () => {
  it("renders trigger checklist, max_action, decision bucket badge, evidence summary", () => {
    const bundle = buildBundleFixture();
    const item = bundle.items[0];
    item.triggerChecklist = ["RSI < 30", "종가 20MA 회복"];
    item.maxAction = { side: "buy", notional: 1000000, limit_price: 195000 };
    item.decisionBucket = "new_buy_candidate";
    item.structuredEvidenceSummary = "2 evidence fields: momentum, valuation";
    render(<InvestmentReportBundleContent bundle={bundle} />);
    expect(screen.getByText("RSI < 30")).toBeInTheDocument();
    expect(screen.getByTestId("item-decision-bucket-badge")).toHaveTextContent("new_buy_candidate");
    expect(screen.getByText(/2 evidence fields/)).toBeInTheDocument();
    expect(screen.getByTestId("item-max-action")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend/invest && npx vitest run src/__tests__/InvestmentReportBundleContent.rawFields.test.tsx`
Expected: FAIL — elements not found.

- [ ] **Step 3: Render the raw fields**

Inside `ItemRow`, add a raw-fields block (after the rationale, before the loop section). Keep `decision_bucket` as a small inline badge — do NOT re-project the ROB-308/322 sections:

```tsx
      {item.decisionBucket && (
        <span className="item-decision-bucket-badge" data-testid="item-decision-bucket-badge">
          {item.decisionBucket}
        </span>
      )}
      {item.triggerChecklist && item.triggerChecklist.length > 0 && (
        <ul className="item-trigger-checklist">
          {item.triggerChecklist.map((t, i) => <li key={i}>{t}</li>)}
        </ul>
      )}
      {item.maxAction && (
        <div className="item-max-action" data-testid="item-max-action">
          {formatMaxAction(item.maxAction)}
        </div>
      )}
      {item.structuredEvidenceSummary && (
        <div className="item-structured-evidence">{item.structuredEvidenceSummary}</div>
      )}
```

Add a small local formatter near the top of the file (module scope):

```tsx
function formatMaxAction(a: Record<string, unknown>): string {
  const parts: string[] = [];
  if (a.side) parts.push(String(a.side));
  if (a.quantity != null) parts.push(`${a.quantity}주`);
  if (a.notional != null) parts.push(`${a.notional}`);
  if (a.limit_price != null) parts.push(`@${a.limit_price}`);
  if (a.ladder_level != null) parts.push(`ladder ${a.ladder_level}`);
  return parts.join(" · ");
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend/invest && npx vitest run src/__tests__/InvestmentReportBundleContent.rawFields.test.tsx`
Expected: PASS

- [ ] **Step 5: Typecheck + commit**

```bash
cd frontend/invest && npx tsc --noEmit
cd /Users/mgh3326/work/auto_trader.rob-715
git add frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx frontend/invest/src/__tests__/InvestmentReportBundleContent.rawFields.test.tsx
git commit -m "feat(ROB-715): render trigger_checklist/max_action/decision_bucket/evidence summary"
```

---

### Task 8: Render plan-vs-actual (c) on `ItemRow`

**Files:**
- Modify: `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx` (`ItemRow` — extend the existing `tradeSetup` / R:R block, `parseTradeSetup` at line 224, R:R render ~289)
- Test: `frontend/invest/src/__tests__/InvestmentReportBundleContent.planVsActual.test.tsx` (create)

**Interfaces:**
- Consumes: parsed `tradeSetup` (planned entry/stop/target — already parsed from `evidenceSnapshot`) + actual fill price from `item.linkedOrders` (already normalized). Frontend-only; no new data.

- [ ] **Step 1: Write the failing test**

Create `frontend/invest/src/__tests__/InvestmentReportBundleContent.planVsActual.test.tsx` (same harness; model on `InvestmentReportBundleContent.tradeSetup.test.tsx` for how tradeSetup is seeded into `evidenceSnapshot`):

```tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { InvestmentReportBundleContent } from "../components/investment-reports/InvestmentReportBundleContent";
import { buildBundleFixture } from "./InvestmentReportBundleContent.linkedOrders.test";

describe("ROB-715 plan vs actual", () => {
  it("shows planned entry alongside the actual fill price", () => {
    const bundle = buildBundleFixture();
    const item = bundle.items[0];
    item.evidenceSnapshot = { trade_setup: { entry: 195000, stop: 185000, target: 220000 } };
    item.linkedOrders = [
      { /* shape per normalizeLinkedOrder */ fillPrice: 194500, side: "buy", status: "filled" } as any,
    ];
    render(<InvestmentReportBundleContent bundle={bundle} />);
    const pva = screen.getByTestId("item-plan-vs-actual");
    expect(pva).toHaveTextContent("195000"); // planned entry
    expect(pva).toHaveTextContent("194500"); // actual fill
  });
});
```

> Confirm the actual `LinkedOrder` fill-price field name in `types/investmentReports.ts` (grep `fillPrice` / `filled`) and use it verbatim in both the render and this test.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend/invest && npx vitest run src/__tests__/InvestmentReportBundleContent.planVsActual.test.tsx`
Expected: FAIL — testid `item-plan-vs-actual` not found.

- [ ] **Step 3: Render plan-vs-actual**

Inside `ItemRow`, where `tradeSetup` is already rendered (the R:R pill block ~289), add a plan-vs-actual row that juxtaposes the planned levels with the first filled linked order's price:

```tsx
      {tradeSetup && (
        <div className="item-plan-vs-actual" data-testid="item-plan-vs-actual">
          <span>계획: 진입 {tradeSetup.entry} · 손절 {tradeSetup.stop} · 목표 {tradeSetup.target}</span>
          {(() => {
            const filled = (item.linkedOrders ?? []).find((o) => o.fillPrice != null);
            return filled ? <span> · 실제 체결 {filled.fillPrice}</span>
                          : <span className="muted"> · 체결 없음</span>;
          })()}
        </div>
      )}
```

> Adjust `tradeSetup.entry/stop/target` and `o.fillPrice` to the exact property names in the file (grep `parseTradeSetup` return shape and `LinkedOrder`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend/invest && npx vitest run src/__tests__/InvestmentReportBundleContent.planVsActual.test.tsx`
Expected: PASS

- [ ] **Step 5: Run all touched frontend tests (no regression)**

Run: `cd frontend/invest && npx vitest run src/__tests__/InvestmentReportBundleContent`
Expected: all PASS (loopSection, rawFields, planVsActual + existing linkedOrders/tradeSetup/etc.)

- [ ] **Step 6: Typecheck + commit**

```bash
cd frontend/invest && npx tsc --noEmit
cd /Users/mgh3326/work/auto_trader.rob-715
git add frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx frontend/invest/src/__tests__/InvestmentReportBundleContent.planVsActual.test.tsx
git commit -m "feat(ROB-715): render plan-vs-actual (planned setup vs actual fill)"
```

---

### Task 9: Full-suite regression + guard verification

**Files:** none (verification only).

- [ ] **Step 1: Backend — run touched + guard suites**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-715
uv run pytest tests/test_investment_reports_item_loop_links.py tests/test_investment_reports_query_service.py tests/test_investment_reports_bundle_loop_maps.py tests/test_structured_evidence_summary.py tests/test_investment_reports_schemas.py -v
uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -v
```
Expected: all PASS (ROB-501 guard green — no new LLM import).

- [ ] **Step 2: Backend — lint gate (format-check, not just check)**

Run: `make lint`
Expected: clean (0 diffs). If `ruff format --check` reports a diff, run `uv run ruff format app/ tests/` and re-commit.

- [ ] **Step 3: Frontend — full invest suite + typecheck**

Run:
```bash
cd frontend/invest
npx vitest run
npx tsc --noEmit
```
Expected: no new failures. (Pre-existing calendar/coverage vitest failures noted in prior batches are not introduced by this work — confirm the failing set is unchanged from `origin/main`.)

- [ ] **Step 4: Manual smoke via `/browse` (optional but recommended)**

Load `/invest` reports, open a report bundle, confirm an item row shows the loop section (or the "해소 대기 / 미연결" empty state), the raw-field chips, and the plan-vs-actual line. Real DB is read-only.

- [ ] **Step 5: Final commit if any format fixups**

```bash
git add -A
git commit -m "chore(ROB-715): lint/format fixups" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage:**
- (a) forecast-resolution + retrospective link → Tasks 1–3 (backend maps) + Task 6 (render). ✓
- (b) raw fields (trigger_checklist, max_action, structured_evidence summary, decision_bucket badge) → Task 4 (summary backend) + Task 7 (render). ✓
- (c) plan-vs-actual → Task 8. ✓
- (d) analysis_artifacts → explicitly deferred (spec D1); no task. ✓
- Batch maps on bundle (D2) → Tasks 2–3. ✓
- Exact join + "해소 대기 / 미연결" empty state (D3) → Task 1 (exact `report_item_uuid`), Task 6 (empty state). ✓
- Migration 0 / read-only / ROB-501 / no decision_history edits → Global Constraints + Task 9 guard run. ✓
- Test-DB xdist discipline (unique uuid + flush-only) → Global Constraints + Task 1 tests. ✓

**Type consistency:** `ForecastLinkResponse`/`RetrospectiveLinkResponse` (backend) ↔ `ForecastLink`/`RetrospectiveLink` (frontend) field names align (snake↔camel). `list_forecasts_for_item_uuids` / `list_retrospectives_for_item_uuids` used identically in Tasks 1–2. Bundle keys `forecasts_by_item_uuid` / `retrospectives_by_item_uuid` consistent across Tasks 2–3–5. `structured_evidence_summary` ↔ `structuredEvidenceSummary` consistent across Tasks 4–5–7. `summarize_structured_evidence` used identically in Task 4.

**Placeholder scan:** No TBD/TODO. Every code step shows the code. Fixture-shape assumptions (`buildBundleFixture`, `_seed_single_item_report`) are flagged with explicit fallback instructions rather than left implicit, because the exact test-harness helper names must be confirmed against the existing files at implementation time.
