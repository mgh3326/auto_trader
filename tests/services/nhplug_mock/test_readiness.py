"""Stage 2 activation requires exact operator confirmation values."""

from __future__ import annotations

import pytest

from app.services.brokers.nhplug.errors import NHPlugMockDisabled
from app.services.brokers.nhplug.gating import _assert_mock_enabled
from app.services.nhplug_mock.readiness import Stage2Disabled, Stage2Readiness


@pytest.mark.parametrize("value", ["TRUE", "True", " true ", "false", ""])
def test_mock_order_gate_requires_exact_true(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", value)
    for name in ("KEY", "TIME", "DB", "HOST", "VENDOR"):
        monkeypatch.setenv(f"NHPLUG_STAGE2_{name}_CONFIRMED", "true")
    with pytest.raises(Stage2Disabled, match="mock_gate_disabled"):
        Stage2Readiness.from_env().assert_ready()


@pytest.mark.parametrize("value", ["TRUE", "True", " true ", "false", ""])
def test_shared_mock_gate_requires_exact_true(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", value)
    with pytest.raises(NHPlugMockDisabled):
        _assert_mock_enabled()


def test_exact_true_allows_fully_confirmed_stage_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    for name in ("KEY", "TIME", "DB", "HOST", "VENDOR"):
        monkeypatch.setenv(f"NHPLUG_STAGE2_{name}_CONFIRMED", "true")
    _assert_mock_enabled()
    Stage2Readiness.from_env().assert_ready()
