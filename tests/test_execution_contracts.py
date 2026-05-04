"""Tests for shared execution contracts (ROB-100)."""

import pytest

from app.schemas import execution_contracts as ec


class TestAccountMode:
    def test_account_modes_constant_matches_spec(self):
        assert ec.ACCOUNT_MODES == frozenset(
            {"kis_live", "kis_mock", "alpaca_paper", "db_simulated"}
        )
