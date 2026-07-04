# app/services/investment_reports/risk_reward.py
"""ROB-690 — pure risk/reward (R:R) arithmetic for report-item trade setups.

Deterministic percentage/ratio arithmetic from an ``entry`` / ``stop`` /
``target`` price triple. This module is **not** a new prediction model — it
computes a ratio of numbers the caller already chose. It never selects which
entry/stop/target to use; that judgment stays with the Hermes/MCP consumer.

stdlib + ``decimal`` only. No LLM / DB / network / broker imports — this must
stay importable from a fully offline unit test (ROB-501 in-process LLM
provider boundary; see ``tests/services/action_report/snapshot_backed/
test_no_internal_llm_imports.py``, and the sibling pure-classifier precedent
at ``app/services/brokers/kis/live_order_expiry.py``).

Direction convention (long default, explicit short opt-in):

- **long**: ``stop < entry < target``
    ``risk_pct   = (entry - stop) / entry * 100``
    ``reward_pct = (target - entry) / entry * 100``
    ``rr_ratio   = (target - entry) / (entry - stop)``
- **short** (explicit only): ``target < entry < stop``
    ``risk_pct   = (stop - entry) / entry * 100``
    ``reward_pct = (entry - target) / entry * 100``
    ``rr_ratio   = (entry - target) / (stop - entry)``

Both ``risk_pct`` and ``reward_pct`` are positive magnitudes (percent of
entry) — the sign of the trade (loss vs. gain) is carried by ``direction``,
not by a signed number. If the price triangle doesn't match the resolved
direction, or the risk leg degenerates (entry == stop, i.e. zero risk
distance), the result is fail-closed: no R:R is produced (caller omits the
key entirely rather than showing a misleading card).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

Direction = Literal["long", "short"]
# resolve_direction() outcome: a direction to compute R:R for, or a reason to
# skip entirely ("exit" = pure liquidation, realized P/L frame is ROB-691's
# concern; "unknown" = ambiguous signal, stay silent rather than guess).
DirectionOrSkip = Literal["long", "short", "exit", "unknown"]
TradeSetupStatus = Literal["computed", "direction_price_mismatch", "degenerate_risk"]

_Q = Decimal("0.01")


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_Q, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RiskRewardLeg:
    """R:R for a single entry level against a shared stop/target."""

    entry: Decimal
    risk_pct: Decimal
    reward_pct: Decimal
    rr_ratio: Decimal


@dataclass(frozen=True)
class TradeSetup:
    """Result of computing R:R across all entry_plan levels + headline."""

    status: TradeSetupStatus
    direction: Direction
    stop: Decimal
    target: Decimal
    legs: tuple[RiskRewardLeg, ...]
    headline: RiskRewardLeg | None
    reason: str | None


def resolve_direction(
    *,
    side: str | None,
    intent: str,
    item_kind: str,
    explicit_direction: str | None,
) -> DirectionOrSkip:
    """Resolve which direction (if any) R:R should be computed for.

    ``item_kind`` is accepted for symmetry with the caller's item shape and
    potential future dispatch (e.g. risk items), but is not currently
    consulted — the side/intent signals are sufficient today.
    """
    del item_kind  # reserved for future dispatch; not consulted today.

    if explicit_direction == "short":
        return "short"
    if explicit_direction == "long":
        return "long"

    if side == "buy":
        return "long"
    if side == "sell":
        return "exit"

    # side is None — fall back to intent.
    if intent in ("buy_review", "trend_recovery_review"):
        return "long"
    if intent == "sell_review":
        return "exit"

    return "unknown"


def _classify_leg(
    *,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    direction: Direction,
) -> tuple[TradeSetupStatus | None, Decimal, Decimal]:
    """Classify one entry/stop/target triangle for ``direction``.

    Returns ``(None, risk_distance, reward_distance)`` when the leg is valid
    (both distances strictly positive). Otherwise returns a
    ``TradeSetupStatus`` describing why it's invalid:

    - ``direction_price_mismatch``: the price ordering itself contradicts
      ``direction`` (e.g. a long with ``stop`` above ``entry``).
    - ``degenerate_risk``: the ordering is directionally consistent (allows
      equality) but a distance collapses to zero or less — e.g.
      ``entry == stop`` (zero risk, would divide by zero).
    """
    if direction == "long":
        order_ok = stop <= entry <= target
        risk_distance = entry - stop
        reward_distance = target - entry
    else:
        order_ok = target <= entry <= stop
        risk_distance = stop - entry
        reward_distance = entry - target

    if not order_ok:
        return "direction_price_mismatch", risk_distance, reward_distance
    if risk_distance <= 0 or reward_distance <= 0:
        return "degenerate_risk", risk_distance, reward_distance
    return None, risk_distance, reward_distance


def compute_leg(
    *,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    direction: Direction,
) -> RiskRewardLeg | None:
    """R:R for one entry/stop/target triangle. ``None`` on any fail-closed case.

    Fail-closed cases: the price triangle doesn't match ``direction``'s
    convention, or a distance is non-positive (degenerate/zero risk or
    reward — including divide-by-zero when ``entry == stop``).
    """
    status, risk_distance, reward_distance = _classify_leg(
        entry=entry, stop=stop, target=target, direction=direction
    )
    if status is not None:
        return None

    risk_pct = _quantize((risk_distance / entry) * 100)
    reward_pct = _quantize((reward_distance / entry) * 100)
    rr_ratio = _quantize(reward_distance / risk_distance)
    return RiskRewardLeg(
        entry=entry,
        risk_pct=risk_pct,
        reward_pct=reward_pct,
        rr_ratio=rr_ratio,
    )


def _representative_entry(
    entry_levels: Sequence[Decimal],
    quantities: Sequence[Decimal | None],
) -> Decimal:
    """D6: quantity-weighted average entry if every level has qty > 0, else
    a simple average of entry prices."""
    if len(entry_levels) == 1:
        return entry_levels[0]

    all_qty_positive = len(quantities) == len(entry_levels) and all(
        q is not None and q > 0 for q in quantities
    )
    if all_qty_positive:
        total_qty = sum(quantities, Decimal("0"))  # type: ignore[arg-type]
        weighted_sum = sum(
            (e * q for e, q in zip(entry_levels, quantities, strict=True)),
            Decimal("0"),
        )
        return weighted_sum / total_qty

    return sum(entry_levels, Decimal("0")) / len(entry_levels)


def build_trade_setup(
    *,
    entry_levels: Sequence[Decimal],
    quantities: Sequence[Decimal | None],
    stop: Decimal,
    target: Decimal,
    direction: Direction,
) -> TradeSetup:
    """Compute per-leg R:R for every entry level + a representative headline.

    Fail-closed as a whole (``legs``/``headline`` empty) if any leg's price
    triangle mismatches ``direction``, or if the risk leg degenerates.
    """
    if not entry_levels:
        return TradeSetup(
            status="direction_price_mismatch",
            direction=direction,
            stop=stop,
            target=target,
            legs=(),
            headline=None,
            reason="no entry_plan levels",
        )

    legs: list[RiskRewardLeg] = []
    for entry in entry_levels:
        status, _, _ = _classify_leg(
            entry=entry, stop=stop, target=target, direction=direction
        )
        if status is not None:
            return TradeSetup(
                status=status,
                direction=direction,
                stop=stop,
                target=target,
                legs=(),
                headline=None,
                reason=f"entry={entry} stop={stop} target={target} direction={direction}",
            )
        leg = compute_leg(entry=entry, stop=stop, target=target, direction=direction)
        assert leg is not None  # _classify_leg already confirmed validity
        legs.append(leg)

    representative_entry = _representative_entry(entry_levels, quantities)
    rep_status, _, _ = _classify_leg(
        entry=representative_entry, stop=stop, target=target, direction=direction
    )
    if rep_status is not None:
        # The representative entry (an average) can itself fall outside the
        # valid triangle even when every individual leg was valid. Fail
        # closed rather than surface a headline computed off nonsense.
        return TradeSetup(
            status=rep_status,
            direction=direction,
            stop=stop,
            target=target,
            legs=(),
            headline=None,
            reason=f"representative_entry={representative_entry} out of range",
        )
    headline = compute_leg(
        entry=representative_entry, stop=stop, target=target, direction=direction
    )
    assert headline is not None  # rep_status already confirmed validity

    return TradeSetup(
        status="computed",
        direction=direction,
        stop=stop,
        target=target,
        legs=tuple(legs),
        headline=headline,
        reason=None,
    )
