from __future__ import annotations

import ast
import inspect
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from research.kr_corpus.d3_engine import engine as engine_module
from research.kr_corpus.d3_engine import golden as golden_module
from research.kr_corpus.d3_engine import signals as signals_module
from research.kr_corpus.d3_engine.acceptance import (
    _contract_signal_bars,
    _resistance_probe_bars,
)
from research.kr_corpus.d3_engine.cash import CashLedger
from research.kr_corpus.d3_engine.engine import PortfolioEngine
from research.kr_corpus.d3_engine.guards import (
    SealedAccessBlocked,
    SealedAccessGuard,
    SealedAccessSpy,
)
from research.kr_corpus.d3_engine.indicators import OhlcPoint, scan_fib_window
from research.kr_corpus.d3_engine.metrics import twr_returns
from research.kr_corpus.d3_engine.models import (
    Arm,
    Bar,
    CashflowView,
    Order,
    OrderClass,
    OrderSide,
    OrderStatus,
    PortfolioRunInput,
    Position,
    RunState,
)
from research.kr_corpus.d3_engine.policies import (
    C1Cycle,
    c3_buy_suppressed,
    update_c3_close,
)
from research.kr_corpus.d3_engine.signals import SignalCandidate
from research.kr_corpus.d3_engine.tick import (
    InvalidTickTable,
    TickTable,
)


def _sealed_tick_shape() -> TickTable:
    return TickTable.from_mapping(
        {
            "schema_version": "d3.krx_tick_table.v1",
            "bands": [
                {"lower_inclusive": 0, "upper_exclusive": 2000, "tick": 1},
                {"lower_inclusive": 2000, "upper_exclusive": 5000, "tick": 5},
                {"lower_inclusive": 5000, "upper_exclusive": 20000, "tick": 10},
                {"lower_inclusive": 20000, "upper_exclusive": 50000, "tick": 50},
                {"lower_inclusive": 50000, "upper_exclusive": 200000, "tick": 100},
                {"lower_inclusive": 200000, "upper_exclusive": 500000, "tick": 500},
                {"lower_inclusive": 500000, "upper_exclusive": None, "tick": 1000},
            ],
        }
    )


def test_fib_window_is_prior_120_and_excludes_t() -> None:
    points = [
        OhlcPoint(Decimal("100"), Decimal("90"), Decimal("95")) for _ in range(121)
    ]
    points[10] = OhlcPoint(Decimal("120"), Decimal("100"), Decimal("110"))
    points[50] = OhlcPoint(Decimal("90"), Decimal("80"), Decimal("85"))
    points[120] = OhlcPoint(Decimal("150"), Decimal("70"), Decimal("100"))

    window = scan_fib_window(points, decision_index=120)

    assert window.start_index == 0
    assert window.end_index == 119
    assert window.excluded_index == 120
    assert window.high == Decimal("120")
    assert window.low == Decimal("80")


def test_v026_engine_candidate_publication_calls_frozen_order_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bool, int, str, str]] = []
    original = signals_module.order_class_sort_key

    def recording_key(
        *, is_add: bool, signal_rank: int, symbol: str, rung: str
    ) -> tuple[int, int, str, int]:
        calls.append((is_add, signal_rank, symbol, rung))
        return original(
            is_add=is_add,
            signal_rank=signal_rank,
            symbol=symbol,
            rung=rung,
        )

    monkeypatch.setattr(signals_module, "order_class_sort_key", recording_key)
    candidates = (
        SignalCandidate("NEW", Decimal(10), Decimal("0.04"), Decimal(100), False),
        SignalCandidate("ADD", Decimal(20), Decimal("0.05"), Decimal(90), True),
    )

    ranked = signals_module.rank_candidates(candidates)

    assert [candidate.symbol for candidate in ranked] == ["ADD", "NEW"]
    assert calls == [(True, 2, "ADD", "L1"), (False, 1, "NEW", "L1")]


def test_v028_fee_golden_uses_the_single_engine_cost_path() -> None:
    source = inspect.getsource(golden_module._v028)

    assert "fee_amount(gross)" in source
    assert "round_trip_basis_points()" in source
    assert 'Decimal("0.00215")' not in source


def test_twr_total_loss_is_represented_as_minus_one() -> None:
    cumulative, annualized = twr_returns(
        start_unit_price=Decimal(1),
        end_unit_price=Decimal(0),
        calendar_days=Decimal("365.2425"),
    )

    assert cumulative == annualized == Decimal(-1)


def test_tick_table_rejects_gap_and_overlap() -> None:
    gap = {
        "bands": [
            {"lower_inclusive": 0, "upper_exclusive": 2000, "tick": 1},
            {"lower_inclusive": 3000, "upper_exclusive": None, "tick": 5},
        ]
    }
    overlap = {
        "bands": [
            {"lower_inclusive": 0, "upper_exclusive": 3000, "tick": 1},
            {"lower_inclusive": 2000, "upper_exclusive": None, "tick": 5},
        ]
    }
    with pytest.raises(InvalidTickTable, match="gap"):
        TickTable.from_mapping(gap)
    with pytest.raises(InvalidTickTable, match="overlap"):
        TickTable.from_mapping(overlap)


def test_tick_alignment_and_sell_minus_one_rule() -> None:
    table = _sealed_tick_shape()

    assert table.align_buy(Decimal("9730.5")) == Decimal("9730")
    assert table.align_sell(Decimal("9730.5")) == Decimal("9740")
    assert table.sell_limit(Decimal("3000000")) == Decimal("2999000")
    assert table.is_valid_price(Decimal("2999000"))


def test_t2_payable_is_not_double_debited_and_receivable_waits() -> None:
    ledger = CashLedger(Decimal("10000000"))
    ledger.fill_buy_immediate(amount=Decimal("300645"), trade_session_index=0)
    ledger.fill_sell(net_amount=Decimal("498925"), trade_session_index=1)

    assert ledger.orderable_cash == Decimal("9699355")
    payable = ledger.settle_pre_open(2)
    assert payable == {
        "payable_cleared": Decimal("300645"),
        "receivable_credited": Decimal("0"),
        "cash_delta": Decimal("0"),
    }
    assert ledger.orderable_cash == Decimal("9699355")
    receivable = ledger.settle_pre_open(3)
    assert receivable["receivable_credited"] == Decimal("498925")
    assert ledger.orderable_cash == Decimal("10198280")


def test_c1_counts_filled_notional_and_reserves_then_returns_unfilled() -> None:
    cycle = C1Cycle()
    admitted, _ = cycle.reserve(notional=Decimal("300000"), is_add=False)
    assert admitted
    cycle.expire(Decimal("300000"), is_add=False)
    assert cycle.filled_buy_gross == 0

    for _ in range(4):
        admitted, _ = cycle.reserve(notional=Decimal("300000"), is_add=False)
        assert admitted
        cycle.fill(notional=Decimal("300000"), is_add=False)

    admitted, reason = cycle.reserve(notional=Decimal("300000"), is_add=False)
    assert not admitted
    assert reason == "filled_notional_cap"


def test_c3_uses_post_fill_average_and_suppresses_add_when_armed() -> None:
    position = Position(
        symbol="000660",
        quantity=9,
        average_price=Decimal("10000"),
        invested_cost_basis=Decimal("90000"),
        underwater_streak=89,
    )
    position.apply_buy(
        quantity=9,
        price=Decimal("8000"),
        fee=Decimal("0"),
        session_index=90,
    )
    assert position.average_price == Decimal("9000")
    outcome = update_c3_close(position, close=Decimal("9500"))
    assert not outcome.underwater

    position.underwater_streak = 89
    armed = update_c3_close(position, close=Decimal("8900"))
    assert armed.armed_90
    assert c3_buy_suppressed(position)


def test_sealed_bar_manifest_and_metadata_block_before_access() -> None:
    spy = SealedAccessSpy()
    guard = SealedAccessGuard(spy)
    calls = 0

    def loader() -> str:
        nonlocal calls
        calls += 1
        return "forbidden"

    with pytest.raises(SealedAccessBlocked):
        guard.read_bar(
            path="/tmp/exploration/bars.parquet",
            session=date(2025, 1, 2),
            loader=loader,
        )
    with pytest.raises(SealedAccessBlocked):
        guard.read_manifest(path="/tmp/HOLDOUT/manifest.json", loader=loader)
    with pytest.raises(SealedAccessBlocked):
        guard.read_metadata({"D3_CALIBRATION_2025": "sealed"}, "D3_CALIBRATION_2025")

    assert calls == 0
    assert spy.evidence()["sealed_access_spy"] == 0


def test_d3_package_has_no_forbidden_runtime_imports() -> None:
    package = (
        Path(__file__).resolve().parents[4] / "research" / "kr_corpus" / "d3_engine"
    )
    forbidden = (
        "stage_b",
        "app.services.brokers",
        "app.models",
        "sqlalchemy",
        "taskiq",
        "openai",
        "anthropic",
        "google.generativeai",
        "krx_tick_size_frozen",
    )
    hits: list[str] = []
    for source in sorted(package.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            for module in modules:
                if any(item in module for item in forbidden):
                    hits.append(f"{source.name}:{node.lineno}:{module}")
    assert hits == []


def test_indicator_state_resets_after_missing_market_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = _sealed_tick_shape()
    sessions = [date(2014, 1, 1) + timedelta(days=index) for index in range(122)]
    bars = tuple(
        Bar(
            session=session,
            symbol="005930",
            open=Decimal("10000"),
            high=Decimal("10100"),
            low=Decimal("9900"),
            close=Decimal("10000"),
        )
        for index, session in enumerate(sessions)
        if index != 60
    )
    decision_indexes: list[int] = []

    def record_segment(_history: list[Bar], decision_index: int) -> None:
        decision_indexes.append(decision_index)
        return None

    engine = PortfolioEngine(ticks)
    monkeypatch.setattr(engine, "_signal_for_session", record_segment)
    engine.run(
        PortfolioRunInput(
            arm=Arm.B0,
            cashflow_view=CashflowView.NO_CONTRIBUTION,
            bars=bars,
            market_sessions=tuple(sessions),
            decision_start=sessions[-1],
        )
    )

    assert decision_indexes == [60]


def test_c2_fails_closed_when_prior_xkrx_index_row_is_missing() -> None:
    ticks = _sealed_tick_shape()
    sessions = [date(2014, 1, 1) + timedelta(days=index) for index in range(201)]
    bars = _contract_signal_bars(sessions[-121:], symbols=("005930",))
    engine = PortfolioEngine(ticks)
    missing = sessions[50]
    result = engine.run(
        PortfolioRunInput(
            arm=Arm.C2,
            cashflow_view=CashflowView.NO_CONTRIBUTION,
            bars=bars,
            market_sessions=tuple(sessions),
            index_closes=tuple(
                (session, Decimal(2000 + index))
                for index, session in enumerate(sessions)
                if session != missing
            ),
            decision_start=sessions[-1],
        )
    )

    assert result.fills == ()
    assert any(
        event.get("reason") == "c2_below_sma200_or_missing" for event in result.events
    )


def test_add_eligibility_uses_t_minus_1_close_not_current_bar_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = _sealed_tick_shape()
    sessions = [date(2014, 1, 1) + timedelta(days=index) for index in range(122)]
    bars = tuple(
        Bar(
            session=session,
            symbol="005930",
            open=Decimal("9600"),
            high=Decimal("12100") if index == 121 else Decimal("10100"),
            low=Decimal("8400") if index >= 120 else Decimal("9400"),
            close=(
                Decimal("9000")
                if index == 120
                else Decimal("12000")
                if index == 121
                else Decimal("10000")
            ),
        )
        for index, session in enumerate(sessions)
    )
    engine = PortfolioEngine(ticks)
    monkeypatch.setattr(
        engine,
        "_signal_for_session",
        lambda _history, _index: (
            Decimal("40"),
            Decimal("8500"),
            Decimal("12000"),
            Decimal("8000"),
        ),
    )
    result = engine.run(
        PortfolioRunInput(
            arm=Arm.B0,
            cashflow_view=CashflowView.NO_CONTRIBUTION,
            bars=bars,
            market_sessions=tuple(sessions),
            decision_start=sessions[-2],
        )
    )

    add_fills = [fill for fill in result.fills if fill.order_class.value == "add"]
    assert len(add_fills) == 2
    assert all(fill.session == sessions[-1] for fill in add_fills)


@pytest.mark.parametrize("arm", list(Arm))
def test_all_four_arms_execute_natural_contract_signal(arm: Arm) -> None:
    ticks = _sealed_tick_shape()
    sessions = [date(2014, 1, 1) + timedelta(days=index) for index in range(201)]
    bar_sessions = sessions[-121:]
    bars = _contract_signal_bars(bar_sessions, symbols=("005930",))
    engine = PortfolioEngine(ticks)
    result = engine.run(
        PortfolioRunInput(
            arm=arm,
            cashflow_view=CashflowView.NO_CONTRIBUTION,
            bars=bars,
            market_sessions=tuple(sessions),
            index_closes=tuple(
                (session, Decimal(2000 + index))
                for index, session in enumerate(sessions)
            ),
            decision_start=bar_sessions[-1],
        )
    )

    assert result.status == "OK"
    assert result.evidence["counterfactual_demand_basis"] == (
        "B0_self" if arm is Arm.B0 else "B0_shadow"
    )
    assert result.evidence["counterfactual_demand_pairs"] == [
        {"session": bar_sessions[-1], "symbol": "005930"}
    ]
    assert [fill.side.value for fill in result.fills] == ["buy", "buy"]
    assert [fill.price for fill in result.fills] == [
        Decimal("9600"),
        Decimal("9450"),
    ]
    assert result.metrics["signals_submitted"] == 2
    assert result.metrics["terminal_nav"] == Decimal("10020402.57250")
    assert result.evidence["sealed_access_spy"] == 0
    assert "primary_run_executed" not in result.evidence


def test_sell_fill_and_unitized_mdd_contract_paths() -> None:
    engine = PortfolioEngine(_sealed_tick_shape())
    session = date(2014, 1, 2)

    assert engine._sell_fill_price(
        Decimal(100),
        Bar(session, "SELL", Decimal(105), Decimal(110), Decimal(90), Decimal(100)),
    ) == Decimal(105)
    assert engine._sell_fill_price(
        Decimal(100),
        Bar(session, "SELL", Decimal(95), Decimal(105), Decimal(90), Decimal(100)),
    ) == Decimal(100)
    assert (
        engine._sell_fill_price(
            Decimal(100),
            Bar(
                session,
                "SELL",
                Decimal(95),
                Decimal(99),
                Decimal(90),
                Decimal(98),
            ),
        )
        is None
    )
    assert engine._max_drawdown((Decimal(100), Decimal(80), Decimal(90))) == Decimal(
        "-0.2"
    )


def test_resistance_sell_orders_remain_available_for_c3_armed_position() -> None:
    ticks = _sealed_tick_shape()
    engine = PortfolioEngine(ticks)
    sessions = [date(2014, 1, 1) + timedelta(days=index) for index in range(121)]
    position = Position(
        symbol="RESIST",
        quantity=10,
        average_price=Decimal(9000),
        invested_cost_basis=Decimal(90000),
        trim90_armed=True,
    )

    orders = engine._resistance_orders(
        symbol="RESIST",
        session=sessions[-1],
        history=_resistance_probe_bars(sessions),
        index=120,
        position=position,
        first_order_number=0,
    )

    assert [(order.rung, order.limit, order.quantity) for order in orders] == [
        ("R1", Decimal(10960), 5)
    ]


def test_day_order_expiry_returns_cash_and_c1_reservation() -> None:
    engine = PortfolioEngine(_sealed_tick_shape())
    session = date(2014, 1, 2)
    cash = CashLedger(Decimal(1000))
    assert cash.reserve_order("EXPIRY", Decimal(200))
    cycle = C1Cycle()
    assert cycle.reserve(notional=Decimal(200), is_add=False)[0]
    order = Order(
        order_id="EXPIRY",
        session=session,
        symbol="EXPIRY",
        side=OrderSide.BUY,
        order_class=OrderClass.NEW,
        limit=Decimal(200),
        quantity=1,
        rung="L1",
        rank=1,
    )
    state = RunState(pending_orders=[order])

    engine._expire_orders(
        state=state,
        cash=cash,
        c1_cycles=defaultdict(C1Cycle, {"EXPIRY": cycle}),
        arm=Arm.C1,
        session=session + timedelta(days=1),
    )

    assert cash.orderable_cash == Decimal(1000)
    assert cash.reserved_orders == {}
    assert state.pending_orders == []
    assert order.status is OrderStatus.EXPIRED
    assert cycle.reserved_buy_gross == 0


def test_global_rank_cap_is_enforced_by_engine_execution() -> None:
    ticks = _sealed_tick_shape()
    sessions = [date(2014, 1, 1) + timedelta(days=index) for index in range(201)]
    decision_sessions = sessions[-121:]

    result = PortfolioEngine(ticks).run(
        PortfolioRunInput(
            arm=Arm.B0,
            cashflow_view=CashflowView.NO_CONTRIBUTION,
            bars=_contract_signal_bars(
                decision_sessions,
                symbols=("000001", "000002", "000003", "000004"),
            ),
            market_sessions=tuple(sessions),
            decision_start=decision_sessions[-1],
        )
    )

    assert len(result.evidence["counterfactual_demand_pairs"]) == 3
    assert len(result.fills) == 6


def test_engine_binds_c3_armed_buy_suppression_policy() -> None:
    position = Position(symbol="C3", quantity=3, trim90_armed=True)

    assert engine_module.c3_buy_suppressed(position)
