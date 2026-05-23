"""ROB-299 — Futures Demo no-secret env readiness."""

from __future__ import annotations

from app.services.brokers.binance.futures_demo.readiness import (
    evaluate_futures_demo_env_readiness,
)


def test_reports_all_missing_at_once_without_raising(monkeypatch):
    for k in (
        "BINANCE_FUTURES_DEMO_ENABLED",
        "BINANCE_FUTURES_DEMO_API_KEY",
        "BINANCE_FUTURES_DEMO_API_SECRET",
        "BINANCE_FUTURES_DEMO_BASE_URL",
    ):
        monkeypatch.delenv(k, raising=False)
    r = evaluate_futures_demo_env_readiness()
    assert r.ready is False
    assert "BINANCE_FUTURES_DEMO_API_KEY" in r.missing
    assert "BINANCE_FUTURES_DEMO_API_SECRET" in r.missing


def test_no_secret_values_in_evidence(monkeypatch):
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "SUPER_SECRET_KEY_VALUE")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "SUPER_SECRET_SECRET_VALUE")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", "https://demo-fapi.binance.com")
    ev = evaluate_futures_demo_env_readiness().to_evidence_dict()
    blob = repr(ev)
    assert "SUPER_SECRET_KEY_VALUE" not in blob
    assert "SUPER_SECRET_SECRET_VALUE" not in blob
    assert ev["api_key_present"] is True
    assert ev["base_url_host_allowed"] is True
    assert ev["ready"] is True


def test_ignores_spot_and_testnet_env(monkeypatch):
    for k in (
        "BINANCE_FUTURES_DEMO_ENABLED",
        "BINANCE_FUTURES_DEMO_API_KEY",
        "BINANCE_FUTURES_DEMO_API_SECRET",
        "BINANCE_FUTURES_DEMO_BASE_URL",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "spot-key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "spot-secret")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "testnet-key")
    r = evaluate_futures_demo_env_readiness()
    assert r.ready is False
    assert r.api_key_present is False
    assert r.api_secret_present is False


def test_canonical_demo_creds_make_ready(monkeypatch):
    """ROB-302: canonical BINANCE_DEMO_* pair satisfies futures readiness."""
    for k in (
        "BINANCE_FUTURES_DEMO_API_KEY",
        "BINANCE_FUTURES_DEMO_API_SECRET",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "CANON_KEY_VALUE")
    monkeypatch.setenv("BINANCE_DEMO_API_SECRET", "CANON_SECRET_VALUE")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", "https://demo-fapi.binance.com")
    ev = evaluate_futures_demo_env_readiness().to_evidence_dict()
    assert ev["ready"] is True
    assert ev["api_key_present"] is True
    assert ev["credential_source"] == "shared_demo_env"
    assert "CANON_KEY_VALUE" not in repr(ev)
    assert "CANON_SECRET_VALUE" not in repr(ev)


def test_futures_specific_creds_label_source(monkeypatch):
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "k")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "s")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", "https://demo-fapi.binance.com")
    ev = evaluate_futures_demo_env_readiness().to_evidence_dict()
    assert ev["credential_source"] == "futures_demo_env"
    assert ev["ready"] is True


def test_host_judgment_rejects_non_demo_host_without_raising(monkeypatch):
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "k")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "s")
    monkeypatch.setenv(
        "BINANCE_FUTURES_DEMO_BASE_URL", "https://testnet.binancefuture.com"
    )
    r = evaluate_futures_demo_env_readiness()
    assert r.base_url_host_allowed is False
    assert r.ready is False
