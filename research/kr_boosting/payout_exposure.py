"""How much of a result rests on terminal payouts U2 does not verify.

The base case settles a delisted position at its last close, whatever the payout
was, so a settled value is the most a wrong payout assumption can take from that
position. The dots audit of ``delist_resolution.v1`` (ROB-1437) confirmed symbol,
delist date and event class on a sample, but not payouts, and named rows to treat
with care. This module counts those positions from the contract fills; it changes
no label and no number the gate reads.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from research.eval_contract.contract import FillModelResult, Side

WEAK_PAYOUT = frozenset({"003415"})
COMPOUND_REASON = frozenset({"002250"})
DISSOLUTION_WORD = "해산"
TERMINAL_PREFIX = "terminal:"


@dataclass(frozen=True, slots=True)
class FlaggedSymbols:
    weak_payout: frozenset[str]
    compound_reason: frozenset[str]
    dissolution_label: frozenset[str]
    same_code_relisting: frozenset[str]

    @property
    def all(self) -> frozenset[str]:
        return (
            self.weak_payout
            | self.compound_reason
            | self.dissolution_label
            | self.same_code_relisting
        )


def load_flags(export_dir: Path) -> FlaggedSymbols:
    """The audit's caveats as symbols, from the hash-checked resolution table."""

    path = export_dir / "delist_resolution.v1.csv"
    entry = json.loads((export_dir / "manifest.json").read_text())["files"][
        "delist_resolution"
    ]
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != entry["sha256"]:
        raise ValueError(f"{path.name} sha256 {actual} != manifest {entry['sha256']}")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    counts = Counter(row["symbol"] for row in rows)
    return FlaggedSymbols(
        weak_payout=WEAK_PAYOUT,
        compound_reason=COMPOUND_REASON,
        dissolution_label=frozenset(
            row["symbol"]
            for row in rows
            if DISSOLUTION_WORD in (row["delist_reason"] or "")
        ),
        same_code_relisting=frozenset(s for s, n in counts.items() if n > 1),
    )


@dataclass(frozen=True, slots=True)
class Exposure:
    settlements: int
    settled_value: Decimal
    flagged_settlements: int
    flagged_value: Decimal
    flagged_symbols: tuple[str, ...]

    def bound_annual(self, capital: Decimal, years: float) -> float:
        """Annual return at stake if every settled position had paid nothing."""

        return float(self.settled_value / capital) / years

    def flagged_bound_annual(self, capital: Decimal, years: float) -> float:
        return float(self.flagged_value / capital) / years


def exposure(result: FillModelResult, flags: FlaggedSymbols) -> Exposure:
    """Terminal settlements in one simulated run, split by the audit's flags."""

    settled = [
        fill
        for fill in result.fills
        if fill.side is Side.SELL
        and (fill.correlation_id or "").startswith(TERMINAL_PREFIX)
    ]
    flagged = [fill for fill in settled if fill.symbol in flags.all]
    return Exposure(
        settlements=len(settled),
        settled_value=sum((f.filled_notional for f in settled), Decimal(0)),
        flagged_settlements=len(flagged),
        flagged_value=sum((f.filled_notional for f in flagged), Decimal(0)),
        flagged_symbols=tuple(sorted({f.symbol for f in flagged})),
    )


def exposure_table(
    policies: Mapping[str, Mapping[str, FillModelResult]],
    *,
    order: tuple[str, ...],
    model_id: str,
    flags: FlaggedSymbols,
    capital: Decimal,
    years: float,
) -> list[str]:
    """Markdown rows: settled exposure per policy at one fill end."""

    lines = [
        "| policy | settled at last close | value, KRW | if all paid nothing, annual | flagged rows settled | flagged value, KRW | if flagged paid nothing, annual | flagged symbols |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name in order:
        e = exposure(policies[name][model_id], flags)
        symbols = ", ".join(e.flagged_symbols) or "none"
        lines.append(
            f"| {name} | {e.settlements} | {int(e.settled_value):,} | "
            f"{-e.bound_annual(capital, years) * 100:.2f}% | {e.flagged_settlements} | "
            f"{int(e.flagged_value):,} | {-e.flagged_bound_annual(capital, years) * 100:.2f}% | "
            f"{symbols} |"
        )
    return lines
