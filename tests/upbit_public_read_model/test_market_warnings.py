from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.upbit_public_read_model.market_warnings import MarketWarningsService


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarRows(self._rows)


class FakeSession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, statement):
        return _Result(self.rows)


def _row(market="KRW-BTC", warning="NONE"):
    return SimpleNamespace(
        market=market,
        quote_currency="KRW",
        is_active=True,
        market_warning=warning,
        updated_at=datetime(2026, 5, 14, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_market_warnings_universe_tier_does_not_call_http(fake_redis):
    async def detail_fetcher():
        raise AssertionError("must not call HTTP")

    block = await MarketWarningsService(
        redis=fake_redis, detail_fetcher=detail_fetcher
    ).get(["KRW-BTC"], db=FakeSession([_row()]))
    assert block.meta.state == "fresh"
    assert block.entries["KRW-BTC"].warning == "NONE"


@pytest.mark.asyncio
async def test_market_warning_detail_tier_uses_cache(fake_redis, monkeypatch):
    calls = 0

    async def detail_fetcher():
        nonlocal calls
        calls += 1
        return [{"market": "KRW-BTC", "market_event": {"warning": True}}]

    svc = MarketWarningsService(redis=fake_redis, detail_fetcher=detail_fetcher)
    session = FakeSession([_row()])
    first = await svc.get(["KRW-BTC"], include_event_detail=True, db=session)
    second = await svc.get(["KRW-BTC"], include_event_detail=True, db=session)
    assert first.entries["KRW-BTC"].event == {"warning": True}
    assert second.entries["KRW-BTC"].event == {"warning": True}
    assert calls == 1

@pytest.mark.asyncio
async def test_market_warnings_preserve_explicit_values(fake_redis):
    async def detail_fetcher():
        raise AssertionError("must not call HTTP")

    rows = [_row("KRW-BTC", "NONE"), _row("KRW-ETH", "CAUTION")]
    block = await MarketWarningsService(
        redis=fake_redis, detail_fetcher=detail_fetcher
    ).get(["KRW-BTC", "KRW-ETH"], db=FakeSession(rows))
    assert block.meta.state == "fresh"
    assert block.entries["KRW-BTC"].warning == "NONE"
    assert block.entries["KRW-ETH"].warning == "CAUTION"


@pytest.mark.asyncio
async def test_market_warnings_never_fabricate_none(fake_redis):
    """Absent/blank/unrecognized stored warnings surface as UNKNOWN (hk 5945)."""
    async def detail_fetcher():
        raise AssertionError("must not call HTTP")

    rows = [
        _row("KRW-XRP", None),
        _row("KRW-DOGE", ""),
        _row("KRW-SOL", "NOTICE"),
    ]
    block = await MarketWarningsService(
        redis=fake_redis, detail_fetcher=detail_fetcher
    ).get(["KRW-XRP", "KRW-DOGE", "KRW-SOL"], db=FakeSession(rows))
    assert block.meta.state == "fresh"
    assert block.entries["KRW-XRP"].warning == "UNKNOWN"
    assert block.entries["KRW-DOGE"].warning == "UNKNOWN"
    assert block.entries["KRW-SOL"].warning == "UNKNOWN"
