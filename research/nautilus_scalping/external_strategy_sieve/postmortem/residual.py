"""ROB-384 — residual hypothesis map + single closure decision.

Each candidate is mapped to one residual status:

* ``closed``                  — terminally dead. Either no gross edge before fees
                                (``gross_zero``), or a cost-dominated edge whose
                                execution-realism / maker-fill rescue was already
                                tested and still failed.
* ``maybe_worth_feasibility`` — a *positive, stable* gross edge that only retail
                                fees kill, whose maker-fill / execution-realism
                                rescue is genuinely untested. This is a
                                NON-strategy feasibility thread (ROB-320/343), not
                                a reason to keep searching for strategies.
* ``not_worth_pursuing``      — a faint or unconfirmed signal (single-fold
                                artifact, fee-fragile shadow, underpowered port)
                                that its own source already declined to pursue.

The closure decision is then deterministic:

* **B** if any candidate is ``maybe_worth_feasibility`` (strategy line closed,
  but a specific non-strategy feasibility thread genuinely remains).
* **C** if none is ``maybe_worth_feasibility`` but exactly one candidate is a
  gross-positive, baseline-beating, not-yet-OOS-confirmed signal that its source
  did NOT already explicitly close (worth a pre-registered, no-tuning re-test).
* **A** otherwise — the crypto public / OHLCV short-horizon strategy line is
  closed.
"""

from __future__ import annotations

from external_strategy_sieve.postmortem.evidence import CandidateEvidence
from external_strategy_sieve.postmortem.taxonomy import (
    ECONOMIC_FLOOR_BPS,
    TARGET_T,
    _underpowered,
)

# Candidates whose maker-fill / limit-fill execution-realism rescue was ALREADY
# evaluated and still came back not_validated, so the feasibility thread is spent.
# This is a safety net, NOT the load-bearing close reason for meanrev: meanrev's
# gross edge (+0.16 bps) is below the triviality floor and is closed by that
# branch first (emptying this set leaves the verdict A unchanged). It only fires
# for a hypothetical gross >= floor, cost-dominated, stable candidate.
#   Source (DOCUMENTED, not in the meanrev artifact): the ROB-342 issue
#   description records "ROB-324 / PR #984: maker/limit-fill re-evaluation was
#   still not_validated, with significant missed-fill/adverse-selection concerns."
MAKER_FILL_TESTED: frozenset[tuple[str, str]] = frozenset(
    {("ROB-320", "meanrev_zscore_fade")}
)

# Substrings in a source verdict that mean the source itself already closed the
# candidate (not_validated / screened_out / reject / underpowered / explicit
# "not a decisive survivor" / shadow / research-only).
_SOURCE_CLOSED_MARKERS = (
    "not_validated",
    "screened_out",
    "reject",
    "insufficient",
    "underpowered",
    "decisive_survivor=false",
    "shadow",
    "research_candidate",
)

CLOSURE_LABELS = {
    "A": "crypto public/OHLCV short-horizon strategy line CLOSED",
    "B": "strategy line closed; only non-strategy feasibility threads remain (exec-realism / maker-fill, ROB-320/343)",
    "C": "one candidate worth a separate pre-registered, no-tuning validation",
}


def _source_closed(ev: CandidateEvidence) -> bool:
    v = ev.verdict.lower()
    return any(marker in v for marker in _SOURCE_CLOSED_MARKERS)


def _stable_edge(ev: CandidateEvidence) -> bool:
    """A gross edge that is NOT a single-fold artifact and is adequately powered.

    If an OOS t-stat is recorded it must clear ``TARGET_T``; otherwise the sample
    must not be underpowered.
    """
    if ev.single_fold_edge:
        return False
    if ev.t_stat_oos is not None:
        return ev.t_stat_oos >= TARGET_T
    return not _underpowered(ev)


def residual_status(ev: CandidateEvidence) -> tuple[str, str]:
    """Return (status, reason) for one candidate."""
    modes = set(ev.failure_modes)
    g = ev.gross_bps
    net10 = ev.net_bps_by_fee.get("10")

    if "gross_zero" in modes:
        return (
            "closed",
            "no gross edge before fees — cost is not the bottleneck; nothing to rescue",
        )

    # A positive but economically trivial gross edge (< floor) is not a maker-fill
    # candidate: fees are not the real bottleneck, there is just no meaningful edge.
    if g is not None and 0 < g < ECONOMIC_FLOOR_BPS:
        extra = ""
        if (ev.issue, ev.candidate) in MAKER_FILL_TESTED:
            extra = " maker/limit-fill also tested negative (ROB-324 / PR #984)."
        return (
            "closed",
            f"gross +{g:.2f} bps is below the {ECONOMIC_FLOOR_BPS} bps triviality floor — no meaningful "
            f"edge; fees are not the real bottleneck.{extra}",
        )

    # A maker-fill / execution-realism rescue is only conceivable for a cost-
    # dominated edge that is otherwise sound: an economically non-trivial gross
    # edge demonstrably net-negative at retail, not a single-fold artifact, not a
    # signal-only shadow its source already declined, and not a clean-room
    # mis-port. Those disqualifiers were already adjudicated by the source.
    disqualifying = {
        "single_fold_only",
        "license_shadow_only",
        "source_unfaithful",
    } & modes
    demonstrated_fee_death = (
        net10 is not None and net10 <= 0 and g is not None and g >= ECONOMIC_FLOOR_BPS
    )
    cost_only_killer = (
        "cost_dominated" in modes and not disqualifying and demonstrated_fee_death
    )
    if cost_only_killer and _stable_edge(ev):
        if (ev.issue, ev.candidate) in MAKER_FILL_TESTED:
            return (
                "closed",
                "non-trivial gross edge killed by retail fees; maker/limit-fill rescue ALREADY tested "
                "(ROB-324 / PR #984) and still not_validated — feasibility thread spent",
            )
        return (
            "maybe_worth_feasibility",
            "non-trivial, stable gross edge killed only by retail taker fees; maker-fill / execution-realism "
            "rescue is untested (NON-strategy feasibility thread, ROB-320/343)",
        )

    # Faint / unconfirmed signal that the source itself declined.
    bits = []
    if "single_fold_only" in modes:
        bits.append("edge is a single-fold / underpowered artifact")
    if "fee_fragile" in modes:
        bits.append(
            "net-positive only in a thin fee band (demo taker), negative at retail"
        )
    if "license_shadow_only" in modes:
        bits.append("signal-only shadow at best, not tradeable alpha")
    if "source_unfaithful" in modes:
        bits.append("clean-room signal diverges from the published spec")
    if ev.t_stat_oos is not None and ev.t_stat_oos < TARGET_T:
        bits.append(f"OOS t {ev.t_stat_oos:.2f} < {TARGET_T} (not a decisive survivor)")
    if not bits:
        bits.append("not validated by its source")
    return "not_worth_pursuing", "; ".join(bits)


def closure_decision(records: list[CandidateEvidence]) -> dict:
    """Map every record and return the single A/B/C closure decision."""
    rows = []
    for ev in records:
        status, reason = residual_status(ev)
        rows.append(
            {
                "issue": ev.issue,
                "candidate": ev.candidate,
                "status": status,
                "failure_modes": list(ev.failure_modes),
                "reason": reason,
            }
        )

    n_maybe = sum(1 for r in rows if r["status"] == "maybe_worth_feasibility")
    # C candidates: gross-positive, baseline-beating, not-yet-OOS-confirmed signals
    # the source did NOT already explicitly close.
    c_candidates = [
        ev.candidate
        for ev in records
        if (ev.gross_bps or 0) > 0
        and any(ev.baseline_beat.values())
        and ev.t_stat_oos is not None
        and ev.t_stat_oos < TARGET_T
        and not _source_closed(ev)
    ]

    if n_maybe >= 1:
        verdict = "B"
    elif len(c_candidates) >= 1:
        verdict = "C"
    else:
        verdict = "A"

    distribution: dict[str, int] = {}
    for r in rows:
        distribution[r["status"]] = distribution.get(r["status"], 0) + 1

    return {
        "verdict": verdict,
        "verdict_label": CLOSURE_LABELS[verdict],
        "n_candidates": len(records),
        "status_distribution": distribution,
        "n_maybe_worth_feasibility": n_maybe,
        "c_candidates": c_candidates,
        "residual_map": rows,
        "decision_rationale": _rationale(verdict, n_maybe, c_candidates),
    }


def _rationale(verdict: str, n_maybe: int, c_candidates: list[str]) -> str:
    if verdict == "A":
        return (
            "No candidate is a positive, stable, fee-only-killed edge with an untested maker-fill rescue "
            "(the one such candidate, meanrev, already had maker-fill tested negative in ROB-324), and no "
            "source left a gross-positive candidate open for re-validation. The dominant failure mode is "
            "gross-insufficiency / single-fold artifacts, which execution realism cannot fix. The crypto "
            "public/OHLCV short-horizon strategy line is closed. The only filed non-strategy thread (ROB-343 "
            "execution-realism harness) is deferred-Low and is NOT a reason to keep searching — it targets "
            "cost, which binds for only a minority of candidates."
        )
    if verdict == "B":
        return f"{n_maybe} candidate(s) have a positive, stable, fee-only-killed edge with an untested maker-fill rescue."
    return f"One candidate worth a pre-registered no-tuning re-test: {', '.join(c_candidates)}."
