"""Shared default-disabled gate for every NHPLUG Stage 1 dispatch."""

from __future__ import annotations

import os

from app.services.brokers.nhplug.errors import NHPlugMockDisabled


def _mock_enabled() -> bool:
    """Return true only for the explicit, case-insensitive ``true`` value."""

    return os.getenv("NHPLUG_MOCK_ENABLED", "").strip().lower() == "true"


def _assert_mock_enabled() -> None:
    """Fail closed unless the operator explicitly armed mock reads."""

    if not _mock_enabled():
        raise NHPlugMockDisabled(
            "NHPLUG mock read access is disabled; set NHPLUG_MOCK_ENABLED=true"
        )


def _mock_orders_enabled() -> bool:
    """Return true only when the separately armed order mutation gate is set."""

    return os.getenv("NHPLUG_MOCK_ORDERS_ENABLED", "").strip().lower() == "true"


def _assert_mock_orders_enabled() -> None:
    """Require both the read boundary and the explicit order-mutation boundary."""

    _assert_mock_enabled()
    if not _mock_orders_enabled():
        raise NHPlugMockDisabled(
            "NHPLUG mock order access is disabled; set NHPLUG_MOCK_ORDERS_ENABLED=true"
        )
