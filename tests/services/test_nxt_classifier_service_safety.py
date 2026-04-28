"""Safety: NXT classifier service must stay pure."""

from __future__ import annotations

import pytest

from tests.services.pure_service_safety import assert_pure_service_import


@pytest.mark.unit
def test_nxt_classifier_service_is_pure() -> None:
    assert_pure_service_import("app.services.nxt_classifier_service")
