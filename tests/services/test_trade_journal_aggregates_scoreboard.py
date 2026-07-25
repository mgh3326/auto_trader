from datetime import UTC, datetime

import pytest

from app.services.trade_journal import aggregates as agg
from app.services.trade_journal.aggregates import (
    ClosedTrade,
    TagInfo,
    TradeMetrics,
    aggregate_by_tag,
)


def _tm(
    pnl_pct, r, tag="pullback_long", tag_source="strategy_key", link="symbol_window"
):
    ct = ClosedTrade(
        market="kr",
        symbol="005930",
        account="a",
        qty=10,
        entry_price=100.0,
        exit_price=100.0 * (1 + pnl_pct),
        entry_ts=datetime(2026, 6, 1, tzinfo=UTC),
        exit_ts=datetime(2026, 6, 2, tzinfo=UTC),
        pnl_abs=1000.0 * pnl_pct,
        pnl_pct=pnl_pct,
        fees=0.0,
        entry_item_uuids=(),
        exit_item_uuid=None,
        entry_correlation_ids=(),
        exit_correlation_id=None,
    )
    return TradeMetrics(
        trade=ct,
        tag=TagInfo(tag, tag_source, link),
        r_multiple=r,
        mae=-0.03,
        mfe=0.08,
    )


def test_aggregate_math():
    rows = [_tm(0.10, 2.0), _tm(-0.05, -1.0), _tm(0.20, 3.0)]
    [g] = aggregate_by_tag(rows)
    assert g["tag"] == "pullback_long"
    assert g["n"] == 3
    assert g["wins"] == 2 and g["losses"] == 1
    assert g["win_rate"] == pytest.approx(2 / 3)
    assert g["expectancy_pct"] == pytest.approx((0.10 - 0.05 + 0.20) / 3)
    assert g["expectancy_r"] == pytest.approx((2.0 - 1.0 + 3.0) / 3)
    # profit factor = gross wins / |gross losses| = (100+200)/50
    assert g["profit_factor"] == pytest.approx(300 / 50)
    assert g["insufficient_sample"] is True  # n < 10


def test_insufficient_sample_flag_clears_at_10():
    rows = [_tm(0.01, 1.0) for _ in range(10)]
    [g] = aggregate_by_tag(rows)
    assert g["n"] == 10
    assert g["insufficient_sample"] is False


@pytest.mark.asyncio
async def test_scoreboard_fail_open_on_ohlcv_error(db_session, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(agg, "get_ohlcv", boom)
    result = await agg.build_trading_scoreboard(db_session, use_cache=False)
    assert result["count"] == 0
    assert result["groups"] == []


@pytest.mark.asyncio
async def test_include_excursions_false_skips_ohlcv(db_session, monkeypatch):
    called = False

    async def spy_get_ohlcv(*a, **k):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(agg, "get_ohlcv", spy_get_ohlcv)
    # market=None, empty CI-owned rows are fine; the assertion is on the call, not counts
    await agg.build_trading_scoreboard(
        db_session, use_cache=False, include_excursions=False
    )
    assert called is False


@pytest.mark.asyncio
async def test_include_excursions_in_cache_key(db_session, monkeypatch):
    from datetime import UTC, datetime

    calls = {"n": 0}

    async def counting_load_fills(*a, **k):
        calls["n"] += 1
        return []

    monkeypatch.setattr(agg, "load_fills", counting_load_fills)
    stamp = datetime(2026, 7, 5, tzinfo=UTC)
    await agg.build_trading_scoreboard(db_session, include_excursions=True, now=stamp)
    await agg.build_trading_scoreboard(db_session, include_excursions=False, now=stamp)
    # distinct cache keys → load_fills ran twice, not served from one cache slot
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_load_fills_excludes_smoke_marked_reason(db_session):
    from datetime import UTC, datetime

    from app.models.review import KISLiveOrderLedger

    db_session.add(
        KISLiveOrderLedger(
            symbol="005930",
            instrument_type="equity_kr",
            side="buy",
            order_type="limit",
            status="filled",
            lifecycle_state="fill",
            filled_qty=10,
            avg_fill_price=100.0,
            trade_date=datetime(2026, 6, 1, tzinfo=UTC),
            reason="smoke-only probe do not journal",
        )
    )
    await db_session.flush()
    fills = await agg.load_fills(db_session, market="kr")
    assert all("005930" not in f.symbol or f.price != 100.0 for f in fills)


def test_excursions_degraded_surfaced_in_group():
    r1 = _tm(0.10, 2.0)
    r2 = _tm(-0.05, -1.0)
    r1.degraded = True  # TradeMetrics is @dataclass (not frozen) → mutable
    [g] = aggregate_by_tag([r1, r2])
    assert g["excursions_degraded"] == 1


@pytest.mark.asyncio
async def test_cache_returns_isolated_copies(db_session, monkeypatch):
    from datetime import UTC, datetime

    async def empty_load_fills(*a, **k):
        return []

    monkeypatch.setattr(agg, "load_fills", empty_load_fills)
    stamp = datetime(2026, 7, 5, tzinfo=UTC)
    first = await agg.build_trading_scoreboard(db_session, now=stamp)
    first["groups"].append({"tag": "MUTANT"})
    first["count"] = 999
    second = await agg.build_trading_scoreboard(db_session, now=stamp)  # cache hit
    assert second["groups"] == []
    assert second["count"] == 0


@pytest.mark.asyncio
async def test_counterfactual_delta_loads_fills_once(db_session, monkeypatch):
    calls = []

    async def fake_load_fills(db, **kw):
        calls.append(kw)
        return []

    monkeypatch.setattr(agg, "load_fills", fake_load_fills)

    result = await agg.build_counterfactual_delta_scoreboard(
        db_session,
        market="kr",
        setup_tag="breakout",
        min_sample=2,
        use_cache=False,
    )

    assert result["paired_count"] == 0
    assert calls == [
        {
            "market": "kr",
            "account_mode": None,
            "date_from": None,
            "date_to": None,
            "cohort": "all",
        }
    ]


@pytest.mark.asyncio
async def test_counterfactual_delta_setup_tag_filters_pairing(db_session, monkeypatch):
    ts1 = datetime(2026, 7, 1, tzinfo=UTC)
    ts2 = datetime(2026, 7, 2, tzinfo=UTC)

    async def fake_load_fills(db, **kw):
        return [
            agg.Fill(
                market="kr",
                symbol="005930",
                account="kis_live",
                side="buy",
                qty=1,
                price=100,
                fee=0,
                ts=ts1,
                item_uuid="item-1",
                correlation_id="mirror:item-1",
                source="kis",
                cohort="live_gated",
            ),
            agg.Fill(
                market="kr",
                symbol="005930",
                account="kis_live",
                side="sell",
                qty=1,
                price=110,
                fee=0,
                ts=ts2,
                item_uuid="item-1",
                correlation_id="mirror:item-1",
                source="kis",
                cohort="live_gated",
            ),
            agg.Fill(
                market="kr",
                symbol="005930",
                account="kis_mock",
                side="buy",
                qty=1,
                price=100,
                fee=0,
                ts=ts1,
                item_uuid="item-1",
                correlation_id="mirror:item-1",
                source="kis_mock",
                cohort="mock_counterfactual",
            ),
            agg.Fill(
                market="kr",
                symbol="005930",
                account="kis_mock",
                side="sell",
                qty=1,
                price=120,
                fee=0,
                ts=ts2,
                item_uuid="item-1",
                correlation_id="mirror:item-1",
                source="kis_mock",
                cohort="mock_counterfactual",
            ),
        ]

    async def fake_resolve_setup_tag(db, trade):
        return TagInfo("mean_revert", "strategy_key", "exact")

    monkeypatch.setattr(agg, "load_fills", fake_load_fills)
    monkeypatch.setattr(agg, "resolve_setup_tag", fake_resolve_setup_tag)

    result = await agg.build_counterfactual_delta_scoreboard(
        db_session,
        market="kr",
        setup_tag="breakout",
        min_sample=1,
        use_cache=False,
    )

    assert result["live_gated"]["groups"] == []
    assert result["mock_counterfactual"]["groups"] == []
    assert result["paired_count"] == 0
    assert result["overall_delta"]["paired_n"] == 0


@pytest.mark.asyncio
async def test_counterfactual_delta_min_sample_filters_pairing(db_session, monkeypatch):
    ts1 = datetime(2026, 7, 1, tzinfo=UTC)
    ts2 = datetime(2026, 7, 2, tzinfo=UTC)

    async def fake_load_fills(db, **kw):
        return [
            agg.Fill(
                "kr",
                "005930",
                "kis_live",
                "buy",
                1,
                100,
                0,
                ts1,
                "item-1",
                "mirror:item-1",
                "kis",
                "live_gated",
            ),
            agg.Fill(
                "kr",
                "005930",
                "kis_live",
                "sell",
                1,
                110,
                0,
                ts2,
                "item-1",
                "mirror:item-1",
                "kis",
                "live_gated",
            ),
            agg.Fill(
                "kr",
                "005930",
                "kis_mock",
                "buy",
                1,
                100,
                0,
                ts1,
                "item-1",
                "mirror:item-1",
                "kis_mock",
                "mock_counterfactual",
            ),
            agg.Fill(
                "kr",
                "005930",
                "kis_mock",
                "sell",
                1,
                120,
                0,
                ts2,
                "item-1",
                "mirror:item-1",
                "kis_mock",
                "mock_counterfactual",
            ),
        ]

    async def fake_resolve_setup_tag(db, trade):
        return TagInfo("breakout", "strategy_key", "exact")

    monkeypatch.setattr(agg, "load_fills", fake_load_fills)
    monkeypatch.setattr(agg, "resolve_setup_tag", fake_resolve_setup_tag)

    result = await agg.build_counterfactual_delta_scoreboard(
        db_session,
        market="kr",
        setup_tag="breakout",
        min_sample=2,
        use_cache=False,
    )

    assert result["paired_count"] == 0
    assert result["overall_delta"]["paired_n"] == 0


@pytest.mark.asyncio
async def test_delta_scoreboard_pairs_when_mock_exit_is_unstamped(
    db_session, monkeypatch
):
    from decimal import Decimal
    from uuid import uuid4

    from app.models.review import KISLiveOrderLedger, KISMockOrderLedger
    from app.models.trading import InstrumentType

    async def no_excursions(trade):
        return None, None, False

    monkeypatch.setattr(agg, "compute_excursions", no_excursions)
    item_uuid = uuid4()
    ts1 = datetime(2026, 7, 1, tzinfo=UTC)
    ts2 = datetime(2026, 7, 2, tzinfo=UTC)
    db_session.add_all(
        [
            KISLiveOrderLedger(
                trade_date=ts1,
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
                report_item_uuid=item_uuid,
            ),
            KISLiveOrderLedger(
                trade_date=ts2,
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
                trade_date=ts1,
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
                correlation_id=f"mirror:{item_uuid}",
                report_item_uuid=item_uuid,
            ),
            KISMockOrderLedger(
                trade_date=ts2,
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
                correlation_id="manual-mock-exit",
            ),
        ]
    )
    await db_session.flush()

    result = await agg.build_counterfactual_delta_scoreboard(
        db_session,
        market="kr",
        include_excursions=False,
        use_cache=False,
        min_pair_threshold=1,
    )
    assert result["paired_count"] == 1
    assert result["overall_delta"]["paired_n"] == 1
    assert result["pairing_health"]["status"] == "ok"


@pytest.mark.asyncio
async def test_delta_scoreboard_health_flags_closed_but_unpaired_samples(
    db_session, monkeypatch
):
    ts1 = datetime(2026, 7, 1, tzinfo=UTC)
    ts2 = datetime(2026, 7, 2, tzinfo=UTC)

    async def fake_load_fills(db, **kw):
        return [
            agg.Fill(
                "kr",
                "005930",
                "kis_live",
                "buy",
                1,
                100,
                0,
                ts1,
                None,
                "live-a",
                "kis",
                "live_gated",
            ),
            agg.Fill(
                "kr",
                "005930",
                "kis_live",
                "sell",
                1,
                105,
                0,
                ts2,
                None,
                "live-b",
                "kis",
                "live_gated",
            ),
            agg.Fill(
                "kr",
                "005930",
                "kis_mock",
                "buy",
                1,
                100,
                0,
                ts1,
                "item-1",
                "mirror:item-1",
                "kis_mock",
                "mock_counterfactual",
            ),
            agg.Fill(
                "kr",
                "005930",
                "kis_mock",
                "sell",
                1,
                110,
                0,
                ts2,
                None,
                "manual-exit",
                "kis_mock",
                "mock_counterfactual",
            ),
        ]

    monkeypatch.setattr(agg, "load_fills", fake_load_fills)
    result = await agg.build_counterfactual_delta_scoreboard(
        db_session,
        market="kr",
        include_excursions=False,
        use_cache=False,
        min_pair_threshold=1,
    )
    assert result["paired_count"] == 0
    assert result["pairing_diagnostics"]["live_trades_without_report_item_uuid"] == 1
    assert result["pairing_diagnostics"]["unpaired_mock_count"] == 1
    assert result["pairing_health"]["status"] == "needs_design_review"
    assert any("report_item_uuid" in caveat for caveat in result["caveats"])
