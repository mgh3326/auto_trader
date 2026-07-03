from __future__ import annotations

import pytest

from app.mcp_server.tooling.analysis_tool_handlers import _summarize_analysis_result


@pytest.mark.unit
def test_summary_copies_nxt_fields_from_quote():
    analysis = {
        "market_type": "equity_kr",
        "source": "kis",
        "quote": {
            "price": 70000,
            "nxt_tradable": True,
            "nxt_tradable_source": "kr_symbol_universe",
            "nxt_tradable_asof": "2026-07-03T06:00:00+09:00",
            "nxt_tradable_stale": False,
        },
    }
    summary = _summarize_analysis_result("005930", analysis)
    assert summary["nxt_tradable"] is True
    assert summary["nxt_tradable_source"] == "kr_symbol_universe"
    assert summary["nxt_tradable_asof"] == "2026-07-03T06:00:00+09:00"


@pytest.mark.unit
def test_summary_us_has_no_nxt_fields():
    analysis = {
        "market_type": "equity_us",
        "source": "yahoo",
        "quote": {"price": 100.0},
    }
    summary = _summarize_analysis_result("AAPL", analysis)
    assert "nxt_tradable" not in summary
