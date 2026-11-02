"""Plan writes on the buy path and the policy block (ROB-1460)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

from app.core.config import settings
from app.mcp_server.tooling.order_proposal_tools import _validate_exit_plan_argument
from app.schemas.trading_policy import TradingPolicyDocument
from app.services.execution_ledger.kis_lots import LedgerFill
from app.services.mechanical_loss_cut import plans as plans_module
from app.services.mechanical_loss_cut.plans import record_buy_exit_plan
from app.services.mechanical_loss_cut.positions import held_lot_from_fills, lot_symbol
from app.services.mechanical_loss_cut.rules import LotKey
from app.services.order_proposals import revalidation as revalidation_module
from app.services.order_proposals.revalidation import (
    RungOutcome,
    _record_exit_plan_after_accepted_buy,
)
from app.services.order_proposals.service import RungInput
from app.services.trading_policy_service import load_trading_policy
from tests.services.mechanical_loss_cut.support import KEY, plan

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 30, 1, 0, tzinfo=UTC)
_POLICY = Path(__file__).resolve().parents[3] / "config/trading_policy.yaml"


class _Session:
    def __init__(self):
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        self.commits += 1


class _Store:
    latest = None
    appended: list = []

    def __init__(self, _session):
        pass

    async def lock_lot(self, _key):
        return None

    async def latest_plan(self, _key):
        return type(self).latest

    async def append_plan(self, draft):
        type(self).appended.append(draft)
        return draft


@pytest.fixture
def writes_on(monkeypatch):
    monkeypatch.setattr(settings, "LOT_EXIT_PLAN_WRITE_ENABLED", True)
    _Store.latest = None
    _Store.appended = []
    monkeypatch.setattr(plans_module, "MechanicalLossCutStore", _Store)

    async def flat(_session, _key):
        return None

    monkeypatch.setattr(plans_module, "load_position", flat)


def _buy(**overrides):
    values = {
        "account_mode": "kis_live",
        "market": "equity_kr",
        "symbol": "035720",
        "price": Decimal("100000"),
        "quantity": Decimal("10"),
        "now": NOW,
        "session_factory": _Session,
    }
    values.update(overrides)
    return record_buy_exit_plan(**values)


@pytest.mark.asyncio
async def test_writes_stay_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "LOT_EXIT_PLAN_WRITE_ENABLED", False)

    def no_session():
        raise AssertionError("a disabled writer opened a session")

    assert await _buy(session_factory=no_session) == "disabled"


@pytest.mark.asyncio
async def test_buy_writes_a_plan(writes_on):
    status = await _buy(
        stated={"stop_price": "85000", "stop_basis": "support", "stop_reason": "low"},
    )

    assert status == "entry"
    draft = _Store.appended[0]
    assert draft.key == KEY
    assert draft.stop_price == Decimal("85000")
    assert draft.stop_basis == "support"
    assert draft.backfilled is False


@pytest.mark.asyncio
async def test_buy_without_a_stop_records_the_policy_default(writes_on):
    assert await _buy() == "entry"

    assert _Store.appended[0].stop_basis == "pct_floor"
    assert _Store.appended[0].stop_price == Decimal("85000")


@pytest.mark.asyncio
async def test_add_updates_the_plan_with_a_new_version(writes_on, monkeypatch):
    _Store.latest = plan()

    async def position(_session, _key):
        return SimpleNamespace(quantity=Decimal("10"), average_cost=Decimal("100000"))

    monkeypatch.setattr(plans_module, "load_position", position)

    status = await _buy(price=Decimal("90000"), quantity=Decimal("10"))

    assert status == "add"
    draft = _Store.appended[0]
    assert draft.event == "add"
    assert draft.average_cost == Decimal("95000")
    assert draft.quantity == Decimal("20")
    assert draft.adds_used == 1


@pytest.mark.asyncio
async def test_buy_right_after_an_unreconciled_entry_is_an_add(writes_on):
    _Store.latest = _with_created_at(plan(), NOW - timedelta(hours=2))

    assert await _buy(price=Decimal("95000")) == "add"


@pytest.mark.asyncio
async def test_buy_after_the_position_went_flat_starts_a_new_entry(writes_on):
    _Store.latest = _with_created_at(plan(adds_used=1), NOW - timedelta(days=30))

    assert await _buy() == "entry"
    assert _Store.appended[0].adds_used == 0


def _with_created_at(value, created_at):
    from dataclasses import replace

    return replace(value, created_at=created_at)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"account_mode": "kis_mock"}, "not_plannable"),
        ({"account_mode": "upbit", "market": "equity_kr"}, "not_plannable"),
        ({"symbol": "459580"}, "parking_instrument"),
        ({"price": None}, "price_or_quantity_unavailable"),
        ({"quantity": 0}, "price_or_quantity_unavailable"),
    ],
)
async def test_buys_that_get_no_plan(writes_on, kwargs, expected):
    assert await _buy(**kwargs) == expected
    assert _Store.appended == []


@pytest.mark.asyncio
async def test_a_failed_write_never_reaches_the_caller(writes_on):
    def broken_factory():
        raise RuntimeError("relation does not exist")

    assert await _buy(session_factory=broken_factory) == "write_failed"


@pytest.mark.asyncio
async def test_an_altered_stored_plan_falls_back_to_the_default(writes_on):
    assert await _buy(stated={"stop_price": "bad", "stop_basis": "support"}) == "entry"

    assert _Store.appended[0].stop_basis == "pct_floor"


def _group(**overrides):
    values = {
        "account_mode": "kis_live",
        "market": "equity_kr",
        "symbol": "035720",
        "proposal_id": uuid.uuid4(),
        "source_asof": {"exit_plan": {"stop_price": "85000", "stop_basis": "support"}},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _rung(side="buy"):
    return SimpleNamespace(
        side=side,
        rung_index=0,
        limit_price=Decimal("100000"),
        quantity=Decimal("10"),
    )


@pytest.mark.asyncio
async def test_accepted_proposal_buy_carries_its_stated_stop(monkeypatch):
    calls: list[dict] = []

    async def record(**kwargs):
        calls.append(kwargs)
        return "entry"

    monkeypatch.setattr(revalidation_module, "record_buy_exit_plan", record)
    group = _group()

    await _record_exit_plan_after_accepted_buy(
        group, _rung(), RungOutcome(0, "submitted_resting", {}), now=NOW
    )

    assert calls[0]["stated"] == {"stop_price": "85000", "stop_basis": "support"}
    assert calls[0]["proposal_id"] == group.proposal_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side", "result"),
    [
        ("sell", "submitted_resting"),
        ("buy", "unverified"),
        ("buy", "guard_blocked"),
        ("buy", "error"),
    ],
)
async def test_only_an_accepted_buy_gets_a_plan(monkeypatch, side, result):
    async def record(**_kwargs):
        raise AssertionError("a plan for something that is not an accepted buy")

    monkeypatch.setattr(revalidation_module, "record_buy_exit_plan", record)

    await _record_exit_plan_after_accepted_buy(
        _group(), _rung(side), RungOutcome(0, result, {}), now=NOW
    )


def _rungs(*prices):
    return [
        RungInput(index, "buy", Decimal("1"), Decimal(price), None)
        for index, price in enumerate(prices)
    ]


def test_proposal_argument_accepts_a_stop_below_every_rung():
    _validate_exit_plan_argument(
        {"stop_price": "85000", "stop_basis": "support"},
        side="buy",
        action="place",
        rungs=_rungs("100000", "95000"),
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"side": "sell", "action": "place"}, "only accepted on a place buy"),
        ({"side": "buy", "action": "replace"}, "only accepted on a place buy"),
    ],
)
def test_proposal_argument_only_fits_a_place_buy(kwargs, message):
    with pytest.raises(ValueError, match=message):
        _validate_exit_plan_argument(
            {"stop_price": "85000", "stop_basis": "support"},
            rungs=_rungs("100000"),
            **kwargs,
        )


def test_proposal_argument_refuses_a_stop_at_or_above_a_rung():
    with pytest.raises(ValueError, match="exit_plan_stop_not_below_rung"):
        _validate_exit_plan_argument(
            {"stop_price": "95000", "stop_basis": "support"},
            side="buy",
            action="place",
            rungs=_rungs("100000", "95000"),
        )


def test_proposal_argument_refuses_a_malformed_plan():
    with pytest.raises(ValueError, match="exit_plan_stop_basis_invalid"):
        _validate_exit_plan_argument(
            {"stop_price": "85000", "stop_basis": "vibes"},
            side="buy",
            action="place",
            rungs=_rungs("100000"),
        )


def _fill(fill_id, side, quantity, price, *, source="reconciler", order="o", day=1):
    return LedgerFill(
        id=fill_id,
        source=source,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        filled_at=datetime(2026, 10, day, tzinfo=UTC),
        broker_order_id=f"{order}-{fill_id}",
    )


def test_position_is_fifo_from_ledger_fills():
    lot = held_lot_from_fills(
        KEY,
        [
            _fill(1, "buy", "10", "100"),
            _fill(2, "buy", "10", "80", day=2),
            _fill(3, "sell", "10", "90", day=3),
        ],
    )

    assert lot is not None
    assert lot.quantity == Decimal("10")
    assert lot.average_cost == Decimal("80")


def test_later_opening_seed_is_the_whole_position():
    lot = held_lot_from_fills(
        KEY,
        [
            _fill(1, "buy", "10", "100", source="manual_import", order="SEED-1"),
            _fill(2, "buy", "5", "90", source="manual_import", order="SEED-2", day=5),
        ],
    )

    assert lot is not None
    assert lot.quantity == Decimal("5")


def test_flat_or_oversold_ledger_is_no_position():
    assert (
        held_lot_from_fills(
            KEY, [_fill(1, "buy", "1", "100"), _fill(2, "sell", "1", "90", day=2)]
        )
        is None
    )
    assert (
        held_lot_from_fills(
            KEY, [_fill(1, "buy", "1", "100"), _fill(2, "sell", "2", "90", day=2)]
        )
        is None
    )


@pytest.mark.parametrize(
    ("symbol", "market", "expected"),
    [
        ("BTC", "crypto", "KRW-BTC"),
        ("KRW-BTC", "crypto", "KRW-BTC"),
        ("brk.b", "equity_us", "BRK.B"),
        ("035720", "equity_kr", "035720"),
    ],
)
def test_lot_symbol_matches_proposals_and_candles(symbol, market, expected):
    assert lot_symbol(symbol, market) == expected


def _raw_policy():
    return yaml.safe_load(_POLICY.read_text(encoding="utf-8"))


def test_policy_declares_every_market_and_confirms_none():
    block = load_trading_policy().mechanical_loss_cut

    assert set(block.markets) == {"kr", "us", "crypto"}
    assert all(not market.thresholds_confirmed for market in block.markets.values())
    assert all(not market.live_include_backfilled for market in block.markets.values())


def test_policy_refuses_a_slippage_wider_than_the_loss_cut_band():
    raw = _raw_policy()
    raw["mechanical_loss_cut"]["markets"]["kr"]["order"]["max_slippage_pct"] = 3

    with pytest.raises(ValidationError, match="sell.loss_cut_max_slip"):
        TradingPolicyDocument.model_validate(raw)


def test_policy_refuses_an_unknown_family_and_a_typo():
    raw = _raw_policy()
    raw["mechanical_loss_cut"]["markets"]["kr"]["trigger"]["family"] = "vibes"
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)

    raw = _raw_policy()
    raw["mechanical_loss_cut"]["markets"]["us"]["bogus"] = 1
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_policy_must_declare_all_three_markets():
    raw = _raw_policy()
    del raw["mechanical_loss_cut"]["markets"]["crypto"]

    with pytest.raises(ValidationError, match="must declare exactly"):
        TradingPolicyDocument.model_validate(raw)


def test_policy_holds_parameters_but_no_switch():
    fields = set(
        TradingPolicyDocument.model_fields[
            "mechanical_loss_cut"
        ].annotation.model_fields
    )

    assert fields == {"parameter_source", "markets"}


def test_lot_key_is_account_market_symbol():
    assert LotKey("upbit", "crypto", "KRW-BTC")._fields == (
        "account_mode",
        "market",
        "symbol",
    )
