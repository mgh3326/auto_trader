# tests/test_catalyst_polarity.py
import pytest

from app.services.market_events.catalyst.polarity import resolve_polarity


@pytest.mark.unit
def test_category_default_polarity():
    assert resolve_polarity("conference", None) == "positive"
    assert resolve_polarity("product_launch", None) == "positive"
    assert resolve_polarity("index_rebalance", None) == "positive"
    assert resolve_polarity("policy_regulation", None) == "negative"
    assert resolve_polarity("lockup_expiry", None) == "negative"
    assert resolve_polarity("corporate_event", None) == "neutral"


@pytest.mark.unit
def test_raw_payload_impact_hint_overrides_category():
    assert resolve_polarity("conference", {"impact_hint": "negative"}) == "negative"
    assert resolve_polarity("policy_regulation", {"impact_hint": "positive"}) == "positive"


@pytest.mark.unit
def test_invalid_hint_falls_back_to_category():
    assert resolve_polarity("conference", {"impact_hint": "bogus"}) == "positive"


@pytest.mark.unit
def test_unknown_category_is_neutral():
    assert resolve_polarity("some_unknown_cat", None) == "neutral"
