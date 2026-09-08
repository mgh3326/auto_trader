from __future__ import annotations

import uuid

import httpx
import pytest

from app.services.brokers.nhplug.account_guard import MockAccountAllowlist
from app.services.brokers.nhplug.errors import (
    NHPlugMockDisabled,
    NHPlugMockOrderRejected,
)
from app.services.brokers.nhplug.orders import NHDomesticOrderClient
from app.services.kis_mock_attribution import MissingAttribution
from app.services.nh_mock_attribution import resolve_attribution

pytestmark = pytest.mark.unit


def _allowlist() -> MockAccountAllowlist:
    return MockAccountAllowlist.from_acctinfo_response(
        payload={"Output_0": [{"acct_no": "mock-account", "acct_type": "03"}]},
        configured_account_no="mock-account",
    )


@pytest.mark.asyncio
async def test_order_gate_off_refuses_before_token_or_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    monkeypatch.delenv("NHPLUG_MOCK_ORDERS_ENABLED", raising=False)
    client = NHDomesticOrderClient(
        app_key="key",
        app_secret="secret",
        token_provider=lambda: (_ for _ in ()).throw(
            AssertionError("token must not be requested")
        ),
        account_allowlist=_allowlist(),
    )
    with pytest.raises(NHPlugMockDisabled):
        await client.place_buy_order(symbol="005930", quantity=1, price=1)


def test_non_mock_account_is_rejected_before_order_client_exists() -> None:
    with pytest.raises(Exception, match="acct_type=03"):
        MockAccountAllowlist.from_acctinfo_response(
            payload={"Output_0": [{"acct_no": "live-account", "acct_type": "01"}]},
            configured_account_no="live-account",
        )


@pytest.mark.asyncio
async def test_nonzero_order_code_never_looks_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    monkeypatch.setenv("NHPLUG_MOCK_ORDERS_ENABLED", "true")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"rsp_cd": "99999"})
    )

    async def token() -> str:
        return "token"

    client = NHDomesticOrderClient(
        app_key="key",
        app_secret="secret",
        token_provider=token,
        account_allowlist=_allowlist(),
        transport=transport,
    )
    with pytest.raises(NHPlugMockOrderRejected) as exc:
        await client.place_buy_order(symbol="005930", quantity=1, price=1)
    assert exc.value.response_code == "99999"


def test_mirror_attribution_refuses_missing_original_or_cohort() -> None:
    with pytest.raises(MissingAttribution):
        resolve_attribution(
            symbol="005930",
            side="buy",
            price=1,
            quantity=1,
            strategy=None,
            correlation_id=None,
            counterfactual_of=None,
            mirror_cohort="mock_counterfactual",
        )


def test_mirror_attribution_requires_the_exact_cohort_label() -> None:
    with pytest.raises(MissingAttribution):
        resolve_attribution(
            symbol="005930",
            side="buy",
            price=1,
            quantity=1,
            strategy=None,
            correlation_id=None,
            counterfactual_of=str(uuid.uuid4()),
            mirror_cohort=None,
        )


@pytest.mark.asyncio
async def test_confirm_gate_blocks_before_attribution_or_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import orders_nhplug_variants

    monkeypatch.setattr(orders_nhplug_variants, "_config_error", lambda **_: None)
    result = await orders_nhplug_variants.nh_mock_place_order(
        symbol="005930", side="buy", quantity=1, price=1, dry_run=False, confirm=False
    )
    assert result["error_code"] == "confirm_required"
