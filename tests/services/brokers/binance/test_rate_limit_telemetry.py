"""ROB-285 — Rate-limit header parser + telemetry."""

from __future__ import annotations

import logging

import pytest

from app.services.brokers.binance.rate_limit_telemetry import (
    RateLimitSnapshot,
    emit_rate_limit_snapshot,
    parse_rate_limit_headers,
)


def test_parses_used_weight_and_order_count() -> None:
    snap = parse_rate_limit_headers(
        {
            "X-MBX-USED-WEIGHT-1M": "150",
            "X-MBX-ORDER-COUNT-1M": "2",
        }
    )
    assert isinstance(snap, RateLimitSnapshot)
    assert snap.used_weight_1m == 150
    assert snap.order_count_1m == 2


def test_missing_headers_returns_none_fields() -> None:
    snap = parse_rate_limit_headers({})
    assert snap.used_weight_1m is None
    assert snap.order_count_1m is None


def test_emit_logs_structured_info(caplog) -> None:
    snap = RateLimitSnapshot(used_weight_1m=400, order_count_1m=5)
    with caplog.at_level(logging.INFO, logger="app.services.brokers.binance"):
        emit_rate_limit_snapshot(snap, declared_weight_limit=1200)
    records = [
        r
        for r in caplog.records
        if r.name.startswith("app.services.brokers.binance")
    ]
    assert any("binance.rate_limit" in r.message for r in records)


def test_emit_does_not_set_sentry_tag_below_threshold(monkeypatch) -> None:
    sentry_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.services.brokers.binance.rate_limit_telemetry._set_sentry_tag",
        lambda k, v: sentry_calls.append((k, v)),
    )
    snap = RateLimitSnapshot(used_weight_1m=300, order_count_1m=0)  # 25% used
    emit_rate_limit_snapshot(snap, declared_weight_limit=1200)
    assert sentry_calls == []


def test_emit_sets_sentry_tag_above_threshold(monkeypatch) -> None:
    sentry_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.services.brokers.binance.rate_limit_telemetry._set_sentry_tag",
        lambda k, v: sentry_calls.append((k, v)),
    )
    snap = RateLimitSnapshot(used_weight_1m=700, order_count_1m=0)  # 58% used
    emit_rate_limit_snapshot(snap, declared_weight_limit=1200)
    assert sentry_calls and sentry_calls[0][0] == "binance.rate_limit_weight_pct"


def test_emit_does_not_raise_when_sentry_sdk_is_missing(monkeypatch) -> None:
    """Telemetry must fail-open when sentry_sdk is not installed."""
    import builtins

    real_import = builtins.__import__

    def stub_import(name, *args, **kwargs):
        if name == "sentry_sdk":
            raise ImportError("simulated missing sentry_sdk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", stub_import)
    snap = RateLimitSnapshot(used_weight_1m=1000, order_count_1m=0)
    # Must not raise. Returns None.
    assert emit_rate_limit_snapshot(snap, declared_weight_limit=1200) is None


def test_emit_does_not_raise_when_sentry_set_tag_raises(monkeypatch) -> None:
    """Telemetry must fail-open when sentry_sdk.set_tag itself raises
    (e.g., not initialized in a way that surfaces as an exception in
    some sentry_sdk versions, or pathological misconfig)."""
    import sys
    import types

    fake_sentry = types.ModuleType("sentry_sdk")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated sentry misconfig")

    fake_sentry.set_tag = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sentry)
    snap = RateLimitSnapshot(used_weight_1m=1000, order_count_1m=0)
    # Must not raise.
    assert emit_rate_limit_snapshot(snap, declared_weight_limit=1200) is None
