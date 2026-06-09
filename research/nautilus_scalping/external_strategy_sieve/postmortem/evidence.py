"""ROB-384 — unified candidate-evidence record + documented registry.

``CandidateEvidence`` is the counts-only record every adapter produces. It holds
*derived numbers only* (gross/net bps, sample/fold counts, verdict, t-stats),
never raw market data and never secrets.

Provenance is a first-class field:

* ``source="reparsed"``  — numbers read directly from a local result JSON whose
  path is recorded in ``citation``. Machine-verifiable.
* ``source="documented"`` — numbers hand-curated from a report / Linear / design
  doc, with the exact source quoted in ``citation``. Kept strictly distinct from
  reparsed numbers and never silently mixed.

ROB-384 refinement of the issue's premise: the issue assumed ROB-342/353/382
were all documented-only. In fact ROB-353 (``rob351_campaign.v1.json``) and
ROB-382 (``rob382_falsification.v1.json``) have local result JSONs and are
re-parsed. Only **ROB-342** is genuinely documented-only, and even its detailed
"negative even at 0 bps / n>=263" closure number could not be traced to a live
Linear comment or in-repo doc as of 2026-05-31 — so that specific number is
recorded as an explicitly *uncited* note (``memory_only_uncited``) and is **not**
used as a quantitative field. Only the citable ROB-339 smoke figures quoted in
the ROB-342 issue description back the documented record.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SOURCES = frozenset({"reparsed", "documented"})


@dataclass
class CandidateEvidence:
    """One strategy candidate's counts-only evidence row."""

    issue: str  # e.g. "ROB-320"
    candidate: str  # e.g. "meanrev_zscore_fade"
    family: str  # short human family label
    source: str  # "reparsed" | "documented"
    schema: str  # source schema id / "documented"
    citation: str  # local path or quoted source pointer

    gross_bps: float | None = None  # per-trade gross edge (fee = 0)
    net_bps_by_fee: dict[str, float] = field(default_factory=dict)  # fee -> net bps
    net_moot_reason: str = ""  # set when net grid is N/A (gross <= 0)

    trade_count: int | None = None
    oos_trade_count: int | None = None
    n_folds: int | None = None
    fold_net: dict[str, float] = field(default_factory=dict)  # train/val/oos net pnl
    single_fold_edge: bool | None = None  # gross edge concentrated in one fold

    t_stat_gross: float | None = None
    t_stat_oos: float | None = None

    verdict: str = ""  # source verdict / class string
    baseline_beat: dict[str, bool] = field(default_factory=dict)  # baseline -> beaten?
    baseline_note: str = ""

    failure_modes: list[str] = field(default_factory=list)
    notes: str = ""

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(
                f"unknown source {self.source!r}; expected one of {sorted(SOURCES)}"
            )

    def to_row(self) -> dict:
        """Flat dict for CSV / JSON emit (counts only)."""
        return {
            "issue": self.issue,
            "candidate": self.candidate,
            "family": self.family,
            "source": self.source,
            "schema": self.schema,
            "gross_bps": _round(self.gross_bps),
            "net_bps_0": _round(self.net_bps_by_fee.get("0")),
            "net_bps_2": _round(self.net_bps_by_fee.get("2")),
            "net_bps_4": _round(self.net_bps_by_fee.get("4")),
            "net_bps_7.5": _round(self.net_bps_by_fee.get("7.5")),
            "net_bps_10": _round(self.net_bps_by_fee.get("10")),
            "net_moot_reason": self.net_moot_reason,
            "trade_count": self.trade_count,
            "oos_trade_count": self.oos_trade_count,
            "n_folds": self.n_folds,
            "single_fold_edge": self.single_fold_edge,
            "t_stat_gross": _round(self.t_stat_gross, 3),
            "t_stat_oos": _round(self.t_stat_oos, 3),
            "verdict": self.verdict,
            "baseline_beat": _fmt_baselines(self.baseline_beat),
            "failure_modes": "|".join(self.failure_modes),
            "citation": self.citation,
        }


def _round(value: float | None, ndigits: int = 4) -> float | None:
    return None if value is None else round(value, ndigits)


def _fmt_baselines(flags: dict[str, bool]) -> str:
    if not flags:
        return ""
    return ";".join(f"{k}={'beat' if v else 'lost'}" for k, v in sorted(flags.items()))


# --------------------------------------------------------------------------- #
# Documented-only registry (ROB-342). Citable numbers only.
# --------------------------------------------------------------------------- #

# The exact source text backing the ROB-342 documented record (verifiable in
# Linear today). The detailed "negative even at 0 bps / n>=263 / 1yr
# regime-conditioned OOS" closure number is intentionally NOT encoded as a
# quantitative field: it could not be traced to a live source on 2026-05-31.
ROB342_MEMORY_ONLY_UNCITED = (
    "ROB-342 closure detail 'short-horizon reversal negative even at 0 bps, "
    "BTC+XRP n>=263, 1yr regime-conditioned OOS' is recalled but NOT traceable "
    "to a live Linear comment or in-repo doc as of 2026-05-31 (issue archived, "
    "0 comments, no design doc committed). Excluded from quantitative fields; "
    "operator to confirm. NOTE: the same conclusion is independently established "
    "by the re-parsed ROB-353 result (all families gross-negative)."
)


def documented_registry() -> list[CandidateEvidence]:
    """Hand-curated, citation-backed evidence for issues with no local JSON.

    Only ROB-342 qualifies. Its numbers are the ROB-339 smoke figures quoted in
    the ROB-342 issue description: best gross edge ~+0.44 bps (BTCUSDT sweep
    reversal) and ~+0.28 bps (XRPUSDT sweep / time-of-day), against an estimated
    6-8 bps cost hurdle; all five tested families screened out; failure is "gross
    edge too small after realistic costs", not insufficient data.
    """
    return [
        CandidateEvidence(
            issue="ROB-342",
            candidate="short_horizon_sweep_reversal (best of 5 families)",
            family="liquidity-sweep / fake-breakout reversal",
            source="documented",
            schema="documented",
            citation=(
                "ROB-342 issue description (Linear), quoting ROB-339 smoke: "
                "'best observed gross edge was only about +0.44 bps for BTCUSDT "
                "sweep reversal and about +0.28 bps for XRPUSDT sweep/time-of-day, "
                "far below the estimated 6-8 bps cost hurdle'; 'all 5 tested "
                "families screened out'."
            ),
            gross_bps=0.44,  # best of the 5 families (BTCUSDT); +0.28 for XRP
            net_moot_reason="documented as-recorded; net grid not published (gross 0.44 bps << 6-8 bps cost hurdle)",
            verdict="screened_out (all 5 families)",
            baseline_beat={},
            baseline_note="not published in the documented source",
            notes=(
                "Cost hurdle 6-8 bps; best gross +0.44 bps (BTC) / +0.28 bps (XRP). "
                + ROB342_MEMORY_ONLY_UNCITED
            ),
        )
    ]
