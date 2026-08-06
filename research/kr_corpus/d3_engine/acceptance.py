"""Single read-only acceptance entrypoint for D3-E1 evidence."""

from __future__ import annotations

import argparse
import ast
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from research.kr_corpus.d3_engine import engine as engine_module
from research.kr_corpus.d3_engine.canonical import canonical_bytes
from research.kr_corpus.d3_engine.cash import CashLedger
from research.kr_corpus.d3_engine.constants import ArtifactPaths, runtime_pins
from research.kr_corpus.d3_engine.engine import PortfolioEngine
from research.kr_corpus.d3_engine.golden import GoldenSuite, GoldenSuiteResult
from research.kr_corpus.d3_engine.guards import (
    SealedAccessBlocked,
    SealedAccessGuard,
    SealedAccessSpy,
)
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
from research.kr_corpus.d3_engine.mutants import run_mutant_probes
from research.kr_corpus.d3_engine.policies import C1Cycle
from research.kr_corpus.d3_engine.sources import (
    ContractDrift,
    FrozenKospiIndex,
    verify_golden_checksums,
    verify_start_gate,
)
from research.kr_corpus.d3_engine.tick import (
    InvalidTickTable,
    TickTable,
    load_tick_table,
)


def _golden_payload(result: GoldenSuiteResult) -> dict[str, Any]:
    return {
        "cases": [
            {
                "vector_id": case.vector_id,
                "actual": case.actual,
                "passed": case.passed,
                "excluded_explanation_fields": case.excluded_explanation_fields,
            }
            for case in result.cases
        ],
        "passed": result.passed,
        "total": result.total,
    }


def _synthetic_four_arm_payload(
    ticks: TickTable, index: FrozenKospiIndex
) -> dict[str, Any]:
    # The first 201 rows are hash-bound XKRX sessions from the frozen index source.
    sessions = [row.session for row in index.rows[:201]]
    if len(sessions) != 201:
        raise AssertionError("synthetic XKRX session fixture drift")
    bar_sessions = sessions[-121:]
    bars = _contract_signal_bars(bar_sessions, symbols=("005930",))
    index_closes = tuple(
        (session, Decimal(2000 + index)) for index, session in enumerate(sessions)
    )
    output: dict[str, Any] = {}
    for arm in Arm:
        spy = SealedAccessSpy()
        engine = PortfolioEngine(ticks, access_guard=SealedAccessGuard(spy))
        result = engine.run(
            PortfolioRunInput(
                arm=arm,
                cashflow_view=CashflowView.WITH_CONTRIBUTION,
                bars=bars,
                market_sessions=tuple(sessions),
                index_closes=index_closes,
                decision_start=bar_sessions[-1],
            )
        )
        output[arm.value] = {
            "status": result.status,
            "metrics": result.metrics,
            "events": result.events,
            "fills": [
                {
                    "side": fill.side.value,
                    "price": fill.price,
                    "quantity": fill.quantity,
                    "rung": next(
                        event["rung"]
                        for event in result.events
                        if event.get("event") == "order_submitted"
                        and event.get("order_id") == fill.order_id
                    ),
                }
                for fill in result.fills
            ],
            "evidence": result.evidence,
        }
    return output


_CONTRACT_SIGNAL_LAST_20 = (
    10660,
    10580,
    9660,
    9220,
    9720,
    10280,
    10540,
    10620,
    10760,
    10380,
    10180,
    9800,
    9980,
    10060,
    10660,
    10200,
    10480,
    10600,
    10540,
    10000,
)


def _contract_signal_bars(
    sessions: list[date] | tuple[date, ...], *, symbols: tuple[str, ...]
) -> tuple[Bar, ...]:
    """Return a natural 120-session indicator history plus a two-rung fill bar."""

    if len(sessions) != 121:
        raise AssertionError("contract signal fixture requires exactly 121 sessions")
    prior_closes = (Decimal(10500),) * 100 + tuple(
        Decimal(value) for value in _CONTRACT_SIGNAL_LAST_20
    )
    bars: list[Bar] = []
    for symbol in symbols:
        bars.extend(
            Bar(
                session=session,
                symbol=symbol,
                open=close,
                high=Decimal(11000),
                low=Decimal(7000),
                close=close,
            )
            for session, close in zip(sessions[:-1], prior_closes, strict=True)
        )
        bars.append(
            Bar(
                session=sessions[-1],
                symbol=symbol,
                open=Decimal(9600),
                high=Decimal(10100),
                low=Decimal(9000),
                close=Decimal(9900),
            )
        )
    return tuple(bars)


def _resistance_probe_bars(sessions: list[date]) -> list[Bar]:
    prior_closes = [Decimal(9520 if index % 2 == 0 else 10480) for index in range(120)]
    prior_closes[-1] = Decimal(10000)
    closes = prior_closes + [Decimal(10000)]
    return [
        Bar(
            session=session,
            symbol="RESIST",
            open=close,
            high=Decimal(11000),
            low=Decimal(7000),
            close=close,
        )
        for session, close in zip(sessions, closes, strict=True)
    ]


def _engine_contract_probes(
    ticks: TickTable, index: FrozenKospiIndex
) -> dict[str, Any]:
    """Exercise non-vacuous engine paths that the golden slices cannot cover."""

    engine = PortfolioEngine(ticks)
    probe_session = index.rows[0].session
    sell_cases = (
        Bar(
            probe_session,
            "SELL",
            Decimal(105),
            Decimal(110),
            Decimal(90),
            Decimal(100),
        ),
        Bar(
            probe_session,
            "SELL",
            Decimal(95),
            Decimal(105),
            Decimal(90),
            Decimal(100),
        ),
        Bar(
            probe_session,
            "SELL",
            Decimal(95),
            Decimal(99),
            Decimal(90),
            Decimal(98),
        ),
    )
    sell_fill_prices = [
        engine._sell_fill_price(Decimal(100), bar) for bar in sell_cases
    ]
    if sell_fill_prices != [Decimal(105), Decimal(100), None]:
        raise AssertionError(f"sell fill contract drift: {sell_fill_prices}")

    mdd = engine._max_drawdown((Decimal(100), Decimal(80), Decimal(90)))
    if mdd != Decimal("-0.2"):
        raise AssertionError(f"max drawdown contract drift: {mdd}")

    resistance_sessions = [row.session for row in index.rows[:121]]
    resistance_bars = _resistance_probe_bars(resistance_sessions)
    resistance_orders = engine._resistance_orders(
        symbol="RESIST",
        session=resistance_sessions[-1],
        history=resistance_bars,
        index=120,
        position=Position(
            symbol="RESIST",
            quantity=10,
            average_price=Decimal(9000),
            invested_cost_basis=Decimal(90000),
        ),
        first_order_number=0,
    )
    resistance_projection = [
        (order.rung, order.limit, order.quantity) for order in resistance_orders
    ]
    if resistance_projection != [("R1", Decimal(10960), 5)]:
        raise AssertionError(
            f"resistance order contract drift: {resistance_projection}"
        )

    expiry_cash = CashLedger(Decimal(1000))
    if not expiry_cash.reserve_order("EXPIRY", Decimal(200)):
        raise AssertionError("expiry probe reservation unexpectedly rejected")
    expiry_cycle = C1Cycle()
    admitted, _ = expiry_cycle.reserve(notional=Decimal(200), is_add=False)
    if not admitted:
        raise AssertionError("expiry probe C1 reservation unexpectedly rejected")
    expiry_order = Order(
        order_id="EXPIRY",
        session=probe_session,
        symbol="EXPIRY",
        side=OrderSide.BUY,
        order_class=OrderClass.NEW,
        limit=Decimal(200),
        quantity=1,
        rung="L1",
        rank=1,
    )
    expiry_state = RunState(pending_orders=[expiry_order])
    engine._expire_orders(
        state=expiry_state,
        cash=expiry_cash,
        c1_cycles=defaultdict(C1Cycle, {"EXPIRY": expiry_cycle}),
        arm=Arm.C1,
        session=index.rows[1].session,
    )
    expiry_ok = (
        expiry_cash.orderable_cash == Decimal(1000)
        and not expiry_cash.reserved_orders
        and not expiry_state.pending_orders
        and expiry_order.status is OrderStatus.EXPIRED
        and expiry_cycle.reserved_buy_gross == 0
    )
    if not expiry_ok:
        raise AssertionError("DAY order expiry contract drift")

    settlement_cash = CashLedger(Decimal(1000))
    settlement_cash.fill_sell(net_amount=Decimal(100), trade_session_index=0)
    first_settle = settlement_cash.settle_pre_open(2)
    second_settle = settlement_cash.settle_pre_open(2)
    settlement_ok = (
        first_settle["receivable_credited"] == Decimal(100)
        and second_settle["receivable_credited"] == 0
        and settlement_cash.orderable_cash == Decimal(1100)
    )
    if not settlement_ok:
        raise AssertionError("T+2 receivable credited more or less than once")

    sessions = [row.session for row in index.rows[:201]]
    decision_sessions = sessions[-121:]
    rank_result = PortfolioEngine(ticks).run(
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
    ranked_pairs = rank_result.evidence["counterfactual_demand_pairs"]
    if len(ranked_pairs) != 3 or len(rank_result.fills) != 6:
        raise AssertionError(
            "global max-new rank cap drift: "
            f"pairs={len(ranked_pairs)} fills={len(rank_result.fills)}"
        )

    missing_index_session = sessions[50]
    c2_result = PortfolioEngine(ticks).run(
        PortfolioRunInput(
            arm=Arm.C2,
            cashflow_view=CashflowView.NO_CONTRIBUTION,
            bars=_contract_signal_bars(decision_sessions, symbols=("005930",)),
            market_sessions=tuple(sessions),
            index_closes=tuple(
                (session, Decimal(2000 + row_index))
                for row_index, session in enumerate(sessions)
                if session != missing_index_session
            ),
            decision_start=decision_sessions[-1],
        )
    )
    c2_missing_ok = not c2_result.fills and any(
        event.get("reason") == "c2_below_sma200_or_missing"
        for event in c2_result.events
    )
    if not c2_missing_ok:
        raise AssertionError("C2 missing-index fail-closed contract drift")

    c3_position = Position(symbol="C3", quantity=3, trim90_armed=True)
    c3_suppression_bound = engine_module.c3_buy_suppressed(c3_position)
    if not c3_suppression_bound:
        raise AssertionError("C3 armed-position buy suppression is not bound")

    return {
        "sell_fill_prices": sell_fill_prices,
        "unitized_mdd": mdd,
        "resistance_orders": [
            {"rung": rung, "limit": limit, "quantity": quantity}
            for rung, limit, quantity in resistance_projection
        ],
        "day_expiry": expiry_ok,
        "receivable_single_credit": settlement_ok,
        "global_rank_cap": {
            "demand_pairs": len(ranked_pairs),
            "fills": len(rank_result.fills),
        },
        "c2_missing_index_fail_closed": c2_missing_ok,
        "c3_buy_suppression_bound": c3_suppression_bound,
    }


class _MetadataSpy(Mapping[str, object]):
    def __init__(self) -> None:
        self.lookups = 0

    def __getitem__(self, key: str) -> object:
        self.lookups += 1
        return {"D3_CALIBRATION_2025": "sealed"}[key]

    def __iter__(self) -> Iterator[str]:
        return iter(("D3_CALIBRATION_2025",))

    def __len__(self) -> int:
        return 1


def _negative_tests(
    paths: ArtifactPaths, tick_table: TickTable, index: FrozenKospiIndex
) -> tuple[dict[str, Any], dict[str, int]]:
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="d3-e1-sha-drift-") as raw_tmp:
        tmp = Path(raw_tmp)
        drifted = tmp / paths.contract_v3.name
        shutil.copyfile(paths.contract_v3, drifted)
        drifted.write_bytes(drifted.read_bytes() + b"\nDRIFT")
        try:
            verify_start_gate(replace(paths, contract_v3=drifted))
        except ContractDrift as exc:
            results["sha_drift"] = {
                "status": "PASS",
                "fail_closed": True,
                "code": exc.code,
            }
        else:
            results["sha_drift"] = {"status": "FAIL", "fail_closed": False}

    invalid_tables = {
        "tick_gap": {
            "bands": [
                {"lower_inclusive": 0, "upper_exclusive": 2000, "tick": 1},
                {"lower_inclusive": 3000, "upper_exclusive": None, "tick": 5},
            ]
        },
        "tick_overlap": {
            "bands": [
                {"lower_inclusive": 0, "upper_exclusive": 3000, "tick": 1},
                {"lower_inclusive": 2000, "upper_exclusive": None, "tick": 5},
            ]
        },
    }
    for name, payload in invalid_tables.items():
        try:
            TickTable.from_mapping(payload)
        except InvalidTickTable as exc:
            results[name] = {
                "status": "PASS",
                "fail_closed": True,
                "code": exc.code,
                "reason": str(exc),
            }
        else:
            results[name] = {"status": "FAIL", "fail_closed": False}

    missing_target = index.rows[300].session
    missing_index = FrozenKospiIndex(
        tuple(row for row in index.rows if row.session != missing_target)
    )
    allowed, close, sma, previous = missing_index.regime_allows(
        decision_session=missing_target
    )
    results["index_missing"] = {
        "status": "PASS" if not allowed and close is None and sma is None else "FAIL",
        "fail_closed": not allowed,
        "forward_fill": False,
        "previous": previous,
    }

    spy = SealedAccessSpy()
    guard = SealedAccessGuard(spy)
    loader_calls = 0

    def loader() -> str:
        nonlocal loader_calls
        loader_calls += 1
        return "forbidden"

    blocked = 0
    attempts = (
        lambda: guard.read_bar(
            path="/tmp/exploration/bars.parquet",
            session=date(2025, 1, 2),
            loader=loader,
        ),
        lambda: guard.read_manifest(path="/tmp/HOLDOUT/manifest.json", loader=loader),
    )
    for attempt in attempts:
        try:
            attempt()
        except SealedAccessBlocked:
            blocked += 1
    metadata = _MetadataSpy()
    try:
        guard.read_metadata(metadata, "D3_CALIBRATION_2025")
    except SealedAccessBlocked:
        blocked += 1
    results["sealed_access"] = {
        "status": (
            "PASS"
            if blocked == 3
            and loader_calls == 0
            and metadata.lookups == 0
            and spy.sealed_reads == 0
            else "FAIL"
        ),
        "blocked_attempts": blocked,
        "loader_calls": loader_calls,
        "metadata_key_lookups": metadata.lookups,
        "sealed_access_spy": spy.sealed_reads,
    }
    tick_table.validate()
    return results, spy.evidence()


def _prove_no_tick_python_import() -> dict[str, Any]:
    root = Path(__file__).resolve().parent
    forbidden = "krx_tick_size_frozen"
    hits: list[str] = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(forbidden in alias.name for alias in node.names):
                    hits.append(f"{path.name}:{node.lineno}")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and forbidden in node.module
            ):
                hits.append(f"{path.name}:{node.lineno}")
    return {
        "source": "krx_tick_table_frozen.yaml",
        "python_import_count": len(hits),
        "python_import_hits": hits,
    }


def run_acceptance(paths: ArtifactPaths | None = None) -> dict[str, Any]:
    paths = paths or ArtifactPaths.defaults()
    sha_gate = verify_start_gate(paths)
    golden_files = verify_golden_checksums(paths.golden_root)
    pins = runtime_pins()
    ticks = load_tick_table(paths.tick_yaml)
    index = FrozenKospiIndex.load(paths.index_csv)

    suite = GoldenSuite(golden_root=paths.golden_root, tick_table=ticks, index=index)
    golden_first = suite.run()
    golden_second = suite.run()
    golden_first_bytes = canonical_bytes(_golden_payload(golden_first))
    golden_second_bytes = canonical_bytes(_golden_payload(golden_second))

    four_arm_first = _synthetic_four_arm_payload(ticks, index)
    four_arm_second = _synthetic_four_arm_payload(ticks, index)
    engine_probes_first = _engine_contract_probes(ticks, index)
    engine_probes_second = _engine_contract_probes(ticks, index)
    deterministic = (
        golden_first_bytes == golden_second_bytes
        and canonical_bytes(four_arm_first) == canonical_bytes(four_arm_second)
        and canonical_bytes(engine_probes_first)
        == canonical_bytes(engine_probes_second)
    )

    mutants = run_mutant_probes(paths.golden_root, ticks)
    negatives, sealed_evidence = _negative_tests(paths, ticks, index)
    tick_proof = _prove_no_tick_python_import()
    all_excluded = sorted(
        path for case in golden_first.cases for path in case.excluded_explanation_fields
    )
    if golden_first.passed != 33:
        raise AssertionError(
            f"golden exact failed: {golden_first.passed}/{golden_first.total}"
        )
    expected_fills = [
        {"side": "buy", "price": Decimal(9600), "quantity": 30, "rung": "L1"},
        {"side": "buy", "price": Decimal(9450), "quantity": 31, "rung": "L2"},
    ]
    for arm, payload in four_arm_first.items():
        if payload["status"] != "OK":
            raise AssertionError(f"{arm} engine status drift: {payload['status']}")
        if payload["fills"] != expected_fills:
            raise AssertionError(f"{arm} natural fill path drift: {payload['fills']}")
        if payload["metrics"]["signals_submitted"] != 2:
            raise AssertionError(f"{arm} submitted-order count drift")
        if payload["metrics"]["terminal_nav"] != Decimal("13520402.57250"):
            raise AssertionError(
                f"{arm} terminal NAV drift: {payload['metrics']['terminal_nav']}"
            )
    if not deterministic:
        raise AssertionError("determinism check failed")
    if not all(probe.differs for probe in mutants):
        raise AssertionError("one or more mutant probes did not differ")
    if any(result["status"] != "PASS" for result in negatives.values()):
        raise AssertionError("one or more negative tests failed")
    if tick_proof["python_import_count"] != 0:
        raise AssertionError("provenance-only tick Python import detected")

    fib_case = next(
        case
        for case in golden_first.cases
        if case.vector_id == "V033_fib_window_scan_pit"
    )
    fib_bounds = next(
        row for row in fib_case.actual if row["name"] == "window_bounds_t_minus_1"
    )
    fib_window_excludes_t = (
        fib_bounds["window_session_indices_inclusive"][1] + 1
        == fib_bounds["excluded_session_index"]
        and fib_bounds["window_session_indices_inclusive"][1]
        - fib_bounds["window_session_indices_inclusive"][0]
        + 1
        == 120
    )
    if not fib_window_excludes_t:
        raise AssertionError("Fibonacci window includes t or is not 120 sessions")

    return {
        "sha_gate": {"passed": len(sha_gate), "total": 9, "rows": sha_gate},
        "golden_files": golden_files,
        "golden_exact": golden_first.as_dict(),
        "deterministic_2runs": deterministic,
        "four_arms": {
            arm: payload["status"] for arm, payload in four_arm_first.items()
        },
        "four_arm_contract": {
            arm: {
                "fills": payload["fills"],
                "signals_submitted": payload["metrics"]["signals_submitted"],
                "terminal_nav": payload["metrics"]["terminal_nav"],
            }
            for arm, payload in four_arm_first.items()
        },
        "engine_contract_probes": engine_probes_first,
        "mutant_diffs": {
            "passed": sum(probe.differs for probe in mutants),
            "total": len(mutants),
            "cases": [
                {
                    "name": probe.name,
                    "vector_id": probe.vector_id,
                    "differs": probe.differs,
                    "correct": probe.correct,
                    "mutant": probe.mutant,
                }
                for probe in mutants
            ],
        },
        "negative_tests": negatives,
        "sealed_access_spy": sealed_evidence["sealed_access_spy"],
        "engine_input_explanation_keys": 0,
        "excluded_explanation_fields": all_excluded,
        "fib_window_excludes_t": fib_window_excludes_t,
        "tick_source": tick_proof,
        "runtime_pins": pins,
        "primary_run_executed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full-json",
        action="store_true",
        help="emit complete canonical evidence instead of a compact summary",
    )
    args = parser.parse_args()
    result = run_acceptance()
    if args.full_json:
        print(canonical_bytes(result).decode("utf-8"), end="")
    else:
        negative_passed = sum(
            row["status"] == "PASS" for row in result["negative_tests"].values()
        )
        negative_total = len(result["negative_tests"])
        print(
            " ".join(
                (
                    f"SHA_GATE={result['sha_gate']['passed']}/9",
                    f"GOLDEN_EXACT={result['golden_exact']['passed']}/33",
                    f"DETERMINISTIC_2RUNS={result['deterministic_2runs']}",
                    f"MUTANT_DIFFS={result['mutant_diffs']['passed']}/23",
                    f"NEGATIVE_TESTS={negative_passed}/{negative_total}",
                    f"SEALED_ACCESS_SPY={result['sealed_access_spy']}",
                    f"PRIMARY_RUN_EXECUTED={result['primary_run_executed']}",
                )
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
