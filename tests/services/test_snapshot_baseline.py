# tests/services/test_snapshot_baseline.py
from app.services.action_report.snapshot_backed import generator as gen


def test_market_baseline_whitelists_indices():
    payload = {
        "market": "us",
        "from_date": "2026-05-30",
        "to_date": "2026-05-30",
        "event_count": 3,
        "events": [{"big": "blob"}],  # excluded
        "indices": {"SPX": {"price": 5300.0, "change_pct": 0.4}},
    }
    base = gen._market_numeric_baseline(payload)
    assert base["indices"] == {"SPX": {"price": 5300.0, "change_pct": 0.4}}
    assert base["market"] == "us"
    assert "events" not in base  # heavy list not copied


def test_portfolio_baseline_whitelists_cash_and_summary():
    payload = {
        "holdings": [{"ticker": "BAC"}],  # excluded heavy list
        "primary_source": "kis_live",
        "cash": {"usd_cash": 3095.26, "usd_orderable": 3078.32},
        "buying_power": {"usd": 3078.32},
        "sellable_summary": {"count": 4},
    }
    base = gen._portfolio_numeric_baseline(payload)
    assert base["cash"] == {"usd_cash": 3095.26, "usd_orderable": 3078.32}
    assert base["buying_power"] == {"usd": 3078.32}
    assert base["sellable_summary"] == {"count": 4}
    assert base["primary_source"] == "kis_live"
    assert base["holdings_count"] == 1
    assert "holdings" not in base


def test_baselines_handle_missing_keys():
    assert gen._market_numeric_baseline({}) == {}
    assert gen._portfolio_numeric_baseline({}) == {"holdings_count": 0}
