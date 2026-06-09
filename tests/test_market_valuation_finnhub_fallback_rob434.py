"""ROB-434: US market_valuation Finnhub fallback (field-fill).

When yahoo .info leaves valuation fields null (or fails), backfill the missing
fields from Finnhub company_basic_financials. source stays 'yahoo'; per-field
provenance in raw_payload['_field_provenance']; default-off, inert without key.
"""

from __future__ import annotations

import datetime as dt

import pytest


@pytest.mark.unit
def test_settings_flag_defaults_off() -> None:
    from app.core.config import settings

    assert settings.market_valuation_finnhub_fallback_enabled is False


@pytest.mark.unit
def test_resolve_raw_value_priority_and_truthiness() -> None:
    from app.services.market_valuation_snapshots.builder import _resolve_raw_value

    # canonical lowercase key wins over the yahoo key
    assert _resolve_raw_value({"roe": 22.0, "ROE": 9.9}, "roe") == 22.0
    # falls back to the yahoo key when canonical absent
    assert _resolve_raw_value({"marketCap": 1234}, "market_cap") == 1234
    # 0/None/absent → None (truthiness, matches _payload_from_raw's or-chain)
    assert _resolve_raw_value({"per": 0.0, "PER": 0}, "per") is None
    assert _resolve_raw_value({}, "high_52w_date") is None


@pytest.mark.unit
def test_map_finnhub_metrics_unit_traps() -> None:
    from app.services.market_valuation_snapshots.finnhub_fallback import (
        _map_finnhub_metrics,
    )

    out = _map_finnhub_metrics(
        {
            "roeTTM": 22.0,  # already percent → NOT ×100
            "peTTM": 8.0,
            "pbAnnual": 0.9,
            "dividendYieldIndicatedAnnual": 3.0,  # percent → ÷100 ratio
            "marketCapitalization": 1500.0,  # millions → ×1e6 absolute
            "52WeekHigh": 110.0,
            "52WeekLow": 80.0,
            "52WeekHighDate": "2026-03-14",
        }
    )
    assert out["roe"] == 22.0  # critical: not 2200
    assert out["per"] == 8.0
    assert out["pbr"] == 0.9
    assert out["dividend_yield"] == 0.03
    assert out["market_cap"] == 1_500_000_000.0
    assert out["high_52w"] == 110.0
    assert out["low_52w"] == 80.0
    assert out["high_52w_date"] == "2026-03-14"  # iso str (JSON-safe, parsed later)


@pytest.mark.unit
def test_map_finnhub_metrics_fail_closed_on_missing_and_nonfinite() -> None:
    from app.services.market_valuation_snapshots.finnhub_fallback import (
        _map_finnhub_metrics,
    )

    out = _map_finnhub_metrics(
        {"roeTTM": None, "peTTM": "n/a", "marketCapitalization": float("inf")}
    )
    assert out == {}  # nothing fabricated; non-finite/None/unparseable dropped


@pytest.mark.unit
def test_map_finnhub_metrics_bad_date_dropped() -> None:
    from app.services.market_valuation_snapshots.finnhub_fallback import (
        _map_finnhub_metrics,
    )

    assert "high_52w_date" not in _map_finnhub_metrics({"52WeekHighDate": ""})
    assert "high_52w_date" not in _map_finnhub_metrics({"52WeekHighDate": None})


