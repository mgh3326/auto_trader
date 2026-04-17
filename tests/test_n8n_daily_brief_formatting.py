from __future__ import annotations

import pytest

from app.services.n8n_formatting import (
    fmt_date_with_weekday,
    fmt_pnl,
    fmt_value,
)


@pytest.mark.unit
class TestDailyBriefFormatting:
    def test_fmt_date_with_weekday(self):
        from datetime import datetime

        from app.core.timezone import KST

        dt = datetime(2026, 3, 17, 8, 30, tzinfo=KST)
        assert fmt_date_with_weekday(dt) == "03/17 (화)"

    def test_fmt_date_with_weekday_sunday(self):
        from datetime import datetime

        from app.core.timezone import KST

        dt = datetime(2026, 3, 15, 8, 30, tzinfo=KST)
        assert fmt_date_with_weekday(dt) == "03/15 (일)"

    def test_fmt_value_krw_man(self):
        assert fmt_value(15_000_000, "KRW") == "1,500만"

    def test_fmt_value_krw_eok(self):
        assert fmt_value(150_000_000, "KRW") == "1.5억"

    def test_fmt_value_usd(self):
        assert fmt_value(42_000, "USD") == "$42,000"

    def test_fmt_value_none(self):
        assert fmt_value(None, "KRW") == "-"

    def test_fmt_pnl_negative(self):
        assert fmt_pnl(-5.2) == "-5.2%"

    def test_fmt_pnl_positive(self):
        assert fmt_pnl(3.1) == "+3.1%"

    def test_fmt_pnl_none(self):
        assert fmt_pnl(None) == "-"


@pytest.mark.unit
class TestBuildBriefText:
    def test_contains_header(self):
        from app.schemas.n8n.common import N8nMarketOverview
        from app.services.n8n_daily_brief_service import _build_brief_text

        text = _build_brief_text(
            date_fmt="03/17 (화)",
            market_overview=N8nMarketOverview(
                fear_greed=None,
                btc_dominance=None,
                total_market_cap_change_24h=None,
                economic_events_today=[],
            ),
            pending_by_market={},
            portfolio_by_market={},
            yesterday_fills={"total": 0, "fills": []},
        )

        assert "📋 Daily Trading Brief — 03/17 (화)" in text
        assert "💼 미체결 주문" in text
        assert "📊 포트폴리오" in text

    def test_includes_pending_counts(self):
        from app.schemas.n8n.common import N8nMarketOverview
        from app.services.n8n_daily_brief_service import _build_brief_text

        text = _build_brief_text(
            date_fmt="03/17 (화)",
            market_overview=N8nMarketOverview(
                fear_greed=None,
                btc_dominance=None,
                total_market_cap_change_24h=None,
                economic_events_today=[],
            ),
            pending_by_market={
                "crypto": {
                    "total": 11,
                    "buy_count": 4,
                    "sell_count": 7,
                    "orders": [],
                },
            },
            portfolio_by_market={},
            yesterday_fills={"total": 0, "fills": []},
        )

        assert "[크립토] 11건 (매수 4 / 매도 7)" in text

    def test_includes_fills(self):
        from app.schemas.n8n.common import N8nMarketOverview
        from app.services.n8n_daily_brief_service import _build_brief_text

        text = _build_brief_text(
            date_fmt="03/17 (화)",
            market_overview=N8nMarketOverview(
                fear_greed=None,
                btc_dominance=None,
                total_market_cap_change_24h=None,
                economic_events_today=[],
            ),
            pending_by_market={},
            portfolio_by_market={},
            yesterday_fills={
                "total": 1,
                "fills": [
                    {
                        "symbol": "ETH",
                        "side": "sell",
                        "price_fmt": "3.35M",
                        "amount_fmt": "402만",
                        "time": "14:23",
                    }
                ],
            },
        )

        assert "✅ 전일 체결" in text
        assert "ETH sell" in text


@pytest.mark.unit
class TestBoardBriefBuilders:
    def _context(self):
        from app.schemas.n8n.board_brief import BoardBriefContext

        return BoardBriefContext(
            manual_cash_krw=1_250_000,
            daily_burn_krw=50_000,
            weights_top_n=[{"symbol": "BTC", "weight_pct": 42.5}],
            holdings=[
                {"symbol": "BTC", "current_krw_value": 1_000_000, "dust": False},
                {"symbol": "DOGE", "current_krw_value": 3_000, "dust": True},
            ],
            dust_items=[{"symbol": "DOGE", "current_krw_value": 3_000, "dust": True}],
        )

    def test_tc_preliminary_has_no_recommendation_or_gate_sections(self):
        from app.services.n8n_daily_brief_service import build_tc_preliminary

        render = build_tc_preliminary(self._context())
        text = render.text

        assert render.phase == "tc_preliminary"
        assert "경로 A·B 병행 가능" in text
        assert "BTC" in text
        assert "🧹 Dust 1종목" in text
        assert "🎯 권고" not in text
        assert "📊 Gate 판정 결과" not in text
        assert "[funding]" not in text
        assert "[action]" not in text

    def test_cio_pending_includes_recommendation_gates_and_questions(self):
        from app.schemas.n8n.board_brief import GateResult, N8nG2GatePayload
        from app.services.n8n_daily_brief_service import build_cio_pending_decision

        ctx = self._context().model_copy(
            update={
                "gate_results": {
                    "G1": GateResult(
                        status="fail",
                        detail="(3) 현금 우선 정책 적용",
                    ),
                    "G2": N8nG2GatePayload(
                        passed=False,
                        status="fail",
                        blocking_reason="runway recovery requires cash",
                    ),
                }
            }
        )

        render = build_cio_pending_decision(ctx)
        text = render.text

        assert render.phase == "cio_pending"
        assert "🎯 권고" in text
        assert "📊 Gate 판정 결과" in text
        assert "🚫 신규 매수 차단 — G2 fail" in text
        assert "(3) 현금 우선 정책 적용" in text
        assert "[funding-confirmation]" in text
        assert "[action]" in text
        assert "경로 A·B 병행 가능" in text
        assert "**A 와 B 는 상호배타 아님 — 병행 가능.**" in text
