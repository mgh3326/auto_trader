"""ROB-285 — Binance public market data adapter (read-only).

This package exposes ONLY read-only public REST + WS surfaces. Any code
that imports a Binance signed endpoint, account method, or API-key header
is a bug — see ``tests/services/brokers/binance/test_audit_no_signed_endpoints``
for the locked invariant.
"""
