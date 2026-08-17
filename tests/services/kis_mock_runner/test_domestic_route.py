"""KR-B0 route proof: KIS mock domestic mutations can never emit SOR."""

from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.brokers.kis import constants

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
    """ROB-1263 r3: a follow-up additionally needs its own capability receipt.

    The routing assertion is unchanged; cancel/modify simply cannot reach the
    transport at all without one, so the test holds a short-lease receipt for
    exactly those two operations.
    """
    import app.services.kis_mock_runner.singleton as singleton

    client, parent = _make_client()
    parent._request_with_rate_limit = AsyncMock(return_value=_success_response())
    nxt = AsyncMock(return_value=True)

    class _Reservations:
        async def __call__(self, account_scope: str):
            from app.services.order_send_intent_service import (
                OrderSendIntentReservation,
            )

            return [
                OrderSendIntentReservation(
                    row_id=1, idempotency_key="mock-idempotency-v1:route", side="buy"
                )
            ]

    class _Lease:
        acquired = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    followup = {
        "cancel": singleton.KISMockOperation.FOLLOWUP_CANCEL,
        "modify": singleton.KISMockOperation.FOLLOWUP_MODIFY,
    }.get(operation)

    async with contextlib.AsyncExitStack() as stack:
        if followup is not None:
            await stack.enter_async_context(
                singleton.issue_kis_mock_followup_capability(
                    operation=followup,
                    claim_account_scope="mockpa:v1:route",
                    claim_idempotency_key="mock-idempotency-v1:route",
                    attributed_broker_order_id="00001",
                    known_remainder=Decimal("10"),
                    reservations=_Reservations(),
                    lease_factory=_Lease,
                )
            )
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
    """ROB-1263 r2: the seam is now the lane authority, and the env gate is gone.

    The guard used to consult ``KIS_MOCK_RUNNER_ENABLED`` and pass
    ``enabled=is_mock and <env>`` to ``enforce_kis_mock_mutation_writer``. It now
    enters ``kis_mock_mutation_authority`` for every mock mutation, so the
    recorded fact is *that authority was entered*, not what an env gate said. The
    env var is deliberately left **false** here to prove exactly that.
    """
    import app.services.kis_mock_runner.singleton as singleton

    client, parent = _make_client()
    parent._request_with_rate_limit = AsyncMock(return_value=_success_response())
    entered: list[str] = []

    @asynccontextmanager
    async def fake_authority(*, client, path, caller_pre_send_hook=None, **kwargs):
        entered.append(path)
        yield singleton.KISMockMutationAuthority(grant=None, pre_send_hook=None)

    monkeypatch.delenv("KIS_MOCK_RUNNER_ENABLED", raising=False)
    monkeypatch.setattr(singleton, "kis_mock_mutation_authority", fake_authority)
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
    expected_path = (
        constants.DOMESTIC_ORDER_URL
        if operation == "place"
        else constants.DOMESTIC_ORDER_CANCEL_URL
    )
    assert entered == [expected_path]
