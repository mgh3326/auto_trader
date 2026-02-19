from app.services.research_backtest_parser import parse_backtest_summary


def test_parse_backtest_summary_reads_required_fields() -> None:
    payload = {
        "run_id": "run-1",
        "strategy_name": "NFI",
        "timeframe": "5m",
        "runner": "mac",
        "total_trades": 25,
        "profit_factor": 1.4,
        "max_drawdown": 0.08,
    }

    parsed = parse_backtest_summary(payload)

    assert parsed.run_id == "run-1"
    assert parsed.total_trades == 25
    assert float(parsed.profit_factor) == 1.4
