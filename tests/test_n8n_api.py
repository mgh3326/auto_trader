from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.timezone import KST, now_kst


def _make_crypto_order(
    *,
    symbol: str = "KRW-BTC",
    side: str = "buy",
    status: str = "pending",
    ordered_price: float = 95_000_000.0,
    ordered_qty: float = 0.01,
    remaining_qty: float = 0.01,
    ordered_at: str = "2026-03-15T10:00:00+09:00",
    order_id: str = "uuid-crypto-1",
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "status": status,
        "ordered_qty": ordered_qty,
        "filled_qty": 0.0,
        "remaining_qty": remaining_qty,
        "ordered_price": ordered_price,
        "filled_avg_price": 0.0,
        "ordered_at": ordered_at,
        "filled_at": "",
        "currency": "KRW",
    }


def _make_kr_order(
    *,
    symbol: str = "005930",
    side: str = "buy",
    status: str = "pending",
    ordered_price: int = 70_000,
    ordered_qty: int = 10,
    remaining_qty: int = 10,
    ordered_at: str = "20260315 100000",
    order_id: str = "KR-001",
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "status": status,
        "ordered_qty": ordered_qty,
        "filled_qty": 0,
        "remaining_qty": remaining_qty,
        "ordered_price": ordered_price,
        "filled_avg_price": 0,
        "ordered_at": ordered_at,
        "filled_at": "",
        "currency": "KRW",
    }


def _make_us_order(
    *,
    symbol: str = "AAPL",
    side: str = "buy",
    status: str = "pending",
    ordered_price: float = 180.50,
    ordered_qty: int = 5,
    remaining_qty: int = 5,
    ordered_at: str = "20260315 090000",
    order_id: str = "US-001",
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "status": status,
        "ordered_qty": ordered_qty,
        "filled_qty": 0,
        "remaining_qty": remaining_qty,
        "ordered_price": ordered_price,
        "filled_avg_price": 0.0,
        "ordered_at": ordered_at,
        "filled_at": "",
        "currency": "USD",
    }


def _impl_result(
    *,
    orders: list[dict[str, object]],
    market: str,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "success": True,
        "symbol": None,
        "market": market,
        "status": "pending",
        "filters": {},
        "orders": orders,
        "summary": {
            "total_orders": len(orders),
            "filled": 0,
            "pending": sum(1 for order in orders if order["status"] == "pending"),
            "partial": sum(1 for order in orders if order["status"] == "partial"),
            "cancelled": 0,
        },
        "truncated": False,
        "total_available": len(orders),
        "errors": errors or [],
    }


@pytest.fixture(autouse=True)
def reset_exchange_rate_cache() -> None:
    from app.services import exchange_rate_service as service

    service._cache.clear()
    service._lock = None
    service._lock_loop = None


@pytest.mark.unit
class TestExchangeRateService:
    @pytest.mark.asyncio
    async def test_fetches_rate_on_cache_miss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import exchange_rate_service as service

        calls: list[dict[str, object]] = []

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, dict[str, float]]:
                return {"rates": {"KRW": 1350.5}}

        class FakeAsyncClient:
            def __init__(self, *, timeout: int) -> None:
                self.timeout = timeout

            async def __aenter__(self) -> FakeAsyncClient:
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                return None

            async def get(self, url: str) -> FakeResponse:
                calls.append({"url": url, "timeout": self.timeout})
                return FakeResponse()

        monkeypatch.setattr(service.httpx, "AsyncClient", FakeAsyncClient)

        rate = await service.get_usd_krw_rate()

        assert rate == 1350.5
        assert calls == [
            {"url": "https://open.er-api.com/v6/latest/USD", "timeout": 10}
        ]

    @pytest.mark.asyncio
    async def test_returns_cached_rate_on_hit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import exchange_rate_service as service

        service._cache["usd_krw"] = {
            "rate": 1400.0,
            "expires_at": time.monotonic() + 300,
        }

        class NoCallHttpxModule:
            class AsyncClient:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    raise AssertionError("HTTP call should not happen on cache hit")

        monkeypatch.setattr(service, "httpx", NoCallHttpxModule())

        rate = await service.get_usd_krw_rate()

        assert rate == 1400.0

    @pytest.mark.asyncio
    async def test_refetches_after_ttl_expires(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import exchange_rate_service as service

        service._cache["usd_krw"] = {"rate": 1390.0, "expires_at": 0.0}
        calls: list[str] = []

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, dict[str, float]]:
                return {"rates": {"KRW": 1412.25}}

        class FakeAsyncClient:
            def __init__(self, *, timeout: int) -> None:
                self.timeout = timeout

            async def __aenter__(self) -> FakeAsyncClient:
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                return None

            async def get(self, url: str) -> FakeResponse:
                calls.append(url)
                return FakeResponse()

        monkeypatch.setattr(service.httpx, "AsyncClient", FakeAsyncClient)

        rate = await service.get_usd_krw_rate()

        assert rate == 1412.25
        assert calls == ["https://open.er-api.com/v6/latest/USD"]
        assert service._cache["usd_krw"]["rate"] == 1412.25
        assert service._cache["usd_krw"]["expires_at"] > 0

    @pytest.mark.asyncio
    async def test_concurrent_calls_share_single_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import exchange_rate_service as service

        call_count = 0
        started = asyncio.Event()
        release = asyncio.Event()

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, dict[str, float]]:
                return {"rates": {"KRW": 1425.0}}

        class FakeAsyncClient:
            def __init__(self, *, timeout: int) -> None:
                self.timeout = timeout

            async def __aenter__(self) -> FakeAsyncClient:
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                return None

            async def get(self, url: str) -> FakeResponse:
                nonlocal call_count
                call_count += 1
                started.set()
                await release.wait()
                return FakeResponse()

        monkeypatch.setattr(service.httpx, "AsyncClient", FakeAsyncClient)

        first = asyncio.create_task(service.get_usd_krw_rate())
        await started.wait()
        second = asyncio.create_task(service.get_usd_krw_rate())
        release.set()

        results = await asyncio.gather(first, second)

        assert results == [1425.0, 1425.0]
        assert call_count == 1


@pytest.mark.unit
class TestN8nPendingOrdersService:
    @pytest.mark.asyncio
    async def test_market_all_fans_out_three_calls(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with (
            patch(
                "app.services.n8n_pending_orders_service.get_order_history_impl",
                new_callable=AsyncMock,
                side_effect=[
                    _impl_result(orders=[_make_crypto_order()], market="crypto"),
                    _impl_result(orders=[_make_kr_order()], market="kr"),
                    _impl_result(orders=[_make_us_order()], market="us"),
                ],
            ) as mock_impl,
            patch(
                "app.services.n8n_pending_orders_service.fetch_multiple_current_prices_cached",
                new_callable=AsyncMock,
                return_value={"KRW-BTC": 96_000_000.0},
            ),
            patch(
                "app.services.n8n_pending_orders_service.get_quote",
                new_callable=AsyncMock,
                side_effect=[
                    type("Quote", (), {"price": 71_000.0})(),
                    type("Quote", (), {"price": 181.0})(),
                ],
            ),
            patch(
                "app.services.n8n_pending_orders_service.get_usd_krw_rate",
                new_callable=AsyncMock,
                return_value=1400.0,
            ),
        ):
            result = await fetch_pending_orders(market="all")

        assert mock_impl.call_count == 3
        assert sorted(call.kwargs["market"] for call in mock_impl.call_args_list) == [
            "crypto",
            "kr",
            "us",
        ]
        assert sorted(order["market"] for order in result["orders"]) == [
            "crypto",
            "kr",
            "us",
        ]

    @pytest.mark.asyncio
    async def test_market_specific_single_call(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=[], market="kr"),
        ) as mock_impl:
            await fetch_pending_orders(market="kr", include_current_price=False)

        mock_impl.assert_called_once()
        assert mock_impl.call_args.kwargs["market"] == "kr"
        assert mock_impl.call_args.kwargs["limit"] == -1

    @pytest.mark.asyncio
    async def test_pending_keeps_partial_orders(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        order = _make_crypto_order(status="partial")
        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=[order], market="crypto"),
        ):
            result = await fetch_pending_orders(
                market="crypto", include_current_price=False
            )

        assert result["orders"][0]["status"] == "partial"

    @pytest.mark.asyncio
    async def test_crypto_symbol_stripping(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=[_make_crypto_order()], market="crypto"),
        ):
            result = await fetch_pending_orders(
                market="crypto", include_current_price=False
            )

        order = result["orders"][0]
        assert order["raw_symbol"] == "KRW-BTC"
        assert order["symbol"] == "BTC"

    @pytest.mark.asyncio
    async def test_created_at_kis_format_normalized_to_kst_iso(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(
                orders=[_make_kr_order(ordered_at="20260315 143000")],
                market="kr",
            ),
        ):
            result = await fetch_pending_orders(
                market="kr", include_current_price=False
            )

        created = result["orders"][0]["created_at"]
        assert created == "2026-03-15T14:30:00+09:00"

    @pytest.mark.asyncio
    async def test_created_at_hhmmss_only_uses_fallback_date(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        as_of = datetime(2026, 3, 17, 15, 0, 0, tzinfo=KST)
        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(
                orders=[_make_kr_order(ordered_at="135334")],
                market="kr",
            ),
        ):
            result = await fetch_pending_orders(
                market="kr",
                include_current_price=False,
                as_of=as_of,
            )

        created = result["orders"][0]["created_at"]
        assert created == "2026-03-17T13:53:34+09:00"

    @pytest.mark.asyncio
    async def test_created_at_hhmmss_with_leading_space(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        as_of = datetime(2026, 3, 17, 15, 0, 0, tzinfo=KST)
        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(
                orders=[_make_kr_order(ordered_at=" 135334")],
                market="kr",
            ),
        ):
            result = await fetch_pending_orders(
                market="kr",
                include_current_price=False,
                as_of=as_of,
            )

        created = result["orders"][0]["created_at"]
        assert created == "2026-03-17T13:53:34+09:00"

    @pytest.mark.asyncio
    async def test_orders_sorted_by_created_at_ascending(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        orders = [
            _make_kr_order(
                ordered_at="20260315 140000", symbol="005930", order_id="KR-001"
            ),
            _make_kr_order(
                ordered_at="20260315 100000", symbol="000660", order_id="KR-002"
            ),
        ]
        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=orders, market="kr"),
        ):
            result = await fetch_pending_orders(
                market="kr", include_current_price=False
            )

        assert [order["raw_symbol"] for order in result["orders"]] == [
            "000660",
            "005930",
        ]

    @pytest.mark.asyncio
    async def test_include_current_price_false_skips_quote_calls(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with (
            patch(
                "app.services.n8n_pending_orders_service.get_order_history_impl",
                new_callable=AsyncMock,
                return_value=_impl_result(
                    orders=[_make_crypto_order()], market="crypto"
                ),
            ),
            patch(
                "app.services.n8n_pending_orders_service.fetch_multiple_current_prices_cached",
                new_callable=AsyncMock,
            ) as mock_crypto_prices,
            patch(
                "app.services.n8n_pending_orders_service.get_quote",
                new_callable=AsyncMock,
            ) as mock_quote,
        ):
            result = await fetch_pending_orders(
                market="crypto", include_current_price=False
            )

        mock_crypto_prices.assert_not_called()
        mock_quote.assert_not_called()
        assert result["orders"][0]["current_price"] is None
        assert result["orders"][0]["gap_pct"] is None

    @pytest.mark.asyncio
    async def test_gap_pct_calculation(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with (
            patch(
                "app.services.n8n_pending_orders_service.get_order_history_impl",
                new_callable=AsyncMock,
                return_value=_impl_result(
                    orders=[_make_crypto_order(ordered_price=100_000_000.0)],
                    market="crypto",
                ),
            ),
            patch(
                "app.services.n8n_pending_orders_service.fetch_multiple_current_prices_cached",
                new_callable=AsyncMock,
                return_value={"KRW-BTC": 105_000_000.0},
            ),
        ):
            result = await fetch_pending_orders(
                market="crypto", include_current_price=True
            )

        assert result["orders"][0]["gap_pct"] == 5.0

    @pytest.mark.asyncio
    async def test_age_hours_and_days(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        ordered_at = (
            (now_kst() - timedelta(hours=50)).replace(microsecond=0).isoformat()
        )
        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(
                orders=[_make_crypto_order(ordered_at=ordered_at)],
                market="crypto",
            ),
        ):
            result = await fetch_pending_orders(
                market="crypto", include_current_price=False
            )

        order = result["orders"][0]
        assert order["age_hours"] >= 50
        assert order["age_days"] == order["age_hours"] // 24

    @pytest.mark.asyncio
    async def test_explicit_as_of_controls_age_calculation(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        as_of = datetime.fromisoformat("2026-03-17T12:00:00+09:00")
        ordered_at = "2026-03-15T10:00:00+09:00"

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(
                orders=[_make_crypto_order(ordered_at=ordered_at)],
                market="crypto",
            ),
        ):
            result = await fetch_pending_orders(
                market="crypto",
                include_current_price=False,
                as_of=as_of,
            )

        order = result["orders"][0]
        assert order["created_at"] == ordered_at
        assert order["age_hours"] == 50
        assert order["age_days"] == 2

    @pytest.mark.asyncio
    async def test_blank_ordered_at_uses_explicit_as_of_fallback(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        as_of = datetime.fromisoformat("2026-03-17T12:00:00+09:00")

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(
                orders=[_make_crypto_order(ordered_at="")],
                market="crypto",
            ),
        ):
            result = await fetch_pending_orders(
                market="crypto",
                include_current_price=False,
                as_of=as_of,
            )

        order = result["orders"][0]
        assert order["created_at"] == as_of.isoformat()
        assert order["age_hours"] == 0
        assert order["age_days"] == 0

    @pytest.mark.asyncio
    async def test_min_amount_filter(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        orders = [
            _make_kr_order(
                ordered_price=1_000, ordered_qty=1, remaining_qty=1, order_id="KR-001"
            ),
            _make_kr_order(
                ordered_price=100_000,
                ordered_qty=10,
                remaining_qty=10,
                symbol="000660",
                order_id="KR-002",
            ),
        ]
        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=orders, market="kr"),
        ):
            result = await fetch_pending_orders(
                market="kr",
                include_current_price=False,
                min_amount=10_000,
            )

        assert len(result["orders"]) == 1
        assert result["orders"][0]["raw_symbol"] == "000660"

    @pytest.mark.asyncio
    async def test_us_amount_krw_uses_exchange_rate(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with (
            patch(
                "app.services.n8n_pending_orders_service.get_order_history_impl",
                new_callable=AsyncMock,
                return_value=_impl_result(
                    orders=[
                        _make_us_order(
                            ordered_price=100.0, ordered_qty=2, remaining_qty=2
                        )
                    ],
                    market="us",
                ),
            ),
            patch(
                "app.services.n8n_pending_orders_service.get_usd_krw_rate",
                new_callable=AsyncMock,
                return_value=1400.0,
            ),
            patch(
                "app.services.n8n_pending_orders_service.get_quote",
                new_callable=AsyncMock,
                side_effect=Exception("Yahoo unavailable"),
            ),
        ):
            result = await fetch_pending_orders(market="us", include_current_price=True)

        assert result["orders"][0]["amount_krw"] == 280_000.0

    @pytest.mark.asyncio
    async def test_exchange_rate_failure_preserves_us_order_and_records_error(
        self,
    ) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with (
            patch(
                "app.services.n8n_pending_orders_service.get_order_history_impl",
                new_callable=AsyncMock,
                return_value=_impl_result(orders=[_make_us_order()], market="us"),
            ),
            patch(
                "app.services.n8n_pending_orders_service.get_usd_krw_rate",
                new_callable=AsyncMock,
                side_effect=RuntimeError("rate lookup unavailable"),
            ),
        ):
            result = await fetch_pending_orders(
                market="us", include_current_price=False
            )

        assert len(result["orders"]) == 1
        assert result["orders"][0]["amount_krw"] is None
        assert result["errors"] == [
            {
                "market": "us",
                "error": "USD/KRW rate fetch failed: rate lookup unavailable",
            }
        ]

    @pytest.mark.asyncio
    async def test_summary_skips_orders_with_null_amount_krw(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with (
            patch(
                "app.services.n8n_pending_orders_service.get_order_history_impl",
                new_callable=AsyncMock,
                side_effect=[
                    _impl_result(orders=[], market="crypto"),
                    _impl_result(
                        orders=[
                            _make_kr_order(
                                side="sell",
                                ordered_price=20_000,
                                remaining_qty=3,
                                symbol="000660",
                                order_id="KR-SELL-001",
                            )
                        ],
                        market="kr",
                    ),
                    _impl_result(
                        orders=[_make_us_order(order_id="US-BUY-001")], market="us"
                    ),
                ],
            ),
            patch(
                "app.services.n8n_pending_orders_service.get_usd_krw_rate",
                new_callable=AsyncMock,
                side_effect=RuntimeError("rate lookup unavailable"),
            ),
        ):
            result = await fetch_pending_orders(
                market="all", include_current_price=False
            )

        summary = result["summary"]
        assert summary["total"] == 2
        assert summary["buy_count"] == 1
        assert summary["sell_count"] == 1
        assert summary["total_buy_krw"] == 0.0
        assert summary["total_sell_krw"] == 60_000.0

    @pytest.mark.asyncio
    async def test_min_amount_keeps_us_orders_with_null_amount_krw(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with (
            patch(
                "app.services.n8n_pending_orders_service.get_order_history_impl",
                new_callable=AsyncMock,
                return_value=_impl_result(orders=[_make_us_order()], market="us"),
            ),
            patch(
                "app.services.n8n_pending_orders_service.get_usd_krw_rate",
                new_callable=AsyncMock,
                side_effect=RuntimeError("rate lookup unavailable"),
            ),
        ):
            result = await fetch_pending_orders(
                market="us",
                include_current_price=False,
                min_amount=1_000_000,
            )

        assert len(result["orders"]) == 1
        assert result["orders"][0]["order_id"] == "US-001"

    @pytest.mark.asyncio
    async def test_quote_failure_preserves_order_and_records_error(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with (
            patch(
                "app.services.n8n_pending_orders_service.get_order_history_impl",
                new_callable=AsyncMock,
                return_value=_impl_result(orders=[_make_kr_order()], market="kr"),
            ),
            patch(
                "app.services.n8n_pending_orders_service.get_kr_names_by_symbols",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "app.services.n8n_pending_orders_service.get_quote",
                new_callable=AsyncMock,
                side_effect=Exception("KIS API timeout"),
            ),
        ):
            result = await fetch_pending_orders(market="kr", include_current_price=True)

        assert len(result["orders"]) == 1
        assert result["orders"][0]["current_price"] is None
        assert result["orders"][0]["gap_pct"] is None
        assert result["errors"] == [
            {"market": "kr", "error": "005930: KIS API timeout"}
        ]

    @pytest.mark.asyncio
    async def test_summary_aggregation(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        orders = [
            _make_kr_order(
                side="buy", ordered_price=10_000, remaining_qty=5, order_id="KR-001"
            ),
            _make_kr_order(
                side="sell",
                ordered_price=20_000,
                remaining_qty=3,
                symbol="000660",
                order_id="KR-002",
            ),
        ]
        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=orders, market="kr"),
        ):
            result = await fetch_pending_orders(
                market="kr", include_current_price=False
            )

        summary = result["summary"]
        assert summary["total"] == 2
        assert summary["buy_count"] == 1
        assert summary["sell_count"] == 1
        assert summary["total_buy_krw"] == 50_000.0
        assert summary["total_sell_krw"] == 60_000.0

    @pytest.mark.asyncio
    async def test_side_filter_passthrough(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=[], market="crypto"),
        ) as mock_impl:
            await fetch_pending_orders(
                market="crypto", side="buy", include_current_price=False
            )

        assert mock_impl.call_args.kwargs["side"] == "buy"

    @pytest.mark.asyncio
    async def test_fallback_infer_market_from_order(self) -> None:
        from app.services.n8n_pending_orders_service import _infer_market_from_order

        assert _infer_market_from_order(_make_us_order()) == "us"
        assert _infer_market_from_order(_make_crypto_order()) == "crypto"
        assert _infer_market_from_order(_make_kr_order()) == "kr"

    @pytest.mark.asyncio
    async def test_orders_include_fmt_fields(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        as_of = datetime.fromisoformat("2026-03-17T12:00:00+09:00")

        with (
            patch(
                "app.services.n8n_pending_orders_service.get_order_history_impl",
                new_callable=AsyncMock,
                return_value=_impl_result(
                    orders=[
                        _make_kr_order(
                            ordered_price=70_000,
                            ordered_qty=10,
                            remaining_qty=10,
                            ordered_at="20260316 120000",
                        )
                    ],
                    market="kr",
                ),
            ),
            patch(
                "app.services.n8n_pending_orders_service.get_quote",
                new_callable=AsyncMock,
                return_value=type("Quote", (), {"price": 71_000.0})(),
            ),
        ):
            result = await fetch_pending_orders(
                market="kr",
                include_current_price=True,
                as_of=as_of,
            )

        order = result["orders"][0]
        assert order["order_price_fmt"] == "7.0만"
        assert order["current_price_fmt"] == "7.1만"
        assert order["gap_pct_fmt"] is not None
        assert order["amount_fmt"] is not None
        assert order["age_fmt"] == "1일"
        assert "005930" in order["summary_line"]
        assert "buy" in order["summary_line"]

    @pytest.mark.asyncio
    async def test_summary_includes_fmt_fields(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        as_of = datetime.fromisoformat("2026-03-16T16:00:00+09:00")

        orders = [
            _make_kr_order(
                side="buy",
                ordered_price=10_000,
                remaining_qty=5,
                order_id="KR-001",
                ordered_at="20260316 100000",
            ),
            _make_kr_order(
                side="sell",
                ordered_price=20_000,
                remaining_qty=3,
                symbol="000660",
                order_id="KR-002",
                ordered_at="20260316 110000",
            ),
        ]
        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=orders, market="kr"),
        ):
            result = await fetch_pending_orders(
                market="kr",
                include_current_price=False,
                as_of=as_of,
            )

        summary = result["summary"]
        assert summary["total_buy_fmt"] == "5.0만"
        assert summary["total_sell_fmt"] == "6.0만"
        assert "03/16" in summary["title"]
        assert "매수 1" in summary["title"]
        assert "매도 1" in summary["title"]

    @pytest.mark.asyncio
    async def test_fmt_fields_present_without_current_price(self) -> None:
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        as_of = datetime.fromisoformat("2026-03-16T16:00:00+09:00")

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(
                orders=[_make_crypto_order(ordered_at="2026-03-16T10:00:00+09:00")],
                market="crypto",
            ),
        ):
            result = await fetch_pending_orders(
                market="crypto",
                include_current_price=False,
                as_of=as_of,
            )

        order = result["orders"][0]
        assert order["order_price_fmt"] is not None
        assert order["current_price_fmt"] == "-"
        assert order["gap_pct_fmt"] == "-"
        assert order["summary_line"] is not None

    @pytest.mark.asyncio
    async def test_fetch_pending_orders_kr_name_enrichment(self) -> None:
        """KR 미체결 주문에 종목명이 enrichment된다."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with (
            patch(
                "app.services.n8n_pending_orders_service.get_order_history_impl",
                new_callable=AsyncMock,
                return_value={
                    "orders": [
                        {
                            "order_id": "KR001",
                            "symbol": "064350",
                            "side": "buy",
                            "status": "pending",
                            "ordered_price": 188000,
                            "ordered_qty": 1,
                            "remaining_qty": 1,
                            "ordered_at": "20260318 100000",
                            "currency": "KRW",
                        },
                    ],
                    "errors": [],
                },
            ),
            patch(
                "app.services.n8n_pending_orders_service.get_kr_names_by_symbols",
                new_callable=AsyncMock,
                return_value={"064350": "현대로템"},
            ),
            patch(
                "app.services.n8n_pending_orders_service.get_quote",
                new_callable=AsyncMock,
                side_effect=Exception("skip"),
            ),
        ):
            result = await fetch_pending_orders(
                market="kr",
                include_current_price=False,
                include_indicators=False,
            )

        order = result["orders"][0]
        assert order["name"] == "현대로템"
        assert "현대로템(064350)" in order["summary_line"]

    @pytest.mark.asyncio
    async def test_fetch_pending_orders_kr_name_lookup_failure_graceful(self) -> None:
        """종목명 조회 실패 시 name=None, summary_line은 symbol만 표시."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with (
            patch(
                "app.services.n8n_pending_orders_service.get_order_history_impl",
                new_callable=AsyncMock,
                return_value={
                    "orders": [
                        {
                            "order_id": "KR001",
                            "symbol": "064350",
                            "side": "buy",
                            "status": "pending",
                            "ordered_price": 188000,
                            "ordered_qty": 1,
                            "remaining_qty": 1,
                            "ordered_at": "20260318 100000",
                            "currency": "KRW",
                        },
                    ],
                    "errors": [],
                },
            ),
            patch(
                "app.services.n8n_pending_orders_service.get_kr_names_by_symbols",
                new_callable=AsyncMock,
                side_effect=Exception("DB down"),
            ),
        ):
            result = await fetch_pending_orders(
                market="kr",
                include_current_price=False,
                include_indicators=False,
            )

        order = result["orders"][0]
        assert order["name"] is None
        assert order["summary_line"].startswith("064350")


class TestN8nPendingOrdersEndpoint:
    @pytest.fixture
    def client(self) -> TestClient:
        from app.routers.n8n import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_default_params_returns_valid_schema(self, client: TestClient) -> None:
        with patch(
            "app.routers.n8n.fetch_pending_orders",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "orders": [],
                "summary": {
                    "total": 0,
                    "buy_count": 0,
                    "sell_count": 0,
                    "total_buy_krw": 0.0,
                    "total_sell_krw": 0.0,
                },
                "errors": [],
            },
        ):
            response = client.get("/api/n8n/pending-orders")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["market"] == "all"
        assert isinstance(data["orders"], list)
        assert isinstance(data["summary"], dict)
        assert isinstance(data["errors"], list)
        assert "as_of" in data

    def test_market_param_passed_to_service(self, client: TestClient) -> None:
        with patch(
            "app.routers.n8n.fetch_pending_orders",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "orders": [],
                "summary": {
                    "total": 0,
                    "buy_count": 0,
                    "sell_count": 0,
                    "total_buy_krw": 0.0,
                    "total_sell_krw": 0.0,
                },
                "errors": [],
            },
        ) as mock_service:
            client.get(
                "/api/n8n/pending-orders?market=crypto&min_amount=10&include_current_price=false"
            )

        assert mock_service.call_args.kwargs["market"] == "crypto"
        assert mock_service.call_args.kwargs["min_amount"] == 10.0
        assert mock_service.call_args.kwargs["include_current_price"] is False
        assert mock_service.call_args.kwargs["side"] is None
        assert mock_service.call_args.kwargs["as_of"].microsecond == 0

    def test_router_passes_single_as_of_to_service(self, client: TestClient) -> None:
        fixed_as_of = datetime.fromisoformat("2026-03-15T16:45:00+09:00")

        with (
            patch("app.routers.n8n.now_kst", return_value=fixed_as_of),
            patch(
                "app.routers.n8n.fetch_pending_orders",
                new_callable=AsyncMock,
                return_value={
                    "success": True,
                    "orders": [],
                    "summary": {
                        "total": 0,
                        "buy_count": 0,
                        "sell_count": 0,
                        "total_buy_krw": 0.0,
                        "total_sell_krw": 0.0,
                    },
                    "errors": [],
                },
            ) as mock_service,
        ):
            response = client.get("/api/n8n/pending-orders")

        assert response.status_code == 200
        assert mock_service.call_args.kwargs == {
            "market": "all",
            "min_amount": 0.0,
            "include_current_price": True,
            "side": None,
            "as_of": fixed_as_of,
            "include_indicators": True,
        }

    def test_router_response_uses_same_generated_as_of(
        self, client: TestClient
    ) -> None:
        fixed_as_of = datetime.fromisoformat("2026-03-15T16:45:00+09:00")

        with (
            patch("app.routers.n8n.now_kst", return_value=fixed_as_of),
            patch(
                "app.routers.n8n.fetch_pending_orders",
                new_callable=AsyncMock,
                return_value={
                    "success": True,
                    "orders": [
                        {
                            "order_id": "US-001",
                            "symbol": "AAPL",
                            "raw_symbol": "AAPL",
                            "market": "us",
                            "side": "buy",
                            "status": "pending",
                            "order_price": 180.5,
                            "current_price": None,
                            "gap_pct": None,
                            "amount_krw": None,
                            "quantity": 5.0,
                            "remaining_qty": 5.0,
                            "created_at": "2026-03-15T09:00:00+09:00",
                            "age_hours": 7,
                            "age_days": 0,
                            "currency": "USD",
                        }
                    ],
                    "summary": {
                        "total": 1,
                        "buy_count": 1,
                        "sell_count": 0,
                        "total_buy_krw": 0.0,
                        "total_sell_krw": 0.0,
                    },
                    "errors": [
                        {
                            "market": "us",
                            "error": "USD/KRW rate fetch failed: rate lookup unavailable",
                        }
                    ],
                },
            ),
        ):
            response = client.get("/api/n8n/pending-orders?market=us")

        assert response.status_code == 200
        data = response.json()
        assert data["as_of"] == fixed_as_of.isoformat()
        assert data["orders"][0]["age_hours"] == 7
        assert data["orders"][0]["amount_krw"] is None

    def test_service_exception_returns_fixed_error_contract(
        self, client: TestClient
    ) -> None:
        with patch(
            "app.routers.n8n.fetch_pending_orders",
            new_callable=AsyncMock,
            side_effect=Exception("boom"),
        ):
            response = client.get("/api/n8n/pending-orders")

        assert response.status_code == 500
        data = response.json()
        assert data["success"] is False
        assert data["orders"] == []
        assert data["summary"]["total"] == 0
        assert data["errors"] == [{"market": "all", "error": "boom"}]

    def test_response_includes_fmt_fields(self, client: TestClient) -> None:
        with patch(
            "app.routers.n8n.fetch_pending_orders",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "orders": [
                    {
                        "order_id": "KR-001",
                        "symbol": "005930",
                        "raw_symbol": "005930",
                        "market": "kr",
                        "side": "buy",
                        "status": "pending",
                        "order_price": 70000.0,
                        "current_price": 71000.0,
                        "gap_pct": 1.43,
                        "amount_krw": 700000.0,
                        "quantity": 10.0,
                        "remaining_qty": 10.0,
                        "created_at": "2026-03-16T10:00:00+09:00",
                        "age_hours": 6,
                        "age_days": 0,
                        "currency": "KRW",
                        "order_price_fmt": "7.0만",
                        "current_price_fmt": "7.1만",
                        "gap_pct_fmt": "+1.4%",
                        "amount_fmt": "70.0만",
                        "age_fmt": "6시간",
                        "summary_line": "005930 buy @7.0만 (현재 7.1만, +1.4%, 70.0만, 6시간)",
                    }
                ],
                "summary": {
                    "total": 1,
                    "buy_count": 1,
                    "sell_count": 0,
                    "total_buy_krw": 700000.0,
                    "total_sell_krw": 0.0,
                    "total_buy_fmt": "70.0만",
                    "total_sell_fmt": "0",
                    "title": "📋 미체결 리뷰 — 03/16 (1건, 매수 1 / 매도 0)",
                },
                "errors": [],
            },
        ):
            response = client.get("/api/n8n/pending-orders?market=kr")

        assert response.status_code == 200
        data = response.json()
        order = data["orders"][0]
        assert order["order_price_fmt"] == "7.0만"
        assert order["summary_line"].startswith("005930 buy")
        assert data["summary"]["total_buy_fmt"] == "70.0만"
        assert data["summary"]["title"].startswith("📋")

        # Backward compatibility: raw fields still present
        assert order["order_price"] == 70000.0
        assert order["gap_pct"] == 1.43
        assert data["summary"]["total_buy_krw"] == 700000.0


class TestN8nKrMorningReportEndpoint:
    @pytest.fixture
    def client(self) -> TestClient:
        from app.routers.n8n import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_get_kr_morning_report_success(self, client: TestClient):
        payload = {
            "success": True,
            "as_of": "2026-03-19T08:50:00+09:00",
            "date_fmt": "03/19 (목)",
            "holdings": {"kis": {}, "toss": {}, "combined": {}},
            "cash_balance": {
                "kis_krw": 45000,
                "kis_krw_fmt": "4.5만",
                "toss_krw": None,
                "toss_krw_fmt": "수동 관리",
                "total_krw": 45000,
                "total_krw_fmt": "4.5만",
            },
            "screening": {
                "total_scanned": 0,
                "top_n": 20,
                "strategy": None,
                "results": [],
                "summary": {},
            },
            "pending_orders": {
                "total": 0,
                "buy_count": 0,
                "sell_count": 0,
                "orders": [],
            },
            "brief_text": "ok",
            "errors": [],
        }

        with patch(
            "app.routers.n8n.fetch_kr_morning_report",
            new_callable=AsyncMock,
            return_value=payload,
        ):
            response = client.get("/api/n8n/kr-morning-report")

        assert response.status_code == 200
        assert response.json()["cash_balance"]["toss_krw_fmt"] == "수동 관리"

    def test_get_kr_morning_report_returns_500_on_service_error(
        self, client: TestClient
    ):
        with patch(
            "app.routers.n8n.fetch_kr_morning_report",
            new_callable=AsyncMock,
            side_effect=Exception("boom"),
        ):
            response = client.get("/api/n8n/kr-morning-report")

        assert response.status_code == 500
        assert response.json()["success"] is False
