"""ROB-26 settings smoke test."""

from app.core.config import settings


def test_research_run_refresh_defaults():
    assert settings.research_run_refresh_enabled is False
    assert settings.research_run_refresh_user_id is None
    assert settings.research_run_refresh_market_hours_only is True
