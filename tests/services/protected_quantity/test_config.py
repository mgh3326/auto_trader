"""Rollout configuration remains dark until a later approved change."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

pytestmark = pytest.mark.unit


def test_protected_quantity_modes_default_to_off() -> None:
    settings = Settings(_env_file=None)

    assert settings.protected_quantity_mode_kis_live == "off"
    assert settings.protected_quantity_mode_toss_live == "off"
    assert settings.protected_quantity_mode_upbit_live == "off"
    assert settings.protected_quantity_toss_sellable_verified is False
    assert settings.protected_quantity_kis_kr_amend_broker_capped_verified is False


def test_shadow_is_the_only_non_off_deployment_mode_in_this_pr() -> None:
    settings = Settings(
        _env_file=None,
        protected_quantity_mode_kis_live="shadow",
        protected_quantity_mode_toss_live="shadow",
        protected_quantity_mode_upbit_live="shadow",
    )

    assert settings.protected_quantity_mode_kis_live == "shadow"
    assert settings.protected_quantity_mode_toss_live == "shadow"
    assert settings.protected_quantity_mode_upbit_live == "shadow"


@pytest.mark.parametrize(
    "field",
    [
        "protected_quantity_mode_kis_live",
        "protected_quantity_mode_toss_live",
        "protected_quantity_mode_upbit_live",
    ],
)
def test_enforce_is_structurally_unreachable_pending_separate_approval(
    field: str,
) -> None:
    values: dict[str, object] = {field: "enforce"}
    if field == "protected_quantity_mode_toss_live":
        values["protected_quantity_toss_sellable_verified"] = True

    with pytest.raises(ValidationError, match="separately operator-approved"):
        Settings(_env_file=None, **values)


def test_toss_enforce_first_requires_q13_evidence() -> None:
    with pytest.raises(ValidationError, match="TOSS_SELLABLE_VERIFIED"):
        Settings(
            _env_file=None,
            protected_quantity_mode_toss_live="enforce",
            protected_quantity_toss_sellable_verified=False,
        )


def test_kis_kr_broker_capped_amend_promotion_is_unreachable_in_this_pr() -> None:
    with pytest.raises(ValidationError, match="separately operator-approved"):
        Settings(
            _env_file=None,
            protected_quantity_kis_kr_amend_broker_capped_verified=True,
        )
