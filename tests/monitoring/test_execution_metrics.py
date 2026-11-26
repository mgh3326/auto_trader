from __future__ import annotations

import logging
import sys
from io import StringIO

import pytest

from app.monitoring.execution_metrics import (
    account_kind_for_mode,
    configure_execution_metrics_logging,
    execution_stage,
    record_execution_stage,
    record_reconnect_recovery,
    record_stale_quote_block,
)


@pytest.mark.unit
def test_execution_stage_emits_named_count_and_latency_with_bounded_labels(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.monitoring.execution_metrics"):
        record_execution_stage(
            venue="upbit",
            market="crypto",
            account_kind="mock",
            stage="broker_accept",
            outcome="success",
            duration_seconds=0.125,
        )

    assert len(caplog.records) == 2
    messages = [record.getMessage() for record in caplog.records]
    assert "metric=execution_stage_total" in messages[0]
    assert "metric=execution_stage_duration_seconds" in messages[1]
    for message in messages:
        assert "venue=upbit" in message
        assert "market=crypto" in message
        assert "account_kind=mock" in message
        assert "stage=broker_accept" in message
        assert "outcome=success" in message
        assert "symbol=" not in message
        assert "account_number=" not in message
        assert "order_id=" not in message
    assert "value=0.125000000" in messages[1]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("account_mode", "shadow", "expected"),
    [
        ("kis_live", False, "live"),
        ("kis_mock", False, "mock"),
        ("kis_mock_us", False, "mock"),
        ("other_mock_us", False, "mock"),
        ("binance_spot_demo", False, "mock"),
        ("upbit", True, "shadow"),
        (None, False, None),
        ("unexpected-account-mode", False, None),
    ],
)
def test_account_kind_is_closed_and_does_not_expose_account_mode(
    account_mode: str | None, shadow: bool, expected: str
) -> None:
    assert account_kind_for_mode(account_mode, shadow=shadow) == expected


@pytest.mark.unit
def test_execution_stage_rejects_unbounded_values_without_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.monitoring.execution_metrics"):
        record_execution_stage(
            venue="upbit",  # type: ignore[arg-type]
            market="crypto",
            account_kind="mock",
            stage="gate",
            outcome="blocked",
            duration_seconds=float("nan"),
        )

    assert caplog.records == []


@pytest.mark.unit
def test_execution_stage_emits_failure_observations_when_fake_operation_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.monitoring.execution_metrics"):
        with pytest.raises(RuntimeError, match="fake broker failure"):
            with execution_stage(
                venue="kis",
                market="kr",
                account_kind="mock",
                stage="broker_accept",
            ):
                raise RuntimeError("fake broker failure")

    assert len(caplog.records) == 2
    assert all("outcome=failure" in record.getMessage() for record in caplog.records)


@pytest.mark.unit
@pytest.mark.parametrize("outcome", ["blocked", "uncertain"])
def test_execution_stage_preserves_explicit_outcome_when_operation_raises(
    caplog: pytest.LogCaptureFixture, outcome: str
) -> None:
    with caplog.at_level(logging.INFO, logger="app.monitoring.execution_metrics"):
        with pytest.raises(RuntimeError, match="fake send failure"):
            with execution_stage(
                venue="kis",
                market="kr",
                account_kind="mock",
                stage="broker_accept",
            ) as observation:
                observation.mark(outcome)  # type: ignore[arg-type]
                raise RuntimeError("fake send failure")

    assert len(caplog.records) == 2
    assert all(f"outcome={outcome}" in record.getMessage() for record in caplog.records)


@pytest.mark.unit
def test_execution_metrics_logger_emits_info_to_stderr_without_stdout_json_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    configure_execution_metrics_logging()
    configure_execution_metrics_logging()
    try:
        record_execution_stage(
            venue="kis",
            market="kr",
            account_kind="mock",
            stage="receive",
            outcome="success",
            duration_seconds=0.1,
        )
    finally:
        metric_logger = logging.getLogger("app.monitoring.execution_metrics")
        for handler in tuple(metric_logger.handlers):
            if getattr(handler, "_execution_metrics_handler", False):
                metric_logger.removeHandler(handler)
                handler.close()

    assert "metric=execution_stage_total" in stream.getvalue()
    assert stream.getvalue().count("metric=execution_stage_total") == 1


@pytest.mark.unit
def test_reconnect_and_stale_quote_events_have_bounded_names_and_labels(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.monitoring.execution_metrics"):
        record_reconnect_recovery(
            venue="kis",
            market="multi",
            account_kind="live",
            outcome="success",
            duration_seconds=1.25,
        )
        record_stale_quote_block(venue="kis", market="kr", account_kind="mock")

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 3
    assert "metric=execution_reconnect_recovery_total" in messages[0]
    assert "metric=execution_reconnect_recovery_seconds" in messages[1]
    assert "stage=reconnect_recovery" in messages[1]
    assert "market=multi" in messages[1]
    assert "metric=execution_stale_quote_blocks_total" in messages[2]
    assert "stage=gate" in messages[2]
    assert "reason=stale_quote" in messages[2]


@pytest.mark.unit
@pytest.mark.parametrize(
    "stage", ["receive", "detect", "gate", "broker_accept", "fill_reconcile"]
)
def test_each_execution_stage_uses_the_same_fixed_metric_names_and_dimensions(
    caplog: pytest.LogCaptureFixture, stage: str
) -> None:
    with caplog.at_level(logging.INFO, logger="app.monitoring.execution_metrics"):
        record_execution_stage(
            venue="alpaca",
            market="us",
            account_kind="mock",
            stage=stage,  # type: ignore[arg-type]
            outcome="blocked",
            duration_seconds=0.25,
        )

    assert len(caplog.records) == 2
    messages = [record.getMessage() for record in caplog.records]
    assert "metric=execution_stage_total" in messages[0]
    assert "metric=execution_stage_duration_seconds" in messages[1]
    for message in messages:
        assert f"stage={stage}" in message
        assert "venue=alpaca" in message
        assert "market=us" in message
        assert "account_kind=mock" in message
        assert "outcome=blocked" in message
        assert not any(
            key in message
            for key in ("symbol=", "account_number=", "order_id=", "account_id=")
        )
