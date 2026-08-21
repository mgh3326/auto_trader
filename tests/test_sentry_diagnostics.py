"""Tests for app.monitoring.sentry_diagnostics — the non-secret diagnostics
surface used by the operator canary CLI and any future health endpoint.

The contract under test: the returned dict is exactly the allowlisted field
set (no accidental extra keys, especially no DSN/secret), and its values
reflect settings without ever echoing the raw DSN string.
"""

from __future__ import annotations

import pytest

from app.monitoring import sentry_diagnostics


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch):
    monkeypatch.setattr(sentry_diagnostics.settings, "SENTRY_DSN", "")
    monkeypatch.setattr(sentry_diagnostics.settings, "SENTRY_TRACES_SAMPLE_RATE", 1.0)
    monkeypatch.setattr(sentry_diagnostics.settings, "SENTRY_PROFILES_SAMPLE_RATE", 1.0)


@pytest.mark.unit
def test_diagnostics_returns_exactly_the_allowlisted_fields():
    result = sentry_diagnostics.get_sentry_diagnostics("api")
    assert set(result) == sentry_diagnostics.DIAGNOSTICS_FIELDS


@pytest.mark.unit
def test_diagnostics_disabled_when_dsn_empty():
    result = sentry_diagnostics.get_sentry_diagnostics("api")
    assert result["enabled"] is False
    assert result["profiler_ready"] is False


@pytest.mark.unit
def test_diagnostics_enabled_and_profiler_ready_with_dsn_and_positive_rate(
    monkeypatch,
):
    monkeypatch.setattr(
        sentry_diagnostics.settings, "SENTRY_DSN", "not-a-real-secret-fixture"
    )
    monkeypatch.setattr(
        sentry_diagnostics.settings, "SENTRY_PROFILES_SAMPLE_RATE", 0.5
    )

    result = sentry_diagnostics.get_sentry_diagnostics("mcp")

    assert result["enabled"] is True
    assert result["profiler_ready"] is True
    assert result["traces_sample_rate"] == 1.0
    assert result["profiles_sample_rate"] == 0.5


@pytest.mark.unit
def test_diagnostics_profiler_not_ready_when_sample_rate_zero(monkeypatch):
    monkeypatch.setattr(
        sentry_diagnostics.settings, "SENTRY_DSN", "not-a-real-secret-fixture"
    )
    monkeypatch.setattr(
        sentry_diagnostics.settings, "SENTRY_PROFILES_SAMPLE_RATE", 0.0
    )

    result = sentry_diagnostics.get_sentry_diagnostics("worker")

    assert result["enabled"] is True
    assert result["profiler_ready"] is False


@pytest.mark.unit
def test_diagnostics_never_echoes_the_dsn_value(monkeypatch):
    fake_dsn = "https://fakepublickey-should-never-leak@fake.invalid.example/1"
    monkeypatch.setattr(sentry_diagnostics.settings, "SENTRY_DSN", fake_dsn)

    result = sentry_diagnostics.get_sentry_diagnostics("api")

    assert fake_dsn not in repr(result)
    assert "fakepublickey-should-never-leak" not in repr(result)


@pytest.mark.unit
def test_diagnostics_process_kind_is_echoed_verbatim():
    result = sentry_diagnostics.get_sentry_diagnostics("taskiq-scheduler")
    assert result["process_kind"] == "taskiq-scheduler"
