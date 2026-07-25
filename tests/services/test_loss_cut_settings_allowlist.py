import pytest

from app.core.config import Settings


@pytest.mark.unit
def test_loss_cut_allowlist_defaults_to_trader_agent():
    s = Settings()
    assert s.loss_cut_allowed_agent_ids == ["6b2192cc-14fa-4335-b572-2fe1e0cb54a7"]


@pytest.mark.unit
def test_loss_cut_allowlist_parses_comma_separated(monkeypatch):
    monkeypatch.setenv("LOSS_CUT_ALLOWED_AGENT_IDS", "aaa, bbb ,ccc")
    s = Settings()
    assert s.loss_cut_allowed_agent_ids == ["aaa", "bbb", "ccc"]
