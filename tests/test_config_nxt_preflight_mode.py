from __future__ import annotations

import pytest

from app.core.config import Settings


@pytest.mark.unit
def test_default_mode_is_warn():
    assert Settings().toss_nxt_preflight_mode == "warn"


@pytest.mark.unit
def test_mode_normalized_case_insensitive():
    assert Settings(toss_nxt_preflight_mode=" Required ").toss_nxt_preflight_mode == (
        "required"
    )


@pytest.mark.unit
def test_invalid_mode_fails_loud():
    with pytest.raises(ValueError, match="approval hash mode"):
        Settings(toss_nxt_preflight_mode="blocky")
