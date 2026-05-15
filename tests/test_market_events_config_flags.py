"""Market-events rolling scheduler config flag tests (ROB-208)."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_market_events_settings_have_safe_defaults() -> None:
    from app.core.config import settings

    assert settings.market_events_ingest_commit_enabled is False
    assert settings.market_events_rolling_window_days_back == 7
    assert settings.market_events_rolling_window_days_forward == 60
    assert settings.market_events_rolling_window_max_partitions_per_run == 90
