"""Canonical Redis key names for broker token and approval-key caches."""

import hashlib


def kis_live_token_keys() -> tuple[str, str]:
    """Return the KIS live access-token key and its issuance lock key."""
    return "kis:access_token", "kis:token:lock"


def kis_mock_token_keys(*, netloc: str, app_key: str) -> tuple[str, str]:
    """Return KIS mock token keys scoped to a host and app-key fingerprint."""
    app_key_fingerprint = hashlib.sha256(app_key.encode()).hexdigest()[:16]
    namespace = f"kis_mock:{netloc.lower()}:{app_key_fingerprint}"
    return f"{namespace}:access_token", f"{namespace}:token:lock"


def toss_client_fingerprint(client_id: str) -> str:
    """Return the existing non-secret Toss client-id fingerprint."""
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]


def toss_oauth_token_keys(*, client_id: str) -> tuple[str, str]:
    """Return Toss OAuth token keys scoped to a client-id fingerprint."""
    client_id_fingerprint = toss_client_fingerprint(client_id)
    return toss_oauth_token_keys_for_fingerprint(client_id_fingerprint)


def toss_oauth_token_keys_for_fingerprint(
    client_fingerprint: str,
) -> tuple[str, str]:
    """Return Toss OAuth token keys from a validated non-secret fp16."""
    if len(client_fingerprint) != 16 or any(
        char not in "0123456789abcdefABCDEF" for char in client_fingerprint
    ):
        raise ValueError("Toss client fingerprint must be 16 hexadecimal characters")
    namespace = f"toss:oauth:{client_fingerprint.lower()}"
    return f"{namespace}:access_token", f"{namespace}:lock"


def kis_websocket_approval_keys(*, account_mode: str) -> tuple[str, str]:
    """Return the KIS WebSocket approval-key cache and lock keys."""
    keys_by_account_mode = {
        "kis_live": (
            "kis:websocket:approval_key",
            "kis:websocket:approval_key:lock",
        ),
        "kis_mock": (
            "kis_mock:websocket:approval_key",
            "kis_mock:websocket:approval_key:lock",
        ),
    }
    return keys_by_account_mode[account_mode]
