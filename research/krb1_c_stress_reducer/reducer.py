"""Sealed §6.2.1 exact-rational exhaustive reference implementation."""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from research.krb1_c_stress_reducer.model import (
    AMENDMENT_CANONICAL_SHA256,
    MARKETS,
    PARENT_CANONICAL_SHA256,
    ContractError,
    CostInputs,
    MarketRates,
    MarketTickTable,
    TickTables,
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
)

MIN_PRICE = 5_000
MAX_PRICE = 400_000
BP_SCALE = 10_000
REDUCER_SPEC_ID = "KRB1C-CSTRESS-REDUCER-v1"
CANDIDATE_FIELDS = (
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
SOURCE_RELATIVE_PATHS = (
    "research/krb1_c_stress_reducer/__init__.py",
    "research/krb1_c_stress_reducer/model.py",
    "research/krb1_c_stress_reducer/reducer.py",
    "research/krb1_c_stress_reducer/cli.py",
    "scripts/krb1_c_stress_reducer.py",
)


@dataclass(frozen=True)
class Candidate:
    market: str
    entry_price: int
    entry_tick: int
    rho_entry: Fraction
    exit_witness_price: int
    exit_witness_tick: int
    rho_exit: Fraction
    entry_multiplier: Fraction
    exit_multiplier: Fraction
    cost_rate: Fraction
    target_price: int = 0
    target_check_passed: bool = False

    def csv_row(self) -> dict[str, str | int]:
        return {
            "market": self.market,
            "entry_price": self.entry_price,
            "entry_tick": self.entry_tick,
            "rho_entry_num": self.rho_entry.numerator,
            "rho_entry_den": self.rho_entry.denominator,
            "exit_witness_price": self.exit_witness_price,
            "exit_witness_tick": self.exit_witness_tick,
            "rho_exit_num": self.rho_exit.numerator,
            "rho_exit_den": self.rho_exit.denominator,
            "entry_multiplier_num": self.entry_multiplier.numerator,
            "entry_multiplier_den": self.entry_multiplier.denominator,
            "exit_multiplier_num": self.exit_multiplier.numerator,
            "exit_multiplier_den": self.exit_multiplier.denominator,
            "c_num": self.cost_rate.numerator,
            "c_den": self.cost_rate.denominator,
            "target_price": self.target_price,
            "target_check_passed": ("true" if self.target_check_passed else "false"),
        }


@dataclass(frozen=True)
class MarketSummary:
    market: str
    candidate_count: int
    first_price: int
    last_price: int
    first_price_after_cap: int
    raw_cost_rate: Fraction
    witness_entry_price: int
    witness_exit_price: int


@dataclass(frozen=True)
class ReducerRun:
    candidates: tuple[Candidate, ...]
    market_summaries: dict[str, MarketSummary]
    raw_cost_rate: Fraction
    witness_market: str
    witness_entry_price: int
    witness_exit_price: int
    cap_bp: int
    cap: Fraction


def enumerate_valid_prices(table: MarketTickTable) -> tuple[tuple[int, ...], int]:
    prices: list[int] = []
    price = table.tick_ceil(Fraction(MIN_PRICE))
    while price <= MAX_PRICE:
        if prices and price <= prices[-1]:
            raise ContractError(f"{table.market} E_m is not strictly increasing")
        if price % table.tick(price) != 0:
            raise ContractError(f"{table.market} E_m contains invalid price {price}")
        prices.append(price)
        price = table.tick_ceil(Fraction(price + 1))
    if not prices:
        raise ContractError(f"{table.market} E_m is empty")
    return tuple(prices), price


def _exit_ratio_and_witness(
    table: MarketTickTable, entry_price: int
) -> tuple[Fraction, int, int]:
    points = {entry_price}
    for band in table.bands:
        if band.lower > entry_price:
            points.add(table.tick_ceil(Fraction(band.lower)))
    best_ratio: Fraction | None = None
    best_price = 0
    best_tick = 0
    for price in sorted(points):
        tick = table.tick(price)
        ratio = Fraction(tick, price)
        if best_ratio is None or ratio > best_ratio:
            best_ratio = ratio
            best_price = price
            best_tick = tick
    if best_ratio is None:
        raise ContractError(f"{table.market} exit witness set is empty")
    return best_ratio, best_price, best_tick


def _candidate(
    market: str,
    entry_price: int,
    table: MarketTickTable,
    rates: MarketRates,
) -> Candidate:
    entry_tick = table.tick(entry_price)
    rho_entry = Fraction(entry_tick, entry_price)
    rho_exit, exit_witness_price, exit_witness_tick = _exit_ratio_and_witness(
        table, entry_price
    )
    entry_multiplier = 1 + rates.buy_commission + rho_entry
    exit_multiplier = 1 - rates.sell_commission - rates.sell_tax - rho_exit
    if exit_multiplier <= 0:
        raise ContractError(
            f"{market} exit_multiplier_cap is not positive at P={entry_price}"
        )
    cost_rate = entry_multiplier / exit_multiplier - 1
    return Candidate(
        market=market,
        entry_price=entry_price,
        entry_tick=entry_tick,
        rho_entry=rho_entry,
        exit_witness_price=exit_witness_price,
        exit_witness_tick=exit_witness_tick,
        rho_exit=rho_exit,
        entry_multiplier=entry_multiplier,
        exit_multiplier=exit_multiplier,
        cost_rate=cost_rate,
    )


def _market_candidates(
    market: str, table: MarketTickTable, rates: MarketRates
) -> tuple[list[Candidate], MarketSummary]:
    prices, first_after_cap = enumerate_valid_prices(table)
    candidates = [
        _candidate(market, entry_price, table, rates) for entry_price in prices
    ]
    worst = candidates[0]
    for candidate in candidates[1:]:
        if candidate.cost_rate > worst.cost_rate:
            worst = candidate
    return candidates, MarketSummary(
        market=market,
        candidate_count=len(candidates),
        first_price=prices[0],
        last_price=prices[-1],
        first_price_after_cap=first_after_cap,
        raw_cost_rate=worst.cost_rate,
        witness_entry_price=worst.entry_price,
        witness_exit_price=worst.exit_witness_price,
    )


def _target_check(
    candidate: Candidate,
    table: MarketTickTable,
    rates: MarketRates,
    cap: Fraction,
) -> Candidate:
    target = table.tick_ceil(Fraction(candidate.entry_price) * (1 + cap))
    left = Fraction(target) * (
        1
        - rates.sell_commission
        - rates.sell_tax
        - Fraction(table.tick(target), target)
    )
    right = Fraction(candidate.entry_price) * (
        1 + rates.buy_commission + candidate.rho_entry
    )
    passed = left >= right
    if not passed:
        raise ContractError(
            f"§6.9 target check failed: market={candidate.market} "
            f"P={candidate.entry_price} T={target}"
        )
    return replace(
        candidate,
        target_price=target,
        target_check_passed=True,
    )


def run_reducer(cost_inputs: CostInputs, tick_tables: TickTables) -> ReducerRun:
    candidates: list[Candidate] = []
    summaries: dict[str, MarketSummary] = {}
    for market in MARKETS:
        market_candidates, summary = _market_candidates(
            market,
            tick_tables.markets[market],
            cost_inputs.rates[market],
        )
        candidates.extend(market_candidates)
        summaries[market] = summary

    witness_market = "KOSPI"
    for market in MARKETS:
        if summaries[market].raw_cost_rate > summaries[witness_market].raw_cost_rate:
            witness_market = market
    raw = summaries[witness_market].raw_cost_rate
    cap_bp = (BP_SCALE * raw.numerator + raw.denominator - 1) // raw.denominator
    cap = Fraction(cap_bp, BP_SCALE)
    checked = tuple(
        _target_check(
            candidate,
            tick_tables.markets[candidate.market],
            cost_inputs.rates[candidate.market],
            cap,
        )
        for candidate in candidates
    )
    witness = summaries[witness_market]
    return ReducerRun(
        candidates=checked,
        market_summaries=summaries,
        raw_cost_rate=raw,
        witness_market=witness_market,
        witness_entry_price=witness.witness_entry_price,
        witness_exit_price=witness.witness_exit_price,
        cap_bp=cap_bp,
        cap=cap,
    )


def _candidate_csv_bytes(candidates: tuple[Candidate, ...]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=CANDIDATE_FIELDS,
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(candidate.csv_row() for candidate in candidates)
    return buffer.getvalue().encode("utf-8")


def _decimal_string(cap_bp: int) -> str:
    return f"{cap_bp // BP_SCALE}.{cap_bp % BP_SCALE:04d}"


def _source_manifest(repo_root: Path) -> tuple[list[dict[str, str]], str]:
    manifest: list[dict[str, str]] = []
    combined = hashlib.sha256()
    for relative in SOURCE_RELATIVE_PATHS:
        path = repo_root / relative
        digest = sha256_file(path)
        manifest.append({"path": relative, "sha256": digest})
        combined.update(relative.encode("utf-8"))
        combined.update(b"\0")
        combined.update(path.read_bytes())
        combined.update(b"\0")
    return manifest, combined.hexdigest()


def _verify_sealed_canonicals(
    parent_canonical_path: Path, amendment_canonical_path: Path
) -> None:
    if sha256_file(parent_canonical_path) != PARENT_CANONICAL_SHA256:
        raise ContractError("sealed parent canonical file SHA-256 mismatch")
    if sha256_file(amendment_canonical_path) != AMENDMENT_CANONICAL_SHA256:
        raise ContractError("sealed amendment canonical file SHA-256 mismatch")


def write_artifacts(
    run: ReducerRun,
    cost_inputs: CostInputs,
    tick_tables: TickTables,
    output_dir: Path,
    repo_root: Path,
    parent_canonical_path: Path,
    amendment_canonical_path: Path,
) -> dict[str, object]:
    _verify_sealed_canonicals(parent_canonical_path, amendment_canonical_path)

    normalized_bytes = canonical_json_bytes(cost_inputs.raw)
    tick_input_bytes = canonical_json_bytes(tick_tables.raw)
    candidates_bytes = _candidate_csv_bytes(run.candidates)
    source_manifest, source_sha256 = _source_manifest(repo_root)
    summaries = {
        market: {
            "candidate_count": summary.candidate_count,
            "first_price": summary.first_price,
            "last_price": summary.last_price,
            "first_price_after_cap": summary.first_price_after_cap,
            "c_raw_num": summary.raw_cost_rate.numerator,
            "c_raw_den": summary.raw_cost_rate.denominator,
            "witness_entry_price": summary.witness_entry_price,
            "witness_exit_price": summary.witness_exit_price,
        }
        for market, summary in run.market_summaries.items()
    }
    result: dict[str, object] = {
        "reducer_spec_id": REDUCER_SPEC_ID,
        "arithmetic": "fractions.Fraction+integer",
        "float_used": False,
        "parent_canonical_sha256": PARENT_CANONICAL_SHA256,
        "amendment_canonical_sha256": AMENDMENT_CANONICAL_SHA256,
        "p0_2_cost_inputs_normalized_sha256": sha256_bytes(normalized_bytes),
        "p0_1_tick_tables_normalized_sha256": sha256_bytes(tick_input_bytes),
        "c_stress_candidates_sha256": sha256_bytes(candidates_bytes),
        "reducer_source_sha256": source_sha256,
        "reducer_source_manifest": source_manifest,
        "market_summaries": summaries,
        "candidate_count_total": len(run.candidates),
        "c_raw_num": run.raw_cost_rate.numerator,
        "c_raw_den": run.raw_cost_rate.denominator,
        "witness_market": run.witness_market,
        "witness_entry_price": run.witness_entry_price,
        "witness_exit_price": run.witness_exit_price,
        "c_stress_cap_bp": run.cap_bp,
        "c_stress_cap_decimal": _decimal_string(run.cap_bp),
        "all_target_checks_passed": all(
            candidate.target_check_passed for candidate in run.candidates
        ),
        "p0_2_completion_hash": None,
        "p0_2_completion_state": "AWAITING_INDEPENDENT_REPRODUCTION",
    }
    result_bytes = canonical_json_bytes(result)
    if output_dir.exists():
        raise ContractError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "p0_2_cost_inputs.normalized.json").write_bytes(normalized_bytes)
    (output_dir / "c_stress_candidates.csv").write_bytes(candidates_bytes)
    (output_dir / "c_stress_reducer_result.json").write_bytes(result_bytes)
    return result
