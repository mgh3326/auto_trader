"""The live gate in the auto-approve classifier (ROB-1460)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.mcp_server.tooling.order_validation import evaluate_sell_price_guards
from app.services.mechanical_loss_cut.gate import (
    MECHANICAL_STRATEGY,
    MechanicalLossCutBinding,
    live_switch_refusal,
    regular_session_open,
)
from app.services.mechanical_loss_cut.job import WrittenDecision
from app.services.mechanical_loss_cut.live import emit_live_decisions
from app.services.mechanical_loss_cut.rules import CutDecision
from app.services.order_proposals import dispatch as dispatch_module
from app.services.order_proposals.auto_approve import (
    AutoApproveLimits,
    evaluate_auto_approve_eligibility,
)
from app.services.order_proposals.auto_approve_audit import (
    append_auto_approve_rejection_attempt,
    project_auto_approve_rejections,
)
from tests.services.mechanical_loss_cut.support import (
    KEY,
    SESSION,
    bars_from_closes,
    held,
    params,
    plan,
)

pytestmark = pytest.mark.unit

# 10:00 KST on a regular Friday session.
NOW = datetime(2026, 10, 30, 1, 0, tzinfo=UTC)
LIMITS = AutoApproveLimits(
    min_distance_pct=Decimal("3"),
    per_order_cap=Decimal("2000000"),
    daily_cap=Decimal("5000000"),
    policy_version="2026-11-02.1",
    mode="expanded",
)


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_ENABLED", True)
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_MARKETS", "kr,us,crypto")
    monkeypatch.setattr(
        settings, "MECHANICAL_LOSS_CUT_LIVE_ACCOUNT_MODES", "kis_live,toss_live,upbit"
    )


def _confirmed(**changes):
    return params().model_copy(update={"thresholds_confirmed": True, **changes})


def _binding(**overrides) -> MechanicalLossCutBinding:
    values = {
        "decision_id": 41,
        "account_mode": "kis_live",
        "market": "equity_kr",
        "symbol": "035720",
        "quantity": Decimal("10"),
        "limit_price": Decimal("83200"),
        "stop_price": Decimal("85000"),
        "backfilled": False,
        "params": _confirmed(),
    }
    values.update(overrides)
    return MechanicalLossCutBinding(**values)


def _group(**overrides):
    values = {
        "proposal_id": uuid.uuid4(),
        "market": "equity_kr",
        "account_mode": "kis_live",
        "broker_account_id": "acct-1",
        "symbol": "035720",
        "order_type": "limit",
        "action": "place",
        "exit_intent": "loss_cut",
        "exit_reason": "stop_loss",
        "strategy": MECHANICAL_STRATEGY,
        "thesis": "Mechanical loss-cut: close below the recorded stop.",
        "source_asof": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _rung(**overrides):
    values = {
        "rung_index": 0,
        "side": "sell",
        "limit_price": Decimal("83200"),
        "quantity": Decimal("10"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


_PREVIEW = {
    "success": True,
    "current_price": "84000",
    "avg_buy_price": "100000",
}


def _evaluate(**overrides):
    values = {
        "group": _group(),
        "rung": _rung(),
        "preview": dict(_PREVIEW),
        "limits": LIMITS,
        "daily_notional": Decimal("0"),
        "now": NOW,
        "mechanical_binding": _binding(),
    }
    values.update(overrides)
    return evaluate_auto_approve_eligibility(**values)


def test_inside_every_gate_the_mechanical_cut_is_eligible(switch_on):
    decision = _evaluate()

    assert decision.eligible is True
    assert decision.details["loss_guard"] == "mechanical_loss_cut"
    assert decision.details["mechanical_decision_id"] == "41"
    # A marketable sell is metered at the price it can execute at.
    assert decision.details["notional"] == "840000"
    assert decision.details["daily_notional_after"] == "840000"


def test_switch_off_goes_to_the_card_even_with_a_binding():
    decision = _evaluate()

    assert decision.eligible is False
    assert decision.reason == "mechanical_loss_cut_refused"
    assert decision.details["mechanical_refusal"] == "switch_off"


@pytest.mark.parametrize(
    ("enabled", "markets", "accounts", "market", "account", "expected"),
    [
        (False, "kr", "kis_live", "equity_kr", "kis_live", "switch_off"),
        (True, "", "kis_live", "equity_kr", "kis_live", "market_not_allowed"),
        (True, "us", "kis_live", "equity_kr", "kis_live", "market_not_allowed"),
        (True, "kr", "", "equity_kr", "kis_live", "account_not_allowed"),
        (True, "kr", "toss_live", "equity_kr", "kis_live", "account_not_allowed"),
        (True, "kr", "kis_live", "forex", "kis_live", "market_not_allowed"),
        (True, "kr", "kis_live", "equity_kr", "kis_live", None),
    ],
)
def test_live_needs_the_switch_and_the_market_and_the_account(
    monkeypatch, enabled, markets, accounts, market, account, expected
):
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_ENABLED", enabled)
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_MARKETS", markets)
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_ACCOUNT_MODES", accounts)

    assert live_switch_refusal(market=market, account_mode=account) == expected


def test_a_truthy_switch_value_is_not_enough(monkeypatch):
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_ENABLED", "true")
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_MARKETS", "kr")
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_ACCOUNT_MODES", "kis_live")

    assert (
        live_switch_refusal(market="equity_kr", account_mode="kis_live") == "switch_off"
    )


def test_per_order_cap_bounds_the_sell_and_sends_it_to_the_card(switch_on):
    decision = _evaluate(
        rung=_rung(quantity=Decimal("30")),
        mechanical_binding=_binding(quantity=Decimal("30")),
    )

    assert decision.eligible is False
    assert decision.reason == "per_order_cap_exceeded"


def test_per_order_cap_meters_the_executable_price_not_the_lower_limit(switch_on):
    """Limit x qty fits the cap. Current price x qty does not."""
    limits = AutoApproveLimits(
        min_distance_pct=Decimal("3"),
        per_order_cap=Decimal("835000"),
        daily_cap=Decimal("5000000"),
        policy_version="2026-11-02.1",
        mode="expanded",
    )

    decision = _evaluate(limits=limits)

    assert decision.eligible is False
    assert decision.reason == "per_order_cap_exceeded"
    assert decision.details["notional"] == "840000"


def test_daily_cap_bounds_the_sell_and_sends_it_to_the_card(switch_on):
    decision = _evaluate(daily_notional=Decimal("4500000"))

    assert decision.eligible is False
    assert decision.reason == "daily_cap_exceeded"


def test_no_binding_is_the_ordinary_human_card(switch_on):
    decision = _evaluate(mechanical_binding=None)

    assert decision.eligible is False
    assert decision.reason == "loss_cut_intent"


def test_a_strategy_label_alone_grants_nothing(switch_on):
    decision = evaluate_auto_approve_eligibility(
        group=_group(strategy=MECHANICAL_STRATEGY),
        rung=_rung(),
        preview=dict(_PREVIEW),
        limits=LIMITS,
        daily_notional=Decimal("0"),
        now=NOW,
    )

    assert decision.reason == "loss_cut_intent"


def test_ordinary_loss_sell_without_loss_cut_intent_is_still_refused(switch_on):
    """No exit intent: the fee-netted profit proof sends it to the card."""
    decision = _evaluate(
        group=_group(exit_intent=None, exit_reason=None),
        mechanical_binding=None,
    )

    assert decision.eligible is False
    assert decision.reason == "expected_pnl_not_positive"


def test_the_average_cost_guard_still_blocks_every_non_mechanical_sell():
    error = evaluate_sell_price_guards(
        price=83200.0,
        current_price=84000.0,
        avg_price=100000.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
    )

    assert error is not None
    assert "below minimum" in error


@pytest.mark.parametrize(
    ("overrides", "refusal"),
    [
        ({"params": params()}, "thresholds_unconfirmed"),
        ({"backfilled": True}, "backfilled_plan"),
        ({"symbol": "000660"}, "binding_mismatch"),
        ({"quantity": Decimal("9")}, "binding_mismatch"),
        ({"limit_price": Decimal("83100")}, "binding_mismatch"),
    ],
)
def test_binding_refusals_reach_the_card(switch_on, overrides, refusal):
    decision = _evaluate(mechanical_binding=_binding(**overrides))

    assert decision.eligible is False
    assert decision.details["mechanical_refusal"] == refusal


def test_backfilled_plan_can_be_admitted_only_by_policy(switch_on):
    decision = _evaluate(
        mechanical_binding=_binding(
            backfilled=True, params=_confirmed(live_include_backfilled=True)
        )
    )

    assert decision.eligible is True


def test_off_mode_never_admits_a_mechanical_cut(switch_on):
    limits = AutoApproveLimits(
        min_distance_pct=Decimal("3"),
        per_order_cap=Decimal("2000000"),
        daily_cap=Decimal("5000000"),
        policy_version="2026-11-02.1",
    )

    decision = _evaluate(limits=limits)

    assert decision.details["mechanical_refusal"] == "mode_not_expanded"


def test_parking_instrument_is_refused_even_with_a_row(switch_on):
    decision = _evaluate(
        group=_group(symbol="459580"),
        mechanical_binding=_binding(symbol="459580"),
    )

    assert decision.details["mechanical_refusal"] == "parking_instrument"


def test_closed_market_goes_to_the_card(switch_on):
    saturday = datetime(2026, 10, 31, 1, 0, tzinfo=UTC)

    decision = _evaluate(now=saturday)

    assert decision.details["mechanical_refusal"] == "session_closed"


def test_session_rule_by_market():
    assert regular_session_open("equity_kr", NOW) is True
    assert (
        regular_session_open("equity_kr", datetime(2026, 10, 30, 8, 0, tzinfo=UTC))
        is False
    )
    assert (
        regular_session_open("equity_us", datetime(2026, 10, 30, 15, 0, tzinfo=UTC))
        is True
    )
    assert regular_session_open("equity_us", NOW) is False
    assert regular_session_open("crypto", None) is True
    assert regular_session_open("equity_kr", None) is False


@pytest.mark.parametrize(
    ("preview", "rung_limit", "refusal"),
    [
        ({"current_price": "85000"}, "83200", "price_recovered"),
        ({"current_price": "90000"}, "83200", "price_recovered"),
        ({"avg_buy_price": "80000"}, "83200", "not_underwater"),
        ({"avg_buy_price": None}, "83200", "average_cost_unavailable"),
        ({"current_price": "84500"}, "83200", "limit_outside_band"),
    ],
)
def test_the_trigger_is_checked_again_on_the_fresh_preview(
    switch_on, preview, rung_limit, refusal
):
    decision = _evaluate(
        preview={**_PREVIEW, **preview},
        rung=_rung(limit_price=Decimal(rung_limit)),
    )

    assert decision.eligible is False
    assert decision.details["mechanical_refusal"] == refusal


@pytest.mark.parametrize(
    "overrides",
    [
        {"order_type": "market"},
        {"account_mode": "kis_mock"},
    ],
)
def test_limit_only_and_live_accounts_only(switch_on, overrides):
    decision = _evaluate(group=_group(**overrides))

    assert decision.eligible is False


def test_failed_preview_goes_to_the_card(switch_on):
    decision = _evaluate(preview={**_PREVIEW, "success": False})

    assert decision.eligible is False
    assert decision.reason == "preview_guard_failed"


def test_policy_deviation_tag_still_vetoes(switch_on):
    decision = _evaluate(group=_group(thesis="policy_deviation noted"))

    assert decision.reason == "approval_required_tag"


def test_missing_thesis_goes_to_the_card(switch_on):
    decision = _evaluate(group=_group(thesis=None))

    assert decision.reason == "thesis_required_for_veto_card"


def test_refusal_survives_the_audit_projection(switch_on):
    decision = _evaluate(mechanical_binding=_binding(backfilled=True))
    source_asof = append_auto_approve_rejection_attempt(
        {},
        decisions=[
            {
                "rung_index": 0,
                "eligible": False,
                "reason": decision.reason,
                **decision.details,
            }
        ],
        now=NOW,
    )

    projected = project_auto_approve_rejections(source_asof)

    rendered = str(projected)
    assert "mechanical_loss_cut_refused" in rendered
    assert "invalid_reason_code" not in rendered
    assert "backfilled_plan" in rendered


class _Scalar:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _BindingSession:
    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error
        self.queries = 0

    async def execute(self, _statement):
        self.queries += 1
        if self.error is not None:
            raise self.error
        return _Scalar(self.row)


@pytest.mark.asyncio
async def test_binding_is_not_even_read_while_the_switch_is_off():
    session = _BindingSession()

    binding = await dispatch_module._load_mechanical_binding(session, _group())

    assert binding is None
    assert session.queries == 0


@pytest.mark.asyncio
async def test_forged_label_without_a_decision_row_has_no_binding(switch_on):
    session = _BindingSession(row=None)

    binding = await dispatch_module._load_mechanical_binding(session, _group())

    assert binding is None
    assert session.queries == 1


@pytest.mark.asyncio
async def test_unreadable_binding_means_the_card(switch_on):
    session = _BindingSession(error=RuntimeError("db down"))

    assert await dispatch_module._load_mechanical_binding(session, _group()) is None


@pytest.mark.asyncio
async def test_binding_comes_from_the_durable_row(switch_on):
    row = SimpleNamespace(
        id=41,
        account_mode="kis_live",
        market="equity_kr",
        symbol="035720",
        intended_quantity=Decimal("10"),
        intended_limit_price=Decimal("83200"),
        stop_price=Decimal("85000"),
        backfilled=False,
    )

    binding = await dispatch_module._load_mechanical_binding(
        _BindingSession(row=row), _group()
    )

    assert binding is not None
    assert binding.decision_id == 41
    assert binding.params.thresholds_confirmed is False


def _written() -> WrittenDecision:
    from app.services.mechanical_loss_cut.rules import evaluate_lot

    decision = evaluate_lot(
        lot=held(),
        plan=plan(),
        bars=bars_from_closes([95000, 93000, 91000, 89000, 87000, 86000, 84000]),
        params=_confirmed(),
        price_fresh=True,
        open_sell_quantity=Decimal("0"),
        protected_quantity=Decimal("0"),
        last_decision_session=None,
    )
    assert isinstance(decision, CutDecision)
    assert decision.session == SESSION
    assert decision.key == KEY
    return WrittenDecision(decision, 1)


class _Notifier:
    async def send_approval_message(self, *_args, **_kwargs):
        raise AssertionError("unexpected card")


async def _noop_round(*_args, **_kwargs):
    return None


@pytest.fixture
def live_seams(monkeypatch):
    """Confirmed thresholds, no database, an empty digest round."""
    from contextlib import asynccontextmanager

    from app.services.mechanical_loss_cut import live as live_module
    from app.services.trading_policy_service import load_trading_policy

    policy = load_trading_policy().model_copy(deep=True)
    policy.mechanical_loss_cut.markets["kr"].thresholds_confirmed = True
    monkeypatch.setattr(live_module, "load_trading_policy", lambda: policy)

    @asynccontextmanager
    async def empty_round(**_kwargs):
        yield None

    monkeypatch.setattr(live_module, "open_auto_digest_round", empty_round)
    created: list[CutDecision] = []

    async def create(decision, **_kwargs):
        created.append(decision)
        return uuid.uuid4()

    monkeypatch.setattr(live_module, "_create_proposal", create)
    return created


@pytest.mark.asyncio
async def test_emitter_creates_and_dispatches_one_proposal(switch_on, live_seams):
    dispatched: list[uuid.UUID] = []

    async def dispatch(proposal_id, **_kwargs):
        dispatched.append(proposal_id)

    async def fresh(_symbol, _market):
        return 84000.0

    result = await emit_live_decisions(
        [_written()],
        NOW,
        session_factory=object,
        fresh_price_fn=fresh,
        dispatch_fn=dispatch,
        notifier=_Notifier(),
    )

    assert result["outcomes"] == {"proposal_dispatched": 1}
    assert len(dispatched) == 1
    assert live_seams[0].intended_limit_price == Decimal("83200")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("price", "outcome"),
    [
        (None, "fresh_price_unavailable"),
        (0, "fresh_price_unavailable"),
        (85000.0, "price_recovered"),
    ],
)
async def test_emitter_sends_nothing_on_a_bad_or_recovered_price(
    switch_on, live_seams, price, outcome
):
    async def dispatch(*_args, **_kwargs):
        raise AssertionError("dispatched without a fresh falling price")

    async def fresh(_symbol, _market):
        return price

    result = await emit_live_decisions(
        [_written()],
        NOW,
        session_factory=object,
        fresh_price_fn=fresh,
        dispatch_fn=dispatch,
        notifier=_Notifier(),
    )

    assert result["outcomes"] == {outcome: 1}
    assert live_seams == []


@pytest.mark.asyncio
async def test_emitter_refuses_with_the_switch_off(monkeypatch, live_seams):
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_ENABLED", False)

    async def dispatch(*_args, **_kwargs):
        raise AssertionError("dispatched with the switch off")

    async def fresh(_symbol, _market):
        raise AssertionError("read a price with the switch off")

    result = await emit_live_decisions(
        [_written()],
        NOW,
        session_factory=object,
        fresh_price_fn=fresh,
        dispatch_fn=dispatch,
        notifier=_Notifier(),
    )

    assert result["outcomes"] == {"switch_off": 1}


@pytest.mark.asyncio
async def test_emitter_refuses_unconfirmed_thresholds(switch_on, monkeypatch):
    from contextlib import asynccontextmanager

    from app.services.mechanical_loss_cut import live as live_module

    @asynccontextmanager
    async def empty_round(**_kwargs):
        yield None

    monkeypatch.setattr(live_module, "open_auto_digest_round", empty_round)

    async def fresh(_symbol, _market):
        raise AssertionError("read a price with unconfirmed thresholds")

    result = await emit_live_decisions(
        [_written()],
        NOW,
        session_factory=object,
        fresh_price_fn=fresh,
        dispatch_fn=_noop_round,
        notifier=_Notifier(),
    )

    assert result["outcomes"] == {"thresholds_unconfirmed": 1}
