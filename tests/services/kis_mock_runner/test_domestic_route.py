"""KR-B0 route proof: KIS mock domestic mutations can never emit SOR."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_NXT_ELIGIBLE_PATH = "app.services.brokers.kis.domestic_orders.is_nxt_eligible"


def _make_client():
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


def _success_response() -> dict[str, object]:
    return {"rt_cd": "0", "output": {"ODNO": "00001", "ORD_TMD": "120000"}}


def _assert_krx_calls(parent: MagicMock) -> None:
    assert parent._request_with_rate_limit.await_count >= 1
    for call in parent._request_with_rate_limit.await_args_list:
        assert call.kwargs["json_body"]["EXCG_ID_DVSN_CD"] == "KRX"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["place", "cancel", "modify"])
async def test_mock_domestic_mutations_ignore_nxt_and_force_krx(operation: str) -> None:
    client, parent = _make_client()
    parent._request_with_rate_limit = AsyncMock(return_value=_success_response())
    nxt = AsyncMock(return_value=True)
    with patch(_NXT_ELIGIBLE_PATH, nxt):
        if operation == "place":
            await client.order_korea_stock("005930", "buy", 10, 70000, is_mock=True)
        elif operation == "cancel":
            await client.cancel_korea_order(
                "00001",
                "005930",
                10,
                70000,
                "buy",
                is_mock=True,
                krx_fwdg_ord_orgno="00091",
            )
        else:
            await client.modify_korea_order(
                "00001",
                "005930",
                10,
                71000,
                is_mock=True,
                krx_fwdg_ord_orgno="00091",
            )
    _assert_krx_calls(parent)
    nxt.assert_not_awaited()


@pytest.mark.asyncio
async def test_mock_token_retry_re_resolves_krx_and_never_sor() -> None:
    client, parent = _make_client()
    parent._request_with_rate_limit = AsyncMock(
        side_effect=[
            {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "expired"},
            _success_response(),
        ]
    )
    nxt = AsyncMock(return_value=True)
    with patch(_NXT_ELIGIBLE_PATH, nxt):
        await client.order_korea_stock("005930", "buy", 10, 70000, is_mock=True)
    _assert_krx_calls(parent)
    assert parent._request_with_rate_limit.await_count == 2
    nxt.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_nxt_route_remains_sor() -> None:
    client, parent = _make_client()
    parent._request_with_rate_limit = AsyncMock(return_value=_success_response())
    with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=True)):
        await client.order_korea_stock("005930", "buy", 10, 70000, is_mock=False)
    assert (
        parent._request_with_rate_limit.await_args.kwargs["json_body"][
            "EXCG_ID_DVSN_CD"
        ]
        == "SOR"
    )


def test_all_domestic_mutation_entry_points_use_writer_guard_when_armed() -> None:
    from app.services.brokers.kis.domestic_orders import DomesticOrderClient

    for method_name in (
        "order_korea_stock",
        "cancel_korea_order",
        "modify_korea_order",
    ):
        assert hasattr(getattr(DomesticOrderClient, method_name), "__wrapped__")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["place", "cancel", "modify"])
async def test_armed_mock_mutations_enter_the_shared_writer_scope(
    monkeypatch, operation: str
) -> None:
    import app.services.brokers.kis.domestic_orders as domestic_orders

    client, parent = _make_client()
    parent._request_with_rate_limit = AsyncMock(return_value=_success_response())
    entered: list[bool] = []

    @asynccontextmanager
    async def fake_scope(*, enabled: bool):
        entered.append(enabled)
        yield

    monkeypatch.setenv("KIS_MOCK_RUNNER_ENABLED", "true")
    monkeypatch.setattr(domestic_orders, "enforce_kis_mock_mutation_writer", fake_scope)
    if operation == "place":
        await client.order_korea_stock("005930", "buy", 10, 70000, is_mock=True)
    elif operation == "cancel":
        await client.cancel_korea_order(
            "00001",
            "005930",
            10,
            70000,
            "buy",
            is_mock=True,
            krx_fwdg_ord_orgno="00091",
        )
    else:
        await client.modify_korea_order(
            "00001",
            "005930",
            10,
            71000,
            is_mock=True,
            krx_fwdg_ord_orgno="00091",
        )
    assert entered == [True]
