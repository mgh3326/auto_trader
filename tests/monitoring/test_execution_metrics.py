from __future__ import annotations

import logging

import pytest

from app.monitoring.execution_metrics import (
    account_kind_for_mode,
    record_execution_stage,
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
        ("binance_spot_demo", False, "mock"),
        ("upbit", True, "shadow"),
        (None, False, "shadow"),
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
