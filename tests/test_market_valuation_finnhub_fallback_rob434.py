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

