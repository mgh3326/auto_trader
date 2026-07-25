# ROB-554 — Order Provenance + Fill Status on /invest Decision Log — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the `/invest` decision-log (report bundle) screen, show each report item's linked live orders + their fill status, by wiring the existing ROB-473 `report_item_uuid` link into a read-back path consumed by the shared bundle serializer.

**Architecture:** A new services-layer reverse-lookup (`linked_orders.py`) queries both live ledgers (`LiveOrderLedger` US/crypto + `KISLiveOrderLedger` KR) by `report_item_uuid`, projecting each row into a unified `LinkedOrderView` with the reconcile-written fill rollup. `get_bundle()` attaches the result; both `_serialise_bundle` paths (web router + MCP handler) set it on each item response — so the web screen *and* the MCP `investment_report_get` tool gain `linked_orders[]` for free. The frontend renders an order/fill card in the existing item row. **Migrations: 0** — every column already exists.

**Tech Stack:** Python 3.13 / SQLAlchemy async / Pydantic v2 / FastAPI · React + TypeScript + Vitest · pytest.

**Spec:** `docs/superpowers/specs/2026-06-14-rob-554-order-provenance-design.md`

**Decisions (locked):** surface = decision-log bundle (not fills page) · scope = all live markets (US/crypto + KR) · fill detail = ledger-row rollup (no `execution_ledger` join) · backfill = forward-only · packaging = single PR, 3 commits (S1 / S2 / S3) · order-level `exit_reason`/`thesis` rendered on the card as a secondary line.

---

## File Structure

**Backend (S1, S2):**
- Modify `app/mcp_server/tooling/orders_registration.py` — document `report_item_uuid` in the generic `place_order` tool description (S1).
- Modify `app/schemas/investment_reports.py` — add `LinkedOrderView`; add `linked_orders` field to `InvestmentReportItemResponse` (S2).
- Create `app/services/investment_reports/linked_orders.py` — projections + batch reverse-lookup (S2).
- Modify `app/services/investment_reports/query_service.py` — `get_bundle()` attaches `linked_orders_by_item_uuid` (S2).
- Modify `app/routers/investment_reports.py` — web `_serialise_bundle` sets `item.linked_orders` (S2).
- Modify `app/mcp_server/tooling/investment_reports_handlers.py` — MCP `_serialise_bundle` sets `item.linked_orders` (S2).
- Modify `app/mcp_server/tooling/live_order_ledger.py` + `app/mcp_server/tooling/kis_live_ledger.py` — the two ROB-473 reverse-lookup helpers delegate to the shared projection (S2).
- Create `tests/test_rob554_linked_orders.py` — backend tests (S2).

**Frontend (S3):**
- Modify `frontend/invest/src/types/investmentReports.ts` — `LinkedOrder` interface + `linkedOrders` field on `InvestmentReportItem`.
- Modify `frontend/invest/src/api/investmentReports.ts` — `normalizeLinkedOrder` + map in `normalizeItem`.
- Modify `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx` — order/fill card in `ItemRow`.
- Modify `frontend/invest/src/__tests__/investmentReports.api.test.ts` — mapper test.
- Create `frontend/invest/src/__tests__/InvestmentReportBundleContent.linkedOrders.test.tsx` — component test.

---

## Task 1 (S1): Document `report_item_uuid` in the `place_order` tool description

**Files:**
- Modify: `app/mcp_server/tooling/orders_registration.py:192` (inside the `place_order` description string, lines 179-206)

This is a doc-only change (the `report_item_uuid` parameter is already fully wired). No behavior changes, so verification is a grep, not a unit test.

- [x] **Step 1: Edit the description string**

In the `@mcp.tool(name="place_order", description=(...))` block, insert a sentence after the existing `"Use exit_reason to record the sell thesis in the journal. "` line. The string is a parenthesized concatenation of adjacent string literals — add a new literal:

```python
            "For sell orders, active trade journals are auto-closed in FIFO order. "
            "Use exit_reason to record the sell thesis in the journal. "
            "If this order originates from an investment_report item, pass that "
            "item's item_uuid (from investment_report_create / investment_report_get) "
            "as report_item_uuid to create the ROB-473 audit link so /invest can show "
            "rationale → order → fill status; omit when there is no originating report item. "
            "dry_run=True by default for safety. "
```

(i.e. the new two-sentence literal goes between the `exit_reason` line and the existing `"dry_run=True by default for safety. "` line.)

- [x] **Step 2: Verify the text landed inside the description block**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-554
grep -n "report_item_uuid to create the ROB-473 audit link" app/mcp_server/tooling/orders_registration.py
```
Expected: one match on a line **above** line 207 (`async def place_order(`), i.e. within the description string.

- [x] **Step 3: Lint the file**

Run: `uv run ruff check app/mcp_server/tooling/orders_registration.py && uv run ruff format --check app/mcp_server/tooling/orders_registration.py`
Expected: no errors.

(Commit happens at the end of S2's tasks per the 3-commit packaging — S1 is small enough to ship in the S2 commit. If executing S1 alone, commit now with message `docs(ROB-554): document report_item_uuid in place_order tool description`.)

---

## Task 2 (S2): Add `LinkedOrderView` schema + `linked_orders` field

**Files:**
- Modify: `app/schemas/investment_reports.py` (add class before `InvestmentReportItemResponse` at :735; add field before its `model_config` at :774)
- Test: `tests/test_rob554_linked_orders.py`

- [x] **Step 1: Write the failing test**

Create `tests/test_rob554_linked_orders.py` with:

```python
"""ROB-554 — linked-order read-back: schema, projection, lookup, serializers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

pytestmark = pytest.mark.asyncio


async def test_linked_order_view_defaults_and_item_field() -> None:
    # async (not sync) so the module-level asyncio marker stays uniform; no await needed.
    from app.schemas.investment_reports import (
        InvestmentReportItemResponse,
        LinkedOrderView,
    )

    view = LinkedOrderView(ledger_id=1, order_no="x", status="filled")
    assert view.market is None
    assert view.filled_qty is None
    assert "linked_orders" in InvestmentReportItemResponse.model_fields
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_rob554_linked_orders.py::test_linked_order_view_defaults_and_item_field -v`
Expected: FAIL with `ImportError: cannot import name 'LinkedOrderView'`.

- [x] **Step 3: Add the `LinkedOrderView` class**

In `app/schemas/investment_reports.py`, immediately before `class InvestmentReportItemResponse(BaseModel):` (currently :735), add:

```python
class LinkedOrderView(BaseModel):
    """ROB-554 — read-side view of a live order linked to a report item.

    Sourced from review.live_order_ledger (US/crypto) and
    review.kis_live_order_ledger (KR) by report_item_uuid (ROB-473). Carries
    the reconcile-written fill rollup so the decision-log card can show
    "rationale → order → fill status" without joining the fills table.
    """

    broker: str | None = None
    account_scope: str | None = None
    market: str | None = None
    order_no: str | None = None
    ledger_id: int
    symbol: str | None = None
    side: str | None = None
    status: str | None = None
    filled_qty: Decimal | None = None
    avg_fill_price: Decimal | None = None
    order_time: str | None = None
    reconciled_at: datetime | None = None
    exit_reason: str | None = None
    thesis: str | None = None
    report_item_uuid: UUID | None = None

    model_config = ConfigDict(extra="forbid")
```

(`Decimal`, `datetime`, `UUID`, `BaseModel`, `ConfigDict` are already imported in this module — confirm at the top; no new imports needed.)

- [x] **Step 4: Add the field to `InvestmentReportItemResponse`**

In `InvestmentReportItemResponse`, immediately before its `model_config = ConfigDict(from_attributes=True, populate_by_name=True)` (currently :774), add:

```python
    # ROB-554 — live orders linked to this item via report_item_uuid (ROB-473),
    # with reconcile-written fill rollup. None when the item has no linked orders;
    # set post-validation by the bundle serializers, not read from the ORM row.
    linked_orders: list[LinkedOrderView] | None = None
```

- [x] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_rob554_linked_orders.py::test_linked_order_view_defaults_and_item_field -v`
Expected: PASS.

---

## Task 3 (S2): Create the reverse-lookup service module

**Files:**
- Create: `app/services/investment_reports/linked_orders.py`
- Test: `tests/test_rob554_linked_orders.py` (append)

- [x] **Step 1: Write the failing tests**

Append to `tests/test_rob554_linked_orders.py`:

```python
async def test_list_linked_orders_groups_both_ledgers(session) -> None:
    from app.models.review import KISLiveOrderLedger, LiveOrderLedger
    from app.services.investment_reports.linked_orders import (
        list_linked_orders_for_item_uuids,
    )

    rid, other = uuid.uuid4(), uuid.uuid4()
    crypto_no = f"rob554-{uuid.uuid4().hex[:10]}"
    kr_no = f"rob554-{uuid.uuid4().hex[:10]}"

    session.add(
        LiveOrderLedger(
            trade_date=datetime(2026, 6, 12, tzinfo=UTC),
            broker="upbit", account_scope="upbit_live", market="crypto",
            symbol="BTC", side="buy", order_kind="limit", order_no=crypto_no,
            status="filled", lifecycle_state="filled",
            filled_qty=Decimal("0.01"), avg_fill_price=Decimal("96180000"),
            report_item_uuid=rid,
        )
    )
    session.add(
        KISLiveOrderLedger(
            trade_date=datetime(2026, 6, 12, tzinfo=UTC),
            symbol="005930", instrument_type="equity_kr", side="buy",
            order_type="limit", order_no=kr_no, account_mode="kis_live",
            broker="kis", status="filled", lifecycle_state="filled",
            filled_qty=Decimal("3"), avg_fill_price=Decimal("70100"),
            report_item_uuid=rid,
        )
    )
    # unrelated order under a different report item — must not leak into rid's group
    session.add(
        LiveOrderLedger(
            trade_date=datetime(2026, 6, 12, tzinfo=UTC),
            broker="upbit", account_scope="upbit_live", market="crypto",
            symbol="ETH", side="buy", order_kind="limit",
            order_no=f"rob554-{uuid.uuid4().hex[:10]}",
            status="accepted", lifecycle_state="accepted", report_item_uuid=other,
        )
    )
    await session.flush()

    grouped = await list_linked_orders_for_item_uuids(session, [rid])

    assert set(grouped) == {str(rid)}
    by_no = {v.order_no: v for v in grouped[str(rid)]}
    assert len(by_no) == 2
    assert by_no[crypto_no].market == "crypto"
    assert by_no[crypto_no].account_scope == "upbit_live"
    assert by_no[crypto_no].filled_qty == Decimal("0.01")
    # KR row: account_mode -> account_scope, market constant "kr"
    assert by_no[kr_no].market == "kr"
    assert by_no[kr_no].account_scope == "kis_live"
    assert by_no[kr_no].broker == "kis"
    assert by_no[kr_no].avg_fill_price == Decimal("70100")


async def test_list_linked_orders_empty_for_unlinked(session) -> None:
    from app.services.investment_reports.linked_orders import (
        list_linked_orders_for_item_uuids,
    )

    grouped = await list_linked_orders_for_item_uuids(session, [uuid.uuid4()])
    assert grouped == {}
```

- [x] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_rob554_linked_orders.py -k "list_linked_orders" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.investment_reports.linked_orders'`.

- [x] **Step 3: Create the module**

Create `app/services/investment_reports/linked_orders.py`:

```python
"""ROB-554 — reverse-lookup of live orders linked to report items.

Given report-item UUIDs, return the live orders (US/crypto + KR) whose
ROB-473 ``report_item_uuid`` matches, projected into ``LinkedOrderView`` with
the reconcile-written fill rollup. Single projection source so the web bundle,
the MCP bundle, and the ROB-473 audit helpers cannot drift.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import KISLiveOrderLedger, LiveOrderLedger
from app.schemas.investment_reports import LinkedOrderView


def project_live_order(row: LiveOrderLedger) -> LinkedOrderView:
    """US/crypto ledger row -> LinkedOrderView."""
    return LinkedOrderView(
        broker=row.broker,
        account_scope=row.account_scope,
        market=row.market,
        order_no=row.order_no,
        ledger_id=row.id,
        symbol=row.symbol,
        side=row.side,
        status=row.status,
        filled_qty=row.filled_qty,
        avg_fill_price=row.avg_fill_price,
        order_time=row.order_time,
        reconciled_at=row.reconciled_at,
        exit_reason=row.exit_reason,
        thesis=row.thesis,
        report_item_uuid=row.report_item_uuid,
    )


def project_kis_live_order(row: KISLiveOrderLedger) -> LinkedOrderView:
    """KR ledger row -> LinkedOrderView.

    KR uses ``account_mode`` (not ``account_scope``) and has no ``market``
    column — normalize both into the unified view shape.
    """
    return LinkedOrderView(
        broker=row.broker,
        account_scope=row.account_mode,
        market="kr",
        order_no=row.order_no,
        ledger_id=row.id,
        symbol=row.symbol,
        side=row.side,
        status=row.status,
        filled_qty=row.filled_qty,
        avg_fill_price=row.avg_fill_price,
        order_time=row.order_time,
        reconciled_at=row.reconciled_at,
        exit_reason=row.exit_reason,
        thesis=row.thesis,
        report_item_uuid=row.report_item_uuid,
    )


async def list_linked_orders_for_item_uuids(
    db: AsyncSession, item_uuids: Sequence[UUID]
) -> dict[str, list[LinkedOrderView]]:
    """Return ``{str(report_item_uuid): [LinkedOrderView, ...]}`` for the items.

    Two batch queries (one per live ledger), grouped by report_item_uuid.
    Items with no linked orders are absent from the dict (caller treats missing
    as "no linked orders"). Most-recent-first within each ledger (id desc).
    """
    grouped: dict[str, list[LinkedOrderView]] = {}
    uuids = list(item_uuids)
    if not uuids:
        return grouped

    live_rows = (
        (
            await db.execute(
                select(LiveOrderLedger)
                .where(LiveOrderLedger.report_item_uuid.in_(uuids))
                .order_by(LiveOrderLedger.id.desc())
            )
        )
        .scalars()
        .all()
    )
    kis_rows = (
        (
            await db.execute(
                select(KISLiveOrderLedger)
                .where(KISLiveOrderLedger.report_item_uuid.in_(uuids))
                .order_by(KISLiveOrderLedger.id.desc())
            )
        )
        .scalars()
        .all()
    )

    for row in live_rows:
        grouped.setdefault(str(row.report_item_uuid), []).append(
            project_live_order(row)
        )
    for row in kis_rows:
        grouped.setdefault(str(row.report_item_uuid), []).append(
            project_kis_live_order(row)
        )
    return grouped
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_rob554_linked_orders.py -k "list_linked_orders" -v`
Expected: PASS (both).

---

## Task 4 (S2): Wire `get_bundle()` to attach linked orders

**Files:**
- Modify: `app/services/investment_reports/query_service.py` (import at top; `get_bundle` at :140-169)
- Test: `tests/test_rob554_linked_orders.py` (append)

- [x] **Step 1: Write the failing test**

Append to `tests/test_rob554_linked_orders.py`:

```python
def _action_item():
    from app.schemas.investment_reports import IngestReportItem

    return IngestReportItem(
        client_item_key="rob554-action-1",
        item_kind="action",
        symbol="005930",
        side="buy",
        intent="buy_review",
        rationale="r",
    )


def _request():
    from app.schemas.investment_reports import IngestReportRequest

    return IngestReportRequest(
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="test",
        title="rob554",
        summary="s",
        kst_date="2026-06-12",
        items=[_action_item()],
    )


async def _seed_report_with_linked_order(session):
    """Ingest a report+item, attach one crypto live order via report_item_uuid."""
    from app.models.review import LiveOrderLedger
    from app.services.investment_reports.ingestion import (
        InvestmentReportIngestionService,
    )
    from app.services.investment_reports.repository import (
        InvestmentReportsRepository,
    )

    report = await InvestmentReportIngestionService(session).ingest(_request())
    item = (await InvestmentReportsRepository(session).list_items_for_report(report.id))[0]
    order_no = f"rob554-{uuid.uuid4().hex[:10]}"
    session.add(
        LiveOrderLedger(
            trade_date=datetime(2026, 6, 12, tzinfo=UTC),
            broker="upbit", account_scope="upbit_live", market="crypto",
            symbol="BTC", side="buy", order_kind="limit", order_no=order_no,
            status="filled", lifecycle_state="filled",
            filled_qty=Decimal("0.01"), avg_fill_price=Decimal("96180000"),
            report_item_uuid=item.item_uuid,
        )
    )
    await session.flush()
    return report, item, order_no


async def test_get_bundle_attaches_linked_orders(session) -> None:
    from app.services.investment_reports.query_service import (
        InvestmentReportQueryService,
    )

    report, item, order_no = await _seed_report_with_linked_order(session)
    bundle = await InvestmentReportQueryService(session).get_bundle(report.report_uuid)

    assert bundle is not None
    linked = bundle["linked_orders_by_item_uuid"]
    assert str(item.item_uuid) in linked
    assert linked[str(item.item_uuid)][0].order_no == order_no
    assert linked[str(item.item_uuid)][0].filled_qty == Decimal("0.01")
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_rob554_linked_orders.py::test_get_bundle_attaches_linked_orders -v`
Expected: FAIL with `KeyError: 'linked_orders_by_item_uuid'`.

- [x] **Step 3: Add the import**

In `app/services/investment_reports/query_service.py`, after the existing `from app.services.investment_reports.repository import InvestmentReportsRepository` (:33), add:

```python
from app.services.investment_reports.linked_orders import (
    list_linked_orders_for_item_uuids,
)
```

- [x] **Step 4: Attach linked orders in `get_bundle`**

In `get_bundle` (:149-169), after `items = await self._repo.list_items_for_report(report.id)` and before the `return {`, add the lookup; then add the new key to the returned dict:

```python
        items = await self._repo.list_items_for_report(report.id)
        item_ids = [it.id for it in items]
        decisions = await self._repo.list_decisions_for_items(item_ids)
        alerts = await self._repo.list_alerts_for_source_reports([report.report_uuid])
        events = await self._repo.list_events_for_source_reports([report.report_uuid])
        citations = await self._repo.list_news_citations_for_report(report.report_uuid)

        # ROB-554 — reverse-lookup live orders linked via report_item_uuid (ROB-473).
        linked_orders_by_item_uuid = await list_linked_orders_for_item_uuids(
            self._session, [it.item_uuid for it in items]
        )

        decisions_by_item: dict[int, list[InvestmentReportItemDecision]] = {
            it.id: [] for it in items
        }
        for d in decisions:
            decisions_by_item.setdefault(d.item_id, []).append(d)

        return {
            "report": report,
            "items": items,
            "decisions_by_item": decisions_by_item,
            "alerts": alerts,
            "events": events,
            "news_citations": citations,
            "linked_orders_by_item_uuid": linked_orders_by_item_uuid,
        }
```

- [x] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_rob554_linked_orders.py::test_get_bundle_attaches_linked_orders -v`
Expected: PASS.

---

## Task 5 (S2): Wire both `_serialise_bundle` paths to set `item.linked_orders`

**Files:**
- Modify: `app/routers/investment_reports.py:56-58` (web serializer)
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py:232-234` (MCP serializer)
- Test: `tests/test_rob554_linked_orders.py` (append)

- [x] **Step 1: Write the failing test**

Append to `tests/test_rob554_linked_orders.py`:

```python
async def test_both_serialisers_carry_linked_orders(session) -> None:
    from app.mcp_server.tooling.investment_reports_handlers import (
        _serialise_bundle as mcp_serialise,
    )
    from app.routers.investment_reports import _serialise_bundle as web_serialise
    from app.services.investment_reports.query_service import (
        InvestmentReportQueryService,
    )

    report, item, order_no = await _seed_report_with_linked_order(session)
    bundle = await InvestmentReportQueryService(session).get_bundle(report.report_uuid)

    web = web_serialise(bundle)
    mcp = mcp_serialise(bundle)

    web_item = next(i for i in web.items if str(i.item_uuid) == str(item.item_uuid))
    mcp_item = next(i for i in mcp.items if str(i.item_uuid) == str(item.item_uuid))
    assert web_item.linked_orders is not None
    assert web_item.linked_orders[0].order_no == order_no
    assert mcp_item.linked_orders is not None
    assert mcp_item.linked_orders[0].order_no == order_no
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_rob554_linked_orders.py::test_both_serialisers_carry_linked_orders -v`
Expected: FAIL — `web_item.linked_orders` is `None` (serializers don't set it yet).

- [x] **Step 3: Update the web serializer**

In `app/routers/investment_reports.py`, replace the single-line item-response build (`:57-58`):

```python
def _serialise_bundle(bundle: dict) -> InvestmentReportBundle:
    items = bundle["items"]
    item_responses = [InvestmentReportItemResponse.model_validate(it) for it in items]
```

with:

```python
def _serialise_bundle(bundle: dict) -> InvestmentReportBundle:
    items = bundle["items"]
    # ROB-554 — attach reverse-looked-up linked orders (set post-validation;
    # the ORM item row has no such attribute). Missing key => legacy/no orders.
    linked_by_uuid = bundle.get("linked_orders_by_item_uuid", {})
    item_responses = []
    for it in items:
        resp = InvestmentReportItemResponse.model_validate(it)
        resp.linked_orders = linked_by_uuid.get(str(it.item_uuid))
        item_responses.append(resp)
```

(The rest of the function — `decisions_by_item_uuid`, `item_groups`, `rollup`, `review_sections`, `action_packet`, and the `InvestmentReportBundle(...)` return using `item_responses` — is unchanged.)

- [x] **Step 4: Update the MCP serializer**

In `app/mcp_server/tooling/investment_reports_handlers.py`, `_serialise_bundle` (:224-242) currently builds items inline inside the `return`. Replace:

```python
    return InvestmentReportBundle(
        report=InvestmentReportResponse.model_validate(bundle["report"]),
        items=[InvestmentReportItemResponse.model_validate(it) for it in items],
        decisions_by_item_uuid=decisions_by_item_uuid,
```

with:

```python
    # ROB-554 — attach reverse-looked-up linked orders (parity with web serializer).
    linked_by_uuid = bundle.get("linked_orders_by_item_uuid", {})
    item_responses = []
    for it in items:
        resp = InvestmentReportItemResponse.model_validate(it)
        resp.linked_orders = linked_by_uuid.get(str(it.item_uuid))
        item_responses.append(resp)
    return InvestmentReportBundle(
        report=InvestmentReportResponse.model_validate(bundle["report"]),
        items=item_responses,
        decisions_by_item_uuid=decisions_by_item_uuid,
```

- [x] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_rob554_linked_orders.py::test_both_serialisers_carry_linked_orders -v`
Expected: PASS.

---

## Task 6 (S2): Refactor the two ROB-473 helpers to delegate to the shared projection

**Files:**
- Modify: `app/mcp_server/tooling/live_order_ledger.py:477-505`
- Modify: `app/mcp_server/tooling/kis_live_ledger.py:769-795`
- Test: `tests/test_rob554_linked_orders.py` (append) + existing `tests/test_rob473_report_item_link_ledger.py` must stay green

- [x] **Step 1: Write the failing test**

Append to `tests/test_rob554_linked_orders.py`:

```python
async def test_live_helper_delegates_to_shared_projection(db_session) -> None:
    from app.mcp_server.tooling import live_order_ledger as m

    rid = uuid.uuid4()
    order_no = f"rob554-{uuid.uuid4().hex[:10]}"
    await m._save_live_order_ledger(
        broker="upbit", account_scope="upbit_live", market="crypto",
        symbol="BTC", exchange=None, market_symbol="KRW-BTC", side="buy",
        order_kind="limit", quantity=0.01, price=96180000.0, amount=961800.0,
        currency="KRW", order_no=order_no, order_time="2026-06-12T00:00:00Z",
        status="accepted", response_code="0", response_message="ok",
        raw_response={}, reason="r", thesis=None, strategy=None,
        target_price=None, stop_loss=None, min_hold_days=None, notes=None,
        exit_reason=None, indicators_snapshot=None, report_item_uuid=rid,
    )
    rows = await m.list_live_orders_by_report_item_uuid(rid)
    row = next(r for r in rows if r["order_no"] == order_no)
    # delegation now surfaces the fill-rollup fields the old projection lacked
    assert "filled_qty" in row
    assert row["market"] == "crypto"
    assert row["status"] == "accepted"
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_rob554_linked_orders.py::test_live_helper_delegates_to_shared_projection -v`
Expected: FAIL with `KeyError: 'filled_qty'` (old projection omits it).

- [x] **Step 3: Refactor the US/crypto helper**

In `app/mcp_server/tooling/live_order_ledger.py`, replace the body of `list_live_orders_by_report_item_uuid` (:477-505) with:

```python
async def list_live_orders_by_report_item_uuid(
    report_item_uuid: uuid.UUID,
) -> list[dict[str, Any]]:
    """ROB-473 — live US/crypto orders linked to a report item (audit).

    ROB-554 — projects via the shared LinkedOrderView so this audit helper,
    the web bundle, and the MCP bundle share one field mapping.
    """
    from app.services.investment_reports.linked_orders import project_live_order

    async with _order_session_factory()() as db:
        rows = (
            (
                await db.execute(
                    select(LiveOrderLedger)
                    .where(LiveOrderLedger.report_item_uuid == report_item_uuid)
                    .order_by(LiveOrderLedger.id.desc())
                )
            )
            .scalars()
            .all()
        )
    return [project_live_order(r).model_dump(mode="json") for r in rows]
```

- [x] **Step 4: Refactor the KR helper**

In `app/mcp_server/tooling/kis_live_ledger.py`, replace the body of `list_kis_live_orders_by_report_item_uuid` (:769-795) with:

```python
async def list_kis_live_orders_by_report_item_uuid(
    report_item_uuid: uuid.UUID,
) -> list[dict[str, Any]]:
    """ROB-473 — live KR orders linked to a report item (audit).

    ROB-554 — projects via the shared LinkedOrderView (account_mode ->
    account_scope, market="kr") so KR and US/crypto share one field mapping.
    """
    from app.services.investment_reports.linked_orders import (
        project_kis_live_order,
    )

    async with _order_session_factory()() as db:
        rows = (
            (
                await db.execute(
                    select(KISLiveOrderLedger)
                    .where(KISLiveOrderLedger.report_item_uuid == report_item_uuid)
                    .order_by(KISLiveOrderLedger.id.desc())
                )
            )
            .scalars()
            .all()
        )
    return [project_kis_live_order(r).model_dump(mode="json") for r in rows]
```

- [x] **Step 5: Run new + existing ROB-473 tests to verify all pass**

Run: `uv run pytest tests/test_rob554_linked_orders.py tests/test_rob473_report_item_link_ledger.py -v`
Expected: PASS (new delegation test + the 4 existing ROB-473 tests, which only assert `order_no` presence and still hold).

- [x] **Step 6: Lint + typecheck the backend changes**

Run:
```bash
uv run ruff check app/ tests/test_rob554_linked_orders.py
uv run ruff format --check app/services/investment_reports/linked_orders.py app/services/investment_reports/query_service.py app/routers/investment_reports.py app/mcp_server/tooling/investment_reports_handlers.py app/mcp_server/tooling/live_order_ledger.py app/mcp_server/tooling/kis_live_ledger.py app/schemas/investment_reports.py tests/test_rob554_linked_orders.py
uv run ty check app/
```
Expected: no errors. (If `ruff format --check` flags a file, run `uv run ruff format <file>` and re-run.)

- [x] **Step 7: Run the full ROB-554 backend suite + adjacent suites**

Run: `uv run pytest tests/test_rob554_linked_orders.py tests/test_investment_reports_query_service.py tests/test_investment_reports_router.py tests/test_investment_reports_mcp.py -v`
Expected: PASS.

- [x] **Step 8: Commit S1 + S2**

```bash
git add app/ tests/test_rob554_linked_orders.py docs/superpowers/
git commit  # message below
```
Message:
```
feat(ROB-554): wire report_item_uuid read-back into investment report bundle

S1: document report_item_uuid in the generic place_order tool description.
S2: add LinkedOrderView + linked_orders[] on the report item response, sourced
from a shared reverse-lookup over both live ledgers (US/crypto + KR) by
report_item_uuid (ROB-473). get_bundle attaches it; both web and MCP
_serialise_bundle set it; the two ROB-473 audit helpers delegate to the shared
projection. Fill status is the reconcile-written ledger rollup. Migration 0.
```
(Append the repo's standard `Co-Authored-By:` trailer.)

---

## Task 7 (S3): Frontend types + API mapper for `linkedOrders`

**Files:**
- Modify: `frontend/invest/src/types/investmentReports.ts` (add `LinkedOrder` before `InvestmentReportItem` at :226; add field at :257)
- Modify: `frontend/invest/src/api/investmentReports.ts` (import `LinkedOrder`; add `normalizeLinkedOrder`; map in `normalizeItem` at :188)
- Test: `frontend/invest/src/__tests__/investmentReports.api.test.ts` (append)

All frontend commands run from `cd /Users/mgh3326/work/auto_trader.rob-554/frontend/invest`.

- [x] **Step 1: Write the failing test**

Append a new `describe` block to `frontend/invest/src/__tests__/investmentReports.api.test.ts` (it already imports `fetchInvestmentReportBundle` and defines `mockFetchOnce`):

```typescript
describe("fetchInvestmentReportBundle linked orders (ROB-554)", () => {
  it("maps item.linked_orders snake_case to camelCase linkedOrders", async () => {
    mockFetchOnce({
      report: { report_uuid: "uuid-1" },
      items: [
        {
          item_uuid: "item-1",
          rationale: "r",
          linked_orders: [
            {
              broker: "upbit",
              account_scope: "upbit_live",
              market: "crypto",
              order_no: "df98c030-abc",
              ledger_id: 7,
              symbol: "BTC",
              side: "buy",
              status: "filled",
              filled_qty: "0.01",
              avg_fill_price: "96180000",
              order_time: "2026-06-12T05:03:00Z",
              report_item_uuid: "item-1",
            },
          ],
        },
      ],
      decisions_by_item_uuid: {},
      alerts: [],
      events: [],
    });

    const bundle = await fetchInvestmentReportBundle("uuid-1");
    const linked = bundle.items[0].linkedOrders;
    expect(linked).not.toBeNull();
    expect(linked?.[0].orderNo).toBe("df98c030-abc");
    expect(linked?.[0].market).toBe("crypto");
    expect(linked?.[0].ledgerId).toBe(7);
    expect(linked?.[0].status).toBe("filled");
  });

  it("maps absent linked_orders to null", async () => {
    mockFetchOnce({
      report: { report_uuid: "uuid-2" },
      items: [{ item_uuid: "item-2", rationale: "r" }],
      decisions_by_item_uuid: {},
      alerts: [],
      events: [],
    });
    const bundle = await fetchInvestmentReportBundle("uuid-2");
    expect(bundle.items[0].linkedOrders).toBeNull();
  });
});
```

- [x] **Step 2: Run it to verify it fails**

Run: `npm test -- --run src/__tests__/investmentReports.api.test.ts`
Expected: FAIL — `linked?.[0].orderNo` is `undefined` (mapper doesn't produce `linkedOrders`).

- [x] **Step 3: Add the `LinkedOrder` type + field**

In `frontend/invest/src/types/investmentReports.ts`, immediately before `export interface InvestmentReportItem {` (:226), add:

```typescript
// ROB-554 — a live order linked to a report item via report_item_uuid (ROB-473),
// with the reconcile-written fill rollup. Read-only; surfaced on the decision log.
export interface LinkedOrder {
  broker?: string | null;
  accountScope?: string | null;
  market?: string | null;
  orderNo?: string | null;
  ledgerId: number;
  symbol?: string | null;
  side?: string | null;
  status?: string | null;
  filledQty?: number | string | null;
  avgFillPrice?: number | string | null;
  orderTime?: string | null;
  reconciledAt?: string | null;
  exitReason?: string | null;
  thesis?: string | null;
  reportItemUuid?: string | null;
}
```

Then inside `InvestmentReportItem`, after `citedDimensionReportUuids?: string[];` (:257), add:

```typescript
  // ROB-554 — live orders linked to this item (null when none).
  linkedOrders?: LinkedOrder[] | null;
```

- [x] **Step 4: Add the mapper**

In `frontend/invest/src/api/investmentReports.ts`, add `LinkedOrder` to the type import from `../types/investmentReports` (the import block near the top of the file). Then, immediately before `function normalizeItem(` (:153), add:

```typescript
function normalizeLinkedOrder(raw: ApiItem): LinkedOrder {
  return {
    broker: asOptionalString(raw.broker),
    accountScope: asOptionalString(raw.account_scope),
    market: asOptionalString(raw.market),
    orderNo: asOptionalString(raw.order_no),
    ledgerId: asNumber(raw.ledger_id, 0),
    symbol: asOptionalString(raw.symbol),
    side: asOptionalString(raw.side),
    status: asOptionalString(raw.status),
    filledQty: (raw.filled_qty as number | string | null | undefined) ?? null,
    avgFillPrice:
      (raw.avg_fill_price as number | string | null | undefined) ?? null,
    orderTime: asOptionalString(raw.order_time),
    reconciledAt: asOptionalString(raw.reconciled_at),
    exitReason: asOptionalString(raw.exit_reason),
    thesis: asOptionalString(raw.thesis),
    reportItemUuid: asOptionalString(raw.report_item_uuid),
  };
}
```

Then inside `normalizeItem`, after the `citedDimensionReportUuids: asArray<string>(raw.cited_dimension_report_uuids),` line (:188), add:

```typescript
    // ROB-554 — linked live orders (null when the backend omits / item has none).
    linkedOrders:
      raw.linked_orders === null || raw.linked_orders === undefined
        ? null
        : asArray<ApiItem>(raw.linked_orders).map(normalizeLinkedOrder),
```

- [x] **Step 5: Run the test to verify it passes**

Run: `npm test -- --run src/__tests__/investmentReports.api.test.ts`
Expected: PASS.

---

## Task 8 (S3): Render the order/fill card in `ItemRow`

**Files:**
- Modify: `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx` (add label map near the other `*_LABELS` consts; add card JSX in `ItemRow` after the rationale `<div>` at :274)
- Test: `frontend/invest/src/__tests__/InvestmentReportBundleContent.linkedOrders.test.tsx`

- [x] **Step 1: Write the failing test**

Create `frontend/invest/src/__tests__/InvestmentReportBundleContent.linkedOrders.test.tsx`:

```tsx
// ROB-554 — order/fill card rendered on the decision-log item row.

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { InvestmentReportBundleContent } from "../components/investment-reports/InvestmentReportBundleContent";
import type {
  InvestmentReport,
  InvestmentReportBundle,
  InvestmentReportItem,
} from "../types/investmentReports";

vi.mock("../hooks/useInvestmentReportBundle", () => ({
  useInvestmentReportBundle: vi.fn(),
}));

import { useInvestmentReportBundle } from "../hooks/useInvestmentReportBundle";

function makeReport(): InvestmentReport {
  return {
    reportUuid: "00000000-0000-0000-0000-000000000001",
    reportType: "kr_morning",
    market: "kr",
    marketSession: "regular",
    accountScope: "upbit_live",
    executionMode: "advisory_only",
    createdByProfile: "test",
    title: "Test Report",
    summary: "summary",
    riskSummary: null,
    thesisText: null,
    noActionNote: null,
    marketSnapshot: {},
    portfolioSnapshot: {},
    previousReportUuid: null,
    status: "published",
    metadata: {},
    createdAt: "2026-06-12T00:00:00Z",
    updatedAt: "2026-06-12T00:00:00Z",
    publishedAt: null,
    validUntil: null,
    snapshotBundleUuid: null,
    snapshotPolicyVersion: null,
    snapshotCoverageSummary: null,
    snapshotFreshnessSummary: null,
    sourceConflicts: null,
    unavailableSources: null,
  };
}

function makeItem(overrides: Partial<InvestmentReportItem>): InvestmentReportItem {
  return {
    itemUuid: "00000000-0000-0000-0000-000000000002",
    itemKind: "action",
    symbol: "BTC",
    side: "buy",
    intent: "buy_review",
    rationale: "test rationale",
    targetKind: "asset",
    priority: 0,
    confidence: null,
    evidenceSnapshot: {},
    watchCondition: null,
    triggerChecklist: [],
    maxAction: {},
    validUntil: null,
    status: "proposed",
    metadata: {},
    createdAt: "2026-06-12T00:00:00Z",
    updatedAt: "2026-06-12T00:00:00Z",
    operation: null,
    targetRef: null,
    currentState: null,
    proposedState: null,
    diff: null,
    applyPolicy: null,
    ...overrides,
  };
}

function renderContent(bundle: InvestmentReportBundle) {
  (useInvestmentReportBundle as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
    { status: "ready", bundle, error: null, reload: vi.fn() },
  );
  return render(
    <MemoryRouter initialEntries={["/reports/00000000-0000-0000-0000-000000000001"]}>
      <InvestmentReportBundleContent />
    </MemoryRouter>,
  );
}

function makeBundle(item: InvestmentReportItem): InvestmentReportBundle {
  return {
    report: makeReport(),
    items: [item],
    decisionsByItemUuid: {},
    alerts: [],
    events: [],
  };
}

describe("InvestmentReportBundleContent — ROB-554 linked orders", () => {
  it("renders the fill badge + order line for a linked filled order", () => {
    renderContent(
      makeBundle(
        makeItem({
          linkedOrders: [
            {
              broker: "upbit",
              accountScope: "upbit_live",
              market: "crypto",
              orderNo: "df98c030-1234",
              ledgerId: 7,
              symbol: "BTC",
              side: "buy",
              status: "filled",
              filledQty: "0.01",
              avgFillPrice: "96180000",
              orderTime: "2026-06-12T05:03:00Z",
            },
          ],
        }),
      ),
    );
    expect(screen.getByText("체결")).toBeInTheDocument();
    expect(screen.getByText(/order df98c030/)).toBeInTheDocument();
  });

  it("renders no order card when there are no linked orders", () => {
    renderContent(makeBundle(makeItem({ linkedOrders: null })));
    expect(screen.queryByText("주문 · 체결")).toBeNull();
  });
});
```

- [x] **Step 2: Run it to verify it fails**

Run: `npm test -- --run src/__tests__/InvestmentReportBundleContent.linkedOrders.test.tsx`
Expected: FAIL — `getByText("체결")` finds nothing (card not rendered yet).

- [x] **Step 3: Add the status-label map**

In `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx`, near the other label constants (e.g. beside `ITEM_KIND_LABELS` / `ITEM_STATUS_LABELS` used at :212-213), add:

```tsx
const LINKED_ORDER_STATUS_LABELS: Record<string, string> = {
  filled: "체결",
  partial: "부분체결",
  accepted: "미체결",
  submitted: "미체결",
  pending: "미체결",
  cancelled: "취소",
  anomaly: "이상",
};
```

- [x] **Step 4: Render the card in `ItemRow`**

In `ItemRow`, immediately after the rationale `<div>` block (closes at :274, `{item.rationale}</div>`) and before the `{item.watchCondition ? (` block (:275), add:

```tsx
      {item.linkedOrders && item.linkedOrders.length > 0 ? (
        <div style={{ display: "grid", gap: 6 }}>
          <div style={{ fontSize: 12, color: "var(--fg-2)", fontWeight: 800 }}>
            주문 · 체결
          </div>
          {item.linkedOrders.map((order) => (
            <div
              key={order.ledgerId}
              style={{
                display: "flex",
                gap: 8,
                alignItems: "baseline",
                flexWrap: "wrap",
                fontSize: 12,
                color: "var(--fg-3)",
                background: "var(--surface-2)",
                padding: "6px 10px",
                borderRadius: 8,
              }}
            >
              <Pill
                tone={order.status === "filled" ? "accent" : "paper"}
                size="sm"
              >
                {LINKED_ORDER_STATUS_LABELS[order.status ?? ""] ??
                  order.status ??
                  "—"}
              </Pill>
              <span style={{ fontWeight: 700 }}>
                {order.side === "buy"
                  ? "매수"
                  : order.side === "sell"
                    ? "매도"
                    : ""}{" "}
                {order.symbol ?? "—"}
              </span>
              <span>
                {order.filledQty ?? "—"} @ {order.avgFillPrice ?? "—"}
              </span>
              {order.orderTime ? <span>· {order.orderTime}</span> : null}
              {order.orderNo ? (
                <span>· order {order.orderNo.slice(0, 8)}</span>
              ) : null}
              {order.exitReason || order.thesis ? (
                <span style={{ width: "100%" }}>
                  {order.exitReason ?? order.thesis}
                </span>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
```

(`Pill` is already imported in this file — it is used for the chips at :256-268.)

- [x] **Step 5: Run the test to verify it passes**

Run: `npm test -- --run src/__tests__/InvestmentReportBundleContent.linkedOrders.test.tsx`
Expected: PASS (both cases).

- [x] **Step 6: Typecheck + lint the frontend**

Run (from `frontend/invest`):
```bash
npm run typecheck
npm run lint
```
Expected: no errors. (If the project uses different script names, check `frontend/invest/package.json` `scripts` and run the type-check + lint equivalents.)

- [x] **Step 7: Run the full frontend test suite**

Run: `npm test -- --run`
Expected: PASS (all suites, including the two ROB-554 additions).

- [x] **Step 8: Commit S3**

```bash
git add frontend/invest/
git commit  # message below
```
Message:
```
feat(ROB-554): render linked-order provenance + fill status on /invest decision log

Add LinkedOrder type + snake->camel mapper, and an order/fill card in the report
item row (fill badge + side/symbol + filled qty @ avg price + order id + order-
level exit_reason/thesis). Consumes the linked_orders[] now embedded on each
bundle item by the backend (ROB-554 S2).
```
(Append the repo's standard `Co-Authored-By:` trailer.)

---

## Final Verification

- [x] **Backend full-suite sanity (changed areas):**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-554
uv run pytest tests/test_rob554_linked_orders.py tests/test_rob473_report_item_link_ledger.py tests/test_investment_reports_query_service.py tests/test_investment_reports_router.py tests/test_investment_reports_mcp.py -v
uv run ruff check app/ tests/ && uv run ty check app/
```
Expected: all PASS, no lint/type errors.

- [x] **Frontend full suite + typecheck:**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-554/frontend/invest
npm test -- --run && npm run typecheck
```
Expected: all PASS.

- [x] **Manual smoke (optional, operator):** open a report on `/invest` whose items have `report_item_uuid`-linked live orders and confirm the order/fill card renders. Note: forward-only — only orders sent *after* S1 (with `report_item_uuid` populated) appear; pre-S1 orders have NULL links and show no card (expected).

---

## Self-Review (completed at authoring)

- **Spec coverage:** S1 (place_order desc) → Task 1. S2 (LinkedOrderView, shared lookup, get_bundle, both serializers, helper delegation) → Tasks 2-6. S3 (types/mapper, card) → Tasks 7-8. All-live-markets scope → `list_linked_orders_for_item_uuids` queries both ledgers; KR `account_mode`→`account_scope` + `market="kr"` normalization in `project_kis_live_order`. Ledger-row rollup (no execution_ledger) → fill fields read straight off the ledger row. Forward-only → no backfill task (documented in Final Verification smoke note). Migration 0 → no alembic task.
- **Placeholder scan:** none — every code step contains complete code.
- **Type consistency:** `LinkedOrderView` (backend) ↔ `LinkedOrder` (frontend) fields aligned: `ledger_id`/`ledgerId` (required int), Decimal→`number|string|null`, `account_scope`/`accountScope`, `order_no`/`orderNo`. `list_linked_orders_for_item_uuids` / `project_live_order` / `project_kis_live_order` names match across Tasks 3, 4, 6. Bundle dict key `linked_orders_by_item_uuid` matches across Tasks 4 and 5.
