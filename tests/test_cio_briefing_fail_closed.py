from __future__ import annotations

import pytest

from app.services.n8n_daily_brief_service import build_cio_pending_decision
from tests.fixtures.cio_briefing import RecordingRouter, drop_required_field


@pytest.mark.parametrize(
    "field",
    [
        "exchange_krw",
        "unverified_cap",
        "next_obligation",
        "tier_scenarios",
        "data_sufficient_by_symbol",
        "btc_regime",
        "holdings",
    ],
)
def test_missing_required_context_fails_closed_and_routes_ops(field: str) -> None:
    router = RecordingRouter()

    render = build_cio_pending_decision(drop_required_field(field), router=router)

    assert render.text.startswith(f"⚠️ {field} 누락")
    assert render.embed == {}
    assert router.board_messages == []
    assert router.ops_messages == [render.text]
