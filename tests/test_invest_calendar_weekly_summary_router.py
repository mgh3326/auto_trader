"""Unit tests for weekly_summary_service."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_weekly_summary_partial_when_missing_days(monkeypatch) -> None:
    from app.services.invest_view_model import weekly_summary_service as svc

    # get_market_reports returns list[dict] (not ORM objects)
    fake_report_dict = {
        "report_date": date(2026, 5, 4).isoformat(),
        "market": "kr",
        "title": "Mon brief",
        "summary": "body",
    }

    async def fake_get(*, report_type, days):  # noqa: ARG001
        return [fake_report_dict] if report_type == "daily_brief" else []

    monkeypatch.setattr(svc, "get_market_reports", fake_get)
    resp = await svc.build_weekly_summary(db=MagicMock(), week_start=date(2026, 5, 4))
    assert resp.partial is True
    assert len(resp.sections) == 1
    assert resp.sections[0].date == date(2026, 5, 4)
    assert len(resp.missingDates) == 6
