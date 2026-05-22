"""ROB-286 — Binance testnet execution adapter error vocabulary.

Reuses Child B's ``BinanceAdapterError`` base class so error-handling
sites can catch a single hierarchy. Adds the testnet-specific failure
modes that ROB-286's hard invariants demand.
"""

from __future__ import annotations

from app.services.brokers.binance.errors import BinanceAdapterError


class BinanceTestnetDisabled(BinanceAdapterError):
    """Raised when caller attempts an order op while testnet is disabled.

    Triggered by either:
      * ``BINANCE_TESTNET_ENABLED`` unset/false at adapter construction.
      * ``submit_order(..., confirm=False)`` is intentionally allowed
        (returns a dry-run preview) but ``submit_order(..., dry_run=False,
        confirm=False)`` is treated as a misuse and raises this.

    Operator gate: hard invariant #5 + #8.
    """


class BinanceMissingCredentials(BinanceAdapterError):
    """Raised when API key/secret are missing at adapter construction.

    Hard invariant #4 — fail-closed. The adapter never constructs an
    HTTP client without both credentials.
    """


class BinanceInvalidStateTransition(BinanceAdapterError):
    """Raised by the testnet ledger service when a transition is illegal.

    The 10-state lifecycle (planned → ... → reconciled + anomaly) has a
    locked transition table; any record_* attempt that violates it raises.
    """


class BinanceReduceOnlyRequired(BinanceAdapterError):
    """Reserved for the futures follow-up (NOT used in this PR).

    Defined here so the futures-track child can extend the testnet adapter
    without re-adding the exception type. Spot has no reduce-only flag;
    the execution client never raises this in the spot path.
    """


class BinanceTestnetCrossAllowlistViolation(BinanceAdapterError):
    """Raised when the testnet transport sees a host in PUBLIC_HOSTS.

    This is the cross-allowlist guard at hard invariant #1: a signed
    request to a public (live) host means a misconfigured deploy is one
    step from sending real-money orders to live Binance. Fail hard at the
    request-event hook, no silent fallback.
    """
