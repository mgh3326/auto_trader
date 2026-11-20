"""Which Upbit account this process would trade (hk 5441, design 5438 B12).

A mandate stores the fingerprint of the credentials it was approved for. Before
any order of a ``go`` sleeve is sent -- a Go child or an exit handoff card --
the sender computes the fingerprint of the credentials configured right now and
compares. The fingerprint is the HMAC that the manual account value snapshot
(hk 3800) already uses, so it is never the credential and cannot be reversed.

``None`` means the fingerprint could not be computed. That is never a match.
"""

from __future__ import annotations


def configured_upbit_account_ref() -> str | None:
    from app.core.config import settings
    from app.services.account_value.identity import configured_fingerprint

    try:
        return configured_fingerprint(
            "upbit",
            "upbit",
            str(settings.upbit_access_key or ""),
            str(settings.upbit_secret_key or ""),
        )
    except Exception:  # noqa: BLE001 - unknown is not a match
        return None
