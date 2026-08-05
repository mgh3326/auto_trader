"""Survivor overlay boundary for KR-B0.

There is intentionally no default strategy, ticker, price rule, or holding
window in this package.  An absent overlay is a normal B0 state and must stop
before an order intent can be constructed.
"""

from __future__ import annotations

from dataclasses import dataclass


class OverlayRequired(RuntimeError):
    """Raised when execution is requested before KR-B1 binds a survivor."""

    code = "OVERLAY_REQUIRED"


@dataclass(frozen=True)
class OverlayBinding:
    """Immutable identifiers supplied by the later survivor-integration job."""

    candidate_id: str
    contract_hash: str
    strategy_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("candidate_id", self.candidate_id),
            ("contract_hash", self.contract_hash),
            ("strategy_id", self.strategy_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} must be non-blank")


def require_overlay(overlay: OverlayBinding | None) -> OverlayBinding:
    """Return an explicit overlay or fail closed without inventing one."""
    if overlay is None:
        raise OverlayRequired(
            "OVERLAY_REQUIRED: no survivor overlay is bound; no order intent exists"
        )
    return overlay
