#!/usr/bin/env python3
"""Independent integer-arithmetic reproduction of the sealed KR-B1c reducer.

This executable intentionally does not import the reference reducer. It enumerates
valid prices directly from bands and represents every rational as a reduced integer
pair.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from math import gcd
from pathlib import Path
from typing import Any

MARKETS = ("KOSPI", "KOSDAQ")
RATE_SCALE = 1_000_000_000_000
BP_SCALE = 10_000
MIN_PRICE = 5_000
MAX_PRICE = 400_000
FIELDS = (
    "market",
    "entry_price",
    "entry_tick",
    "rho_entry_num",
    "rho_entry_den",
    "exit_witness_price",
    "exit_witness_tick",
    "rho_exit_num",
    "rho_exit_den",
    "entry_multiplier_num",
    "entry_multiplier_den",
    "exit_multiplier_num",
    "exit_multiplier_den",
    "c_num",
    "c_den",
    "target_price",
    "target_check_passed",
)


class VerificationError(ValueError):
    """Independent reproduction failed."""


def reject_float(_: str) -> None:
    raise VerificationError("JSON floating-point numbers are prohibited")


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_float=reject_float,
            parse_constant=reject_float,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"{path} must contain an object")
    return value


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def ratio(numerator: int, denominator: int = 1) -> tuple[int, int]:
    if denominator == 0:
        raise VerificationError("zero rational denominator")
    if denominator < 0:
        numerator = -numerator
        denominator = -denominator
    divisor = gcd(abs(numerator), denominator)
    return numerator // divisor, denominator // divisor


def add(left: tuple[int, int], right: tuple[int, int]) -> tuple[int, int]:
    return ratio(
        left[0] * right[1] + right[0] * left[1],
        left[1] * right[1],
    )


def subtract(left: tuple[int, int], right: tuple[int, int]) -> tuple[int, int]:
    return ratio(
        left[0] * right[1] - right[0] * left[1],
        left[1] * right[1],
    )


def multiply(left: tuple[int, int], right: tuple[int, int]) -> tuple[int, int]:
    return ratio(left[0] * right[0], left[1] * right[1])


def divide(left: tuple[int, int], right: tuple[int, int]) -> tuple[int, int]:
    return ratio(left[0] * right[1], left[1] * right[0])


def greater(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] * right[1] > right[0] * left[1]


def greater_equal(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] * right[1] >= right[0] * left[1]


def positive(value: tuple[int, int]) -> bool:
    return value[0] > 0


def bands_for(tick_input: dict[str, Any], market: str) -> list[dict[str, Any]]:
    markets = tick_input.get("markets")
    if not isinstance(markets, dict):
        raise VerificationError("tick input markets is malformed")
    market_value = markets.get(market)
    if not isinstance(market_value, dict):
        raise VerificationError(f"missing tick table for {market}")
    bands = market_value.get("bands")
    if not isinstance(bands, list) or not bands:
        raise VerificationError(f"missing tick bands for {market}")
    if not all(isinstance(band, dict) for band in bands):
        raise VerificationError(f"malformed tick band for {market}")
    return bands


def tick_at(bands: list[dict[str, Any]], price: int) -> int:
    for band in bands:
        lower = band["lower"]
        upper = band["upper_exclusive"]
        if price >= lower and (upper is None or price < upper):
            return band["tick"]
    raise VerificationError(f"no tick band at price {price}")


def aligned_ceiling(integer: int, step: int) -> int:
    return ((integer + step - 1) // step) * step


def tick_ceil_ratio(bands: list[dict[str, Any]], value: tuple[int, int]) -> int:
    integer = (value[0] + value[1] - 1) // value[1]
    for band in bands:
        candidate = max(integer, band["lower"])
        aligned = aligned_ceiling(candidate, band["tick"])
        upper = band["upper_exclusive"]
        if upper is None or aligned < upper:
            return aligned
    raise VerificationError("tick table is not open-ended")


def direct_prices(bands: list[dict[str, Any]]) -> list[int]:
    prices: list[int] = []
    for band in bands:
        lower = max(MIN_PRICE, band["lower"])
        upper_raw = band["upper_exclusive"]
        upper = MAX_PRICE + 1 if upper_raw is None else min(MAX_PRICE + 1, upper_raw)
        first = aligned_ceiling(lower, band["tick"])
        if first < upper:
            prices.extend(range(first, upper, band["tick"]))
    if not prices or prices != sorted(set(prices)):
        raise VerificationError("direct E_m enumeration is empty or non-bijective")
    return prices


def independent_exit_witness(
    bands: list[dict[str, Any]], entry_price: int
) -> tuple[tuple[int, int], int, int]:
    best: tuple[int, int] | None = None
    best_price = 0
    best_tick = 0
    for band in bands:
        lower = max(entry_price, band["lower"])
        price = aligned_ceiling(lower, band["tick"])
        upper = band["upper_exclusive"]
        if upper is not None and price >= upper:
            continue
        candidate = ratio(band["tick"], price)
        if (
            best is None
            or greater(candidate, best)
            or (candidate == best and price < best_price)
        ):
            best = candidate
            best_price = price
            best_tick = band["tick"]
    if best is None:
        raise VerificationError("independent exit witness search is empty")
    return best, best_price, best_tick


def market_rates(
    cost_input: dict[str, Any], market: str
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    records = cost_input.get("market_cost_records")
    if not isinstance(records, list):
        raise VerificationError("market_cost_records is malformed")
    selected = [
        record
        for record in records
        if isinstance(record, dict) and record.get("market") == market
    ]
    if not selected:
        raise VerificationError(f"no cost records for {market}")
    buy = max(record["buy_commission_rate_e12"] for record in selected)
    sell = max(record["sell_commission_rate_e12"] for record in selected)
    tax = max(
        sum(component["rate_e12"] for component in record["sell_tax_components"])
        for record in selected
    )
    return ratio(buy, RATE_SCALE), ratio(sell, RATE_SCALE), ratio(tax, RATE_SCALE)


def expected_rows(
    cost_input: dict[str, Any], tick_input: dict[str, Any]
) -> tuple[
    list[dict[str, str]],
    dict[str, dict[str, int]],
    tuple[int, int],
    str,
    int,
    int,
    int,
]:
    pending: list[
        tuple[
            str,
            int,
            int,
            tuple[int, int],
            int,
            int,
            tuple[int, int],
            tuple[int, int],
            tuple[int, int],
            tuple[int, int],
        ]
    ] = []
    summaries: dict[str, dict[str, int]] = {}
    market_worst: dict[str, tuple[tuple[int, int], int, int]] = {}
    for market in MARKETS:
        bands = bands_for(tick_input, market)
        prices = direct_prices(bands)
        buy, sell, tax = market_rates(cost_input, market)
        worst: tuple[int, int] | None = None
        worst_entry = 0
        worst_exit = 0
        for entry_price in prices:
            entry_tick = tick_at(bands, entry_price)
            rho_entry = ratio(entry_tick, entry_price)
            rho_exit, exit_price, exit_tick = independent_exit_witness(
                bands, entry_price
            )
            entry_multiplier = add(add(ratio(1), buy), rho_entry)
            exit_multiplier = subtract(
                subtract(subtract(ratio(1), sell), tax), rho_exit
            )
            if not positive(exit_multiplier):
                raise VerificationError(
                    f"nonpositive exit multiplier at {market}/{entry_price}"
                )
            cost_rate = subtract(divide(entry_multiplier, exit_multiplier), ratio(1))
            pending.append(
                (
                    market,
                    entry_price,
                    entry_tick,
                    rho_entry,
                    exit_price,
                    exit_tick,
                    rho_exit,
                    entry_multiplier,
                    exit_multiplier,
                    cost_rate,
                )
            )
            if worst is None or greater(cost_rate, worst):
                worst = cost_rate
                worst_entry = entry_price
                worst_exit = exit_price
        if worst is None:
            raise VerificationError(f"no candidates for {market}")
        first_after_cap = tick_ceil_ratio(bands, ratio(MAX_PRICE + 1))
        summaries[market] = {
            "candidate_count": len(prices),
            "first_price": prices[0],
            "last_price": prices[-1],
            "first_price_after_cap": first_after_cap,
            "c_raw_num": worst[0],
            "c_raw_den": worst[1],
            "witness_entry_price": worst_entry,
            "witness_exit_price": worst_exit,
        }
        market_worst[market] = (worst, worst_entry, worst_exit)

    witness_market = "KOSPI"
    if greater(market_worst["KOSDAQ"][0], market_worst["KOSPI"][0]):
        witness_market = "KOSDAQ"
    raw, witness_entry, witness_exit = market_worst[witness_market]
    cap_bp = (BP_SCALE * raw[0] + raw[1] - 1) // raw[1]
    cap = ratio(cap_bp, BP_SCALE)

    rows: list[dict[str, str]] = []
    for (
        market,
        entry_price,
        entry_tick,
        rho_entry,
        exit_price,
        exit_tick,
        rho_exit,
        entry_multiplier,
        exit_multiplier,
        cost_rate,
    ) in pending:
        bands = bands_for(tick_input, market)
        buy, sell, tax = market_rates(cost_input, market)
        target = tick_ceil_ratio(
            bands,
            multiply(ratio(entry_price), add(ratio(1), cap)),
        )
        left = multiply(
            ratio(target),
            subtract(
                subtract(
                    subtract(ratio(1), sell),
                    tax,
                ),
                ratio(tick_at(bands, target), target),
            ),
        )
        right = multiply(
            ratio(entry_price),
            add(add(ratio(1), buy), rho_entry),
        )
        if not greater_equal(left, right):
            raise VerificationError(
                f"independent target check failed at {market}/{entry_price}"
            )
        rows.append(
            {
                "market": market,
                "entry_price": str(entry_price),
                "entry_tick": str(entry_tick),
                "rho_entry_num": str(rho_entry[0]),
                "rho_entry_den": str(rho_entry[1]),
                "exit_witness_price": str(exit_price),
                "exit_witness_tick": str(exit_tick),
                "rho_exit_num": str(rho_exit[0]),
                "rho_exit_den": str(rho_exit[1]),
                "entry_multiplier_num": str(entry_multiplier[0]),
                "entry_multiplier_den": str(entry_multiplier[1]),
                "exit_multiplier_num": str(exit_multiplier[0]),
                "exit_multiplier_den": str(exit_multiplier[1]),
                "c_num": str(cost_rate[0]),
                "c_den": str(cost_rate[1]),
                "target_price": str(target),
                "target_check_passed": "true",
            }
        )
    return (
        rows,
        summaries,
        raw,
        witness_market,
        witness_entry,
        witness_exit,
        cap_bp,
    )


def compare_candidates(candidate_path: Path, expected: list[dict[str, str]]) -> None:
    with candidate_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise VerificationError("candidate CSV 17-field schema mismatch")
        for index, expected_row in enumerate(expected):
            actual = next(reader, None)
            if actual is None:
                raise VerificationError(f"candidate CSV ended before row {index}")
            if actual != expected_row:
                differing = [
                    field
                    for field in FIELDS
                    if actual.get(field) != expected_row[field]
                ]
                raise VerificationError(
                    f"candidate mismatch at row {index}: fields={differing}"
                )
        if next(reader, None) is not None:
            raise VerificationError("candidate CSV contains extra rows")


def boundary_checks(tick_input: dict[str, Any]) -> dict[str, list[dict[str, int]]]:
    output: dict[str, list[dict[str, int]]] = {}
    for market in MARKETS:
        bands = bands_for(tick_input, market)
        checks: list[dict[str, int]] = []
        for band in bands[1:]:
            lower = band["lower"]
            checks.append(
                {
                    "lower": lower,
                    "tick_below": tick_at(bands, lower - 1),
                    "tick_at": tick_at(bands, lower),
                    "tick_above": tick_at(bands, lower + 1),
                }
            )
        final = bands[-1]
        open_probe = final["lower"] + 1_234_567
        checks.append(
            {
                "open_ended_probe": open_probe,
                "tick": tick_at(bands, open_probe),
            }
        )
        output[market] = checks
    return output


def verify_source_manifest(result: dict[str, Any], repo_root: Path) -> None:
    manifest = result.get("reducer_source_manifest")
    if not isinstance(manifest, list) or not manifest:
        raise VerificationError("result reducer_source_manifest is malformed")
    combined = hashlib.sha256()
    for item in manifest:
        if not isinstance(item, dict):
            raise VerificationError("source manifest entry is malformed")
        relative = item.get("path")
        expected_sha = item.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected_sha, str):
            raise VerificationError("source manifest fields are malformed")
        path = repo_root / relative
        if file_digest(path) != expected_sha:
            raise VerificationError(f"reference source drift: {relative}")
        combined.update(relative.encode("utf-8"))
        combined.update(b"\0")
        combined.update(path.read_bytes())
        combined.update(b"\0")
    if combined.hexdigest() != result.get("reducer_source_sha256"):
        raise VerificationError("combined reducer source SHA-256 mismatch")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Independently reproduce all KR-B1c exact-rational candidates."
    )
    parser.add_argument("--cost-input", type=Path, required=True)
    parser.add_argument("--tick-input", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--verification-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cost_input = read_object(args.cost_input)
        tick_input = read_object(args.tick_input)
        result = read_object(args.result)
        verify_source_manifest(result, args.repo_root.resolve())
        (
            expected,
            summaries,
            raw,
            witness_market,
            witness_entry,
            witness_exit,
            cap_bp,
        ) = expected_rows(cost_input, tick_input)
        compare_candidates(args.candidates, expected)
        if result.get("p0_2_cost_inputs_normalized_sha256") != digest(
            canonical(cost_input)
        ):
            raise VerificationError("normalized P0-2 input SHA-256 mismatch")
        if result.get("p0_1_tick_tables_normalized_sha256") != digest(
            canonical(tick_input)
        ):
            raise VerificationError("normalized P0-1 tick SHA-256 mismatch")
        if result.get("c_stress_candidates_sha256") != file_digest(args.candidates):
            raise VerificationError("candidate CSV SHA-256 mismatch")
        expected_result_fields = {
            "market_summaries": summaries,
            "candidate_count_total": len(expected),
            "c_raw_num": raw[0],
            "c_raw_den": raw[1],
            "witness_market": witness_market,
            "witness_entry_price": witness_entry,
            "witness_exit_price": witness_exit,
            "c_stress_cap_bp": cap_bp,
            "c_stress_cap_decimal": (f"{cap_bp // BP_SCALE}.{cap_bp % BP_SCALE:04d}"),
            "all_target_checks_passed": True,
        }
        for field, expected_value in expected_result_fields.items():
            if result.get(field) != expected_value:
                raise VerificationError(f"result field mismatch: {field}")
        report: dict[str, object] = {
            "status": "PASS",
            "implementation_independence": (
                "standalone; no reference reducer imports; direct band enumeration; "
                "reduced integer pairs"
            ),
            "arithmetic": "integer cross-products+gcd",
            "float_used": False,
            "candidate_rows_compared": len(expected),
            "fraction_rows_matched": len(expected),
            "witness_rows_matched": len(expected),
            "target_rows_matched": len(expected),
            "market_summaries": summaries,
            "c_raw_num": raw[0],
            "c_raw_den": raw[1],
            "witness_market": witness_market,
            "witness_entry_price": witness_entry,
            "witness_exit_price": witness_exit,
            "c_stress_cap_bp": cap_bp,
            "c_stress_cap_decimal": (f"{cap_bp // BP_SCALE}.{cap_bp % BP_SCALE:04d}"),
            "boundary_checks": boundary_checks(tick_input),
            "reference_result_sha256": file_digest(args.result),
            "candidate_csv_sha256": file_digest(args.candidates),
            "independent_source_sha256": file_digest(Path(__file__)),
            "p0_2_completion_hash_created": False,
            "p0_2_completion_hash_reason": (
                "completion hash format is not specified and fixture is not sealed P0 input"
            ),
        }
        report_bytes = canonical(report)
        if args.verification_output is not None:
            args.verification_output.parent.mkdir(parents=True, exist_ok=True)
            try:
                with args.verification_output.open("xb") as handle:
                    handle.write(report_bytes)
            except FileExistsError as exc:
                raise VerificationError(
                    f"verification output already exists: {args.verification_output}"
                ) from exc
        print(report_bytes.decode("utf-8"))
        return 0
    except (VerificationError, KeyError, TypeError) as exc:
        print(f"KR-B1c independent reproduction FAIL: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
