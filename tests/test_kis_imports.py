"""Import smoke tests for KIS client module.

This module verifies that all KIS components can be imported correctly
and that the facade pattern is properly wired.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


class TestKISImports:
    """Test that all KIS module imports work correctly."""

    def test_protocol_import(self):
        """Test KISClientProtocol can be imported."""
        from app.services.brokers.kis.protocols import KISClientProtocol

        assert hasattr(KISClientProtocol, "_ensure_client")
        assert hasattr(KISClientProtocol, "_ensure_token")
        assert hasattr(KISClientProtocol, "_request_with_rate_limit")

    def test_base_import(self):
        """Test BaseKISClient can be imported."""
        from app.services.brokers.kis.base import BaseKISClient

        assert hasattr(BaseKISClient, "_ensure_client")
        assert hasattr(BaseKISClient, "_ensure_token")
        assert hasattr(BaseKISClient, "close")

    def test_facade_import(self):
        """Test KISClient facade can be imported."""
        from app.services.brokers.kis.client import KISClient, kis

        assert KISClient is not None
        assert kis is not None
        assert isinstance(kis, KISClient)

    def test_sub_clients_import(self):
        """Test all sub-clients can be imported."""
        from app.services.brokers.kis import (
            AccountClient,
            DomesticOrderClient,
            MarketDataClient,
            OverseasOrderClient,
        )

        assert AccountClient is not None
        assert DomesticOrderClient is not None
        assert MarketDataClient is not None
        assert OverseasOrderClient is not None

    def test_protocol_runtime_check(self):
        """Test that KISClient can be runtime-checked as KISClientProtocol."""
        from app.services.brokers.kis.client import KISClient
        from app.services.brokers.kis.protocols import KISClientProtocol

        client = KISClient()
        # KISClient should satisfy KISClientProtocol at runtime
        assert isinstance(client, KISClientProtocol)


class TestKISFacadeDelegation:
    """Test that KISClient facade properly delegates to sub-clients."""

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_facade_delegates_to_market_data(self, mock_client_class):
        """Test that volume_rank delegates to MarketDataClient."""
        from app.services.brokers.kis.client import KISClient

        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output": [{"hts_kor_isnm": "Test"}],
        }
        mock_client.get.return_value = mock_response
        mock_client_class.return_value = mock_client

        client = KISClient()

        with patch.object(client, "_ensure_token"):
            # Mock the _market_data sub-client's actual method
            with patch.object(
                client._market_data,
                "volume_rank",
                return_value=[{"hts_kor_isnm": "Test"}],
            ) as mock_volume_rank:
                result = await client.volume_rank()
                assert result == [{"hts_kor_isnm": "Test"}]
                mock_volume_rank.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_facade_delegates_to_account(self, mock_client_class):
        """Test that fetch_my_stocks delegates to AccountClient."""
        from app.services.brokers.kis.client import KISClient

        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        client = KISClient()

        with patch.object(client, "_ensure_token"):
            with patch.object(
                client._account,
                "fetch_my_stocks",
                return_value=[{"pdno": "005930"}],
            ) as mock_fetch:
                result = await client.fetch_my_stocks()
                assert result == [{"pdno": "005930"}]
                mock_fetch.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_facade_delegates_to_domestic_orders(self, mock_client_class):
        """Test that order_korea_stock delegates to DomesticOrderClient."""
        from app.services.brokers.kis.client import KISClient

        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        client = KISClient()

        with patch.object(client, "_ensure_token"):
            with patch.object(
                client._domestic_orders,
                "order_korea_stock",
                return_value={"odno": "12345"},
            ) as mock_order:
                result = await client.order_korea_stock(
                    stock_code="005930",
                    order_type="buy",
                    quantity=10,
                )
                assert result == {"odno": "12345"}
                mock_order.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_facade_delegates_to_overseas_orders(self, mock_client_class):
        """Test that order_overseas_stock delegates to OverseasOrderClient."""
        from app.services.brokers.kis.client import KISClient

        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        client = KISClient()

        with patch.object(client, "_ensure_token"):
            with patch.object(
                client._overseas_orders,
                "order_overseas_stock",
                return_value={"odno": "67890"},
            ) as mock_order:
                result = await client.order_overseas_stock(
                    symbol="AAPL",
                    exchange_code="NASD",
                    order_type="buy",
                    quantity=5,
                )
                assert result == {"odno": "67890"}
                mock_order.assert_called_once()


class TestKISClientLifecycle:
    """Test KISClient lifecycle management."""

    def test_singleton_instance(self):
        """Test that kis singleton is properly initialized."""
        from app.services.brokers.kis.client import KISClient, kis

        assert isinstance(kis, KISClient)
        assert hasattr(kis, "_market_data")
        assert hasattr(kis, "_account")
        assert hasattr(kis, "_domestic_orders")
        assert hasattr(kis, "_overseas_orders")

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self):
        """Test that close() can be called multiple times safely."""
        from app.services.brokers.kis.client import KISClient

        client = KISClient()

        # First close should work
        await client.close()

        # Second close should not raise
        await client.close()

    @pytest.mark.asyncio
    async def test_instances_share_underlying_http_client(self):
        from app.services.brokers.kis.client import KISClient

        class DummyOwner:
            def __init__(self):
                self.client = AsyncMock()

            async def __aenter__(self):
                return self.client

            async def __aexit__(self, exc_type, exc, tb):
                return None

        owner = DummyOwner()

        with patch.object(
            KISClient,
            "_build_http_client",
            return_value=owner,
        ) as mock_build:
            client_one = KISClient()
            client_two = KISClient()

            try:
                http_client_one = await client_one._ensure_client()
                http_client_two = await client_two._ensure_client()

                assert http_client_one is http_client_two
                mock_build.assert_called_once()
            finally:
                await client_one.close()

    def test_stale_shared_client_from_closed_loop_does_not_break_reopen(self):
        from app.services.brokers.kis.client import KISClient

        class LoopBoundOwner:
            def __init__(self) -> None:
                self.client = AsyncMock()
                self.loop: asyncio.AbstractEventLoop | None = None

            async def __aenter__(self):
                self.loop = asyncio.get_running_loop()
                return self.client

            async def __aexit__(self, exc_type, exc, tb):
                if self.loop is not None and self.loop.is_closed():
                    raise RuntimeError("Event loop is closed")
                return None

        async def open_shared_client(owner: LoopBoundOwner) -> None:
            with patch.object(KISClient, "_build_http_client", return_value=owner):
                client = KISClient()
                await client._ensure_client()

        first_owner = LoopBoundOwner()
        first_loop = asyncio.new_event_loop()
        try:
            first_loop.run_until_complete(open_shared_client(first_owner))
        finally:
            first_loop.close()

        second_owner = LoopBoundOwner()
        second_loop = asyncio.new_event_loop()
        try:
            with patch.object(
                KISClient, "_build_http_client", return_value=second_owner
            ):
                client = KISClient()
                http_client = second_loop.run_until_complete(client._ensure_client())
                assert http_client is second_owner.client
                second_loop.run_until_complete(client.close())
        finally:
            second_loop.close()
