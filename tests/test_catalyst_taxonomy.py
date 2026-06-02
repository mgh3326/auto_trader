# tests/test_catalyst_taxonomy.py
import pytest

from app.services.market_events.catalyst.polarity import CATALYST_CATEGORIES
from app.services.market_events.taxonomy import CATEGORIES, validate_category


@pytest.mark.unit
def test_catalyst_categories_added_to_taxonomy():
    for cat in ("conference", "corporate_event", "product_launch",
                "policy_regulation", "lockup_expiry", "index_rebalance"):
        assert cat in CATEGORIES
        validate_category(cat)  # 검증 통과(예외 없음)


@pytest.mark.unit
def test_existing_categories_preserved():
    for cat in ("earnings", "economic", "disclosure", "regulatory"):
        assert cat in CATEGORIES


@pytest.mark.unit
def test_catalyst_categories_constant_is_subset():
    assert CATALYST_CATEGORIES <= CATEGORIES
    assert "earnings" not in CATALYST_CATEGORIES  # earnings는 기존 catalyst-신규 아님
