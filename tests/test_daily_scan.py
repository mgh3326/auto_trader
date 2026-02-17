from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, call

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
        daily_scan.settings, "DAILY_SCAN_CRASH_HOLDING_THRESHOLD", 0.04, raising=False
    )
    monkeypatch.setattr(
        daily_scan.settings, "DAILY_SCAN_CRASH_TOP10_THRESHOLD", 0.06, raising=False
    )
    monkeypatch.setattr(
        daily_scan.settings, "DAILY_SCAN_CRASH_TOP30_THRESHOLD", 0.08, raising=False
    )
    monkeypatch.setattr(
        daily_scan.settings, "DAILY_SCAN_CRASH_TOP50_THRESHOLD", 0.10, raising=False
    )
    monkeypatch.setattr(
        daily_scan.settings, "DAILY_SCAN_CRASH_TOP100_THRESHOLD", 0.12, raising=False
    )
    monkeypatch.setattr(
        daily_scan.settings, "DAILY_SCAN_CRASH_TOP_RANK_LIMIT", 100, raising=False
    )
    monkeypatch.setattr(
        daily_scan.settings, "DAILY_SCAN_CRASH_NEAR_MISS_RATIO", 0.8, raising=False
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
                {"market": "KRW-XRP"},
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
async def test_check_price_crash_filters_non_tradable_holding_markets(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, _openclaw, _redis, daily_scan = scanner_env

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
        AsyncMock(return_value=[{"currency": "KRW"}, {"currency": "PCI"}]),
    )
    fetch_tickers = AsyncMock(return_value=[])
    monkeypatch.setattr(daily_scan, "fetch_multiple_tickers", fetch_tickers)

    await scanner.check_price_crash()

    fetch_tickers.assert_awaited_once_with(["KRW-BTC", "KRW-ETH"])


@pytest.mark.asyncio
async def test_check_price_crash_applies_rank_tiers_and_holding_override(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, openclaw, _redis, daily_scan = scanner_env

    top_coins = [{"market": f"KRW-C{i:03d}"} for i in range(1, 121)]
    fetch_tickers = AsyncMock(
        return_value=[
            {"market": "KRW-C050", "signed_change_rate": 0.10},
            {"market": "KRW-C051", "signed_change_rate": 0.11},
            {"market": "KRW-C115", "signed_change_rate": 0.05},
        ]
    )
    monkeypatch.setattr(
        daily_scan,
        "fetch_top_traded_coins",
        AsyncMock(return_value=top_coins),
    )
    monkeypatch.setattr(
        daily_scan,
        "fetch_my_coins",
        AsyncMock(return_value=[{"currency": "C115"}]),
    )
    monkeypatch.setattr(daily_scan, "fetch_multiple_tickers", fetch_tickers)

    alerts = await scanner.check_price_crash()

    assert len(alerts) == 2
    assert len(openclaw.messages) == 2
    assert any("C050(C050) 24h +10.00% — 급등 감지" in msg for msg in openclaw.messages)
    assert any("C115(C115) 24h +5.00% — 급등 감지" in msg for msg in openclaw.messages)
    assert all("C051(C051)" not in msg for msg in openclaw.messages)

    requested_markets = list(fetch_tickers.await_args.args[0])
    assert "KRW-C101" not in requested_markets
    assert "KRW-C115" in requested_markets
    assert len(requested_markets) == 101


@pytest.mark.asyncio
async def test_check_price_crash_logs_near_miss_for_tuning(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    scanner, openclaw, _redis, daily_scan = scanner_env

    monkeypatch.setattr(
        daily_scan,
        "fetch_top_traded_coins",
        AsyncMock(return_value=[{"market": "KRW-BTC"}]),
    )
    monkeypatch.setattr(
        daily_scan,
        "fetch_my_coins",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        daily_scan,
        "fetch_multiple_tickers",
        AsyncMock(return_value=[{"market": "KRW-BTC", "signed_change_rate": 0.055}]),
    )

    caplog.set_level(logging.INFO)
    alerts = await scanner.check_price_crash()

    assert alerts == []
    assert openclaw.messages == []
    assert any(
        "Crash scan near-miss market=KRW-BTC" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_check_sma20_crossings_filters_non_tradable_holding_markets(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, _openclaw, _redis, daily_scan = scanner_env

    monkeypatch.setattr(
        daily_scan,
        "fetch_top_traded_coins",
        AsyncMock(return_value=[{"market": "KRW-BTC"}]),
    )
    monkeypatch.setattr(
        daily_scan,
        "fetch_my_coins",
        AsyncMock(return_value=[{"currency": "KRW"}, {"currency": "PCI"}]),
    )
    fetch_ohlcv_mock = AsyncMock(return_value=_make_ohlcv([100.0] * 20))
    monkeypatch.setattr(daily_scan, "fetch_ohlcv", fetch_ohlcv_mock)

    await scanner.check_sma20_crossings()

    requested_markets = [
        str(call_args.args[0]) for call_args in fetch_ohlcv_mock.await_args_list
    ]
    assert requested_markets == ["KRW-BTC"]


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


@pytest.mark.asyncio
async def test_run_strategy_scan_sends_single_batched_alert(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, _openclaw, _redis, daily_scan = scanner_env

    monkeypatch.setattr(
        daily_scan,
        "now_kst",
        lambda: datetime(2026, 2, 16, 9, 30, tzinfo=timezone(timedelta(hours=9))),
        raising=False,
    )

    monkeypatch.setattr(
        scanner,
        "_get_btc_context",
        AsyncMock(return_value="BTC_CTX"),
        raising=False,
    )
    monkeypatch.setattr(
        scanner,
        "check_overbought_holdings",
        AsyncMock(return_value=["overbought message"]),
        raising=False,
    )
    monkeypatch.setattr(
        scanner,
        "check_oversold_top30",
        AsyncMock(return_value=["oversold message"]),
        raising=False,
    )
    monkeypatch.setattr(
        scanner,
        "check_fear_greed",
        AsyncMock(return_value=[]),
        raising=False,
    )
    monkeypatch.setattr(
        scanner,
        "check_sma20_crossings",
        AsyncMock(return_value=[]),
        raising=False,
    )
    send_mock = AsyncMock(return_value="scan-1")
    monkeypatch.setattr(scanner, "_send_alert", send_mock, raising=False)

    result = await scanner.run_strategy_scan()

    send_mock.assert_awaited_once()
    assert send_mock.await_args_list
    first_await = send_mock.await_args_list[0]
    assert first_await is not None
    batched_message = first_await.args[0]
    assert result == {
        "alerts_sent": 1,
        "details": [batched_message],
    }
    assert "크립토 스캔 (09:30)" in batched_message
    assert "매수 신호" in batched_message
    assert "매도 신호" in batched_message
    assert "overbought message" in batched_message
    assert "oversold message" in batched_message


@pytest.mark.asyncio
async def test_run_strategy_scan_does_not_send_when_no_alerts(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, _openclaw, _redis, _daily_scan = scanner_env

    monkeypatch.setattr(
        scanner,
        "_get_btc_context",
        AsyncMock(return_value="BTC_CTX"),
        raising=False,
    )
    monkeypatch.setattr(
        scanner,
        "check_overbought_holdings",
        AsyncMock(return_value=[]),
        raising=False,
    )
    monkeypatch.setattr(
        scanner,
        "check_oversold_top30",
        AsyncMock(return_value=[]),
        raising=False,
    )
    monkeypatch.setattr(
        scanner,
        "check_fear_greed",
        AsyncMock(return_value=[]),
        raising=False,
    )
    monkeypatch.setattr(
        scanner,
        "check_sma20_crossings",
        AsyncMock(return_value=[]),
        raising=False,
    )
    send_mock = AsyncMock(return_value="scan-1")
    monkeypatch.setattr(scanner, "_send_alert", send_mock, raising=False)

    result = await scanner.run_strategy_scan()

    assert result == {"alerts_sent": 0, "details": []}
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_strategy_scan_returns_zero_when_batched_send_fails(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, _openclaw, _redis, _daily_scan = scanner_env

    monkeypatch.setattr(
        scanner,
        "_get_btc_context",
        AsyncMock(return_value="BTC_CTX"),
        raising=False,
    )
    monkeypatch.setattr(
        scanner,
        "check_overbought_holdings",
        AsyncMock(return_value=["overbought message"]),
        raising=False,
    )
    monkeypatch.setattr(
        scanner,
        "check_oversold_top30",
        AsyncMock(return_value=[]),
        raising=False,
    )
    monkeypatch.setattr(
        scanner,
        "check_fear_greed",
        AsyncMock(return_value=[]),
        raising=False,
    )
    monkeypatch.setattr(
        scanner,
        "check_sma20_crossings",
        AsyncMock(return_value=[]),
        raising=False,
    )

    send_mock = AsyncMock(return_value=None)
    record_mock = AsyncMock()
    monkeypatch.setattr(scanner, "_send_alert", send_mock, raising=False)
    monkeypatch.setattr(scanner, "_record_alert", record_mock, raising=False)

    result = await scanner.run_strategy_scan()

    assert result == {"alerts_sent": 0, "details": []}
    send_mock.assert_awaited_once()
    record_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_strategy_scan_records_deduped_cooldowns_on_batch_success(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, _openclaw, _redis, _daily_scan = scanner_env

    monkeypatch.setattr(
        scanner,
        "_get_btc_context",
        AsyncMock(return_value="BTC_CTX"),
        raising=False,
    )

    async def fake_overbought(
        btc_ctx: str,
        send_immediately: bool = True,
        pending_cooldowns: list[tuple[str, str]] | None = None,
    ) -> list[str]:
        _ = btc_ctx
        assert not send_immediately
        assert pending_cooldowns is not None
        pending_cooldowns.extend(
            [
                ("BTC", "overbought"),
                ("BTC", "overbought"),
            ]
        )
        return ["overbought message"]

    async def fake_oversold(
        btc_ctx: str,
        send_immediately: bool = True,
        pending_cooldowns: list[tuple[str, str]] | None = None,
    ) -> list[str]:
        _ = btc_ctx
        assert not send_immediately
        assert pending_cooldowns is not None
        pending_cooldowns.extend(
            [
                ("ETH", "oversold"),
                ("BTC", "overbought"),
            ]
        )
        return ["oversold message"]

    monkeypatch.setattr(scanner, "check_overbought_holdings", fake_overbought)
    monkeypatch.setattr(scanner, "check_oversold_top30", fake_oversold)
    monkeypatch.setattr(
        scanner,
        "check_fear_greed",
        AsyncMock(return_value=[]),
        raising=False,
    )
    monkeypatch.setattr(
        scanner,
        "check_sma20_crossings",
        AsyncMock(return_value=[]),
        raising=False,
    )

    send_mock = AsyncMock(return_value="scan-1")
    record_mock = AsyncMock()
    monkeypatch.setattr(scanner, "_send_alert", send_mock, raising=False)
    monkeypatch.setattr(scanner, "_record_alert", record_mock, raising=False)

    result = await scanner.run_strategy_scan()

    send_mock.assert_awaited_once()
    assert send_mock.await_args_list
    first_await = send_mock.await_args_list[0]
    assert first_await is not None
    batched_message = first_await.args[0]
    assert result == {
        "alerts_sent": 1,
        "details": [batched_message],
    }
    assert "overbought message" in batched_message
    assert "oversold message" in batched_message
    assert record_mock.await_args_list == [
        call("BTC", "overbought"),
        call("ETH", "oversold"),
    ]


@pytest.mark.asyncio
async def test_run_crash_detection_sends_single_batched_alert(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, _openclaw, _redis, daily_scan = scanner_env

    monkeypatch.setattr(
        daily_scan,
        "now_kst",
        lambda: datetime(2026, 2, 16, 9, 30, tzinfo=timezone(timedelta(hours=9))),
        raising=False,
    )

    async def fake_check_price_crash(
        send_immediately: bool = True,
        pending_cooldowns: list[tuple[str, str]] | None = None,
    ) -> list[str]:
        assert not send_immediately
        assert pending_cooldowns is not None
        pending_cooldowns.extend(
            [
                ("BTC", "crash"),
                ("XRP", "crash"),
                ("BTC", "crash"),
            ]
        )
        return [
            "비트코인(BTC) 24h +7.00% — 급등 감지",
            "리플(XRP) 24h -8.00% — 급락 감지",
        ]

    monkeypatch.setattr(scanner, "check_price_crash", fake_check_price_crash)
    send_mock = AsyncMock(return_value="scan-1")
    record_mock = AsyncMock()
    monkeypatch.setattr(scanner, "_send_alert", send_mock, raising=False)
    monkeypatch.setattr(scanner, "_record_alert", record_mock, raising=False)

    result = await scanner.run_crash_detection()

    send_mock.assert_awaited_once()
    assert send_mock.await_args_list
    first_await = send_mock.await_args_list[0]
    assert first_await is not None
    batched_message = first_await.args[0]
    assert result == {
        "alerts_sent": 1,
        "details": [batched_message],
    }
    assert "크래시 감지 스캔 (09:30)" in batched_message
    assert "변동성 경보" in batched_message
    assert "- 비트코인(BTC) 24h +7.00% — 급등 감지" in batched_message
    assert "- 리플(XRP) 24h -8.00% — 급락 감지" in batched_message
    assert "🚨" not in batched_message
    assert record_mock.await_args_list == [
        call("BTC", "crash"),
        call("XRP", "crash"),
    ]


@pytest.mark.asyncio
async def test_run_crash_detection_does_not_send_when_no_alerts(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, _openclaw, _redis, _daily_scan = scanner_env

    monkeypatch.setattr(
        scanner,
        "check_price_crash",
        AsyncMock(return_value=[]),
        raising=False,
    )
    send_mock = AsyncMock(return_value="scan-1")
    record_mock = AsyncMock()
    monkeypatch.setattr(scanner, "_send_alert", send_mock, raising=False)
    monkeypatch.setattr(scanner, "_record_alert", record_mock, raising=False)

    result = await scanner.run_crash_detection()

    assert result == {"alerts_sent": 0, "details": []}
    send_mock.assert_not_awaited()
    record_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_crash_detection_returns_zero_when_batched_send_fails(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, _openclaw, _redis, _daily_scan = scanner_env

    async def fake_check_price_crash(
        send_immediately: bool = True,
        pending_cooldowns: list[tuple[str, str]] | None = None,
    ) -> list[str]:
        assert not send_immediately
        assert pending_cooldowns is not None
        pending_cooldowns.extend(
            [
                ("BTC", "crash"),
                ("BTC", "crash"),
            ]
        )
        return ["비트코인(BTC) 24h +7.00% — 급등 감지"]

    monkeypatch.setattr(scanner, "check_price_crash", fake_check_price_crash)
    send_mock = AsyncMock(return_value=None)
    record_mock = AsyncMock()
    monkeypatch.setattr(scanner, "_send_alert", send_mock, raising=False)
    monkeypatch.setattr(scanner, "_record_alert", record_mock, raising=False)

    result = await scanner.run_crash_detection()

    assert result == {"alerts_sent": 0, "details": []}
    send_mock.assert_awaited_once()
    record_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_crash_detection_sends_single_batched_alert(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, _openclaw, _redis, daily_scan = scanner_env

    monkeypatch.setattr(
        daily_scan,
        "now_kst",
        lambda: datetime(2026, 2, 16, 9, 30, tzinfo=timezone(timedelta(hours=9))),
        raising=False,
    )

    async def fake_check_price_crash(
        send_immediately: bool = True,
        pending_cooldowns: list[tuple[str, str]] | None = None,
    ) -> list[str]:
        assert not send_immediately
        assert pending_cooldowns is not None
        pending_cooldowns.extend(
            [
                ("BTC", "crash"),
                ("XRP", "crash"),
                ("BTC", "crash"),
            ]
        )
        return [
            "비트코인(BTC) 24h +7.00% — 급등 감지",
            "리플(XRP) 24h -8.00% — 급락 감지",
        ]

    monkeypatch.setattr(scanner, "check_price_crash", fake_check_price_crash)
    send_mock = AsyncMock(return_value="scan-1")
    record_mock = AsyncMock()
    monkeypatch.setattr(scanner, "_send_alert", send_mock, raising=False)
    monkeypatch.setattr(scanner, "_record_alert", record_mock, raising=False)

    result = await scanner.run_crash_detection()

    send_mock.assert_awaited_once()
    assert send_mock.await_args_list
    first_await = send_mock.await_args_list[0]
    assert first_await is not None
    batched_message = first_await.args[0]
    assert result == {
        "alerts_sent": 1,
        "details": [batched_message],
    }
    assert "크래시 감지 스캔 (09:30)" in batched_message
    assert "변동성 경보" in batched_message
    assert "- 비트코인(BTC) 24h +7.00% — 급등 감지" in batched_message
    assert "- 리플(XRP) 24h -8.00% — 급락 감지" in batched_message
    assert "🚨" not in batched_message
    assert record_mock.await_args_list == [
        call("BTC", "crash"),
        call("XRP", "crash"),
    ]


@pytest.mark.asyncio
async def test_run_crash_detection_does_not_send_when_no_alerts(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, _openclaw, _redis, _daily_scan = scanner_env

    monkeypatch.setattr(
        scanner,
        "check_price_crash",
        AsyncMock(return_value=[]),
        raising=False,
    )
    send_mock = AsyncMock(return_value="scan-1")
    record_mock = AsyncMock()
    monkeypatch.setattr(scanner, "_send_alert", send_mock, raising=False)
    monkeypatch.setattr(scanner, "_record_alert", record_mock, raising=False)

    result = await scanner.run_crash_detection()

    assert result == {"alerts_sent": 0, "details": []}
    send_mock.assert_not_awaited()
    record_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_crash_detection_returns_zero_when_batched_send_fails(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, _openclaw, _redis, _daily_scan = scanner_env

    async def fake_check_price_crash(
        send_immediately: bool = True,
        pending_cooldowns: list[tuple[str, str]] | None = None,
    ) -> list[str]:
        assert not send_immediately
        assert pending_cooldowns is not None
        pending_cooldowns.extend(
            [
                ("BTC", "crash"),
                ("BTC", "crash"),
            ]
        )
        return ["비트코인(BTC) 24h +7.00% — 급등 감지"]

    monkeypatch.setattr(scanner, "check_price_crash", fake_check_price_crash)
    send_mock = AsyncMock(return_value=None)
    record_mock = AsyncMock()
    monkeypatch.setattr(scanner, "_send_alert", send_mock, raising=False)
    monkeypatch.setattr(scanner, "_record_alert", record_mock, raising=False)

    result = await scanner.run_crash_detection()

    assert result == {"alerts_sent": 0, "details": []}
    send_mock.assert_awaited_once()
    record_mock.assert_not_awaited()
