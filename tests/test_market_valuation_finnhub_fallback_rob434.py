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
