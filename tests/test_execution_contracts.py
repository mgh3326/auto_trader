"""Tests for shared execution contracts (ROB-100)."""

import pytest

from app.schemas import execution_contracts as ec


class TestAccountMode:
    def test_account_modes_constant_matches_spec(self):
        assert ec.ACCOUNT_MODES == frozenset(
            {"kis_live", "kis_mock", "alpaca_paper", "db_simulated"}
        )


class TestExecutionSource:
    def test_execution_sources_constant_matches_spec(self):
        assert ec.EXECUTION_SOURCES == frozenset(
            {"preopen", "watch", "manual", "websocket", "reconciler"}
        )


class TestOrderLifecycleState:
    def test_order_lifecycle_states_constant_matches_spec(self):
        assert ec.ORDER_LIFECYCLE_STATES == frozenset(
            {
                "planned",
                "previewed",
                "submitted",
                "accepted",
                "pending",
                "fill",
                "reconciled",
                "stale",
                "failed",
                "anomaly",
            }
        )

    def test_terminal_states(self):
        assert ec.TERMINAL_LIFECYCLE_STATES == frozenset(
            {"fill", "reconciled", "failed", "stale"}
        )

    def test_in_flight_states(self):
        assert ec.IN_FLIGHT_LIFECYCLE_STATES == frozenset(
            {"submitted", "accepted", "pending"}
        )

    def test_terminal_and_in_flight_are_disjoint(self):
        assert (
            ec.TERMINAL_LIFECYCLE_STATES & ec.IN_FLIGHT_LIFECYCLE_STATES
            == frozenset()
        )

    def test_anomaly_is_in_neither_classification_set(self):
        assert "anomaly" not in ec.TERMINAL_LIFECYCLE_STATES
        assert "anomaly" not in ec.IN_FLIGHT_LIFECYCLE_STATES

    def test_planned_and_previewed_are_in_neither_classification_set(self):
        for state in ("planned", "previewed"):
            assert state not in ec.TERMINAL_LIFECYCLE_STATES
            assert state not in ec.IN_FLIGHT_LIFECYCLE_STATES

    def test_is_terminal_state_for_every_state(self):
        for state in ec.ORDER_LIFECYCLE_STATES:
            expected = state in ec.TERMINAL_LIFECYCLE_STATES
            assert ec.is_terminal_state(state) is expected, state

    def test_is_in_flight_state_for_every_state(self):
        for state in ec.ORDER_LIFECYCLE_STATES:
            expected = state in ec.IN_FLIGHT_LIFECYCLE_STATES
            assert ec.is_in_flight_state(state) is expected, state
