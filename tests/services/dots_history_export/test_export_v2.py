"""ROB-1450: export v2 — fill_seq, the seed flag, and the daily balance table."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.dots_history_export.leak_check import LeakCheckFailed, scan_table
from app.services.dots_history_export.projection import Projection, RefHasher
from app.services.dots_history_export.publish import publish
from app.services.dots_history_export.spec import (
    BALANCE_UNAVAILABLE,
    SCHEMA_VERSION,
    TABLE_BY_NAME,
)
from tests.services.dots_history_export._fixtures import (
    ACCOUNT_NUMBER,
    CREATED,
    SALT,
    SNAPSHOT_AT,
    WINDOW,
    fill_row,
    paper_reservation_row,
    paper_snapshot_row,
    projected,
)

pytestmark = pytest.mark.unit

US_ORDER_NO = "0030112233"
ORDER_TIME = datetime(2026, 6, 10, 14, 0, tzinfo=UTC)  # 10:00 ET
SEED_ORDER_ID = "SEED-20260601-kis-krx-005930"
FILLS = TABLE_BY_NAME["execution_ledger"]
BALANCES = TABLE_BY_NAME["account_balance_daily"]
NEW_FILL_COLUMNS = (
    "fill_seq",
    "fill_seq_null_reason",
    "fill_qty_basis",
    "incremental_qty",
    "is_seed",
)


def _us_fill(row_id: int, qty: str, **overrides) -> dict:
    """A KIS US fill. The reconciler stamps the order time, not the fill time."""
    row = {
        "id": row_id,
        "venue": "NASD",
        "instrument_type": "equity_us",
        "symbol": "AAPL",
        "broker_order_id": US_ORDER_NO,
        "filled_qty": Decimal(qty),
        "filled_price": Decimal("200.5"),
        "filled_at": ORDER_TIME,
        "currency": "USD",
        "correlation_id": None,
    }
    return fill_row(**{**row, **overrides})


def _by_id(*rows: dict) -> dict[int, dict]:
    projection = projected(execution_ledger=list(rows))
    hasher = RefHasher(SALT)
    by_ref = {row["row_ref"]: row for row in projection.tables["execution_ledger"]}
    return {
        row["id"]: by_ref[hasher.ref("row", "execution_ledger", str(row["id"]))]
        for row in rows
    }


def test_schema_version_is_v2() -> None:
    assert SCHEMA_VERSION == "dots-history-export/v2"


def test_a_partial_fill_reported_twice_cumulatively_and_once_incrementally() -> None:
    # The reconciler saw 3 then 5 filled; the websocket saw one 2-share execution.
    first = _us_fill(30, "3", created_at=ORDER_TIME + timedelta(minutes=10))
    second = _us_fill(31, "5", created_at=ORDER_TIME + timedelta(minutes=40))
    notice = _us_fill(
        32,
        "2",
        venue="krx",
        source="websocket",
        filled_at=ORDER_TIME + timedelta(minutes=20),
        created_at=ORDER_TIME + timedelta(minutes=20),
    )
    rows = _by_id(notice, second, first)

    assert [rows[i]["fill_seq"] for i in (30, 31, 32)] == ["1", "2", "3"]
    assert len({rows[i]["order_ref"] for i in (30, 31, 32)}) == 1
    assert [rows[i]["fill_qty_basis"] for i in (30, 31, 32)] == [
        "cumulative",
        "cumulative",
        "incremental",
    ]
    assert [rows[i]["incremental_qty"] for i in (30, 31, 32)] == ["3", "2", "2"]
    assert [rows[i]["filled_qty"] for i in (30, 31, 32)] == ["3", "5", "2"]
    # The websocket execution is the same fill the reconciler booked.
    assert rows[32]["superseded_duplicate"] == "true"
    assert {rows[i]["fill_seq_null_reason"] for i in (30, 31, 32)} == {None}


def test_a_pure_cumulative_re_report_adds_nothing() -> None:
    rows = _by_id(
        _us_fill(30, "5", created_at=ORDER_TIME + timedelta(minutes=1)),
        _us_fill(31, "5", created_at=ORDER_TIME + timedelta(minutes=2)),
    )
    assert [rows[30]["incremental_qty"], rows[31]["incremental_qty"]] == ["5", "0"]
    assert [rows[30]["fill_seq"], rows[31]["fill_seq"]] == ["1", "2"]


def test_a_cumulative_total_that_goes_down_has_no_increment() -> None:
    rows = _by_id(
        _us_fill(30, "5", created_at=ORDER_TIME + timedelta(minutes=1)),
        _us_fill(31, "4", created_at=ORDER_TIME + timedelta(minutes=2)),
    )
    assert rows[31]["fill_qty_basis"] == "cumulative"
    assert rows[31]["incremental_qty"] is None


def test_each_order_numbers_its_own_rows() -> None:
    other = _us_fill(40, "1", broker_order_id="0030999888")
    rows = _by_id(_us_fill(30, "3"), other)
    assert rows[30]["fill_seq"] == rows[40]["fill_seq"] == "1"
    assert rows[30]["order_ref"] != rows[40]["order_ref"]


@pytest.mark.parametrize(
    ("overrides", "basis", "increment"),
    [
        # Upbit: ledger fill_seq 0 is the order's executed_volume aggregate.
        (
            {"broker": "upbit", "instrument_type": "crypto", "fill_seq": 0},
            "cumulative",
            "0.5",
        ),
        (
            {"broker": "upbit", "instrument_type": "crypto", "fill_seq": 91827},
            "incremental",
            "0.5",
        ),
        # Toss books the delta since its previous reconcile.
        ({"broker": "toss"}, "incremental", "0.5"),
        ({"source": "websocket"}, "incremental", "0.5"),
        ({"broker": "kis"}, "cumulative", "0.5"),
        ({"source": "manual_import"}, "unknown", None),
    ],
)
def test_the_basis_follows_the_writer(overrides, basis, increment) -> None:
    row = fill_row(id=50, filled_qty=Decimal("0.5"), **overrides)
    [cell] = _by_id(row).values()
    assert cell["fill_qty_basis"] == basis
    assert cell["incremental_qty"] == increment
    assert cell["fill_seq"] == "1"
    assert cell["is_seed"] == "false"


def test_a_seeded_holding_is_flagged_and_not_numbered() -> None:
    seed = fill_row(
        id=60,
        source="manual_import",
        broker_order_id=SEED_ORDER_ID,
        correlation_id=None,
        filled_qty=Decimal("12"),
    )
    real = fill_row(id=61)
    rows = _by_id(seed, real)
    assert rows[60]["is_seed"] == "true"
    assert rows[60]["fill_source"] == "manual_import"
    assert rows[60]["fill_qty_basis"] == "position_seed"
    assert rows[60]["fill_seq"] is None
    assert rows[60]["fill_seq_null_reason"] == "position_seed"
    assert rows[60]["incremental_qty"] is None
    assert rows[61]["is_seed"] == "false"
    assert rows[61]["fill_seq"] == "1"


def test_a_row_without_an_order_id_has_a_reason_instead_of_a_seq() -> None:
    [cell] = _by_id(fill_row(id=70, broker_order_id="  ")).values()
    assert cell["fill_seq"] is None
    assert cell["fill_seq_null_reason"] == "no_order_id"


def _balances(**tables) -> list[dict]:
    return projected(**tables).tables["account_balance_daily"]


def test_balance_rows_carry_cash_settled_and_reserved_for_the_paper_account() -> None:
    rows = [row for row in _balances() if row["available"] == "true"]
    by_currency = {row["currency"]: row for row in rows}
    assert set(by_currency) == {"KRW", "USD"}
    krw, usd = by_currency["KRW"], by_currency["USD"]
    assert krw["account_alias"] == usd["account_alias"] == "db_paper"
    assert krw["account_ref"] == usd["account_ref"]
    assert krw["account_ref"] == RefHasher(SALT).ref("acc", "db_paper", "11")
    assert krw["balance_date"] == "2026-06-10"
    assert krw["source_table"] == "paper.paper_daily_snapshots"
    assert krw["cash"] == "8000000.0000"
    assert krw["cash_basis"] == "free_after_open_order_reservations"
    # The paper simulator settles nothing, so settled cash is not known.
    assert krw["settled_cash"] is None
    assert krw["reserved_open_orders"] == "2001000.0000"
    assert usd["cash"] == "1500.2500"
    assert usd["reserved_open_orders"] is None
    assert krw["snapshot_created_at_utc"] == "2026-06-10T14:50:00Z"
    assert krw["snapshot_created_at_local"] == "2026-06-10T23:50:00+09:00"
    assert krw["unavailable_reason"] is None


def test_every_account_without_a_source_is_marked_unavailable() -> None:
    markers = [row for row in _balances() if row["available"] == "false"]
    assert sorted(row["account_alias"] for row in markers) == sorted(
        BALANCE_UNAVAILABLE
    )
    assert "db_paper" not in BALANCE_UNAVAILABLE
    for row in markers:
        assert row["unavailable_reason"] == "no_balance_history_table"
        for name in (
            "cash",
            "settled_cash",
            "reserved_open_orders",
            "currency",
            "balance_date",
            "account_ref",
            "source_table",
        ):
            assert row[name] is None


HELD = "2001000.0000"


@pytest.mark.parametrize(
    ("overrides", "reserved"),
    [
        ({"filled_at": SNAPSHOT_AT - timedelta(hours=1), "status": "filled"}, "0"),
        ({"cancelled_at": SNAPSHOT_AT - timedelta(hours=1)}, "0"),
        ({"filled_at": SNAPSHOT_AT + timedelta(hours=1), "status": "filled"}, HELD),
        ({"placed_at": SNAPSHOT_AT + timedelta(minutes=1)}, "0"),
        ({"account_id": 99}, "0"),
        # Resolved with no time to place it: the total is not known.
        ({"status": "filled"}, None),
    ],
)
def test_reservations_count_only_what_was_open_at_the_snapshot(
    overrides, reserved
) -> None:
    rows = _balances(paper_reservations=[paper_reservation_row(**overrides)])
    [krw] = [row for row in rows if row["currency"] == "KRW"]
    assert krw["reserved_open_orders"] == reserved


def test_a_day_with_no_snapshot_has_no_paper_row() -> None:
    rows = _balances(paper_daily_snapshots=[])
    assert {row["available"] for row in rows} == {"false"}


def test_two_snapshot_days_give_two_dated_rows_per_currency() -> None:
    later = paper_snapshot_row(
        id=14,
        snapshot_date=date(2026, 6, 11),
        created_at=SNAPSHOT_AT + timedelta(days=1),
    )
    rows = _balances(paper_daily_snapshots=[paper_snapshot_row(), later])
    dated = [row for row in rows if row["available"] == "true"]
    assert sorted((row["balance_date"], row["currency"]) for row in dated) == [
        ("2026-06-10", "KRW"),
        ("2026-06-10", "USD"),
        ("2026-06-11", "KRW"),
        ("2026-06-11", "USD"),
    ]
    assert len({row["row_ref"] for row in rows}) == len(rows)


def _publish(projection: Projection, out: Path) -> dict:
    return publish(
        projection,
        out_dir=out,
        window=WINDOW,
        salt=SALT,
        salt_fingerprint=RefHasher(SALT).fingerprint(),
        code_commit="e" * 40,
        code_dirty=False,
        extra_forbidden={ACCOUNT_NUMBER, ACCOUNT_DIGITS},
    )


ACCOUNT_DIGITS = ACCOUNT_NUMBER.replace("-", "")  # "8765432101"
SHORT_ACCOUNT = "87654321"
NEW_COLUMNS = [("execution_ledger", name) for name in NEW_FILL_COLUMNS] + [
    ("account_balance_daily", name) for name in BALANCES.column_names
]


@pytest.mark.parametrize(("table", "column"), NEW_COLUMNS)
def test_the_leak_check_refuses_a_planted_account_id_in_each_new_column(
    table: str, column: str, tmp_path: Path
) -> None:
    for planted in (ACCOUNT_NUMBER, ACCOUNT_DIGITS, SHORT_ACCOUNT):
        projection = projected()
        projection.tables[table][0][column] = planted
        out = tmp_path / f"export-{planted}"
        with pytest.raises(LeakCheckFailed) as caught:
            publish(
                projection,
                out_dir=out,
                window=WINDOW,
                salt=SALT,
                salt_fingerprint=RefHasher(SALT).fingerprint(),
                code_commit="e" * 40,
                code_dirty=False,
                extra_forbidden={SHORT_ACCOUNT},
            )
        assert {(v.table, v.column) for v in caught.value.violations} == {
            (table, column)
        }
        assert not out.exists()
        assert list(tmp_path.glob(f".{out.name}.staging-*")) == []


def test_a_digits_only_account_number_in_a_numeric_column_is_refused() -> None:
    # 8 digits is a valid amount by shape; only the deny list can refuse it.
    rows = [{**dict.fromkeys(BALANCES.column_names), "cash": SHORT_ACCOUNT}]
    found = scan_table(BALANCES, BALANCES.column_names, rows, forbidden={"87654321"})
    assert [(v.column, v.rule) for v in found] == [("cash", "forbidden_value")]
    assert scan_table(BALANCES, BALANCES.column_names, rows, forbidden=()) == []


def test_a_real_amount_that_only_contains_an_identifier_passes() -> None:
    rows = [{**dict.fromkeys(BALANCES.column_names), "cash": "187654321.5"}]
    assert (
        scan_table(BALANCES, BALANCES.column_names, rows, forbidden={"87654321"}) == []
    )


def test_an_integer_column_refuses_an_order_number_sized_value() -> None:
    rows = [{**dict.fromkeys(FILLS.column_names), "fill_seq": "1234567"}]
    found = scan_table(FILLS, FILLS.column_names, rows)
    assert [(v.column, v.rule) for v in found] == [("fill_seq", "shape_integer")]


def test_a_v2_export_with_seeds_and_balances_publishes(tmp_path: Path) -> None:
    seed = fill_row(
        id=60,
        source="manual_import",
        broker_order_id=SEED_ORDER_ID,
        correlation_id=None,
        filled_at=CREATED,
    )
    manifest = _publish(
        projected(execution_ledger=[fill_row(), seed]), tmp_path / "export"
    )
    assert manifest["schema_version"] == "dots-history-export/v2"
    assert manifest["tables"]["execution_ledger"]["row_count"] == 2
    assert manifest["tables"]["account_balance_daily"]["row_count"] == 2 + len(
        BALANCE_UNAVAILABLE
    )
    body = (tmp_path / "export" / "execution_ledger.csv").read_text("utf-8")
    assert SEED_ORDER_ID not in body
