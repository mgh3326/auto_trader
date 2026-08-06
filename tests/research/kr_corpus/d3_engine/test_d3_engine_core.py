from __future__ import annotations

import ast
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from research.kr_corpus.d3_engine.cash import CashLedger
from research.kr_corpus.d3_engine.engine import PortfolioEngine
from research.kr_corpus.d3_engine.guards import (
    SealedAccessBlocked,
    SealedAccessGuard,
    SealedAccessSpy,
)
from research.kr_corpus.d3_engine.indicators import OhlcPoint, scan_fib_window
from research.kr_corpus.d3_engine.models import (
    Arm,
    Bar,
    CashflowView,
    PortfolioRunInput,
    Position,
)
from research.kr_corpus.d3_engine.policies import (
    C1Cycle,
    c3_buy_suppressed,
    update_c3_close,
)
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


def test_c2_fails_closed_when_prior_xkrx_index_row_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = _sealed_tick_shape()
    sessions = [date(2014, 1, 1) + timedelta(days=index) for index in range(201)]
    bars = tuple(
        Bar(
            session=session,
            symbol="005930",
            open=Decimal("9600") if index == 120 else Decimal("10000"),
            high=Decimal("10100"),
            low=Decimal("9400") if index == 120 else Decimal("9900"),
            close=Decimal("9900") if index == 120 else Decimal("10000"),
        )
        for index, session in enumerate(sessions[-121:])
    )
    engine = PortfolioEngine(ticks)
    monkeypatch.setattr(
        engine,
        "_signal_for_session",
        lambda _history, _index: (
            Decimal("40"),
            Decimal("9500"),
            Decimal("12000"),
            Decimal("8000"),
        ),
    )
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
def test_all_four_arms_execute_contract_signal(
    arm: Arm, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = _sealed_tick_shape()
    sessions = [date(2014, 1, 1) + timedelta(days=index) for index in range(201)]
    bar_sessions = sessions[-121:]
    bars = tuple(
        Bar(
            session=session,
            symbol="005930",
            open=Decimal("9600") if index == 120 else Decimal("10000"),
            high=Decimal("10100"),
            low=Decimal("9400") if index == 120 else Decimal("9900"),
            close=Decimal("9900") if index == 120 else Decimal("10000"),
        )
        for index, session in enumerate(bar_sessions)
    )
    engine = PortfolioEngine(ticks)
    monkeypatch.setattr(
        engine,
        "_signal_for_session",
        lambda _history, _index: (
            Decimal("40"),
            Decimal("9500"),
            Decimal("12000"),
            Decimal("8000"),
        ),
    )
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
        Decimal("9500"),
    ]
    assert result.evidence["sealed_access_spy"] == 0
    assert result.evidence["primary_run_executed"] is False
