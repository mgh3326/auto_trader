"""retro_positions, mock_experiments, get_bars and data_gaps against the contract."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.retro_v1 import (
    RetroReadError,
    read_data_gaps,
    read_get_bars,
    read_mock_experiments,
    read_retro_positions,
)
from tests.services.retro_v1.support import (
    FILL_AT,
    FakeSession,
    bar_row,
    kr_day_session,
    ledger,
    ns,
    operational_error,
    validate,
)

pytestmark = pytest.mark.unit
CUTOFF = date(2026, 1, 5)


@pytest.mark.asyncio
async def test_retro_positions_matches_contract_and_excludes_phantoms() -> None:
    session = kr_day_session()
    response = await read_retro_positions(session, cutoff_date=CUTOFF)
    validate(response, "RetroPositionsResponse")
    assert response["total"] == 1
    row = response["data"][0]
    assert (row["market"], row["broker"]) == ("kr", "kis")
    # quarantined phantom and websocket duplicate do not inflate the position
    assert (row["quantity"], row["average_price"]) == ("3", "70000")
    assert row["unrealized_amount"] == "3000"  # (71000 - 70000) * 3 at the cutoff close
    assert (row["lot_state"], row["basis"], row["missing_reason"]) == (
        "known",
        "inference",
        None,
    )
    assert "123456" not in json.dumps(response)
    assert any("quarantined_at IS NULL" in s for s in session.statements)


@pytest.mark.asyncio
async def test_retro_positions_distinguishes_same_symbol_by_broker() -> None:
    rows = [ledger(3001, FILL_AT), ledger(3002, FILL_AT, broker="toss")]
    response = await read_retro_positions(
        FakeSession({"FROM review.execution_ledger": rows}), cutoff_date=CUTOFF
    )
    validate(response, "RetroPositionsResponse")
    assert [(row["broker"], row["market"]) for row in response["data"]] == [
        ("kis", "kr"),
        ("toss", "kr"),
    ]
    assert "account" not in json.dumps(response).lower()


@pytest.mark.asyncio
async def test_retro_positions_unproven_position_is_unknown_not_zero() -> None:
    provisional_only = [ledger(3000, FILL_AT, source="websocket")]
    session = kr_day_session(**{"FROM review.execution_ledger": provisional_only})
    response = await read_retro_positions(session, cutoff_date=CUTOFF)
    validate(response, "RetroPositionsResponse")
    row = response["data"][0]
    assert row["lot_state"] == "unknown"
    assert (row["quantity"], row["average_price"], row["unrealized_amount"]) == (
        None,
        None,
        None,
    )
    assert row["missing_reason"] == "incomplete_evidence"


@pytest.mark.asyncio
async def test_retro_positions_missing_price_keeps_quantity_and_flags_unrealized() -> (
    None
):
    session = kr_day_session(**{"FROM public.kr_candles_1d": []})
    response = await read_retro_positions(session, cutoff_date=CUTOFF)
    validate(response, "RetroPositionsResponse")
    row = response["data"][0]
    assert row["quantity"] == "3" and row["unrealized_amount"] is None
    assert row["missing_reason"] == "missing_data"


@pytest.mark.asyncio
async def test_retro_positions_flat_symbol_is_not_listed_and_read_failure_is_total_null() -> (
    None
):
    sold = [
        ledger(1, FILL_AT),
        ledger(2, FILL_AT + timedelta(hours=1), side="sell", broker_order_id="0000777"),
    ]
    flat = await read_retro_positions(
        kr_day_session(**{"FROM review.execution_ledger": sold}), cutoff_date=CUTOFF
    )
    validate(flat, "RetroPositionsResponse")
    assert flat["data"] == [] and flat["total"] == 0
    failed = await read_retro_positions(
        kr_day_session(**{"FROM review.execution_ledger": operational_error()}),
        cutoff_date=CUTOFF,
    )
    validate(failed, "RetroPositionsResponse")
    assert failed["total"] is None
    assert {"field": "total", "reason": "count_unavailable"} in failed["unknowns"]


def experiment(**kw):
    values = {
        "strategy_id": "S_ALPHA",
        "venue": "alpaca_paper",
        "total": 4,
        "sent": 3,
        "filled": 2,
        "unresolved": 1,
        "closed_count": 2,
        "closed_unpriced": 0,
        "pnl_sum": Decimal("12.50"),
        "last_at": datetime(2026, 1, 4, tzinfo=UTC),
    }
    values.update(kw)
    return ns(**values)


@pytest.mark.asyncio
async def test_mock_experiments_matches_contract() -> None:
    session = FakeSession(
        {
            "FROM review.mock_strategy_orders": [
                experiment(),
                experiment(
                    strategy_id="S_BETA", closed_unpriced=1, pnl_sum=Decimal("3")
                ),
                experiment(strategy_id="S_GAMMA", closed_count=0, pnl_sum=None),
            ]
        }
    )
    response = await read_mock_experiments(session)
    validate(response, "MockExperimentsResponse")
    alpha, beta, gamma = response["data"]
    assert (
        alpha["planned_count"],
        alpha["sent_count"],
        alpha["filled_count"],
        alpha["uncertain_count"],
    ) == (4, 3, 2, 1)
    assert alpha["episode_pnl_amount"] == "12.50" and alpha["currency"] == "USD"
    # a closed episode with no realized P&L makes the sum unknown, not partial
    assert beta["episode_pnl_amount"] is None
    assert {
        "field": "data.1.episode_pnl_amount",
        "reason": "incomplete_evidence",
    } in response["unknowns"]
    assert gamma["episode_pnl_amount"] is None
    assert all("INSERT" not in s and "UPDATE" not in s for s in session.statements)


@pytest.mark.asyncio
async def test_mock_experiments_filters_by_strategy_and_rejects_bad_ids() -> None:
    session = FakeSession({"FROM review.mock_strategy_orders": [experiment()]})
    await read_mock_experiments(session, strategy_id="S_ALPHA")
    assert any("strategy_id =" in s for s in session.statements)
    with pytest.raises(RetroReadError):
        await read_mock_experiments(session, strategy_id="bad id; drop")


@pytest.mark.asyncio
async def test_get_bars_daily_kr_reports_source_venue_and_unknown_adjusted() -> None:
    bars = [bar_row("005930", datetime(2026, 1, 5, tzinfo=UTC), "71000.5")]
    session = FakeSession({"FROM public.kr_candles_1d": bars})
    response = await read_get_bars(
        session, symbol="005930", tf="1d", to=CUTOFF, **{"from": CUTOFF}
    )
    validate(response, "GetBarsResponse")
    row = response["data"][0]
    assert (row["source"], row["venue"], row["adjusted"]) == ("kis", "KRX", None)
    assert row["missing_reason"] == "unavailable"
    assert row["close_price"] == "71000.5" and row["quantity"] == "1000"
    assert response["total"] == 1


@pytest.mark.asyncio
async def test_get_bars_us_daily_is_raw_and_crypto_uses_its_own_table() -> None:
    us = FakeSession(
        {
            "FROM public.us_candles_1d": [
                bar_row(
                    "AAPL",
                    datetime(2026, 1, 5, tzinfo=UTC),
                    "190.25",
                    venue="NASD",
                    source="yahoo",
                )
            ]
        }
    )
    response = await read_get_bars(us, symbol="AAPL", tf="1d", from_=CUTOFF, to=CUTOFF)
    validate(response, "GetBarsResponse")
    row = response["data"][0]
    assert (row["adjusted"], row["currency"], row["venue"], row["missing_reason"]) == (
        False,
        "USD",
        "NASD",
        None,
    )
    assert row["market"] == "us"
    crypto = FakeSession(
        {
            "FROM public.crypto_candles_1m": [
                bar_row(
                    "KRW-BTC",
                    datetime(2026, 1, 5, 0, 1, tzinfo=UTC),
                    "95000000",
                    venue="upbit",
                    source="upbit_ws",
                )
            ]
        }
    )
    response = await read_get_bars(
        crypto, symbol="KRW-BTC", market="crypto", tf="1m", from_=CUTOFF, to=CUTOFF
    )
    validate(response, "GetBarsResponse")
    row = response["data"][0]
    # the crypto minute table stores its provider, so it is reported
    assert (row["currency"], row["source"], row["missing_reason"]) == (
        "KRW",
        "upbit_ws",
        None,
    )
    assert row["market"] == "crypto"
    assert "c.source" in crypto.statements[-1] or "source" in crypto.statements[-1]
    kr_minute = FakeSession(
        {
            "FROM public.kr_candles_1m": [
                bar_row(
                    "005930",
                    datetime(2026, 1, 5, 0, 1, tzinfo=UTC),
                    "71000",
                    source=None,
                )
            ]
        }
    )
    response = await read_get_bars(
        kr_minute, symbol="005930", tf="1m", from_=CUTOFF, to=CUTOFF
    )
    validate(response, "GetBarsResponse")
    # kr/us minute tables carry no provider column
    assert response["data"][0]["source"] is None
    assert response["data"][0]["missing_reason"] == "unavailable"


@pytest.mark.asyncio
async def test_get_bars_explicit_crypto_shape_and_legacy_us_inference() -> None:
    crypto = FakeSession(
        {
            "FROM public.crypto_candles_1d": [
                bar_row("KRW-BTC", datetime(2026, 1, 5, tzinfo=UTC), "95000000")
            ]
        }
    )
    response = await read_get_bars(
        crypto, symbol="KRW-BTC", market="crypto", tf="1d", from_=CUTOFF, to=CUTOFF
    )
    assert response["data"][0]["market"] == "crypto"
    assert "public.crypto_candles_1d" in crypto.statements[-1]
    with pytest.raises(RetroReadError) as err:
        await read_get_bars(
            FakeSession(),
            symbol="BTC",
            market="crypto",
            tf="1d",
            from_=CUTOFF,
            to=CUTOFF,
        )
    assert "market" in err.value.safe_message.lower()
    with pytest.raises(RetroReadError) as err:
        await read_get_bars(
            FakeSession(),
            symbol="KRW-BTC",
            market="us",
            tf="1d",
            from_=CUTOFF,
            to=CUTOFF,
        )
    assert "market" in err.value.safe_message.lower()
    for symbol in ("ETH-USD", "SOL-USDT", "KRW/BTC", "KRW.BTC"):
        with pytest.raises(RetroReadError) as err:
            await read_get_bars(
                FakeSession(),
                symbol=symbol,
                market="us",
                tf="1d",
                from_=CUTOFF,
                to=CUTOFF,
            )
        assert "market" in err.value.safe_message.lower()
    us = FakeSession(
        {
            "FROM public.us_candles_1d": [
                bar_row("BTC", datetime(2026, 1, 5, tzinfo=UTC), "5"),
                bar_row("BAC.PL", datetime(2026, 1, 5, tzinfo=UTC), "40"),
            ]
        }
    )
    response = await read_get_bars(us, symbol="BTC", tf="1d", from_=CUTOFF, to=CUTOFF)
    assert response["data"][0]["market"] == "us"
    preferred = await read_get_bars(
        FakeSession(
            {
                "FROM public.us_candles_1d": [
                    bar_row("BAC.PL", datetime(2026, 1, 5, tzinfo=UTC), "40")
                ]
            }
        ),
        symbol="BAC-PL",
        market="us",
        tf="1d",
        from_=CUTOFF,
        to=CUTOFF,
    )
    assert preferred["data"][0]["symbol"] == "BAC.PL"
    assert preferred["data"][0]["market"] == "us"


@pytest.mark.asyncio
async def test_get_bars_unsupported_timeframe_is_an_unknown_not_an_empty_success() -> (
    None
):
    session = FakeSession()
    response = await read_get_bars(
        session, symbol="005930", tf="5m", from_=CUTOFF, to=CUTOFF
    )
    validate(response, "GetBarsResponse")
    assert response["data"] == [] and response["total"] is None
    assert {"field": "data", "reason": "unsupported"} in response["unknowns"]
    assert not any("candles" in s for s in session.statements)
    with pytest.raises(RetroReadError):
        await read_get_bars(session, symbol="005930", tf="2m", from_=CUTOFF, to=CUTOFF)


@pytest.mark.asyncio
async def test_get_bars_read_failure_is_an_unknown() -> None:
    session = FakeSession({"FROM public.kr_candles_1d": operational_error()})
    response = await read_get_bars(
        session, symbol="005930", tf="1d", from_=CUTOFF, to=CUTOFF
    )
    validate(response, "GetBarsResponse")
    assert response["total"] is None
    assert {"field": "data", "reason": "upstream_error"} in response["unknowns"]


def gap_session(**extra):
    now = datetime.now(UTC)
    stale_run = ns(
        run_id="00000000-0000-0000-0000-000000000001",
        broker="kis",
        window_start=now - timedelta(days=1),
        window_end=now,
        started_at=now - timedelta(hours=5, minutes=1),
        finished_at=now - timedelta(hours=5),
        dry_run=False,
        would_insert=0,
        would_update=0,
        unchanged=0,
        committed_insert=0,
        committed_update=0,
        error_summary=None,
        notes=None,
    )
    routes = {
        "krx_after_market_eligibility": [(0, None, None)],
        "execution_ledger_reconcile_runs": [stale_run],
        "review.trade_forecasts": [None],
        "investment_report_items": [None],
    }
    routes.update(extra)
    return FakeSession(routes)


@pytest.mark.asyncio
async def test_data_gaps_reports_existing_staleness_signals() -> None:
    session = gap_session()
    response = await read_data_gaps(session)
    validate(response, "DataGapsResponse")
    by_dataset = {row["dataset"]: row for row in response["data"]}
    assert (
        by_dataset["krx_after_market_eligibility"]["missing_reason"] == "missing_data"
    )
    assert by_dataset["execution_ledger_reconcile"]["missing_reason"] == "stale"
    assert by_dataset["execution_ledger_reconcile"]["last_updated_at"] is not None
    assert {d for d in by_dataset if d.startswith("negative_class_recording.")} == {
        "negative_class_recording.kr",
        "negative_class_recording.us",
        "negative_class_recording.crypto",
    }
    assert response["total"] == 5


@pytest.mark.asyncio
async def test_data_gaps_failed_signal_is_unknown_never_no_gap() -> None:
    session = gap_session(krx_after_market_eligibility=operational_error())
    response = await read_data_gaps(session)
    validate(response, "DataGapsResponse")
    assert response["total"] is None
    fields = {(u["field"], u["reason"]) for u in response["unknowns"]}
    assert ("source.krx_after_market_eligibility", "upstream_error") in fields
    assert ("total", "count_unavailable") in fields
    assert "krx_after_market_eligibility" not in {
        r["dataset"] for r in response["data"]
    }
