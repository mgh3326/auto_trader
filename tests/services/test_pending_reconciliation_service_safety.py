"""Safety: pending reconciliation service must stay pure."""

from __future__ import annotations

import pytest

from tests.services.pure_service_safety import assert_pure_service_import


@pytest.mark.unit
def test_pending_reconciliation_service_is_pure() -> None:
    assert_pure_service_import("app.services.pending_reconciliation_service")
