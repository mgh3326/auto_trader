"""ROB-339 — fast-fail classification + artifact assembly (pure, stdlib).

Discovery is a *screen*, not a verdict. Each hypothesis summary is classified into
one of three non-canonical recommendations; promote candidates are explicitly
flagged ``in_sample_only`` because the in-sample gross/fee-adjusted edge has not
survived the full walk-forward gate (OOS/baseline/overfit + bootstrap CI), which
remains the sole owner of ``validated``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

SCHEMA_VERSION = "scalping_discovery.v1"

Recommendation = Literal[
    "screened_out", "needs_more_data", "promote_to_full_validation"
]

_NON_CANONICAL_NOTE = (
    "Discovery output is non-canonical. These recommendations (screened_out / "
    "needs_more_data / promote_to_full_validation) are cheap pre-screens, NOT gate "
    "verdicts. Only the conservative validated_signal_gate.v1 (walk-forward OOS + "
    "baselines + overfit flags + bootstrap CI) produces validated / not_validated."
)


@dataclass(frozen=True)
class HypothesisSummary:
    """Bounded feature->outcome summary for one hypothesis on one symbol set.

    ``fee_adjusted_bps`` is the in-sample (decision-fold) gross expectancy minus the
    round-trip fee budget; ``oos_fee_adjusted_bps`` is the same on the held-out tail.
    ``missed_fill_ratio`` is set only for maker/passive-entry hypotheses.
    """

    name: str
    conditions: str
    sample_count: int
    gross_expectancy_bps: float
    fee_adjusted_bps: float
    oos_fee_adjusted_bps: float | None = None
    oos_gross_bps: float | None = None
    missed_fill_ratio: float | None = None
    regime: str | None = None
    time_bucket: str | None = None
    symbol: str | None = None


@dataclass(frozen=True)
class ClassifiedHypothesis:
    summary: HypothesisSummary
    recommendation: Recommendation
    reason: str
    in_sample_only: bool = False
    cost_binding: bool = False

    def to_dict(self) -> dict:
        d = asdict(self.summary)
        d.update(
            recommendation=self.recommendation,
            reason=self.reason,
            in_sample_only=self.in_sample_only,
            cost_binding=self.cost_binding,
        )
        return d


def _classify_cost_blind(
    s: HypothesisSummary, *, min_samples: int, min_gross_bps: float
) -> ClassifiedHypothesis:
    """ROB-351 cost-blind mode: screen on GROSS sign (fees=0) with a triviality
    floor; flag ``cost_binding`` when positive-gross but cost-killed.

    The deeper "is the cost gap closable by maker execution" test is NOT done
    here — that lives in the maker_fill pure path (ROB-351 Issue 3 / Stage 2).
    This is the cheap screen-level 343 signal only.
    """
    if s.sample_count < min_samples:
        return ClassifiedHypothesis(
            s, "needs_more_data",
            f"sample_count {s.sample_count} < min_samples {min_samples}",
        )
    if s.gross_expectancy_bps <= min_gross_bps:
        return ClassifiedHypothesis(
            s, "screened_out",
            f"gross expectancy {s.gross_expectancy_bps:.2f}bps <= triviality floor "
            f"{min_gross_bps:.2f}bps (no economically meaningful gross edge)",
        )
    if s.oos_gross_bps is not None and s.oos_gross_bps <= 0:
        return ClassifiedHypothesis(
            s, "screened_out",
            f"OOS gross expectancy {s.oos_gross_bps:.2f}bps <= 0 "
            "(in-sample gross edge does not hold out of sample)",
        )
    cost_binding = s.fee_adjusted_bps <= 0
    note = ("positive gross, killed by fees -> cost_binding 343 signal"
            if cost_binding else "positive gross AND net-viable after fees")
    return ClassifiedHypothesis(
        s, "promote_to_full_validation",
        f"cost-blind: gross {s.gross_expectancy_bps:.2f}bps > floor "
        f"{min_gross_bps:.2f}bps with {s.sample_count} samples; {note}",
        in_sample_only=True,
        cost_binding=cost_binding,
    )


def classify(
    summary: HypothesisSummary,
    *,
    min_samples: int = 200,
    missed_fill_max: float = 0.6,
    cost_blind: bool = False,
    min_gross_bps: float = 0.0,
) -> ClassifiedHypothesis:
    """Apply the fast-fail rules in priority order (see design §6).

    ``cost_blind=True`` (ROB-351) screens on gross sign with ``min_gross_bps`` as
    an economic-triviality floor and flags ``cost_binding``; default mode is the
    unchanged ROB-339 fee-adjusted screen.
    """
    s = summary
    if cost_blind:
        return _classify_cost_blind(s, min_samples=min_samples, min_gross_bps=min_gross_bps)
    if s.sample_count < min_samples:
        return ClassifiedHypothesis(
            s,
            "needs_more_data",
            f"sample_count {s.sample_count} < min_samples {min_samples}",
        )
    if s.fee_adjusted_bps <= 0:
        return ClassifiedHypothesis(
            s,
            "screened_out",
            f"fee-adjusted expectancy {s.fee_adjusted_bps:.2f}bps <= 0 "
            f"(gross {s.gross_expectancy_bps:.2f}bps does not clear the fee budget)",
        )
    if s.missed_fill_ratio is not None and s.missed_fill_ratio > missed_fill_max:
        return ClassifiedHypothesis(
            s,
            "screened_out",
            f"missed-fill ratio {s.missed_fill_ratio:.2f} > {missed_fill_max:.2f} "
            "(passive entry loses too many fills)",
        )
    if s.oos_fee_adjusted_bps is not None and s.oos_fee_adjusted_bps <= 0:
        return ClassifiedHypothesis(
            s,
            "screened_out",
            f"OOS-tail fee-adjusted expectancy {s.oos_fee_adjusted_bps:.2f}bps <= 0 "
            "(in-sample edge does not hold out of sample)",
        )
    return ClassifiedHypothesis(
        s,
        "promote_to_full_validation",
        f"in-sample fee-adjusted {s.fee_adjusted_bps:.2f}bps > 0 with "
        f"{s.sample_count} samples; OOS-tail sign agrees",
        in_sample_only=True,
    )


def build_artifact(classified: list[ClassifiedHypothesis], run: dict) -> dict:
    """Assemble the ``scalping_discovery.v1`` artifact dict (pure; caller writes it)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run": run,
        "hypotheses_tested": len(classified),
        "hypotheses": [c.to_dict() for c in classified],
        "note": _NON_CANONICAL_NOTE,
    }
