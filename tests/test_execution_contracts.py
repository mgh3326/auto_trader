"""Tests for shared execution contracts (ROB-100)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

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
            ec.TERMINAL_LIFECYCLE_STATES & ec.IN_FLIGHT_LIFECYCLE_STATES == frozenset()
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
            checked_at=datetime(2026, 5, 4, 10, 0, tzinfo=UTC),
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


class TestOrderPreviewLine:
    def _minimal_kwargs(self):
        return {
            "symbol": "005930",
            "market": "KOSPI",
            "side": "buy",
            "account_mode": "kis_mock",
            "execution_source": "preopen",
        }

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
        kwargs = {
            "symbol": "005930",
            "market": "KOSPI",
            "side": "buy",
            "account_mode": "kis_mock",
            "execution_source": "preopen",
        }
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


class TestOrderLifecycleEvent:
    def test_minimal_event(self):
        event = ec.OrderLifecycleEvent(
            account_mode="kis_mock",
            execution_source="reconciler",
            state="submitted",
            occurred_at=datetime(2026, 5, 4, 10, 0, tzinfo=UTC),
        )
        assert event.contract_version == "v1"
        assert event.state == "submitted"
        assert event.broker_order_id is None
        assert event.correlation_id is None
        assert event.detail == {}
        assert event.warnings == []

    def test_full_event(self):
        event = ec.OrderLifecycleEvent(
            account_mode="kis_live",
            execution_source="websocket",
            state="fill",
            occurred_at=datetime(2026, 5, 4, 10, 1, tzinfo=UTC),
            broker_order_id="0000123456",
            correlation_id="watch_alert_xyz",
            detail={"raw": {"FILL_QTY": "10"}},
            warnings=["partial_then_full"],
        )
        assert event.broker_order_id == "0000123456"
        assert event.detail["raw"]["FILL_QTY"] == "10"

    def test_invalid_state_rejected(self):
        with pytest.raises(ValidationError):
            ec.OrderLifecycleEvent(
                account_mode="kis_mock",
                execution_source="reconciler",
                state="queued",  # not in the literal
                occurred_at=datetime(2026, 5, 4, 10, 0, tzinfo=UTC),
            )


class TestSerializationRoundTrip:
    def _sample_models(self):
        return [
            ec.ExecutionGuard(
                execution_allowed=False,
                approval_required=True,
                blocking_reasons=["market_closed"],
                warnings=["soft warn"],
            ),
            ec.ExecutionReadiness(
                account_mode="kis_mock",
                execution_source="preopen",
                is_ready=False,
                guard=ec.ExecutionGuard(blocking_reasons=["news_stale"]),
                checked_at=datetime(2026, 5, 4, 9, 0, tzinfo=UTC),
                notes=["initial check"],
            ),
            ec.OrderPreviewLine(
                symbol="005930",
                market="KOSPI",
                side="buy",
                account_mode="kis_mock",
                execution_source="preopen",
                quantity=Decimal("10"),
                limit_price=Decimal("70000.5"),
                notional=Decimal("705005"),
                currency="KRW",
                rationale=["test"],
                correlation_id="decision_run_xyz",
            ),
            ec.OrderBasketPreview(
                account_mode="alpaca_paper",
                execution_source="manual",
                readiness=ec.ExecutionReadiness(
                    account_mode="alpaca_paper",
                    execution_source="manual",
                ),
                lines=[
                    ec.OrderPreviewLine(
                        symbol="AAPL",
                        market="NASDAQ",
                        side="buy",
                        account_mode="alpaca_paper",
                        execution_source="manual",
                        notional=Decimal("100"),
                        currency="USD",
                    )
                ],
            ),
            ec.OrderLifecycleEvent(
                account_mode="kis_live",
                execution_source="websocket",
                state="fill",
                occurred_at=datetime(2026, 5, 4, 10, 1, tzinfo=UTC),
                broker_order_id="0000123456",
                correlation_id="watch_alert_xyz",
                detail={"raw": {"FILL_QTY": "10"}},
            ),
        ]

    def test_python_round_trip(self):
        for model in self._sample_models():
            dumped = model.model_dump()
            restored = type(model).model_validate(dumped)
            assert restored == model

    def test_json_round_trip(self):
        for model in self._sample_models():
            dumped_json = model.model_dump_json()
            restored = type(model).model_validate_json(dumped_json)
            assert restored == model

    def test_contract_version_present_in_serialized_output(self):
        for model in self._sample_models():
            dumped = model.model_dump()
            if "contract_version" in type(model).model_fields:
                assert dumped["contract_version"] == "v1"

    def test_module_constant_matches_field_default(self):
        assert ec.CONTRACT_VERSION == "v1"


class TestCompatibilityWithExistingSchemas:
    def test_no_string_overlap_with_existing_order_intent_execution_mode(self):
        # Existing values in OrderIntentPreviewRequest.execution_mode.
        # Hard-coded to avoid coupling this test to that schema's import graph.
        existing_execution_mode_values = {
            "requires_final_approval",
            "paper_only",
            "dry_run_only",
        }
        new_vocab = ec.ACCOUNT_MODES | ec.EXECUTION_SOURCES | ec.ORDER_LIFECYCLE_STATES
        overlap = existing_execution_mode_values & new_vocab
        assert overlap == set(), (
            f"new vocabulary collides with existing execution_mode values: {overlap}"
        )

    def test_existing_order_intent_preview_request_still_imports(self):
        # Sanity check: the existing schema still loads and exposes the same
        # execution_mode literal we asserted above. Catches accidental damage.
        from app.schemas.order_intent_preview import OrderIntentPreviewRequest

        req = OrderIntentPreviewRequest()
        assert req.execution_mode == "requires_final_approval"


class TestModuleIsLeaf:
    def test_does_not_import_other_app_modules(self):
        import importlib
        import sys

        # Drop everything under app.* so we measure only what loading
        # execution_contracts pulls in. Capture and restore so the rest of
        # the test session is unaffected.
        snapshot = {
            name: mod for name, mod in sys.modules.items() if name.startswith("app")
        }
        for name in list(snapshot):
            del sys.modules[name]
        try:
            importlib.import_module("app.schemas.execution_contracts")
            loaded_app_modules = {
                name for name in sys.modules if name.startswith("app")
            }
            allowed = {"app", "app.schemas", "app.schemas.execution_contracts"}
            unexpected = loaded_app_modules - allowed
            assert unexpected == set(), (
                f"execution_contracts pulled in unexpected app.* modules: {unexpected}"
            )
        finally:
            for name in list(sys.modules):
                if name.startswith("app"):
                    del sys.modules[name]
            for name, mod in snapshot.items():
                sys.modules[name] = mod
