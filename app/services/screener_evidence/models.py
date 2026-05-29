"""Normalized candidate evidence shared by the screener view-model and the
report candidate_universe path (ROB-304). Deterministic; no LLM, no I/O."""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True)
class CandidateEvidence:
    symbol: str
    market: str  # "kr" | "us" | "crypto"
    name: str
    score: float  # normalized 0–10
    score_label: str  # Korean display, e.g. "RSI 28.3", "+4.20%"
    change_rate: float | None
    price: float | None
    volume_value: float | None  # turnover / 24h trade amount
    reasons: list[str]  # Korean reason strings
    source: str  # provenance: tvscreener_upbit / upbit_official / kis / yahoo / ...
    risk_flags: list[str]  # Korean risk labels, e.g. "Upbit 유의 종목"
    # ROB-359 Scope E — provenance lineage so /invest/reports can explain why a
    # new-buy candidate surfaced. ``source_preset`` is the ranking/preset that
    # produced this candidate (e.g. "top_gainers", "crypto_momentum"). Universe-
    # level descriptors (freshness/parity) are stamped onto the payload candidate
    # dict by the collector, not here, to keep this transformer pure.
    source_preset: str | None = None

    def to_payload_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
