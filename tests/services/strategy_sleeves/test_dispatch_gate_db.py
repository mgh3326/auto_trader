"""ROB-1470 dispatch gate on the test database (Acceptance 4).

Each refused case proves three things: revalidation (the only path to a broker)
is never entered, the proposal goes to the human approval card, and the card's
durable audit row names the sleeve reason. The untagged case proves the gate is
never consulted.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from app.core.db import engine
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals import dispatch as dispatch_module
from app.services.order_proposals import revalidation as revalidation_module
from app.services.order_proposals.dispatch import dispatch_proposal
from app.services.order_proposals.revalidation import RungOutcome
from app.services.strategy_sleeves import dispatch_gate, store
from app.services.strategy_sleeves.vocabulary import SYSTEM_LOSS_GATE_ACTOR
from app.telegram_contract import TelegramMethodResult, telegram_text_length
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.order_proposals.window_fakes import allow_known_session
from tests.services.strategy_sleeves.db_support import (
    SleeveSeed,
    unique_order_id,
    unique_symbol,
)

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_approval_message(
        self, text, inline_keyboard, *, chat_id, parse_mode="Markdown", **_kw
    ):
        self.sent.append(text)
        return TelegramMethodResult(
            ok=True,
            message_id=7000 + len(self.sent),
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=telegram_text_length(text),
        )


def _factory(db_session):
    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    return _session


@pytest.fixture(autouse=True)
def _auto_lane_on(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(
        dispatch_module, "evaluate_approval_window", allow_known_session
    )
    monkeypatch.setattr(
        revalidation_module, "evaluate_approval_window", allow_known_session
    )
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        f"sleeve-{uuid.uuid4().hex}",
    )


@pytest_asyncio.fixture
async def seed(db_session):
    helper = SleeveSeed(db_session)
    try:
        yield helper
    finally:
        await helper.cleanup()


async def _must_not_revalidate(**_kwargs):
    raise AssertionError("a refused sleeve must never reach revalidation")


async def _dispatch(db_session, group, revalidate_fn=_must_not_revalidate):
    notifier = _Notifier()
    await dispatch_proposal(
        group.proposal_id,
        notifier=notifier,
        now=datetime.now(UTC),
        service_factory=_factory(db_session),
        revalidate_fn=revalidate_fn,
    )
    refreshed, rungs = await OrderProposalsService(db_session).get_proposal(
        group.proposal_id
    )
    return notifier, refreshed, rungs


def _reason(refreshed) -> dict:
    [rung] = refreshed.source_asof["auto_approve_rejections"][-1]["rungs"]
    return rung


async def test_not_live_sleeve_sends_the_order_to_the_card(db_session, seed) -> None:
    sleeve = await seed.sleeve(state="shadow")
    group = await seed.proposal(sleeve_id=sleeve.id, symbol=unique_symbol())

    notifier, refreshed, rungs = await _dispatch(db_session, group)

    assert rungs[0].state == "pending_approval"
    assert "auto_approved" not in (refreshed.source_asof or {})
    assert notifier.sent and "sleeve_not_live" in notifier.sent[-1]
    evidence = _reason(refreshed)
    assert evidence["reason_code"] == "sleeve_not_live"
    assert evidence["inputs"]["sleeve_state"] == "shadow"


async def test_over_budget_sends_the_order_to_the_card(db_session, seed) -> None:
    sleeve = await seed.sleeve(budget_cap="999")
    # 10 @ 100 = 1000 > 999.
    group = await seed.proposal(sleeve_id=sleeve.id, symbol=unique_symbol())

    notifier, refreshed, rungs = await _dispatch(db_session, group)

    assert rungs[0].state == "pending_approval"
    evidence = _reason(refreshed)
    assert evidence["reason_code"] == "sleeve_budget_exceeded"
    assert evidence["inputs"]["sleeve_used_after"] == "1000"
    assert evidence["inputs"]["sleeve_budget_cap"] == "999"
    assert "sleeve_budget_exceeded" in notifier.sent[-1]


async def test_loss_limit_reached_sends_to_the_card_and_stops_the_sleeve(
    db_session, seed, monkeypatch
) -> None:
    sleeve = await seed.sleeve(budget_cap="100000", loss_limit="100")
    symbol = unique_symbol()
    held = await seed.proposal(sleeve_id=sleeve.id, symbol=symbol)
    order_id = unique_order_id()
    await seed.mark_acked(held, order_id)
    await seed.fill(order_id=order_id, symbol=symbol, quantity="10", price="100")

    async def marks(_session, _market, symbols, _now):
        return dict.fromkeys(symbols, Decimal("90"))  # 10 x -10 = -100

    monkeypatch.setattr(dispatch_gate, "latest_close_marks", marks)
    group = await seed.proposal(
        sleeve_id=sleeve.id, symbol=symbol, quantity="1", limit_price="1"
    )

    notifier, refreshed, rungs = await _dispatch(db_session, group)

    assert rungs[0].state == "pending_approval"
    evidence = _reason(refreshed)
    assert evidence["reason_code"] == "sleeve_loss_limit_reached"
    assert evidence["inputs"]["sleeve_net_pnl"] == "-100"
    current = await store.current_version(db_session, sleeve.id)
    assert current is not None
    assert (current.version, current.state, current.set_by) == (
        2,
        "stopped",
        SYSTEM_LOSS_GATE_ACTOR,
    )
    assert "sleeve_loss_limit_reached" in notifier.sent[-1]


async def test_a_live_sleeve_inside_its_limits_reaches_revalidation(
    db_session, seed
) -> None:
    sleeve = await seed.sleeve(budget_cap="1000")
    group = await seed.proposal(sleeve_id=sleeve.id, symbol=unique_symbol())
    calls: list[uuid.UUID] = []

    async def revalidate(**kwargs):
        calls.append(kwargs["proposal_id"])
        return [RungOutcome(0, "approval_required", {"reason": "test_stop_here"})]

    await _dispatch(db_session, group, revalidate_fn=revalidate)

    assert calls == [group.proposal_id]


async def test_an_untagged_proposal_never_consults_the_sleeve_gate(
    db_session, seed, monkeypatch
) -> None:
    async def gate_must_not_run(*_args, **_kwargs):
        raise AssertionError("untagged proposals bypass the sleeve gate entirely")

    monkeypatch.setattr(
        dispatch_module, "evaluate_sleeve_for_dispatch", gate_must_not_run
    )
    group = await seed.proposal(sleeve_id=None, symbol=unique_symbol())
    calls: list[uuid.UUID] = []

    async def revalidate(**kwargs):
        calls.append(kwargs["proposal_id"])
        return [RungOutcome(0, "approval_required", {"reason": "test_stop_here"})]

    await _dispatch(db_session, group, revalidate_fn=revalidate)

    assert calls == [group.proposal_id]
