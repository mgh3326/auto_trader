"""Physical-account binding tests for canonical Alpaca routing."""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.brokers.alpaca.config import AlpacaPaperSettings
from app.services.brokers.alpaca.endpoints import PAPER_TRADING_BASE_URL
from app.services.brokers.alpaca.exceptions import AlpacaPaperIdentityMismatch
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService


def _response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _settings() -> AlpacaPaperSettings:
    return AlpacaPaperSettings(
        api_key="key",
        api_secret="secret",
        base_url=PAPER_TRADING_BASE_URL,
        expected_account_id_suffix="c60c74",
        expected_account_number_suffix="4AE7",
    )


def _account(account_id: str, account_number: str) -> dict[str, object]:
    return {
        "id": account_id,
        "account_number": account_number,
        "buying_power": "100",
        "cash": "100",
        "portfolio_value": "100",
        "status": "ACTIVE",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clean_route_verifies_physical_identity_before_account_use():
    transport = AsyncMock()
    transport.request = AsyncMock(
        return_value=_response(_account("acct-c60c74", "1234AE7"))
    )
    service = AlpacaPaperBrokerService(
        transport=transport, settings=_settings(), profile="clean"
    )

    account = await service.get_account()

    assert account.id == "acct-c60c74"
    assert transport.request.await_count == 2  # bind, then return the snapshot
    assert all(
        call.args[:2] == ("GET", "/v2/account")
        for call in transport.request.await_args_list
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clean_route_fails_closed_on_identity_mismatch():
    transport = AsyncMock()
    transport.request = AsyncMock(
        return_value=_response(_account("acct-other", "1234AE7"))
    )
    service = AlpacaPaperBrokerService(
        transport=transport, settings=_settings(), profile="clean"
    )

    with pytest.raises(AlpacaPaperIdentityMismatch):
        await service.list_positions()

    assert transport.request.await_count == 1
