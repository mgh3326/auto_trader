"""Executable adversarial probes for the 23 contract-listed mutants."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from research.kr_corpus.d3_engine.canonical import fixed, plain
from research.kr_corpus.d3_engine.cash import CashLedger
from research.kr_corpus.d3_engine.constants import DECIMAL_PRECISION
from research.kr_corpus.d3_engine.indicators import (
    OhlcPoint,
    bollinger_bands,
    fib_levels,
    rsi_wilder,
    scan_fib_window,
)
from research.kr_corpus.d3_engine.metrics import (
    deployment_mean,
    dual_view_result,
    locked_share_time_weighted_mean,
    nearest_rank,
    unserved_counterfactual_demand_sessions,
)
from research.kr_corpus.d3_engine.models import Position
from research.kr_corpus.d3_engine.policies import (
    C1Cycle,
    adjusted_simulation_quantity,
    c2_allows,
    update_c3_close,
)
from research.kr_corpus.d3_engine.signals import (
    LevelCluster,
    PriceLevel,
    build_buy_rungs,
    cluster_levels,
    signal_is_eligible,
)
from research.kr_corpus.d3_engine.tick import InvalidTickTable, TickTable


@dataclass(frozen=True, slots=True)
class MutantProbeResult:
    name: str
    vector_id: str
    correct: Any
    mutant: Any

    @property
    def differs(self) -> bool:
        return self.correct != self.mutant


def _load_input(root: Path, vector_id: str) -> dict[str, Any]:
    parsed = json.loads(
        (root / "vectors" / f"{vector_id}.json").read_text(encoding="utf-8")
    )
    value = parsed["input"]
    if not isinstance(value, dict):
        raise ValueError(f"{vector_id}: input must be object")
    return value


def run_mutant_probes(root: Path, ticks: TickTable) -> tuple[MutantProbeResult, ...]:
    probes = (
        _rsi_seed_ddof(root),
        _fib_direction(root),
        _fib_close(root),
        _l2_less_signal(root),
        _same_source_confluence(root),
        _l1_l2_priority(root, ticks),
        _tick_alignment(root, ticks),
        _c1_submitted_notional(root),
        _c3_prefill_average(root),
        _c3_armed_add(root),
        _c2_t_instead_of_t_minus_1(root),
        _t0_reuse(root),
        _t2_off_by_one(root),
        _payable_double_debit(root),
        _split_quantity(root),
        _p95_instead_of_p05(root),
        _p05_right_censored(root),
        _arm_local_eligibility(root),
        _clamp_only_winner(root),
        _locked_simple_mean(root),
        _deployment_absolute(root),
        _unserved_notional_primary(root),
        _tick_gap_overlap_missed(root),
    )
    if len(probes) != 23 or len({probe.name for probe in probes}) != 23:
        raise AssertionError("mutant probe count/name drift")
    return probes


def _rsi_seed_ddof(root: Path) -> MutantProbeResult:
    rsi_data = _load_input(root, "V001_rsi_wilder_seed")
    closes = [Decimal(value) for value in rsi_data["adjusted_closes"]]
    correct_rsi = rsi_wilder(closes)[15]
    assert correct_rsi is not None
    changes = [right - left for left, right in zip(closes, closes[1:], strict=False)]
    rolling = changes[-14:]
    gain = sum((max(value, Decimal(0)) for value in rolling), Decimal(0)) / 14
    loss = sum((max(-value, Decimal(0)) for value in rolling), Decimal(0)) / 14
    mutant_rsi = (
        Decimal(0) if gain == 0 else Decimal(100) - Decimal(100) / (1 + gain / loss)
    )

    bb_data = _load_input(root, "V002_bb_ddof0")
    values = [Decimal(value) for value in bb_data["adjusted_closes"]]
    correct_lower = bollinger_bands(values).lower
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        mean = sum(values, Decimal(0)) / len(values)
        sample_std = (
            sum((value - mean) ** 2 for value in values) / (len(values) - 1)
        ).sqrt()
        mutant_lower = mean - Decimal(2) * sample_std
    return MutantProbeResult(
        "rsi_seed_ddof",
        "V001+V002",
        {"rsi15": fixed(correct_rsi, 4), "bb_lower": fixed(correct_lower, 30)},
        {"rsi15": fixed(mutant_rsi, 4), "bb_lower": fixed(mutant_lower, 30)},
    )


def _fib_direction(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V003_fib_direction")
    low = Decimal(data["adjusted_low_min"])
    high = Decimal(data["adjusted_high_max"])
    correct = fib_levels(low, high)[Decimal("0.236")]
    mutant = high - Decimal("0.236") * (high - low)
    return MutantProbeResult(
        "fib_direction_reversed", "V003_fib_direction", plain(correct), plain(mutant)
    )


def _fib_close(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V033_fib_window_scan_pit")
    points = tuple(
        OhlcPoint(
            Decimal(item["adjusted_high"]),
            Decimal(item["adjusted_low"]),
            Decimal(item["adjusted_close"]),
        )
        for item in data["sessions"]
    )
    window = scan_fib_window(points, decision_index=int(data["decision_session_index"]))
    selected = points[window.start_index : window.end_index + 1]
    correct = fib_levels(window.low, window.high)[Decimal("0.236")]
    close_low = min(point.close for point in selected)
    close_high = max(point.close for point in selected)
    mutant = fib_levels(close_low, close_high)[Decimal("0.236")]
    return MutantProbeResult(
        "fib_close_as_extrema",
        "V033_fib_window_scan_pit",
        plain(correct),
        plain(mutant),
    )


def _l2_less_signal(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V004_l2_less_no_signal")
    close = Decimal(data["t_minus_1_close"])
    clusters = tuple(
        LevelCluster(
            members=tuple(
                PriceLevel(
                    Decimal(item["price"]),
                    str(source),
                    f"cluster-{cluster_index}-{source_index}",
                )
                for source_index, source in enumerate(item["sources"])
            ),
            representative=Decimal(item["price"]),
            distinct_sources=tuple(sorted(set(item["sources"]))),
        )
        for cluster_index, item in enumerate(data["support_clusters"])
    )
    rsi = Decimal(data["rsi"])
    correct = signal_is_eligible(rsi=rsi, clusters=clusters, close=close)
    mutant = rsi < Decimal("45") and bool(clusters)
    return MutantProbeResult(
        "l2_less_signal", "V004_l2_less_no_signal", correct, mutant
    )


def _same_source_confluence(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V031_fib_only_no_confluence")
    close = Decimal(data["t_minus_1_close"])
    levels = [
        PriceLevel(Decimal(item["price"]), item["source"], str(index))
        for index, item in enumerate(data["levels"])
    ]
    cluster = cluster_levels(levels, close=close)[0]
    mutant = len(cluster.members) >= 2
    return MutantProbeResult(
        "same_source_counts_as_confluence",
        "V031_fib_only_no_confluence",
        cluster.qualifies,
        mutant,
    )


def _l1_l2_priority(root: Path, ticks: TickTable) -> MutantProbeResult:
    data = _load_input(root, "V006_l1_l2_priority")
    correct = [
        name
        for name, _ in build_buy_rungs(
            close=Decimal(data["t_minus_1_close"]),
            l2_price=Decimal(data["qualifying_cluster_price"]),
            tick_table=ticks,
        )
    ]
    return MutantProbeResult(
        "l1_l2_priority_reversed",
        "V006_l1_l2_priority",
        correct,
        list(reversed(correct)),
    )


def _tick_alignment(root: Path, ticks: TickTable) -> MutantProbeResult:
    data = _load_input(root, "V007_tick_align_sell")
    raw = Decimal(
        next(
            item["raw"]
            for item in data["cases"]
            if item["raw"] == "9730.5" and item["side"] == "buy"
        )
    )
    return MutantProbeResult(
        "tick_alignment_omitted",
        "V007_tick_align_sell",
        plain(ticks.align_buy(raw)),
        plain(raw),
    )


def _c1_submitted_notional(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V009_c1_filled_notional_cap")
    correct_cycle = C1Cycle()
    mutant_consumed = Decimal(0)
    correct_s5 = False
    mutant_s5 = False
    for step in data["steps"]:
        amount = Decimal(step["notional"])
        admitted, _ = correct_cycle.reserve(notional=amount, is_add=False)
        mutant_admitted = mutant_consumed + amount <= Decimal(
            data["per_symbol_total_cap_gross"]
        )
        if step["session"] == "S5":
            correct_s5 = admitted
            mutant_s5 = mutant_admitted
        if admitted:
            if step["fill"]:
                correct_cycle.fill(notional=amount, is_add=False)
            else:
                correct_cycle.expire(amount, is_add=False)
        if mutant_admitted:
            mutant_consumed += amount
    return MutantProbeResult(
        "c1_submitted_notional_consumed",
        "V009_c1_filled_notional_cap",
        correct_s5,
        mutant_s5,
    )


def _c3_prefill_average(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V011_c3_trim_streak")
    item = next(row for row in data["timeline"] if row["session"] == "D_add")
    position = Position(
        symbol=data["symbol"],
        quantity=9,
        average_price=Decimal(item["pre_session_avg"]),
        invested_cost_basis=Decimal(item["pre_session_avg"]) * 9,
    )
    position.apply_buy(
        quantity=int(item["add_fill"]["qty"]),
        price=Decimal(item["add_fill"]["price"]),
        fee=Decimal(0),
        session_index=1,
    )
    close = Decimal(item["close_B"])
    correct = update_c3_close(position, close=close).underwater
    return MutantProbeResult(
        "c3_prefill_average",
        "V011_c3_trim_streak",
        correct,
        close < Decimal(item["pre_session_avg"]),
    )


def _c3_armed_add(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V011_c3_trim_streak")
    item = next(row for row in data["timeline"] if row["session"] == "D90")
    position = Position(
        symbol=data["symbol"],
        quantity=9,
        average_price=Decimal("10000"),
        underwater_streak=89,
    )
    update_c3_close(position, close=Decimal(item["close"]))
    correct_add_issued = not position.trim90_armed
    mutant_add_issued = bool(item["same_session_add_signal"])
    return MutantProbeResult(
        "c3_armed_session_add_issued",
        "V011_c3_trim_streak",
        correct_add_issued,
        mutant_add_issued,
    )


def _c2_t_instead_of_t_minus_1(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V010_c2_regime_sma200")
    item = next(row for row in data["cases"] if row["id"] == "t_close_trap")
    sma = Decimal(item["sma200_t_minus_1"])
    return MutantProbeResult(
        "c2_uses_t_close",
        "V010_c2_regime_sma200",
        c2_allows(t_minus_1_close=Decimal(item["t_minus_1_close"]), sma200=sma),
        c2_allows(t_minus_1_close=Decimal(item["index_close_t"]), sma200=sma),
    )


def _t0_reuse(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V012_t2_settle_payable")
    ledger = CashLedger(Decimal(data["initial_settled_cash"]))
    buy = Decimal(data["buy"]["gross"]) + Decimal(data["buy"]["fee"])
    sell = Decimal(data["sell"]["net"])
    ledger.fill_buy_immediate(amount=buy, trade_session_index=0)
    ledger.fill_sell(net_amount=sell, trade_session_index=1)
    return MutantProbeResult(
        "t0_sell_receivable_reuse",
        "V012_t2_settle_payable",
        plain(ledger.orderable_cash),
        plain(ledger.orderable_cash + sell),
    )


def _t2_off_by_one(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V012_t2_settle_payable")
    ledger = CashLedger(Decimal(data["initial_settled_cash"]))
    amount = Decimal(data["buy"]["gross"]) + Decimal(data["buy"]["fee"])
    payable = ledger.fill_buy_immediate(amount=amount, trade_session_index=0)
    return MutantProbeResult(
        "t2_settlement_off_by_one",
        "V012_t2_settle_payable",
        data["sessions"][payable.settle_session_index],
        data["sessions"][1],
    )


def _payable_double_debit(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V012_t2_settle_payable")
    amount = Decimal(data["buy"]["gross"]) + Decimal(data["buy"]["fee"])
    ledger = CashLedger(Decimal(data["initial_settled_cash"]))
    payable = ledger.fill_buy_immediate(amount=amount, trade_session_index=0)
    before = ledger.orderable_cash
    ledger.settle_pre_open(payable.settle_session_index)
    return MutantProbeResult(
        "payable_double_debit",
        "V012_t2_settle_payable",
        plain(ledger.orderable_cash - before),
        plain(-amount),
    )


def _split_quantity(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V021_split_adjusted_price_sim")
    quantity = int(data["qty"])
    return MutantProbeResult(
        "split_quantity_restatement_without_ledger",
        "V021_split_adjusted_price_sim",
        adjusted_simulation_quantity(quantity),
        quantity * 2,
    )


def _p95_instead_of_p05(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V019_funding_p05")
    values = [int(value) for value in data["complete_anchors_days"]]
    return MutantProbeResult(
        "p95_instead_of_p05",
        "V019_funding_p05",
        nearest_rank(values, Decimal("0.05")),
        nearest_rank(values, Decimal("0.95")),
    )


def _p05_right_censored(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V019_funding_p05")
    complete = [int(value) for value in data["complete_anchors_days"]]
    contaminated = complete + [int(value) for value in data["right_censored_exclude"]]
    return MutantProbeResult(
        "p05_includes_right_censored",
        "V019_funding_p05",
        nearest_rank(complete, Decimal("0.05")),
        nearest_rank(contaminated, Decimal("0.05")),
    )


def _arm_local_eligibility(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V016_counterfactual_demand_gaming")
    labels = dict.fromkeys(
        data["b0_demand_signals"],
        "policy_rejected" if data["c2_regime_block"] else "filled",
    )
    rejected_sessions = ["S1" for label in labels.values() if label != "filled"]
    correct = unserved_counterfactual_demand_sessions(rejected_sessions)
    mutant = 0 if data["c2_regime_block"] else correct
    return MutantProbeResult(
        "arm_local_eligibility_gaming",
        "V016_counterfactual_demand_gaming",
        correct,
        mutant,
    )


def _clamp_only_winner(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V022_dual_view_consistency")
    original = data["original_valid_bar"]
    clamp = data["clamp_admit_v1"]
    correct = dual_view_result(
        original_verdicts=original["verdicts"],
        original_hard_guards={},
        original_winner=original["winner_id"],
        clamp_verdicts=clamp["verdicts"],
        clamp_hard_guards={},
        clamp_winner=clamp["winner_id"],
    )
    mutant = data["clamp_admit_v1"]["winner_id"]
    return MutantProbeResult(
        "clamp_only_winner", "V022_dual_view_consistency", correct, mutant
    )


def _locked_simple_mean(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V018_locked_share_tw")
    values = [Decimal(value) for value in data["daily_ratios"]]
    nonzero = [value for value in values if value]
    return MutantProbeResult(
        "locked_share_simple_nonzero_mean",
        "V018_locked_share_tw",
        plain(locked_share_time_weighted_mean(values)),
        plain(sum(nonzero, Decimal(0)) / len(nonzero)),
    )


def _deployment_absolute(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V020_deployment_formula")
    invested = [Decimal(value) for value in data["daily_invested_cost"]]
    contributions = [Decimal(value) for value in data["cum_contrib"]]
    initial = Decimal(data["initial_cash"])
    correct, _ = deployment_mean(
        daily_invested_cost=invested,
        cumulative_contribution=contributions,
        initial_cash=initial,
    )
    mutant = sum(invested, Decimal(0)) / len(invested) / initial
    return MutantProbeResult(
        "deployment_absolute_denominator",
        "V020_deployment_formula",
        fixed(correct, 20),
        plain(mutant),
    )


def _unserved_notional_primary(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V015_cash_exhaustion")
    notional = sum(
        (Decimal(item["notional"]) for item in data["b0_demand_orders_same_session"]),
        Decimal(0),
    )
    return MutantProbeResult(
        "unserved_notional_as_primary",
        "V015_cash_exhaustion",
        unserved_counterfactual_demand_sessions(
            ["S1"] if data["b0_demand_orders_same_session"] else []
        ),
        plain(notional),
    )


def _tick_gap_overlap_missed(root: Path) -> MutantProbeResult:
    data = _load_input(root, "V008_tick_table_validation")
    correct: dict[str, str] = {}
    for name in ("gap", "overlap"):
        try:
            TickTable.from_mapping(data["mutated_tables"][name])
        except InvalidTickTable:
            correct[name] = "RUN_INVALID_TICK_TABLE"
        else:
            correct[name] = "VALID"
    mutant = {"gap": "VALID", "overlap": "VALID"}
    return MutantProbeResult(
        "tick_gap_overlap_not_detected",
        "V008_tick_table_validation",
        correct,
        mutant,
    )
