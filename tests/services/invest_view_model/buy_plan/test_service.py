"""§144차 — 매수 계획 aggregate contract.

The board is a funding decision aid, so the properties pinned here are the
ones that would send the operator to move the wrong amount of money: which
holdings become rows, which cash counts, what is double-counted, and how an
unreadable gate metric is reported.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from app.schemas.invest_home import (
    Account,
    CashAmounts,
    GroupedHolding,
    GroupedSourceBreakdown,
    HomeSummary,
    InvestHomeResponse,
)
from app.schemas.invest_watches import WatchAlertRow, WatchesResponse
from app.schemas.open_orders import OpenOrderRow, OpenOrdersResponse
from app.services.invest_view_model.buy_plan import gate_inputs
from app.services.invest_view_model.buy_plan.gate_inputs import GateMetricReading
from app.services.invest_view_model.buy_plan.service import BuyPlanService

NOW = dt.datetime(2026, 8, 23, 3, 0, tzinfo=dt.UTC)


def _breakdown(
    *,
    source: str,
    quantity: float,
    average_cost: float,
    tradeable: bool = True,
) -> GroupedSourceBreakdown:
    return GroupedSourceBreakdown(
        holdingId=f"{source}:{quantity}",
        accountId=f"acct-{source}",
        source=source,  # type: ignore[arg-type]
        accountKind="live" if tradeable else "manual",
        quantity=quantity,
        averageCost=average_cost,
        costBasis=quantity * average_cost,
        isTradeable=tradeable,
        sourceOfTruth=tradeable,
        manualOnly=not tradeable,
    )


def _group(
    *,
    symbol: str,
    market: str,
    currency: str,
    quantity: float,
    average_cost: float,
    current_price: float,
    breakdown: list[GroupedSourceBreakdown] | None = None,
) -> GroupedHolding:
    parts = breakdown or [
        _breakdown(
            source="upbit" if market == "CRYPTO" else "kis",
            quantity=quantity,
            average_cost=average_cost,
        )
    ]
    return GroupedHolding(
        groupId=f"{market}:{symbol}",
        symbol=symbol,
        market=market,  # type: ignore[arg-type]
        assetType="crypto" if market == "CRYPTO" else "equity",
        assetCategory=(
            "crypto"
            if market == "CRYPTO"
            else ("kr_stock" if market == "KR" else "us_stock")
        ),
        displayName=symbol,
        currency=currency,  # type: ignore[arg-type]
        totalQuantity=quantity,
        tradeableQuantity=sum(p.quantity for p in parts if p.isTradeable),
        averageCost=average_cost,
        costBasis=quantity * average_cost,
        valueNative=quantity * current_price,
        pnlRate=(current_price - average_cost) / average_cost,
        priceState="live",
        includedSources=[p.source for p in parts],
        sourceBreakdown=parts,
    )


def _home(
    *,
    groups: list[GroupedHolding],
    accounts: list[Account] | None = None,
) -> InvestHomeResponse:
    return InvestHomeResponse(
        homeSummary=HomeSummary(
            includedSources=["kis", "upbit"],
            excludedSources=[],
            totalValueKrw=0.0,
        ),
        accounts=accounts if accounts is not None else [],
        holdings=[],
        groupedHoldings=groups,
    )


def _account(
    *,
    account_id: str,
    source: str,
    kind: str = "live",
    krw: float | None = None,
    usd: float | None = None,
    buying_krw: float | None = None,
) -> Account:
    return Account(
        accountId=account_id,
        displayName=account_id,
        source=source,  # type: ignore[arg-type]
        accountKind=kind,  # type: ignore[arg-type]
        includedInHome=True,
        valueKrw=0.0,
        cashBalances=CashAmounts(krw=krw, usd=usd),
        buyingPower=CashAmounts(krw=buying_krw),
    )


class _StubHome:
    def __init__(self, response: InvestHomeResponse | Exception) -> None:
        self._response = response

    async def get_home(self, *, user_id: int) -> InvestHomeResponse:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _StubWatches:
    def __init__(self, items: list[WatchAlertRow] | None = None) -> None:
        self._items = items or []

    async def list_watches(self, *, market: str, status: str) -> WatchesResponse:
        return WatchesResponse(
            market=market,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            count=len(self._items),
            data_state="ok",
            as_of=NOW,
            items=self._items,
        )


class _StubOpenOrders:
    def __init__(self, items: list[OpenOrderRow] | None = None) -> None:
        self._items = items or []

    async def list_open_orders(self, *, market: str) -> OpenOrdersResponse:
        return OpenOrdersResponse(
            market=market,  # type: ignore[arg-type]
            count=len(self._items),
            data_state="ok",
            as_of=NOW,
            items=self._items,
            sources=[],
        )


def _service(
    *,
    home: Any,
    watches: Any = None,
    open_orders: Any = None,
) -> BuyPlanService:
    return BuyPlanService(
        home_service=home,
        watch_service=watches or _StubWatches(),
        open_orders_service=open_orders or _StubOpenOrders(),
    )


@pytest.fixture(autouse=True)
def _stub_gate_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """No network in unit tests; each test opts into its own gate readings."""

    gate_inputs._reset_cache_for_tests()

    async def _breadth() -> GateMetricReading:
        return GateMetricReading(
            metric="upbit_alt_breadth_24h", value=Decimal("62"), source="stub"
        )

    async def _lsr() -> GateMetricReading:
        return GateMetricReading(
            metric="btc_long_short_ratio", value=Decimal("1.2"), source="stub"
        )

    monkeypatch.setattr(
        "app.services.invest_view_model.buy_plan.service.read_alt_breadth_24h", _breadth
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.buy_plan.service.read_btc_long_short_ratio",
        _lsr,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_underwater_lot_becomes_an_averaging_row_with_a_turn_point() -> None:
    home = _home(
        groups=[
            _group(
                symbol="XRP",
                market="CRYPTO",
                currency="KRW",
                quantity=100,
                average_cost=1100,
                current_price=1050,
            )
        ]
    )
    plan = await _service(home=_StubHome(home)).build(user_id=1, market="all", now=NOW)

    assert [row.symbol for row in plan.averaging_triggers] == ["XRP"]
    row = plan.averaging_triggers[0]
    assert row.turn_point_price == Decimal("1000")
    assert row.turn_point_reached is False
    assert [s.offset_from_turn_point_pct for s in row.samples] == [
        Decimal("-1"),
        Decimal("-3"),
    ]
    # The reserve figure is the deeper (more expensive) of the two samples.
    assert row.reserve_plan_notional == max(s.additional_notional for s in row.samples)
    assert row.market_rank == 1
    assert row.within_policy_add_cap is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_manual_reference_quantity_is_excluded_from_the_average() -> None:
    """A manual lot cannot be added to through a broker.

    Folding it into the average would move the turn point to a price no order
    can act on, so it is excluded and the exclusion is reported.
    """

    home = _home(
        groups=[
            _group(
                symbol="005930",
                market="KR",
                currency="KRW",
                quantity=30,
                average_cost=70000,
                current_price=60000,
                breakdown=[
                    _breakdown(source="kis", quantity=10, average_cost=110000),
                    _breakdown(
                        source="toss_manual",
                        quantity=20,
                        average_cost=50000,
                        tradeable=False,
                    ),
                ],
            )
        ]
    )
    plan = await _service(home=_StubHome(home)).build(user_id=1, market="kr", now=NOW)

    row = plan.averaging_triggers[0]
    assert row.quantity == Decimal("10")
    assert row.average_price == Decimal("110000")
    assert row.turn_point_price == Decimal("100000")
    assert any("수동/참고" in note for note in row.notes)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_market_add_cap_bounds_the_funding_total() -> None:
    """Only the top ``max_add_symbols_per_market`` rows are money to reserve."""

    groups = [
        _group(
            symbol=symbol,
            market="CRYPTO",
            currency="KRW",
            quantity=1000,
            average_cost=1100,
            current_price=price,
        )
        for symbol, price in (("AAA", 900), ("BBB", 910), ("CCC", 920))
    ]
    plan = await _service(home=_StubHome(_home(groups=groups))).build(
        user_id=1, market="crypto", now=NOW
    )

    assert len(plan.averaging_triggers) == 3
    within = [row for row in plan.averaging_triggers if row.within_policy_add_cap]
    assert len(within) == 2
    krw = next(c for c in plan.funding.currencies if c.currency == "KRW")
    assert krw.required_averaging_adds == sum(
        (row.reserve_plan_notional for row in within), Decimal(0)
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_only_live_accounts_fund_the_reserve() -> None:
    home = _home(
        groups=[],
        accounts=[
            _account(account_id="upbit-1", source="upbit", krw=500000),
            _account(
                account_id="pension", source="pension_manual", kind="manual", krw=9e9
            ),
        ],
    )
    plan = await _service(home=_StubHome(home)).build(user_id=1, market="all", now=NOW)

    krw = next(c for c in plan.funding.currencies if c.currency == "KRW")
    assert krw.available_cash == Decimal("500000")
    assert krw.verdict == "sufficient"
    included = {
        row.account_id for row in plan.funding.accounts if row.included_in_reserve
    }
    assert included == {"upbit-1"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_buying_power_wins_over_the_raw_cash_balance() -> None:
    home = _home(
        groups=[],
        accounts=[
            _account(account_id="kis-1", source="kis", krw=1000000, buying_krw=400000)
        ],
    )
    plan = await _service(home=_StubHome(home)).build(user_id=1, market="all", now=NOW)

    row = next(r for r in plan.funding.accounts if r.currency == "KRW")
    assert row.available_cash == Decimal("400000")
    assert row.available_cash_source == "buyingPower"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shortfall_reports_the_deposit_amount() -> None:
    home = _home(
        groups=[
            _group(
                symbol="XRP",
                market="CRYPTO",
                currency="KRW",
                quantity=1000,
                average_cost=1100,
                current_price=900,
            )
        ],
        accounts=[_account(account_id="upbit-1", source="upbit", krw=1000)],
    )
    plan = await _service(home=_StubHome(home)).build(
        user_id=1, market="crypto", now=NOW
    )

    krw = next(c for c in plan.funding.currencies if c.currency == "KRW")
    assert krw.verdict == "shortfall"
    assert krw.shortfall == krw.required_total - Decimal("1000")
    assert krw.shortfall > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_partially_unknown_cash_holds_the_verdict_instead_of_understating() -> (
    None
):
    home = _home(
        groups=[],
        accounts=[
            _account(account_id="upbit-1", source="upbit", krw=500000),
            _account(account_id="kis-1", source="kis", usd=None, krw=None),
        ],
    )
    plan = await _service(home=_StubHome(home)).build(user_id=1, market="all", now=NOW)

    krw = next(c for c in plan.funding.currencies if c.currency == "KRW")
    assert krw.verdict == "unknown"
    assert krw.available_cash is None
    assert any("가용 현금" in warning for warning in plan.warnings)


def _watch(
    *,
    symbol: str,
    market: str = "crypto",
    threshold: str = "900",
    max_action: dict | None = None,
    intent: str = "buy_review",
) -> WatchAlertRow:
    return WatchAlertRow(
        alert_uuid=uuid4(),
        source_report_uuid=None,
        market=market,  # type: ignore[arg-type]
        symbol=symbol,
        target_kind="asset",
        metric="price_below",
        operator="below",
        threshold=Decimal(threshold),
        status="active",
        valid_until=NOW + dt.timedelta(days=5),
        intent=intent,
        action_mode="review",
        rationale="test",
        max_action=max_action or {},
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sell_watches_never_enter_the_buy_plan() -> None:
    watches = _StubWatches(
        [
            _watch(symbol="KRW-XRP", max_action={"side": "buy", "notional": 100000}),
            _watch(symbol="KRW-SOL", max_action={"side": "sell", "notional": 100000}),
        ]
    )
    plan = await _service(home=_StubHome(_home(groups=[])), watches=watches).build(
        user_id=1, market="all", now=NOW
    )

    assert [row.symbol for row in plan.active_buy_watches] == ["KRW-XRP"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_action_side_overrides_a_buy_review_intent() -> None:
    """The typed execution plan is authoritative over the legacy intent field."""

    watches = _StubWatches(
        [
            _watch(
                symbol="KRW-XRP",
                intent="buy_review",
                max_action={"side": "sell", "notional": 100000},
            )
        ]
    )
    plan = await _service(home=_StubHome(_home(groups=[])), watches=watches).build(
        user_id=1, market="all", now=NOW
    )

    assert plan.active_buy_watches == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watch_without_a_notional_is_carded_not_auto_submitted() -> None:
    watches = _StubWatches([_watch(symbol="KRW-XRP", max_action={"side": "buy"})])
    plan = await _service(home=_StubHome(_home(groups=[])), watches=watches).build(
        user_id=1, market="all", now=NOW
    )

    row = plan.active_buy_watches[0]
    assert row.planned_notional is None
    assert row.approval_lane == "human_card"
    assert row.approval_lane_reason == "notional_unavailable"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_support_net_lists_profitable_holdings_and_excludes_losers() -> None:
    home = _home(
        groups=[
            _group(
                symbol="XRP",
                market="CRYPTO",
                currency="KRW",
                quantity=1000,
                average_cost=1000,
                current_price=1200,
            ),
            _group(
                symbol="SOL",
                market="CRYPTO",
                currency="KRW",
                quantity=10,
                average_cost=200000,
                current_price=150000,
            ),
        ]
    )
    plan = await _service(home=_StubHome(home)).build(
        user_id=1, market="crypto", now=NOW
    )

    by_symbol = {row.symbol: row for row in plan.support_net.rows}
    assert by_symbol["XRP"].eligible is True
    assert by_symbol["SOL"].eligible is False
    assert by_symbol["SOL"].ineligible_reason == "이익권 아님 (평가손익 ≤ 0)"
    # Ineligible coins get no headroom to spend.
    assert by_symbol["SOL"].remaining_headroom_notional == 0
    assert plan.support_net.per_symbol_cap_notional == Decimal("300000")
    assert plan.support_net.tier_cap_notional == Decimal("900000")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resting_buy_order_consumes_headroom_but_not_new_cash() -> None:
    """The broker already holds cash for a resting limit.

    Counting it again as "money to deposit" would tell the operator to move
    KRW that is already committed.
    """

    home = _home(
        groups=[
            _group(
                symbol="XRP",
                market="CRYPTO",
                currency="KRW",
                quantity=1000,
                average_cost=1000,
                current_price=1200,
            )
        ],
        accounts=[_account(account_id="upbit-1", source="upbit", krw=1000000)],
    )
    orders = _StubOpenOrders(
        [
            OpenOrderRow(
                broker="upbit",
                market="crypto",
                symbol="KRW-XRP",
                side="buy",
                price=Decimal("1100"),
                quantity=Decimal("100"),
                remaining_qty=Decimal("100"),
                order_no="upbit-1",
            )
        ]
    )
    plan = await _service(home=_StubHome(home), open_orders=orders).build(
        user_id=1, market="crypto", now=NOW
    )

    row = next(r for r in plan.support_net.rows if r.symbol == "XRP")
    assert [p.form for p in row.placements] == ["resting_order"]
    assert row.placed_notional == Decimal("110000")
    assert row.remaining_headroom_notional == Decimal("190000")
    krw = next(c for c in plan.funding.currencies if c.currency == "KRW")
    assert krw.required_support_net == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_watch_rung_is_not_counted_twice() -> None:
    """The same watch appears in both the net and the watch list.

    It must contribute its notional to the required total exactly once.
    """

    home = _home(
        groups=[
            _group(
                symbol="XRP",
                market="CRYPTO",
                currency="KRW",
                quantity=1000,
                average_cost=1000,
                current_price=1200,
            )
        ],
        accounts=[_account(account_id="upbit-1", source="upbit", krw=1000000)],
    )
    watches = _StubWatches(
        [_watch(symbol="KRW-XRP", max_action={"side": "buy", "amount_krw": 120000})]
    )
    plan = await _service(home=_StubHome(home), watches=watches).build(
        user_id=1, market="crypto", now=NOW
    )

    krw = next(c for c in plan.funding.currencies if c.currency == "KRW")
    assert krw.required_support_net == Decimal("120000")
    assert krw.required_active_watches == 0
    assert krw.required_total == krw.required_averaging_adds + Decimal("120000")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_opens_when_both_conditions_pass() -> None:
    plan = await _service(home=_StubHome(_home(groups=[]))).build(
        user_id=1, market="crypto", now=NOW
    )

    gate = plan.discovery_gates[0]
    assert gate.state == "open"
    assert gate.met_count == 2
    assert gate.unavailable_count == 0
    # Provenance is the policy's declared upstream list, not a constant the
    # reader could drift away from.
    breadth = next(c for c in gate.conditions if c.metric == "upbit_alt_breadth_24h")
    assert breadth.source == "upbit_open_api_ticker_derived"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unreadable_metric_never_counts_as_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``missing_or_null_threshold: do_not_infer_or_count_as_met``."""

    async def _dead() -> GateMetricReading:
        return GateMetricReading(
            metric="upbit_alt_breadth_24h", value=None, source="stub", note="down"
        )

    monkeypatch.setattr(
        "app.services.invest_view_model.buy_plan.service.read_alt_breadth_24h", _dead
    )
    plan = await _service(home=_StubHome(_home(groups=[]))).build(
        user_id=1, market="crypto", now=NOW
    )

    gate = plan.discovery_gates[0]
    assert gate.state == "indeterminate"
    assert gate.met_count == 1
    assert gate.unavailable_count == 1
    breadth = next(c for c in gate.conditions if c.metric == "upbit_alt_breadth_24h")
    assert breadth.state == "unavailable"
    assert breadth.current_value is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failing_condition_closes_the_gate() -> None:
    async def _low() -> GateMetricReading:
        return GateMetricReading(
            metric="upbit_alt_breadth_24h", value=Decimal("30"), source="stub"
        )

    service = _service(home=_StubHome(_home(groups=[])))
    import app.services.invest_view_model.buy_plan.service as service_module

    original = service_module.read_alt_breadth_24h
    service_module.read_alt_breadth_24h = _low  # type: ignore[assignment]
    try:
        plan = await service.build(user_id=1, market="crypto", now=NOW)
    finally:
        service_module.read_alt_breadth_24h = original  # type: ignore[assignment]

    gate = plan.discovery_gates[0]
    assert gate.state == "closed"
    assert gate.met_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_dead_holdings_reader_degrades_instead_of_raising() -> None:
    plan = await _service(home=_StubHome(RuntimeError("KIS down"))).build(
        user_id=1, market="all", now=NOW
    )

    assert plan.averaging_triggers == []
    assert any("보유·현금 조회 실패" in warning for warning in plan.warnings)
    krw = next(c for c in plan.funding.currencies if c.currency == "KRW")
    assert krw.verdict == "unknown"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_response_carries_its_own_provenance() -> None:
    plan = await _service(home=_StubHome(_home(groups=[]))).build(
        user_id=1, market="all", now=NOW
    )

    assert plan.as_of == NOW
    assert plan.policy.version
    assert plan.policy.content_hash
    assert "표시용 근사" in plan.approximation_notice
    assert plan.cache_ttl_seconds > 0
    assert {source.field for source in plan.value_sources} >= {
        "averaging_triggers.*",
        "funding.accounts[].available_cash",
        "*.approval_lane",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_market_filter_scopes_every_block() -> None:
    home = _home(
        groups=[
            _group(
                symbol="XRP",
                market="CRYPTO",
                currency="KRW",
                quantity=1000,
                average_cost=1100,
                current_price=900,
            ),
            _group(
                symbol="005930",
                market="KR",
                currency="KRW",
                quantity=10,
                average_cost=110000,
                current_price=90000,
            ),
        ]
    )
    plan = await _service(home=_StubHome(home)).build(user_id=1, market="kr", now=NOW)

    assert [row.symbol for row in plan.averaging_triggers] == ["005930"]
    assert plan.support_net.rows == []
    assert plan.support_net.enabled is False
    # The crypto gate is not evaluated for a KR-scoped board.
    assert plan.discovery_gates == []
