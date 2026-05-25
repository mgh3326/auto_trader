"""ROB-307 PR1 — tests for the observe-only Demo scalping signal CLI.

Default-disabled (zero side effects) unless BINANCE_DEMO_SCALPING_ENABLED
is truthy. Orchestration is tested with a fake market-data reader and a
fake snapshot loader, so no DB and no network are touched.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_scalping.contract import LedgerSnapshot
from app.services.brokers.binance.demo_scalping.market_data import BookTicker
from app.services.brokers.binance.demo_scalping.signal import Candle
from scripts.binance_demo_scalping_signal import (
    _parse_args,
    _truthy,
    main,
    observe_symbols,
)

_NOW = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=dt.UTC)
_NOW_MS = int(_NOW.timestamp() * 1000)


def _uptrend(now_ms: int = _NOW_MS) -> list[Candle]:
    closes = list(range(100, 130))
    n = len(closes)
    return [
        Candle(
            open_time_ms=now_ms - (n - 1 - i) * 60_000 - 59_999,
            open=Decimal(c),
            high=Decimal(c),
            low=Decimal(c),
            close=Decimal(c),
            close_time_ms=now_ms - (n - 1 - i) * 60_000,
        )
        for i, c in enumerate(closes)
    ]


class _FakeMarketData:
    async def fetch_klines(self, product, symbol, *, interval="1m", limit=50):
        return _uptrend()

    async def fetch_book_ticker(self, product, symbol):
        return BookTicker(bid=Decimal("129.99"), ask=Decimal("130.00"))


async def _healthy_loader(service, *, product, symbol, now):
    return LedgerSnapshot(
        has_open_lifecycle_for_symbol=False,
        global_open_lifecycle_count=0,
        orders_today=0,
        realized_loss_today_usdt=Decimal("0"),
        seconds_since_last_close_for_symbol=None,
    )


def test_truthy_accepts_common_true_values() -> None:
    assert _truthy("true") and _truthy("1") and _truthy("YES") and _truthy("on")
    assert not _truthy("") and not _truthy(None) and not _truthy("false")


def test_parse_args_defaults_to_allowlist_and_spot() -> None:
    args = _parse_args([])
    assert set(args.symbols.split(",")) == {"XRPUSDT", "DOGEUSDT", "SOLUSDT"}
    assert args.products == "spot"


def test_disabled_by_default_returns_zero_and_prints_nothing(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("BINANCE_DEMO_SCALPING_ENABLED", raising=False)
    rc = main(["--symbols", "XRPUSDT"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "observe_only" not in out  # no evidence emitted when disabled


@pytest.mark.asyncio
async def test_observe_symbols_emits_one_record_per_pair() -> None:
    records = await observe_symbols(
        market_data=_FakeMarketData(),
        ledger_service=None,
        products=["spot"],
        symbols=["XRPUSDT", "DOGEUSDT"],
        now=_NOW,
        snapshot_loader=_healthy_loader,
    )
    assert len(records) == 2
    assert {r.symbol for r in records} == {"XRPUSDT", "DOGEUSDT"}
    assert all(r.action == "observe_only" for r in records)
    assert all(r.has_entry for r in records)  # uptrend -> long signal
