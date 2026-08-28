"""§144차 — 매수 계획 aggregate contract.

The board is a funding decision aid, so the properties pinned here are the
ones that would send the operator to move the wrong amount of money: which
holdings become rows, which cash counts, what is double-counted, and how an
unreadable gate metric is reported.
"""

from __future__ import annotations

import datetime as dt
import textwrap
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
    InvestHomeResponseMeta,
    InvestHomeWarning,
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
    meta_warnings: list[InvestHomeWarning] | None = None,
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
        meta=InvestHomeResponseMeta(warnings=meta_warnings or []),
    )


def _scope(plan, broker: str, currency: str = "KRW"):
    """The one reconciliation row for a (broker, currency) pair."""

    return next(
        row
        for row in plan.funding.scopes
        if row.broker == broker and row.currency == currency
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
    def __init__(
        self,
        items: list[WatchAlertRow] | None = None,
        *,
        data_state: str = "ok",
        warnings: list[str] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._items = items or []
        self._data_state = data_state
        self._warnings = warnings or []
        self._raises = raises

    async def list_watches(self, *, market: str, status: str) -> WatchesResponse:
        if self._raises is not None:
            raise self._raises
        return WatchesResponse(
            market=market,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            count=len(self._items),
            data_state=self._data_state,  # type: ignore[arg-type]
            as_of=NOW,
            items=self._items,
            warnings=self._warnings,
        )


class _StubOpenOrders:
    def __init__(
        self,
        items: list[OpenOrderRow] | None = None,
        *,
        data_state: str = "ok",
        warnings: list[str] | None = None,
    ) -> None:
        self._items = items or []
        self._data_state = data_state
        self._warnings = warnings or []

    async def list_open_orders(self, *, market: str) -> OpenOrdersResponse:
        return OpenOrdersResponse(
            market=market,  # type: ignore[arg-type]
            count=len(self._items),
            data_state=self._data_state,  # type: ignore[arg-type]
            as_of=NOW,
            items=self._items,
            sources=[],
            warnings=self._warnings,
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
    krw = _scope(plan, "upbit")
    assert krw.required_averaging_adds == sum(
        (row.reserve_plan_notional for row in within), Decimal(0)
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_only_live_accounts_fund_the_reserve() -> None:
    """Only live broker cash can actually be moved and spent by an order.

    verify-r1 §3 found this test green against a mutant that added ``paper``
    to ``_RESERVE_ACCOUNT_KINDS`` — the fixture held a manual account and no
    paper one, so the assertion guarded nothing. Both non-live kinds are in
    the fixture now, and each carries an absurd balance so an accidental
    inclusion is impossible to miss.
    """

    home = _home(
        groups=[],
        accounts=[
            _account(account_id="upbit-1", source="upbit", krw=500000),
            _account(
                account_id="pension", source="pension_manual", kind="manual", krw=9e9
            ),
            _account(
                account_id="alpaca-paper",
                source="alpaca_paper",
                kind="paper",
                krw=9e9,
            ),
            _account(account_id="kis-mock", source="kis_mock", kind="paper", krw=9e9),
        ],
    )
    plan = await _service(home=_StubHome(home)).build(user_id=1, market="all", now=NOW)

    krw = _scope(plan, "upbit")
    assert krw.available_cash == Decimal("500000")
    assert krw.verdict == "sufficient"
    included = {
        row.account_id for row in plan.funding.accounts if row.included_in_reserve
    }
    assert included == {"upbit-1"}
    # No scope anywhere may hold the paper balance, whatever broker it maps to.
    assert all(
        scope.available_cash is None or scope.available_cash <= Decimal("500000")
        for scope in plan.funding.scopes
    )
    assert "alpaca-paper" not in {
        account_id for scope in plan.funding.scopes for account_id in scope.account_ids
    }


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

    krw = _scope(plan, "upbit")
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

    # Broker scoping means an unreadable KIS account no longer taints the
    # Upbit verdict — Upbit's own cash and requirements are both known.
    assert _scope(plan, "upbit").verdict == "sufficient"
    kis = _scope(plan, "kis")
    assert kis.verdict == "unknown"
    assert kis.available_cash is None
    assert any("가용 현금" in warning for warning in plan.warnings)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kis_usd_mismatch_warning_keeps_usd_scope_unknown() -> None:
    """KIS KRW cash must not make an explicitly unavailable USD read disappear."""
    home = _home(
        groups=[],
        accounts=[_account(account_id="kis-1", source="kis", krw=500000, usd=None)],
        meta_warnings=[
            InvestHomeWarning(
                source="kis",
                message=(
                    "USD 예수금/주문가능 금액 모순("
                    "kis_overseas_usd_balance_orderable_mismatch)"
                ),
            )
        ],
    )

    plan = await _service(home=_StubHome(home)).build(user_id=1, market="all", now=NOW)

    usd = _scope(plan, "kis", "USD")
    assert usd.available_cash is None
    assert usd.verdict == "unknown"
    assert usd.account_ids == ["kis-1"]
    assert any(
        row.account_id == "kis-1"
        and row.currency == "USD"
        and row.available_cash is None
        for row in plan.funding.accounts
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_unreadable_account_holds_its_own_scope() -> None:
    """Two accounts at the same broker, one silent → that scope goes unknown.

    Totalling only the account that answered would understate the broker's
    cash and send the operator to deposit money they already hold.
    """

    home = _home(
        groups=[],
        accounts=[
            _account(account_id="kis-a", source="kis", krw=500000),
            _account(account_id="kis-b", source="kis"),
        ],
    )
    plan = await _service(home=_StubHome(home)).build(user_id=1, market="all", now=NOW)

    kis = _scope(plan, "kis")
    assert kis.verdict == "unknown"
    assert kis.available_cash is None
    assert sorted(kis.account_ids) == ["kis-a", "kis-b"]


def _watch(
    *,
    symbol: str,
    market: str = "crypto",
    threshold: str = "900",
    max_action: dict | None = None,
    intent: str = "buy_review",
) -> WatchAlertRow:
    """A watch whose ``max_action`` names its execution account by default.

    ``account_mode`` is what attributes the cash to a broker; tests that need
    the unattributed path pass a ``max_action`` without it.
    """
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
        max_action=max_action if max_action is not None else {},
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sell_watches_never_enter_the_buy_plan() -> None:
    watches = _StubWatches(
        [
            _watch(
                symbol="KRW-XRP",
                max_action={"side": "buy", "account_mode": "upbit", "notional": 100000},
            ),
            _watch(
                symbol="KRW-SOL",
                max_action={
                    "side": "sell",
                    "account_mode": "upbit",
                    "notional": 100000,
                },
            ),
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
                max_action={
                    "side": "sell",
                    "account_mode": "upbit",
                    "notional": 100000,
                },
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
    watches = _StubWatches(
        [_watch(symbol="KRW-XRP", max_action={"side": "buy", "account_mode": "upbit"})]
    )
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
    krw = _scope(plan, "upbit")
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
        [
            _watch(
                symbol="KRW-XRP",
                max_action={
                    "side": "buy",
                    "account_mode": "upbit",
                    "amount_krw": 120000,
                },
            )
        ]
    )
    plan = await _service(home=_StubHome(home), watches=watches).build(
        user_id=1, market="crypto", now=NOW
    )

    krw = _scope(plan, "upbit")
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
    assert all(scope.verdict == "unknown" for scope in plan.funding.scopes)


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


# ---------------------------------------------------------------------------
# verify-r1 BLOCKER regressions.
#
# Each test replays the exact disproof input from the T2 verification report
# (s144-investboard-verify-20260823-1650) and pins the corrected output. They
# are grouped here so the shared theme stays visible: an upstream that says it
# is incomplete, or a requirement whose account is unknown, must never render
# as a confident number.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_b1_kis_cash_cannot_fund_an_upbit_order() -> None:
    """verify-r1 B1 — Upbit XRP, Upbit KRW=0, KIS KRW=1,000,000.

    The old currency-only total reported ``sufficient`` off KIS cash that no
    Upbit order can spend, so the operator never saw the Upbit deposit.
    """

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
        accounts=[
            _account(account_id="upbit-1", source="upbit", krw=0),
            _account(account_id="kis-1", source="kis", krw=1000000),
        ],
    )
    plan = await _service(home=_StubHome(home)).build(
        user_id=1, market="crypto", now=NOW
    )

    upbit = _scope(plan, "upbit")
    assert upbit.available_cash == Decimal("0")
    assert upbit.required_averaging_adds > 0
    assert upbit.verdict == "shortfall"
    assert upbit.shortfall == upbit.required_total

    # KIS cash is still shown, but in its own scope and with no crypto need.
    kis = _scope(plan, "kis")
    assert kis.available_cash == Decimal("1000000")
    assert kis.required_total == 0
    # Nothing anywhere aggregates the two.
    assert not any(
        scope.available_cash == Decimal("1000000")
        for scope in plan.funding.scopes
        if scope.broker == "upbit"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_b1_requirement_without_an_account_holds_the_scope() -> None:
    """An unattributed requirement is published and suspends the verdict.

    It is not silently dropped from the total (which would understate the
    deposit) and not charged to an arbitrary broker (which would misdirect it).
    """

    home = _home(
        groups=[],
        accounts=[_account(account_id="upbit-1", source="upbit", krw=100000)],
    )
    watches = _StubWatches(
        [_watch(symbol="KRW-XRP", max_action={"side": "buy", "notional": 500000})]
    )
    plan = await _service(home=_StubHome(home), watches=watches).build(
        user_id=1, market="crypto", now=NOW
    )

    assert [row.funding_broker for row in plan.active_buy_watches] == ["unattributed"]
    assert [row.amount for row in plan.funding.unattributed] == [Decimal("500000")]
    upbit = _scope(plan, "upbit")
    assert upbit.required_total == 0
    assert upbit.unattributed_same_currency == Decimal("500000")
    assert upbit.verdict == "unknown"
    # The money also gets a destination row of its own so it is not merely a
    # footnote on somebody else's card.
    destination = _scope(plan, "unattributed")
    assert destination.required_active_watches == Decimal("500000")
    assert destination.verdict == "unknown"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_b1_covering_the_unattributed_pool_is_not_sufficiency() -> None:
    """verify-r2 B1 — this test used to pin the opposite, and was wrong.

    The old rule read "if this account can cover the whole unattributed pool,
    it is sufficient". That treats *this* scope absorbing the money as the
    worst case, but the money may be destined for a different broker whose
    account is empty. Covering it here proves nothing about the account that
    will actually place the order, so the honest verdict is unknown.
    """

    home = _home(
        groups=[],
        accounts=[_account(account_id="upbit-1", source="upbit", krw=900000)],
    )
    watches = _StubWatches(
        [_watch(symbol="KRW-XRP", max_action={"side": "buy", "notional": 500000})]
    )
    plan = await _service(home=_StubHome(home), watches=watches).build(
        user_id=1, market="crypto", now=NOW
    )

    upbit = _scope(plan, "upbit")
    assert upbit.unattributed_same_currency == Decimal("500000")
    assert upbit.upper_bound_if_all_unattributed_lands_here == Decimal("500000")
    assert upbit.verdict == "unknown"
    assert upbit.shortfall is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_b2_missing_home_source_surfaces_instead_of_shrinking_cash() -> None:
    """verify-r1 B2 — KIS reader dies, ``get_home`` still returns 200.

    Old behaviour: ``accounts`` simply lacked KIS, the board totalled what was
    left, printed a 100,000 deposit and **no warning at all**.
    """

    home = _home(
        groups=[],
        accounts=[_account(account_id="upbit-1", source="upbit", krw=500000)],
        meta_warnings=[InvestHomeWarning(source="kis", message="KIS reader failed")],
    )
    watches = _StubWatches(
        [
            _watch(
                symbol="KRW-XRP",
                max_action={
                    "side": "buy",
                    "account_mode": "upbit",
                    "amount_krw": 600000,
                },
            )
        ]
    )
    plan = await _service(home=_StubHome(home), watches=watches).build(
        user_id=1, market="all", now=NOW
    )

    # The failure is now on the board rather than invisible.
    assert any("KIS reader failed" in warning for warning in plan.warnings)
    assert any("KIS reader failed" in reason for reason in plan.funding.source_warnings)

    upbit = _scope(plan, "upbit")
    # Upbit's own numbers are still known, so the proven deficit still shows...
    assert upbit.shortfall == Decimal("100000")
    # ...but the response no longer claims the requirement side is complete.
    assert upbit.requirements_complete is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_b3_watch_fetch_failure_does_not_fold_to_sufficient() -> None:
    """verify-r1 B3 — watch store down → required 0 → ``리저브 충분``.

    An unknown requirement is not a zero requirement.
    """

    home = _home(
        groups=[],
        accounts=[_account(account_id="upbit-1", source="upbit", krw=0)],
    )
    watches = _StubWatches(raises=RuntimeError("watch store unavailable"))
    plan = await _service(home=_StubHome(home), watches=watches).build(
        user_id=1, market="all", now=NOW
    )

    upbit = _scope(plan, "upbit")
    assert upbit.verdict == "unknown"
    assert upbit.requirements_complete is False
    assert any("워치 조회 실패" in reason for reason in upbit.incomplete_reasons)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_b3_watch_data_state_and_item_skip_warnings_are_read() -> None:
    """The panel usually degrades rather than raising — that path counts too."""

    home = _home(
        groups=[],
        accounts=[_account(account_id="upbit-1", source="upbit", krw=5000000)],
    )
    watches = _StubWatches(
        [],
        data_state="degraded",
        warnings=["1 alert skipped: malformed max_action"],
    )
    plan = await _service(home=_StubHome(home), watches=watches).build(
        user_id=1, market="all", now=NOW
    )

    upbit = _scope(plan, "upbit")
    # Plenty of cash, zero visible requirement — and still not "sufficient",
    # because the skipped alert's cash is unaccounted for.
    assert upbit.verdict == "unknown"
    assert any("alert skipped" in reason for reason in upbit.incomplete_reasons)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_b4_unavailable_open_orders_do_not_publish_a_confident_headroom() -> None:
    """verify-r1 B4 — upstream ``data_state='unavailable'``.

    Old behaviour: 걸린 0원 / 여력 300,000원 as confident numbers, no warning.
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
    orders = _StubOpenOrders([], data_state="unavailable")
    plan = await _service(home=_StubHome(home), open_orders=orders).build(
        user_id=1, market="crypto", now=NOW
    )

    row = next(r for r in plan.support_net.rows if r.symbol == "XRP")
    assert row.placed_notional is None
    assert row.remaining_headroom_notional is None
    assert row.placements_state == "unavailable"
    assert plan.support_net.placed_notional is None
    assert plan.support_net.remaining_notional is None
    assert any("미체결 주문 조회 상태" in warning for warning in plan.warnings)
    assert _scope(plan, "upbit").requirements_complete is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_b4_degraded_open_orders_also_suspend_the_headroom() -> None:
    """A partial collector failure is still a partial order list."""

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
    )
    orders = _StubOpenOrders(
        [], data_state="degraded", warnings=["upbit collector timed out"]
    )
    plan = await _service(home=_StubHome(home), open_orders=orders).build(
        user_id=1, market="crypto", now=NOW
    )

    row = next(r for r in plan.support_net.rows if r.symbol == "XRP")
    assert row.remaining_headroom_notional is None
    assert row.placements_state == "degraded"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_b6_approval_context_publishes_the_master_gate() -> None:
    """verify-r1 B6 — a cap check is not an approval decision.

    ``ORDER_PROPOSALS_AUTO_APPROVE`` ships False, so the honest default is
    "every proposal becomes a manual card" no matter what the caps say.
    """

    plan = await _service(home=_StubHome(_home(groups=[]))).build(
        user_id=1, market="all", now=NOW
    )

    context = plan.approval_context
    assert context.master_gate_setting == "ORDER_PROPOSALS_AUTO_APPROVE"
    assert context.master_gate_enabled is False
    assert "꺼져" in context.notice
    # The conditions the board does not evaluate are named, not implied.
    codes = {condition.code for condition in context.unevaluated_conditions}
    assert "preview_guard_failed" in codes
    assert "daily_cap_exceeded" in codes
    assert len(context.unevaluated_conditions) >= 15
    # The one gate it does evaluate is on the other side of the split.
    assert [c.code for c in context.evaluated_conditions] == ["per_order_cap_exceeded"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_b6_master_gate_on_still_says_the_lane_is_cap_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even armed, the board checked two caps and nothing else."""

    from app.core.config import settings

    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True, raising=False)
    plan = await _service(home=_StubHome(_home(groups=[]))).build(
        user_id=1, market="all", now=NOW
    )

    assert plan.approval_context.master_gate_enabled is True
    assert "cap 두 개만" in plan.approval_context.notice


# ---------------------------------------------------------------------------
# verify-r2 regressions.
#
# Round 1's broker scoping closed the attributed path but opened an
# unattributed one: a "this account covers the whole pool" rule handed a green
# verdict to whichever broker happened to be funded. Each case below replays a
# round-2 disproof input verbatim.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_r2a_funded_broker_never_greenlights_an_unattributed_need() -> None:
    """verify-r2 (a) — Upbit KRW=0, KIS KRW=1,000,000, watch 330,000, no account_mode.

    The old rule printed ``kis: sufficient``. The operator reads a green card,
    skips the Upbit deposit, and the watch fires against an empty account —
    the round-1 B1 mistake reached through the attribution-failure path.
    """

    home = _home(
        groups=[],
        accounts=[
            _account(account_id="upbit-1", source="upbit", krw=0),
            _account(account_id="kis-1", source="kis", krw=1000000),
        ],
    )
    watches = _StubWatches(
        [_watch(symbol="KRW-XRP", max_action={"side": "buy", "notional": 330000})]
    )
    plan = await _service(home=_StubHome(home), watches=watches).build(
        user_id=1, market="crypto", now=NOW
    )

    assert _scope(plan, "kis").verdict == "unknown"
    assert _scope(plan, "upbit").verdict == "unknown"
    # Nothing anywhere on the board reads green while this is unresolved.
    assert all(scope.verdict != "sufficient" for scope in plan.funding.scopes)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_r2b_absent_broker_still_gets_a_destination_row() -> None:
    """verify-r2 (b) — Upbit-only book, KRW 1,000,000, unattributed 600,000.

    The old board showed only ``upbit`` and called it sufficient, so a need
    bound for KIS or Toss had no account on screen to fund.
    """

    home = _home(
        groups=[],
        accounts=[_account(account_id="upbit-1", source="upbit", krw=1000000)],
    )
    watches = _StubWatches(
        [_watch(symbol="KRW-XRP", max_action={"side": "buy", "notional": 600000})]
    )
    plan = await _service(home=_StubHome(home), watches=watches).build(
        user_id=1, market="crypto", now=NOW
    )

    assert _scope(plan, "upbit").verdict == "unknown"
    destination = _scope(plan, "unattributed")
    assert destination.required_active_watches == Decimal("600000")
    assert destination.available_cash is None
    assert destination.verdict == "unknown"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_r2d_unattributed_usd_is_held_even_with_no_usd_account() -> None:
    """verify-r2 (d) — unattributed USD 200 on a KRW-only book.

    ``scope_keys`` used to come only from attributed requirements and cash
    accounts, so no USD row existed and the USD need reached no verdict at
    all. The KRW scope stayed green because its own currency had nothing
    unattributed — which was true, and beside the point.
    """

    home = _home(
        groups=[],
        accounts=[_account(account_id="upbit-1", source="upbit", krw=1000000)],
    )
    watches = _StubWatches(
        [
            _watch(
                symbol="AAPL",
                market="us",
                threshold="150",
                max_action={"side": "buy", "notional": 200},
            )
        ]
    )
    plan = await _service(home=_StubHome(home), watches=watches).build(
        user_id=1, market="all", now=NOW
    )

    usd = _scope(plan, "unattributed", "USD")
    assert usd.required_active_watches == Decimal("200")
    assert usd.verdict == "unknown"
    # The KRW scope is genuinely unaffected — no KRW money is unresolved.
    assert _scope(plan, "upbit").verdict == "sufficient"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_mode", ["upbit_live", "kiwoom_mock", "", "KIS_LIVE"])
async def test_r2f_unmapped_account_mode_lands_unattributed_with_its_real_reason(
    bad_mode: str,
) -> None:
    """verify-r2 (f) — values outside the closed map fall through this path.

    Two things are pinned: the value does not silently attribute to some
    broker, and the published reason names the offending value instead of
    claiming ``account_mode`` was missing (verify-r2 SHOULD-3).
    """

    home = _home(
        groups=[],
        accounts=[
            _account(account_id="upbit-1", source="upbit", krw=0),
            _account(account_id="kis-1", source="kis", krw=1000000),
        ],
    )
    watches = _StubWatches(
        [
            _watch(
                symbol="KRW-XRP",
                max_action={
                    "side": "buy",
                    "account_mode": bad_mode,
                    "notional": 330000,
                },
            )
        ]
    )
    plan = await _service(home=_StubHome(home), watches=watches).build(
        user_id=1, market="crypto", now=NOW
    )

    row = plan.active_buy_watches[0]
    assert row.funding_broker == "unattributed"
    assert all(scope.verdict != "sufficient" for scope in plan.funding.scopes)

    reason = plan.funding.unattributed[0].reason
    if bad_mode.strip():
        # The wrong value is quoted back rather than laundered into "missing".
        assert bad_mode in reason
        assert "지정하지 않았습니다" not in reason
    else:
        assert "account_mode" in reason


@pytest.mark.unit
@pytest.mark.asyncio
async def test_r2_unattributed_row_does_not_double_count_itself() -> None:
    """The destination row holds the pool; it must not also be measured by it."""

    home = _home(groups=[], accounts=[])
    watches = _StubWatches(
        [_watch(symbol="KRW-XRP", max_action={"side": "buy", "notional": 300000})]
    )
    plan = await _service(home=_StubHome(home), watches=watches).build(
        user_id=1, market="crypto", now=NOW
    )

    destination = _scope(plan, "unattributed")
    assert destination.required_total == Decimal("300000")
    assert destination.unattributed_same_currency == Decimal("0")
    assert destination.upper_bound_if_all_unattributed_lands_here == Decimal("300000")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_r2_fully_attributed_book_still_reaches_sufficient() -> None:
    """The fix must not make every board permanently unknown."""

    home = _home(
        groups=[],
        accounts=[_account(account_id="upbit-1", source="upbit", krw=900000)],
    )
    watches = _StubWatches(
        [
            _watch(
                symbol="KRW-XRP",
                max_action={
                    "side": "buy",
                    "account_mode": "upbit",
                    "notional": 300000,
                },
            )
        ]
    )
    plan = await _service(home=_StubHome(home), watches=watches).build(
        user_id=1, market="crypto", now=NOW
    )

    upbit = _scope(plan, "upbit")
    assert upbit.required_active_watches == Decimal("300000")
    assert upbit.unattributed_same_currency == Decimal("0")
    assert upbit.verdict == "sufficient"
    assert plan.funding.unattributed == []
    assert not any(scope.broker == "unattributed" for scope in plan.funding.scopes)


@pytest.mark.unit
def test_s1_unevaluated_conditions_cover_every_real_eligibility_gate() -> None:
    """verify-r2 SHOULD-1 — the published list must not be a short list.

    The reject reasons are string literals inside
    ``evaluate_auto_approve_eligibility``; they are extracted from its AST here
    so the board's split cannot drift away from the function it describes. A
    seven-item list against eighteen real gates still reads as "these are the
    only things we skipped".
    """

    import ast
    import inspect

    from app.services.invest_view_model.buy_plan.service import (
        AUTO_APPROVE_EVALUATED_CONDITIONS,
        AUTO_APPROVE_UNEVALUATED_CONDITIONS,
    )
    from app.services.order_proposals import auto_approve

    source = inspect.getsource(auto_approve.evaluate_auto_approve_eligibility)
    tree = ast.parse(textwrap.dedent(source))

    reasons: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "reject"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            reasons.add(first.value)
        elif isinstance(first, ast.Subscript) and isinstance(first.value, ast.Dict):
            # reject({...}[verdict], ...) — the sell-classification branch.
            for value in first.value.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    reasons.add(value.value)
        elif isinstance(first, ast.IfExp):
            # reject("a" if expanded else "b", ...)
            for value in (first.body, first.orelse):
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    reasons.add(value.value)

    # Sanity: the extractor must actually find gates, or this test proves nothing.
    assert len(reasons) >= 15, sorted(reasons)

    published = {code for code, _ in AUTO_APPROVE_UNEVALUATED_CONDITIONS} | {
        code for code, _ in AUTO_APPROVE_EVALUATED_CONDITIONS
    }
    assert reasons == published, {
        "missing_from_board": sorted(reasons - published),
        "not_in_function": sorted(published - reasons),
    }

    # The one gate the board really does check is on the evaluated side.
    assert {code for code, _ in AUTO_APPROVE_EVALUATED_CONDITIONS} == {
        "per_order_cap_exceeded"
    }
