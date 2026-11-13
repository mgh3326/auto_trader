"""Validate opaque broker account series keys without exposing account numbers."""

import re

_ACCOUNT = re.compile(
    r"(?:kis:kis_live|toss:toss_live|upbit:upbit):(?:[0-9a-f]{64}|identity_unknown)\Z"
)


def valid_account(value: str) -> bool:
    return bool(_ACCOUNT.fullmatch(value))


def valid_account_market(account: str, market: str) -> bool:
    if not valid_account(account):
        return False
    if market == "account":
        return True
    broker = account.split(":", 1)[0]
    return (
        market
        in {"kis": {"kr", "us"}, "toss": {"kr", "us"}, "upbit": {"crypto"}}[broker]
    )


def configured_fingerprint(
    broker: str, mode: str, identifier: str, secret: str
) -> str | None:
    import hashlib
    import hmac

    if not identifier or not secret:
        return None
    material = f"account-value-v1\0{broker}\0{mode}\0{identifier}".encode()
    digest = hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()
    return f"{broker}:{mode}:{digest}"
