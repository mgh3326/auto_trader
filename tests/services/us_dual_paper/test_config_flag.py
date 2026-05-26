import pytest

from app.core.config import Settings


@pytest.mark.unit
def test_us_dual_paper_preview_disabled_by_default():
    s = Settings(_env_file=None)
    assert s.us_dual_paper_preview_enabled is False


@pytest.mark.unit
def test_us_dual_paper_preview_enabled_from_env(monkeypatch):
    monkeypatch.setenv("US_DUAL_PAPER_PREVIEW_ENABLED", "true")
    s = Settings(_env_file=None)
    assert s.us_dual_paper_preview_enabled is True
