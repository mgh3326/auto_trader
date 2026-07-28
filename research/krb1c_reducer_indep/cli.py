"""CLI for the independent §6.2.1 reducer reproduction.

Emits the §7.3~7.6 publication triple into an output directory:

    p0_2_cost_inputs.normalized.json
    c_stress_candidates.csv          (17 reduced-fraction fields per P)
    c_stress_reducer_result.json

This is the §7.7 step-3 independent reproduction, NOT the §7.2 "정확히 1회"
binding numeric reducer execution: it emits no P0-2 completion hash and writes
nothing outside the chosen output directory. It performs no network, database,
broker, order, watch or journal access.

Usage:
    uv run python -m research.krb1c_reducer_indep.cli --out-dir <dir>
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from fractions import Fraction

from .reducer import (
    MarketCostInput,
    ReducerFailClosed,
    ReducerResult,
    reduce_c_stress_cap,
)
from .sealed_input import (
    AMENDMENT_CANONICAL_SHA256,
    OFFICIAL_TARIFF_SNAPSHOT_SHA256,
    PARENT_CANONICAL_SHA256,
    default_canonical_path,
    load_from_canonical,
    sealed_cost_inputs,
    sealed_records,
)
from .tick import KOSDAQ_TICK_TABLE, KOSPI_TICK_TABLE, PRICE_MAX, PRICE_MIN

TABLES = {"KOSPI": KOSPI_TICK_TABLE, "KOSDAQ": KOSDAQ_TICK_TABLE}

CANDIDATE_FIELDS = (
    "market",
    "price",
    "tick_at_price",
    "rho_entry_num",
    "rho_entry_den",
    "exit_witness_q",
    "tick_at_exit_witness",
    "rho_exit_num",
    "rho_exit_den",
    "entry_multiplier_num",
    "entry_multiplier_den",
    "exit_multiplier_cap_num",
    "exit_multiplier_cap_den",
    "c_num",
    "c_den",
    "c_bp_ceil",
    "is_market_argmax",
)
assert len(CANDIDATE_FIELDS) == 17, "§7.5 requires 17 fields"


def frac(value: Fraction) -> dict[str, int]:
    """Reduced-fraction rendering. ``Fraction`` is always in lowest terms."""
    return {"num": value.numerator, "den": value.denominator}


def write_candidates_csv(result: ReducerResult, path: str) -> int:
    rows = 0
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CANDIDATE_FIELDS)
        for market in sorted(result.markets):
            market_result = result.markets[market]
            for row in market_result.candidates:
                writer.writerow(
                    [
                        row.market,
                        row.price,
                        row.tick_at_price,
                        row.rho_entry.numerator,
                        row.rho_entry.denominator,
                        row.exit_witness_q,
                        row.tick_at_exit_witness,
                        row.rho_exit.numerator,
                        row.rho_exit.denominator,
                        row.entry_multiplier.numerator,
                        row.entry_multiplier.denominator,
                        row.exit_multiplier_cap.numerator,
                        row.exit_multiplier_cap.denominator,
                        row.c.numerator,
                        row.c.denominator,
                        row.c_bp_ceil,
                        int(row.price == market_result.witness_price),
                    ]
                )
                rows += 1
    return rows


def build_normalized_inputs(costs: dict[str, MarketCostInput]) -> dict:
    return {
        "schema_version": "krb1.p0_2_cost_inputs.v1",
        "parent_canonical_sha256": PARENT_CANONICAL_SHA256,
        "amendment_canonical_sha256": AMENDMENT_CANONICAL_SHA256,
        "official_tariff_snapshot_sha256": OFFICIAL_TARIFF_SNAPSHOT_SHA256,
        "rate_scale": 10**12,
        "note": (
            "Independent reproduction input (§7.7 step 3). Not a sealed P0-2 "
            "artifact; carries no p0_2_completed_at_kst and no completion hash."
        ),
        "market_cost_records": {
            market: [
                {
                    "market": rec.market,
                    "broker_id": rec.broker_id,
                    "account_product_id": rec.account_product_id,
                    "order_channel_id": rec.order_channel_id,
                    "cost_basis": rec.cost_basis,
                    "effective_from": rec.effective_from,
                    "effective_to": rec.effective_to,
                    "buy_commission_rate_e12": rec.buy_commission_rate_e12,
                    "sell_commission_rate_e12": rec.sell_commission_rate_e12,
                    "sell_tax_components": [
                        {
                            "component_code": comp.component_code,
                            "rate_e12": comp.rate_e12,
                        }
                        for comp in rec.sell_tax_components
                    ],
                    "source_snapshot_sha256": rec.source_snapshot_sha256,
                    "probe_reconciliation_status": rec.probe_reconciliation_status,
                    "mock_cost_relation": rec.mock_cost_relation,
                }
                for rec in records
            ]
            for market, records in sealed_records().items()
        },
        "reduced": {
            market: {
                "B_m_rate_e12": cost.buy_rate_e12,
                "S_m_rate_e12": cost.sell_rate_e12,
                "A_m_rate_e12": cost.sell_tax_rate_e12,
                "b_m": frac(cost.b),
                "s_m": frac(cost.s),
                "tau_m": frac(cost.tau),
                "a_m": frac(cost.a),
            }
            for market, cost in sorted(costs.items())
        },
    }


def build_result_json(result: ReducerResult) -> dict:
    return {
        "reducer_spec_id": result.reducer_spec_id,
        "implementation": "independent-reproduction",
        "parent_canonical_sha256": PARENT_CANONICAL_SHA256,
        "amendment_canonical_sha256": AMENDMENT_CANONICAL_SHA256,
        "official_tariff_snapshot_sha256": OFFICIAL_TARIFF_SNAPSHOT_SHA256,
        "price_range": {"min": PRICE_MIN, "max": PRICE_MAX},
        "c_raw": frac(result.c_raw),
        "witness_market": result.witness_market,
        "witness_price": result.witness_price,
        "c_stress_cap_bp": result.c_stress_cap_bp,
        "c_stress_cap": frac(result.c_stress_cap),
        "c_stress_cap_decimal": result.c_stress_cap_decimal,
        "all_target_checks_passed": result.all_target_checks_passed,
        "enumerated_candidate_count": result.enumerated_count,
        "target_check_count": result.target_check_count,
        "per_market": {
            market: {
                "enumerated_count": res.enumerated_count,
                "c_raw": frac(res.c_raw),
                "witness_price": res.witness_price,
                "tick_table_provenance": TABLES[market].provenance,
                "target_check_count": len(res.target_checks),
                "target_check_failures": len(
                    [row for row in res.target_checks if not row.passed]
                ),
            }
            for market, res in sorted(result.markets.items())
        },
        "p0_2_completion_hash": None,
        "p0_2_completion_hash_reason": (
            "§7.7 step 3 reproduction only; completion hash requires step 4 "
            "full-enumeration agreement and an independence review PASS."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="KRB1C C_stress_cap reducer — independent reproduction"
    )
    parser.add_argument("--out-dir", required=True)
    parser.add_argument(
        "--canonical-path",
        default=default_canonical_path(),
        help="sealed amendment canonical JSON (hash-verified before use)",
    )
    parser.add_argument(
        "--skip-canonical-check",
        action="store_true",
        help="use the transcribed constants without re-reading the canonical",
    )
    args = parser.parse_args(argv)

    if args.skip_canonical_check:
        costs = sealed_cost_inputs()
    else:
        costs = load_from_canonical(args.canonical_path)

    try:
        result = reduce_c_stress_cap(TABLES, costs)
    except ReducerFailClosed as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 2

    os.makedirs(args.out_dir, exist_ok=True)
    inputs_path = os.path.join(args.out_dir, "p0_2_cost_inputs.normalized.json")
    csv_path = os.path.join(args.out_dir, "c_stress_candidates.csv")
    result_path = os.path.join(args.out_dir, "c_stress_reducer_result.json")

    with open(inputs_path, "w", encoding="utf-8") as handle:
        json.dump(
            build_normalized_inputs(costs),
            handle,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    rows = write_candidates_csv(result, csv_path)
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(
            build_result_json(result),
            handle,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    print(
        f"C_raw            = {result.c_raw} "
        f"({result.c_raw.numerator}/{result.c_raw.denominator})"
    )
    print(f"witness          = {result.witness_market} P={result.witness_price}")
    print(f"C_stress_cap_bp  = {result.c_stress_cap_bp}")
    print(f"C_stress_cap     = {result.c_stress_cap_decimal}")
    print(f"candidates       = {rows}")
    print(
        f"target checks    = {result.target_check_count} "
        f"(all passed = {result.all_target_checks_passed})"
    )
    for market in sorted(result.markets):
        res = result.markets[market]
        print(
            f"  {market:<7} |E_m|={res.enumerated_count} "
            f"C_raw_m={res.c_raw} witness_P={res.witness_price}"
        )
    print(f"artifacts -> {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
