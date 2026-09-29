"""Manual listing reader keeps pagination and account proof on the mock host."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

import app.services.brokers.nhplug.client as client_module
from app.services.brokers.nhplug.client import NHPlugMockClient
from app.services.nhplug_mock.ledger import NHPlugMockLedger

pytestmark = pytest.mark.unit
FIXTURES = json.loads(
    (
        Path(__file__).resolve().parents[3] / "fixtures/nhplug_stage2/responses.json"
    ).read_text()
)


@pytest.fixture(autouse=True)
def enable_disposable_stage2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    for name in ("KEY", "TIME", "DB", "HOST", "VENDOR"):
        monkeypatch.setenv(f"NHPLUG_STAGE2_{name}_CONFIRMED", "true")

    async def account_ref(engine: AsyncEngine, act_no: str, keys: object) -> UUID:
        assert act_no == "MOCK-1"
        return UUID(int=711)

    monkeypatch.setattr(client_module, "resolve_account_ref", account_ref)


def fake_ledger() -> NHPlugMockLedger:
    return NHPlugMockLedger(cast(AsyncEngine, object()))


async def listing_client() -> NHPlugMockClient:
    async def token() -> str:
        return "test-token"

    wire = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "rsp_cd": "00000",
                "Output_0": [{"acct_no": "MOCK-1", "acct_type": "03"}],
            },
            request=request,
        )
    )
    client = NHPlugMockClient(
        app_key="test", app_secret="test", token_provider=token, transport=wire
    )
    await client.verify_and_bind_mock_account("MOCK-1")
    return client


@pytest.mark.asyncio
async def test_complete_listing_follows_header_key_and_pins_request_body() -> None:
    client = await listing_client()
    observed: list[tuple[str, dict[str, object], str | None]] = []

    def reply(request: httpx.Request) -> httpx.Response:
        observed.append(
            (request.url.path, json.loads(request.content), request.headers.get("cts"))
        )
        if len(observed) == 1:
            return httpx.Response(
                200,
                headers={"cts": "page-next"},
                json=FIXTURES["listing_open_one"],
                request=request,
            )
        return httpx.Response(200, json={"rsp_cd": "13578"}, request=request)

    client._transport = httpx.MockTransport(reply)
    found = await client.fetch_order_listing(
        ledger=fake_ledger(), keys={}, order_date="20260926", scope="all"
    )
    assert found.complete and found.pages == 2 and found.find(1000123) is not None
    assert found.account_ref == UUID(int=711) and found.order_date == date(2026, 9, 26)
    assert observed == [
        (
            "/krstock/inquiry/v1/dailyOrderExecution",
            {
                "Input_0": {
                    "orr_dt": "20260926",
                    "act_no": "MOCK-1",
                    "orr_mkt_cd": "00",
                    "ost_cns_dit": "0",
                }
            },
            None,
        ),
        (
            "/krstock/inquiry/v1/dailyOrderExecution",
            {
                "Input_0": {
                    "orr_dt": "20260926",
                    "act_no": "MOCK-1",
                    "orr_mkt_cd": "00",
                    "ost_cns_dit": "0",
                }
            },
            "page-next",
        ),
    ]


@pytest.mark.asyncio
async def test_continuation_without_key_never_becomes_complete() -> None:
    client = await listing_client()
    observed = 0

    def reply(request: httpx.Request) -> httpx.Response:
        nonlocal observed
        observed += 1
        return httpx.Response(
            200,
            headers={"cts_flag": "Y"},
            json={"rsp_cd": "00000", "Output_1": []},
            request=request,
        )

    client._transport = httpx.MockTransport(reply)
    found = await client.fetch_order_listing(
        ledger=fake_ledger(), keys={}, order_date="20260926", scope="open"
    )
    assert not found.complete and observed == 1
