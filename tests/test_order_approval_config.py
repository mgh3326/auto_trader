# tests/test_order_approval_config.py
import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings

_ALLOWED = {"off", "optional", "warn", "required"}


def test_order_approval_hash_mode_defaults_optional():
    assert settings.order_approval_hash_mode == "optional"
    assert settings.order_approval_hash_mode in _ALLOWED


def test_toss_approval_hash_mode_defaults_optional():
    assert settings.toss_approval_hash_mode == "optional"
    assert settings.toss_approval_hash_mode in _ALLOWED


@pytest.mark.parametrize("mode", sorted(_ALLOWED))
def test_approval_hash_mode_accepts_valid(mode):
    s = Settings(order_approval_hash_mode=mode, toss_approval_hash_mode=mode)
    assert s.order_approval_hash_mode == mode
    assert s.toss_approval_hash_mode == mode


@pytest.mark.parametrize(
    "field", ["order_approval_hash_mode", "toss_approval_hash_mode"]
)
def test_approval_hash_mode_rejects_typo_fail_loud(field):
    # ROB-659: a typo like "requird" must fail-loud at settings load rather than
    # silently degrading to optional-level behavior (the pre-fix footgun).
    with pytest.raises(ValidationError):
        Settings(**{field: "requird"})


@pytest.mark.parametrize(
    "field", ["order_approval_hash_mode", "toss_approval_hash_mode"]
)
def test_approval_hash_mode_normalizes_case_and_whitespace(field):
    # Case/whitespace are normalized so the exact-string comparisons at the read
    # sites ("off"/"required"/"warn") can never be defeated by "Required" / " required ".
    s = Settings(**{field: "  Required "})
    assert getattr(s, field) == "required"
