from __future__ import annotations

import pytest

from app.schemas.n8n.board_brief import BoardBriefContext
from app.services.n8n_daily_brief_service import (
    InvariantViolation,
    RenderInvariantError,
    RenderRouter,
    build_cio_pending_decision,
    build_tc_preliminary,
    validate_render_invariants,
)


def _v2_context(**updates: object) -> BoardBriefContext:
    payload = {
        "exchange_krw": 1_000_000,
        "unverified_cap": {
            "amount": 5_000_000,
            "verified_by_boss_today": False,
        },
        "daily_burn_krw": 100_000,
        "next_obligation": {
            "date": "2026-04-24",
            "days_remaining": 7,
            "cash_needed_until": 2_500_000,
        },
        "tier_scenarios": [
            {
                "label": "T1",
                "deposit_amount": 1_500_000,
                "target_exchange_krw": 2_500_000,
                "buffer_days": 25,
                "cushion_after_obligation": 0,
            },
        ],
        "data_sufficient_by_symbol": {"BTC": True},
        "btc_regime": {
            "close_vs_20d_ma": "above",
            "ma20_slope": "up",
            "drawdown_14d_pct": -3.2,
        },
        "holdings": [
            {"symbol": "BTC", "current_krw_value": 1_000_000, "dust": False},
            {"symbol": "DOGE", "current_krw_value": 3_000, "dust": True},
        ],
        "dust_items": [{"symbol": "DOGE", "current_krw_value": 3_000}],
    }
    payload.update(updates)
    return BoardBriefContext.model_validate(payload)


class RecordingRouter(RenderRouter):
    def __init__(self) -> None:
        self.ops_messages: list[str] = []

    def route_ops_escalation(self, message: str) -> None:
        self.ops_messages.append(message)


def test_missing_required_context_returns_anchor_only_and_routes_ops() -> None:
    router = RecordingRouter()
    ctx = _v2_context(unverified_cap=None)

    render = build_tc_preliminary(ctx, router=router)

    assert render.text == "⚠️ unverified_cap 누락 — manual_cash 관련 권고/문구 생성 금지"
    assert render.embed == {}
    assert router.ops_messages == [render.text]


def test_forbidden_pattern_blocks_partial_render_and_routes_ops() -> None:
    router = RecordingRouter()
    ctx = _v2_context()

    with pytest.raises(RenderInvariantError) as exc_info:
        build_cio_pending_decision(
            ctx,
            router=router,
            text_postprocessor=lambda text: text + "\n가용 현금 1000000",
        )

    assert exc_info.value.violations == [
        InvariantViolation(
            code="forbidden_pattern",
            detail=r"가용\s*현금[^(]*\d",
        )
    ]
    assert router.ops_messages
    assert "forbidden_pattern" in router.ops_messages[0]


def test_validate_render_invariants_reports_structural_violations() -> None:
    ctx = _v2_context()
    text = "\n".join(
        [
            "💵 자금 현황",
            "- 거래소 KRW: 1,000,000 KRW",
            "- runway 산식: 1,000,000 KRW + 5,000,000 KRW / 100,000 KRW = 60.00일",
            "경로 A: 신규 매수 없이 현금 runway 회복 우선.",
            "🧹 Dust 1종목 · 합계 3,000 KRW · 포트폴리오 0.30%",
        ]
    )

    violations = validate_render_invariants(text, ctx, phase="cio_pending")

    codes = {violation.code for violation in violations}
    assert "funding_rows" in codes
    assert "runway_excludes_unverified_cap" in codes
    assert "ab_anchor_triple" in codes
    assert "g2_phrase_exactly_one" in codes
