"""Tests for NXT-conditional EXCG_ID_DVSN_CD routing in domestic orders."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_NXT_ELIGIBLE_PATH = "app.services.brokers.kis.domestic_orders.is_nxt_eligible"


def _make_client():
    """Create DomesticOrderClient with mocked parent (same pattern as retry tests)."""
    from app.services.brokers.kis.domestic_orders import DomesticOrderClient

    parent = MagicMock()
    parent._hdr_base = {"content-type": "application/json"}
    parent._ensure_token = AsyncMock()
    parent._token_manager = AsyncMock()

    settings = MagicMock()
    settings.kis_account_no = "1234567890"
    settings.kis_access_token = "test-token"
    parent._settings = settings

    return DomesticOrderClient(parent), parent


def _success_response(**extra):
    return {"rt_cd": "0", "output": {"ODNO": "00001", "ORD_TMD": "120000"}, **extra}


@pytest.mark.unit
class TestOrderKoreaStockNxtRouting:
    """order_korea_stock sets EXCG_ID_DVSN_CD based on NXT eligibility."""

    @pytest.mark.asyncio
    async def test_nxt_eligible_uses_sor(self):
        instance, parent = _make_client()
        parent._request_with_rate_limit = AsyncMock(return_value=_success_response())

        with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=True)):
            await instance.order_korea_stock("005930", "buy", 10, 70000)

        body = parent._request_with_rate_limit.call_args.kwargs.get(
            "json_body"
        ) or parent._request_with_rate_limit.call_args[1].get("json_body")
        assert body["EXCG_ID_DVSN_CD"] == "SOR"

    @pytest.mark.asyncio
    async def test_non_nxt_uses_empty_string(self):
        instance, parent = _make_client()
        parent._request_with_rate_limit = AsyncMock(return_value=_success_response())

        with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=False)):
            await instance.order_korea_stock("034220", "buy", 10, 5000)

        body = parent._request_with_rate_limit.call_args.kwargs.get(
            "json_body"
        ) or parent._request_with_rate_limit.call_args[1].get("json_body")
        assert body["EXCG_ID_DVSN_CD"] == ""


@pytest.mark.unit
class TestCancelKoreaOrderNxtRouting:
    """cancel_korea_order sets EXCG_ID_DVSN_CD based on NXT eligibility."""

    @pytest.mark.asyncio
    async def test_nxt_eligible_uses_sor(self):
        instance, parent = _make_client()
        parent._request_with_rate_limit = AsyncMock(return_value=_success_response())

        with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=True)):
            await instance.cancel_korea_order(
                order_number="00001",
                stock_code="005930",
                quantity=10,
                price=70000,
                order_type="buy",
                krx_fwdg_ord_orgno="00091",
            )

        body = parent._request_with_rate_limit.call_args.kwargs.get(
            "json_body"
        ) or parent._request_with_rate_limit.call_args[1].get("json_body")
        assert body["EXCG_ID_DVSN_CD"] == "SOR"

    @pytest.mark.asyncio
    async def test_non_nxt_uses_empty_string(self):
        instance, parent = _make_client()
        parent._request_with_rate_limit = AsyncMock(return_value=_success_response())

        with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=False)):
            await instance.cancel_korea_order(
                order_number="00001",
                stock_code="034220",
                quantity=10,
                price=5000,
                order_type="sell",
                krx_fwdg_ord_orgno="00091",
            )

        body = parent._request_with_rate_limit.call_args.kwargs.get(
            "json_body"
        ) or parent._request_with_rate_limit.call_args[1].get("json_body")
        assert body["EXCG_ID_DVSN_CD"] == ""


@pytest.mark.unit
class TestModifyKoreaOrderNxtRouting:
    """modify_korea_order sets EXCG_ID_DVSN_CD based on NXT eligibility."""

    @pytest.mark.asyncio
    async def test_nxt_eligible_uses_sor(self):
        instance, parent = _make_client()
        parent._request_with_rate_limit = AsyncMock(return_value=_success_response())

        with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=True)):
            await instance.modify_korea_order(
                order_number="00001",
                stock_code="005930",
                quantity=10,
                new_price=71000,
                krx_fwdg_ord_orgno="00091",
            )

        body = parent._request_with_rate_limit.call_args.kwargs.get(
            "json_body"
        ) or parent._request_with_rate_limit.call_args[1].get("json_body")
        assert body["EXCG_ID_DVSN_CD"] == "SOR"

    @pytest.mark.asyncio
    async def test_non_nxt_uses_empty_string(self):
        instance, parent = _make_client()
        parent._request_with_rate_limit = AsyncMock(return_value=_success_response())

        with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=False)):
            await instance.modify_korea_order(
                order_number="00001",
                stock_code="034220",
                quantity=10,
                new_price=5500,
                krx_fwdg_ord_orgno="00091",
            )

        body = parent._request_with_rate_limit.call_args.kwargs.get(
            "json_body"
        ) or parent._request_with_rate_limit.call_args[1].get("json_body")
        assert body["EXCG_ID_DVSN_CD"] == ""
