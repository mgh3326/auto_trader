import pytest

from app.services.symbol_analysis.floor import floored_action, insufficient_inputs


@pytest.mark.unit
def test_insufficient_inputs_lists_missing_core_fields():
    assert insufficient_inputs(
        price_present=True, rsi_present=True, consensus_present=True
    ) == []
    assert insufficient_inputs(
        price_present=True, rsi_present=True, consensus_present=False
    ) == ["consensus"]
    assert insufficient_inputs(
        price_present=False, rsi_present=False, consensus_present=False
    ) == ["price", "rsi14", "consensus"]


@pytest.mark.unit
def test_floored_action_price_absent_is_unavailable():
    assert floored_action("buy", "high", insufficient=["price", "rsi14", "consensus"]) == (
        "unavailable",
        "low",
    )


@pytest.mark.unit
def test_floored_action_insufficient_floors_to_hold():
    assert floored_action("buy", "high", insufficient=["consensus"]) == ("hold", "low")
    assert floored_action("sell", "medium", insufficient=["rsi14"]) == ("hold", "low")


@pytest.mark.unit
def test_floored_action_complete_inputs_passthrough():
    assert floored_action("buy", "high", insufficient=[]) == ("buy", "high")
    assert floored_action("hold", "low", insufficient=[]) == ("hold", "low")
