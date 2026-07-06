from app.mcp_server.tooling.analysis_tool_handlers import _summarize_analysis_result


def test_summarize_analysis_result_passes_us_quote_session_freshness_fields():
    result = _summarize_analysis_result(
        "NVDA",
        {
            "market_type": "equity_us",
            "source": "yahoo",
            "quote": {
                "symbol": "NVDA",
                "instrument_type": "equity_us",
                "price": 195.29,
                "source": "kis_overseas",
                "session": "premarket",
                "data_state": "fresh",
                "price_source": "kis_overseas_last",
                "venue": "NASD",
                "quote_asof": "2026-07-06T08:45:12-04:00",
                "delayed": True,
            },
            "indicators": {"rsi": {"14": 61.2}},
            "support_resistance": {"supports": [], "resistances": []},
            "opinions": {"consensus": {"rating": "buy"}},
            "recommendation": {"action": "hold"},
        },
    )

    assert result["current_price"] == 195.29
    assert result["session"] == "premarket"
    assert result["data_state"] == "fresh"
    assert result["price_source"] == "kis_overseas_last"
    assert result["venue"] == "NASD"
    assert result["quote_asof"] == "2026-07-06T08:45:12-04:00"
    assert result["delayed"] is True
