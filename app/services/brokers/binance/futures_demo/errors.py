"""ROB-298 PR 2 — Futures Demo adapter error vocabulary."""

from __future__ import annotations

from app.services.brokers.binance.errors import BinanceAdapterError


class BinanceFuturesDemoDisabled(BinanceAdapterError):
    """Raised when BINANCE_FUTURES_DEMO_ENABLED is not 'true'."""


class BinanceFuturesDemoMissingCredentials(BinanceAdapterError):
    """Raised when API key/secret env vars are empty."""


class BinanceFuturesDemoCrossAllowlistViolation(BinanceAdapterError):
    """Raised when a signed request would route to a non-Futures-Demo host."""


class BinanceFuturesDemoUnsupportedAuth(BinanceAdapterError):
    """Raised when the Futures Demo server rejects HMAC-SHA256 signing.

    Mirrors ``BinanceSpotDemoUnsupportedAuth`` (ROB-296 §5): if the
    operator's Futures Demo account requires Ed25519 (or any non-HMAC
    signing mechanism), this PR does NOT silently fall back. The
    preflight surfaces the auth-rejection response and raises this
    exception so the operator reports it as a scope-expansion follow-up
    rather than landing a half-baked signer.

    Detected via Binance API error codes returned from a read-only
    preflight call: -2014 (API-key format invalid), -2008 (Invalid
    API-key), -1022 (Signature for this request is not valid). The
    underlying response is summarized in the exception message with
    credential values redacted.
    """


class BinanceFuturesDemoHedgeModeBlocked(BinanceAdapterError):
    """Raised when the Demo account is in Hedge mode.

    ROB-298 PR 2 only supports One-way mode. Hedge mode would require
    explicit positionSide on every order, which is out of scope.
    """


class BinanceFuturesDemoLeverageMismatch(BinanceAdapterError):
    """Raised when the post-set_leverage echo from Binance is not 1x.

    The smoke contract enforces 1x leverage exactly. Any other leverage
    indicates either a Binance-side bug or an env tampering attempt.
    """


class BinanceFuturesDemoReduceOnlyRequired(BinanceAdapterError):
    """Raised when a close-side order is submitted without reduceOnly=true.

    Defense in depth: a close without reduceOnly could flip the position
    (open opposite side). PR 2 close path always sets reduceOnly=true.
    """


class BinanceFuturesDemoUnsupportedSymbol(BinanceAdapterError):
    """Raised when a symbol is not in the configured allowlist.

    Default allowlist: XRPUSDT (primary), DOGEUSDT, SOLUSDT.
    BTCUSDT is explicitly excluded due to MIN_NOTIONAL=50 USDT > 10 USDT cap.
    Operator CLI override extends the list but the cap is never bypassed.
    """


class BinanceFuturesDemoPositionSideMismatch(BinanceAdapterError):
    """Raised when the broker's ``positionSide`` echo does not match the request.

    ROB-1288 / D2 contract v2 §4.3. When a caller states an explicit
    ``position_side`` on a submit, the Binance response MUST echo that exact
    value back. Two ways this fails, both fatal:

      * the response echoes a *different* ``positionSide`` than requested, or
      * the response omits ``positionSide`` entirely.

    The omission case is deliberately an error and not a shrug: with no
    echoed value there is nothing to verify against, and the only remaining
    way to "know" the side would be to infer it — from the quantity sign, or
    from a default. Contract v2 §4.3 forbids exactly that ("v2 does not infer
    the missing value from quantity sign"), so absence fails closed.

    Mirrors the ``BinanceFuturesDemoLeverageMismatch`` echo-verification
    pattern already used for ``set_leverage``.
    """


class BinanceFuturesDemoPositionSideUnavailable(BinanceAdapterError):
    """Raised when a readback row carries no ``positionSide`` and one is required.

    ROB-1288. ``/fapi/v2/positionRisk`` rows are surfaced verbatim, so a row
    without a ``positionSide`` field yields ``position_side=None`` rather than
    a fabricated value. A caller that needs the side calls
    ``FuturesDemoPositionResult.require_position_side()``, which raises this
    instead of deriving the side from the sign of ``positionAmt``.

    🔴 The sign of ``positionAmt`` is NOT a substitute. Contract v2 §4.3
    forbids inferring the missing value from the quantity sign; this exception
    is the fail-closed alternative.
    """
