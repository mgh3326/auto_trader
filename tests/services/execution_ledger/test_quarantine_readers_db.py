"""#1175 A3 — quarantined rows are not fills for any ledger reader (test DB only).

Scenario per test: a symbol with one authoritative reconciler buy (the real
position the broker reports) plus one phantom websocket accept-notice row
(``CNTG_YN=1``). Before quarantine the phantom distorts the reader; after
quarantine the reader behaves as if the phantom never existed.

Each reader is also run once with its module's ``execution_ledger_in_effect``
patched to ``true()`` (the "filter dropped" mutant) to prove the filter is
load-bearing: the mutant sees the quarantined phantom again.

Timestamps sit in 2099-11 so this file's reconcile run is the latest KIS run
while a test runs; every row uses a random ``Q`` symbol and is deleted after.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import delete

from app.models.execution_ledger import ExecutionLedger, ExecutionLedgerReconcileRun
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services import protected_quantity_service as pq
from app.services.execution_ledger import kis_lots
from app.services.execution_ledger import quarantine as q
from app.services.execution_ledger import query_service as query_service_module
from app.services.execution_ledger import repository as repository_module
from app.services.execution_ledger.kis_lots import PositionRef
from app.services.execution_ledger.opening_lots import (
    OpeningLotCandidate,
    build_opening_lot_plan,
)
from app.services.execution_ledger.query_service import ExecutionLedgerQueryService
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.fill_event_handoff import broker_risk
from app.services.market_close_digest import queries as digest_queries
from app.services.order_proposals import kis_leftover_inference_service as inference
from app.services.quotes_consumer import repository as quotes_repository
from tests.services.execution_ledger._quarantine_fixtures import row_kwargs

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# 2099-11-10 14:30 KST
NOW = datetime(2099, 11, 10, 5, 30, tzinfo=UTC)
TODAY_MORNING = datetime(2099, 11, 10, 0, 30, tzinfo=UTC)  # 09:30 KST
REASON = "hk 1172: accept notice recorded as a fill"
ACTOR = "desk-operator"


@dataclass
class Scenario:
    symbol: str
    real_id: int
    phantom_id: int
    phantom_order_no: str
    phantom_side: str


class _Seed:
    def __init__(self, db) -> None:
        self.db = db
        self.symbols: set[str] = set()
        self.run_ids: list[uuid.UUID] = []

    async def scenario(self, *, phantom_side: str = "buy") -> Scenario:
        tag = uuid.uuid4().hex[:5].upper()
        symbol = f"Q{tag}"
        self.symbols.add(symbol)
        run = ExecutionLedgerReconcileRun(
            run_id=uuid.uuid4(),
            broker="kis",
            window_start=NOW - timedelta(days=2),
            window_end=NOW - timedelta(minutes=11),
            started_at=NOW - timedelta(minutes=11),
            finished_at=NOW - timedelta(minutes=10),
            dry_run=False,
        )
        self.run_ids.append(run.run_id)
        real = ExecutionLedger(
            **ExecutionLedgerUpsert(
                **row_kwargs(
                    symbol=symbol,
                    order_no=f"R{tag}0001",
                    source="reconciler",
                    raw_payload_json=None,
                    fill_seq=0,
                    filled_qty="10",
                    filled_notional="50000",
                    filled_at=(NOW - timedelta(days=5)).isoformat(),
                )
            ).model_dump()
        )
        phantom_order_no = f"P{tag}0001"
        phantom = ExecutionLedger(
            **ExecutionLedgerUpsert(
                **row_kwargs(
                    symbol=symbol,
                    order_no=phantom_order_no,
                    side=phantom_side,
                    filled_at=TODAY_MORNING.isoformat(),
                )
            ).model_dump()
        )
        self.db.add_all([run, real, phantom])
        await self.db.flush()
        result = Scenario(
            symbol, int(real.id), int(phantom.id), phantom_order_no, phantom_side
        )
        await self.db.commit()
        return result

    async def cleanup(self) -> None:
        await self.db.rollback()
        await self.db.execute(
            delete(ExecutionLedger).where(ExecutionLedger.symbol.in_(self.symbols))
        )
        await self.db.execute(
            delete(ExecutionLedgerReconcileRun).where(
                ExecutionLedgerReconcileRun.run_id.in_(self.run_ids)
            )
        )
        await self.db.commit()


@pytest_asyncio.fixture
async def seed(db_session):
    helper = _Seed(db_session)
    try:
        yield helper
    finally:
        await helper.cleanup()


async def _quarantine(db, scenario: Scenario) -> None:
    result = await q.commit_quarantine(
        db, [scenario.phantom_id], reason=REASON, actor=ACTOR
    )
    assert result.status == "committed"


def _drop_filter(monkeypatch, module) -> None:
    """The mutant: this module's reader no longer excludes quarantined rows."""
    assert hasattr(module, "execution_ledger_in_effect"), module.__name__
    monkeypatch.setattr(module, "execution_ledger_in_effect", lambda: sa.true())


async def _lots(db, scenario: Scenario) -> dict[str, Any]:
    blocks = await kis_lots.load_kis_live_kr_lot_blocks(
        db,
        [PositionRef(scenario.symbol, Decimal("10"), Decimal("5000"))],
        now=NOW,
    )
    await db.rollback()
    return blocks[scenario.symbol]


# ------------------------------------------------------------ lots (A3)


async def test_lots_provisional_listing_drops_the_quarantined_phantom(
    db_session, seed, monkeypatch
) -> None:
    scenario = await seed.scenario()
    before = await _lots(db_session, scenario)
    assert [r["broker_order_id"] for r in before["provisional_rows_excluded"]] == [
        scenario.phantom_order_no
    ]

    await _quarantine(db_session, scenario)
    after = await _lots(db_session, scenario)
    assert after["ledger_state"] == "known", after["unknown_reasons"]
    assert after["provisional_rows_excluded"] == []
    assert [lot["quantity"] for lot in after["lots"]] == ["10"]

    _drop_filter(monkeypatch, kis_lots)
    mutant = await _lots(db_session, scenario)
    assert [r["broker_order_id"] for r in mutant["provisional_rows_excluded"]] == [
        scenario.phantom_order_no
    ]


async def _reseed(db, scenario: Scenario, cutover: datetime) -> str:
    """Run the opening-seed plan for the scenario's symbol and commit it."""
    candidate = OpeningLotCandidate(
        broker="kis",
        account_mode="live",
        venue="krx",
        instrument_type="equity_kr",
        symbol=scenario.symbol,
        raw_symbol=scenario.symbol,
        currency="KRW",
        current_qty=Decimal("10"),
        avg_price=Decimal("5000"),
    )
    repo = ExecutionLedgerRepository(db)
    plan = build_opening_lot_plan(
        candidates=[candidate],
        ledger_net_by_key=await repo.net_quantity_by_match_key_since(cutover=cutover),
        cutover=cutover,
    )
    [upsert] = plan.upserts
    status, _ = await repo.upsert_fill(upsert)
    await db.commit()
    return format(upsert.filled_qty.normalize(), "f")


async def test_lots_symbol_whose_only_mismatch_was_the_phantom_becomes_known(
    db_session, seed, monkeypatch
) -> None:
    """The one path by which a websocket phantom reaches lot net quantity.

    Lots never count websocket rows, but the opening seed is carved as
    ``broker qty - ledger net since cutover`` and that net counted every
    non-manual_import row, phantoms included. A seed carved while the phantom
    existed is one share short, so the symbol is ``quantity_mismatch``.
    Quarantine alone does not rewrite the seed row; re-running the seed plan
    (the existing operator script) with the phantom excluded does.
    """
    cutover = datetime(2099, 11, 8, tzinfo=UTC)
    scenario = await seed.scenario()
    assert await _reseed(db_session, scenario, cutover) == "9"
    before = await _lots(db_session, scenario)
    assert before["ledger_state"] == "unknown"
    # exactly the production shape: short by the phantom, which "would"
    # reconcile if it were a real pending fill
    assert before["unknown_reasons"] == [
        "quantity_mismatch_with_reference",
        "provisional_rows_pending_reconcile",
    ]

    await _quarantine(db_session, scenario)
    still_short = await _lots(db_session, scenario)
    assert still_short["unknown_reasons"] == ["quantity_mismatch_with_reference"]

    assert await _reseed(db_session, scenario, cutover) == "10"
    after = await _lots(db_session, scenario)
    assert after["ledger_state"] == "known", after["unknown_reasons"]
    assert [(lot["origin"], lot["quantity"]) for lot in after["lots"]] == [
        ("opening_seed", "10")
    ]
    assert after["sellable_by_ledger"] == "10"

    _drop_filter(monkeypatch, repository_module)
    assert await _reseed(db_session, scenario, cutover) == "9"
    mutant = await _lots(db_session, scenario)
    assert mutant["ledger_state"] == "unknown"


# ------------------------------------------- #1087 same_day_buy_evidence


async def test_1087_same_day_buy_evidence_ignores_a_quarantined_phantom_buy(
    db_session, seed, monkeypatch
) -> None:
    scenario = await seed.scenario(phantom_side="buy")
    before = await _lots(db_session, scenario)
    assert before["same_day_buy_evidence"]["blocking"] is True
    assert before["open_buy_evidence"]["blocking"] is True

    await _quarantine(db_session, scenario)
    after = await _lots(db_session, scenario)
    assert after["same_day_buy_evidence"]["blocking"] is False
    assert after["same_day_buy_evidence"]["blocking_reasons"] == []
    assert after["open_buy_evidence"]["blocking"] is False

    _drop_filter(monkeypatch, kis_lots)
    mutant = await _lots(db_session, scenario)
    assert mutant["same_day_buy_evidence"]["blocking"] is True


# -------------------------------------------- #1087 open_sell_evidence


async def test_1087_open_sell_evidence_ignores_a_quarantined_phantom_sell(
    db_session, seed, monkeypatch
) -> None:
    scenario = await seed.scenario(phantom_side="sell")
    before = await _lots(db_session, scenario)
    assert before["open_sell_evidence"]["blocking"] is True
    assert before["same_day_sell_evidence"]["blocking"] is True

    await _quarantine(db_session, scenario)
    after = await _lots(db_session, scenario)
    assert after["open_sell_evidence"]["blocking"] is False
    assert after["open_sell_evidence"]["state"] == "known"
    assert after["same_day_sell_evidence"]["blocking"] is False
    assert after["ledger_state"] == "known", after["unknown_reasons"]

    _drop_filter(monkeypatch, kis_lots)
    mutant = await _lots(db_session, scenario)
    assert mutant["open_sell_evidence"]["blocking"] is True


# ------------------------------------------------ #1112 inference facts


async def test_1112_inference_no_longer_sees_the_phantom_as_a_fill(
    db_session, seed, monkeypatch
) -> None:
    scenario = await seed.scenario()
    service = inference.KisLeftoverInferenceService(db_session)

    async def phantom_visible() -> bool:
        fills = await service._symbol_fills(scenario.symbol)
        await db_session.rollback()
        return scenario.phantom_order_no in {f.broker_order_id for f in fills}

    assert await phantom_visible() is True
    await _quarantine(db_session, scenario)
    assert await phantom_visible() is False
    # the real holding is still seen, so "holding unchanged" keeps its witness
    fills = await service._symbol_fills(scenario.symbol)
    await db_session.rollback()
    assert [f.source for f in fills] == ["reconciler"]

    _drop_filter(monkeypatch, inference)
    assert await phantom_visible() is True


# ------------------------------------------------------- fill evidence


Probe = Callable[[Any, Scenario], Awaitable[bool]]


async def _broker_risk_fills(db, s: Scenario) -> bool:
    rows = await broker_risk.SqlAlchemyEvidenceSource(db).list_fills_for_order(
        broker="kis",
        account_mode="live",
        venue="krx",
        broker_order_id=s.phantom_order_no,
    )
    return bool(rows)


async def _has_fill_for_order(db, s: Scenario) -> bool:
    return await ExecutionLedgerRepository(db).has_fill_for_order(
        broker="kis",
        account_mode="live",
        venue="krx",
        broker_order_id=s.phantom_order_no,
    )


async def _triage(db, s: Scenario) -> bool:
    rows = await ExecutionLedgerRepository(db).list_recent_fills_for_triage(
        after_id=s.phantom_id - 1, source=None, limit=500
    )
    return s.phantom_id in {int(r.id) for r in rows}


async def _position_before(db, s: Scenario) -> bool:
    qty, _count = await ExecutionLedgerRepository(db).position_before_fill(
        broker="kis",
        account_mode="live",
        venue="krx",
        instrument_type="equity_kr",
        symbol=s.symbol,
        currency="KRW",
        filled_at=NOW + timedelta(days=1),
        ledger_id=10**15,
    )
    return qty != Decimal("10")


async def _net_since(db, s: Scenario) -> bool:
    net = await ExecutionLedgerRepository(db).net_quantity_by_match_key_since(
        cutover=NOW - timedelta(days=30)
    )
    return net[("kis", "live", "krx", "equity_kr", s.symbol, "KRW")] != Decimal("10")


async def _query_by_symbol(db, s: Scenario) -> bool:
    page = await ExecutionLedgerQueryService(db).list_by_symbol(
        symbol=s.symbol, days=100_000
    )
    return s.phantom_id in {item.id for item in page.items}


async def _query_recent(db, s: Scenario) -> bool:
    page = await ExecutionLedgerQueryService(db).list_recent(limit=500, market="kr")
    return s.phantom_id in {item.id for item in page.items}


async def _query_today(db, s: Scenario) -> bool:
    page = await ExecutionLedgerQueryService(db).list_fills_today(now=NOW)
    return s.phantom_id in {item.id for item in page.items}


async def _query_sell_history(db, s: Scenario) -> bool:
    page = await ExecutionLedgerQueryService(db).list_sell_history(
        days=100_000, market="kr", limit=10_000
    )
    return s.phantom_id in {item.id for item in page.items}


async def _protected_drift(db, s: Scenario) -> bool:
    key = pq.normalize_protection_key(
        account_scope="kis_live", market="kr", symbol=s.symbol
    )
    net = await pq._net_execution_quantity_since(
        db, key=key, since=NOW - timedelta(days=30)
    )
    return net != Decimal("10")


async def _digest(db, s: Scenario) -> bool:
    fills = await digest_queries.SqlAlchemyDigestSources(db)._execution_fills(
        "kr", NOW - timedelta(days=1), NOW + timedelta(days=1)
    )
    return any(f.symbol == s.symbol for f in fills)


async def _quotes_own_fill(db, s: Scenario) -> bool:
    fills = await quotes_repository.QuotesConsumerRepository(db).fills_after(
        s.phantom_id - 1
    )
    return s.phantom_id in {int(f.ledger_id) for f in fills}


READERS: dict[str, tuple[Probe, Any, str]] = {
    # name: (probe answering "is the phantom counted?", module to mutate, side)
    "broker_risk.list_fills_for_order": (_broker_risk_fills, broker_risk, "buy"),
    "repository.has_fill_for_order": (_has_fill_for_order, repository_module, "buy"),
    "repository.list_recent_fills_for_triage": (_triage, repository_module, "buy"),
    "repository.position_before_fill": (_position_before, repository_module, "buy"),
    "repository.net_quantity_by_match_key_since": (
        _net_since,
        repository_module,
        "buy",
    ),
    "query_service.list_recent": (_query_recent, query_service_module, "buy"),
    "query_service.list_by_symbol": (_query_by_symbol, query_service_module, "buy"),
    "query_service.list_fills_today": (_query_today, query_service_module, "buy"),
    "query_service.list_sell_history": (
        _query_sell_history,
        query_service_module,
        "sell",
    ),
    "protected_quantity._net_execution_quantity_since": (_protected_drift, pq, "buy"),
    "market_close_digest._execution_fills": (_digest, digest_queries, "buy"),
    "quotes_consumer.fills_after": (_quotes_own_fill, quotes_repository, "buy"),
}


@pytest.mark.parametrize("reader", sorted(READERS))
async def test_fill_evidence_and_report_readers_exclude_the_quarantined_phantom(
    db_session, seed, monkeypatch, reader: str
) -> None:
    probe, module, side = READERS[reader]
    scenario = await seed.scenario(phantom_side=side)

    async def counted() -> bool:
        try:
            return await probe(db_session, scenario)
        finally:
            await db_session.rollback()

    assert await counted() is True, "the phantom distorts the reader before"
    await _quarantine(db_session, scenario)
    assert await counted() is False, "the quarantined phantom is excluded"
    _drop_filter(monkeypatch, module)
    assert await counted() is True, "dropping the filter re-admits the phantom"
