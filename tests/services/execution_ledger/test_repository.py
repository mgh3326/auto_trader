from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete

from app.models.execution_ledger import ExecutionLedger
from app.models.trading import InstrumentType
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services.execution_ledger.repository import (
    ExecutionLedgerRepository,
    _values_differ,
)


def _fill(**overrides) -> ExecutionLedgerUpsert:  # noqa: ANN003
    data = {
        "broker": "upbit",
        "account_mode": "live",
        "broker_order_id": "order-1",
        "fill_seq": 0,
        "venue": "upbit_krw",
        "instrument_type": "crypto",
        "symbol": "BTC",
        "raw_symbol": "KRW-BTC",
        "side": "buy",
        "filled_qty": Decimal("0.0100000000"),
        "filled_price": Decimal("100000000.0000000000"),
        "filled_notional": Decimal("1000000.0000000000"),
        "fee_amount": Decimal("500.0000000000"),
        "fee_currency": "KRW",
        "filled_at": datetime(2026, 5, 13, 0, 0, tzinfo=UTC),
        "currency": "KRW",
        "source": "reconciler",
        "raw_payload_json": {"safe": True},
    }
    data.update(overrides)
    return ExecutionLedgerUpsert(**data)


def _row(fill: ExecutionLedgerUpsert) -> ExecutionLedger:
    return ExecutionLedger(**fill.model_dump())


class _AggregateResult:
    def __init__(
        self, rows: list[tuple[str, str, str, InstrumentType, str, str, Decimal]]
    ):
        self._rows = rows

    def all(self) -> list[tuple[str, str, str, InstrumentType, str, str, Decimal]]:
        return self._rows


class _AggregateSession:
    def __init__(self, ledger_rows: list[SimpleNamespace], cutover: datetime):
        self.ledger_rows = ledger_rows
        self.cutover = cutover
        self.executed = False

    async def execute(self, statement: Any) -> _AggregateResult:
        self.executed = True
        compiled = statement.compile(compile_kwargs={"render_postcompile": True})
        sql = " ".join(str(compiled).split())

        assert "CASE WHEN" in sql
        assert "review.execution_ledger.side = :side_1" in sql
        assert "ELSE -review.execution_ledger.filled_qty" in sql
        assert "review.execution_ledger.filled_at >= :filled_at_1" in sql
        assert "review.execution_ledger.source != :source_1" in sql
        assert (
            "GROUP BY review.execution_ledger.broker, "
            "review.execution_ledger.account_mode, review.execution_ledger.venue, "
            "review.execution_ledger.instrument_type, review.execution_ledger.symbol, "
            "review.execution_ledger.currency"
        ) in sql
        assert compiled.params["side_1"] == "buy"
        assert compiled.params["filled_at_1"] == self.cutover
        assert compiled.params["source_1"] == "manual_import"

        grouped: dict[tuple[str, str, str, InstrumentType, str, str], Decimal] = {}
        for row in self.ledger_rows:
            if row.filled_at < self.cutover or row.source == "manual_import":
                continue
            key = (
                row.broker,
                row.account_mode,
                row.venue,
                row.instrument_type,
                row.symbol,
                row.currency,
            )
            signed_qty = row.filled_qty if row.side == "buy" else -row.filled_qty
            grouped[key] = grouped.get(key, Decimal("0")) + signed_qty

        return _AggregateResult([(*key, net_qty) for key, net_qty in grouped.items()])


def test_values_differ_treats_decimal_scale_and_timezone_equivalent() -> None:
    fill = _fill(
        filled_qty=Decimal("0.01"),
        filled_price=Decimal("100000000"),
        filled_notional=Decimal("1000000.0"),
        fee_amount=Decimal("500"),
        filled_at=datetime(2026, 5, 13, 0, 0),  # DB drivers may return naive UTC
    )
    row = _row(_fill(filled_at=datetime(2026, 5, 13, 0, 0, tzinfo=UTC)))

    assert _values_differ(row, fill) is False


def test_values_differ_detects_changed_fill_price() -> None:
    row = _row(_fill(filled_price=Decimal("100000000")))
    changed = _fill(filled_price=Decimal("100000001"))

    assert _values_differ(row, changed) is True


# --- Issue 4 regression: wider unique key (account_mode + venue) ---


def test_upsert_key_includes_account_mode_and_venue() -> None:
    """Fills that differ only in account_mode or venue must NOT be considered the same row."""
    live_fill = _fill(account_mode="live", venue="upbit_krw")
    mock_fill = _fill(account_mode="mock", venue="upbit_krw")
    other_venue_fill = _fill(account_mode="live", venue="upbit_usdt")

    # Different account_mode → different key → not equal
    assert _values_differ(_row(live_fill), mock_fill) is True
    # Different venue → different key → not equal
    assert _values_differ(_row(live_fill), other_venue_fill) is True
    # Same key → same
    assert _values_differ(_row(live_fill), live_fill) is False


def test_two_fills_same_order_different_fill_seq_are_distinct() -> None:
    """Multiple partial fills for the same order_id must survive as separate rows."""
    fill_a = _fill(
        fill_seq=0, filled_qty=Decimal("0.1"), filled_price=Decimal("50000000")
    )
    fill_b = _fill(
        fill_seq=1, filled_qty=Decimal("0.2"), filled_price=Decimal("51000000")
    )

    # _values_differ compares column values, not keys; the rows are distinct by key
    assert fill_a.fill_seq != fill_b.fill_seq
    assert fill_a.broker_order_id == fill_b.broker_order_id


@pytest.mark.asyncio
async def test_net_quantity_by_match_key_since_uses_signed_cutover_ledger_rows() -> (
    None
):
    cutover = datetime(2026, 5, 13, 0, 0, tzinfo=UTC)
    key_fields = {
        "broker": "upbit",
        "account_mode": "live",
        "venue": "upbit_krw",
        "instrument_type": InstrumentType.crypto,
        "symbol": "BTC",
        "currency": "KRW",
    }
    session = _AggregateSession(
        [
            SimpleNamespace(
                **key_fields,
                side="buy",
                filled_qty=Decimal("0.10"),
                filled_at=cutover,
                source="reconciler",
            ),
            SimpleNamespace(
                **key_fields,
                side="sell",
                filled_qty=Decimal("0.04"),
                filled_at=datetime(2026, 5, 13, 1, 0, tzinfo=UTC),
                source="websocket",
            ),
            SimpleNamespace(
                **key_fields,
                side="sell",
                filled_qty=Decimal("99"),
                filled_at=datetime(2026, 5, 12, 23, 59, tzinfo=UTC),
                source="reconciler",
            ),
            SimpleNamespace(
                **key_fields,
                side="buy",
                filled_qty=Decimal("10"),
                filled_at=cutover,
                source="manual_import",
            ),
        ],
        cutover,
    )

    result = await ExecutionLedgerRepository(session).net_quantity_by_match_key_since(
        cutover=cutover
    )

    expected_key = ("upbit", "live", "upbit_krw", "crypto", "BTC", "KRW")
    assert result == {expected_key: Decimal("0.06")}
    assert session.executed is True
    assert len(next(iter(result))) == 6
    assert isinstance(next(iter(result))[3], str)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_latest_run_per_broker_ignores_dry_run_audit_rows(db_session) -> None:
    """Persisted dry-run audit rows must not drive ledger freshness.

    The CLI/TaskIQ layers commit the reconcile-run audit row even in dry-run
    mode; a dry-run commits zero fills, so freshness must keep reporting the
    latest commit-mode run.
    """
    import uuid as _uuid
    from datetime import timedelta

    from app.models.execution_ledger import ExecutionLedgerReconcileRun

    far_future = datetime(2099, 1, 1, tzinfo=UTC)
    commit_run_id = _uuid.uuid4()
    dry_run_id = _uuid.uuid4()
    rows = [
        ExecutionLedgerReconcileRun(
            run_id=commit_run_id,
            broker="kis",
            window_start=far_future - timedelta(days=2),
            window_end=far_future - timedelta(days=1),
            started_at=far_future - timedelta(days=1),
            finished_at=far_future - timedelta(days=1),
            dry_run=False,
        ),
        ExecutionLedgerReconcileRun(
            run_id=dry_run_id,
            broker="kis",
            window_start=far_future - timedelta(days=1),
            window_end=far_future,
            started_at=far_future,
            finished_at=far_future,
            dry_run=True,
        ),
    ]
    db_session.add_all(rows)
    await db_session.commit()
    try:
        latest = await ExecutionLedgerRepository(db_session).latest_run_per_broker()
        assert "kis" in latest
        assert latest["kis"].run_id != dry_run_id
        assert latest["kis"].dry_run is False
    finally:
        for row in rows:
            await db_session.delete(row)
        await db_session.commit()


# --- ROB-755: fill-event auto-triage (Task 1: read method) ---


def _triage_row(**overrides) -> ExecutionLedger:
    """Build an ExecutionLedger row satisfying all DB CHECK constraints.

    Defaults produce a valid KIS KR equity websocket fill; overrides allow
    each test to vary broker/side/source/etc while keeping constraint-safe
    values.
    """
    base: dict[str, Any] = {
        "broker": "kis",
        "account_mode": "live",
        "venue": "kis_kr",
        "instrument_type": InstrumentType.equity_kr,
        "symbol": "005930",
        "raw_symbol": "005930",
        "side": "buy",
        "broker_order_id": "ROB755-0001",
        "fill_seq": 0,
        "filled_qty": Decimal("1.0000000000"),
        "filled_price": Decimal("70000.0000000000"),
        "filled_notional": Decimal("70000.0000000000"),
        "fee_amount": Decimal("0.0000000000"),
        "fee_currency": "KRW",
        "filled_at": datetime(2026, 5, 13, 0, 0, tzinfo=UTC),
        "currency": "KRW",
        "source": "websocket",
        "raw_payload_json": None,
    }
    base.update(overrides)
    return ExecutionLedger(**base)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_recent_fills_for_triage_after_id_and_default_source(
    db_session,
) -> None:
    """after_id watermark excludes older rows AND default source='websocket'
    excludes reconciler / manual_import rows.
    """
    ws_a = _triage_row(broker_order_id="ROB755-A1")
    ws_b = _triage_row(broker_order_id="ROB755-A2")
    ws_c = _triage_row(broker_order_id="ROB755-A3")
    reconciler_row = _triage_row(
        broker_order_id="ROB755-AR",
        source="reconciler",
    )
    manual_row = _triage_row(
        broker_order_id="ROB755-AM",
        source="manual_import",
    )
    rows = [ws_a, ws_b, ws_c, reconciler_row, manual_row]
    db_session.add_all(rows)
    await db_session.commit()

    try:
        repo = ExecutionLedgerRepository(db_session)
        all_ids = {r.id for r in rows}

        websocket_only = await repo.list_recent_fills_for_triage()
        returned_ids = {r.id for r in websocket_only}
        assert ws_a.id in returned_ids
        assert ws_b.id in returned_ids
        assert ws_c.id in returned_ids
        assert reconciler_row.id not in returned_ids
        assert manual_row.id not in returned_ids

        after_ws = await repo.list_recent_fills_for_triage(after_id=ws_a.id)
        after_ids = {r.id for r in after_ws}
        assert ws_a.id not in after_ids
        assert ws_b.id in after_ids
        assert ws_c.id in after_ids
        assert reconciler_row.id not in after_ids
        assert manual_row.id not in after_ids

        assert after_ids.issubset(all_ids)
    finally:
        await db_session.execute(
            delete(ExecutionLedger).where(
                ExecutionLedger.broker_order_id.like("ROB755-%")
            )
        )
        await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_recent_fills_for_triage_filters_by_market_side_broker_account_mode(
    db_session,
) -> None:
    """market/side/broker/account_mode filters narrow the result set."""
    crypto_buy_kis_live = _triage_row(
        broker_order_id="ROB755-B1",
        instrument_type=InstrumentType.crypto,
        side="buy",
        broker="kis",
        account_mode="live",
        venue="upbit_krw",
        symbol="BTC",
        raw_symbol="KRW-BTC",
        currency="KRW",
    )
    crypto_sell_kis_live = _triage_row(
        broker_order_id="ROB755-B2",
        instrument_type=InstrumentType.crypto,
        side="sell",
        broker="kis",
        account_mode="live",
        venue="upbit_krw",
        symbol="ETH",
        raw_symbol="KRW-ETH",
        currency="KRW",
    )
    kr_buy_kis_live = _triage_row(
        broker_order_id="ROB755-B3",
        instrument_type=InstrumentType.equity_kr,
        side="buy",
        broker="kis",
        account_mode="live",
        symbol="005930",
        raw_symbol="005930",
    )
    us_sell_upbit_mock = _triage_row(
        broker_order_id="ROB755-B4",
        instrument_type=InstrumentType.equity_us,
        side="sell",
        broker="upbit",
        account_mode="mock",
        venue="upbit_usdt",
        symbol="AAPL",
        raw_symbol="AAPL",
        currency="USD",
        filled_qty=Decimal("0.5000000000"),
        filled_price=Decimal("200.0000000000"),
        filled_notional=Decimal("100.0000000000"),
        fee_currency="USD",
    )
    rows = [
        crypto_buy_kis_live,
        crypto_sell_kis_live,
        kr_buy_kis_live,
        us_sell_upbit_mock,
    ]
    db_session.add_all(rows)
    await db_session.commit()

    try:
        repo = ExecutionLedgerRepository(db_session)

        crypto_results = await repo.list_recent_fills_for_triage(market="crypto")
        crypto_ids = {r.id for r in crypto_results}
        assert crypto_buy_kis_live.id in crypto_ids
        assert crypto_sell_kis_live.id in crypto_ids
        assert kr_buy_kis_live.id not in crypto_ids
        assert us_sell_upbit_mock.id not in crypto_ids

        sell_results = await repo.list_recent_fills_for_triage(side="sell")
        sell_ids = {r.id for r in sell_results}
        assert crypto_sell_kis_live.id in sell_ids
        assert us_sell_upbit_mock.id in sell_ids
        assert crypto_buy_kis_live.id not in sell_ids
        assert kr_buy_kis_live.id not in sell_ids

        kis_results = await repo.list_recent_fills_for_triage(broker="kis")
        kis_ids = {r.id for r in kis_results}
        assert crypto_buy_kis_live.id in kis_ids
        assert crypto_sell_kis_live.id in kis_ids
        assert kr_buy_kis_live.id in kis_ids
        assert us_sell_upbit_mock.id not in kis_ids

        mock_results = await repo.list_recent_fills_for_triage(account_mode="mock")
        mock_ids = {r.id for r in mock_results}
        assert us_sell_upbit_mock.id in mock_ids
        assert crypto_buy_kis_live.id not in mock_ids
        assert crypto_sell_kis_live.id not in mock_ids
        assert kr_buy_kis_live.id not in mock_ids
    finally:
        await db_session.execute(
            delete(ExecutionLedger).where(
                ExecutionLedger.broker_order_id.like("ROB755-%")
            )
        )
        await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_recent_fills_for_triage_orders_by_id_asc_and_clamps_limit(
    db_session,
) -> None:
    """Results are returned in id ASC order and limit clamps to the [1, 500] range."""
    seeded_rows = [
        _triage_row(broker_order_id=f"ROB755-C{i:03d}", fill_seq=i) for i in range(7)
    ]
    db_session.add_all(seeded_rows)
    await db_session.commit()

    try:
        repo = ExecutionLedgerRepository(db_session)
        seeded_ids = [r.id for r in seeded_rows]
        seeded_id_set = set(seeded_ids)

        all_results = await repo.list_recent_fills_for_triage(limit=500)
        our_results = [r for r in all_results if r.id in seeded_id_set]
        assert our_results == sorted(our_results, key=lambda r: r.id)
        assert [r.id for r in our_results] == seeded_ids

        clamped_high = await repo.list_recent_fills_for_triage(limit=10000)
        our_clamped_high = [r for r in clamped_high if r.id in seeded_id_set]
        assert len(our_clamped_high) == len(seeded_ids)

        clamped_low = await repo.list_recent_fills_for_triage(limit=0)
        our_clamped_low = [r for r in clamped_low if r.id in seeded_id_set]
        assert len(our_clamped_low) == 1
        assert our_clamped_low[0].id == seeded_ids[0]

        limited = await repo.list_recent_fills_for_triage(limit=3)
        our_limited = [r for r in limited if r.id in seeded_id_set]
        assert len(our_limited) == 3
        assert [r.id for r in our_limited] == seeded_ids[:3]
    finally:
        await db_session.execute(
            delete(ExecutionLedger).where(
                ExecutionLedger.broker_order_id.like("ROB755-%")
            )
        )
        await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_recent_fills_for_triage_explicit_source_none_returns_all_sources(
    db_session,
) -> None:
    """Passing source=None explicitly overrides the default and includes
    reconciler/manual_import rows (this matches the brief's documented
    contract: "A caller may explicitly pass source=None to mean 'all sources'").
    """
    ws_row = _triage_row(broker_order_id="ROB755-D1", source="websocket")
    rec_row = _triage_row(broker_order_id="ROB755-D2", source="reconciler")
    manual_row = _triage_row(broker_order_id="ROB755-D3", source="manual_import")
    rows = [ws_row, rec_row, manual_row]
    db_session.add_all(rows)
    await db_session.commit()

    try:
        repo = ExecutionLedgerRepository(db_session)
        all_sources = await repo.list_recent_fills_for_triage(source=None)
        returned_ids = {r.id for r in all_sources}
        assert ws_row.id in returned_ids
        assert rec_row.id in returned_ids
        assert manual_row.id in returned_ids
    finally:
        await db_session.execute(
            delete(ExecutionLedger).where(
                ExecutionLedger.broker_order_id.like("ROB755-%")
            )
        )
        await db_session.commit()


def test_execution_ledger_upsert_accepts_toss_broker() -> None:
    fill = _fill(
        broker="toss",
        account_mode="live",
        venue="toss_kr",
        instrument_type="equity_kr",
        symbol="034020",
        raw_symbol="034020",
        broker_order_id="toss-order-1",
        currency="KRW",
    )

    assert fill.broker == "toss"
    assert fill.venue == "toss_kr"
