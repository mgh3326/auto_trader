import pytest

from app.core.config import Settings


@pytest.mark.unit
def test_order_proposals_disabled_by_default():
    s = Settings(_env_file=None)
    assert s.ORDER_PROPOSALS_ENABLED is False
