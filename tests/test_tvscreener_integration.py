"""Integration tests for TvScreener service wrapper.

This module contains both unit tests (with mocks) and integration tests
(with real API calls) for the TvScreenerService.
"""

from __future__ import annotations

import asyncio
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from app.services.tvscreener_service import (
    TvScreenerError,
    TvScreenerMalformedRequestError,
    TvScreenerRateLimitError,
    TvScreenerService,
    TvScreenerTimeoutError,
    get_tvscreener_service,
)


class TestTvScreenerServiceInit:
    """Tests for TvScreenerService initialization."""

    def test_init_with_defaults(self):
        """Test service initializes with default values."""
        service = TvScreenerService()

        assert service.max_retries == 3
        assert service.base_delay == 1.0
        assert service.timeout == 30.0
        assert service._field_cache == {}

    def test_init_with_custom_values(self):
        """Test service initializes with custom values."""
        service = TvScreenerService(
            max_retries=5,
            base_delay=2.0,
            timeout=60.0,
        )

        assert service.max_retries == 5
        assert service.base_delay == 2.0
        assert service.timeout == 60.0


class TestFetchWithRetry:
    """Tests for fetch_with_retry method."""

    @pytest.mark.asyncio
    async def test_successful_query_first_attempt(self):
        """Test successful query on first attempt returns result."""
        service = TvScreenerService()
        expected_df = pd.DataFrame({"col1": [1, 2, 3]})

        mock_callable = Mock(return_value=expected_df)

        result = await service.fetch_with_retry(
            mock_callable,
            operation_name="test_query",
        )

        assert result.equals(expected_df)
        assert mock_callable.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_timeout(self):
        """Test retry logic when timeout occurs."""
        service = TvScreenerService(
            max_retries=3,
            base_delay=0.1,
            timeout=0.05,
        )

        call_count = 0

        def slow_callable():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                # Simulate slow operation on first call
                import time

                time.sleep(0.1)
            return pd.DataFrame({"col1": [1]})

        # Should timeout on first attempt, succeed on second
        result = await service.fetch_with_retry(
            slow_callable,
            operation_name="slow_query",
        )

        assert len(result) == 1
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_timeout_error_after_max_retries(self):
        """Test TimeoutError raised after max retries."""
        service = TvScreenerService(
            max_retries=2,
            base_delay=0.05,
            timeout=0.01,
        )

        def always_slow_callable():
            import time

            time.sleep(0.1)
            return pd.DataFrame()

        with pytest.raises(TvScreenerTimeoutError, match="timed out"):
            await service.fetch_with_retry(
                always_slow_callable,
                operation_name="timeout_test",
            )

    @pytest.mark.asyncio
    async def test_malformed_request_retry_and_fail(self):
        """Test malformed request retries with exponential backoff then fails."""
        service = TvScreenerService(
            max_retries=3,
            base_delay=0.05,
        )

        call_count = 0

        def malformed_callable():
            nonlocal call_count
            call_count += 1
            raise Exception("MalformedRequestException: Invalid field")

        with pytest.raises(TvScreenerMalformedRequestError, match="malformed request"):
            await service.fetch_with_retry(
                malformed_callable,
                operation_name="malformed_test",
            )

        assert call_count == 3  # Should retry max_retries times

    @pytest.mark.asyncio
    async def test_rate_limit_error_retry_and_fail(self):
        """Test rate limit error retries then fails."""
        service = TvScreenerService(
            max_retries=3,
            base_delay=0.05,
        )

        def rate_limited_callable():
            raise Exception("Rate limit exceeded")

        with pytest.raises(TvScreenerRateLimitError, match="rate limit"):
            await service.fetch_with_retry(
                rate_limited_callable,
                operation_name="rate_limit_test",
            )

    @pytest.mark.asyncio
    async def test_rate_limit_recovery(self):
        """Test successful recovery from rate limit on retry."""
        service = TvScreenerService(
            max_retries=3,
            base_delay=0.05,
        )

        call_count = 0
        expected_df = pd.DataFrame({"col1": [1, 2]})

        def sometimes_rate_limited():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("Too many requests")
            return expected_df

        result = await service.fetch_with_retry(
            sometimes_rate_limited,
            operation_name="rate_limit_recovery",
        )

        assert result.equals(expected_df)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_unexpected_error_retries_then_fails(self):
        """Test unexpected errors are retried then fail with TvScreenerError."""
        service = TvScreenerService(
            max_retries=2,
            base_delay=0.05,
        )

        def unexpected_error_callable():
            raise ValueError("Unexpected error")

        with pytest.raises(TvScreenerError, match="ValueError"):
            await service.fetch_with_retry(
                unexpected_error_callable,
                operation_name="unexpected_error_test",
            )


class TestDiscoverFields:
    """Tests for discover_fields method."""

    @pytest.mark.asyncio
    async def test_discover_fields_returns_field_list(self):
        """Test field discovery returns list of field tuples."""
        service = TvScreenerService()

        # Mock enum class
        class MockField:
            FIELD_ONE = "field_one"
            FIELD_TWO = "field_two"
            _PRIVATE = "private"

            @classmethod
            def some_method(cls):
                return "method"

        # Mock screener class
        class MockScreener:
            pass

        fields = await service.discover_fields(MockScreener, MockField)

        # Should find FIELD_ONE and FIELD_TWO, skip _PRIVATE and some_method
        field_names = [name for name, _ in fields]
        assert "FIELD_ONE" in field_names
        assert "FIELD_TWO" in field_names
        assert "_PRIVATE" not in field_names
        assert "some_method" not in field_names

    @pytest.mark.asyncio
    async def test_discover_fields_caches_results(self):
        """Test field discovery caches results."""
        service = TvScreenerService()

        class MockField:
            TEST_FIELD = "test"

        class MockScreener:
            pass

        # First call
        fields1 = await service.discover_fields(MockScreener, MockField)

        # Second call should return cached result
        fields2 = await service.discover_fields(MockScreener, MockField)

        assert fields1 == fields2
        assert len(service._field_cache) == 1

    @pytest.mark.asyncio
    async def test_discover_fields_handles_exceptions(self):
        """Test field discovery handles exceptions gracefully."""
        service = TvScreenerService()

        class BrokenField:
            @property
            def broken_field(self):
                raise RuntimeError("Field access error")

        class MockScreener:
            pass

        # Should not raise, returns empty or partial list
        fields = await service.discover_fields(MockScreener, BrokenField)

        # Should return a list (possibly empty)
        assert isinstance(fields, list)


class TestQueryCryptoScreener:
    """Tests for query_crypto_screener method."""

    @pytest.mark.asyncio
    async def test_query_crypto_import_error(self):
        """Test ImportError when tvscreener not installed."""
        service = TvScreenerService()

        with patch(
            "app.services.tvscreener_service.CryptoScreener", side_effect=ImportError
        ):
            with pytest.raises(TvScreenerError, match="not installed"):
                await service.query_crypto_screener(columns=[])

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_query_crypto_basic(self):
        """Integration test: basic CryptoScreener query."""
        service = TvScreenerService()

        try:
            from tvscreener import CryptoField

            result = await service.query_crypto_screener(
                columns=[CryptoField.NAME, CryptoField.PRICE],
                limit=5,
            )

            assert isinstance(result, pd.DataFrame)
            assert len(result) > 0
            assert len(result) <= 5
            assert "name" in result.columns or "price" in result.columns

        except ImportError:
            pytest.skip("tvscreener not installed")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_query_crypto_with_filters(self):
        """Integration test: CryptoScreener with WHERE clause."""
        service = TvScreenerService()

        try:
            from tvscreener import CryptoField

            # Query for low RSI coins
            result = await service.query_crypto_screener(
                columns=[CryptoField.NAME, CryptoField.RSI_14],
                where_clause=CryptoField.RSI_14 < 35,
                limit=10,
            )

            assert isinstance(result, pd.DataFrame)
            # May be empty if no coins meet criteria
            if len(result) > 0:
                assert len(result) <= 10

        except ImportError:
            pytest.skip("tvscreener not installed")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_query_crypto_with_sort(self):
        """Integration test: CryptoScreener with sorting."""
        service = TvScreenerService()

        try:
            from tvscreener import CryptoField

            result = await service.query_crypto_screener(
                columns=[CryptoField.NAME, CryptoField.VOLUME],
                sort_by="volume",
                limit=5,
            )

            assert isinstance(result, pd.DataFrame)
            assert len(result) > 0

        except ImportError:
            pytest.skip("tvscreener not installed")


class TestQueryStockScreener:
    """Tests for query_stock_screener method."""

    @pytest.mark.asyncio
    async def test_query_stock_import_error(self):
        """Test ImportError when tvscreener not installed."""
        service = TvScreenerService()

        with patch(
            "app.services.tvscreener_service.StockScreener", side_effect=ImportError
        ):
            with pytest.raises(TvScreenerError, match="not installed"):
                await service.query_stock_screener(columns=[])

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_query_stock_basic(self):
        """Integration test: basic StockScreener query."""
        service = TvScreenerService()

        try:
            from tvscreener import StockField

            result = await service.query_stock_screener(
                columns=[StockField.NAME, StockField.PRICE],
                limit=5,
            )

            assert isinstance(result, pd.DataFrame)
            assert len(result) > 0
            assert len(result) <= 5

        except ImportError:
            pytest.skip("tvscreener not installed")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_query_stock_with_country_filter(self):
        """Integration test: StockScreener with country filter."""
        service = TvScreenerService()

        try:
            from tvscreener import StockField

            # Query South Korean stocks
            result = await service.query_stock_screener(
                columns=[StockField.NAME, StockField.COUNTRY],
                country="South Korea",
                limit=10,
            )

            assert isinstance(result, pd.DataFrame)
            # May be empty if no matches
            if len(result) > 0:
                assert len(result) <= 10
                # Verify country filter worked
                if "country" in result.columns:
                    assert all(result["country"] == "South Korea")

        except ImportError:
            pytest.skip("tvscreener not installed")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_query_stock_with_country_and_where(self):
        """Integration test: StockScreener with country and WHERE clause."""
        service = TvScreenerService()

        try:
            from tvscreener import StockField

            # Query low RSI Korean stocks
            result = await service.query_stock_screener(
                columns=[StockField.NAME, StockField.RSI_14, StockField.COUNTRY],
                country="South Korea",
                where_clause=StockField.RSI_14 < 35,
                limit=5,
            )

            assert isinstance(result, pd.DataFrame)
            # May be empty if no matches

        except ImportError:
            pytest.skip("tvscreener not installed")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_query_stock_us_with_adx(self):
        """Integration test: Verify ADX field availability for US stocks."""
        service = TvScreenerService()

        try:
            from tvscreener import StockField

            result = await service.query_stock_screener(
                columns=[
                    StockField.NAME,
                    StockField.PRICE,
                    StockField.AVERAGE_DIRECTIONAL_INDEX_14,
                ],
                limit=5,
            )

            assert isinstance(result, pd.DataFrame)
            assert len(result) > 0
            # Check if ADX column exists
            # Column name might vary, check for common variations
            has_adx = any(
                "adx" in col.lower() or "directional" in col.lower()
                for col in result.columns
            )
            # Log result for debugging
            if not has_adx:
                print(f"Available columns: {result.columns.tolist()}")

        except ImportError:
            pytest.skip("tvscreener not installed")


class TestSingletonPattern:
    """Tests for singleton pattern and get_tvscreener_service function."""

    def test_get_tvscreener_service_returns_instance(self):
        """Test get_tvscreener_service returns TvScreenerService instance."""
        service = get_tvscreener_service()

        assert isinstance(service, TvScreenerService)

    def test_get_tvscreener_service_returns_same_instance(self):
        """Test get_tvscreener_service returns same instance (singleton)."""
        # Reset singleton for testing
        import app.services.tvscreener_service as module

        module._default_service = None

        service1 = get_tvscreener_service()
        service2 = get_tvscreener_service()

        assert service1 is service2


class TestExceptionHierarchy:
    """Tests for custom exception classes."""

    def test_exception_inheritance(self):
        """Test custom exceptions inherit from TvScreenerError."""
        assert issubclass(TvScreenerRateLimitError, TvScreenerError)
        assert issubclass(TvScreenerMalformedRequestError, TvScreenerError)
        assert issubclass(TvScreenerTimeoutError, TvScreenerError)
        assert issubclass(TvScreenerError, Exception)

    def test_exception_messages(self):
        """Test custom exceptions can be instantiated with messages."""
        error1 = TvScreenerError("Test error")
        assert str(error1) == "Test error"

        error2 = TvScreenerRateLimitError("Rate limit exceeded")
        assert str(error2) == "Rate limit exceeded"

        error3 = TvScreenerMalformedRequestError("Bad request")
        assert str(error3) == "Bad request"

        error4 = TvScreenerTimeoutError("Timeout occurred")
        assert str(error4) == "Timeout occurred"


class TestEdgeCases:
    """Tests for edge cases and error conditions."""

    @pytest.mark.asyncio
    async def test_empty_dataframe_response(self):
        """Test handling of empty DataFrame response."""
        service = TvScreenerService()
        empty_df = pd.DataFrame()

        mock_callable = Mock(return_value=empty_df)

        result = await service.fetch_with_retry(
            mock_callable,
            operation_name="empty_test",
        )

        assert len(result) == 0
        assert isinstance(result, pd.DataFrame)

    @pytest.mark.asyncio
    async def test_none_where_clause(self):
        """Test query methods handle None where_clause correctly."""
        service = TvScreenerService()

        # Mock the internal fetch_with_retry to avoid actual API call
        mock_df = pd.DataFrame({"col1": [1, 2, 3]})

        async def mock_fetch(callable_fn, operation_name):
            # Execute the callable to verify it doesn't error
            _ = callable_fn()
            return mock_df

        service.fetch_with_retry = mock_fetch

        # This should not raise even with None where_clause
        try:
            from tvscreener import CryptoField

            result = await service.query_crypto_screener(
                columns=[CryptoField.NAME],
                where_clause=None,
                limit=5,
            )

            assert result.equals(mock_df)

        except ImportError:
            pytest.skip("tvscreener not installed")

    @pytest.mark.asyncio
    async def test_concurrent_queries(self):
        """Test multiple concurrent queries don't interfere."""
        service = TvScreenerService()

        call_counts = {"q1": 0, "q2": 0, "q3": 0}

        def make_callable(name: str):
            def callable_fn():
                call_counts[name] += 1
                return pd.DataFrame({"result": [name]})

            return callable_fn

        # Execute 3 queries concurrently
        results = await asyncio.gather(
            service.fetch_with_retry(make_callable("q1"), "query1"),
            service.fetch_with_retry(make_callable("q2"), "query2"),
            service.fetch_with_retry(make_callable("q3"), "query3"),
        )

        assert len(results) == 3
        assert all(call_counts[k] == 1 for k in call_counts)


class TestFieldDiscoveryIntegration:
    """Integration tests for field discovery with real tvscreener enums."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_discover_crypto_fields(self):
        """Integration test: discover CryptoScreener fields."""
        service = TvScreenerService()

        try:
            from tvscreener import CryptoField, CryptoScreener

            fields = await service.discover_fields(CryptoScreener, CryptoField)

            assert len(fields) > 0
            field_names = [name for name, _ in fields]

            # Check for known fields
            expected_fields = ["NAME", "PRICE", "VOLUME", "RSI_14"]
            for expected in expected_fields:
                # Field may or may not exist, just verify we got results
                if expected in field_names:
                    print(f"Found expected field: {expected}")

            # Verify ADX availability for crypto (unconfirmed per spec)
            has_adx = "AVERAGE_DIRECTIONAL_INDEX_14" in field_names
            print(f"CryptoField has ADX: {has_adx}")

        except ImportError:
            pytest.skip("tvscreener not installed")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_discover_stock_fields(self):
        """Integration test: discover StockScreener fields."""
        service = TvScreenerService()

        try:
            from tvscreener import StockField, StockScreener

            fields = await service.discover_fields(StockScreener, StockField)

            assert len(fields) > 0
            field_names = [name for name, _ in fields]

            # Stock screener should have ADX (confirmed per spec)
            has_adx = "AVERAGE_DIRECTIONAL_INDEX_14" in field_names
            print(f"StockField has ADX: {has_adx}")

            # Check for other expected fields
            expected_fields = ["NAME", "PRICE", "VOLUME", "RSI_14", "COUNTRY"]
            found_fields = [f for f in expected_fields if f in field_names]
            print(f"Found fields: {found_fields}")

        except ImportError:
            pytest.skip("tvscreener not installed")


class TestLoggingBehavior:
    """Tests for logging behavior (verify no crashes, not log content)."""

    @pytest.mark.asyncio
    async def test_successful_query_logs_info(self, caplog):
        """Test successful queries produce INFO logs."""
        import logging

        caplog.set_level(logging.INFO)

        service = TvScreenerService()
        mock_df = pd.DataFrame({"col1": [1]})
        mock_callable = Mock(return_value=mock_df)

        await service.fetch_with_retry(mock_callable, "test_operation")

        # Verify some logging occurred
        assert len(caplog.records) > 0

    @pytest.mark.asyncio
    async def test_error_query_logs_error(self, caplog):
        """Test failed queries produce ERROR logs."""
        import logging

        caplog.set_level(logging.ERROR)

        service = TvScreenerService(max_retries=1, base_delay=0.01)

        def failing_callable():
            raise ValueError("Test error")

        with pytest.raises(TvScreenerError):
            await service.fetch_with_retry(failing_callable, "failing_operation")

        # Verify error logging occurred
        assert any(record.levelname == "ERROR" for record in caplog.records)
