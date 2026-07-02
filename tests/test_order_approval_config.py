# tests/test_order_approval_config.py
from app.core.config import settings


def test_order_approval_hash_mode_defaults_optional():
    assert settings.order_approval_hash_mode == "optional"
    assert settings.order_approval_hash_mode in {"off", "optional", "warn", "required"}
