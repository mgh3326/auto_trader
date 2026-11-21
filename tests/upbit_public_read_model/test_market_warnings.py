"""hk 6026: the Upbit market-alert read model is state-based and observation-dated."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.upbit_public_read_model.market_warnings import MarketWarningsService

_NOW = datetime(2026, 11, 22, 7, 0, tzinfo=UTC)


class _Result:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class FakeSession:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.execute_calls = 0

    async def execute(self, _statement) -> _Result:
        self.execute_calls += 1
        return _Result(self.rows)


def _service(session: FakeSession) -> MarketWarningsService:
    return MarketWarningsService(now_provider=lambda: _NOW)


def _clean() -> dict:
    return {
        "warning": False,
        "caution": {
            "PRICE_FLUCTUATIONS": False,
            "TRADING_VOLUME_SOARING": False,
            "DEPOSIT_AMOUNT_SOARING": False,
            "GLOBAL_PRICE_DIFFERENCES": False,
            "CONCENTRATION_OF_SMALL_ACCOUNTS": False,
        },
    }


@pytest.mark.asyncio
async def test_recent_observations_read_fresh_without_any_http_tier():
    session = FakeSession(
        [
            ("KRW-BTC", _clean(), _NOW - timedelta(hours=1)),
            ("KRW-WAVES", {"warning": True, "caution": {}}, _NOW - timedelta(hours=2)),
            (
                "KRW-KAVA",
                {
                    "warning": False,
                    "caution": {"PRICE_FLUCTUATIONS": True, "UPSTREAM_NEW_TYPE": True},
                },
                _NOW - timedelta(minutes=30),
            ),
        ]
    )

    block = await _service(session).get(db=session)

    assert block.meta.state == "fresh"
    assert block.meta.fetchedAt == _NOW - timedelta(minutes=30)
    assert session.execute_calls == 1
    assert block.entries["KRW-BTC"].state == "none"
    assert block.entries["KRW-WAVES"].state == "warning"
    assert block.entries["KRW-WAVES"].warning == "CAUTION"
    assert block.entries["KRW-KAVA"].state == (
        "caution:PRICE_FLUCTUATIONS,UPSTREAM_NEW_TYPE"
    )
    assert block.entries["KRW-KAVA"].caution_types == [
        "PRICE_FLUCTUATIONS",
        "UPSTREAM_NEW_TYPE",
    ]
    # 주의(caution) is not 유의(warning) — the legacy column stays NONE.
    assert block.entries["KRW-KAVA"].warning == "NONE"


@pytest.mark.asyncio
async def test_old_observation_cannot_read_as_fresh():
    """Regression for the 2026-02-21 freshness claim (dots)."""
    observed = datetime(2026, 2, 21, 6, 15, tzinfo=UTC)
    session = FakeSession([("KRW-KAVA", _clean(), observed)])

    block = await _service(session).get(markets=["KRW-KAVA"], db=session)

    assert block.meta.state == "stale"
    assert block.meta.fetchedAt == observed
    assert block.entries["KRW-KAVA"].state == "stale"
    assert block.entries["KRW-KAVA"].warning == "UNKNOWN"
    assert block.entries["KRW-KAVA"].observed_at == observed


@pytest.mark.asyncio
async def test_missing_event_is_unknown_never_none():
    session = FakeSession([("KRW-DOGE", None, _NOW - timedelta(hours=1))])

    block = await _service(session).get(db=session)

    entry = block.entries["KRW-DOGE"]
    assert entry.state == "unknown"
    assert entry.warning == "UNKNOWN"
    assert entry.caution_types == []


@pytest.mark.asyncio
async def test_empty_universe_is_missing_not_fresh():
    session = FakeSession([])

    block = await _service(session).get(db=session)

    assert block.meta.state == "missing"
    assert block.meta.fetchedAt is None
    assert block.entries == {}


@pytest.mark.asyncio
async def test_requested_market_filter_keeps_only_asked_markets():
    session = FakeSession(
        [
            ("KRW-BTC", _clean(), _NOW - timedelta(hours=1)),
            ("KRW-ETH", {"warning": True, "caution": {}}, _NOW - timedelta(hours=1)),
        ]
    )

    block = await _service(session).get(markets=["krw-eth"], db=session)

    assert set(block.entries) == {"KRW-ETH"}
    assert block.entries["KRW-ETH"].state == "warning"


@pytest.mark.asyncio
async def test_include_event_detail_echoes_the_stored_event():
    event = {"warning": False, "caution": {"PRICE_FLUCTUATIONS": True}}
    session = FakeSession([("KRW-KAVA", event, _NOW - timedelta(hours=1))])

    without = await _service(session).get(db=session)
    with_detail = await _service(session).get(db=session, include_event_detail=True)

    assert without.entries["KRW-KAVA"].event is None
    assert with_detail.entries["KRW-KAVA"].event == event
