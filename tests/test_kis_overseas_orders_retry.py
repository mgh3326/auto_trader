"""Tests for KIS overseas orders transient error retry logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.unit
class TestOverseasOrdersTransientRetry:
    """Verify SYDB0050 transient errors trigger retry in overseas daily order."""

    @pytest.fixture
    def _mock_overseas_orders(self):
        """Create OverseasOrders instance with mocked parent."""
        from app.services.brokers.kis.overseas_orders import OverseasOrderClient

        parent = MagicMock()
        parent._hdr_base = {"content-type": "application/json"}
        parent._ensure_token = AsyncMock()

        settings = MagicMock()
        settings.kis_account_no = "1234567890"
        settings.kis_access_token = "test-token"
        parent._settings = settings

        instance = OverseasOrderClient(parent)
        return instance, parent

    @pytest.mark.asyncio
    async def test_retries_on_sydb0050_then_succeeds(self, _mock_overseas_orders):
        instance, parent = _mock_overseas_orders

        transient_response = {
            "rt_cd": "1",
            "msg_cd": "SYDB0050",
            "msg1": "조회이후에 자료가 변경되었습니다.(다시 조회하세요)",
        }
        success_response = {
            "rt_cd": "0",
            "output1": [{"odno": "001", "pdno": "AAPL"}],
            "ctx_area_fk200": "",
            "ctx_area_nk200": "",
        }

        parent._request_with_rate_limit = AsyncMock(
            side_effect=[transient_response, success_response]
        )

        result = await instance.inquire_daily_order_overseas(
            start_date="20260317", end_date="20260317"
        )

        assert len(result) == 1
        assert result[0]["odno"] == "001"
        assert parent._request_with_rate_limit.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_max_retries_exhausted(self, _mock_overseas_orders):
        instance, parent = _mock_overseas_orders

        transient_response = {
            "rt_cd": "1",
            "msg_cd": "SYDB0050",
            "msg1": "조회이후에 자료가 변경되었습니다.(다시 조회하세요)",
        }

        parent._request_with_rate_limit = AsyncMock(return_value=transient_response)

        with pytest.raises(RuntimeError, match="SYDB0050"):
            await instance.inquire_daily_order_overseas(
                start_date="20260317", end_date="20260317"
            )

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self, _mock_overseas_orders):
        instance, parent = _mock_overseas_orders

        error_response = {
            "rt_cd": "1",
            "msg_cd": "SOME_OTHER",
            "msg1": "알 수 없는 오류",
        }

        parent._request_with_rate_limit = AsyncMock(return_value=error_response)

        with pytest.raises(RuntimeError, match="SOME_OTHER"):
            await instance.inquire_daily_order_overseas(
                start_date="20260317", end_date="20260317"
            )

        # Should fail on first attempt, no retry
        assert parent._request_with_rate_limit.call_count == 1
