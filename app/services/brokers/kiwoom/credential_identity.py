"""Safe, deterministic identity for Kiwoom mock credentials.

The raw app key and secret must never enter ledger metadata, logs, or evidence.
The mock app key is the approved high-entropy credential identifier, so a
domain-separated SHA-256 digest binds the lane to the exact app key without
persisting either credential component.
"""

from __future__ import annotations

import hashlib

_DOMAIN = b"auto-trader:kiwoom-mock-app-key:v1\x00"


def kiwoom_mock_credential_fingerprint(app_key: str) -> str:
    """Return an opaque fingerprint; never return or retain the raw app key."""
    if not isinstance(app_key, str) or not app_key:
        raise ValueError("app_key must be a non-empty string")
    digest = hashlib.sha256(_DOMAIN + app_key.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
