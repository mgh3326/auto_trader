"""hk 6424: pure validation of a futures bar on the research minute ingest.

The server derives product, contract month, roll state, session segment and session
date from the symbol and the bar start. These tests pin each derived value, each
closed refusal reason, and that a sent value which disagrees is refused rather than
adopted. No DB, no network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.services.kis_futures_quotes.contracts import roll_state
from app.services.research_candles import minute_ingest as mi

pytestmark = [pytest.mark.unit]

#: Tuesday 2026-12-01, 09:00 KST: a day-session bar.
BAR = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 12, 2, 1, 0, tzinfo=UTC)
#: Mini KOSPI200 listed months; 2026-11 expired on 2026-11-12, 2026-12 is live
#: until 2026-12-10, 2027-03 is live until 2027-03-11.
LISTING: dict[str, tuple[str, ...]] = {"MF200": ("2026-11", "2026-12", "2027-03")}


def _decide(**overrides: Any) -> mi.BarDecision:
    kwargs: dict[str, Any] = {
        "symbol": "MF200.2703",
        "venue": "KRX",
        "time_utc": BAR,
        "open_price": Decimal("400.00"),
        "high_price": Decimal("400.20"),
        "low_price": Decimal("399.90"),
        "close_price": Decimal("400.10"),
        "volume": Decimal("5"),
        "value": None,
        "received_at": BAR + timedelta(minutes=2),
        "source": "KIS",
        "product": None,
        "contract_month": None,
        "roll_state": None,
        "session_segment": None,
        "session_date": None,
        "listing": LISTING,
        "now": NOW,
    }
    kwargs.update(overrides)
    return mi.decide_futures_bar(**kwargs)


def test_valid_bar_derives_every_server_owned_field() -> None:
    decision = _decide()

    assert decision.reason is None
    bar = decision.bar
    assert bar is not None
    assert bar.symbol == "MF200.2703"
    assert bar.product == "MF200"
    assert bar.contract_month == "2703"  # the stored YYMM
    assert bar.roll_state == "next"  # 2026-12 is still live on 2026-12-01
    assert bar.session_segment == "FUT_DAY"
    assert bar.session_date == date(2026, 12, 1)
    assert bar.source == "KIS"
    assert decision.retrieved_at == BAR + timedelta(minutes=2)


def test_roll_state_matches_the_collectors_own_rule() -> None:
    # The server must not re-invent the rule: it calls the collector's function.
    expected = roll_state(
        "2027-03", date(2026, 12, 1), ["2026-11", "2026-12", "2027-03"]
    )
    assert expected == "next"
    assert _decide().bar.roll_state == expected


@pytest.mark.parametrize(
    ("symbol", "now", "bar_start", "expected"),
    [
        # 2026-11 expired on 2026-11-12 (before 2026-11-24).
        ("MF200.2611", NOW, BAR, "expired"),
        # 2026-12 expires in the ISO week of 2026-12-10 (Thu).
        (
            "MF200.2612",
            datetime(2026, 12, 8, 0, 5, tzinfo=UTC),
            datetime(2026, 12, 8, 0, 0, tzinfo=UTC),
            "expiring",
        ),
        # 2026-12 is the nearest live contract on 2026-12-01.
        ("MF200.2612", NOW, BAR, "front"),
    ],
)
def test_roll_state_is_derived_for_each_roll_stage(
    symbol: str, now: datetime, bar_start: datetime, expected: str
) -> None:
    decision = _decide(
        symbol=symbol,
        time_utc=bar_start,
        received_at=bar_start + timedelta(minutes=2),
        now=now,
    )
    assert decision.reason is None, decision.reason
    assert decision.bar is not None
    assert decision.bar.roll_state == expected


def test_night_session_bar_takes_the_next_trading_day() -> None:
    # 2026-12-01 21:00 KST opens the night session that belongs to 2026-12-02.
    start = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
    decision = _decide(
        time_utc=start,
        received_at=start + timedelta(minutes=2),
        now=start + timedelta(hours=1),
    )
    bar = decision.bar
    assert bar is not None
    assert bar.session_segment == "FUT_NIGHT"
    assert bar.session_date == date(2026, 12, 2)


def test_sent_values_that_agree_are_accepted() -> None:
    decision = _decide(
        product="MF200",
        contract_month="2703",
        roll_state="next",
        session_segment="FUT_DAY",
        session_date=date(2026, 12, 1),
    )
    assert decision.reason is None
    assert decision.bar is not None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"roll_state": "front"}, mi.REASON_ROLL_STATE_MISMATCH),
        ({"session_date": date(2026, 11, 30)}, mi.REASON_SESSION_DATE_MISMATCH),
        ({"session_segment": "FUT_NIGHT"}, mi.REASON_SESSION_SEGMENT_MISMATCH),
        ({"product": "F200"}, mi.REASON_PRODUCT_MISMATCH),
        ({"contract_month": "2026-03"}, mi.REASON_CONTRACT_MONTH_MISMATCH),
    ],
)
def test_a_sent_value_that_disagrees_is_refused_not_adopted(
    overrides: dict[str, Any], reason: str
) -> None:
    decision = _decide(**overrides)
    assert decision.bar is None
    assert decision.reason == reason


@pytest.mark.parametrize(
    "symbol",
    [
        "mf200.2703",  # not the canonical case
        " MF200.2703",  # padded
        "F300.2703",  # not a closed product
        "MF200.2713",  # month 13
        "MF200.27",  # too short
        "005930",  # a stock code
        "MF200-2703",  # wrong separator
    ],
)
def test_a_non_canonical_or_unclosed_symbol_is_refused(symbol: str) -> None:
    assert _decide(symbol=symbol).reason == mi.REASON_INVALID_SYMBOL


def test_value_is_refused_because_the_futures_table_has_no_value_column() -> None:
    decision = _decide(value=Decimal("1000"))
    assert decision.reason == mi.REASON_FIELD_NOT_APPLICABLE


def test_an_unreadable_master_refuses_the_bar_and_never_guesses_roll_state() -> None:
    decision = _decide(listing=None)
    assert decision.bar is None
    assert decision.reason == mi.REASON_MASTER_UNAVAILABLE


def test_a_listing_without_the_product_refuses_the_roll_state() -> None:
    assert _decide(listing={}).reason == mi.REASON_ROLL_STATE_UNVERIFIABLE


def test_more_than_one_earlier_live_contract_is_unverifiable() -> None:
    # 2027-06 with 2026-12 and 2027-03 both live on 2026-12-01 is beyond "next".
    decision = _decide(
        symbol="MF200.2706",
        listing={"MF200": ("2026-12", "2027-03", "2027-06")},
    )
    assert decision.reason == mi.REASON_ROLL_STATE_UNVERIFIABLE


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"venue": "NASD"}, mi.REASON_UNKNOWN_VENUE),
        ({"source": "KIWOOM"}, mi.REASON_UNKNOWN_SOURCE),
        ({"source": "UNKNOWN_SOURCE"}, mi.REASON_UNKNOWN_SOURCE),
        (
            {"time_utc": BAR + timedelta(seconds=30)},
            mi.REASON_BAR_TIME_NOT_MINUTE_ALIGNED,
        ),
        (
            {"received_at": BAR + timedelta(minutes=1, seconds=5)},
            mi.REASON_BAR_NOT_CLOSED,
        ),
        ({"received_at": NOW + timedelta(hours=1)}, mi.REASON_RECEIVED_AT_IN_FUTURE),
        ({"received_at": BAR - timedelta(minutes=1)}, mi.REASON_RECEIVED_AT_BEFORE_BAR),
        ({"volume": Decimal("-1")}, mi.REASON_INVALID_VOLUME),
        ({"high_price": Decimal("399")}, mi.REASON_INCONSISTENT_OHLC),
    ],
)
def test_shared_refusals_hold_for_futures(
    overrides: dict[str, Any], reason: str
) -> None:
    assert _decide(**overrides).reason == reason


def test_every_futures_reason_is_in_the_closed_vocabulary() -> None:
    decisions = [
        _decide(symbol="x"),
        _decide(venue="NASD"),
        _decide(source="UPBIT"),
        _decide(time_utc=BAR + timedelta(seconds=1)),
        _decide(received_at=NOW + timedelta(hours=1)),
        _decide(value=Decimal("1")),
        _decide(listing=None),
        _decide(listing={}),
        _decide(product="F200"),
        _decide(roll_state="front"),
    ]
    reasons = {d.reason for d in decisions}
    assert None not in reasons
    assert reasons <= mi.REASONS


def test_a_stock_market_source_is_not_a_futures_source() -> None:
    # The futures set is its own closed list; a KR or US vendor label is refused.
    assert "KIWOOM" not in mi.SOURCES_BY_MARKET["futures"]
    assert "KIS" in mi.SOURCES_BY_MARKET["futures"]
