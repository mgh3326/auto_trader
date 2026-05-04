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


from pydantic import ValidationError


class TestExecutionGuard:
    def test_default_is_conservative(self):
        guard = ec.ExecutionGuard()
        assert guard.execution_allowed is False
        assert guard.approval_required is True
        assert guard.blocking_reasons == []
        assert guard.warnings == []

    def test_can_allow_execution_when_no_blocking_reasons(self):
        guard = ec.ExecutionGuard(execution_allowed=True, approval_required=False)
        assert guard.execution_allowed is True
        assert guard.approval_required is False

    def test_blocking_reasons_force_execution_not_allowed(self):
        with pytest.raises(ValidationError) as excinfo:
            ec.ExecutionGuard(execution_allowed=True, blocking_reasons=["x"])
        assert "blocking_reasons" in str(excinfo.value)

    def test_blocking_reasons_with_default_execution_allowed_is_ok(self):
        guard = ec.ExecutionGuard(blocking_reasons=["risk_too_high"])
        assert guard.execution_allowed is False
        assert guard.blocking_reasons == ["risk_too_high"]

    def test_warnings_do_not_force_execution_not_allowed(self):
        guard = ec.ExecutionGuard(execution_allowed=True, warnings=["soft warn"])
        assert guard.execution_allowed is True
        assert guard.warnings == ["soft warn"]


from datetime import datetime, timezone


class TestExecutionReadiness:
    def test_default_is_not_ready_with_conservative_guard(self):
        readiness = ec.ExecutionReadiness(
            account_mode="kis_mock",
            execution_source="preopen",
        )
        assert readiness.contract_version == "v1"
        assert readiness.account_mode == "kis_mock"
        assert readiness.execution_source == "preopen"
        assert readiness.is_ready is False
        assert readiness.guard.execution_allowed is False
        assert readiness.guard.approval_required is True
        assert readiness.checked_at is None
        assert readiness.notes == []

    def test_can_construct_ready_state_with_clean_guard(self):
        readiness = ec.ExecutionReadiness(
            account_mode="alpaca_paper",
            execution_source="manual",
            is_ready=True,
            guard=ec.ExecutionGuard(execution_allowed=True, approval_required=False),
            checked_at=datetime(2026, 5, 4, 10, 0, tzinfo=timezone.utc),
            notes=["operator confirmed"],
        )
        assert readiness.is_ready is True
        assert readiness.checked_at.year == 2026

    def test_is_ready_with_blocking_reasons_raises(self):
        with pytest.raises(ValidationError) as excinfo:
            ec.ExecutionReadiness(
                account_mode="kis_live",
                execution_source="watch",
                is_ready=True,
                guard=ec.ExecutionGuard(blocking_reasons=["market_closed"]),
            )
        assert "is_ready" in str(excinfo.value)

    def test_invalid_account_mode_rejected(self):
        with pytest.raises(ValidationError):
            ec.ExecutionReadiness(
                account_mode="binance_live",  # not in the literal
                execution_source="manual",
            )

    def test_invalid_execution_source_rejected(self):
        with pytest.raises(ValidationError):
            ec.ExecutionReadiness(
                account_mode="kis_mock",
                execution_source="cron",  # not in the literal
            )


from decimal import Decimal


class TestOrderPreviewLine:
    def _minimal_kwargs(self):
        return dict(
            symbol="005930",
            market="KOSPI",
            side="buy",
            account_mode="kis_mock",
            execution_source="preopen",
        )

    def test_defaults(self):
        line = ec.OrderPreviewLine(**self._minimal_kwargs())
        assert line.contract_version == "v1"
        assert line.lifecycle_state == "previewed"
        assert line.quantity is None
        assert line.limit_price is None
        assert line.notional is None
        assert line.currency is None
        assert line.guard.execution_allowed is False
        assert line.guard.approval_required is True
        assert line.rationale == []
        assert line.correlation_id is None

    def test_full_construction(self):
        line = ec.OrderPreviewLine(
            **self._minimal_kwargs(),
            quantity=Decimal("10"),
            limit_price=Decimal("70000"),
            notional=Decimal("700000"),
            currency="KRW",
            rationale=["RSI oversold", "above MA20"],
            correlation_id="decision_run_abc123",
        )
        assert line.quantity == Decimal("10")
        assert line.limit_price == Decimal("70000")
        assert line.notional == Decimal("700000")
        assert line.currency == "KRW"
        assert line.correlation_id == "decision_run_abc123"

    def test_invalid_side_rejected(self):
        kwargs = self._minimal_kwargs()
        kwargs["side"] = "hold"
        with pytest.raises(ValidationError):
            ec.OrderPreviewLine(**kwargs)

    def test_invalid_lifecycle_state_rejected(self):
        kwargs = self._minimal_kwargs()
        kwargs["lifecycle_state"] = "queued"
        with pytest.raises(ValidationError):
            ec.OrderPreviewLine(**kwargs)


class TestOrderBasketPreview:
    def _readiness(self, account="kis_mock", source="preopen"):
        return ec.ExecutionReadiness(
            account_mode=account,
            execution_source=source,
        )

    def _line(self, **overrides):
        kwargs = dict(
            symbol="005930",
            market="KOSPI",
            side="buy",
            account_mode="kis_mock",
            execution_source="preopen",
        )
        kwargs.update(overrides)
        return ec.OrderPreviewLine(**kwargs)

    def test_empty_basket_defaults(self):
        basket = ec.OrderBasketPreview(
            account_mode="kis_mock",
            execution_source="preopen",
            readiness=self._readiness(),
        )
        assert basket.contract_version == "v1"
        assert basket.lines == []
        assert basket.basket_warnings == []
        assert basket.readiness.is_ready is False

    def test_basket_with_matching_lines(self):
        basket = ec.OrderBasketPreview(
            account_mode="kis_mock",
            execution_source="preopen",
            readiness=self._readiness(),
            lines=[self._line(), self._line(symbol="000660")],
        )
        assert len(basket.lines) == 2

    def test_line_account_mode_mismatch_rejected(self):
        with pytest.raises(ValidationError) as excinfo:
            ec.OrderBasketPreview(
                account_mode="kis_mock",
                execution_source="preopen",
                readiness=self._readiness(),
                lines=[self._line(account_mode="alpaca_paper")],
            )
        assert "account_mode" in str(excinfo.value)

    def test_line_execution_source_mismatch_rejected(self):
        with pytest.raises(ValidationError) as excinfo:
            ec.OrderBasketPreview(
                account_mode="kis_mock",
                execution_source="preopen",
                readiness=self._readiness(),
                lines=[self._line(execution_source="watch")],
            )
        assert "execution_source" in str(excinfo.value)
