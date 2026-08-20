"""Hook ⓐ — supply ``catalyst_basis`` for the momentum_spike sell tier (pure).

``config/trading_policy.yaml`` declares
``momentum_spike_profit_ladder.conditions.required_thesis_evidence:
[catalyst_basis, flow_basis]``. That requirement is consumed by the session, not
by code — no module reads it today. This builder turns an attribution record
into the ``catalyst_basis`` block a session can quote, with two hard properties:

* it **cannot manufacture sufficiency** — an ``unattributed`` record returns
  ``satisfies=False`` with the unattributed wording intact; and
* it supplies ``catalyst_basis`` only. ``flow_basis`` stays unsupplied because
  investor flow lands T+1 and is simply not available on the spike day.

Wiring only: nothing here places, proposes, or sizes anything.
"""

from __future__ import annotations

from typing import Any

from app.services.spike_attribution.attribute import UNATTRIBUTED_PHRASE
from app.services.spike_attribution.contract import SpikeAttribution
from app.services.spike_attribution.spec import (
    EXPERIMENT_ID,
    PRE_REGISTRATION,
)

_HOOK = PRE_REGISTRATION["catalyst_basis_hook"]

POLICY_TIER: str = _HOOK["consumer_policy_tier"]
SUPPLIED_EVIDENCE: tuple[str, ...] = tuple(_HOOK["supplies"])
UNSUPPLIED_EVIDENCE: tuple[str, ...] = tuple(_HOOK["does_not_supply"])


def build_catalyst_basis(attribution: SpikeAttribution) -> dict[str, Any]:
    """Return the ``catalyst_basis`` evidence block for one attribution record."""

    citations = [
        {
            "attribution_type": item.attribution_type,
            "source": item.source,
            "title": item.title,
            "url": item.url,
            "published_at": (
                item.published_at.isoformat() if item.published_at else None
            ),
            "published_at_source": item.published_at_source,
            "judgment": item.judgment,
        }
        for item in attribution.candidates
    ]
    satisfies = not attribution.unattributed
    return {
        "evidence_kind": "catalyst_basis",
        "experiment_id": EXPERIMENT_ID,
        "policy_tier": POLICY_TIER,
        "symbol": attribution.event.symbol,
        "market": attribution.event.market,
        "session_date": attribution.event.session_date.isoformat(),
        "session_change_pct": str(attribution.event.close_to_close_pct),
        "intraday_extreme_pct": str(attribution.event.intraday_extreme_pct),
        "triggered_bases": list(attribution.event.triggered_bases),
        "attribution_types": list(attribution.attribution_types),
        "citations": citations,
        "satisfies_catalyst_basis_requirement": satisfies,
        "unsatisfied_reason": (
            None
            if satisfies
            else attribution.unattributed_reason or UNATTRIBUTED_PHRASE
        ),
        # The tier needs both evidence kinds. We supply one of them; the session
        # must not read a present catalyst_basis as the pair being satisfied.
        "supplies": list(SUPPLIED_EVIDENCE),
        "does_not_supply": list(UNSUPPLIED_EVIDENCE),
        "flow_basis": {
            "supplied": False,
            "reason": _HOOK["flow_basis_reason"],
        },
        "required_thesis_evidence_complete": False,
        "spec_sha256": attribution.spec_sha256,
        "correlation_id": attribution.correlation_id,
        # Guard rails restated on every payload so a consumer that only reads
        # this block still sees them.
        "can_loosen_live_gate": False,
        "promote": False,
        "live_gate_impact": False,
    }


__all__ = [
    "POLICY_TIER",
    "SUPPLIED_EVIDENCE",
    "UNSUPPLIED_EVIDENCE",
    "build_catalyst_basis",
]
