"""Alpaca paper extended_hours (hk 6497) — fake transport, no database.

Covers the closed acceptance list:
1. extended_hours=true limit DAY sends extended_hours=true in the request body;
   the default sends a body identical to before (golden).
2. market or non-DAY with extended_hours=true is refused before the transport.
3. A submit whose extended_hours differs from the packet bound at preview is
   refused.

Everything here runs against an in-memory fake transport and a fake ledger, so
the file is DB-free (no integration marker, no DB fixture).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.mcp_server.tooling import alpaca_paper_orders as orders_mod
from app.mcp_server.tooling import alpaca_paper_preview as preview_mod
from app.mcp_server.tooling.alpaca_paper_preview import PreviewOrderInput
from app.services.alpaca_paper_submit_service import (
    AlpacaPaperSubmitCoordinator,
    build_canonical_payload,
    canonical_hash,
    derive_client_order_id,
)
from app.services.brokers.alpaca.config import AlpacaPaperSettings
from app.services.brokers.alpaca.exceptions import AlpacaPaperRequestError
from app.services.brokers.alpaca.schemas import OrderRequest
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService
from app.services.paper_approval_packet import PaperApprovalPacket

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 25, 14, 0, tzinfo=UTC)

# Golden request body for the default (regular-session) limit DAY buy. Captured
# from the code before hk 6497 (see the PR body). The key order matches the
# OrderRequest field order; extended_hours is not present.
GOLDEN_DEFAULT_BODY_JSON = (
    '{"symbol":"AAPL","qty":"2","side":"buy","type":"limit",'
    '"time_in_force":"day","limit_price":"150.25",'
    '"client_order_id":"rob73-ff98cbd355599463"}'
)
GOLDEN_DEFAULT_COID = "rob73-ff98cbd355599463"


class _FakeTransport:
    """Records every request and answers like Alpaca's POST /v2/orders."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"method": method, "path": path, "json": kwargs.get("json")})
        body = kwargs.get("json") or {}
        order = {
            "id": "11111111-2222-3333-4444-555555555555",
            "client_order_id": body.get("client_order_id"),
            "symbol": body.get("symbol"),
            "side": body.get("side"),
            "type": body.get("type"),
            "time_in_force": body.get("time_in_force"),
            "status": "new",
            "qty": body.get("qty"),
            "limit_price": body.get("limit_price"),
        }
        return httpx.Response(200, json=order)


class _FakeLedger:
    """Only the ledger calls the buy submit path makes on a fresh order."""

    def __init__(self) -> None:
        self.session = SimpleNamespace(expire_all=lambda: None)
        self.recorded: list[str] = []

    async def get_execution_by_client_order_id(self, client_order_id: str) -> None:
        return None

    async def claim_submit(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(won=True, row=None)

    async def record_submit(
        self, client_order_id: str, order_dict: Any, **kwargs: Any
    ) -> None:
        self.recorded.append(client_order_id)

    async def record_submit_failure(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("no broker failure is expected in these tests")


class _RecordingFactory:
    """Service/preview factory that fails the test if anything calls it."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        raise AssertionError("broker/service must not be reached on a refused request")


class _PreviewOfflineService:
    """Preview service whose account read is unavailable (non-fatal in preview)."""

    async def get_cash(self) -> Any:
        raise AlpacaPaperRequestError("offline in unit test")


def _service(transport: _FakeTransport) -> AlpacaPaperBrokerService:
    return AlpacaPaperBrokerService(
        transport=transport,
        settings=AlpacaPaperSettings(api_key="test-key", api_secret="test-secret"),
    )


def _limit_day_canonical(*, extended_hours: bool) -> dict[str, Any]:
    return build_canonical_payload(
        symbol="AAPL",
        side="buy",
        type="limit",
        time_in_force="day",
        qty=Decimal("2"),
        notional=None,
        limit_price=Decimal("150.25"),
        asset_class="us_equity",
        extended_hours=extended_hours,
    )


def _packet(
    *,
    client_order_id: str,
    preview_canonical: dict[str, Any],
    execution_extended_hours: bool,
) -> PaperApprovalPacket:
    """Packet built from a preview, as the manual tool does, bound to that preview."""
    return PaperApprovalPacket(
        signal_source="manual_operator",
        artifact_id=uuid.uuid4(),
        signal_symbol="AAPL",
        signal_venue="upbit",
        execution_symbol="AAPL",
        execution_venue="alpaca_paper",
        execution_asset_class="us_equity",
        side="buy",
        max_qty=Decimal("2"),
        qty_source="manual_operator",
        expected_lifecycle_step="previewed",
        lifecycle_correlation_id=client_order_id,
        client_order_id=client_order_id,
        expires_at=NOW + timedelta(minutes=2),
        account_mode="alpaca_paper",
        origin="manual",
        market_data_asof=NOW,
        market_data_source="unit-test",
        preview_payload_hash=canonical_hash(preview_canonical),
        execution_order_type="limit",
        execution_time_in_force="day",
        execution_extended_hours=execution_extended_hours,
        reference_price=Decimal("150.25"),
    )


def _coordinator(
    ledger: _FakeLedger, service: AlpacaPaperBrokerService
) -> AlpacaPaperSubmitCoordinator:
    return AlpacaPaperSubmitCoordinator(
        ledger,
        lambda: service,
        now_fn=lambda: NOW,
        expected_account_mode="alpaca_paper",
    )


# ---------------------------------------------------------------------------
# 1. Request body: golden default, and extended_hours=true reaches the wire
# ---------------------------------------------------------------------------
def test_default_order_request_body_is_byte_identical_to_golden() -> None:
    request = OrderRequest(
        symbol="AAPL",
        qty=Decimal("2"),
        notional=None,
        side="buy",
        type="limit",
        time_in_force="day",
        limit_price=Decimal("150.25"),
        stop_price=None,
        client_order_id=GOLDEN_DEFAULT_COID,
    )
    body = request.model_dump(mode="json", exclude_none=True)
    assert json.dumps(body, separators=(",", ":")) == GOLDEN_DEFAULT_BODY_JSON
    assert "extended_hours" not in body


def test_default_canonical_and_client_order_id_are_unchanged() -> None:
    canonical = _limit_day_canonical(extended_hours=False)
    assert "extended_hours" not in canonical
    assert derive_client_order_id(canonical) == GOLDEN_DEFAULT_COID


@pytest.mark.asyncio
async def test_extended_hours_limit_day_sends_extended_hours_true_in_body() -> None:
    transport = _FakeTransport()
    ledger = _FakeLedger()
    canonical = _limit_day_canonical(extended_hours=True)
    coid = derive_client_order_id(canonical)
    packet = _packet(
        client_order_id=coid, preview_canonical=canonical, execution_extended_hours=True
    )

    outcome = await _coordinator(ledger, _service(transport)).submit(
        packet, submit_canonical=canonical
    )

    assert outcome.status == "submitted"
    assert len(transport.calls) == 1
    assert transport.calls[0]["path"] == "/v2/orders"
    body = transport.calls[0]["json"]
    assert body["extended_hours"] is True
    assert body["type"] == "limit"
    assert body["time_in_force"] == "day"
    assert body["client_order_id"] == coid
    # extended-hours coid is distinct from the regular-session coid for the same order
    assert coid != GOLDEN_DEFAULT_COID


@pytest.mark.asyncio
async def test_default_limit_day_submit_sends_body_identical_to_golden() -> None:
    transport = _FakeTransport()
    ledger = _FakeLedger()
    canonical = _limit_day_canonical(extended_hours=False)
    packet = _packet(
        client_order_id=GOLDEN_DEFAULT_COID,
        preview_canonical=canonical,
        execution_extended_hours=False,
    )

    outcome = await _coordinator(ledger, _service(transport)).submit(
        packet, submit_canonical=canonical
    )

    assert outcome.status == "submitted"
    sent = transport.calls[0]["json"]
    assert "extended_hours" not in sent
    assert json.dumps(sent, separators=(",", ":")) == GOLDEN_DEFAULT_BODY_JSON


# ---------------------------------------------------------------------------
# 2. Local refusals before the transport is called
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"type": "market", "limit_price": None}, "requires type=limit"),
        ({"time_in_force": "gtc"}, "requires time_in_force=day"),
        ({"time_in_force": "ioc"}, "requires time_in_force=day"),
    ],
)
def test_preview_refuses_market_or_non_day_extended_before_network(
    overrides: dict[str, Any], message: str
) -> None:
    base: dict[str, Any] = {
        "symbol": "AAPL",
        "side": "buy",
        "type": "limit",
        "qty": Decimal("2"),
        "notional": None,
        "time_in_force": "day",
        "limit_price": Decimal("150.25"),
        "asset_class": "us_equity",
        "extended_hours": True,
    }
    base.update(overrides)
    with pytest.raises(ValueError, match=message):
        PreviewOrderInput(**base)


def test_preview_accepts_fractional_limit_day_extended() -> None:
    # Alpaca docs (fetched 2026-11-25): fractional qty is permitted in extended hours
    # for exchange-listed, fractionable assets. Fractionability is the broker's call,
    # so a fractional limit DAY extended-hours request is not refused locally.
    validated = PreviewOrderInput(
        symbol="NVDA",
        side="buy",
        type="limit",
        qty=Decimal("0.5"),
        notional=None,
        time_in_force="day",
        limit_price=Decimal("100"),
        asset_class="us_equity",
        extended_hours=True,
    )
    assert validated.extended_hours is True
    assert _canonical_of(validated)["extended_hours"] is True


def _canonical_of(validated: PreviewOrderInput) -> dict[str, Any]:
    return orders_mod._canonical_payload(validated)


def test_crypto_extended_hours_is_refused_locally() -> None:
    with pytest.raises(ValueError, match="us_equity"):
        PreviewOrderInput(
            symbol="BTC/USD",
            side="buy",
            type="limit",
            qty=Decimal("0.01"),
            notional=None,
            time_in_force="gtc",
            limit_price=Decimal("50000"),
            asset_class="crypto",
            extended_hours=True,
        )


@pytest.mark.asyncio
async def test_submit_tool_refuses_market_extended_before_transport() -> None:
    factory = _RecordingFactory()
    orders_mod.set_alpaca_paper_orders_service_factory(factory)
    try:
        with pytest.raises(ValueError, match="requires type=limit"):
            await orders_mod.alpaca_paper_submit_order(
                symbol="AAPL",
                side="buy",
                type="market",
                qty=Decimal("1"),
                time_in_force="day",
                quote_snapshot_id=1,
                extended_hours=True,
                confirm=True,
            )
    finally:
        orders_mod.reset_alpaca_paper_orders_service_factory()
    assert factory.calls == 0


@pytest.mark.asyncio
async def test_preview_tool_refuses_market_extended_before_service() -> None:
    factory = _RecordingFactory()
    preview_mod.set_alpaca_paper_preview_service_factory(factory)
    try:
        with pytest.raises(ValueError, match="requires type=limit"):
            await preview_mod.alpaca_paper_preview_order(
                symbol="AAPL",
                side="buy",
                type="market",
                qty=Decimal("1"),
                time_in_force="day",
                extended_hours=True,
            )
    finally:
        preview_mod.reset_alpaca_paper_preview_service_factory()
    assert factory.calls == 0


# ---------------------------------------------------------------------------
# Tool echoes: the key appears only when true (default output unchanged)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_submit_confirm_false_echo_has_no_key_by_default() -> None:
    result = await orders_mod.alpaca_paper_submit_order(
        symbol="AAPL",
        side="buy",
        type="limit",
        qty=Decimal("2"),
        time_in_force="day",
        limit_price=Decimal("150.25"),
    )
    assert "extended_hours" not in result["order_request"]
    assert result["client_order_id"] == GOLDEN_DEFAULT_COID


@pytest.mark.asyncio
async def test_submit_confirm_false_echo_carries_extended_hours_when_true() -> None:
    result = await orders_mod.alpaca_paper_submit_order(
        symbol="AAPL",
        side="buy",
        type="limit",
        qty=Decimal("2"),
        time_in_force="day",
        limit_price=Decimal("150.25"),
        extended_hours=True,
    )
    assert result["order_request"]["extended_hours"] is True
    assert result["client_order_id"] != GOLDEN_DEFAULT_COID


@pytest.mark.asyncio
async def test_preview_echo_has_no_key_by_default_and_carries_it_when_true() -> None:
    preview_mod.set_alpaca_paper_preview_service_factory(_PreviewOfflineService)
    try:
        default = await preview_mod.alpaca_paper_preview_order(
            symbol="AAPL",
            side="buy",
            type="limit",
            qty=Decimal("2"),
            time_in_force="day",
            limit_price=Decimal("150.25"),
        )
        extended = await preview_mod.alpaca_paper_preview_order(
            symbol="AAPL",
            side="buy",
            type="limit",
            qty=Decimal("2"),
            time_in_force="day",
            limit_price=Decimal("150.25"),
            extended_hours=True,
        )
    finally:
        preview_mod.reset_alpaca_paper_preview_service_factory()
    assert "extended_hours" not in default["order_request"]
    assert extended["order_request"]["extended_hours"] is True


# ---------------------------------------------------------------------------
# 3. Preview/submit mismatch on extended_hours is refused
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("preview_extended", "submit_extended"),
    [(True, False), (False, True)],
)
@pytest.mark.asyncio
async def test_submit_extended_hours_differing_from_preview_is_refused(
    preview_extended: bool, submit_extended: bool
) -> None:
    transport = _FakeTransport()
    ledger = _FakeLedger()
    submit_canonical = _limit_day_canonical(extended_hours=submit_extended)
    preview_canonical = _limit_day_canonical(extended_hours=preview_extended)
    # The packet's client_order_id matches the submit canonical, so the refusal
    # is the extended_hours check and not a server-key mismatch.
    coid = derive_client_order_id(submit_canonical)
    packet = _packet(
        client_order_id=coid,
        preview_canonical=preview_canonical,
        execution_extended_hours=preview_extended,
    )

    outcome = await _coordinator(ledger, _service(transport)).submit(
        packet, submit_canonical=submit_canonical
    )

    assert outcome.status == "rejected"
    assert outcome.reason_code == "order_extended_hours_mismatch"
    assert outcome.broker_called is False
    assert transport.calls == []
    assert ledger.recorded == []
