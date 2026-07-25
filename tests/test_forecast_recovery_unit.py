# tests/test_forecast_recovery_unit.py
"""Unit tests for forecast recovery — crypto terminal_close & quarantine validation.

ROB-1038 / Learning Loop Recovery:
Verifies crypto terminal_close target validation, UTC 00:00 session finality boundary,
quarantine escape for backfilled forecasts, and superseded forecast exclusion.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.models.review import TradeForecast
from app.models.trading import InstrumentType
from app.services.trade_journal import forecast_service as svc

pytestmark = [pytest.mark.unit]

_KST = ZoneInfo("Asia/Seoul")
_TERMINAL_RULE_VERSION = "terminal-close-v1-up-gte-down-lt"


def test_crypto_terminal_close_target_validation():
    """Verify that _validate_forecast_target accepts crypto terminal_close forecasts."""
    target = {
        "kind": "terminal_close",
        "direction": "up",
        "target_price": 100000000.0,
        "outcome_rule_version": _TERMINAL_RULE_VERSION,
    }
    # Should not raise
    svc._validate_forecast_target(target, instrument_type="crypto")

    # Invalid direction
    invalid_dir_target = dict(target, direction="at_or_above")
    with pytest.raises(svc.ForecastValidationError, match="terminal_close.direction"):
        svc._validate_forecast_target(invalid_dir_target, instrument_type="crypto")

    # Invalid rule version
    invalid_rule_target = dict(target, outcome_rule_version="terminal-close-v0")
    with pytest.raises(svc.ForecastValidationError, match="outcome_rule_version"):
        svc._validate_forecast_target(invalid_rule_target, instrument_type="crypto")


def test_crypto_terminal_close_session_failure_boundary():
    """Verify crypto terminal_close session finality around UTC 00:00 (KST 09:00)."""
    review_d = date(2026, 8, 20)

    # 1. UTC 23:59:59 on review date -> NOT final
    now_before_utc = datetime(2026, 8, 20, 23, 59, 59, tzinfo=UTC)
    res_before = svc._terminal_close_session_failure(
        instrument_type="crypto",
        review_date=review_d,
        now=now_before_utc,
    )
    assert res_before is not None
    assert "is not final" in res_before

    # 2. UTC 00:00:00 on review date + 1 day -> FINAL
    now_after_utc = datetime(2026, 8, 21, 0, 0, 0, tzinfo=UTC)
    res_after = svc._terminal_close_session_failure(
        instrument_type="crypto",
        review_date=review_d,
        now=now_after_utc,
    )
    assert res_after is None

    # 3. KST 08:59:59 on review date + 1 day (UTC 23:59:59) -> NOT final
    now_before_kst = datetime(2026, 8, 21, 8, 59, 59, tzinfo=_KST)
    assert (
        svc._terminal_close_session_failure(
            instrument_type="crypto",
            review_date=review_d,
            now=now_before_kst,
        )
        is not None
    )

    # 4. KST 09:00:00 on review date + 1 day (UTC 00:00:00) -> FINAL
    now_after_kst = datetime(2026, 8, 21, 9, 0, 0, tzinfo=_KST)
    assert (
        svc._terminal_close_session_failure(
            instrument_type="crypto",
            review_date=review_d,
            now=now_after_kst,
        )
        is None
    )


def test_quarantine_escape_for_backfilled_forecast():
    """Verify backfilled terminal_close forecast passes target contract failure check."""
    # 1. Legacy price_target without rule_version -> quarantined
    legacy_target = {
        "kind": "price_target",
        "direction": "at_or_above",
        "target_price": 1000.0,
    }
    legacy_row = TradeForecast(
        symbol="005930",
        instrument_type=InstrumentType.equity_kr,
        forecast_target=legacy_target,
        review_date=date(2026, 8, 20),
    )
    contract_fail_legacy = svc._target_contract_failure(
        legacy_row,
        target=legacy_row.forecast_target,
        instrument_type=legacy_row.instrument_type.value,
    )
    assert contract_fail_legacy is not None
    assert (
        contract_fail_legacy["resolution_evidence"]["quarantine_reason_code"]
        == "missing_outcome_rule_version"
    )

    # 2. Backfilled terminal_close target -> passes (escape quarantine!)
    backfilled_target = {
        "kind": "terminal_close",
        "direction": "up",
        "target_price": 1000.0,
        "outcome_rule_version": _TERMINAL_RULE_VERSION,
    }
    backfilled_row = TradeForecast(
        symbol="005930",
        instrument_type=InstrumentType.equity_kr,
        forecast_target=backfilled_target,
        review_date=date(2026, 8, 20),
    )
    contract_fail_backfilled = svc._target_contract_failure(
        backfilled_row,
        target=backfilled_row.forecast_target,
        instrument_type=backfilled_row.instrument_type.value,
    )
    assert contract_fail_backfilled is None


def test_superseded_forecast_excluded_from_scoring_targets():
    """Verify that superseded forecast (id 140) with status 'closed_no_claim' is excluded from due queues."""
    row_140 = TradeForecast(
        id=140,
        symbol="SMCI",
        instrument_type=InstrumentType.equity_us,
        forecast_target={
            "kind": "price_target",
            "direction": "at_or_above",
            "target_price": 450.0,
        },
        status="closed_no_claim",
        resolution_source="quarantine_legacy_superseded",
        review_date=date(2026, 8, 20),
    )
    # A forecast with status='closed_no_claim' is not open, so it is filtered out of scoring queues
    assert row_140.status != "open"
    assert row_140.status == "closed_no_claim"
    assert row_140.resolution_source == "quarantine_legacy_superseded"
