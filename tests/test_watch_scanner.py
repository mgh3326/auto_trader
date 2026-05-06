from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.jobs.watch_scanner import WatchScanner
from app.services.openclaw_client import WatchAlertDeliveryResult


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
    def __init__(self, status: str = "success") -> None:
        self._status = status
        self.messages: list[str] = []
        self.triggered_payloads: list[list[dict[str, object]]] = []

    async def send_scan_alert(self, message: str) -> str | None:
        self.messages.append(message)
        return "scan-1" if self._status == "success" else None

    async def send_watch_alert(self, message: str) -> str | None:
        self.messages.append(message)
        return "watch-1" if self._status == "success" else None

    async def send_watch_alert_to_router(
        self,
        *,
        message: str,
        market: str,
        triggered: list[dict[str, object]],
        as_of: str,
        correlation_id: str | None = None,
        intents: list[dict[str, object]] | None = None,
    ) -> WatchAlertDeliveryResult:
        _ = market, as_of, correlation_id, intents
        self.messages.append(message)
        self.triggered_payloads.append(triggered)
        if self._status == "success":
            return WatchAlertDeliveryResult(status="success", request_id="watch-1")
        if self._status == "skipped":
            return WatchAlertDeliveryResult(
                status="skipped",
                reason="router_not_configured",
            )
        return WatchAlertDeliveryResult(status="failed", reason="request_failed")


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
    scanner._openclaw = _FakeOpenClawClient(status="success")

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
    assert scanner._openclaw.triggered_payloads[0][0]["target_kind"] == "asset"


@pytest.mark.asyncio
async def test_scan_market_dispatches_asset_trade_value_index_and_fx_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = WatchScanner()
    scanner._watch_service = _FakeWatchService()
    scanner._watch_service._rows_by_market["kr"] = [
        {
            "target_kind": "asset",
            "symbol": "005930",
            "condition_type": "trade_value_above",
            "threshold": 1_000_000.0,
            "field": "asset:005930:trade_value_above:1000000",
        },
        {
            "target_kind": "index",
            "symbol": "KOSPI",
            "condition_type": "price_below",
            "threshold": 6200.0,
            "field": "index:KOSPI:price_below:6200",
        },
        {
            "target_kind": "fx",
            "symbol": "USDKRW",
            "condition_type": "price_above",
            "threshold": 1478.0,
            "field": "fx:USDKRW:price_above:1478",
        },
    ]
    scanner._openclaw = _FakeOpenClawClient(status="success")

    monkeypatch.setattr(scanner, "_is_market_open", lambda market: True)
    monkeypatch.setattr(
        scanner, "_get_trade_value", AsyncMock(return_value=1_500_000.0)
    )
    monkeypatch.setattr(scanner, "_get_index_price", AsyncMock(return_value=6176.75))
    monkeypatch.setattr(scanner, "_get_fx_price", AsyncMock(return_value=1479.5))

    result = await scanner.scan_market("kr")

    assert result["alerts_sent"] == 3
    assert scanner._watch_service.removed_fields == [
        ("kr", "asset:005930:trade_value_above:1000000"),
        ("kr", "index:KOSPI:price_below:6200"),
        ("kr", "fx:USDKRW:price_above:1478"),
    ]
    payload = scanner._openclaw.triggered_payloads[0]
    assert [row["target_kind"] for row in payload] == ["asset", "index", "fx"]
    assert [row["symbol"] for row in payload] == ["005930", "KOSPI", "USDKRW"]


@pytest.mark.asyncio
async def test_scan_market_allows_fx_watch_when_kr_equity_market_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = WatchScanner()
    scanner._watch_service = _FakeWatchService()
    scanner._watch_service._rows_by_market["kr"] = [
        {
            "target_kind": "fx",
            "symbol": "USDKRW",
            "condition_type": "price_above",
            "threshold": 1478.0,
            "field": "fx:USDKRW:price_above:1478",
        }
    ]
    scanner._openclaw = _FakeOpenClawClient(status="success")

    monkeypatch.setattr(scanner, "_is_market_open", lambda market: False)
    monkeypatch.setattr(scanner, "_get_fx_price", AsyncMock(return_value=1479.5))

    result = await scanner.scan_market("kr")

    assert result["alerts_sent"] == 1
    assert scanner._watch_service.removed_fields == [
        ("kr", "fx:USDKRW:price_above:1478")
    ]
    assert scanner._openclaw.triggered_payloads[0][0]["target_kind"] == "fx"


@pytest.mark.asyncio
async def test_get_index_price_uses_market_index_service_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import watch_scanner as watch_scanner_module

    scanner = WatchScanner()
    mock_get_kr_index_quote = AsyncMock(return_value={"current": 6176.75})
    monkeypatch.setattr(
        watch_scanner_module.market_index_service,
        "get_kr_index_quote",
        mock_get_kr_index_quote,
    )

    current = await scanner._get_index_price("kospi", "kr")

    assert current == pytest.approx(6176.75)
    mock_get_kr_index_quote.assert_awaited_once_with("KOSPI")


@pytest.mark.asyncio
async def test_scan_market_skips_unsupported_target_metric_without_removing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = WatchScanner()
    scanner._watch_service = _FakeWatchService(
        rows=[
            {
                "target_kind": "index",
                "symbol": "KOSPI",
                "condition_type": "rsi_below",
                "threshold": 30.0,
                "field": "index:KOSPI:rsi_below:30",
            }
        ]
    )
    scanner._watch_service._rows_by_market["kr"] = (
        scanner._watch_service._rows_by_market["crypto"]
    )
    scanner._openclaw = _FakeOpenClawClient(status="success")

    monkeypatch.setattr(scanner, "_is_market_open", lambda market: True)
    monkeypatch.setattr(scanner, "_get_rsi", AsyncMock(return_value=20.0))

    result = await scanner.scan_market("kr")

    assert result["reason"] == "no_triggered_alerts"
    assert scanner._watch_service.removed_fields == []
    assert scanner._openclaw.messages == []


@pytest.mark.asyncio
async def test_run_scans_all_markets_and_skips_closed_market(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = WatchScanner()
    scanner._watch_service = _FakeWatchService(rows=[])
    scanner._openclaw = _FakeOpenClawClient(status="success")

    monkeypatch.setattr(scanner, "_is_market_open", lambda market: market != "us")
    monkeypatch.setattr(scanner, "_get_price", AsyncMock(return_value=None))
    monkeypatch.setattr(scanner, "_get_rsi", AsyncMock(return_value=None))

    result = await scanner.run()

    assert set(result.keys()) == {"crypto", "kr", "us"}
    assert result["us"] == {
        "market": "us",
        "status": "skipped",
        "skipped": True,
        "reason": "market_closed",
        "failed_lookups": 0,
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

    assert await scanner._get_price("005930", "kr") == pytest.approx(55000.0)
    assert await scanner._get_price("AMZN", "us") == pytest.approx(190.0)
    assert await scanner._get_price("BTC", "crypto") == pytest.approx(91000000.0)

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
async def test_scan_market_us_yahoo_failure_does_not_abort_other_watches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = WatchScanner()
    scanner._watch_service = _FakeWatchService()
    scanner._watch_service._rows_by_market["us"] = [
        {
            "target_kind": "asset",
            "symbol": "BADTKR",
            "condition_type": "price_below",
            "threshold": 100.0,
            "field": "asset:BADTKR:price_below:100",
        },
        {
            "target_kind": "asset",
            "symbol": "AAPL",
            "condition_type": "price_below",
            "threshold": 200.0,
            "field": "asset:AAPL:price_below:200",
        },
    ]
    scanner._openclaw = _FakeOpenClawClient(status="success")

    monkeypatch.setattr(scanner, "_is_market_open", lambda market: True)

    async def _price_side_effect(symbol: str, market: str) -> float:
        if symbol == "BADTKR":
            raise RuntimeError("US watch price fetch failed for BADTKR: invalid close")
        return 150.0

    monkeypatch.setattr(
        scanner, "_get_price", AsyncMock(side_effect=_price_side_effect)
    )

    result = await scanner.scan_market("us")

    assert result["alerts_sent"] == 1
    assert result["status"] == "success"
    assert result.get("failed_lookups") == 1
    assert scanner._watch_service.removed_fields == [
        ("us", "asset:AAPL:price_below:200"),
    ]


@pytest.mark.asyncio
async def test_run_continues_other_markets_when_scan_market_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = WatchScanner()
    scanner._watch_service = _FakeWatchService(rows=[])
    scanner._openclaw = _FakeOpenClawClient(status="success")

    original_scan_market = scanner.scan_market

    async def _scan_market(market: str) -> dict[str, object]:
        if market == "us":
            raise RuntimeError("simulated unexpected scanner error")
        return await original_scan_market(market)

    monkeypatch.setattr(scanner, "scan_market", _scan_market)
    monkeypatch.setattr(scanner, "_is_market_open", lambda market: True)
    monkeypatch.setattr(scanner, "_get_price", AsyncMock(return_value=None))

    result = await scanner.run()

    assert set(result.keys()) == {"crypto", "kr", "us"}
    assert result["us"]["status"] == "failed"
    assert result["us"]["reason"] == "scan_aborted"
    assert result["crypto"]["alerts_sent"] == 0
    assert result["kr"]["alerts_sent"] == 0


@pytest.mark.asyncio
async def test_scan_market_records_failed_lookups_in_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = WatchScanner()
    scanner._watch_service = _FakeWatchService()
    scanner._watch_service._rows_by_market["crypto"] = [
        {
            "target_kind": "asset",
            "symbol": "BTC",
            "condition_type": "price_below",
            "threshold": 100.0,
            "field": "asset:BTC:price_below:100",
        },
        {
            "target_kind": "asset",
            "symbol": "ETH",
            "condition_type": "price_below",
            "threshold": 50.0,
            "field": "asset:ETH:price_below:50",
        },
    ]
    scanner._openclaw = _FakeOpenClawClient(status="success")

    monkeypatch.setattr(scanner, "_is_market_open", lambda market: True)

    async def _price_side_effect(symbol: str, market: str) -> float:
        raise RuntimeError(f"simulated failure for {symbol}")

    monkeypatch.setattr(
        scanner, "_get_price", AsyncMock(side_effect=_price_side_effect)
    )

    result = await scanner.scan_market("crypto")

    assert "failed_lookups" in result
    assert result["failed_lookups"] == 2
    assert result["status"] == "skipped"
    assert result["reason"] == "no_triggered_alerts"


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

    assert await scanner._get_price("BTC", "crypto") == pytest.approx(91000000.0)
    assert await scanner._get_rsi("BTC", "crypto") is not None

    domain_get_quote.assert_awaited_once_with(symbol="KRW-BTC", market="crypto")
    domain_get_ohlcv.assert_awaited_once_with(
        symbol="KRW-BTC",
        market="crypto",
        period="day",
        count=200,
    )


@pytest.mark.asyncio
async def test_scan_market_keeps_watch_records_when_n8n_delivery_failed(
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
            }
        ]
    )
    scanner._openclaw = _FakeOpenClawClient(status="failed")

    monkeypatch.setattr(scanner, "_is_market_open", lambda market: True)
    monkeypatch.setattr(scanner, "_get_price", AsyncMock(return_value=90.0))

    result = await scanner.scan_market("crypto")

    assert result["alerts_sent"] == 0
    assert result["status"] == "failed"
    assert scanner._watch_service.removed_fields == []


@pytest.mark.asyncio
async def test_scan_market_keeps_watch_records_when_n8n_delivery_skipped(
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
            }
        ]
    )
    scanner._openclaw = _FakeOpenClawClient(status="skipped")

    monkeypatch.setattr(scanner, "_is_market_open", lambda market: True)
    monkeypatch.setattr(scanner, "_get_price", AsyncMock(return_value=90.0))

    result = await scanner.scan_market("crypto")

    assert result["alerts_sent"] == 0
    assert result["status"] == "skipped"
    assert result["reason"] == "router_not_configured"
    assert scanner._watch_service.removed_fields == []


class TestScannerWithCreateOrderIntent:
    @pytest.mark.asyncio
    async def test_create_order_intent_previewed_branches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from contextlib import asynccontextmanager
        from decimal import Decimal

        from app.services.watch_order_intent_service import IntentEmissionResult

        scanner = WatchScanner()
        scanner._watch_service = _FakeWatchService()
        field = "asset:005930:price_below:70000"
        scanner._watch_service._rows_by_market["kr"] = [
            {
                "market": "kr",
                "target_kind": "asset",
                "symbol": "005930",
                "condition_type": "price_below",
                "threshold": 70000.0,
                "field": field,
                "raw_payload": '{"action":"create_order_intent","side":"buy","quantity":1}',
            }
        ]
        scanner._openclaw = _FakeOpenClawClient(status="success")

        monkeypatch.setattr(scanner, "_is_market_open", lambda market: True)
        monkeypatch.setattr(scanner, "_get_price", AsyncMock(return_value=69000.0))

        emission = IntentEmissionResult(
            status="previewed",
            ledger_id=123,
            correlation_id="corr-1",
            idempotency_key="key-1",
            market="kr",
            symbol="005930",
            side="buy",
            quantity=Decimal("1"),
            limit_price=Decimal("70000"),
            blocked_by=None,
            reason=None,
        )
        mock_emit = AsyncMock(return_value=emission)

        @asynccontextmanager
        async def fake_session():
            yield None, lambda db: SimpleNamespace(emit_intent=mock_emit)

        monkeypatch.setattr(scanner, "_intent_session", fake_session)

        # Patch send_watch_alert_to_router to capture intents
        captured_intents = []

        async def fake_send_router(**kwargs):
            captured_intents.extend(kwargs.get("intents", []))
            return WatchAlertDeliveryResult(status="success", request_id="watch-1")

        monkeypatch.setattr(
            scanner._openclaw, "send_watch_alert_to_router", fake_send_router
        )

        result = await scanner.scan_market("kr")

        assert result["alerts_sent"] == 1
        assert scanner._watch_service.removed_fields == [("kr", field)]
        assert len(captured_intents) == 1
        assert captured_intents[0]["status"] == "previewed"
        assert captured_intents[0]["ledger_id"] == 123

    @pytest.mark.asyncio
    async def test_failed_intent_keeps_watch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from contextlib import asynccontextmanager
        from decimal import Decimal

        from app.services.watch_order_intent_service import IntentEmissionResult

        scanner = WatchScanner()
        scanner._watch_service = _FakeWatchService()
        field = "asset:005930:price_below:70000"
        scanner._watch_service._rows_by_market["kr"] = [
            {
                "market": "kr",
                "target_kind": "asset",
                "symbol": "005930",
                "condition_type": "price_below",
                "threshold": 70000.0,
                "field": field,
                "raw_payload": '{"action":"create_order_intent","side":"buy","quantity":100,"max_notional_krw":10000}',
            }
        ]
        scanner._openclaw = _FakeOpenClawClient(status="success")

        monkeypatch.setattr(scanner, "_is_market_open", lambda market: True)
        monkeypatch.setattr(scanner, "_get_price", AsyncMock(return_value=69000.0))

        emission = IntentEmissionResult(
            status="failed",
            ledger_id=456,
            correlation_id="corr-fail",
            idempotency_key="key-fail",
            market="kr",
            symbol="005930",
            side="buy",
            quantity=Decimal("100"),
            limit_price=Decimal("70000"),
            blocked_by="max_notional_krw_cap",
            reason="max_notional_krw_cap",
        )
        mock_emit = AsyncMock(return_value=emission)

        @asynccontextmanager
        async def fake_session():
            yield None, lambda db: SimpleNamespace(emit_intent=mock_emit)

        monkeypatch.setattr(scanner, "_intent_session", fake_session)

        result = await scanner.scan_market("kr")

        assert result["alerts_sent"] == 1
        assert scanner._watch_service.removed_fields == []  # Watch KEPT
        assert "failed" in scanner._openclaw.messages[0]
        assert "max_notional_krw_cap" in scanner._openclaw.messages[0]

    @pytest.mark.asyncio
    async def test_dedupe_hit_still_deletes_watch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from contextlib import asynccontextmanager
        from decimal import Decimal

        from app.services.watch_order_intent_service import IntentEmissionResult

        scanner = WatchScanner()
        scanner._watch_service = _FakeWatchService()
        field = "asset:005930:price_below:70000"
        scanner._watch_service._rows_by_market["kr"] = [
            {
                "market": "kr",
                "target_kind": "asset",
                "symbol": "005930",
                "condition_type": "price_below",
                "threshold": 70000.0,
                "field": field,
                "raw_payload": '{"action":"create_order_intent","side":"buy","quantity":1}',
            }
        ]
        scanner._openclaw = _FakeOpenClawClient(status="success")

        monkeypatch.setattr(scanner, "_is_market_open", lambda market: True)
        monkeypatch.setattr(scanner, "_get_price", AsyncMock(return_value=69000.0))

        emission = IntentEmissionResult(
            status="dedupe_hit",
            ledger_id=123,
            correlation_id="corr-old",
            idempotency_key="key-1",
            market="kr",
            symbol="005930",
            side="buy",
            quantity=Decimal("1"),
            limit_price=Decimal("70000"),
            blocked_by=None,
            reason="already_previewed_today",
        )
        mock_emit = AsyncMock(return_value=emission)

        @asynccontextmanager
        async def fake_session():
            yield None, lambda db: SimpleNamespace(emit_intent=mock_emit)

        monkeypatch.setattr(scanner, "_intent_session", fake_session)

        result = await scanner.scan_market("kr")

        assert result["alerts_sent"] == 1
        assert scanner._watch_service.removed_fields == [("kr", field)]
        assert "dedupe_hit" in scanner._openclaw.messages[0]
