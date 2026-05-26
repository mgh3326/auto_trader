import pytest

from app.schemas.us_dual_paper import (
    AccountStateSummary,
    BrokerPreviewResult,
    DualBrokerPreviewPacket,
    DualPaperBrokerStatus,
)


@pytest.mark.unit
def test_packet_defaults_are_safe():
    packet = DualBrokerPreviewPacket(
        symbol="NVDA",
        limit_price_source="operator_input",
        notional_cap_usd=50.0,
        brokers={},
    )
    assert packet.market == "us"
    assert packet.side == "buy"
    assert packet.order_type == "limit"
    assert packet.submit_enabled is False


@pytest.mark.unit
def test_broker_result_independent_status():
    ok = BrokerPreviewResult(account_scope="alpaca_paper", status=DualPaperBrokerStatus.PREVIEWED)
    bad = BrokerPreviewResult(
        account_scope="kis_mock",
        status=DualPaperBrokerStatus.ERROR,
        reason="boom",
    )
    packet = DualBrokerPreviewPacket(
        symbol="NVDA",
        limit_price_source="operator_input",
        notional_cap_usd=50.0,
        brokers={"alpaca_paper": ok, "kis_mock": bad},
    )
    assert packet.brokers["alpaca_paper"].status is DualPaperBrokerStatus.PREVIEWED
    assert packet.brokers["kis_mock"].status is DualPaperBrokerStatus.ERROR


@pytest.mark.unit
def test_account_state_summary_is_numbers_only():
    summary = AccountStateSummary(
        cash_usd=100.0, buying_power_usd=100.0, position_count=2, open_order_count=0
    )
    dumped = summary.model_dump()
    assert set(dumped) == {"cash_usd", "buying_power_usd", "position_count", "open_order_count"}
