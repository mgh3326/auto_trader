# tests/services/test_trade_journal_mirror_aggregates.py
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.review import KISLiveOrderLedger, KISMockOrderLedger
from app.models.trading import InstrumentType
from app.services.trade_journal import aggregates as agg


@pytest.mark.asyncio
async def test_load_fills_can_isolate_mock_counterfactual(db_session):
    item_uuid = uuid4()
    db_session.add(
        KISMockOrderLedger(
            trade_date=datetime(2026, 7, 6, tzinfo=UTC),
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            side="buy",
            order_type="limit",
            quantity=Decimal("2"),
            price=Decimal("70000"),
            amount=Decimal("140000"),
            fee=Decimal("0"),
            currency="KRW",
            order_no=f"MIRROR-{uuid4().hex[:8]}",
            account_mode="kis_mock",
            broker="kis",
            status="accepted",
            lifecycle_state="fill",
            last_reconcile_detail={"attributed_fill_qty": "2"},
            report_item_uuid=item_uuid,
            mirror_cohort="mock_counterfactual",
            mirror_source_bucket="place_original",
            correlation_id="mirror:item-1",
        )
    )
    db_session.add(
        KISMockOrderLedger(
            trade_date=datetime(2026, 7, 6, tzinfo=UTC),
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            side="buy",
            order_type="limit",
            quantity=Decimal("2"),
            price=Decimal("70000"),
            amount=Decimal("140000"),
            fee=Decimal("0"),
            currency="KRW",
            order_no=f"PRACTICE-{uuid4().hex[:8]}",
            account_mode="kis_mock",
            broker="kis",
            status="accepted",
            lifecycle_state="fill",
            last_reconcile_detail={"attributed_fill_qty": "2"},
            correlation_id="practice:item-1",
        )
    )
    await db_session.flush()

    fills = await agg.load_fills(db_session, market="kr", cohort="mock_counterfactual")
    assert len([f for f in fills if f.symbol == "005930"]) == 1
    fill = [f for f in fills if f.symbol == "005930"][0]
    assert fill.cohort == "mock_counterfactual"
    assert fill.source_bucket == "place_original"
    assert fill.qty == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_delta_scoreboard_pairs_by_report_item_when_correlation_differs(
    db_session, monkeypatch
):
    async def no_excursions(trade):
        return None, None, False

    monkeypatch.setattr(agg, "compute_excursions", no_excursions)
    when = datetime(2026, 7, 6, tzinfo=UTC)
    common = {"report_item_uuid": uuid4()}
    db_session.add_all(
        [
            KISLiveOrderLedger(
                trade_date=when,
                symbol="005930",
                instrument_type="equity_kr",
                side="buy",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("100"),
                amount=Decimal("100"),
                status="filled",
                lifecycle_state="fill",
                filled_qty=Decimal("1"),
                avg_fill_price=Decimal("100"),
                account_mode="kis_live",
                broker="kis",
                correlation_id="live-entry",
                **common,
            ),
            KISLiveOrderLedger(
                trade_date=when,
                symbol="005930",
                instrument_type="equity_kr",
                side="sell",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("105"),
                amount=Decimal("105"),
                status="filled",
                lifecycle_state="fill",
                filled_qty=Decimal("1"),
                avg_fill_price=Decimal("105"),
                account_mode="kis_live",
                broker="kis",
                correlation_id="live-exit",
            ),
            KISMockOrderLedger(
                trade_date=when,
                symbol="005930",
                instrument_type=InstrumentType.equity_kr,
                side="buy",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("100"),
                amount=Decimal("100"),
                fee=Decimal("0"),
                currency="KRW",
                order_no=f"MBUY-{uuid4().hex[:8]}",
                account_mode="kis_mock",
                broker="kis",
                status="accepted",
                lifecycle_state="fill",
                last_reconcile_detail={"attributed_fill_qty": "1"},
                mirror_cohort="mock_counterfactual",
                mirror_source_bucket="place_original",
                correlation_id="mirror:item-2",
                **common,
            ),
            KISMockOrderLedger(
                trade_date=when,
                symbol="005930",
                instrument_type=InstrumentType.equity_kr,
                side="sell",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("110"),
                amount=Decimal("110"),
                fee=Decimal("0"),
                currency="KRW",
                order_no=f"MSELL-{uuid4().hex[:8]}",
                account_mode="kis_mock",
                broker="kis",
                status="accepted",
                lifecycle_state="fill",
                last_reconcile_detail={"attributed_fill_qty": "1"},
                mirror_cohort="mock_counterfactual",
                mirror_source_bucket="place_original",
                correlation_id="mock-exit",
            ),
        ]
    )
    await db_session.flush()

    result = await agg.build_counterfactual_delta_scoreboard(
        db_session, market="kr", include_excursions=False, use_cache=False
    )
    assert result["paired_count"] >= 1
    assert result["overall_delta"]["mock_minus_live_expectancy_pct"] == pytest.approx(
        0.05
    )
    assert result["caveats"]
