from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import order_validation as ov
from app.mcp_server.tooling import orders_toss_variants as otv

_TRADER_AGENT_ID = "6b2192cc-14fa-4335-b572-2fe1e0cb54a7"


class _TossClient:
    def __init__(self) -> None:
        self.placed_payloads: list[dict] = []
        self.current_price = Decimal("100")
        self.average_purchase_price = Decimal("200")

    async def aclose(self) -> None:
        return None

    async def warnings(self, symbol: str):
        return []

    async def holdings(self, *, symbol: str | None = None):
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    symbol=symbol or "AAPL",
                    average_purchase_price=self.average_purchase_price,
                )
            ],
            raw_overview={},
        )

    async def prices(self, symbols):
        return [
            SimpleNamespace(
                symbol=symbols[0],
                last_price=self.current_price,
                currency="USD",
            )
        ]

    async def list_orders(self, *, status: str, symbol: str | None = None, **kwargs):
        return SimpleNamespace(orders=[], next_cursor=None, has_next=False)

    async def place_order(self, payload: dict):
        self.placed_payloads.append(payload)
        return SimpleNamespace(
            order_id="toss-loss-cut-order",
            client_order_id=payload["clientOrderId"],
        )


def _configure_toss(monkeypatch) -> _TossClient:
    client = _TossClient()
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])
    monkeypatch.setattr(
        otv.TossReadClient,
        "from_settings",
        lambda: client,
    )
    monkeypatch.setattr(otv.settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv.settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(otv, "_nxt_preflight_context", AsyncMock(return_value=None))
    monkeypatch.setattr(
        otv,
        "_preview_cost_context",
        AsyncMock(
            return_value={
                "estimated_value": "99",
                "estimated_value_currency": "USD",
                "fee": "0",
                "fee_currency": "USD",
                "fx_cost_full_conversion": "0",
                "fx_cost_full_conversion_currency": "KRW",
                "estimated_costs": {},
            }
        ),
    )
    return client


def _configure_loss_cut_preconditions(
    monkeypatch,
    *,
    caller_agent_id: str = _TRADER_AGENT_ID,
) -> None:
    retro = SimpleNamespace(
        id=42,
        symbol="AAPL",
        trigger_type="stop_loss",
        created_at=datetime.now(UTC),
    )
    monkeypatch.setattr(ov, "get_caller_agent_id", lambda: caller_agent_id)
    monkeypatch.setattr(
        ov,
        "_get_retrospective_by_id_for_loss_cut",
        AsyncMock(return_value=retro),
    )
    monkeypatch.setattr(ov, "_loss_cut_max_slip_value", lambda: 0.02)


@pytest.mark.asyncio
async def test_toss_proposal_loss_cut_allows_missing_issue_without_paperclip(
    monkeypatch,
):
    _configure_toss(monkeypatch)
    _configure_loss_cut_preconditions(monkeypatch)

    result = await _preview_loss_cut(approval_issue_id=None)

    assert result["success"] is True
    assert result["retrospective_id"] == 42


@pytest.mark.asyncio
async def test_toss_proposal_loss_cut_still_rejects_denied_caller(monkeypatch):
    _configure_toss(monkeypatch)
    _configure_loss_cut_preconditions(monkeypatch, caller_agent_id="caller-denied")

    result = await _preview_loss_cut()

    assert result["success"] is False
    assert any("not permitted" in item for item in result["violations"])


@pytest.mark.asyncio
async def test_toss_direct_loss_cut_points_to_proposal_flow(monkeypatch):
    _configure_toss(monkeypatch)
    _configure_loss_cut_preconditions(monkeypatch)

    result = await otv.toss_preview_order(
        symbol="AAPL",
        side="sell",
        order_type="limit",
        quantity="1",
        price="99",
        market="us",
        account_mode="toss_live",
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
    )

    assert result["success"] is False
    assert result["violations"] == [
        "loss_cut_direct_path_disabled_use_order_proposal_create"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("price", "success"),
    [("99", True), ("97", False)],
    ids=["within_band", "below_band"],
)
async def test_toss_loss_cut_preview_applies_slip_band(monkeypatch, price, success):
    _configure_toss(monkeypatch)
    _configure_loss_cut_preconditions(monkeypatch)

    result = await _preview_loss_cut(price=price)

    assert result["success"] is success
    if success:
        assert result["exit_intent"] == "loss_cut"
        assert result["retrospective_id"] == 42
        assert result["loss_cut_slip_band"] == pytest.approx(98.0)
        assert result["avg_buy_price"] == "200"
    else:
        assert "slip band floor" in result["error"]


@pytest.mark.asyncio
async def test_toss_loss_cut_preview_fails_closed_without_current_price(monkeypatch):
    _configure_toss(monkeypatch)
    _configure_loss_cut_preconditions(monkeypatch)
    monkeypatch.setattr(
        otv,
        "_preview_price_context",
        AsyncMock(return_value=(None, None, "quote unavailable")),
    )

    result = await _preview_loss_cut()

    assert result["success"] is False
    assert "current price" in result["error"]


async def _preview_loss_cut(
    *, price: str = "99", approval_issue_id: str | None = "ROB-858"
) -> dict:
    with otv._bind_order_proposal_context(
        client_order_id="tosprop-loss-cut", correlation_id=None, rung=0
    ):
        return await otv.toss_preview_order(
            symbol="AAPL",
            side="sell",
            order_type="limit",
            quantity="1",
            price=price,
            market="us",
            account_mode="toss_live",
            exit_intent="loss_cut",
            exit_reason="stop_loss",
            retrospective_id=42,
            approval_issue_id=approval_issue_id,
        )


@pytest.mark.asyncio
async def test_toss_loss_cut_submit_records_audit_binding(monkeypatch):
    client = _configure_toss(monkeypatch)
    _configure_loss_cut_preconditions(monkeypatch)
    monkeypatch.setattr(otv.settings, "toss_approval_hash_mode", "off")
    record_send = AsyncMock(
        return_value={
            "ledger_id": 858,
            "broker_status": "accepted",
            "fill_recorded": False,
            "journal_created": False,
        }
    )
    monkeypatch.setattr(otv, "record_toss_place_order", record_send)
    preview = await _preview_loss_cut()

    with otv._bind_order_proposal_context(
        client_order_id="tosprop-loss-cut", correlation_id="corr", rung=0
    ):
        result = await otv.toss_place_order(
            symbol="AAPL",
            side="sell",
            order_type="limit",
            quantity="1",
            price="99",
            market="us",
            dry_run=False,
            confirm=True,
            account_mode="toss_live",
            approval_hash=preview["approval_hash"],
            exit_intent="loss_cut",
            exit_reason="stop_loss",
            retrospective_id=42,
            approval_issue_id="ROB-858",
        )

    assert result["success"] is True
    assert len(client.placed_payloads) == 1
    assert record_send.await_args.kwargs["exit_intent"] == "loss_cut"
    assert record_send.await_args.kwargs["retrospective_id"] == 42
    assert record_send.await_args.kwargs["approval_issue_id"] == "ROB-858"


@pytest.mark.asyncio
async def test_toss_loss_cut_submit_does_not_query_paperclip(monkeypatch):
    client = _configure_toss(monkeypatch)
    _configure_loss_cut_preconditions(monkeypatch)
    record_send = AsyncMock(
        return_value={
            "ledger_id": 858,
            "broker_status": "accepted",
            "fill_recorded": False,
            "journal_created": False,
        }
    )
    monkeypatch.setattr(otv, "record_toss_place_order", record_send)
    preview = await _preview_loss_cut()
    with otv._bind_order_proposal_context(
        client_order_id="tosprop-loss-cut", correlation_id="corr", rung=0
    ):
        result = await otv.toss_place_order(
            symbol="AAPL",
            side="sell",
            order_type="limit",
            quantity="1",
            price="99",
            market="us",
            dry_run=False,
            confirm=True,
            account_mode="toss_live",
            approval_hash=preview["approval_hash"],
            exit_intent="loss_cut",
            exit_reason="stop_loss",
            retrospective_id=42,
            approval_issue_id=None,
        )

    assert result["success"] is True
    assert len(client.placed_payloads) == 1
    record_send.assert_awaited_once()
    assert record_send.await_args.kwargs["approval_issue_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token_case", "error_code"),
    [
        ("missing", "loss_cut_approval_hash_required"),
        ("mismatch", "approval_hash_mismatch"),
        ("expired", "approval_expired"),
    ],
)
async def test_toss_loss_cut_submit_requires_supplied_valid_hash_in_off_mode(
    monkeypatch, token_case, error_code
):
    client = _configure_toss(monkeypatch)
    _configure_loss_cut_preconditions(monkeypatch)
    monkeypatch.setattr(otv.settings, "toss_approval_hash_mode", "off")
    issued = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(otv, "now_kst", lambda: issued)
    preview = await _preview_loss_cut()
    approval_hash = None if token_case == "missing" else preview["approval_hash"]
    price = "99.5" if token_case == "mismatch" else "99"
    if token_case == "expired":
        monkeypatch.setattr(
            otv,
            "now_kst",
            lambda: issued + timedelta(seconds=301),
        )

    with otv._bind_order_proposal_context(
        client_order_id="tosprop-loss-cut", correlation_id="corr", rung=0
    ):
        result = await otv.toss_place_order(
            symbol="AAPL",
            side="sell",
            order_type="limit",
            quantity="1",
            price=price,
            market="us",
            dry_run=False,
            confirm=True,
            account_mode="toss_live",
            approval_hash=approval_hash,
            exit_intent="loss_cut",
            exit_reason="stop_loss",
            retrospective_id=42,
            approval_issue_id=None,
        )

    assert result["success"] is False
    assert result["error_code"] == error_code
    assert client.placed_payloads == []
