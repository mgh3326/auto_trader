from __future__ import annotations

from unittest.mock import AsyncMock

import pandas as pd
import pytest


class _FakeRedis:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self.closed = False

    async def get(self, key: str) -> str | None:
        return self._values.get(key)

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool | None = None,
    ) -> bool:
        _ = ex
        if nx and key in self._values:
            return False
        self._values[key] = value
        return True

    async def close(self) -> None:
        self.closed = True


class _DummyOpenClawClient:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_scan_alert(self, message: str) -> str | None:
        self.messages.append(message)
        return f"scan-{len(self.messages)}"


def _make_ohlcv(closes: list[float]) -> pd.DataFrame:
    rows = len(closes)
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=rows, freq="D").date,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [100.0] * rows,
            "value": [1000.0] * rows,
        }
    )


@pytest.fixture
def scanner_env(monkeypatch: pytest.MonkeyPatch):
    from app.jobs import daily_scan

    openclaw = _DummyOpenClawClient()
    monkeypatch.setattr(daily_scan, "OpenClawClient", lambda: openclaw)

    scanner = daily_scan.DailyScanner()
    fake_redis = _FakeRedis()
    scanner._get_redis = AsyncMock(return_value=fake_redis)  # type: ignore[method-assign]

    monkeypatch.setattr(
        daily_scan.upbit_pairs,
        "COIN_TO_NAME_KR",
        {"BTC": "비트코인", "ETH": "이더리움", "XRP": "리플"},
        raising=False,
    )
    monkeypatch.setattr(
        daily_scan.upbit_pairs,
        "prime_upbit_constants",
        AsyncMock(return_value=None),
        raising=False,
    )

    monkeypatch.setattr(
        daily_scan.settings, "DAILY_SCAN_CRASH_THRESHOLD", 0.05, raising=False
    )
    monkeypatch.setattr(
        daily_scan.settings, "DAILY_SCAN_RSI_OVERBOUGHT", 70.0, raising=False
    )
    monkeypatch.setattr(
        daily_scan.settings, "DAILY_SCAN_RSI_OVERSOLD", 35.0, raising=False
    )
    monkeypatch.setattr(daily_scan.settings, "DAILY_SCAN_FNG_LOW", 10, raising=False)
    monkeypatch.setattr(daily_scan.settings, "DAILY_SCAN_FNG_HIGH", 80, raising=False)
    monkeypatch.setattr(
        daily_scan.settings, "DAILY_SCAN_TOP_COINS_COUNT", 30, raising=False
    )
    monkeypatch.setattr(daily_scan.settings, "DAILY_SCAN_ENABLED", True, raising=False)

    return scanner, openclaw, fake_redis, daily_scan


@pytest.mark.asyncio
async def test_check_overbought_holdings_sends_alert(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, openclaw, _redis, daily_scan = scanner_env

    monkeypatch.setattr(
        daily_scan,
        "fetch_my_coins",
        AsyncMock(return_value=[{"currency": "KRW"}, {"currency": "BTC"}]),
    )
    monkeypatch.setattr(
        daily_scan,
        "fetch_ohlcv",
        AsyncMock(return_value=_make_ohlcv([100.0] * 50)),
    )
    monkeypatch.setattr(daily_scan, "_calculate_rsi", lambda _: {"14": 75.0})

    alerts = await scanner.check_overbought_holdings("BTC_CTX")

    assert len(alerts) == 1
    assert "과매수" in alerts[0]
    assert len(openclaw.messages) == 1
    assert "BTC_CTX" in openclaw.messages[0]


@pytest.mark.asyncio
async def test_check_oversold_top30_sends_alert(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, openclaw, _redis, daily_scan = scanner_env

    monkeypatch.setattr(
        daily_scan,
        "fetch_top_traded_coins",
        AsyncMock(return_value=[{"market": "KRW-ETH"}]),
    )
    monkeypatch.setattr(
        daily_scan,
        "fetch_ohlcv",
        AsyncMock(return_value=_make_ohlcv([100.0] * 50)),
    )
    monkeypatch.setattr(daily_scan, "_calculate_rsi", lambda _: {"14": 25.0})

    alerts = await scanner.check_oversold_top30("BTC_CTX")

    assert len(alerts) == 1
    assert "과매도" in alerts[0]
    assert len(openclaw.messages) == 1
    assert "BTC_CTX" in openclaw.messages[0]


@pytest.mark.asyncio
async def test_check_price_crash_threshold_applies(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, openclaw, _redis, daily_scan = scanner_env

    monkeypatch.setattr(
        daily_scan,
        "fetch_top_traded_coins",
        AsyncMock(
            return_value=[
                {"market": "KRW-BTC"},
                {"market": "KRW-ETH"},
            ]
        ),
    )
    monkeypatch.setattr(
        daily_scan,
        "fetch_my_coins",
        AsyncMock(return_value=[{"currency": "KRW"}, {"currency": "XRP"}]),
    )
    monkeypatch.setattr(
        daily_scan,
        "fetch_multiple_tickers",
        AsyncMock(
            return_value=[
                {"market": "KRW-BTC", "signed_change_rate": 0.07},
                {"market": "KRW-ETH", "signed_change_rate": 0.03},
                {"market": "KRW-XRP", "signed_change_rate": -0.08},
            ]
        ),
    )

    alerts = await scanner.check_price_crash()

    assert len(alerts) == 2
    assert len(openclaw.messages) == 2
    assert any("+7.00%" in msg for msg in openclaw.messages)
    assert any("-8.00%" in msg for msg in openclaw.messages)
    assert all("+3.00%" not in msg for msg in openclaw.messages)


@pytest.mark.asyncio
async def test_check_fear_greed_extreme_only_alerts(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, openclaw, _redis, daily_scan = scanner_env

    monkeypatch.setattr(
        daily_scan,
        "get_fear_greed_index_impl",
        AsyncMock(
            return_value={
                "success": True,
                "current": {
                    "value": 8,
                    "classification": "Extreme Fear",
                    "date": "2026-02-16",
                },
            }
        ),
    )
    alerts = await scanner.check_fear_greed()
    assert len(alerts) == 1
    assert len(openclaw.messages) == 1

    monkeypatch.setattr(
        daily_scan,
        "get_fear_greed_index_impl",
        AsyncMock(
            return_value={
                "success": True,
                "current": {
                    "value": 50,
                    "classification": "Neutral",
                    "date": "2026-02-16",
                },
            }
        ),
    )
    alerts = await scanner.check_fear_greed()
    assert alerts == []
    assert len(openclaw.messages) == 1


@pytest.mark.asyncio
async def test_check_sma20_crossings_detects_golden_and_dead(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, openclaw, _redis, daily_scan = scanner_env

    golden = _make_ohlcv(([100.0] * 19) + [95.0, 105.0])
    dead = _make_ohlcv(([100.0] * 19) + [105.0, 95.0])

    monkeypatch.setattr(
        daily_scan,
        "fetch_my_coins",
        AsyncMock(return_value=[{"currency": "KRW"}]),
    )
    monkeypatch.setattr(
        daily_scan,
        "fetch_top_traded_coins",
        AsyncMock(return_value=[{"market": "KRW-BTC"}, {"market": "KRW-ETH"}]),
    )

    async def fake_fetch_ohlcv(market: str, days: int = 50):
        _ = days
        if market == "KRW-BTC":
            return golden
        return dead

    monkeypatch.setattr(daily_scan, "fetch_ohlcv", fake_fetch_ohlcv)

    alerts = await scanner.check_sma20_crossings()

    assert len(alerts) == 2
    assert any("골든크로스" in msg for msg in alerts)
    assert any("데드크로스" in msg for msg in alerts)
    assert len(openclaw.messages) == 2


@pytest.mark.asyncio
async def test_cooldown_blocks_duplicate_alert(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, openclaw, _redis, daily_scan = scanner_env

    monkeypatch.setattr(
        daily_scan,
        "fetch_my_coins",
        AsyncMock(return_value=[{"currency": "KRW"}, {"currency": "BTC"}]),
    )
    monkeypatch.setattr(
        daily_scan,
        "fetch_ohlcv",
        AsyncMock(return_value=_make_ohlcv([100.0] * 50)),
    )
    monkeypatch.setattr(daily_scan, "_calculate_rsi", lambda _: {"14": 75.0})

    first = await scanner.check_overbought_holdings("BTC_CTX")
    second = await scanner.check_overbought_holdings("BTC_CTX")

    assert len(first) == 1
    assert second == []
    assert len(openclaw.messages) == 1


@pytest.mark.asyncio
async def test_get_btc_context_builds_summary(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, _openclaw, _redis, daily_scan = scanner_env

    monkeypatch.setattr(
        daily_scan,
        "fetch_ohlcv",
        AsyncMock(return_value=_make_ohlcv(([100.0] * 49) + [110.0])),
    )
    monkeypatch.setattr(
        daily_scan,
        "fetch_multiple_tickers",
        AsyncMock(return_value=[{"market": "KRW-BTC", "signed_change_rate": 0.0123}]),
    )
    monkeypatch.setattr(daily_scan, "_calculate_rsi", lambda _: {"14": 55.5})
    monkeypatch.setattr(
        daily_scan,
        "_calculate_sma",
        lambda _close, periods=None: {"20": 101.0, "60": 98.5, "200": 90.2},
    )

    context = await scanner._get_btc_context()

    assert "RSI14 55.5" in context
    assert "SMA20 101.0" in context
    assert "SMA60 98.5" in context
    assert "SMA200 90.2" in context
    assert "+1.23%" in context


@pytest.mark.asyncio
async def test_run_methods_skip_when_disabled(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, _openclaw, _redis, daily_scan = scanner_env

    monkeypatch.setattr(daily_scan.settings, "DAILY_SCAN_ENABLED", False, raising=False)

    strategy_result = await scanner.run_strategy_scan()
    crash_result = await scanner.run_crash_detection()

    assert strategy_result == {"skipped": True, "reason": "disabled"}
    assert crash_result == {"skipped": True, "reason": "disabled"}
