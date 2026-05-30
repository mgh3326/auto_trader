"""ROB-373 — account-independence policy for investment snapshot kinds.

Single source of truth deciding which snapshot kinds are *account-independent*
(market-wide evidence shared across broker scopes) vs *account-bound* (portfolio,
journal, watch, pending orders — meaningful only within one broker account).

Account-independent kinds are normalized to ``account_scope=None`` at the write
chokepoint so the snapshot dedup key ``(canonical_payload_hash, snapshot_kind,
market, account_scope)`` collapses identical market/news/candidate/symbol payloads
into ONE row that both a ``kis_live`` and a ``kis_mock`` bundle can cite.
"""

from __future__ import annotations

# market-wide evidence: identical regardless of which account requested it.
ACCOUNT_INDEPENDENT_SNAPSHOT_KINDS: frozenset[str] = frozenset(
    {"market", "news", "candidate_universe", "symbol"}
)


def is_account_independent(snapshot_kind: str) -> bool:
    """True when the kind is market-wide evidence (no account binding)."""
    return snapshot_kind in ACCOUNT_INDEPENDENT_SNAPSHOT_KINDS


def normalize_account_scope(
    snapshot_kind: str, account_scope: str | None
) -> str | None:
    """Force ``None`` for account-independent kinds; pass through otherwise.

    Idempotent and total: account-bound kinds keep whatever scope (incl. None)
    they arrived with.
    """
    if is_account_independent(snapshot_kind):
        return None
    return account_scope
