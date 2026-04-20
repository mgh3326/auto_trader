from __future__ import annotations

import pytest

from app.schemas.n8n.board_brief import GateResult, N8nG2GatePayload
from app.services.n8n_daily_brief_service import (
    RenderInvariantError,
    build_cio_pending_decision,
)
from tests.fixtures.cio_briefing import plan_v2_section_f_context


def test_g6_rsi_trigger_cannot_emit_immediate_buy_when_upper_gate_fails() -> None:
    ctx = plan_v2_section_f_context(
        gate_results={
            "G1": GateResult(status="fail", detail="target OHLCV missing"),
            "G2": N8nG2GatePayload(
                passed=False,
                status="fail",
                blocking_reason="runway recovery only",
            ),
            "G3": GateResult(status="fail", detail="obligation cushion negative"),
            "G4": GateResult(status="fail", detail="target below 20D MA"),
            "G5": GateResult(status="fail", detail="24h volatility halt"),
            "G6": GateResult(status="pass", detail="RSI=30 oversold trigger"),
        }
    )

    with pytest.raises(RenderInvariantError) as exc_info:
        build_cio_pending_decision(
            ctx,
            text_postprocessor=lambda text: text + "\nCIO 권고 (1) 즉시 매수",
        )

    assert [violation.code for violation in exc_info.value.violations] == [
        "immediate_buy_requires_g2_g5_pass"
    ]
    assert exc_info.value.violations[0].detail == (
        "immediate buy rendered while G2, G3, G4, G5 not pass"
    )
