"""ROB-302 — shared Binance Demo credential resolver.

The Spot Demo and Futures Demo lanes share ONE Binance Demo credential. To
avoid duplicating the same secret across two env var pairs in production, both
lanes resolve through a canonical ``BINANCE_DEMO_API_KEY`` /
``BINANCE_DEMO_API_SECRET`` pair. Per-product vars remain optional overrides.

Resolution chain (per product, PAIR-by-source — never mixes key/secret across
sources, per Codex review #2):

    product-specific pair  ->  canonical pair  ->  MissingCredentials
       (SPOT/FUTURES)            (BINANCE_DEMO_*)

    spot:    BINANCE_SPOT_DEMO_API_{KEY,SECRET}    -> BINANCE_DEMO_API_{KEY,SECRET}
    futures: BINANCE_FUTURES_DEMO_API_{KEY,SECRET} -> BINANCE_DEMO_API_{KEY,SECRET}

Fail-closed rules:
  * If EITHER product-specific var is set, the PAIR must come from product vars.
    A half-set product override raises ``BinanceDemoIncompleteCredentialOverride``
    — we never backfill the missing half from canonical (that would pair a
    product key with a canonical secret).
  * A half-set canonical pair also fails closed.
  * Nothing set -> ``BinanceDemoMissingCredentials``.

Isolation invariant: a Spot-specific var never resolves for Futures and vice
versa. The only cross-lane sharing is the explicit canonical pair. This module
does NOT read ``*_ENABLED`` flags — lane activation stays gated by each lane's
own ``from_env`` (``BINANCE_{SPOT,FUTURES}_DEMO_ENABLED``).

Secret hygiene: ``ResolvedDemoCredential`` and ``DemoCredentialInspection`` keep
the key/secret out of ``repr``; only the source label is shown.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from app.services.brokers.binance.demo.errors import (
    BinanceDemoIncompleteCredentialOverride,
    BinanceDemoMissingCredentials,
)

DemoProduct = Literal["spot", "futures"]

CANONICAL_KEY_ENV: Final[str] = "BINANCE_DEMO_API_KEY"
CANONICAL_SECRET_ENV: Final[str] = "BINANCE_DEMO_API_SECRET"

_PRODUCT_ENV: Final[dict[str, tuple[str, str]]] = {
    "spot": ("BINANCE_SPOT_DEMO_API_KEY", "BINANCE_SPOT_DEMO_API_SECRET"),
    "futures": ("BINANCE_FUTURES_DEMO_API_KEY", "BINANCE_FUTURES_DEMO_API_SECRET"),
}


@dataclass(frozen=True, slots=True)
class ResolvedDemoCredential:
    """A matched key/secret pair plus the env source it came from.

    ``api_key`` / ``api_secret`` are excluded from ``repr`` so the pair never
    leaks into logs or evidence files. Only ``credential_source`` is shown.
    """

    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    credential_source: str  # "spot_demo_env" | "futures_demo_env" | "shared_demo_env"


@dataclass(frozen=True, slots=True)
class DemoCredentialInspection:
    """Non-raising presence/source view for the readiness reflector.

    Reports only booleans + the source label that WOULD be used. Carries no
    secret material, so it is safe to serialize into readiness evidence.
    """

    api_key_present: bool
    api_secret_present: bool
    credential_source: str | None
    incomplete: bool


def _clean(value: str | None) -> str:
    return (value or "").strip()


def resolve_demo_credentials(
    product: DemoProduct,
    env: Mapping[str, str],
) -> ResolvedDemoCredential:
    """Resolve a matched Demo credential pair for ``product``.

    Raises ``BinanceDemoIncompleteCredentialOverride`` on a half-set source and
    ``BinanceDemoMissingCredentials`` when no source is present.
    """
    key_env, secret_env = _PRODUCT_ENV[product]
    product_key = _clean(env.get(key_env))
    product_secret = _clean(env.get(secret_env))

    # 1. Product-specific override: if either half is set, the PAIR must come
    #    from product vars. No canonical backfill (Codex #2).
    if product_key or product_secret:
        if not (product_key and product_secret):
            missing = secret_env if product_key else key_env
            raise BinanceDemoIncompleteCredentialOverride(
                f"{key_env}/{secret_env}: only one half is set (missing "
                f"{missing}). Set BOTH product vars, or NEITHER to fall back to "
                f"the canonical {CANONICAL_KEY_ENV}/{CANONICAL_SECRET_ENV} pair. "
                "Refusing to pair a product key with a canonical secret."
            )
        return ResolvedDemoCredential(
            api_key=product_key,
            api_secret=product_secret,
            credential_source=f"{product}_demo_env",
        )

    # 2. Canonical shared pair.
    canon_key = _clean(env.get(CANONICAL_KEY_ENV))
    canon_secret = _clean(env.get(CANONICAL_SECRET_ENV))
    if canon_key or canon_secret:
        if not (canon_key and canon_secret):
            missing = CANONICAL_SECRET_ENV if canon_key else CANONICAL_KEY_ENV
            raise BinanceDemoIncompleteCredentialOverride(
                f"{CANONICAL_KEY_ENV}/{CANONICAL_SECRET_ENV}: only one half is "
                f"set (missing {missing}). Set BOTH canonical vars."
            )
        return ResolvedDemoCredential(
            api_key=canon_key,
            api_secret=canon_secret,
            credential_source="shared_demo_env",
        )

    # 3. Nothing usable.
    raise BinanceDemoMissingCredentials(
        f"No Demo credentials for product={product!r}. Set both {key_env}+"
        f"{secret_env}, or both {CANONICAL_KEY_ENV}+{CANONICAL_SECRET_ENV}."
    )


def inspect_demo_credential(
    product: DemoProduct,
    env: Mapping[str, str],
) -> DemoCredentialInspection:
    """Non-raising presence/source inspection for readiness reporting."""
    try:
        resolved = resolve_demo_credentials(product, env)
    except BinanceDemoIncompleteCredentialOverride:
        key_env, secret_env = _PRODUCT_ENV[product]
        key_present = bool(_clean(env.get(key_env)) or _clean(env.get(CANONICAL_KEY_ENV)))
        secret_present = bool(
            _clean(env.get(secret_env)) or _clean(env.get(CANONICAL_SECRET_ENV))
        )
        return DemoCredentialInspection(
            api_key_present=key_present,
            api_secret_present=secret_present,
            credential_source=None,
            incomplete=True,
        )
    except BinanceDemoMissingCredentials:
        return DemoCredentialInspection(
            api_key_present=False,
            api_secret_present=False,
            credential_source=None,
            incomplete=False,
        )
    return DemoCredentialInspection(
        api_key_present=True,
        api_secret_present=True,
        credential_source=resolved.credential_source,
        incomplete=False,
    )
