"""hk 6026: closed Upbit market-alert state (``market_event``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.upbit_market_event import (
    MARKET_EVENT_STALE_AFTER,
    STATE_NONE,
    STATE_STALE,
    STATE_UNKNOWN,
    STATE_WARNING,
    classify_market_event,
    load_market_event_states,
    parse_market_event,
)

_NOW = datetime(2026, 11, 22, 7, 0, tzinfo=UTC)
_CLEAN = {
    "warning": False,
    "caution": {
        "PRICE_FLUCTUATIONS": False,
        "TRADING_VOLUME_SOARING": False,
        "DEPOSIT_AMOUNT_SOARING": False,
        "GLOBAL_PRICE_DIFFERENCES": False,
        "CONCENTRATION_OF_SMALL_ACCOUNTS": False,
    },
}


def _event(**caution: bool) -> dict:
    return {"warning": False, "caution": {**_CLEAN["caution"], **caution}}


def _classify(
    market_event,
    *,
    observed_at=_NOW,
    now=_NOW,
    market="KRW-BTC",
):
    return classify_market_event(
        market=market,
        market_event=market_event,
        observed_at=observed_at,
        now=now,
    )


class TestParseMarketEvent:
    def test_keeps_type_names_and_booleans(self) -> None:
        payload = {
            "warning": True,
            "caution": {"PRICE_FLUCTUATIONS": True, "UPSTREAM_NEW_TYPE": False},
        }
        assert parse_market_event(payload) == payload

    def test_absent_or_wrong_shape_is_none(self) -> None:
        assert parse_market_event(None) is None
        assert parse_market_event({}) is None
        assert parse_market_event({"warning": True}) is None
        assert parse_market_event({"caution": {}}) is None
        assert parse_market_event([]) is None

    def test_non_boolean_values_reject_the_statement(self) -> None:
        assert parse_market_event({"warning": "false", "caution": {}}) is None
        assert parse_market_event({"warning": 0, "caution": {}}) is None
        assert parse_market_event({"warning": False, "caution": []}) is None
        assert parse_market_event({"warning": False, "caution": {"TYPE": 1}}) is None


class TestClassify:
    def test_clean_market_is_none(self) -> None:
        state = _classify(_CLEAN)
        assert state.state == STATE_NONE
        assert state.warning is False
        assert state.caution_types == ()
        assert state.blocks_new_buys is False

    def test_warning_wins_over_caution_and_blocks_buys(self) -> None:
        state = _classify({"warning": True, "caution": {"PRICE_FLUCTUATIONS": True}})
        assert state.state == STATE_WARNING
        # The caution types are still reported for display.
        assert state.caution_types == ("PRICE_FLUCTUATIONS",)
        assert state.blocks_new_buys is True

    def test_caution_lists_sorted_types_and_does_not_block(self) -> None:
        state = _classify(_event(TRADING_VOLUME_SOARING=True, PRICE_FLUCTUATIONS=True))
        assert state.state == "caution:PRICE_FLUCTUATIONS,TRADING_VOLUME_SOARING"
        assert state.caution_types == (
            "PRICE_FLUCTUATIONS",
            "TRADING_VOLUME_SOARING",
        )
        assert state.blocks_new_buys is False

    def test_missing_event_is_unknown_not_none(self) -> None:
        state = _classify(None)
        assert state.state == STATE_UNKNOWN
        assert state.warning is None
        assert state.blocks_new_buys is True

    def test_missing_observation_time_is_unknown(self) -> None:
        state = _classify(_CLEAN, observed_at=None)
        assert state.state == STATE_UNKNOWN
        assert state.blocks_new_buys is True

    def test_old_observation_is_stale_even_when_it_says_none(self) -> None:
        state = _classify(
            _CLEAN, observed_at=_NOW - MARKET_EVENT_STALE_AFTER - timedelta(seconds=1)
        )
        assert state.state == STATE_STALE
        assert state.blocks_new_buys is True

    def test_stale_boundary_is_inclusive(self) -> None:
        state = _classify(_CLEAN, observed_at=_NOW - MARKET_EVENT_STALE_AFTER)
        assert state.state == STATE_NONE

    def test_naive_observation_time_is_utc(self) -> None:
        naive = _NOW.replace(tzinfo=None)
        state = _classify(_CLEAN, observed_at=naive)
        assert state.state == STATE_NONE
        assert state.observed_at == _NOW

    def test_state_string_uses_upbit_type_names(self) -> None:
        state = _classify(_event(UPSTREAM_NEW_TYPE=True))
        assert state.state.startswith("caution:")
        assert state.caution_types == ("UPSTREAM_NEW_TYPE",)


class _FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    async def execute(self, _statement) -> _FakeResult:
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_load_states_reads_stored_observations() -> None:
    session = _FakeSession(
        [
            ("KRW-BTC", _CLEAN, _NOW - timedelta(hours=1)),
            ("KRW-ETH", {"warning": True, "caution": {}}, _NOW - timedelta(hours=1)),
            ("KRW-XRP", None, _NOW - timedelta(hours=1)),
            ("KRW-OLD", _CLEAN, _NOW - timedelta(days=3)),
        ]
    )

    states = await load_market_event_states(session, quote_currency="KRW", now=_NOW)

    assert {market: state.state for market, state in states.items()} == {
        "KRW-BTC": STATE_NONE,
        "KRW-ETH": STATE_WARNING,
        "KRW-XRP": STATE_UNKNOWN,
        "KRW-OLD": STATE_STALE,
    }
    assert states["KRW-BTC"].observed_at == _NOW - timedelta(hours=1)
