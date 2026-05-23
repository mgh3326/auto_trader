"""ROB-298 PR 2 — Futures Demo adapter error vocabulary."""

from __future__ import annotations

from app.services.brokers.binance.errors import BinanceAdapterError


class BinanceFuturesDemoDisabled(BinanceAdapterError):
    """Raised when BINANCE_FUTURES_DEMO_ENABLED is not 'true'."""


class BinanceFuturesDemoMissingCredentials(BinanceAdapterError):
    """Raised when API key/secret env vars are empty."""


class BinanceFuturesDemoCrossAllowlistViolation(BinanceAdapterError):
    """Raised when a signed request would route to a non-Futures-Demo host."""


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
