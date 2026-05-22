"""ROB-296 — Binance Spot Demo Mode adapter error vocabulary.

Self-contained exception hierarchy (the prior ``binance.testnet.errors``
sibling was removed in ROB-298). Exception types remain Spot Demo
specific so a caller cannot accidentally conflate Spot Demo failures
with any future signed-lane failures when catching by name.

Exceptions share `BinanceAdapterError` so call-sites that catch the
common base still work.

Constraint per ROB-296: the "Disabled" / "MissingCredentials" naming is
intentional environment isolation — keep it Spot Demo specific even when
adding future signed lanes.
"""

from __future__ import annotations

from app.services.brokers.binance.errors import BinanceAdapterError


class BinanceSpotDemoDisabled(BinanceAdapterError):
    """Raised when caller attempts a Spot Demo op while the env gate is off.

    Triggered by ``BINANCE_SPOT_DEMO_ENABLED`` unset or non-truthy at
    adapter construction. Hard fail-closed per ROB-296 §6 smoke path.
    """


class BinanceSpotDemoMissingCredentials(BinanceAdapterError):
    """Raised when API key/secret are missing at Spot Demo adapter construction.

    The adapter never constructs an HTTP client without both credentials.
    """


class BinanceSpotDemoCrossAllowlistViolation(BinanceAdapterError):
    """Raised when the Spot Demo transport sees a host in TESTNET_HOSTS or PUBLIC_HOSTS.

    Cross-allowlist guard: a signed Spot Demo request that lands on a
    testnet host (Spot Testnet) or a live host (mainnet Binance) means a
    misconfigured deploy is one step from leaking credentials to the
    wrong environment. Fail hard at the request-event hook, no silent
    fallback.
    """


class BinanceSpotDemoUnsupportedAuth(BinanceAdapterError):
    """Raised when the Spot Demo server rejects HMAC-SHA256 signing.

    Per ROB-296 §5 signer preflight: if the operator's Spot Demo account
    requires Ed25519 (or any non-HMAC signing mechanism), this PR does
    NOT silently fall back. The preflight surfaces the auth-rejection
    response and raises this exception so the operator reports it as a
    scope-expansion follow-up rather than landing a half-baked signer.

    Detected via Binance API error codes returned from a read-only
    preflight call: -2014 (API-key format invalid), -2008 (Invalid
    API-key), -1022 (Signature for this request is not valid). The
    underlying response is summarized in the exception message with
    credential values redacted.
    """


# Note: BinanceSpotDemoOrderSubmitNotImplemented was removed in ROB-298 when
# the Spot Demo execution client (submit/test/cancel/status) landed. The
# previous placeholder behavior is now covered by the operator gate on
# ``BinanceSpotDemoExecutionClient.submit_order`` (default returns a
# ``SpotDemoDryRunResult``; HTTP only on explicit ``confirm=True``).
