"""Conftest for backtest tests — overrides app-level autouse fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _mock_nxt_eligible():
    """No-op override: backtest tests do not use KIS broker services."""
    return None


@pytest.fixture(autouse=True)
def mock_auth_middleware_db():
    """No-op override: backtest tests do not use auth middleware."""
    return None


@pytest.fixture(autouse=True)
def reset_auth_mock_db():
    """No-op override: backtest tests do not use auth mock DB."""
    return None
