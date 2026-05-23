"""ROB-302 — shared Demo credential resolver.

The Spot and Futures Demo lanes share ONE Binance Demo credential. To avoid
duplicating the same secret across two env var pairs, a canonical
``BINANCE_DEMO_API_KEY`` / ``BINANCE_DEMO_API_SECRET`` pair is read by both
lanes. Per-product vars (``BINANCE_{SPOT,FUTURES}_DEMO_API_*``) remain optional
overrides.

Resolution rules (verified against Codex review #2 — never mix key/secret
across sources):

    1. If EITHER product-specific var is set, the PAIR must come from product
       vars. A half-set product override fails closed (never backfills the
       missing half from canonical).
    2. Else the canonical pair is used (both halves, or fail closed).
    3. Else MissingCredentials.

Isolation invariant: a Spot-specific var never resolves for Futures and vice
versa. Crossing happens ONLY through the explicit canonical var.
"""

from __future__ import annotations

import pytest

from app.services.brokers.binance.demo.credentials import (
    ResolvedDemoCredential,
    inspect_demo_credential,
    resolve_demo_credentials,
)
from app.services.brokers.binance.demo.errors import (
    BinanceDemoIncompleteCredentialOverride,
    BinanceDemoMissingCredentials,
)

_CANON_KEY = "BINANCE_DEMO_API_KEY"
_CANON_SECRET = "BINANCE_DEMO_API_SECRET"
_PRODUCT_VARS = {
    "spot": ("BINANCE_SPOT_DEMO_API_KEY", "BINANCE_SPOT_DEMO_API_SECRET"),
    "futures": ("BINANCE_FUTURES_DEMO_API_KEY", "BINANCE_FUTURES_DEMO_API_SECRET"),
}
_ALL_VARS = [
    _CANON_KEY,
    _CANON_SECRET,
    *_PRODUCT_VARS["spot"],
    *_PRODUCT_VARS["futures"],
]


@pytest.fixture(autouse=True)
def _clear_demo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("product", ["spot", "futures"])
def test_product_specific_pair_wins(monkeypatch, product):
    key_env, secret_env = _PRODUCT_VARS[product]
    monkeypatch.setenv(key_env, "prod-key")
    monkeypatch.setenv(secret_env, "prod-secret")
    # canonical also present — product override must win
    monkeypatch.setenv(_CANON_KEY, "canon-key")
    monkeypatch.setenv(_CANON_SECRET, "canon-secret")

    resolved = resolve_demo_credentials(product, dict(__import__("os").environ))

    assert resolved.api_key == "prod-key"
    assert resolved.api_secret == "prod-secret"
    assert resolved.credential_source == f"{product}_demo_env"


@pytest.mark.parametrize("product", ["spot", "futures"])
def test_canonical_pair_used_when_product_absent(monkeypatch, product):
    monkeypatch.setenv(_CANON_KEY, "canon-key")
    monkeypatch.setenv(_CANON_SECRET, "canon-secret")

    resolved = resolve_demo_credentials(product, dict(__import__("os").environ))

    assert resolved.api_key == "canon-key"
    assert resolved.api_secret == "canon-secret"
    assert resolved.credential_source == "shared_demo_env"


@pytest.mark.parametrize("product", ["spot", "futures"])
def test_missing_everything_fails_closed(monkeypatch, product):
    with pytest.raises(BinanceDemoMissingCredentials):
        resolve_demo_credentials(product, {})


@pytest.mark.parametrize("product", ["spot", "futures"])
@pytest.mark.parametrize("present_half", ["key", "secret"])
def test_partial_product_override_fails_closed_no_canonical_backfill(
    monkeypatch, product, present_half
):
    """Codex #2: a half-set product override must NOT pair with a canonical half."""
    key_env, secret_env = _PRODUCT_VARS[product]
    if present_half == "key":
        monkeypatch.setenv(key_env, "prod-key")
    else:
        monkeypatch.setenv(secret_env, "prod-secret")
    # canonical fully present — must NOT be used to complete the product pair
    monkeypatch.setenv(_CANON_KEY, "canon-key")
    monkeypatch.setenv(_CANON_SECRET, "canon-secret")

    with pytest.raises(BinanceDemoIncompleteCredentialOverride):
        resolve_demo_credentials(product, dict(__import__("os").environ))


@pytest.mark.parametrize("present_half", ["key", "secret"])
def test_partial_canonical_fails_closed(monkeypatch, present_half):
    if present_half == "key":
        monkeypatch.setenv(_CANON_KEY, "canon-key")
    else:
        monkeypatch.setenv(_CANON_SECRET, "canon-secret")
    with pytest.raises(BinanceDemoIncompleteCredentialOverride):
        resolve_demo_credentials("futures", dict(__import__("os").environ))


def test_spot_specific_var_does_not_resolve_for_futures(monkeypatch):
    """Isolation: spot-specific vars are not in the futures resolution chain."""
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "spot-key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "spot-secret")
    with pytest.raises(BinanceDemoMissingCredentials):
        resolve_demo_credentials("futures", dict(__import__("os").environ))


def test_no_secret_value_in_repr(monkeypatch):
    resolved = ResolvedDemoCredential(
        api_key="SUPER_SECRET_KEY",
        api_secret="SUPER_SECRET_SECRET",
        credential_source="shared_demo_env",
    )
    blob = repr(resolved)
    assert "SUPER_SECRET_KEY" not in blob
    assert "SUPER_SECRET_SECRET" not in blob
    assert "shared_demo_env" in blob


def test_inspect_reports_presence_and_source_without_secret(monkeypatch):
    monkeypatch.setenv(_CANON_KEY, "SUPER_SECRET_KEY")
    monkeypatch.setenv(_CANON_SECRET, "SUPER_SECRET_SECRET")
    inspection = inspect_demo_credential("futures", dict(__import__("os").environ))
    blob = repr(inspection)
    assert inspection.api_key_present is True
    assert inspection.api_secret_present is True
    assert inspection.credential_source == "shared_demo_env"
    assert inspection.incomplete is False
    assert "SUPER_SECRET_KEY" not in blob
    assert "SUPER_SECRET_SECRET" not in blob


def test_inspect_missing(monkeypatch):
    inspection = inspect_demo_credential("futures", {})
    assert inspection.api_key_present is False
    assert inspection.api_secret_present is False
    assert inspection.credential_source is None
    assert inspection.incomplete is False


def test_inspect_partial_marks_incomplete(monkeypatch):
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "prod-key")
    inspection = inspect_demo_credential("futures", dict(__import__("os").environ))
    assert inspection.incomplete is True
    assert inspection.credential_source is None
