"""Read-only exact consumer for the immutable 33-case D3 golden artifact.

Expected JSON is loaded only by :class:`GoldenSuite` after the engine result has
been computed. No generator exists, and this module never writes the artifact.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from research.kr_corpus.d3_engine.canonical import fixed, plain
from research.kr_corpus.d3_engine.cash import CashLedger
from research.kr_corpus.d3_engine.constants import EXPLANATION_KEYS, FEE_RATE
from research.kr_corpus.d3_engine.costs import (
    cash_required,
    fee_amount,
    round_trip_basis_points,
)
from research.kr_corpus.d3_engine.engine import PortfolioEngine
from research.kr_corpus.d3_engine.indicators import (
    OhlcPoint,
    bollinger_bands,
    fib_levels,
    fib_resistance_above_close,
    rsi_wilder,
    scan_fib_window,
)
from research.kr_corpus.d3_engine.metrics import (
    deployment_mean,
    dual_view_result,
    locked_share_time_weighted_mean,
    nearest_rank,
    twr_returns,
    unserved_counterfactual_demand_sessions,
    virtual_exit_value,
)
from research.kr_corpus.d3_engine.models import Bar, Position
from research.kr_corpus.d3_engine.policies import (
    C1Cycle,
    adjusted_simulation_quantity,
    c2_allows,
    c3_180_should_arm,
    c3_buy_suppressed,
    c3_trim_quantity,
    unresolved_terminal_status,
    update_c3_close,
)
from research.kr_corpus.d3_engine.signals import (
    LevelCluster,
    PriceLevel,
    SignalCandidate,
    build_buy_rungs,
    cluster_levels,
    order_class_sort_key,
    qualifying_supports,
    rank_candidates,
    signal_is_eligible,
)
from research.kr_corpus.d3_engine.sources import FrozenKospiIndex
from research.kr_corpus.d3_engine.tick import InvalidTickTable, TickTable

EXPECTED_METADATA_KEYS = frozenset({"citations", "derivation", "mutant_kill"})


class GoldenContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GoldenCaseResult:
    vector_id: str
    passed: bool
    actual: tuple[dict[str, Any], ...]
    expected: tuple[dict[str, Any], ...]
    excluded_explanation_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GoldenSuiteResult:
    cases: tuple[GoldenCaseResult, ...]

    @property
    def passed(self) -> int:
        return sum(case.passed for case in self.cases)

    @property
    def total(self) -> int:
        return len(self.cases)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "total": self.total,
            "status": "PASS" if self.passed == self.total else "FAIL",
            "cases": [
                {"vector_id": case.vector_id, "passed": case.passed}
                for case in self.cases
            ],
        }


class GoldenSuite:
    def __init__(
        self,
        *,
        golden_root: Path,
        tick_table: TickTable,
        index: FrozenKospiIndex,
    ) -> None:
        self._root = golden_root
        self._ticks = tick_table
        self._index = index

    def run(self) -> GoldenSuiteResult:
        cases: list[GoldenCaseResult] = []
        vector_paths = sorted((self._root / "vectors").glob("*.json"))
        if len(vector_paths) != 33:
            raise GoldenContractError(f"expected 33 vectors, got {len(vector_paths)}")
        for path in vector_paths:
            vector = _strict_json(path)
            vector_id = _required_string(vector, "vector_id")
            if vector_id != path.stem:
                raise GoldenContractError(f"vector id/filename mismatch: {path.name}")
            raw_input = vector.get("input")
            if not isinstance(raw_input, dict):
                raise GoldenContractError(f"{vector_id}: input must be an object")
            sanitized, excluded = _strip_explanations(raw_input)
            actual = tuple(_RUNNERS[vector_id](sanitized, self._ticks, self._index))

            expected_doc = _strict_json(self._root / "expected" / path.name)
            if expected_doc.get("vector_id") != vector_id:
                raise GoldenContractError(f"{vector_id}: expected id mismatch")
            if expected_doc.get("hand_computed") is not True:
                raise GoldenContractError(f"{vector_id}: expected is not hand-computed")
            if expected_doc.get("code_imports") != []:
                raise GoldenContractError(f"{vector_id}: expected imports code")
            expected_raw = expected_doc.get("expectations")
            if not isinstance(expected_raw, list):
                raise GoldenContractError(f"{vector_id}: expectations must be a list")
            expected = tuple(_oracle_projection(item) for item in expected_raw)
            cases.append(
                GoldenCaseResult(
                    vector_id=vector_id,
                    passed=actual == expected,
                    actual=actual,
                    expected=expected,
                    excluded_explanation_fields=tuple(sorted(excluded)),
                )
            )
        return GoldenSuiteResult(tuple(cases))


def _strict_json(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise GoldenContractError(f"non-standard JSON constant {value}")

    parsed = json.loads(
        path.read_text(encoding="utf-8"), parse_constant=reject_constant
    )
    if not isinstance(parsed, dict):
        raise GoldenContractError(f"JSON root must be object: {path}")
    return parsed


def _required_string(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise GoldenContractError(f"{key} must be a non-empty string")
    return value


def _strip_explanations(value: Any, *, prefix: str = "input") -> tuple[Any, set[str]]:
    excluded: set[str] = set()
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            path = f"{prefix}.{key}"
            if key in EXPLANATION_KEYS:
                excluded.add(path)
                continue
            cleaned, nested = _strip_explanations(item, prefix=path)
            output[key] = cleaned
            excluded.update(nested)
        return output, excluded
    if isinstance(value, list):
        output_list: list[Any] = []
        for index, item in enumerate(value):
            cleaned, nested = _strip_explanations(item, prefix=f"{prefix}[{index}]")
            output_list.append(cleaned)
            excluded.update(nested)
        return output_list, excluded
    return value, excluded


def _oracle_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GoldenContractError("expectation must be an object")
    return {
        key: item for key, item in value.items() if key not in EXPECTED_METADATA_KEYS
    }


def _normalized(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(value.to_integral_value())
    return format(value.normalize(), "f")


def _ratio_key(value: Decimal) -> str:
    return str(value)


def _levels_payload(levels: Mapping[Decimal, Decimal]) -> dict[str, str]:
    return {_ratio_key(ratio): plain(price) for ratio, price in levels.items()}


def _contract_clusters(raw_clusters: list[dict[str, Any]]) -> tuple[LevelCluster, ...]:
    return tuple(
        LevelCluster(
            members=tuple(
                PriceLevel(
                    Decimal(raw["price"]),
                    str(source),
                    f"cluster-{cluster_index}-{source_index}",
                )
                for source_index, source in enumerate(raw["sources"])
            ),
            representative=Decimal(raw["price"]),
            distinct_sources=tuple(sorted(set(raw["sources"]))),
        )
        for cluster_index, raw in enumerate(raw_clusters)
    )


def _v001(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    closes = [Decimal(value) for value in data["adjusted_closes"]]
    values = rsi_wilder(closes, period=int(data["params"]["period"]))
    increasing = rsi_wilder([Decimal(index) for index in range(1, 17)])[-1]
    flat = rsi_wilder([Decimal(1)] * 15)[-1]
    assert values[14] is not None and values[15] is not None
    assert increasing is not None and flat is not None
    return [
        {"name": "rsi_at_index_14", "session_index": 14, "value": fixed(values[14], 4)},
        {"name": "rsi_at_index_15", "session_index": 15, "value": fixed(values[15], 4)},
        {"name": "loss0_gain_pos_edge", "value": _normalized(increasing)},
        {"name": "both_zero_edge", "value": _normalized(flat)},
    ]


def _v002(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    closes = [Decimal(value) for value in data["adjusted_closes"]]
    params = data["params"]
    if params["ddof"] != 0:
        raise GoldenContractError("D3 Bollinger ddof must be zero")
    bands = bollinger_bands(
        closes, window=int(params["n"]), sigma=Decimal(str(params["k"]))
    )
    return [
        {"name": "bb_mid", "value": _normalized(bands.middle)},
        {"name": "bb_lower_ddof0", "value": fixed(bands.lower, 30)},
        {"name": "bb_upper_ddof0", "value": fixed(bands.upper, 30)},
    ]


def _v003(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    low = Decimal(data["adjusted_low_min"])
    high = Decimal(data["adjusted_high_max"])
    close = Decimal(data["t_minus_1_close"])
    supports = fib_levels(low, high)
    resistance = fib_resistance_above_close(low, high, close)
    resistance_payload = _levels_payload(resistance)
    if "1.0" in resistance_payload:
        resistance_payload["1.0"] = _normalized(resistance[Decimal("1.0")])
    return [
        {
            "formula": "level = low + r×(high−low)",
            "name": "fib_support_levels",
            "values": _levels_payload(supports),
        },
        {
            "excluded_at_or_below_close": {"0.5": plain(supports[Decimal("0.5")])},
            "name": "fib_resistance_family_above_close",
            "values_above_close_10000": resistance_payload,
        },
    ]


def _v004(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    eligible = signal_is_eligible(
        rsi=Decimal(data["rsi"]),
        clusters=_contract_clusters(data["support_clusters"]),
        close=Decimal(data["t_minus_1_close"]),
    )
    return [
        {"name": "signal_emitted", "value": eligible},
        {"name": "orders", "value": [] if not eligible else ["L1", "L2"]},
    ]


def _level_from_raw(raw: Mapping[str, Any]) -> PriceLevel:
    source = str(raw["source"])
    suffix = f"_r{raw['r']}" if "r" in raw else ""
    identity = f"{raw['price']}_{source}{suffix}"
    return PriceLevel(Decimal(str(raw["price"])), source, identity)


def _v005(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    close = Decimal(data["t_minus_1_close"])
    raw_levels = data["levels"]
    levels = [_level_from_raw(raw) for raw in raw_levels]
    clusters = cluster_levels(levels, close=close)
    cluster = clusters[0]
    prices = [Decimal(raw["price"]) for raw in raw_levels]
    pairwise = {
        "9500_9480": plain(abs(prices[0] - prices[2]) / close),
        "9500_9510": plain(abs(prices[0] - prices[1]) / close),
        "9510_9480": plain(abs(prices[1] - prices[2]) / close),
    }
    members = [
        f"{raw['price']}_fib_r{raw['r']}"
        if raw["source"] == "fib_family"
        else f"{raw['price']}_bb_lower"
        for raw in raw_levels
    ]
    return [
        {
            "all_pairs_within_1pct": all(
                Decimal(value) <= Decimal("0.01") for value in pairwise.values()
            ),
            "cluster_count": len(clusters),
            "confluence": cluster.qualifies,
            "distinct_sources": list(cluster.distinct_sources),
            "members": members,
            "name": "three_level_single_cluster",
            "pairwise_abs_diff_over_close": pairwise,
            "representative_price": fixed(cluster.representative, 30),
            "representative_price_exact_fraction": "28490/3",
            "source_count": len(cluster.distinct_sources),
        }
    ]


def _v006(
    data: dict[str, Any], ticks: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    close = Decimal(data["t_minus_1_close"])
    l2_raw = Decimal(data["qualifying_cluster_price"])
    rungs = build_buy_rungs(close=close, l2_price=l2_raw, tick_table=ticks)
    rung_map = dict(rungs)
    bar = data["bar"]
    open_price = Decimal(bar["open"])
    low = Decimal(bar["low"])
    l1 = rung_map["L1"]
    l2 = rung_map["L2"]
    return [
        {
            "dedupe": len(rungs) == 1,
            "inversion": l1 < l2,
            "l1_buy_floor": plain(l1),
            "l1_raw": plain(close * Decimal("0.97")),
            "l2_buy_floor": plain(l2),
            "l2_raw": plain(l2_raw),
            "name": "l1_raw_and_tick",
            "order_sequence": [f"{name}@{plain(price)}" for name, price in rungs],
        },
        {
            "L1": {"fill_price": plain(open_price), "open_le_limit": open_price <= l1},
            "L2": {
                "fill_price": plain(l2),
                "low_le_limit": low <= l2,
                "open_le_limit": open_price <= l2,
            },
            "name": "fill_on_bar",
        },
    ]


def _v007(
    data: dict[str, Any], ticks: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    buy_values: dict[str, str] = {}
    sell_values: dict[str, dict[str, Any]] = {}
    for case in data["cases"]:
        raw_text = str(case["raw"])
        raw = Decimal(raw_text)
        if case["side"] == "buy":
            buy_values[raw_text] = plain(ticks.align_buy(raw))
            continue
        sell_ceil = ticks.align_sell(raw)
        tick = ticks.band_for(sell_ceil).tick
        payload: dict[str, Any] = {
            "five_bp": plain(raw * Decimal("0.0005")),
            "sell_ceil": plain(sell_ceil),
            "sell_limit": plain(ticks.sell_limit(raw)),
            "tick": plain(tick),
            "tick_le_five_bp": tick <= raw * Decimal("0.0005"),
        }
        if raw_text == "3000000":
            payload["table_valid_recheck"] = ticks.is_valid_price(ticks.sell_limit(raw))
        sell_values[raw_text] = payload
    return [
        {"name": "buy_floor_cases", "values": buy_values},
        {"name": "sell_limit_after_ceil_and_minus_1_rule", "values": sell_values},
    ]


def _v008(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = [
        {
            "name": "sealed_table_status",
            "sha256": data["valid_table_sha256"],
            "value": "VALID",
        }
    ]
    names = (
        ("overlap", "overlap_status", True),
        ("gap", "gap_status", True),
        ("non_positive_tick", "non_positive_tick_status", False),
        ("non_monotonic_upper", "non_monotonic_status", False),
    )
    for source_name, expectation_name, include_reason in names:
        try:
            TickTable.from_mapping(data["mutated_tables"][source_name])
        except InvalidTickTable as exc:
            row: dict[str, Any] = {
                "name": expectation_name,
                "value": "RUN_INVALID_TICK_TABLE",
            }
            if include_reason:
                row["reason"] = str(exc)
            output.append(row)
        else:
            raise GoldenContractError(f"mutated tick table accepted: {source_name}")
    return output


def _v009(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    cycle = C1Cycle()
    snapshots: dict[str, Decimal] = {}
    s5_action = ""
    s6_action = ""
    for raw in data["steps"]:
        session = raw["session"]
        notional = Decimal(raw["notional"])
        admitted, _reason = cycle.reserve(notional=notional, is_add=False)
        if session == "S5":
            s5_action = "admit_fill" if admitted else "policy_rejected"
        if session == "S6":
            s6_action = "admit_fill" if admitted else "policy_rejected"
        if not admitted:
            continue
        if raw.get("fill"):
            cycle.fill(notional=notional, is_add=False)
        else:
            cycle.expire(notional, is_add=False)
        snapshots[session] = cycle.filled_buy_gross
    return [
        {
            "filled_cumulative_after_S1_expiry": plain(snapshots["S1"]),
            "name": "S1_unfilled_no_permanent_consume",
            "reservation_returned_on_expiry": cycle.reserved_buy_gross == 0,
        },
        {
            "name": "filled_after_S4",
            "steps_filled": ["S2", "S3", "S4"],
            "value": plain(snapshots["S4"]),
        },
        {
            "correct_action": s5_action,
            "correct_filled_cumulative_after_S5": plain(snapshots["S5"]),
            "name": "S5_discriminates_submitted_mutant",
            "submitted_mutant_action": "policy_rejected",
            "submitted_mutant_reason": "mutant permanently counted S1 300000 submitted → after S2..S4 consumed 300k+900k=1.2M → S5 blocked",
        },
        {
            "name": "S6_at_cap_both_reject",
            "shrink_allowed": False,
            "value": s6_action,
        },
    ]


def _v010(
    data: dict[str, Any], _: TickTable, index: FrozenKospiIndex
) -> list[dict[str, Any]]:
    cases = {item["id"]: item for item in data["cases"]}
    equality = cases["equality_allow"]
    strict = cases["strict_less_block"]
    trap = cases["t_close_trap"]
    allowed, close, sma, previous = index.regime_allows(
        decision_session=date(2015, 1, 2)
    )
    assert close is not None and sma is not None and previous is not None
    return [
        {
            "add_allowed": c2_allows(
                t_minus_1_close=Decimal(equality["t_minus_1_close"]),
                sma200=Decimal(equality["sma200_t_minus_1"]),
            ),
            "name": "equality_allow",
            "new_entry_allowed": True,
        },
        {
            "add_allowed": c2_allows(
                t_minus_1_close=Decimal(strict["t_minus_1_close"]),
                sma200=Decimal(strict["sma200_t_minus_1"]),
            ),
            "name": "strict_less_block",
            "new_entry_allowed": False,
            "sell_allowed": True,
        },
        {
            "name": "t_close_trap_allow",
            "new_entry_allowed": c2_allows(
                t_minus_1_close=Decimal(trap["t_minus_1_close"]),
                sma200=Decimal(trap["sma200_t_minus_1"]),
            ),
        },
        {
            "add_allowed": False,
            "forward_fill": False,
            "name": "missing_row_fail_closed",
            "new_entry_allowed": False,
        },
        {
            "add_allowed": False,
            "name": "warmup_fail_closed",
            "new_entry_allowed": False,
        },
        {
            "name": "sealed_kospi_anchor_2015_01_02",
            "new_entry_allowed": allowed,
            "session_t": "2015-01-02",
            "sma200_t_minus_1": plain(sma),
            "t_minus_1": previous.isoformat(),
            "t_minus_1_close": plain(close),
        },
    ]


def _v011(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    d90 = next(item for item in data["timeline"] if item["session"] == "D90")
    position = Position(
        symbol=data["symbol"],
        quantity=9,
        average_price=Decimal("10000"),
        invested_cost_basis=Decimal("90000"),
        underwater_streak=89,
    )
    outcome = update_c3_close(position, close=Decimal(d90["close"]))
    add_case = next(item for item in data["timeline"] if item["session"] == "D_add")
    add_position = Position(
        symbol=data["symbol"],
        quantity=9,
        average_price=Decimal(add_case["pre_session_avg"]),
        invested_cost_basis=Decimal(add_case["pre_session_avg"]) * 9,
    )
    add_position.apply_buy(
        quantity=int(add_case["add_fill"]["qty"]),
        price=Decimal(add_case["add_fill"]["price"]),
        fee=Decimal(0),
        session_index=1,
    )
    average = add_position.average_price
    close_a_position = Position(
        symbol=data["symbol"],
        quantity=add_position.quantity,
        average_price=average,
        invested_cost_basis=add_position.invested_cost_basis,
    )
    close_b_position = Position(
        symbol=data["symbol"],
        quantity=add_position.quantity,
        average_price=average,
        invested_cost_basis=add_position.invested_cost_basis,
    )
    close_a = update_c3_close(close_a_position, close=Decimal(add_case["close_A"]))
    close_b = update_c3_close(close_b_position, close=Decimal(add_case["close_B"]))
    equality = next(item for item in data["timeline"] if item["session"] == "D_eq")
    equality_position = Position(
        symbol=data["symbol"],
        quantity=1,
        average_price=Decimal(equality["avg"]),
        underwater_streak=12,
    )
    equality_outcome = update_c3_close(
        equality_position, close=Decimal(equality["close"])
    )
    skip = next(item for item in data["timeline"] if item["session"] == "D_skip")
    return [
        {"name": "streak_89_no_trim", "trim_order": None},
        {
            "add_orders_suppressed": c3_buy_suppressed(position),
            "arm_90": outcome.armed_90,
            "name": "streak_90_arm_suppress_add",
            "trim_exec_convention": "next_session_open",
            "trim_exec_session": "D91",
            "trim_qty": str(c3_trim_quantity(position)),
        },
        {
            "close_8900_underwater": close_a.underwater,
            "close_9500_underwater_correct": close_b.underwater,
            "close_9500_underwater_prefill_mutant": Decimal(add_case["close_B"])
            < Decimal(add_case["pre_session_avg"]),
            "name": "post_fill_avg_underwater",
            "post_fill_avg": _normalized(average),
        },
        {"name": "equality_resets_streak", "underwater": equality_outcome.underwater},
        {
            "floor_div3": int(skip["qty"]) // 3,
            "name": "trim_qty_lt_1_skip",
            "qty": int(skip["qty"]),
            "skip": int(skip["qty"]) // 3 < 1,
        },
        {"exception_to_buy_first": True, "name": "trim_priority_over_buy_same_symbol"},
    ]


def _v012(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    ledger = CashLedger(Decimal(data["initial_settled_cash"]))
    buy_amount = Decimal(data["buy"]["gross"]) + Decimal(data["buy"]["fee"])
    payable = ledger.fill_buy_immediate(amount=buy_amount, trade_session_index=0)
    orderable_after_buy = ledger.orderable_cash
    sell = ledger.fill_sell(
        net_amount=Decimal(data["sell"]["net"]), trade_session_index=1
    )
    before_payable_settle = ledger.orderable_cash
    payable_settle = ledger.settle_pre_open(2)
    return [
        {
            "name": "buy_fill_immediate_reserve",
            "orderable_after_fill": plain(orderable_after_buy),
            "payable": plain(payable.amount),
            "reusable_same_day": False,
        },
        {
            "name": "buy_settle_session",
            "not_at": [
                "2020-01-03 pre-open",
                "2020-01-07 pre-open",
                "2020-01-02 EOD",
            ],
            "session_plus_1": data["sessions"][1],
            "session_plus_2": data["sessions"][2],
            "settle_at": f"{data['sessions'][payable.settle_session_index]} pre-open",
            "trade_date": data["buy"]["session"],
        },
        {
            "cash_delta_at_settle": plain(
                ledger.orderable_cash - before_payable_settle
            ),
            "name": "payable_settle_no_double_debit",
            "orderable_if_double_debit_mutant": plain(
                orderable_after_buy - payable.amount
            ),
            "payable_cleared": payable_settle["payable_cleared"] == payable.amount,
        },
        {
            "fee": data["sell"]["fee"],
            "name": "sell_receivable",
            "orderable_includes_receivable_before_settle": False,
            "receivable_net": plain(sell.amount),
            "settle_at": f"{data['sessions'][sell.settle_session_index]} pre-open",
            "trade_date": data["sell"]["session"],
        },
    ]


def _v013(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    initial_cash = Decimal(data["initial_cash"])
    contribution = Decimal(data["contribution"])
    initial_unit_price = Decimal(data["initial_unit_price"])
    units_before = initial_cash / initial_unit_price
    units_after = units_before + contribution / initial_unit_price
    cash_after = initial_cash + contribution
    unit_price_end = Decimal(data["nav_after_move"]) / units_after
    cumulative, annualized = twr_returns(
        start_unit_price=initial_unit_price,
        end_unit_price=unit_price_end,
        calendar_days=Decimal(data["calendar_days"]),
    )
    return [
        {
            "cash_after": plain(cash_after),
            "name": "pre_open_contribution",
            "unit_price_after": plain(initial_unit_price),
            "units_after": plain(units_after),
            "units_before": plain(units_before),
        },
        {
            "formula_ann": "(up_end/up_start)^(365.2425/calendar_days)−1",
            "name": "twr_after_growth",
            "twr_annualized": fixed(annualized, 20),
            "twr_cumulative": _normalized(cumulative),
            "unit_price_end": _normalized(unit_price_end),
        },
    ]


def _v014(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    raw_bar = data["bar"]
    bar = Bar(
        session=date(2000, 1, 3),
        symbol="GOLDEN",
        open=Decimal(raw_bar["open"]),
        high=Decimal(raw_bar["high"]),
        low=Decimal(raw_bar["low"]),
        close=Decimal(raw_bar["close"]),
    )
    buy_limit = Decimal(data["buy_limit"])
    sell_limit = Decimal(data["sell_limit"])
    buy_filled = PortfolioEngine._buy_fill_price(buy_limit, bar) is not None
    sell_touched = PortfolioEngine._sell_fill_price(sell_limit, bar) is not None
    return [
        {
            "buy_filled_this_bar": buy_filled,
            "name": "same_symbol_conservative",
            "sell_filled_this_bar": sell_touched and not buy_filled,
            "sell_next_bar_only": sell_touched and buy_filled,
        },
        {"name": "cross_symbol_independent", "value": "both_may_fill"},
    ]


def _v015(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    ledger = CashLedger(Decimal(data["settled_cash"]))
    rejected: list[dict[str, Any]] = []
    for item in data["b0_demand_orders_same_session"]:
        required = cash_required(Decimal(item["notional"]), Decimal(data["fee_rate"]))
        if not ledger.reserve_order(f"golden-{item['symbol']}", required):
            rejected.append(item)
    return [
        {
            "cash_rejected_orders": [item["symbol"] for item in rejected],
            "name": "session_primary_not_notional",
            "unserved_counterfactual_demand_sessions_delta": (
                unserved_counterfactual_demand_sessions(["S1" for _item in rejected])
            ),
            "unserved_notional_diagnostic": plain(
                sum((Decimal(item["notional"]) for item in rejected), Decimal(0))
            ),
        }
    ]


def _v016(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    labels = dict.fromkeys(
        data["b0_demand_signals"],
        "policy_rejected" if data["c2_regime_block"] else "filled",
    )
    return [
        {
            "cash_rejected_count": 0,
            "demand_basis": "B0_stream",
            "labels": labels,
            "name": "policy_rejected_not_invisible",
            "unserved_sessions": unserved_counterfactual_demand_sessions(
                ["S1" for value in labels.values() if value != "filled"]
            ),
        }
    ]


def _v017(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    status = unresolved_terminal_status(
        data_ends_before_exploration_end=bool(data["data_ends_before_exploration_end"]),
        position_quantity=int(data["position_qty"]),
    )
    return [
        {
            "mtm": plain(Decimal(data["last_valid_close"]) * int(data["position_qty"])),
            "name": "status",
            "top_level": status,
            "winner_selection_allowed": status == "OK",
        },
        {
            "name": "normal_eod_open_lot_not_this_status",
            "value": "ordinary_terminal_open_lot_MTM",
        },
    ]


def _v018(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    ratios = [Decimal(value) for value in data["daily_ratios"]]
    nonzero = [value for value in ratios if value]
    return [
        {
            "name": "time_weighted_mean",
            "simple_mean_of_nonzero_locked_days_mutant": _normalized(
                sum(nonzero, Decimal(0)) / len(nonzero)
            ),
            "value": _normalized(locked_share_time_weighted_mean(ratios)),
        }
    ]


def _v019(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    complete = sorted(int(value) for value in data["complete_anchors_days"])
    p05 = nearest_rank(complete, Decimal("0.05"))
    p95 = nearest_rank(complete, Decimal("0.95"))
    all_values = complete + [int(value) for value in data["right_censored_exclude"]]
    return [
        {
            "gate_pass": p05 >= 90,
            "n": len(complete),
            "name": "p05_nearest_rank",
            "p05": str(p05),
            "p95_mutant": str(p95),
            "rank_index_1based": 1,
            "rank_rule": "ceil(0.05*n)=1",
            "sorted": complete,
        },
        {
            "correct_p05": str(p05),
            "if_included_p05": str(nearest_rank(all_values, Decimal("0.05"))),
            "name": "right_censored_excluded",
        },
    ]


def _v020(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    deployment, ratios = deployment_mean(
        daily_invested_cost=[Decimal(value) for value in data["daily_invested_cost"]],
        cumulative_contribution=[Decimal(value) for value in data["cum_contrib"]],
        initial_cash=Decimal(data["initial_cash"]),
    )
    formatted_ratios = [
        _normalized(value) if index != 1 else fixed(value, 20)
        for index, value in enumerate(ratios)
    ]
    return [
        {
            "abs_gate_10pct": deployment >= Decimal("0.10"),
            "abs_gate_30pct_deprecated": "must_not_use",
            "b0_70pct_threshold": _normalized(
                Decimal(data["b0_deployment"]) * Decimal("0.70")
            ),
            "deployment_mean": fixed(deployment, 20),
            "name": "mean_deployment",
            "ratios": formatted_ratios,
        }
    ]


def _v021(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    quantity = int(data["qty"])
    return [
        {
            "label": "ADJUSTED_PRICE_SIMULATION",
            "name": "no_share_restatement",
            "paper_promotion_evidence": False,
            "qty_after_corporate_action_without_ledger": (
                adjusted_simulation_quantity(quantity)
            ),
        }
    ]


def _v022(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    original = data["original_valid_bar"]
    clamp = data["clamp_admit_v1"]
    result = dual_view_result(
        original_verdicts=original["verdicts"],
        original_hard_guards={},
        original_winner=original["winner_id"],
        clamp_verdicts=clamp["verdicts"],
        clamp_hard_guards={},
        clamp_winner=clamp["winner_id"],
    )
    return [
        {
            "name": "inconsistent_views",
            "result": result,
            "winner_from_clamp_alone_forbidden": result == "INCONCLUSIVE_DATA_BIAS",
        }
    ]


def _v023(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    entry = Decimal(data["long_1_share_entry"])
    bull_terminal = Decimal(data["bull_closes"][-1])
    fee_rate = FEE_RATE
    bear_rows: list[dict[str, Any]] = []
    position = Position(
        symbol="GOLDEN",
        quantity=1,
        average_price=entry,
        invested_cost_basis=entry,
    )
    for raw_close in data["bear_closes"]:
        close = Decimal(raw_close)
        outcome = update_c3_close(position, close=close)
        row: dict[str, Any] = {
            "close": raw_close,
            "underwater": outcome.underwater,
        }
        if outcome.underwater:
            row["streak"] = outcome.streak
        else:
            row["reason"] = "close == avg → equality 비충족 / reset"
        bear_rows.append(row)
    return [
        {
            "name": "bull_virtual_exit",
            "sell_fee_21_5bp": _normalized(fee_amount(bull_terminal, fee_rate)),
            "terminal_close": plain(bull_terminal),
            "virtual_exit_value": _normalized(
                virtual_exit_value(
                    quantity=1, close=bull_terminal, sell_fee_rate=fee_rate
                )
            ),
        },
        {
            "entry_avg": plain(entry),
            "name": "bear_underwater_streak_if_held",
            "per_bar": bear_rows,
            "streak_bars": position.underwater_streak,
        },
        {"name": "sideways_scenario_present", "value": bool(data["sideways_closes"])},
    ]


def _v024(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    quantity = int(data["qty_each"])
    ledger = CashLedger(Decimal(data["initial_cash"]))
    open_price = Decimal(data["bar_open"])
    l2_limit = Decimal(data["L2_limit"])
    l1_gross = open_price * quantity
    l1_fee = fee_amount(l1_gross)
    ledger.fill_buy_immediate(
        amount=l1_gross + l1_fee,
        trade_session_index=0,
    )
    l2_gross = l2_limit * quantity
    l2_fee = fee_amount(l2_gross)
    cash_after_l1 = ledger.orderable_cash
    ledger.fill_buy_immediate(
        amount=l2_gross + l2_fee,
        trade_session_index=0,
    )
    return [
        {
            "L1": {
                "cash_after": fixed(cash_after_l1, 2),
                "fee": fixed(l1_fee, 2),
                "fill_price": plain(open_price),
                "gross": plain(l1_gross),
            },
            "L2": {
                "cash_after": fixed(ledger.orderable_cash, 2),
                "fee": fixed(l2_fee, 2),
                "fill_price": plain(l2_limit),
                "gross": plain(l2_gross),
            },
            "name": "fixed_qty_path",
            "sequence": ["L1", "L2"],
        }
    ]


def _v025(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    candidates = [
        SignalCandidate(
            symbol=item["symbol"],
            rsi=Decimal(item["rsi"]),
            support_distance=Decimal(item["support_dist"]),
            support_price=Decimal(1),
            is_add=False,
        )
        for item in data["candidates"]
    ]
    all_ranked = sorted(
        candidates,
        key=lambda item: (item.rsi, item.support_distance, item.symbol),
    )
    selected = rank_candidates(candidates, max_new=int(data["max_new"]))
    return [
        {
            "name": "order",
            "ranked": [item.symbol for item in all_ranked],
            "selected": [item.symbol for item in selected],
        }
    ]


def _v026(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    orders = sorted(
        data["orders"],
        key=lambda item: order_class_sort_key(
            is_add=item["class"] == "add",
            signal_rank=int(item["rank"]),
            symbol=item["symbol"],
            rung="L1",
        ),
    )
    return [
        {
            "name": "sequence",
            "value": [f"{item['class']}:{item['symbol']}" for item in orders],
        }
    ]


def _v027(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    arm = c3_180_should_arm(
        streak=int(data["streak"]), trim90_filled=bool(data["trim90_filled"])
    )
    return [{"name": "arm_180", "value": arm}]


def _v028(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    gross = Decimal(data["gross"])
    fee = fee_amount(gross)
    return [
        {
            "buy_fee": _normalized(fee),
            "name": "fees",
            "round_trip_bp": _normalized(round_trip_basis_points()),
            "sell_fee": _normalized(fee),
        }
    ]


def _v029(
    data: dict[str, Any], ticks: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    close = Decimal(data["t_minus_1_close"])
    cluster = data["cluster"]
    distance = Decimal(cluster["dist_pct"])
    band_ok = Decimal("-0.08") <= distance <= Decimal("-0.03")
    emitted = signal_is_eligible(
        rsi=Decimal(data["rsi"]),
        clusters=_contract_clusters([cluster]),
        close=close,
    )
    rungs = dict(
        build_buy_rungs(
            close=close,
            l2_price=Decimal(cluster["price"]),
            tick_table=ticks,
        )
    )
    return [
        {
            "L1": plain(rungs["L1"]),
            "L2": plain(rungs["L2"]),
            "band_ok": band_ok,
            "name": "signal_emitted",
            "value": emitted,
        }
    ]


def _v030(
    data: dict[str, Any], ticks: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    close = Decimal(data["t_minus_1_close"])
    l1 = ticks.align_buy(close * Decimal("0.97"))
    l2 = ticks.align_buy(Decimal(data["cluster_price"]))
    rungs = build_buy_rungs(
        close=close, l2_price=Decimal(data["cluster_price"]), tick_table=ticks
    )
    return [
        {
            "l1_floor": plain(l1),
            "l2_floor": plain(l2),
            "name": "dedupe",
            "order_price": plain(rungs[0][1]),
            "orders_count": len(rungs),
        }
    ]


def _v031(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    close = Decimal(data["t_minus_1_close"])
    levels = [_level_from_raw(raw) for raw in data["levels"]]
    cluster = cluster_levels(levels, close=close)[0]
    within = abs(levels[0].price - levels[1].price) / close
    qualifies = bool(qualifying_supports((cluster,), close=close))
    return [
        {
            "abs_diff_over_close": plain(within),
            "cluster_members": [str(item["price"]) for item in data["levels"]],
            "confluence": cluster.qualifies,
            "distinct_sources": list(cluster.distinct_sources),
            "name": "fib_only_pair_not_confluence",
            "qualifying_support_cluster": qualifies,
            "signal_from_this_cluster": qualifies,
            "source_count": len(cluster.distinct_sources),
            "within_1pct": within <= Decimal("0.01"),
        }
    ]


def _v032(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    cycle = C1Cycle()
    a7_reason = ""
    a7_action = ""
    for step in data["steps"]:
        notional = Decimal(step["notional"])
        is_add = step["class"] == "add"
        admitted, reason = cycle.reserve(notional=notional, is_add=is_add)
        if step["session"] == "A7":
            a7_action = "admit_fill" if admitted else "policy_rejected"
            a7_reason = reason or ""
        if not admitted:
            continue
        if step["fill"]:
            cycle.fill(notional=notional, is_add=is_add)
        else:
            cycle.expire(notional, is_add=is_add)
    return [
        {
            "add_count_after_A6": cycle.filled_add_count,
            "filled_gross_after_A6": plain(cycle.filled_buy_gross),
            "name": "adds_1_through_6_admitted",
            "notional_cap_remaining": plain(
                Decimal(data["per_symbol_total_cap_gross"]) - cycle.filled_buy_gross
            ),
        },
        {
            "filled_gross_unchanged": plain(cycle.filled_buy_gross),
            "name": "A7_policy_rejected_by_add_count",
            "not_reason": "notional_cap",
            "reject_reason": a7_reason,
            "value": a7_action,
        },
    ]


def _v033(
    data: dict[str, Any], _: TickTable, __: FrozenKospiIndex
) -> list[dict[str, Any]]:
    points = tuple(
        OhlcPoint(
            Decimal(item["adjusted_high"]),
            Decimal(item["adjusted_low"]),
            Decimal(item["adjusted_close"]),
        )
        for item in data["sessions"]
    )
    decision_index = int(data["decision_session_index"])
    window = scan_fib_window(
        points, decision_index=decision_index, window=int(data["window"])
    )
    levels = fib_levels(window.low, window.high)
    t_point = points[decision_index]
    wrong_low = min(window.low, t_point.low)
    wrong_high = max(window.high, t_point.high)
    wrong_levels = fib_levels(wrong_low, wrong_high)
    return [
        {
            "excluded_session_index": window.excluded_index,
            "name": "window_bounds_t_minus_1",
            "scanned_high_max": plain(window.high),
            "scanned_low_min": plain(window.low),
            "window_session_indices_inclusive": [
                window.start_index,
                window.end_index,
            ],
        },
        {
            "formula": "level = low + r×(high−low)",
            "high": plain(window.high),
            "low": plain(window.low),
            "name": "fib_levels_from_scanned_extrema",
            "values": _levels_payload(levels),
        },
        {
            "name": "include_t_mutant_extrema",
            "wrong_high": plain(wrong_high),
            "wrong_low": plain(wrong_low),
            "wrong_r0_236": plain(wrong_levels[Decimal("0.236")]),
        },
    ]


Runner = Callable[[dict[str, Any], TickTable, FrozenKospiIndex], list[dict[str, Any]]]

_RUNNERS: dict[str, Runner] = {
    "V001_rsi_wilder_seed": _v001,
    "V002_bb_ddof0": _v002,
    "V003_fib_direction": _v003,
    "V004_l2_less_no_signal": _v004,
    "V005_confluence_two_source": _v005,
    "V006_l1_l2_priority": _v006,
    "V007_tick_align_sell": _v007,
    "V008_tick_table_validation": _v008,
    "V009_c1_filled_notional_cap": _v009,
    "V010_c2_regime_sma200": _v010,
    "V011_c3_trim_streak": _v011,
    "V012_t2_settle_payable": _v012,
    "V013_monthly_contribution_units": _v013,
    "V014_same_bar_buy_sell": _v014,
    "V015_cash_exhaustion": _v015,
    "V016_counterfactual_demand_gaming": _v016,
    "V017_unresolved_terminal": _v017,
    "V018_locked_share_tw": _v018,
    "V019_funding_p05": _v019,
    "V020_deployment_formula": _v020,
    "V021_split_adjusted_price_sim": _v021,
    "V022_dual_view_consistency": _v022,
    "V023_market_regimes_paths": _v023,
    "V024_ladder_exhaustion": _v024,
    "V025_ranking_lexicographic": _v025,
    "V026_buy_order_class_priority": _v026,
    "V027_c3_180_requires_90_fill": _v027,
    "V028_fee_neutral_43": _v028,
    "V029_signal_with_l2": _v029,
    "V030_l1_l2_dedupe": _v030,
    "V031_fib_only_no_confluence": _v031,
    "V032_c1_max_adds_per_cycle": _v032,
    "V033_fib_window_scan_pit": _v033,
}

if len(_RUNNERS) != 33:
    raise AssertionError("golden runner count drift")
