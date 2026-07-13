import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.mcp_server.tooling import order_proposal_tools as opt
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.target_order import TargetOrderSnapshot


class _FakeNotifier:
    def __init__(self, *, message_id: int | None = 4242) -> None:
        self.calls: list[tuple[str, dict, str]] = []
        self._message_id = message_id

    async def send_approval_message(self, text, inline_keyboard, *, chat_id):
        self.calls.append((text, inline_keyboard, chat_id))
        return self._message_id


class _RaisingNotifier:
    async def send_approval_message(self, text, inline_keyboard, *, chat_id):
        raise RuntimeError("telegram down")


def _create_kwargs(**overrides):
    kwargs = {
        "symbol": "005930",
        "market": "equity_kr",
        "account_mode": "kis_live",
        "side": "buy",
        "order_type": "limit",
        "proposer": "operator:sess-dispatch",
        "thesis": "t",
        "strategy": "ladder",
        "rungs": [
            {
                "rung_index": 0,
                "side": "buy",
                "quantity": "10",
                "limit_price": "70000",
                "notional": None,
            }
        ],
    }
    kwargs.update(overrides)
    return kwargs


def _target_snapshot(**overrides):
    payload = {
        "broker_order_id": "manual-upbit-1",
        "symbol": "KRW-AVAX",
        "side": "sell",
        "order_type": "limit",
        "limit_price": "42000",
        "remaining_quantity": "3.5",
        "status": "open",
        "observed_at": "2026-07-11T08:23:00+00:00",
    }
    payload.update(overrides)
    return TargetOrderSnapshot.from_payload(payload)


def _target_create_kwargs(**overrides):
    return _create_kwargs(
        symbol="KRW-AVAX",
        market="crypto",
        account_mode="upbit",
        side="sell",
        action="replace",
        target_broker_order_id="manual-upbit-1",
        rungs=[
            {
                "rung_index": 0,
                "side": "sell",
                "quantity": "3.5",
                "limit_price": "43000",
                "notional": None,
            }
        ],
        **overrides,
    )


@pytest.mark.asyncio
async def test_create_preflights_manual_target_and_returns_action_evidence(monkeypatch):
    calls = []

    async def fake_fetch(**kwargs):
        calls.append(kwargs)
        return _target_snapshot()

    monkeypatch.setattr(opt, "fetch_target_order", fake_fetch)

    created = await opt.order_proposal_create(**_target_create_kwargs())
    got = await opt.order_proposal_get(created["proposal_id"])

    assert created["success"] is True
    assert created["action"] == "replace"
    assert created["target_broker_order_id"] == "manual-upbit-1"
    assert calls[0]["order_id"] == "manual-upbit-1"
    assert got["proposal"]["action"] == "replace"
    assert got["proposal"]["target_broker_order_id"] == "manual-upbit-1"


@pytest.mark.asyncio
async def test_create_lookup_failure_returns_error_without_service_insert(monkeypatch):
    async def failed_fetch(**kwargs):
        raise OrderProposalError("target broker order lookup failed: offline")

    async def insert_must_not_run(*args, **kwargs):
        raise AssertionError("target preflight failure must not create a proposal")

    monkeypatch.setattr(opt, "fetch_target_order", failed_fetch)
    monkeypatch.setattr(
        opt.OrderProposalsService, "create_proposal", insert_must_not_run
    )

    result = await opt.order_proposal_create(**_target_create_kwargs())

    assert result == {
        "success": False,
        "error": "target broker order lookup failed: offline",
    }


@pytest.mark.asyncio
async def test_place_create_never_fetches_a_target(monkeypatch):
    async def fetch_must_not_run(**kwargs):
        raise AssertionError("place proposals must not fetch target orders")

    monkeypatch.setattr(opt, "fetch_target_order", fetch_must_not_run)

    result = await opt.order_proposal_create(**_create_kwargs())

    assert result["success"] is True
    assert result["action"] == "place"
    assert result["target_broker_order_id"] is None


@pytest.mark.asyncio
async def test_create_normalizes_kr_alias_before_storage_and_payload_hash():
    alias = await opt.order_proposal_create(**_create_kwargs(market="kr"))
    canonical = await opt.order_proposal_create(**_create_kwargs(market="equity_kr"))

    assert alias["success"] is True
    assert canonical["success"] is True
    got = await opt.order_proposal_get(alias["proposal_id"])
    async with opt.AsyncSessionLocal() as session:
        service = opt.OrderProposalsService(session)
        alias_group, _ = await service.get_proposal(uuid.UUID(alias["proposal_id"]))
        canonical_group, _ = await service.get_proposal(
            uuid.UUID(canonical["proposal_id"])
        )

    assert got["proposal"]["market"] == "equity_kr"
    assert alias_group.market == "equity_kr"
    assert canonical_group.market == "equity_kr"
    assert alias_group.payload_hash == canonical_group.payload_hash


@pytest.mark.asyncio
async def test_create_rejects_unknown_market_with_allowed_contract_guidance():
    result = await opt.order_proposal_create(
        **_create_kwargs(market="jp", account_mode="toss_live")
    )

    assert result["success"] is False
    assert "allowed: kis_live×equity_kr|equity_us" in result["error"]
    assert "toss_live×equity_kr|equity_us" in result["error"]
    assert "upbit×crypto" in result["error"]
    assert "market aliases kr→equity_kr, us→equity_us" in result["error"]


def test_create_docstring_documents_markets_aliases_and_account_modes():
    doc = opt.order_proposal_create.__doc__ or ""
    for value in (
        "equity_kr",
        "equity_us",
        "crypto",
        "kr",
        "us",
        "kis_live",
        "toss_live",
        "upbit",
    ):
        assert value in doc


@pytest.mark.asyncio
async def test_create_dispatches_telegram_when_enabled_and_allowlisted(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "chat-1"
    )
    fake_notifier = _FakeNotifier(message_id=9999)
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.notifier.get_trade_notifier",
        lambda: fake_notifier,
    )

    created = await opt.order_proposal_create(**_create_kwargs())

    assert created["success"] is True
    assert len(fake_notifier.calls) == 1
    _, _, chat_id = fake_notifier.calls[0]
    assert chat_id == "chat-1"


@pytest.mark.asyncio
async def test_create_does_not_dispatch_when_telegram_disabled(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", False)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "chat-1"
    )
    fake_notifier = _FakeNotifier()
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.notifier.get_trade_notifier",
        lambda: fake_notifier,
    )

    created = await opt.order_proposal_create(**_create_kwargs())

    assert created["success"] is True
    assert fake_notifier.calls == []


@pytest.mark.asyncio
async def test_create_does_not_dispatch_when_allowlist_empty(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "")
    fake_notifier = _FakeNotifier()
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.notifier.get_trade_notifier",
        lambda: fake_notifier,
    )

    created = await opt.order_proposal_create(**_create_kwargs())

    assert created["success"] is True
    assert fake_notifier.calls == []


@pytest.mark.asyncio
async def test_create_succeeds_even_when_notifier_raises(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "chat-1"
    )
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.notifier.get_trade_notifier",
        lambda: _RaisingNotifier(),
    )

    created = await opt.order_proposal_create(**_create_kwargs())

    assert created["success"] is True
    assert "proposal_id" in created


@pytest.mark.asyncio
async def test_create_then_get_then_list():
    created = await opt.order_proposal_create(
        symbol="000660",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="operator:sess-x",
        thesis="t",
        strategy="ladder",
        rungs=[
            {
                "rung_index": 0,
                "side": "buy",
                "quantity": "10",
                "limit_price": "2226000",
                "notional": None,
            }
        ],
    )
    assert created["success"] is True
    pid = created["proposal_id"]

    got = await opt.order_proposal_get(proposal_id=pid)
    assert got["success"] is True
    assert got["proposal"]["symbol"] == "000660"
    assert len(got["rungs"]) == 1

    listed = await opt.order_proposal_list(limit=10, symbol="000660")
    assert listed["success"] is True
    assert any(p["proposal_id"] == pid for p in listed["proposals"])


@pytest.mark.asyncio
async def test_loss_cut_binding_round_trips_through_create_get_list(monkeypatch):
    async def fake_lookup(session, retrospective_id):
        return SimpleNamespace(
            symbol="005930", trigger_type="stop_loss", created_at=datetime.now(UTC)
        )

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    created = await opt.order_proposal_create(
        **_create_kwargs(
            side="sell",
            rungs=[
                {
                    "rung_index": 0,
                    "side": "sell",
                    "quantity": "1",
                    "limit_price": "65000",
                    "notional": None,
                }
            ],
            exit_intent="loss_cut",
            exit_reason="stop_loss",
            retrospective_id=42,
            approval_issue_id="ROB-800",
        )
    )
    got = await opt.order_proposal_get(created["proposal_id"])
    listed = await opt.order_proposal_list(symbol="005930")
    expected = {
        "exit_intent": "loss_cut",
        "exit_reason": "stop_loss",
        "retrospective_id": 42,
        "approval_issue_id": "ROB-800",
    }
    assert {key: got["proposal"][key] for key in expected} == expected
    row = next(
        p for p in listed["proposals"] if p["proposal_id"] == created["proposal_id"]
    )
    assert {key: row[key] for key in expected} == expected


@pytest.mark.asyncio
async def test_void_requires_reason_and_terminalizes_proposal():
    created = await opt.order_proposal_create(**_create_kwargs())
    blank = await opt.order_proposal_void(created["proposal_id"], "   ")
    assert blank == {"success": False, "error": "void reason is required"}
    result = await opt.order_proposal_void(created["proposal_id"], "superseded thesis")
    assert result["success"] is True
    assert result["lifecycle_state"] == "voided"
    assert result["void_reason"] == "superseded thesis"


@pytest.mark.asyncio
async def test_create_rejects_empty_rungs():
    res = await opt.order_proposal_create(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[],
    )
    assert res["success"] is False
    assert "rung" in res["error"].lower()


@pytest.mark.unit
def test_tools_registered_and_names_exported():
    from fastmcp import FastMCP

    mcp = FastMCP(name="t", on_duplicate="error")
    opt.register_order_proposal_tools(mcp)
    assert opt.ORDER_PROPOSAL_TOOL_NAMES == {
        "order_proposal_create",
        "order_proposal_get",
        "order_proposal_list",
        "order_proposal_void",
    }
