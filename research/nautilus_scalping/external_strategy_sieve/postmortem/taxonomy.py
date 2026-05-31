"""ROB-384 — deterministic failure-mode taxonomy.

``assign_failure_modes`` reads a ``CandidateEvidence`` and returns the set of
failure modes (>= 1) plus a human-readable reason per mode. Rules are pure
functions of the recorded numbers so the assignment is reproducible and tested.

The modes (issue ROB-384):

* ``gross_zero``          — no positive gross edge (gross <= 0 before fees).
* ``cost_dominated``      — a positive gross edge that retail taker fees turn
                            net-negative (net@10bps <= 0). For documented rows
                            with no published net grid, a tiny gross edge well
                            below the demo cost hurdle counts.
* ``fee_fragile``         — net-positive at the demo taker (4 bps) but
                            net-negative at retail (10 bps): survives only in a
                            thin fee band (the "shadow" case).
* ``single_fold_only``    — gross edge concentrated in one fold, OR the sample
                            is too small / OOS t-stat too weak to confirm a
                            multi-fold edge (underpowered, not robust).
* ``regime_bound``        — edge documented as conditioned on a specific regime
                            window (assigned only from a recorded signal).
* ``listing_artifact``    — survivorship / delisting drives the result
                            (assigned only from a recorded signal).
* ``source_unfaithful``   — clean-room signal extraction diverges from the
                            published strategy (e.g. simplified indicator).
* ``license_shadow_only`` — usable as a signal-only / dry-run shadow at best
                            (sieve ``shadow`` class), not a tradeable edge.
* ``implementation_blocked`` — could not be evaluated for an implementation
                            reason (assigned only from a recorded signal).
"""

from __future__ import annotations

from external_strategy_sieve.postmortem.evidence import CandidateEvidence

ECONOMIC_FLOOR_BPS = 0.5  # triviality floor (sieve / ROB-351 convention)
DEMO_COST_HURDLE_BPS = 6.0  # ~6-8 bps demo round-trip; used for documented rows
MIN_TRADES = 100  # below this a per-trade edge is underpowered
TARGET_T = 2.0  # OOS t-stat bar for a decisive edge
DEMO_FEE = "4"
RETAIL_FEE = "10"


def assign_failure_modes(ev: CandidateEvidence) -> tuple[list[str], list[str]]:
    """Return (modes, reasons). ``modes`` always has at least one entry."""
    modes: list[str] = []
    reasons: list[str] = []
    g = ev.gross_bps
    net = ev.net_bps_by_fee
    net10 = net.get(RETAIL_FEE)
    net4 = net.get(DEMO_FEE)

    def add(mode: str, why: str) -> None:
        if mode not in modes:
            modes.append(mode)
            reasons.append(f"{mode}: {why}")

    # 1. gross_zero — no positive gross edge at all.
    if g is not None and g <= 0:
        add(
            "gross_zero",
            f"gross {g:.2f} bps <= 0 (no edge before fees; cost is not the bottleneck)",
        )

    # 2. cost_dominated — positive gross edge killed by retail fees.
    if g is not None and g > 0:
        if net10 is not None and net10 <= 0:
            add(
                "cost_dominated",
                f"gross +{g:.2f} bps but net@10bps {net10:.2f} <= 0 (retail fees eliminate the edge)",
            )
        elif not net and g < DEMO_COST_HURDLE_BPS:
            add(
                "cost_dominated",
                f"gross +{g:.2f} bps is far below the ~{DEMO_COST_HURDLE_BPS:.0f}-8 bps demo cost hurdle "
                "(documented; net grid not published)",
            )

    # 3. fee_fragile — survives demo taker, dies at retail.
    if net4 is not None and net10 is not None and net4 > 0 and net10 <= 0:
        add(
            "fee_fragile",
            f"net@4bps +{net4:.2f} > 0 but net@10bps {net10:.2f} <= 0 (thin fee band only)",
        )

    # 4. single_fold_only — concentrated in one fold OR underpowered/unconfirmed.
    #    Underpowered only matters when there is a positive gross edge to confirm;
    #    a gross-negative family (already gross_zero) is not "underpowered", it has
    #    no edge to begin with.
    if ev.single_fold_edge:
        add(
            "single_fold_only",
            "gross edge concentrated in a single fold (train/val negative)",
        )
    elif g is not None and g > 0 and _underpowered(ev):
        add(
            "single_fold_only",
            f"underpowered: trades={ev.trade_count}, oos={ev.oos_trade_count}, "
            f"t_oos={ev.t_stat_oos}; positive gross edge not confirmable across folds",
        )

    # 5. source_unfaithful — clean-room divergence from published spec.
    if (
        "non_faithful" in ev.notes
        or "clean_room" in ev.notes
        or "clean-room" in ev.notes
    ):
        add(
            "source_unfaithful",
            "clean-room signal diverges from the published strategy spec",
        )

    # 6. license_shadow_only — sieve shadow class (signal-only at best).
    if "shadow" in ev.verdict.lower():
        add(
            "license_shadow_only",
            "sieve shadow class — signal-only / dry-run observation at best, not tradeable",
        )

    if not modes:
        # Gross-positive, net-positive even at retail, adequately powered: the
        # edge is unconfirmed only by the decisive-survivor bar (t_oos < target).
        add(
            "single_fold_only",
            f"gross +{(g or 0):.2f} bps positive but not a decisive survivor "
            f"(t_oos={ev.t_stat_oos} < {TARGET_T}); not independently validated",
        )
    return modes, reasons


def _underpowered(ev: CandidateEvidence) -> bool:
    if ev.trade_count is not None and ev.trade_count < MIN_TRADES:
        return True
    v = ev.verdict.lower()
    if "underpowered" in v or "insufficient_data" in v:
        return True
    return False


def annotate(records: list[CandidateEvidence]) -> list[CandidateEvidence]:
    """Assign failure modes in-place and return the list (convenience)."""
    for ev in records:
        modes, reasons = assign_failure_modes(ev)
        ev.failure_modes = modes
        if reasons:
            ev.notes = (ev.notes + " || taxonomy: " + "; ".join(reasons)).strip(" |")
    return records
