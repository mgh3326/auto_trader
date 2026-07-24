import functools
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
from app.telegram_contract import TelegramMethodResult, telegram_text_length


def _unique_chat() -> str:
    """Per-test chat id — batches scope by chat, so unique ids keep the
    shared test DB's 10-minute batch windows from leaking across tests."""
    return f"chat-{uuid.uuid4().hex[:10]}"


class _FakeNotifier:
    def __init__(self, *, message_id: int | None = 4242) -> None:
        self.calls: list[tuple[str, dict, str]] = []
        self.edited: list[tuple[str, int, str, object]] = []
        self._message_id = message_id

    async def send_approval_message(
        self, text, inline_keyboard, *, chat_id, parse_mode="Markdown"
    ):
        self.calls.append((text, inline_keyboard, chat_id))
        if self._message_id is None:
            return TelegramMethodResult.failed(
                payload_chars=telegram_text_length(text),
                failure_code="telegram_error_400",
                status_code=400,
                error_code=400,
            )
        return TelegramMethodResult(
            ok=True,
            message_id=self._message_id,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=telegram_text_length(text),
        )

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((chat_id, message_id, text, reply_markup))
        return TelegramMethodResult(
            ok=True,
            message_id=message_id,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=telegram_text_length(text),
        )


class _RaisingNotifier:
    async def send_approval_message(
        self, text, inline_keyboard, *, chat_id, parse_mode="Markdown"
    ):
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


def _toss_target_create_kwargs(action: str, *, market="equity_kr", **overrides):
    symbol = "005930" if market == "equity_kr" else "AAPL"
    return _create_kwargs(
        symbol=symbol,
        market=market,
        account_mode="toss_live",
        side="sell",
        action=action,
        target_broker_order_id="broker-1",
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
@pytest.mark.parametrize("action", ["replace", "cancel"])
@pytest.mark.parametrize("market", ["equity_kr", "equity_us"])
async def test_create_toss_live_target_action_preflights_and_creates_proposal(
    monkeypatch, action, market
):
    """ROB-972: order_proposal_create(action='cancel'|'replace') must accept
    toss_live x equity_kr/us. Only fetch_target_order (read-only preflight) is
    ever imported/called by this tool -- broker_gateway.cancel_target_order
    isn't imported here at all, so there is no code path for a mutation to
    leak into proposal creation regardless of account_mode.
    """
    calls = []
    symbol = "005930" if market == "equity_kr" else "AAPL"

    async def fake_fetch(**kwargs):
        calls.append(kwargs)
        return _target_snapshot(
            broker_order_id="broker-1",
            symbol=symbol,
            side="sell",
            limit_price="43000",
            remaining_quantity="3.5",
        )

    monkeypatch.setattr(opt, "fetch_target_order", fake_fetch)

    created = await opt.order_proposal_create(
        **_toss_target_create_kwargs(action, market=market)
    )

    assert created["success"] is True
    assert created["action"] == action
    assert created["target_broker_order_id"] == "broker-1"
    assert len(calls) == 1
    assert calls[0]["order_id"] == "broker-1"
    assert calls[0]["symbol"] == symbol
    assert calls[0]["market"] == market
    assert calls[0]["account_mode"] == "toss_live"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace", "cancel"])
async def test_create_toss_live_cancel_replace_auto_dispatch_never_mutates_before_gate(
    monkeypatch, action
):
    """ROB-972 round-1 false-green fix.

    `test_create_toss_live_target_action_preflights_and_creates_proposal`'s
    docstring claims there is "no code path for a mutation to leak into
    proposal creation" because only the read-only `fetch_target_order`
    preflight is imported by `order_proposal_create` -- but that test never
    exercises `order_proposal_create`'s own best-effort `dispatch_proposal`
    call a few lines later, which IS a real code path, and which is exactly
    where the round-1 incident happened (auto-dispatch reaching a broker
    cancel before the eligibility gate ran). Cover that indirect path for
    real: enable Telegram + auto-approve, let `order_proposal_create` drive
    its own real `dispatch_proposal` -> real `revalidate_and_submit` (only
    the broker/network edges are faked, via a `revalidate_fn` partial bound
    onto the real functions -- not a stub that skips the gate logic), and
    assert the broker cancel never fires.
    """
    from app.services.order_proposals.dispatch import (
        dispatch_proposal as real_dispatch_proposal,
    )
    from app.services.order_proposals.revalidation import revalidate_and_submit

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True)
    chat = _unique_chat()
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)

    fake_notifier = _FakeNotifier(message_id=9977)
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.notifier.get_trade_notifier",
        lambda: fake_notifier,
    )

    target_snapshot = _target_snapshot(
        broker_order_id="broker-1",
        symbol="005930",
        side="sell",
        limit_price="43000",
        remaining_quantity="3.5",
    )

    async def fake_fetch(**kwargs):
        return target_snapshot

    monkeypatch.setattr(opt, "fetch_target_order", fake_fetch)

    cancel_calls = []

    async def cancel_fn_must_not_run(**kwargs):
        cancel_calls.append(kwargs)
        raise AssertionError(
            "order_proposal_create's own dispatch_proposal call must never "
            "reach a broker cancel before the eligibility gate runs"
        )

    async def place_fn(**kwargs):
        if kwargs.get("dry_run"):
            return {
                "success": True,
                "approval_hash": "fresh",
                "price": "43000",
                "quantity": "3.5",
            }
        raise AssertionError("live submit must never fire before the gate runs")

    async def no_opposite_pending(**kwargs):
        return None

    # Bind the fakes onto the REAL dispatch_proposal/revalidate_and_submit --
    # not a stand-in that reimplements or skips the gate call -- so this
    # test actually exercises the production gate-application code path.
    monkeypatch.setattr(
        opt,
        "dispatch_proposal",
        functools.partial(
            real_dispatch_proposal,
            revalidate_fn=functools.partial(
                revalidate_and_submit,
                fetch_target_fn=fake_fetch,
                cancel_target_fn=cancel_fn_must_not_run,
                place_order_fn=place_fn,
                opposite_pending_check_fn=no_opposite_pending,
            ),
        ),
    )

    created = await opt.order_proposal_create(
        **_toss_target_create_kwargs(action, market="equity_kr")
    )

    assert created["success"] is True
    assert cancel_calls == []
    assert len(fake_notifier.calls) == 1
    text, _keyboard, _chat_id = fake_notifier.calls[0]
    assert "주문 제안 승인" in text
    assert "자동 접수됨" not in text

    got = await opt.order_proposal_get(created["proposal_id"])
    assert got["rungs"][0]["state"] == "pending_approval"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace", "cancel"])
async def test_create_rejects_toss_live_crypto_target_action_with_supported_matrix(
    monkeypatch, action
):
    async def fetch_must_not_run(**kwargs):
        raise AssertionError("unsupported combo must be rejected before any fetch")

    monkeypatch.setattr(opt, "fetch_target_order", fetch_must_not_run)

    result = await opt.order_proposal_create(
        **_toss_target_create_kwargs(action, market="crypto")
    )

    assert result["success"] is False
    assert "toss_live×equity_kr|equity_us" in result["error"]
    assert result["requested"] == {
        "account_mode": "toss_live",
        "market": "crypto",
        "action": action,
    }
    assert {"account_mode": "toss_live", "market": "equity_kr"} in (
        result["supported_matrix"][action]
    )
    assert {"account_mode": "toss_live", "market": "crypto"} not in (
        result["supported_matrix"][action]
    )


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
    chat = _unique_chat()
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat)
    fake_notifier = _FakeNotifier(message_id=9999)
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.notifier.get_trade_notifier",
        lambda: fake_notifier,
    )

    created = await opt.order_proposal_create(**_create_kwargs())

    assert created["success"] is True
    assert created["approval_dispatch"]["ok"] is True
    assert created["approval_dispatch"]["state"] == "sent"
    assert created["approval_dispatch"]["message_id"] == 9999
    assert len(fake_notifier.calls) == 1
    _, _, chat_id = fake_notifier.calls[0]
    assert chat_id == chat


@pytest.mark.asyncio
async def test_create_4444_thesis_returns_visible_split_dispatch_result(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True)
    chat = _unique_chat()
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat)
    fake_notifier = _FakeNotifier(message_id=10001)
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.notifier.get_trade_notifier",
        lambda: fake_notifier,
    )

    created = await opt.order_proposal_create(
        **_create_kwargs(thesis="가" * 4444, strategy="분할 매도")
    )

    assert created["success"] is True
    assert created["approval_dispatch"]["ok"] is True
    assert created["approval_dispatch"]["state"] == "sent"
    assert created["approval_dispatch"]["payload_chars"] > 4096
    assert len(fake_notifier.calls) == 3
    assert all(keyboard is None for _, keyboard, _ in fake_notifier.calls[:-1])
    assert fake_notifier.calls[-1][1]["inline_keyboard"]


@pytest.mark.asyncio
async def test_supersede_edits_old_approval_message_and_removes_buttons(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True)
    chat = _unique_chat()
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat)
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
            chat,
            9999,
            f"🔁 → {replacement['proposal_id'][:8]}로 대체됨",
            {"inline_keyboard": []},
        )
    ]


@pytest.mark.asyncio
async def test_supersede_message_edit_setup_failure_is_best_effort(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True)
    chat = _unique_chat()
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat)
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
    chat = _unique_chat()
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat)
    fake_notifier = _FakeNotifier()
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.notifier.get_trade_notifier",
        lambda: fake_notifier,
    )

    created = await opt.order_proposal_create(**_create_kwargs())

    assert created["success"] is True
    assert fake_notifier.calls == []
    assert created["approval_dispatch"]["ok"] is False
    assert created["approval_dispatch"]["state"] == "failed"
    assert created["approval_dispatch"]["failure_code"] == "telegram_disabled"
    got = await opt.order_proposal_get(created["proposal_id"])
    assert got["proposal"]["approval_dispatch_state"] == "failed"
    assert got["proposal"]["approval_dispatch_failure_code"] == "telegram_disabled"


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
    assert created["approval_dispatch"]["ok"] is False
    assert created["approval_dispatch"]["state"] == "failed"
    assert created["approval_dispatch"]["failure_code"] == "telegram_allowlist_empty"


@pytest.mark.asyncio
async def test_create_succeeds_even_when_notifier_raises(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True)
    chat = _unique_chat()
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", chat)
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.notifier.get_trade_notifier",
        lambda: _RaisingNotifier(),
    )

    created = await opt.order_proposal_create(**_create_kwargs())

    assert created["success"] is True
    assert "proposal_id" in created
    assert created["approval_dispatch"]["ok"] is False
    assert (
        created["approval_dispatch"]["failure_code"] == "approval_card_dispatch_failed"
    )


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
async def test_fetch_void_evidence_forwards_group_valid_until(monkeypatch):
    valid_until = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)
    now = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    rungs = [SimpleNamespace(rung_index=0)]
    captured = {}
    expected = object()

    async def capture_evidence(**kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(opt, "fetch_operator_void_evidence", capture_evidence)

    result = await opt._fetch_void_evidence(
        group=SimpleNamespace(
            account_mode="toss_live",
            market="equity_kr",
            symbol="005930",
            valid_until=valid_until,
        ),
        rungs=rungs,
        now=now,
    )

    assert result is expected
    assert captured["valid_until"] == valid_until


@pytest.mark.asyncio
async def test_void_unverified_uses_broker_evidence_and_disables_telegram_buttons(
    monkeypatch,
):
    from app.services.order_proposals.broker_gateway import OperatorVoidEvidence

    chat = _unique_chat()
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
            chat_id=chat,
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
            chat,
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
        "order_proposal_expire_sweep",
        "order_proposal_list_expired_defensive",
    }


# -- ROB-897 cause (1): order_proposal_expire_sweep MCP tool ----------------


@pytest.mark.asyncio
async def test_expire_sweep_dry_run_lists_candidates_without_mutating(monkeypatch):
    created = await opt.order_proposal_create(**_create_kwargs(symbol="EXPSWEEP1"))
    proposal_id = uuid.UUID(created["proposal_id"])
    past = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
    async with AsyncSessionLocal() as session:
        service = OrderProposalsService(session)
        group, _ = await service.get_proposal(proposal_id)
        group.valid_until = past
        await session.commit()

    monkeypatch.setattr(opt, "now_kst", lambda: datetime(2026, 7, 15, 9, 0, tzinfo=UTC))

    result = await opt.order_proposal_expire_sweep(dry_run=True)

    assert result["success"] is True
    assert result["dry_run"] is True
    matching = [c for c in result["candidates"] if c["proposal_id"] == str(proposal_id)]
    assert len(matching) == 1
    assert matching[0]["symbol"] == "EXPSWEEP1"
    assert matching[0]["rung_states"] == ["pending_approval"]

    got = await opt.order_proposal_get(str(proposal_id))
    assert got["proposal"]["lifecycle_state"] == "proposed"
    assert got["rungs"][0]["state"] == "pending_approval"


@pytest.mark.asyncio
async def test_expire_sweep_confirm_expires_and_edits_telegram_message(monkeypatch):
    created = await opt.order_proposal_create(**_create_kwargs(symbol="EXPSWEEP2"))
    proposal_id = uuid.UUID(created["proposal_id"])
    chat = _unique_chat()
    past = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
    async with AsyncSessionLocal() as session:
        service = OrderProposalsService(session)
        await service.record_approval_dispatch(
            proposal_id,
            message_id=7777,
            chat_id=chat,
            now=datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
        )
        group, _ = await service.get_proposal(proposal_id)
        group.valid_until = past
        await session.commit()

    notifier = _FakeNotifier()
    monkeypatch.setattr(opt, "_get_trade_notifier", lambda: notifier)
    monkeypatch.setattr(opt, "now_kst", lambda: datetime(2026, 7, 15, 9, 0, tzinfo=UTC))

    result = await opt.order_proposal_expire_sweep(dry_run=False)

    assert result["success"] is True
    assert result["dry_run"] is False
    assert str(proposal_id) in result["swept_proposal_ids"]
    assert result["swept_count"] >= 1

    got = await opt.order_proposal_get(str(proposal_id))
    assert got["proposal"]["lifecycle_state"] == "expired"
    assert got["rungs"][0]["state"] == "expired"
    # Global sweep edits every due proposal's Telegram message; the unique chat
    # (_unique_chat) keeps this tuple collision-free, so assert membership.
    assert (
        chat,
        7777,
        "⏰ 제안 만료됨\n종목: EXPSWEEP2",
        {"inline_keyboard": []},
    ) in notifier.edited


@pytest.mark.asyncio
async def test_expire_sweep_confirm_skips_non_voidable_and_does_not_edit_telegram(
    monkeypatch,
):
    created = await opt.order_proposal_create(**_create_kwargs(symbol="EXPSWEEP3"))
    proposal_id = uuid.UUID(created["proposal_id"])
    chat = _unique_chat()
    async with AsyncSessionLocal() as session:
        service = OrderProposalsService(session)
        await service.record_approval_dispatch(
            proposal_id,
            message_id=8888,
            chat_id=chat,
            now=datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
        )
        for state in ("revalidating", "approved", "submitting"):
            await service.transition_rung(proposal_id, 0, new_state=state)
        group, _ = await service.get_proposal(proposal_id)
        group.valid_until = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
        await session.commit()

    notifier = _FakeNotifier()
    monkeypatch.setattr(opt, "_get_trade_notifier", lambda: notifier)
    monkeypatch.setattr(opt, "now_kst", lambda: datetime(2026, 7, 15, 9, 0, tzinfo=UTC))

    result = await opt.order_proposal_expire_sweep(dry_run=False)

    assert result["success"] is True
    assert str(proposal_id) not in result["swept_proposal_ids"]
    assert result["skipped_count"] >= 1
    # A global sweep legitimately edits other tests' due proposals, so assert a
    # TARGETED negative: this test's seeded chat/message_id was never edited.
    assert all(edited[1] != 8888 for edited in notifier.edited)
    assert all(edited[0] != chat for edited in notifier.edited)
    got = await opt.order_proposal_get(str(proposal_id))
    assert got["rungs"][0]["state"] == "submitting"


def test_expire_sweep_registered_in_tool_names():
    assert "order_proposal_expire_sweep" in opt.ORDER_PROPOSAL_TOOL_NAMES


# -- ROB-929: order_proposal_list_expired_defensive MCP tool ----------------


def _handoff_recent() -> datetime:
    """A timestamp inside the tool's 24h handoff window.

    The MCP tool stamps `now` from the real clock, so a fixed calendar date
    silently drifts out of the window once that day passes."""
    return datetime.now(UTC) - timedelta(minutes=30)


def test_list_expired_defensive_registered_in_tool_names():
    assert "order_proposal_list_expired_defensive" in opt.ORDER_PROPOSAL_TOOL_NAMES


@pytest.mark.asyncio
async def test_list_expired_defensive_returns_expired_loss_cut_proposal(
    monkeypatch,
):
    # exit_intent="defensive_trim" is rejected at create time (ROB-929 code
    # review: no execution-path support yet) -- loss_cut is the only
    # defensive exit_intent actually creatable today.
    recent = _handoff_recent()

    async def fake_lookup(session, retrospective_id):
        return SimpleNamespace(
            symbol="MCPHANDOFF1",
            trigger_type="stop_loss",
            created_at=recent,
        )

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    created = await opt.order_proposal_create(
        **_create_kwargs(
            symbol="MCPHANDOFF1",
            market="equity_us",
            side="sell",
            exit_intent="loss_cut",
            exit_reason="stop_loss",
            retrospective_id=42,
            rungs=[
                {
                    "rung_index": 0,
                    "side": "sell",
                    "quantity": "1",
                    "limit_price": "150",
                    "notional": None,
                }
            ],
        )
    )
    assert created["success"] is True
    proposal_id = uuid.UUID(created["proposal_id"])
    async with AsyncSessionLocal() as session:
        service = OrderProposalsService(session)
        group, _ = await service.get_proposal(proposal_id)
        group.valid_until = recent - timedelta(days=1)
        await session.commit()
        assert await service.expire_if_needed(proposal_id, now=recent)
        group.updated_at = recent
        await session.commit()

    result = await opt.order_proposal_list_expired_defensive(hours=24)

    assert result["success"] is True
    matching = [p for p in result["proposals"] if p["proposal_id"] == str(proposal_id)]
    assert len(matching) == 1
    assert matching[0]["symbol"] == "MCPHANDOFF1"
    assert matching[0]["exit_intent"] == "loss_cut"
    assert matching[0]["lifecycle_state"] == "expired"
    assert matching[0]["needs_reassessment"] is True
    assert Decimal(matching[0]["limit_price"]) == Decimal("150")


@pytest.mark.asyncio
async def test_list_expired_defensive_excludes_non_defensive_proposal():
    recent = _handoff_recent()
    created = await opt.order_proposal_create(**_create_kwargs(symbol="MCPHANDOFF2"))
    proposal_id = uuid.UUID(created["proposal_id"])
    async with AsyncSessionLocal() as session:
        service = OrderProposalsService(session)
        group, _ = await service.get_proposal(proposal_id)
        group.valid_until = recent - timedelta(days=1)
        await session.commit()
        assert await service.expire_if_needed(proposal_id, now=recent)
        group.updated_at = recent
        await session.commit()

    result = await opt.order_proposal_list_expired_defensive(hours=24)

    assert result["success"] is True
    assert str(proposal_id) not in {p["proposal_id"] for p in result["proposals"]}


@pytest.mark.asyncio
async def test_list_expired_defensive_filters_by_market(monkeypatch):
    recent = _handoff_recent()

    async def fake_lookup(session, retrospective_id):
        return SimpleNamespace(
            symbol="MCPHANDOFF3",
            trigger_type="stop_loss",
            created_at=recent,
        )

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    created = await opt.order_proposal_create(
        **_create_kwargs(
            symbol="MCPHANDOFF3",
            market="equity_kr",
            account_mode="kis_live",
            side="sell",
            exit_intent="loss_cut",
            exit_reason="stop_loss",
            retrospective_id=42,
            rungs=[
                {
                    "rung_index": 0,
                    "side": "sell",
                    "quantity": "1",
                    "limit_price": "50000",
                    "notional": None,
                }
            ],
        )
    )
    proposal_id = uuid.UUID(created["proposal_id"])
    async with AsyncSessionLocal() as session:
        service = OrderProposalsService(session)
        group, _ = await service.get_proposal(proposal_id)
        group.valid_until = recent - timedelta(days=1)
        await session.commit()
        assert await service.expire_if_needed(proposal_id, now=recent)
        group.updated_at = recent
        await session.commit()

    result = await opt.order_proposal_list_expired_defensive(
        hours=24, market="equity_us"
    )

    assert result["success"] is True
    assert str(proposal_id) not in {p["proposal_id"] for p in result["proposals"]}
