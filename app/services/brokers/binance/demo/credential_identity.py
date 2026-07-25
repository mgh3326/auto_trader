"""Safe, deterministic identity for Binance Demo credentials.

The raw API key and secret must never enter ledger metadata, logs, or evidence.
API keys are high-entropy credential identifiers, so a domain-separated SHA-256
digest is sufficient to bind a root reservation to the exact credential used
without persisting either credential component.
"""

from __future__ import annotations

import hashlib

_DOMAIN = b"auto-trader:binance-demo-api-key:v1\x00"


def demo_credential_fingerprint(api_key: str) -> str:
    """Return an opaque fingerprint; never return or retain the raw key."""
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("api_key must be a non-empty string")
    digest = hashlib.sha256(_DOMAIN + api_key.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
