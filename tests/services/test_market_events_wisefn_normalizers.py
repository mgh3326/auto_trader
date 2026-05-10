"""WiseFn earnings normalizer tests (ROB-171)."""

from __future__ import annotations

from datetime import date

import pytest

WISEFN_ROW_SAMSUNG = {
    "stock_code": "005930",
    "corp_name": "삼성전자",
    "release_date": "2026-05-13",
    "fiscal_year": 2026,
    "fiscal_quarter": 1,
    "release_type": "scheduled",
    "title": "삼성전자 2026년 1분기 실적발표 예정",
    "time_hint": "after_close",
}

WISEFN_ROW_RELEASED = {
    **WISEFN_ROW_SAMSUNG,
    "release_type": "released",
    "title": "삼성전자 2026년 1분기 실적발표",
}


@pytest.mark.unit
def test_normalize_wisefn_basic_fields():
    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    event, values = normalize_wisefn_earnings_row(WISEFN_ROW_SAMSUNG)

    assert event["category"] == "earnings"
    assert event["market"] == "kr"
    assert event["country"] == "KR"
    assert event["symbol"] == "005930"
    assert event["company_name"] == "삼성전자"
    assert event["event_date"] == date(2026, 5, 13)
    assert event["time_hint"] == "after_close"
    assert event["source"] == "wisefn"
    assert event["fiscal_year"] == 2026
    assert event["fiscal_quarter"] == 1
    assert event["status"] == "scheduled"
    assert event["source_timezone"] == "Asia/Seoul"
    assert values == []  # Forward-looking schedule has no eps/revenue numbers.


@pytest.mark.unit
def test_normalize_wisefn_released_status():
    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    event, _ = normalize_wisefn_earnings_row(WISEFN_ROW_RELEASED)
    assert event["status"] == "released"


@pytest.mark.unit
def test_normalize_wisefn_uses_deterministic_source_event_id():
    """Re-normalizing the same row must yield the same source_event_id (idempotency)."""
    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    e1, _ = normalize_wisefn_earnings_row(WISEFN_ROW_SAMSUNG)
    e2, _ = normalize_wisefn_earnings_row(dict(WISEFN_ROW_SAMSUNG))

    assert e1["source_event_id"] == e2["source_event_id"]
    assert e1["source_event_id"] == "wisefn::005930::2026-05-13::2026::1"


@pytest.mark.unit
def test_normalize_wisefn_unknown_time_hint_falls_back():
    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    event, _ = normalize_wisefn_earnings_row(
        {**WISEFN_ROW_SAMSUNG, "time_hint": "garbage_value"}
    )
    assert event["time_hint"] == "unknown"


@pytest.mark.unit
def test_normalize_wisefn_missing_stock_code_raises():
    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    with pytest.raises(ValueError):
        normalize_wisefn_earnings_row({**WISEFN_ROW_SAMSUNG, "stock_code": ""})


@pytest.mark.unit
def test_normalize_wisefn_missing_release_date_raises():
    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    with pytest.raises(ValueError):
        normalize_wisefn_earnings_row({**WISEFN_ROW_SAMSUNG, "release_date": None})


@pytest.mark.unit
def test_normalize_wisefn_non_numeric_stock_code_raises():
    """KR tickers are 6-digit numeric; non-numeric is a row-shape error."""
    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    with pytest.raises(ValueError):
        normalize_wisefn_earnings_row(
            {**WISEFN_ROW_SAMSUNG, "stock_code": "BAD-CODE"}
        )


@pytest.mark.unit
def test_normalize_wisefn_payload_is_jsonable():
    """raw_payload_json must be JSONB-safe (no datetime objects)."""
    import json

    from app.services.market_events.normalizers import normalize_wisefn_earnings_row

    event, _ = normalize_wisefn_earnings_row(WISEFN_ROW_SAMSUNG)
    json.dumps(event["raw_payload_json"])  # must not raise
