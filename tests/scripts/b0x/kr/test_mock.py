"""``scripts.b0x.kr.mock`` — tick alignment, sizing, fresh-truth reads.

Every test here is offline: ``_FakeKrClient`` stands in for
``ReadOnlyKISMockDomesticClient`` (same two async methods,
``fetch_my_stocks``/``inquire_cash_balance``), so nothing in this file makes a
network call, and nothing in it can reach kis_mock or KIS live.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from scripts.b0x.broker_truth import BrokerTruth, OwnPendingResubmitBlocked
from scripts.b0x.derivation import DerivedOrder
from scripts.b0x.envelope import KR_MOCK_ENVELOPE
from scripts.b0x.kr import mock as kr_mock

pytestmark = pytest.mark.unit

ENVELOPE = KR_MOCK_ENVELOPE


def _buy(
    symbol: str = "005930",
    *,
    table_price: str,
    notional: str | None = None,
    order_key: str = "abc123",
) -> DerivedOrder:
    return DerivedOrder(
        sequence=0,
        symbol=symbol,
        side="buy",
        leg="buy_l1",
        price_ratio=Decimal("0.97"),
        table_price=Decimal(table_price),
        table_previous_close=Decimal(table_price) / Decimal("0.97"),
        notional=None if notional is None else Decimal(notional),
        quantity_fraction=None,
        basis="A_buy_side.buy_l1.price",
        labels=(),
        detail={},
        order_key=order_key,
    )


def _sell(
    symbol: str = "005930",
    *,
    table_price: str,
    fraction: str = "0.5",
    order_key: str = "def456",
) -> DerivedOrder:
    return DerivedOrder(
        sequence=1,
        symbol=symbol,
        side="sell",
        leg="sell_r1",
        price_ratio=Decimal("1.05"),
        table_price=Decimal(table_price),
        table_previous_close=Decimal(table_price) / Decimal("1.05"),
        notional=None,
        quantity_fraction=Decimal(fraction),
        basis="B_sell_side.sell_r1",
        labels=("SELL_SIDE_MODEL_MISMATCH",),
        detail={},
        order_key=order_key,
    )


# ---------------------------------------------------------------------------
# Tick alignment — matches app.mcp_server.tick_size's documented examples.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "price,side,expected",
    [
        ("327272", "buy", 327000),
        ("327272", "sell", 327500),
        ("1098000", "buy", 1098000),
        ("15723", "buy", 15720),
        ("1", "buy", 1),
    ],
)
def test_align_price_kr_matches_krx_tick_table(
    price: str, side: str, expected: int
) -> None:
    assert kr_mock.align_price_kr(Decimal(price), side=side) == expected


def test_align_price_kr_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        kr_mock.align_price_kr(Decimal("0"), side="buy")


# ---------------------------------------------------------------------------
# plan_orders — buy sizing, sell sizing, whole-share flooring.
# ---------------------------------------------------------------------------


def test_buy_sizes_to_whole_shares_within_envelope_cap() -> None:
    order = _buy(table_price="97000", notional="300000")
    planned, blocked = kr_mock.plan_orders(
        (order,), envelope=ENVELOPE, held_quantities={}
    )
    assert blocked == []
    assert len(planned) == 1
    leg = planned[0]
    # tick-aligned buy price: 97000 has tick=100 (50k-200k band), floor(97000/100)*100=97000
    assert leg.price == 97000
    assert leg.quantity == 3  # floor(300000 / 97000) = 3
    assert leg.notional == Decimal("291000")
    assert leg.notional <= ENVELOPE.per_order_notional
    assert leg.client_order_id == "b0xk-abc123"


def test_buy_blocked_when_notional_floors_below_one_share() -> None:
    order = _buy(table_price="500000", notional="300000")
    planned, blocked = kr_mock.plan_orders(
        (order,), envelope=ENVELOPE, held_quantities={}
    )
    assert planned == []
    assert len(blocked) == 1
    assert blocked[0].reason == "sizing_blocked"


def test_sell_sizes_from_held_quantity_times_fraction() -> None:
    order = _sell(table_price="100000", fraction="0.5")
    planned, blocked = kr_mock.plan_orders(
        (order,), envelope=ENVELOPE, held_quantities={"005930": Decimal("7")}
    )
    assert blocked == []
    assert len(planned) == 1
    # floor(7 * 0.5) = 3 shares
    assert planned[0].quantity == 3
    assert planned[0].side == "sell"


def test_sell_blocked_when_fraction_floors_to_zero_shares() -> None:
    order = _sell(table_price="100000", fraction="0.5")
    planned, blocked = kr_mock.plan_orders(
        (order,), envelope=ENVELOPE, held_quantities={"005930": Decimal("1")}
    )
    assert planned == []
    assert blocked[0].reason == "sizing_blocked"


def test_non_positive_table_price_is_blocked_not_raised() -> None:
    order = replace(_buy(table_price="1", notional="300000"), table_price=Decimal("0"))
    planned, blocked = kr_mock.plan_orders(
        (order,), envelope=ENVELOPE, held_quantities={}
    )
    assert planned == []
    assert blocked[0].reason == "non_positive_price"


# ---------------------------------------------------------------------------
# read_fresh_truth — NAV = cash + sum(evaluation_amount), offline fake client.
# ---------------------------------------------------------------------------


class _FakeKrClient:
    def __init__(
        self,
        *,
        cash: dict[str, Any],
        stocks: list[dict[str, Any]],
    ) -> None:
        self._cash = cash
        self._stocks = stocks

    async def inquire_cash_balance(self) -> dict[str, Any]:
        return self._cash

    async def fetch_my_stocks(self) -> list[dict[str, Any]]:
        return self._stocks


@pytest.mark.asyncio
async def test_read_fresh_truth_computes_nav_from_cash_and_holdings() -> None:
    client = _FakeKrClient(
        cash={"dnca_tot_amt": 1000000.0, "stck_cash_ord_psbl_amt": 950000.0},
        stocks=[
            {
                "pdno": "005930",
                "hldg_qty": "10",
                "pchs_avg_pric": "70000",
                "evlu_amt": "750000",
            },
            # zero-quantity rows must not appear as positions or add to NAV
            {"pdno": "000660", "hldg_qty": "0", "pchs_avg_pric": "0", "evlu_amt": "0"},
        ],
    )
    fresh = await kr_mock.read_fresh_truth(client)
    assert fresh.cash == Decimal(
        "950000"
    )  # orderable cash preferred over deposit total
    assert len(fresh.positions) == 1
    assert fresh.positions[0].symbol == "005930"
    assert fresh.nav == Decimal("950000") + Decimal("750000")


@pytest.mark.asyncio
async def test_read_fresh_truth_falls_back_to_deposit_total_when_orderable_absent() -> (
    None
):
    client = _FakeKrClient(
        cash={"dnca_tot_amt": 500000.0, "stck_cash_ord_psbl_amt": 0.0},
        stocks=[],
    )
    fresh = await kr_mock.read_fresh_truth(client)
    assert fresh.cash == Decimal("500000")
    assert fresh.nav == Decimal("500000")


# ---------------------------------------------------------------------------
# Submission is a deliberately unwired extension point.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unwired_submit_order_always_raises() -> None:
    planned = kr_mock.PlannedOrder(
        order_key="abc123",
        client_order_id="b0xk-abc123",
        symbol="005930",
        side="buy",
        leg="buy_l1",
        price=97000,
        quantity=3,
        notional=Decimal("291000"),
    )
    with pytest.raises(kr_mock.KrMockSubmissionNotWired):
        await kr_mock.unwired_submit_order(planned=planned, confirm=True)


# ---------------------------------------------------------------------------
# submit_planned_order — wired to KisMockBroker (contract v1.3 ③).
# ---------------------------------------------------------------------------


#: A venue that *can* answer "what of mine is resting?" and answered "nothing".
#: The real kis_mock lane reports ``KR_PENDING_UNREADABLE`` instead — see
#: ``test_kr_pending_is_unreadable_and_fails_closed_at_the_submission_boundary``
#: below, which pins that. These routing tests use a readable state so they
#: exercise the wiring under the gate rather than re-testing the gate.
_READABLE_EMPTY = BrokerTruth(position_symbols=(), own_pending=())


class _FakeBroker:
    """Records what it was called with; no network, no reservation DB write."""

    def __init__(self) -> None:
        self.buy_calls: list[dict[str, Any]] = []
        self.sell_calls: list[dict[str, Any]] = []

    async def submit_buy(self, **kwargs: Any) -> dict[str, Any]:
        self.buy_calls.append(kwargs)
        return {"success": True, "odno": "FAKE-BUY-1"}

    async def submit_exit_sell(self, **kwargs: Any) -> dict[str, Any]:
        self.sell_calls.append(kwargs)
        return {"success": True, "odno": "FAKE-SELL-1"}


@pytest.mark.asyncio
async def test_submit_planned_order_routes_buy_to_submit_buy() -> None:
    broker = _FakeBroker()
    planned = kr_mock.PlannedOrder(
        order_key="abc123",
        client_order_id="b0xk-abc123",
        symbol="005930",
        side="buy",
        leg="buy_l1",
        price=97000,
        quantity=3,
        notional=Decimal("291000"),
    )
    result = await kr_mock.submit_planned_order(
        broker, planned=planned, confirm=True, broker_truth=_READABLE_EMPTY
    )
    assert result["success"] is True
    assert broker.buy_calls == [
        {
            "symbol": "005930",
            "price": Decimal("97000"),
            "quantity": Decimal("3"),
            "correlation_id": "b0xk-abc123",
            "confirm": True,
        }
    ]
    assert broker.sell_calls == []


@pytest.mark.asyncio
async def test_submit_planned_order_refreshes_the_reused_broker_before_buy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B0-X supplies a real mock-host book to the existing pre-send hook.

    The test uses the actual sanctioned adapter type but replaces its network
    methods, proving the thin B0-X chokepoint orders the read before submit
    without creating another order implementation.
    """

    broker = kr_mock.build_kis_mock_broker()
    events: list[str] = []

    async def _refresh(*, symbol: str) -> object:
        assert symbol == "005930"
        events.append("refresh")
        return object()

    async def _submit_buy(**kwargs: Any) -> dict[str, Any]:
        events.append("submit")
        return {"success": True, "odno": "FAKE-BUY-1"}

    monkeypatch.setattr(broker, "refresh_market_state", _refresh)
    monkeypatch.setattr(broker, "submit_buy", _submit_buy)
    planned = kr_mock.PlannedOrder(
        order_key="abc123",
        client_order_id="b0xk-abc123",
        symbol="005930",
        side="buy",
        leg="buy_l1",
        price=97000,
        quantity=3,
        notional=Decimal("291000"),
    )

    result = await kr_mock.submit_planned_order(
        broker, planned=planned, confirm=True, broker_truth=_READABLE_EMPTY
    )

    assert result["success"] is True
    assert events == ["refresh", "submit"]


@pytest.mark.asyncio
async def test_submit_planned_order_blocks_before_buy_when_mock_book_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = kr_mock.build_kis_mock_broker()
    refresh = AsyncMock(side_effect=RuntimeError("mock quote unavailable"))
    submit = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(broker, "refresh_market_state", refresh)
    monkeypatch.setattr(broker, "submit_buy", submit)
    planned = kr_mock.PlannedOrder(
        order_key="abc123",
        client_order_id="b0xk-abc123",
        symbol="005930",
        side="buy",
        leg="buy_l1",
        price=97000,
        quantity=3,
        notional=Decimal("291000"),
    )

    result = await kr_mock.submit_planned_order(
        broker, planned=planned, confirm=True, broker_truth=_READABLE_EMPTY
    )

    assert result == {
        "success": False,
        "pre_send_blocked": True,
        "reason_codes": ["mock_orderbook_unavailable"],
        "detail": "RuntimeError: mock orderbook refresh failed",
        "dry_run": False,
    }
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_planned_order_routes_sell_to_submit_exit_sell() -> None:
    broker = _FakeBroker()
    planned = kr_mock.PlannedOrder(
        order_key="def456",
        client_order_id="b0xk-def456",
        symbol="005930",
        side="sell",
        leg="sell_r1",
        price=101850,
        quantity=3,
        notional=Decimal("305550"),
    )
    result = await kr_mock.submit_planned_order(
        broker, planned=planned, confirm=False, broker_truth=_READABLE_EMPTY
    )
    assert result["success"] is True
    assert broker.buy_calls == []
    assert broker.sell_calls == [
        {
            "symbol": "005930",
            "price": Decimal("101850"),
            "quantity": Decimal("3"),
            "exit_reason": "b0x_rule_exit",
            "strategy_id": kr_mock.CLIENT_ORDER_ID_PREFIX,
            "correlation_id": "b0xk-def456",
            "confirm": False,
        }
    ]


def test_build_kis_mock_broker_get_state_always_none() -> None:
    """No live WS feed for B0-X — see module docstring. A real BUY dispatch
    must fail closed via the broker's own PreSendFreshnessError, not a
    fabricated quote.
    """

    broker = kr_mock.build_kis_mock_broker()
    assert kr_mock._b0x_get_state("005930") is None
    assert broker._get_state("005930") is None


# ---------------------------------------------------------------------------
# Contract v1.5 ① — broker-derived cap inputs, and KR's unreadable pending.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kr_pending_is_unreadable_and_fails_closed_at_the_submission_boundary() -> (
    None
):
    """The last line before the venue re-checks ①, and KR always refuses.

    Derivation already refuses every row on this lane, so reaching here means a
    caller bypassed it. That must stop, not double-submit blind — same posture
    as the sidecar re-running its own gates in ``submit_planned``.
    """

    truth = kr_mock.FreshTruth(
        cash=Decimal("5000000"), nav=Decimal("5000000"), positions=()
    ).broker_truth()
    assert truth.pending_unreadable is kr_mock.KR_PENDING_UNREADABLE
    assert truth.own_pending_symbols == ()  # empty is NOT "nothing is resting"

    planned = kr_mock.PlannedOrder(
        order_key="abc123",
        client_order_id="b0xk-abc123",
        symbol="005930",
        side="buy",
        leg="buy_l1",
        price=97000,
        quantity=3,
        notional=Decimal("291000"),
    )
    broker = _FakeBroker()
    with pytest.raises(OwnPendingResubmitBlocked):
        await kr_mock.submit_planned_order(
            broker, planned=planned, confirm=True, broker_truth=truth
        )
    assert broker.buy_calls == [] and broker.sell_calls == []


def test_kr_unreadable_reason_names_both_dead_surfaces() -> None:
    """Both KIS mock dead ends are cited, so the record is auditable.

    A bare "unsupported" would leave a reader unable to tell whether the lane
    simply had not wired an inquiry yet — the distinction the KR kill-switch
    note already makes ("시도조차 구조적으로 불가", not untested).
    """

    detail = kr_mock.KR_PENDING_UNREADABLE.detail
    assert "TTTC8036R" in detail  # pending-order inquiry raises for is_mock
    assert "daily-ccld" in detail  # daily execution inquiry can be empty
    assert (
        kr_mock.KR_PENDING_UNREADABLE.reason == "kis_mock_pending_inquiry_unsupported"
    )


def test_kr_non_dust_positions_use_the_krx_whole_share_unit() -> None:
    """① 동시 포지션 counts holdings that could become a SELL.

    KRX's minimum trade unit is one whole share, so a sub-share residue floors
    to zero and cannot be sold — the KR spelling of the contract v1.2 dust rule
    (LOT_SIZE floor only). A notional-based widening stays forbidden: a 1-share
    holding of a cheap stock is still a position.
    """

    truth = kr_mock.FreshTruth(
        cash=Decimal("0"),
        nav=Decimal("0"),
        positions=(
            kr_mock.RawPosition(
                symbol="005930",
                quantity=Decimal("1"),  # exactly one share — a position
                average_price=Decimal("100"),
                evaluation_amount=Decimal("100"),
            ),
            kr_mock.RawPosition(
                symbol="000660",
                quantity=Decimal("0.4"),  # floors to zero shares — dust
                average_price=Decimal("200000"),
                evaluation_amount=Decimal("80000"),
            ),
        ),
    )
    assert truth.non_dust_position_symbols() == ("005930",)
    assert truth.broker_truth().concurrent_position_count == 1


def test_kr_broker_truth_carries_no_persisted_state() -> None:
    """v1.5 ① — the cap inputs are a pure function of one broker read.

    Two ``FreshTruth`` values with identical contents produce identical cap
    inputs, and nothing about them depends on a previous cycle. This is the
    property ``attributed_book.json`` did not have (its contents were always
    ``None``, so the counters always restarted).
    """

    def _truth() -> BrokerTruth:
        return kr_mock.FreshTruth(
            cash=Decimal("1"),
            nav=Decimal("1"),
            positions=(
                kr_mock.RawPosition(
                    symbol="005930",
                    quantity=Decimal("5"),
                    average_price=Decimal("100"),
                    evaluation_amount=Decimal("500"),
                ),
            ),
        ).broker_truth()

    assert _truth().canonical() == _truth().canonical()
    assert _truth().canonical()["position_symbols"] == ["005930"]
