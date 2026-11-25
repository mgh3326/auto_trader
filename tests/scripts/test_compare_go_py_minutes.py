"""hk 6345: the Go↔Python compare CLI is SELECT-only, read-only and bounded.

Static checks read the script's AST (no database). Offline checks feed it a JSONL
file and an in-memory Python side, so no connection is opened.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.research_candles import go_py_compare as cmp
from scripts import compare_go_py_minutes as cli

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "compare_go_py_minutes.py"
FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|COPY)\b",
    re.IGNORECASE,
)


def _tree() -> ast.Module:
    return ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))


def _sql_literals(tree: ast.Module) -> list[str]:
    """Every string passed to sqlalchemy ``text(...)``."""
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "text"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.append(node.args[0].value)
    return found


def test_every_sql_statement_is_a_single_select():
    statements = _sql_literals(_tree())
    assert len(statements) == 4  # two market SELECTs + transaction/timeout setup
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 2  # kr and us research reads, nothing else
    for sql in statements:
        assert not FORBIDDEN_SQL.search(sql), sql


def test_statements_are_run_inside_read_only_transaction_with_timeout():
    source = SCRIPT.read_text(encoding="utf-8")
    statements = _sql_literals(_tree())
    assert "SET TRANSACTION READ ONLY" in statements
    assert "SET LOCAL statement_timeout = '8s'" in statements
    # The read runs inside conn.begin(), which is the transaction these SETs apply to.
    assert "async with conn.begin():" in source


def test_only_the_two_research_tables_are_read():
    tables = set()
    for sql in _sql_literals(_tree()):
        tables.update(re.findall(r"FROM\s+(research\.[a-z_0-9]+)", sql, re.IGNORECASE))
    assert tables == {"research.kr_candles_1m", "research.us_candles_1m"}


def test_no_broker_order_ledger_or_write_path_is_imported():
    forbidden_prefixes = (
        "app.services.brokers",
        "app.services.order",
        "app.services.execution_ledger",
        "app.services.toss",
        "app.services.kis",
        "app.mcp_server",
        "app.routers",
        "app.tasks",
        "app.services.research_candles.session_minutes_collector",
        "app.services.research_candles.minute_ingest",
    )
    for node in ast.walk(_tree()):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            assert not name.startswith(forbidden_prefixes), name


def test_script_never_calls_write_or_commit_methods():
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Attribute) and node.attr in {
            "commit",
            "add",
            "add_all",
            "flush",
            "merge",
            "rollback",
        }:
            raise AssertionError(f"write-capable attribute used: .{node.attr}")


# ---- offline behaviour ------------------------------------------------------

KR_DAY = date(2026, 11, 24)
GOOD = (
    '{"time_utc":"2026-11-24T00:00:00Z","session_date":"2026-11-24",'
    '"symbol":"005930","venue":"KRX","session_segment":"KRX_REGULAR",'
    '"source":"KIS","open":"100","high":"101","low":"99","close":"100",'
    '"volume":"10","value":"100"}'
)


def _write(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "go.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_loader_keeps_only_the_requested_day_and_market_venues(tmp_path):
    other_day = GOOD.replace("2026-11-24", "2026-11-25").replace(
        "2026-11-24T00:00:00Z", "2026-11-25T00:00:00Z"
    )
    us_row = GOOD.replace('"KRX"', '"NASD"')
    path = _write(tmp_path, [GOOD, other_day, us_row, ""])

    bars, stats = cli._load_go(path, "kr", KR_DAY, None)

    assert len(bars) == 1
    assert bars[0].symbol == "005930"
    assert stats == {
        "go_lines": 3,
        "go_rows_compared": 1,
        "go_rows_other_scope": 2,
        "go_rows_other_symbol": 0,
    }


def test_loader_rejects_a_malformed_line_without_echoing_it(tmp_path):
    path = _write(tmp_path, [GOOD, '{"time_utc": 1.0}'])
    with pytest.raises(ValueError) as excinfo:
        cli._load_go(path, "kr", KR_DAY, None)
    assert "line 2" in str(excinfo.value)
    assert "1.0" not in str(excinfo.value)


def test_loader_rejects_a_float_price(tmp_path):
    raw = json.loads(GOOD)
    raw["close"] = 100.0
    path = _write(tmp_path, [json.dumps(raw)])
    with pytest.raises(ValueError):
        cli._load_go(path, "kr", KR_DAY, None)


def _py_bar(minute: int) -> cmp.MinuteBar:
    return cmp.MinuteBar(
        time_utc=datetime(2026, 11, 24, 0, minute, tzinfo=UTC),
        session_date=KR_DAY,
        symbol="005930",
        venue="KRX",
        session_segment="KRX_REGULAR",
        source="KIS",
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("10"),
        value=Decimal("100"),
    )


def test_report_shape_and_exit_relevant_verdict():
    py = [_py_bar(m) for m in range(5)]
    go = [_py_bar(m) for m in range(5)]
    report = cli.build_report(
        "kr",
        KR_DAY,
        go,
        py,
        {"go_lines": 5, "go_rows_compared": 5, "go_rows_other_scope": 0},
    )

    assert report["status"] == "ok"
    assert report["verdict"] == cmp.PASS
    assert report["blocking"]["missing_sessions_on_go"] == []
    assert {row["check"] for row in report["cutover"]} >= {
        "같은 종목 커버 (양방향)",
        "같은 세션 커버 (양방향)",
        "수신 지연 차이",
    }
    assert report["failure_code_map"]["budget_cut"]["hk6299"] == "cut_by_budget"


def test_report_blocks_on_a_whole_missing_session():
    py = [_py_bar(m) for m in range(5)]
    report = cli.build_report(
        "kr",
        KR_DAY,
        [],
        py,
        {"go_lines": 0, "go_rows_compared": 0, "go_rows_other_scope": 0},
    )
    assert report["verdict"] == cmp.BLOCK
    assert report["blocking"]["missing_symbols_on_go"] == ["005930"]


def test_main_exits_usage_on_a_bad_go_file_before_any_database_work(tmp_path):
    path = _write(tmp_path, ["not json"])
    code = cli.main(["--market", "kr", "--date", "2026-11-24", "--go-jsonl", str(path)])
    assert code == cli.EXIT_USAGE


# ---- review r1: Python side = session collector rows only ------------------


def test_python_prefix_is_pinned_to_the_collector_batch_prefix():
    from app.services.research_candles.session_minutes_collector import (
        BATCH_PREFIX,
    )

    assert cli.PY_COLLECTOR_BATCH_PREFIX == BATCH_PREFIX


def test_split_keeps_only_collector_rows_and_counts_the_rest():
    rows = [
        {"batch_id": "session-minutes-kr-20261124T000000Z-abcd1234"},
        {"batch_id": "minutes-ingest-kr-20261124T000000Z-ffff0000"},
        {"batch_id": "promote-20261124"},
        {"batch_id": "session-minutes-us-x"},
        {"batch_id": None},
    ]
    collector, excluded = cli.split_collector_rows(rows)

    assert [r["batch_id"] for r in collector] == [
        "session-minutes-kr-20261124T000000Z-abcd1234",
        "session-minutes-us-x",
    ]
    assert excluded == 3


def test_both_reads_select_the_batch_id_needed_for_the_filter():
    for sql in _sql_literals(_tree()):
        if sql.lstrip().upper().startswith("SELECT"):
            assert "batch_id" in sql


# ---- review r1: symbol count after filtering and exit codes ----------------


def test_loader_counts_symbol_filtered_rows_separately(tmp_path):
    other_symbol = GOOD.replace("005930", "000660")
    path = _write(tmp_path, [GOOD, other_symbol])

    bars, stats = cli._load_go(path, "kr", KR_DAY, ["005930"])

    assert len(bars) == 1
    assert stats["go_rows_compared"] == 1
    assert stats["go_rows_other_symbol"] == 1
    assert stats["go_rows_other_scope"] == 0


def test_verdict_exit_codes_are_distinct():
    assert cli._VERDICT_EXIT == {cmp.PASS: 0, cmp.BLOCK: 3, cmp.INCONCLUSIVE: 4}


def test_requested_symbols_that_normalise_to_nothing_are_usage():
    assert cli._requested_symbols("kr", [","]) == []
    assert cli._requested_symbols("kr", ["5930", " 000660 "]) == ["000660", "005930"]


def test_main_rejects_empty_symbol_list_before_reading_anything(tmp_path):
    path = _write(tmp_path, [GOOD])
    code = cli.main(
        [
            "--market",
            "kr",
            "--date",
            "2026-11-24",
            "--go-jsonl",
            str(path),
            "--symbols",
            ",",
        ]
    )
    assert code == cli.EXIT_USAGE


def test_report_reports_inconclusive_with_reasons():
    report = cli.build_report(
        "kr",
        KR_DAY,
        [],
        [],
        {"go_lines": 0, "go_rows_compared": 0, "go_rows_other_scope": 0},
    )
    assert report["verdict"] == cmp.INCONCLUSIVE
    assert "go_rows_empty" in report["inconclusive_reasons"]
