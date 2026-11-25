"""hk 6343: pure validation for the research minute-bar HTTP ingest."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.services.research_candles import minute_ingest as mi
from app.services.research_candles import session_minutes_collector as sc

pytestmark = [pytest.mark.unit]

NOW = datetime(2026, 11, 24, 21, 0, 0, tzinfo=UTC)
BAR = datetime(2026, 11, 24, 0, 0, 0, tzinfo=UTC)  # 09:00 KST


def _decide(**overrides: Any) -> mi.BarDecision:
    kwargs: dict[str, Any] = {
        "market": "kr",
        "symbol": "005930",
        "venue": "KRX",
        "time_utc": BAR,
        "open_price": Decimal("100"),
        "high_price": Decimal("110"),
        "low_price": Decimal("90"),
        "close_price": Decimal("105"),
        "volume": Decimal("10"),
        "value": None,
        "received_at": BAR + timedelta(minutes=1),
        "source": "KIS",
        "now": NOW,
    }
    kwargs.update(overrides)
    return mi.decide_bar(**kwargs)


def test_valid_kr_bar_derives_server_owned_fields() -> None:
    decision = _decide()

    assert decision.reason is None
    bar = decision.bar
    assert bar is not None
    assert bar.symbol == "005930"
    assert bar.venue == "KRX"
    assert bar.session_segment == "KRX_REGULAR"
    assert bar.session_date.isoformat() == "2026-11-24"
    assert bar.source == "KIS"
    assert bar.value is None  # KR: unproven per-minute value stays NULL
    assert decision.retrieved_at == BAR + timedelta(minutes=1)


def test_valid_us_bar_requires_and_keeps_the_per_bar_value() -> None:
    decision = _decide(
        market="us",
        symbol="NVDA",
        venue="NASD",
        time_utc=datetime(2026, 11, 24, 14, 31, tzinfo=UTC),  # 09:31 ET
        received_at=datetime(2026, 11, 24, 14, 32, tzinfo=UTC),
        value=Decimal("1234.5"),
        source="TOSS",
    )

    assert decision.reason is None and decision.bar is not None
    assert decision.bar.session_segment == "US_REGULAR"
    assert decision.bar.session_date.isoformat() == "2026-11-24"
    assert decision.bar.value == Decimal("1234.5")
    assert decision.bar.source == "TOSS"


def test_us_bar_without_value_is_refused() -> None:
    assert (
        _decide(
            market="us",
            symbol="NVDA",
            venue="NASD",
            value=None,
            time_utc=datetime(2026, 11, 24, 14, 31, tzinfo=UTC),
            received_at=datetime(2026, 11, 24, 14, 32, tzinfo=UTC),
        ).reason
        == mi.REASON_VALUE_REQUIRED
    )


@pytest.mark.parametrize(
    ("market", "source"),
    [("kr", "UPBIT"), ("kr", "kis "), ("us", "KIWOOM"), ("us", "NH_MOCK")],
)
def test_unknown_source_is_refused(market: str, source: str) -> None:
    decision = _decide(market=market, source=source)
    # "kis " strips to KIS and is accepted; the rest are outside the closed set.
    if source.strip().upper() in mi.SOURCES_BY_MARKET[market]:
        assert decision.bar is not None
    else:
        assert decision.reason == mi.REASON_UNKNOWN_SOURCE


def test_venue_outside_the_market_vocabulary_is_refused() -> None:
    assert _decide(market="us", symbol="NVDA", venue="KRX").reason == (
        mi.REASON_UNKNOWN_VENUE
    )
    assert _decide(venue="NASDAQ").reason == mi.REASON_UNKNOWN_VENUE


def test_symbol_is_normalized_per_market() -> None:
    assert _decide(symbol=" 5930 ").bar is not None
    assert _decide(symbol="5930").bar is not None  # KR zero-fills to 6 digits
    assert _decide(symbol="bad symbol").reason == mi.REASON_INVALID_SYMBOL
    assert (
        _decide(
            market="us",
            symbol="BRK-B",
            venue="NYSE",
            time_utc=datetime(2026, 11, 24, 14, 30, tzinfo=UTC),
            received_at=datetime(2026, 11, 24, 14, 31, tzinfo=UTC),
            value=Decimal("1"),
        ).bar.symbol
        == "BRK.B"
    )


def test_bar_time_must_be_aware_and_minute_aligned() -> None:
    assert (
        _decide(time_utc=datetime(2026, 11, 24, 0, 0)).reason
        == mi.REASON_BAR_TIME_INVALID
    )
    assert (
        _decide(time_utc=BAR + timedelta(seconds=30)).reason
        == mi.REASON_BAR_TIME_NOT_MINUTE_ALIGNED
    )


def test_future_received_at_is_refused_but_clock_skew_is_tolerated() -> None:
    within_skew = _decide(received_at=datetime(2026, 11, 24, 21, 0, 3, tzinfo=UTC))
    assert within_skew.bar is not None

    beyond_skew = _decide(received_at=datetime(2026, 11, 24, 21, 0, 6, tzinfo=UTC))
    assert beyond_skew.reason == mi.REASON_RECEIVED_AT_IN_FUTURE

    far_future = _decide(received_at=datetime(2026, 11, 25, tzinfo=UTC))
    assert far_future.reason == mi.REASON_RECEIVED_AT_IN_FUTURE


def test_received_at_cannot_precede_its_bar_or_be_naive() -> None:
    assert (
        _decide(received_at=BAR - timedelta(seconds=1)).reason
        == mi.REASON_RECEIVED_AT_BEFORE_BAR
    )
    assert (
        _decide(received_at=datetime(2026, 11, 24, 0, 1)).reason
        == mi.REASON_RECEIVED_AT_INVALID
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"open_price": Decimal("0")}, mi.REASON_INVALID_PRICE),
        ({"close_price": Decimal("-1")}, mi.REASON_INVALID_PRICE),
        ({"high_price": Decimal("NaN")}, mi.REASON_INVALID_PRICE),
        ({"high_price": Decimal("104")}, mi.REASON_INCONSISTENT_OHLC),
        ({"low_price": Decimal("106")}, mi.REASON_INCONSISTENT_OHLC),
        ({"volume": Decimal("-1")}, mi.REASON_INVALID_VOLUME),
        ({"value": Decimal("-1")}, mi.REASON_INVALID_VALUE),
        ({"value": Decimal("Infinity")}, mi.REASON_INVALID_VALUE),
    ],
)
def test_malformed_numbers_are_refused(overrides: dict[str, Any], reason: str) -> None:
    assert _decide(**overrides).reason == reason


def test_every_reason_is_in_the_closed_vocabulary() -> None:
    decisions = [
        _decide(source="UPBIT"),
        _decide(venue="NASDAQ"),
        _decide(symbol=""),
        _decide(time_utc=BAR + timedelta(seconds=1)),
        _decide(received_at=datetime(2030, 1, 1, tzinfo=UTC)),
        _decide(volume=Decimal("-1")),
    ]
    reasons = {d.reason for d in decisions}
    assert reasons <= mi.REASONS
    assert None not in reasons


def test_batch_id_is_bounded_ascii_and_market_tagged() -> None:
    batch = mi.build_batch_id("us", NOW)
    assert batch.startswith("minutes-ingest-us-")
    assert mi.BATCH_ID_PATTERN.fullmatch(batch)


def test_targets_payload_is_the_build_targets_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = sc.TargetSet(
        symbols=["005930", "000660"],
        by_origin={"fixed_list": 2},
        errors=[{"origin": "kis_holdings", "error_class": "RuntimeError"}],
        truncated={"screener_snapshot": 1},
        tiers=[
            sc.TierReport(
                tier="fixed", budget=50, allowance=50, candidates=2, selected=2, cut=0
            )
        ],
        cuts=[
            {"symbol": "035420", "tier": "screener_snapshot", "reason": "budget_cut"}
        ],
    )

    async def _fake_build_targets(**kwargs: Any) -> sc.TargetSet:
        assert kwargs["market"] == "kr"
        return fake

    monkeypatch.setattr(mi, "build_targets", _fake_build_targets)

    payload = asyncio.run(
        mi.build_targets_payload(
            market="kr",
            session=object(),  # type: ignore[arg-type]
            kis=object(),  # type: ignore[arg-type]
            user_id=1,
            fixed_symbols_raw="069500",
            now=NOW,
            screener_top_n=20,
            max_symbols=150,
            fixed_budget=50,
            intraday_budget=40,
            screener_budget=60,
        )
    )

    assert payload["market"] == "kr"
    assert payload["generated_at"] == NOW.isoformat()
    expected = sc.targets_report(fake)
    for key, value in expected.items():
        assert payload[key] == value
    assert payload["errors"] == fake.errors
