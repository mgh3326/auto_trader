from __future__ import annotations

import json

import pytest

from scripts.diagnose_invest_screener_toss_parity import (
    build_parity_report,
    load_toss_symbols,
)


@pytest.mark.unit
def test_build_parity_report_counts_rank_deltas_and_metrics() -> None:
    auto_rows = [
        {
            "symbol": "222222",
            "week_change_rate": 8.0,
            "consecutive_up_days": 5,
            "snapshot_date": "2026-05-11",
            "_screener_snapshot_state": "fresh",
        },
        {
            "symbol": "333333",
            "week_change_rate": 3.0,
            "consecutive_up_days": 6,
            "snapshot_date": "2026-05-11",
            "_screener_snapshot_state": "fresh",
        },
        {
            "symbol": "999999",
            "week_change_rate": 1.0,
            "consecutive_up_days": 7,
            "snapshot_date": "2026-05-11",
            "_screener_snapshot_state": "fresh",
        },
    ]
    toss_rows = [
        {"rank": 1, "symbol": "111111"},
        {"rank": 2, "symbol": "222222"},
        {"rank": 3, "symbol": "333333"},
    ]

    report = build_parity_report(auto_rows, toss_rows, limit=80)

    assert report["autoTraderCount"] == 3
    assert report["tossCount"] == 3
    assert report["overlapCount"] == 2
    assert report["missingFromAutoTrader"] == [{"symbol": "111111", "tossRank": 1}]
    assert report["extraInAutoTrader"][0]["symbol"] == "999999"
    assert report["topRankDeltas"][0] == {
        "symbol": "222222",
        "tossRank": 2,
        "autoTraderRank": 1,
        "delta": -1,
        "autoTraderMetrics": {
            "week_change_rate": 8.0,
            "consecutive_up_days": 5,
            "change_rate": None,
            "snapshot_date": "2026-05-11",
            "_screener_snapshot_state": "fresh",
        },
    }
    assert "instrument_type" in report["notes"][0]


@pytest.mark.unit
def test_load_toss_symbols_accepts_csv_and_normalizes_exchange_prefix(tmp_path) -> None:
    path = tmp_path / "toss.csv"
    path.write_text(
        "rank,symbol,name,week_change_rate,consecutive_up_days\n"
        "1,KRX:005930,삼성전자,8.5%,6\n"
        "2,000660,SK하이닉스,3.2,5\n",
        encoding="utf-8",
    )

    rows = load_toss_symbols(path)

    assert rows == [
        {
            "rank": 1,
            "symbol": "005930",
            "name": "삼성전자",
            "week_change_rate": 8.5,
            "consecutive_up_days": 6,
        },
        {
            "rank": 2,
            "symbol": "000660",
            "name": "SK하이닉스",
            "week_change_rate": 3.2,
            "consecutive_up_days": 5,
        },
    ]


@pytest.mark.unit
def test_load_toss_symbols_accepts_json_symbol_list(tmp_path) -> None:
    path = tmp_path / "toss.json"
    path.write_text(json.dumps(["005930", {"symbol": "KRX:000660", "rank": 4}]), encoding="utf-8")

    rows = load_toss_symbols(path)

    assert [(row["rank"], row["symbol"]) for row in rows] == [(1, "005930"), (4, "000660")]


@pytest.mark.unit
def test_load_toss_symbols_accepts_plain_symbol_list(tmp_path) -> None:
    path = tmp_path / "toss_symbols.txt"
    path.write_text("KRX:005930\n000660\n", encoding="utf-8")

    rows = load_toss_symbols(path)

    assert [(row["rank"], row["symbol"]) for row in rows] == [(1, "005930"), (2, "000660")]


@pytest.mark.unit
def test_load_toss_symbols_rejects_sensitive_exports(tmp_path) -> None:
    path = tmp_path / "toss.csv"
    path.write_text("symbol,authorization\n005930,Bearer abc.def\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must not contain"):
        load_toss_symbols(path)
