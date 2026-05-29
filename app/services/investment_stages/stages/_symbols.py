"""Shared symbol helpers for deterministic stages."""

from __future__ import annotations


def normalize_symbol(value: str) -> str:
    """Normalize a ticker for held/candidate matching.

    Strips the crypto ``KRW-`` quote prefix so a held ticker ``BTC`` (as the
    Upbit reader reports it) matches a ``KRW-BTC`` candidate/symbol snapshot.
    Shared by ``CandidateUniverseStage`` and ``SymbolStage`` so the rule stays
    in one place.
    """
    s = (value or "").strip().upper()
    return s[4:] if s.startswith("KRW-") else s
