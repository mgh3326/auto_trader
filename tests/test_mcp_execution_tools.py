"""MCP execution support tools."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.timezone import now_kst
from tests._mcp_tooling_support import build_tools


class _ScalarRows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None


class _ExecuteResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> _ScalarRows:
        return _ScalarRows(self._rows)


class _DummyDb:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> _ExecuteResult:
        self.statements.append(stmt)
        return _ExecuteResult(self.rows)


class _DummySessionFactory:
    def __init__(self, db: _DummyDb) -> None:
        self.db = db

    def __call__(self) -> _DummySessionFactory:
        return self

    async def __aenter__(self) -> _DummyDb:
        return self.db

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def _analysis(
    *,
    id: int = 1,
    decision: str = "buy",
    confidence: int = 82,
    created_at=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        model_name="test-model",
        decision=decision,
        confidence=confidence,
        appropriate_buy_min=70000.0,
        appropriate_buy_max=72000.0,
        appropriate_sell_min=82000.0,
        appropriate_sell_max=84000.0,
        buy_hope_min=69000.0,
        buy_hope_max=70500.0,
        sell_target_min=83000.0,
        sell_target_max=85000.0,
        reasons=["earnings momentum"],
        detailed_text="Strong demand and improving margins.",
        created_at=created_at or now_kst(),
    )


def _stock_info() -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        symbol="005930",
        name="Samsung Electronics",
        instrument_type="equity_kr",
    )


@pytest.mark.asyncio
async def test_execution_tools_are_registered() -> None:
    tools = build_tools()

    assert "get_trade_journal" in tools
    assert "format_execution_comment" in tools
    assert "get_latest_market_brief" in tools
    assert "get_market_reports" in tools


@pytest.mark.asyncio
async def test_format_execution_comment_fill_markdown() -> None:
    tools = build_tools()

    result = await tools["format_execution_comment"](
        stage="fill",
        symbol="005930",
        side="buy",
        filled_qty=3,
        filled_price=71200,
        currency="KRW ",
        journal_context={
            "thesis": "Memory cycle recovery",
            "strategy": "swing",
            "target_price": 84000,
            "stop_loss": 68000,
            "min_hold_days": 5,
        },
        market_brief="Latest brief: buy, confidence 82.",
    )

    assert result["success"] is True
    assert result["stage"] == "fill"
    markdown = result["markdown"]
    assert "005930" in markdown
    assert "Memory cycle recovery" in markdown
    assert "KRW 71,200.00" in markdown
    assert "Latest brief: buy, confidence 82." in markdown


@pytest.mark.asyncio
async def test_format_execution_comment_follow_up_markdown() -> None:
    tools = build_tools()

    result = await tools["format_execution_comment"](
        stage="follow_up",
        symbol="005930",
        side="sell",
        filled_qty=3,
        filled_price=80000,
        currency="KRW ",
        journal_context={"entry_price": 70000},
        analysis_summary="Momentum is fading near resistance.",
        next_action="hold",
    )

    assert result["success"] is True
    assert result["stage"] == "follow_up"
    markdown = result["markdown"]
    assert "후속 판단" in markdown
    assert "+14.29%" in markdown
    assert "Momentum is fading near resistance." in markdown
    assert "**hold**" in markdown


@pytest.mark.asyncio
async def test_get_latest_market_brief_returns_latest_analysis(monkeypatch) -> None:
    from app.mcp_server.tooling import market_brief_tools

    db = _DummyDb(rows=[(_analysis(), _stock_info())])
    monkeypatch.setattr(
        market_brief_tools,
        "_session_factory",
        lambda: _DummySessionFactory(db),
    )
    tools = build_tools()

    result = await tools["get_latest_market_brief"](symbols=["005930"], limit=5)

    assert result["success"] is True
    assert result["briefs"] == [
        {
            "symbol": "005930",
            "name": "Samsung Electronics",
            "instrument_type": "equity_kr",
            "decision": "buy",
            "confidence": 82,
            "buy_range": {"min": 70000.0, "max": 72000.0},
            "sell_range": {"min": 82000.0, "max": 84000.0},
            "analyzed_at": result["briefs"][0]["analyzed_at"],
        }
    ]
    assert result["summary"]["buy_count"] == 1
    assert db.statements


@pytest.mark.asyncio
async def test_get_market_reports_returns_symbol_history(monkeypatch) -> None:
    from app.mcp_server.tooling import market_brief_tools

    db = _DummyDb(rows=[(_analysis(id=9, decision="hold"), _stock_info())])
    monkeypatch.setattr(
        market_brief_tools,
        "_session_factory",
        lambda: _DummySessionFactory(db),
    )
    tools = build_tools()

    result = await tools["get_market_reports"](symbol="005930", days=3)

    assert result["success"] is True
    assert result["symbol"] == "005930"
    assert result["reports"][0]["id"] == 9
    assert result["reports"][0]["price_analysis"]["sell_target"] == {
        "min": 83000.0,
        "max": 85000.0,
    }
    assert result["trend"]["latest_decision"] == "hold"


@pytest.mark.asyncio
async def test_get_trade_journal_filters_by_paperclip_issue_id(monkeypatch) -> None:
    from app.mcp_server.tooling import trade_journal_tools

    created_at = now_kst() - timedelta(hours=1)
    journal = SimpleNamespace(
        id=11,
        symbol="005930",
        instrument_type=SimpleNamespace(value="equity_kr"),
        side="buy",
        entry_price=Decimal("70000"),
        quantity=Decimal("3"),
        amount=Decimal("210000"),
        thesis="Memory cycle recovery",
        strategy="swing",
        target_price=Decimal("84000"),
        stop_loss=Decimal("68000"),
        min_hold_days=5,
        hold_until=now_kst() + timedelta(days=4),
        indicators_snapshot={"rsi": 55},
        extra_metadata={"source": "paperclip"},
        status="active",
        trade_id=None,
        exit_price=None,
        exit_date=None,
        exit_reason=None,
        pnl_pct=None,
        account="main",
        account_type="live",
        paper_trade_id=None,
        paperclip_issue_id="ROB-74",
        notes="tracking fill",
        created_at=created_at,
        updated_at=created_at,
    )
    db = _DummyDb(rows=[journal])
    monkeypatch.setattr(
        trade_journal_tools,
        "_session_factory",
        lambda: _DummySessionFactory(db),
    )
    tools = build_tools()

    result = await tools["get_trade_journal"](paperclip_issue_id="ROB-74")

    assert result["success"] is True
    assert result["entries"][0]["paperclip_issue_id"] == "ROB-74"
    assert result["entries"][0]["thesis"] == "Memory cycle recovery"
    assert result["summary"]["total_active"] == 1
    assert db.statements
