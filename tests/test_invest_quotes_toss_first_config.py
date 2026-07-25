from __future__ import annotations

import pytest

from app.core.config import Settings

pytestmark = pytest.mark.unit


class TestInvestQuotesTossFirstFlags:
    def test_defaults_are_kis_first(self):
        s = Settings()
        assert s.invest_quotes_toss_first_kr is False
        assert s.invest_quotes_toss_first_us is False

    def test_kwarg_override(self):
        s = Settings(invest_quotes_toss_first_kr=True)
        assert s.invest_quotes_toss_first_kr is True
        assert s.invest_quotes_toss_first_us is False  # per-market independent

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("INVEST_QUOTES_TOSS_FIRST_KR", "true")
        monkeypatch.setenv("INVEST_QUOTES_TOSS_FIRST_US", "false")
        s = Settings()
        assert s.invest_quotes_toss_first_kr is True
        assert s.invest_quotes_toss_first_us is False
