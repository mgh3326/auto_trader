import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling import order_proposal_tools as opt
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.target_order import TargetOrderSnapshot


class _FakeNotifier:
    def __init__(self, *, message_id: int | None = 4242) -> None:
        self.calls: list[tuple[str, dict, str]] = []
        self.edited: list[tuple[str, int, str, object]] = []
        self._message_id = message_id

    async def send_approval_message(self, text, inline_keyboard, *, chat_id):
        self.calls.append((text, inline_keyboard, chat_id))
        return self._message_id

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((chat_id, message_id, text, reply_markup))
        return True


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
async def test_toss_create_warns_on_same_account_pending_buying_power_shortfall(
    monkeypatch,
):
    broker_account_id = f"rob-861-{uuid.uuid4()}"

    async def reader(**kwargs):
        assert kwargs["account_mode"] == "toss_live"
        assert kwargs["broker_account_id"] == broker_account_id
        assert kwargs["currency"] == "KRW"
        return Decimal("500000")

    monkeypatch.setattr(opt, "default_buying_power_reader", reader, raising=False)
    first = await opt.order_proposal_create(
        **_create_kwargs(
            account_mode="toss_live",
            broker_account_id=broker_account_id,
            rungs=[
                {
                    "rung_index": 0,
                    "side": "buy",
                    "quantity": "2",
                    "limit_price": "100000",
                    "notional": None,
                }
            ],
        )
    )
    created = await opt.order_proposal_create(
        **_create_kwargs(
            account_mode="toss_live",
            broker_account_id=broker_account_id,
            rungs=[
                {
                    "rung_index": 0,
                    "side": "buy",
                    "quantity": "5",
                    "limit_price": "100000",
                    "notional": None,
                }
            ],
        )
    )

    assert first["success"] is True
    assert created["success"] is True
    assert created["buying_power_advisory"] == [
        {
            "status": "insufficient",
            "currency": "KRW",
            "buying_power": "500000",
            "pending_required": "700000",
            "shortfall": "200000",
            "skipped_market_rungs": 0,
            "warning": (
                "매수가능 500,000원 / 승인대기 필요 700,000원 → 부족 200,000원"
            ),
        }
    ]
    assert created["warnings"] == [
        "매수가능 500,000원 / 승인대기 필요 700,000원 → 부족 200,000원"
    ]


@pytest.mark.asyncio
async def test_toss_create_reports_sufficient_buying_power_without_warning(monkeypatch):
    broker_account_id = f"rob-861-sufficient-{uuid.uuid4()}"

    async def reader(**kwargs):
        return Decimal("1000000")

    monkeypatch.setattr(opt, "default_buying_power_reader", reader)
    created = await opt.order_proposal_create(
        **_create_kwargs(
            account_mode="toss_live",
            broker_account_id=broker_account_id,
            rungs=[
                {
                    "rung_index": 0,
                    "side": "buy",
                    "quantity": "2",
                    "limit_price": "100000",
                    "notional": None,
                }
            ],
        )
    )

    assert created["success"] is True
    assert created["buying_power_advisory"][0]["status"] == "sufficient"
    assert created["buying_power_advisory"][0]["pending_required"] == "200000"
    assert created["buying_power_advisory"][0]["shortfall"] == "0"
    assert created["buying_power_advisory"][0]["warning"] is None
    assert "warnings" not in created


@pytest.mark.asyncio
async def test_toss_create_reader_failure_is_non_blocking_unavailable_advisory(
    monkeypatch,
):
    broker_account_id = f"rob-861-unavailable-{uuid.uuid4()}"

    async def failed_reader(**kwargs):
        raise RuntimeError("buying-power unavailable")

    monkeypatch.setattr(opt, "default_buying_power_reader", failed_reader)
    created = await opt.order_proposal_create(
        **_create_kwargs(
            account_mode="toss_live",
            broker_account_id=broker_account_id,
        )
    )

    assert created["success"] is True
    assert created["buying_power_advisory"][0]["status"] == "unavailable"
    assert created["buying_power_advisory"][0]["buying_power"] is None
    assert created["buying_power_advisory"][0]["pending_required"] == "700000"
    assert "warnings" not in created


@pytest.mark.asyncio
async def test_create_advisory_skips_kis_and_sell_proposals(monkeypatch):
    async def forbidden_reader(**kwargs):
        pytest.fail(f"unsupported create advisory read: {kwargs}")

    monkeypatch.setattr(opt, "default_buying_power_reader", forbidden_reader)
    kis = await opt.order_proposal_create(**_create_kwargs())
    toss_sell = await opt.order_proposal_create(
        **_create_kwargs(
            account_mode="toss_live",
            side="sell",
            rungs=[
                {
                    "rung_index": 0,
                    "side": "sell",
                    "quantity": "10",
                    "limit_price": "70000",
                    "notional": None,
                }
            ],
        )
    )

    assert kis["success"] is True
    assert toss_sell["success"] is True
    assert "buying_power_advisory" not in kis
    assert "buying_power_advisory" not in toss_sell


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market_alias", "canonical_market"),
    [("kr", "equity_kr"), ("us", "equity_us")],
)
async def test_create_normalizes_market_alias_before_storage_and_payload_hash(
    market_alias, canonical_market
):
    alias = await opt.order_proposal_create(**_create_kwargs(market=market_alias))
    canonical = await opt.order_proposal_create(
        **_create_kwargs(market=canonical_market)
    )

    assert alias["success"] is True
    assert canonical["success"] is True
    got = await opt.order_proposal_get(alias["proposal_id"])
    async with opt.AsyncSessionLocal() as session:
        service = opt.OrderProposalsService(session)
        alias_group, _ = await service.get_proposal(uuid.UUID(alias["proposal_id"]))
        canonical_group, _ = await service.get_proposal(
            uuid.UUID(canonical["proposal_id"])
        )

    assert got["proposal"]["market"] == canonical_market
    assert alias_group.market == canonical_market
    assert canonical_group.market == canonical_market
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
async def test_supersede_edits_old_approval_message_and_removes_buttons(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "chat-1"
    )
    fake_notifier = _FakeNotifier(message_id=9999)
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.notifier.get_trade_notifier",
        lambda: fake_notifier,
    )
    monkeypatch.setattr(opt, "_get_trade_notifier", lambda: fake_notifier)
    original = await opt.order_proposal_create(**_create_kwargs())

    replacement = await opt.order_proposal_create(
        **_create_kwargs(
            supersedes_proposal_id=original["proposal_id"],
            rungs=[
                {
                    "rung_index": 0,
                    "side": "buy",
                    "quantity": "10",
                    "limit_price": "69000",
                    "notional": None,
                }
            ],
        )
    )

    assert replacement["success"] is True
    assert fake_notifier.edited == [
        (
            "chat-1",
            9999,
            f"🔁 → {replacement['proposal_id'][:8]}로 대체됨",
            {"inline_keyboard": []},
        )
    ]


@pytest.mark.asyncio
async def test_supersede_message_edit_setup_failure_is_best_effort(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "chat-1"
    )
    fake_notifier = _FakeNotifier(message_id=9998)
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.notifier.get_trade_notifier",
        lambda: fake_notifier,
    )
    original = await opt.order_proposal_create(**_create_kwargs())

    def notifier_setup_failure():
        raise RuntimeError("notifier setup failed")

    monkeypatch.setattr(opt, "_get_trade_notifier", notifier_setup_failure)
    replacement = await opt.order_proposal_create(
        **_create_kwargs(supersedes_proposal_id=original["proposal_id"])
    )

    assert replacement["success"] is True
    got = await opt.order_proposal_get(original["proposal_id"])
    assert got["proposal"]["lifecycle_state"] == "superseded"


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
async def test_void_unverified_uses_broker_evidence_and_disables_telegram_buttons(
    monkeypatch,
):
    from app.services.order_proposals.broker_gateway import OperatorVoidEvidence

    created = await opt.order_proposal_create(
        **_create_kwargs(account_mode="toss_live")
    )
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as session:
        service = OrderProposalsService(session)
        proposal_id = uuid.UUID(created["proposal_id"])
        for state in ("revalidating", "approved", "submitting"):
            await service.transition_rung(proposal_id, 0, new_state=state)
        await service.record_unverified(
            proposal_id,
            0,
            reason="legacy_timeout",
            idempotency_key="tosprop-legacy-1",
            now=now - timedelta(minutes=6),
        )
        await service.record_approval_dispatch(
            proposal_id,
            message_id=4242,
            chat_id="chat-1",
            now=now,
        )
        await session.commit()

    async def absent_evidence(**_kwargs):
        return {
            0: OperatorVoidEvidence(
                "absent", "toss GET /orders OPEN + CLOSED 2026-07-13..2026-07-13"
            )
        }

    notifier = _FakeNotifier()
    monkeypatch.setattr(opt, "fetch_operator_void_evidence", absent_evidence)
    monkeypatch.setattr(opt, "_get_trade_notifier", lambda: notifier)

    result = await opt.order_proposal_void(created["proposal_id"], "operator cleanup")

    assert result["success"] is True
    assert result["rungs"][0]["state"] == "voided_local_stale"
    assert "outcome=absent" in result["void_reason"]
    assert notifier.edited == [
        (
            "chat-1",
            4242,
            "🗑️ 제안 무효화됨\n사유: " + result["void_reason"].replace("_", "\\_"),
            {"inline_keyboard": []},
        )
    ]


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
