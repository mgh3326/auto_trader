from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.jobs.watch_scanner import WatchScanner


class _FakeWatchService:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self._rows_by_market: dict[str, list[dict[str, object]]] = {
            "crypto": list(rows or []),
            "kr": [],
            "us": [],
        }
        self.removed_fields: list[tuple[str, str]] = []
        self.closed = False

    async def get_watches_for_market(self, market: str) -> list[dict[str, object]]:
        return list(self._rows_by_market.get(market, []))

    async def trigger_and_remove(self, market: str, field: str) -> bool:
        self.removed_fields.append((market, field))
        return True

    async def close(self) -> None:
        self.closed = True


class _FakeOpenClawClient:
    def __init__(self, success: bool = True) -> None:
        self._success = success
        self.messages: list[str] = []

    async def send_scan_alert(self, message: str) -> str | None:
        self.messages.append(message)
        return "scan-1" if self._success else None

    async def send_watch_alert(self, message: str) -> str | None:
        self.messages.append(message)
        return "watch-1" if self._success else None


@pytest.mark.asyncio
async def test_scan_market_sends_single_batched_message_and_removes_only_triggered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = WatchScanner()
    scanner._watch_service = _FakeWatchService(
        rows=[
            {
                "symbol": "BTC",
                "condition_type": "price_below",
                "threshold": 100.0,
                "field": "BTC:price_below:100",
            },
            {
                "symbol": "ETH",
                "condition_type": "rsi_above",
                "threshold": 70.0,
                "field": "ETH:rsi_above:70",
            },
        ]
    )
    scanner._openclaw = _FakeOpenClawClient(success=True)

    monkeypatch.setattr(scanner, "_is_market_open", lambda market: True)
    monkeypatch.setattr(scanner, "_get_price", AsyncMock(return_value=90.0))
    monkeypatch.setattr(scanner, "_get_rsi", AsyncMock(return_value=72.5))

    result = await scanner.scan_market("crypto")

    assert result["alerts_sent"] == 2
    assert len(scanner._openclaw.messages) == 1
    assert scanner._watch_service.removed_fields == [
        ("crypto", "BTC:price_below:100"),
        ("crypto", "ETH:rsi_above:70"),
    ]


@pytest.mark.asyncio
async def test_run_scans_all_markets_and_skips_closed_market(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = WatchScanner()
    scanner._watch_service = _FakeWatchService(rows=[])
    scanner._openclaw = _FakeOpenClawClient(success=True)

    monkeypatch.setattr(scanner, "_is_market_open", lambda market: market != "us")
    monkeypatch.setattr(scanner, "_get_price", AsyncMock(return_value=None))
    monkeypatch.setattr(scanner, "_get_rsi", AsyncMock(return_value=None))

    result = await scanner.run()

    assert set(result.keys()) == {"crypto", "kr", "us"}
    assert result["us"] == {
        "market": "us",
        "skipped": True,
        "reason": "market_closed",
    }
    assert result["crypto"]["alerts_sent"] == 0
    assert result["kr"]["alerts_sent"] == 0


@pytest.mark.asyncio
async def test_get_price_and_rsi_use_market_specific_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import watch_scanner as watch_scanner_module

    scanner = WatchScanner()

    async def _quote_side_effect(*, symbol: str, market: str):
        if market == "equity_kr":
            return SimpleNamespace(price=55000.0)
        if market == "equity_us":
            return SimpleNamespace(price=190.0)
        if market == "crypto":
            return SimpleNamespace(price=91000000.0)
        raise RuntimeError(f"unexpected symbol/market: {symbol}/{market}")

    mock_get_quote = AsyncMock(side_effect=_quote_side_effect)
    mock_get_ohlcv = AsyncMock(
        return_value=[SimpleNamespace(close=float(x)) for x in [1, 2, 3, 4, 5] * 20]
    )
    monkeypatch.setattr(
        watch_scanner_module.market_data_service, "get_quote", mock_get_quote
    )
    monkeypatch.setattr(
        watch_scanner_module.market_data_service, "get_ohlcv", mock_get_ohlcv
    )

    assert await scanner._get_price("005930", "kr") == 55000.0
    assert await scanner._get_price("AMZN", "us") == 190.0
    assert await scanner._get_price("BTC", "crypto") == 91000000.0

    kr_rsi = await scanner._get_rsi("005930", "kr")
    us_rsi = await scanner._get_rsi("AMZN", "us")
    crypto_rsi = await scanner._get_rsi("BTC", "crypto")
    assert kr_rsi is not None
    assert us_rsi is not None
    assert crypto_rsi is not None

    mock_get_quote.assert_any_await(symbol="005930", market="equity_kr")
    mock_get_quote.assert_any_await(symbol="AMZN", market="equity_us")
    mock_get_quote.assert_any_await(symbol="KRW-BTC", market="crypto")
    mock_get_ohlcv.assert_any_await(
        symbol="005930",
        market="equity_kr",
        period="day",
        count=250,
    )
    mock_get_ohlcv.assert_any_await(
        symbol="AMZN",
        market="equity_us",
        period="day",
        count=250,
    )
    mock_get_ohlcv.assert_any_await(
        symbol="KRW-BTC",
        market="crypto",
        period="day",
        count=200,
    )


@pytest.mark.asyncio
async def test_get_price_us_raises_when_yahoo_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import watch_scanner as watch_scanner_module

    scanner = WatchScanner()
    monkeypatch.setattr(
        watch_scanner_module.market_data_service,
        "get_quote",
        AsyncMock(side_effect=RuntimeError("timeout")),
    )

    with pytest.raises(RuntimeError, match="timeout"):
        await scanner._get_price("AAPL", "us")


@pytest.mark.asyncio
async def test_get_rsi_crypto_uses_supported_ohlcv_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import watch_scanner as watch_scanner_module

    scanner = WatchScanner()
    mock_crypto_ohlcv = AsyncMock(
        return_value=[SimpleNamespace(close=float(x)) for x in [1, 2, 3, 4, 5] * 20]
    )
    monkeypatch.setattr(
        watch_scanner_module.market_data_service,
        "get_ohlcv",
        mock_crypto_ohlcv,
    )

    rsi = await scanner._get_rsi("BTC", "crypto")

    assert rsi is not None
    await_args = mock_crypto_ohlcv.await_args
    assert await_args is not None
    assert await_args.kwargs["count"] <= 200
    assert await_args.kwargs["market"] == "crypto"
    assert await_args.kwargs["symbol"] == "KRW-BTC"


@pytest.mark.asyncio
async def test_watch_scanner_uses_market_data_domain_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import watch_scanner as watch_scanner_module

    scanner = WatchScanner()
    domain_get_quote = AsyncMock(return_value=SimpleNamespace(price=91000000.0))
    domain_get_ohlcv = AsyncMock(
        return_value=[SimpleNamespace(close=float(x)) for x in [1, 2, 3, 4, 5] * 20]
    )

    monkeypatch.setattr(
        watch_scanner_module,
        "market_data_service",
        SimpleNamespace(get_quote=domain_get_quote, get_ohlcv=domain_get_ohlcv),
        raising=False,
    )

    assert await scanner._get_price("BTC", "crypto") == 91000000.0
    assert await scanner._get_rsi("BTC", "crypto") is not None

    domain_get_quote.assert_awaited_once_with(symbol="KRW-BTC", market="crypto")
    domain_get_ohlcv.assert_awaited_once_with(
        symbol="KRW-BTC",
        market="crypto",
        period="day",
        count=200,
    )
