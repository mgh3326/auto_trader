"""The opaque account reference of a mandate (§4.6 "계좌 결속", T4c).

A reference is a keyed digest of the broker account identifier together with its
account mode, so the raw account number is never stored and a changed credential for
the same account keeps the same reference. Clause and plan registration take the
reference as an operator input verified against the broker; the send boundary
computes the same reference from the account a request will really use and refuses
when they differ or when the reference cannot be computed.
"""

from __future__ import annotations

import hashlib
import hmac

_DOMAIN = b"mandate-account-ref/v1"


def derive_account_ref(
    account_mode: str, identifier: str, *, secret: str
) -> str | None:
    """``None`` when the account cannot be identified (a blank identifier or secret)."""
    ident = (identifier or "").strip()
    if not ident or not secret or not account_mode:
        return None
    key = hmac.new(secret.encode(), _DOMAIN, hashlib.sha256).digest()
    digest = hmac.new(
        key, f"{account_mode}:{ident}".encode(), hashlib.sha256
    ).hexdigest()
    return f"acct_{digest[:32]}"
