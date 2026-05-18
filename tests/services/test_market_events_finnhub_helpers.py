"""Tests for finnhub_helpers 429 mapping (ROB-264)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class _FakeFinnhubAPIException(Exception):
    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_earnings_calendar_finnhub_maps_429_to_quota_error(monkeypatch):
    from app.services.market_events import finnhub_helpers

    fake_client = MagicMock()
    fake_client.earnings_calendar.side_effect = _FakeFinnhubAPIException(
        status_code=429,
        message="API limit reached. Please try again later. Remaining Limit: 0",
    )
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: fake_client)

    with pytest.raises(finnhub_helpers.FinnhubQuotaExceededError) as exc_info:
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            None, "2026-05-11", "2026-07-17"
        )

    assert exc_info.value.status_code == 429
    assert "API limit" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_earnings_calendar_finnhub_passes_through_non_429(monkeypatch):
    from app.services.market_events import finnhub_helpers

    fake_client = MagicMock()
    fake_client.earnings_calendar.side_effect = _FakeFinnhubAPIException(
        status_code=500, message="upstream boom"
    )
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: fake_client)

    with pytest.raises(_FakeFinnhubAPIException):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            None, "2026-05-11", "2026-05-11"
        )
