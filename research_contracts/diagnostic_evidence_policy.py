"""Neutral stdlib-only diagnostic-evidence carrier cap policy.

ROB-970 R2 audit: the diagnostic-evidence carrier cap must have exactly ONE
production authority, never two independently declared literals that can
silently drift apart. Both the research H4/H6 producer
(``rob944_diagnostic_evidence``) and the app schema/service boundaries
(``app.schemas.research_campaign_bridge``) import this constant rather than
redeclaring it -- mirroring the existing ``research_contracts.canonical_hash``
precedent for a single cross-boundary-safe authority.
"""

from __future__ import annotations

__all__ = ["MAX_DISTINCT_SIGNATURES"]

MAX_DISTINCT_SIGNATURES = 32
