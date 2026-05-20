from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.diagnose_invest_screener_toss_parity import (
    build_double_buy_parity_report,
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
    path.write_text(
        json.dumps(["005930", {"symbol": "KRX:000660", "rank": 4}]), encoding="utf-8"
    )

    rows = load_toss_symbols(path)

    assert [(row["rank"], row["symbol"]) for row in rows] == [
        (1, "005930"),
        (4, "000660"),
    ]


@pytest.mark.unit
def test_load_toss_symbols_accepts_plain_symbol_list(tmp_path) -> None:
    path = tmp_path / "toss_symbols.txt"
    path.write_text("KRX:005930\n000660\n", encoding="utf-8")

    rows = load_toss_symbols(path)

    assert [(row["rank"], row["symbol"]) for row in rows] == [
        (1, "005930"),
        (2, "000660"),
    ]


@pytest.mark.unit
def test_load_toss_symbols_rejects_sensitive_exports(tmp_path) -> None:
    path = tmp_path / "toss.csv"
    path.write_text("symbol,authorization\n005930,Bearer abc.def\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must not contain"):
        load_toss_symbols(path)


@pytest.mark.unit
def test_build_double_buy_parity_report_emits_ab_blocks_with_overlap_counts() -> None:
    toss_rows = [
        {"rank": 1, "symbol": "011000"},
        {"rank": 2, "symbol": "439960"},
        {"rank": 3, "symbol": "083500"},
    ]
    a_rows = [
        {"symbol": "011000"},
        {"symbol": "439960"},
        {"symbol": "EXTRA01"},
    ]
    b_rows = [
        {"symbol": "011000"},
        {"symbol": "083500"},
        {"symbol": "EXTRA02"},
        {"symbol": "EXTRA03"},
    ]

    report = build_double_buy_parity_report(
        a_rows=a_rows,
        b_rows=b_rows,
        toss_rows=toss_rows,
        limit=50,
        interpretation="both",
        current_date="2026-05-19",
        prev_date="2026-05-18",
    )

    assert report["preset"] == "double_buy"
    assert report["interpretation"] == "both"
    assert report["limit"] == 50
    assert report["currentSnapshotDate"] == "2026-05-19"
    assert report["previousSnapshotDate"] == "2026-05-18"
    assert report["tossCount"] == 3
    assert report["lockedInterpretation"] == "A"
    assert "Decision 1" in report["note"]

    block_a = report["interpretationA"]
    assert block_a["count"] == 3
    assert block_a["overlapCount"] == 2  # 011000, 439960
    assert block_a["missingFromAutoTrader"] == [{"symbol": "083500", "tossRank": 3}]
    assert block_a["extraInAutoTrader"] == [
        {"symbol": "EXTRA01", "autoTraderRank": 3},
    ]

    block_b = report["interpretationB"]
    assert block_b["count"] == 4
    assert block_b["overlapCount"] == 2  # 011000, 083500
    assert block_b["missingFromAutoTrader"] == [{"symbol": "439960", "tossRank": 2}]
    assert [item["symbol"] for item in block_b["extraInAutoTrader"]] == [
        "EXTRA02",
        "EXTRA03",
    ]


@pytest.mark.unit
def test_build_double_buy_parity_report_a_only_leaves_b_block_null() -> None:
    report = build_double_buy_parity_report(
        a_rows=[{"symbol": "011000"}],
        b_rows=None,
        toss_rows=[{"rank": 1, "symbol": "011000"}],
        limit=10,
        interpretation="a",
        current_date=None,
        prev_date=None,
    )
    assert report["interpretation"] == "a"
    assert report["interpretationA"] is not None
    assert report["interpretationA"]["overlapCount"] == 1
    assert report["interpretationB"] is None
    assert report["lockedInterpretation"] == "A"


@pytest.mark.unit
def test_build_double_buy_parity_report_b_only_leaves_a_block_null() -> None:
    report = build_double_buy_parity_report(
        a_rows=None,
        b_rows=[{"symbol": "011000"}],
        toss_rows=[{"rank": 1, "symbol": "011000"}],
        limit=10,
        interpretation="b",
        current_date=None,
        prev_date=None,
    )
    assert report["interpretation"] == "b"
    assert report["interpretationA"] is None
    assert report["interpretationB"] is not None
    assert report["interpretationB"]["overlapCount"] == 1


@pytest.mark.unit
def test_double_buy_supported_and_emits_ab_comparison_blocks(
    monkeypatch, capsys, tmp_path
) -> None:
    """End-to-end main() exercises double_buy preset with both interpretations.

    Loader and session plumbing are patched so the test is independent of DB
    state — only the dispatch + report-building path is exercised here.
    """
    toss_path = tmp_path / "toss_ref.json"
    toss_path.write_text(
        json.dumps(
            {
                "results": [
                    {"symbol": "011000", "rank": 1},
                    {"symbol": "439960", "rank": 2},
                    {"symbol": "083500", "rank": 3},
                ]
            }
        ),
        encoding="utf-8",
    )

    fake_a_rows = [
        {"symbol": "011000"},
        {"symbol": "439960"},
        {"symbol": "EXTRA01"},
    ]
    fake_b_rows = [
        {"symbol": "011000"},
        {"symbol": "083500"},
        {"symbol": "EXTRA02"},
        {"symbol": "EXTRA03"},
    ]

    a_calls: list[str] = []
    b_calls: list[str] = []

    async def _fake_a(session, *, market, limit):
        a_calls.append(market)
        return fake_a_rows

    async def _fake_b(session, *, market, limit):
        b_calls.append(market)
        return fake_b_rows

    # Patch session execute to return canned current/prev snapshot dates.
    from datetime import date

    async def _fake_execute(stmt):
        result = MagicMock()
        # Cycle through current_date then prev_date for the two scalar lookups.
        if not hasattr(_fake_execute, "_calls"):
            _fake_execute._calls = 0
        _fake_execute._calls += 1
        if _fake_execute._calls == 1:
            result.scalar_one_or_none.return_value = date(2026, 5, 19)
        elif _fake_execute._calls == 2:
            result.scalar_one_or_none.return_value = date(2026, 5, 18)
        else:
            result.scalar_one_or_none.return_value = None
        return result

    fake_session = MagicMock()
    fake_session.execute = _fake_execute
    fake_ctx = AsyncMock()
    fake_ctx.__aenter__.return_value = fake_session
    fake_ctx.__aexit__.return_value = False

    monkeypatch.setattr(
        "scripts.diagnose_invest_screener_toss_parity.load_double_buy_rows_interpretation_a",
        _fake_a,
    )
    monkeypatch.setattr(
        "scripts.diagnose_invest_screener_toss_parity.load_double_buy_rows_interpretation_b",
        _fake_b,
    )

    # Patch AsyncSessionLocal at its source so the dynamic import inside main()
    # picks up the fake.
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", lambda: fake_ctx)
    # Avoid sentry/logging side-effects in the test.
    monkeypatch.setattr(
        "app.core.cli.setup_logging_and_sentry", lambda *a, **kw: None
    )

    from scripts import diagnose_invest_screener_toss_parity as mod

    exit_code = asyncio.run(
        mod.main(
            [
                "--market",
                "kr",
                "--preset",
                "double_buy",
                "--toss-symbols-file",
                str(toss_path),
                "--interpretation",
                "both",
                "--limit",
                "50",
            ]
        )
    )
    assert exit_code == 0
    assert a_calls == ["kr"]
    assert b_calls == ["kr"]

    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["preset"] == "double_buy"
    assert report["interpretation"] == "both"
    assert report["currentSnapshotDate"] == "2026-05-19"
    assert report["previousSnapshotDate"] == "2026-05-18"
    assert report["interpretationA"]["count"] == 3
    assert report["interpretationA"]["overlapCount"] == 2  # 011000, 439960
    assert report["interpretationB"]["count"] == 4
    assert report["interpretationB"]["overlapCount"] == 2  # 011000, 083500
    assert report["lockedInterpretation"] == "A"


@pytest.mark.unit
def test_double_buy_interpretation_a_skips_b_loader(
    monkeypatch, capsys, tmp_path
) -> None:
    """--interpretation a must NOT invoke the B loader (and vice versa)."""
    toss_path = tmp_path / "toss_ref.json"
    toss_path.write_text(json.dumps([{"symbol": "011000", "rank": 1}]), encoding="utf-8")

    a_calls: list[str] = []
    b_calls: list[str] = []

    async def _fake_a(session, *, market, limit):
        a_calls.append(market)
        return [{"symbol": "011000"}]

    async def _fake_b(session, *, market, limit):
        b_calls.append(market)
        return [{"symbol": "SHOULD_NOT_BE_CALLED"}]

    from datetime import date

    async def _fake_execute(stmt):
        result = MagicMock()
        result.scalar_one_or_none.return_value = date(2026, 5, 19)
        return result

    fake_session = MagicMock()
    fake_session.execute = _fake_execute
    fake_ctx = AsyncMock()
    fake_ctx.__aenter__.return_value = fake_session
    fake_ctx.__aexit__.return_value = False

    monkeypatch.setattr(
        "scripts.diagnose_invest_screener_toss_parity.load_double_buy_rows_interpretation_a",
        _fake_a,
    )
    monkeypatch.setattr(
        "scripts.diagnose_invest_screener_toss_parity.load_double_buy_rows_interpretation_b",
        _fake_b,
    )
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", lambda: fake_ctx)
    monkeypatch.setattr(
        "app.core.cli.setup_logging_and_sentry", lambda *a, **kw: None
    )

    from scripts import diagnose_invest_screener_toss_parity as mod

    exit_code = asyncio.run(
        mod.main(
            [
                "--market",
                "kr",
                "--preset",
                "double_buy",
                "--toss-symbols-file",
                str(toss_path),
                "--interpretation",
                "a",
                "--limit",
                "10",
            ]
        )
    )
    assert exit_code == 0
    assert a_calls == ["kr"]
    assert b_calls == []

    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["interpretation"] == "a"
    assert report["interpretationA"] is not None
    assert report["interpretationB"] is None
