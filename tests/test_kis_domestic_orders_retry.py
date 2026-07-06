"""Tests for KIS domestic orders transient error retry logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_NXT_ELIGIBLE_PATH = "app.services.brokers.kis.domestic_orders.is_nxt_eligible"


def _token_error(error_code: str) -> dict[str, str]:
    return {"rt_cd": "1", "msg_cd": error_code, "msg1": "token expired"}


def _make_domestic_orders():
    from app.services.brokers.kis.domestic_orders import DomesticOrderClient

    parent = MagicMock()
    parent._hdr_base = {"content-type": "application/json"}
    parent._ensure_token = AsyncMock()
    parent._kis_url = lambda path: f"https://host{path}"

    token_manager = MagicMock()
    token_manager.clear_token = AsyncMock()
    parent._token_manager = token_manager

    settings = MagicMock()
    settings.kis_account_no = "1234567890"
    settings.kis_access_token = "test-token"
    parent._settings = settings

    return DomesticOrderClient(parent), parent


@pytest.mark.unit
class TestDomesticOrdersTransientRetry:
    """Verify SYDB0050 transient errors trigger retry in domestic daily order."""

    @pytest.fixture
    def _mock_domestic_orders(self):
        """Create DomesticOrderClient with mocked parent."""
        return _make_domestic_orders()

    @pytest.mark.asyncio
    async def test_retries_on_sydb0050_then_succeeds(self, _mock_domestic_orders):
        instance, parent = _mock_domestic_orders

        transient_response = {
            "rt_cd": "1",
            "msg_cd": "SYDB0050",
            "msg1": "조회이후에 자료가 변경되었습니다.(다시 조회하세요)",
        }
        success_response = {
            "rt_cd": "0",
            "output1": [{"odno": "001", "pdno": "005930"}],
            "ctx_area_fk100": "",
            "ctx_area_nk100": "",
        }

        parent._request_with_rate_limit = AsyncMock(
            side_effect=[transient_response, success_response]
        )

        result = await instance.inquire_daily_order_domestic(
            start_date="20260317", end_date="20260317"
        )

        assert len(result) == 1
        assert result[0]["odno"] == "001"
        assert parent._request_with_rate_limit.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_max_retries_exhausted(self, _mock_domestic_orders):
        instance, parent = _mock_domestic_orders

        transient_response = {
            "rt_cd": "1",
            "msg_cd": "SYDB0050",
            "msg1": "조회이후에 자료가 변경되었습니다.(다시 조회하세요)",
        }

        parent._request_with_rate_limit = AsyncMock(return_value=transient_response)

        with pytest.raises(RuntimeError, match="SYDB0050"):
            await instance.inquire_daily_order_domestic(
                start_date="20260317", end_date="20260317"
            )

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self, _mock_domestic_orders):
        instance, parent = _mock_domestic_orders

        error_response = {
            "rt_cd": "1",
            "msg_cd": "SOME_OTHER",
            "msg1": "알 수 없는 오류",
        }

        parent._request_with_rate_limit = AsyncMock(return_value=error_response)

        with pytest.raises(RuntimeError, match="SOME_OTHER"):
            await instance.inquire_daily_order_domestic(
                start_date="20260317", end_date="20260317"
            )

        assert parent._request_with_rate_limit.call_count == 1

    @pytest.mark.asyncio
    async def test_raises_when_domestic_history_reaches_max_pages_with_cursor(
        self, _mock_domestic_orders
    ):
        instance, parent = _mock_domestic_orders
        parent._request_with_rate_limit = AsyncMock(
            side_effect=[
                {
                    "rt_cd": "0",
                    "output1": [{"ord_no": "001", "pdno": "005930"}],
                    "ctx_area_fk100": "FK2",
                    "ctx_area_nk100": "NK2",
                },
                {
                    "rt_cd": "0",
                    "output1": [{"ord_no": "002", "pdno": "005930"}],
                    "ctx_area_fk100": "FK3",
                    "ctx_area_nk100": "NK3",
                },
            ]
        )

        with pytest.raises(
            RuntimeError, match="domestic daily order history truncated"
        ):
            await instance.inquire_daily_order_domestic(
                start_date="20260201",
                end_date="20260208",
                max_pages=2,
            )


@pytest.mark.unit
class TestDomesticOrdersTokenExpiryMutationGuards:
    """ROB-739: token-expiry mutation resubmits are bounded fail-closed."""

    @pytest.mark.parametrize("error_code", ["EGW00123", "EGW00121"])
    @pytest.mark.asyncio
    async def test_order_token_expiry_repeated_is_bounded_fail_closed(self, error_code):
        instance, parent = _make_domestic_orders()
        parent._request_with_rate_limit = AsyncMock(
            return_value=_token_error(error_code)
        )

        with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=False)):
            with pytest.raises(RuntimeError, match=error_code):
                await instance.order_korea_stock("005930", "buy", 1, 70000)

        assert parent._request_with_rate_limit.call_count == 2
        assert parent._token_manager.clear_token.await_count == 1

    @pytest.mark.parametrize("error_code", ["EGW00123", "EGW00121"])
    @pytest.mark.asyncio
    async def test_cancel_token_expiry_repeated_is_bounded_fail_closed(
        self, error_code
    ):
        instance, parent = _make_domestic_orders()
        parent._request_with_rate_limit = AsyncMock(
            return_value=_token_error(error_code)
        )

        with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=False)):
            with pytest.raises(RuntimeError, match=error_code):
                await instance.cancel_korea_order(
                    order_number="0001",
                    stock_code="005930",
                    quantity=1,
                    price=70000,
                    order_type="buy",
                    krx_fwdg_ord_orgno="00091",
                )

        assert parent._request_with_rate_limit.call_count == 2
        assert parent._token_manager.clear_token.await_count == 1

    @pytest.mark.parametrize("error_code", ["EGW00123", "EGW00121"])
    @pytest.mark.asyncio
    async def test_modify_token_expiry_repeated_is_bounded_fail_closed(
        self, error_code
    ):
        instance, parent = _make_domestic_orders()
        parent._request_with_rate_limit = AsyncMock(
            return_value=_token_error(error_code)
        )

        with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=False)):
            with pytest.raises(RuntimeError, match=error_code):
                await instance.modify_korea_order(
                    order_number="0001",
                    stock_code="005930",
                    quantity=1,
                    new_price=71000,
                    krx_fwdg_ord_orgno="00091",
                )

        assert parent._request_with_rate_limit.call_count == 2
        assert parent._token_manager.clear_token.await_count == 1
