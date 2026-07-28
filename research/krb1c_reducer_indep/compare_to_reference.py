"""§7.7 step 4 — full-enumeration comparison against a reference candidate CSV.

Reads a reference ``c_stress_candidates.csv``, runs this independent reducer,
and compares **every** row on **every** shared field. No sampling.

The reference implementation is not imported or vendored; only its published
CSV artifact is read, so running this does not compromise the step-3
independence of the reducer itself.

Field-name mapping is explicit because the two implementations chose different
column names for the same §5/§6 quantities. Two of each side's 17 columns are
implementation-specific (this side publishes ``c_bp_ceil`` and
``is_market_argmax``; the observed reference publishes ``target_price`` and
``target_check_passed``); ``target_price`` and ``target_check_passed`` are
recomputed here from this implementation's §6.8/§6.9 output so they can still
be compared.

Usage:
    uv run python -m research.krb1c_reducer_indep.compare_to_reference \
        --reference-csv <path> [--reference-result-json <path>]

Exit code 0 iff there are zero divergences.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from fractions import Fraction
from typing import Dict, List, Tuple

from .cli import TABLES
from .reducer import reduce_c_stress_cap
from .sealed_input import sealed_cost_inputs

# (reference column, this implementation's key)
FIELD_MAP: Tuple[Tuple[str, str], ...] = (
    ("entry_price", "price"),
    ("entry_tick", "tick_at_price"),
    ("rho_entry_num", "rho_entry_num"),
    ("rho_entry_den", "rho_entry_den"),
    ("exit_witness_price", "exit_witness_q"),
    ("exit_witness_tick", "tick_at_exit_witness"),
    ("rho_exit_num", "rho_exit_num"),
    ("rho_exit_den", "rho_exit_den"),
    ("entry_multiplier_num", "entry_multiplier_num"),
    ("entry_multiplier_den", "entry_multiplier_den"),
    ("exit_multiplier_num", "exit_multiplier_cap_num"),
    ("exit_multiplier_den", "exit_multiplier_cap_den"),
    ("c_num", "c_num"),
    ("c_den", "c_den"),
    ("target_price", "target_price"),
    ("target_check_passed", "target_check_passed"),
)

BOOL_FIELDS = {"target_check_passed"}


def independent_rows() -> Tuple[Dict[Tuple[str, int], dict], object]:
    result = reduce_c_stress_cap(TABLES, sealed_cost_inputs())
    rows: Dict[Tuple[str, int], dict] = {}
    for market, res in result.markets.items():
        checks = {c.price: c for c in res.target_checks}
        for row in res.candidates:
            chk = checks[row.price]
            rows[(market, row.price)] = {
                "price": row.price,
                "tick_at_price": row.tick_at_price,
                "rho_entry_num": row.rho_entry.numerator,
                "rho_entry_den": row.rho_entry.denominator,
                "exit_witness_q": row.exit_witness_q,
                "tick_at_exit_witness": row.tick_at_exit_witness,
                "rho_exit_num": row.rho_exit.numerator,
                "rho_exit_den": row.rho_exit.denominator,
                "entry_multiplier_num": row.entry_multiplier.numerator,
                "entry_multiplier_den": row.entry_multiplier.denominator,
                "exit_multiplier_cap_num": row.exit_multiplier_cap.numerator,
                "exit_multiplier_cap_den": row.exit_multiplier_cap.denominator,
                "c_num": row.c.numerator,
                "c_den": row.c.denominator,
                "target_price": chk.target,
                "target_check_passed": chk.passed,
            }
    return rows, result


def reference_rows(path: str) -> Dict[Tuple[str, int], dict]:
    rows: Dict[Tuple[str, int], dict] = {}
    with open(path, encoding="utf-8", newline="") as handle:
        for rec in csv.DictReader(handle):
            key = (rec["market"], int(rec["entry_price"]))
            parsed: dict = {}
            for ref_name, _ in FIELD_MAP:
                raw = rec[ref_name]
                if ref_name in BOOL_FIELDS:
                    if raw.lower() not in ("true", "false"):
                        raise ValueError(f"non-boolean {ref_name}: {raw!r}")
                    parsed[ref_name] = raw.lower() == "true"
                else:
                    parsed[ref_name] = int(raw)
            rows[key] = parsed
    return rows


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-csv", required=True)
    parser.add_argument("--reference-result-json", default=None)
    args = parser.parse_args(argv)

    mine, result = independent_rows()
    ref = reference_rows(args.reference_csv)

    only_mine = sorted(set(mine) - set(ref))
    only_ref = sorted(set(ref) - set(mine))
    shared = sorted(set(mine) & set(ref))

    print(f"independent rows           : {len(mine)}")
    print(f"reference rows             : {len(ref)}")
    print(f"keys only in independent   : {len(only_mine)} {only_mine[:5]}")
    print(f"keys only in reference     : {len(only_ref)} {only_ref[:5]}")

    mismatches = []
    comparisons = 0
    for key in shared:
        m, r = mine[key], ref[key]
        for ref_name, my_name in FIELD_MAP:
            comparisons += 1
            if m[my_name] != r[ref_name]:
                mismatches.append((key, ref_name, r[ref_name], m[my_name]))

    value_mismatches = sum(
        1
        for key in shared
        if Fraction(mine[key]["c_num"], mine[key]["c_den"])
        != Fraction(ref[key]["c_num"], ref[key]["c_den"])
    )

    print(f"rows compared              : {len(shared)}")
    print(f"field comparisons          : {comparisons}")
    print(f"field mismatches           : {len(mismatches)}")
    for item in mismatches[:50]:
        print(f"  MISMATCH key={item[0]} field={item[1]} ref={item[2]} mine={item[3]}")
    print(f"c-value mismatches         : {value_mismatches}")

    scalar_bad = 0
    if args.reference_result_json:
        with open(args.reference_result_json, encoding="utf-8") as handle:
            ref_result = json.load(handle)
        scalars = {
            "c_raw_num": (result.c_raw.numerator, ref_result.get("c_raw_num")),
            "c_raw_den": (result.c_raw.denominator, ref_result.get("c_raw_den")),
            "c_stress_cap_bp": (
                result.c_stress_cap_bp,
                ref_result.get("c_stress_cap_bp"),
            ),
            "c_stress_cap_decimal": (
                result.c_stress_cap_decimal,
                ref_result.get("c_stress_cap_decimal"),
            ),
            "witness_market": (
                result.witness_market,
                ref_result.get("witness_market"),
            ),
            "witness_price": (
                result.witness_price,
                ref_result.get("witness_entry_price"),
            ),
            "all_target_checks_passed": (
                result.all_target_checks_passed,
                ref_result.get("all_target_checks_passed"),
            ),
            "candidate_count_total": (
                result.enumerated_count,
                ref_result.get("candidate_count_total"),
            ),
        }
        print("\nscalar comparison:")
        for name, (mine_v, ref_v) in scalars.items():
            ok = mine_v == ref_v
            scalar_bad += 0 if ok else 1
            print(f"  {'OK ' if ok else 'BAD'} {name}: mine={mine_v!r} ref={ref_v!r}")

    total = (
        len(only_mine)
        + len(only_ref)
        + len(mismatches)
        + value_mismatches
        + scalar_bad
    )
    print(f"\nTOTAL DIVERGENCES: {total}")
    if total:
        print(
            "\n§8.7 — divergence must be resolved by exact-rational "
            "full-enumeration comparison. Do NOT pick the larger/smaller/mean/"
            "more-favourable value, and do not generate a completion hash.",
            file=sys.stderr,
        )
    return 0 if total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
