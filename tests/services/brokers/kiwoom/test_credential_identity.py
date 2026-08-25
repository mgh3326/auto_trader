"""Kiwoom mock credential fingerprints are opaque and app-key scoped."""

from __future__ import annotations

import pytest

from app.services.brokers.kiwoom.credential_identity import (
    kiwoom_mock_credential_fingerprint,
)


def test_credential_fingerprint_is_deterministic_opaque_and_key_specific() -> None:
    app_key = "MOCK_KIWOOM_APP_KEY_MUST_NEVER_PERSIST"
    fingerprint = kiwoom_mock_credential_fingerprint(app_key)

    assert fingerprint == kiwoom_mock_credential_fingerprint(app_key)
    assert fingerprint != kiwoom_mock_credential_fingerprint(app_key + "-rotated")
    assert fingerprint.startswith("sha256:")
    assert app_key not in fingerprint


@pytest.mark.parametrize("invalid_key", ["", None, b"bytes-are-not-an-app-key"])
def test_credential_fingerprint_rejects_invalid_app_keys(invalid_key: object) -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        kiwoom_mock_credential_fingerprint(invalid_key)  # type: ignore[arg-type]
