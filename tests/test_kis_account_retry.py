"""Tests for KIS account fetch_my_stocks transient error retry logic."""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock


class TestFetchMyStocksRetry:
    """Test retry behavior for transient errors in fetch_my_stocks."""

    @pytest.fixture
    def mock_parent(self):
        """Create mock parent with required attributes."""
        parent = MagicMock()
        parent._hdr_base = {"content-type": "application/json"}
        parent._token_manager = AsyncMock()
        parent._ensure_token = AsyncMock()
        parent._request_with_rate_limit = AsyncMock()
        
        settings = MagicMock()
        settings.kis_access_token = "test_token"
        settings.kis_account_no = "1234567801"
        parent._settings = settings
        
        return parent

    @pytest.fixture
    def account_client(self, mock_parent):
        """Create AccountClient instance with mocked parent."""
        from app.services.brokers.kis.account import AccountClient
        return AccountClient(mock_parent)

    @pytest.mark.asyncio
    async def test_egw00316_transient_error_triggers_retry(self, account_client, mock_parent):
        """Verify EGW00316 transient errors trigger retry in fetch_my_stocks."""
        # First call fails with EGW00316, second succeeds
        transient_response = {
            "rt_cd": "1",
            "msg_cd": "EGW00316",
            "msg1": "조회 처리 중 오류 발생하였습니다. 재 조회 수행 부탁드립니다.",
        }
        success_response = {
            "rt_cd": "0",
            "msg_cd": "MSG00000",
            "output1": [],
        }
        mock_parent._request_with_rate_limit.side_effect = [
            transient_response,
            success_response,
        ]

        result = await account_client.fetch_my_stocks(is_overseas=True, exchange_code="NASD")

        assert result == []
        assert mock_parent._request_with_rate_limit.call_count == 2

    @pytest.mark.asyncio
    async def test_sydb0050_transient_error_triggers_retry(self, account_client, mock_parent):
        """Verify SYDB0050 transient errors trigger retry."""
        transient_response = {
            "rt_cd": "1",
            "msg_cd": "SYDB0050",
            "msg1": "조회이후에 자료가 변경되었습니다.(다시 조회하세요)",
        }
        success_response = {
            "rt_cd": "0",
            "msg_cd": "MSG00000",
            "output1": [{"pdno": "005930", "hldg_qty": "10"}],
        }
        mock_parent._request_with_rate_limit.side_effect = [
            transient_response,
            success_response,
        ]

        result = await account_client.fetch_my_stocks(is_overseas=False)

        assert len(result) == 1
        assert mock_parent._request_with_rate_limit.call_count == 2

    @pytest.mark.asyncio
    async def test_transient_error_exceeds_max_attempts_raises(self, account_client, mock_parent):
        """Verify transient errors raise after max retry attempts exceeded."""
        from app.services.brokers.kis import constants

        transient_response = {
            "rt_cd": "1",
            "msg_cd": "EGW00316",
            "msg1": "조회 처리 중 오류 발생하였습니다. 재 조회 수행 부탁드립니다.",
        }
        # Return transient error more than max attempts
        mock_parent._request_with_rate_limit.return_value = transient_response

        with pytest.raises(RuntimeError) as exc_info:
            await account_client.fetch_my_stocks(is_overseas=True, exchange_code="NASD")

        assert "EGW00316" in str(exc_info.value)
        assert mock_parent._request_with_rate_limit.call_count == constants.RETRYABLE_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self, account_client, mock_parent):
        """Verify non-retryable errors raise immediately without retry."""
        error_response = {
            "rt_cd": "1",
            "msg_cd": "EGW99999",
            "msg1": "Some other error",
        }
        mock_parent._request_with_rate_limit.return_value = error_response

        with pytest.raises(RuntimeError) as exc_info:
            await account_client.fetch_my_stocks(is_overseas=False)

        assert "EGW99999" in str(exc_info.value)
        assert mock_parent._request_with_rate_limit.call_count == 1
