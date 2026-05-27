# tests/services/investment_reports/test_action_packet.py
"""ROB-335 — ActionPacket read-time projection + schema."""

from __future__ import annotations

import pytest

from app.schemas.investment_reports import (
    ActionPacket,
    InvestmentReportBundle,
)

pytestmark = pytest.mark.unit


def test_action_packet_defaults_are_empty() -> None:
    packet = ActionPacket()
    assert packet.held_actions == []
    assert packet.new_buy_candidates == []
    assert packet.no_new_buy_reason is None
    assert packet.risk_reviews == []
    assert packet.no_action_reason is None
    assert packet.data_gaps_for_next_cycle == []


def test_bundle_action_packet_field_is_optional() -> None:
    # Additive, null for legacy reports (mirrors review_sections).
    assert "action_packet" in InvestmentReportBundle.model_fields
    assert InvestmentReportBundle.model_fields["action_packet"].default is None
