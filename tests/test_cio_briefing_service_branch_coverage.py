from __future__ import annotations

import httpx
import pytest

from app.schemas.n8n.board_brief import BoardBriefContext
from app.services.n8n_daily_brief_service import (
    RenderInvariantError,
    RenderRouter,
    _build_candidate_lines,
    _build_concentration_lines,
    _build_tier_lines,
    _cash_runway_days,
    _collect_symbols_by_market,
    _format_unverified_amounts,
    _gate_passed,
    build_cio_pending_decision,
    build_tc_preliminary,
    resolve_funding_intent,
)
from tests.fixtures.cio_briefing import drop_required_field, plan_v2_section_f_context


def test_renderer_helper_fallback_branches() -> None:
    assert (
        _format_unverified_amounts(plan_v2_section_f_context(unverified_cap=None))
        == set()
    )
    assert _gate_passed(None) is False
    assert (
        _cash_runway_days(plan_v2_section_f_context(manual_cash_runway_days=7.5))
        == pytest.approx(7.5)
    )
    assert (
        _cash_runway_days(BoardBriefContext(manual_cash_krw=1_000, daily_burn_krw=100))
        == 10
    )
    assert _cash_runway_days(BoardBriefContext(manual_cash_krw=1_000)) is None
    assert _build_concentration_lines(plan_v2_section_f_context(weights_top_n=[])) == [
        "- 상위 비중 데이터 없음"
    ]
    assert _build_candidate_lines(
        plan_v2_section_f_context(
            holdings=[{"symbol": "APT", "current_krw_value": 654, "dust": True}]
        )
    ) == ["- execution-actionable 매도/축소 후보 없음"]
    assert _build_tier_lines(plan_v2_section_f_context(tier_scenarios=[])) == [
        "tier_scenarios 미수신, 입금 시나리오 산출 보류"
    ]
    assert (
        resolve_funding_intent(plan_v2_section_f_context(next_obligation=None), None)[0]
        == "runway_recovery"
    )


def test_collect_symbols_normalizes_crypto_and_skips_incomplete_rows() -> None:
    symbols_by_market = _collect_symbols_by_market(
        {
            "orders": [
                {"market": "crypto", "symbol": "KRW-ETH"},
                {"market": "", "symbol": "IGNORED"},
            ]
        },
        {
            "positions": [
                {"market_type": "CRYPTO", "symbol": "btc"},
                {"market_type": "KR", "symbol": ""},
            ]
        },
    )

    assert symbols_by_market == {"crypto": {"KRW-ETH", "KRW-BTC"}}


def test_tc_preliminary_missing_context_fails_closed() -> None:
    render = build_tc_preliminary(drop_required_field("exchange_krw"))

    assert render.text.startswith("⚠️ exchange_krw 누락")
    assert render.embed == {}


def test_forbidden_pattern_routes_ops_and_raises() -> None:
    with pytest.raises(RenderInvariantError) as exc_info:
        build_cio_pending_decision(
            plan_v2_section_f_context(),
            text_postprocessor=lambda text: text + "\nPlanning cash 100",
        )

    assert [violation.code for violation in exc_info.value.violations] == [
        "forbidden_pattern"
    ]


def test_default_router_posts_ops_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple[str, dict[str, str]]] = []

    class FakeClient:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, url: str, json: dict[str, str]) -> None:
            posted.append((url, json))

    monkeypatch.setenv("N8N_OPS_ESCALATION_WEBHOOK", "https://ops.example/hook")
    monkeypatch.setattr(httpx, "Client", FakeClient)

    RenderRouter().route_ops_escalation("blocked")

    assert posted == [("https://ops.example/hook", {"content": "blocked"})]
