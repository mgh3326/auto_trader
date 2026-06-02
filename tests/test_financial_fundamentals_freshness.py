from __future__ import annotations

import datetime as dt

from app.services.financial_fundamentals_snapshots.freshness import row_data_state


def test_partial_when_filing_date_missing():
    assert row_data_state(filing_date=None) == "partial"


def test_fresh_when_filing_date_present():
    assert row_data_state(filing_date=dt.date(2026, 3, 20)) == "fresh"
