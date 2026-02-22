from __future__ import annotations

from unittest.mock import AsyncMock

import pandas as pd
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


def _make_ohlcv(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=len(closes), freq="D"),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [100.0] * len(closes),
            "value": [1000.0] * len(closes),
        }
    )


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

    mock_kis = AsyncMock()
    mock_kis.inquire_price.return_value = pd.DataFrame(
        [{"code": "005930", "close": 55000.0}]
    ).set_index("code")
    mock_kis.inquire_daily_itemchartprice.return_value = _make_ohlcv(
        [1, 2, 3, 4, 5] * 20
    )
    scanner._kis = mock_kis

    mock_us_ohlcv = AsyncMock(return_value=_make_ohlcv([1, 2, 3, 4, 5] * 20))
    mock_us_price = AsyncMock(return_value=pd.DataFrame([{"close": 190.0}]))
    monkeypatch.setattr(
        watch_scanner_module.yahoo_service, "fetch_price", mock_us_price
    )
    monkeypatch.setattr(
        watch_scanner_module.yahoo_service, "fetch_ohlcv", mock_us_ohlcv
    )

    mock_crypto_price = AsyncMock(return_value=pd.DataFrame([{"close": 91000000.0}]))
    mock_crypto_ohlcv = AsyncMock(return_value=_make_ohlcv([1, 2, 3, 4, 5] * 20))
    monkeypatch.setattr(
        watch_scanner_module.upbit_service,
        "fetch_price",
        mock_crypto_price,
    )
    monkeypatch.setattr(
        watch_scanner_module.upbit_service,
        "fetch_ohlcv",
        mock_crypto_ohlcv,
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

    mock_kis.inquire_price.assert_awaited_once_with("005930", market="UN")
    mock_kis.inquire_daily_itemchartprice.assert_awaited_once_with(
        code="005930",
        market="UN",
        n=250,
        period="D",
    )
    mock_us_price.assert_awaited_once_with("AMZN")
    mock_crypto_price.assert_awaited_once_with("KRW-BTC")


@pytest.mark.asyncio
async def test_get_price_us_raises_when_yahoo_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import watch_scanner as watch_scanner_module

    scanner = WatchScanner()
    monkeypatch.setattr(
        watch_scanner_module.yahoo_service,
        "fetch_price",
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
    mock_crypto_ohlcv = AsyncMock(return_value=_make_ohlcv([1, 2, 3, 4, 5] * 20))
    monkeypatch.setattr(
        watch_scanner_module.upbit_service,
        "fetch_ohlcv",
        mock_crypto_ohlcv,
    )

    rsi = await scanner._get_rsi("BTC", "crypto")

    assert rsi is not None
    await_args = mock_crypto_ohlcv.await_args
    assert await_args is not None
    assert await_args.kwargs["days"] <= 200
