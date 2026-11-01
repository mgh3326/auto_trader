"""ROB-1438 acceptance 2: a planted identifier fails the run and publishes nothing."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from app.services.dots_history_export.leak_check import (
    LEAK_PATTERNS,
    LeakCheckFailed,
    Violation,
    scan_table,
)
from app.services.dots_history_export.projection import Projection, RefHasher
from app.services.dots_history_export.publish import (
    _write_all,
    check_written_files,
    publish,
)
from app.services.dots_history_export.spec import TABLE_BY_NAME, TABLES
from scripts import export_dots_history as cli
from tests.services.dots_history_export._fixtures import (
    SALT,
    TOSS_ORDER_ID,
    WINDOW,
    kis_live_row,
    projected,
)

pytestmark = pytest.mark.unit

ORDERS = TABLE_BY_NAME["kis_live_order_ledger"]
COMMIT = "b" * 40
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl"


def _publish(projection: Projection, out: Path, **kwargs) -> dict:
    return publish(
        projection,
        out_dir=out,
        window=WINDOW,
        salt=SALT,
        salt_fingerprint=RefHasher(SALT).fingerprint(),
        code_commit=COMMIT,
        code_dirty=False,
        **kwargs,
    )


def _plant(column: str, value: str) -> Projection:
    projection = projected()
    projection.tables["kis_live_order_ledger"][0][column] = value
    return projection


@pytest.mark.parametrize(
    ("column", "value", "rule"),
    [
        ("venue", "87654321-01", "account_number"),
        ("strategy_tag", "acct-123-45-678901", "account_number"),
        ("strategy_tag", "acct 12345", "shape_tag"),
        ("strategy_tag", "cancel_unconfirmed:12345", "long_digit_run"),
        ("strategy_tag", "87654321", "long_digit_run"),
        ("venue", "0012345678", "long_digit_run"),
        ("symbol", "0012345678", "long_digit_run"),
        ("qty", "0012345678", "shape_decimal"),
        ("qty", "1234567890", "order_id_shaped_number"),
        ("strategy_tag", "0f3c7e8a-1b2d-4c5e-8f90-a1b2c3d4e5f6", "uuid"),
        ("strategy_tag", JWT, "jwt"),
        ("venue", "ghp_abcdefghijklmnop1234", "token_prefix"),
        ("venue", "AKIAIOSFODNN7EXAMPLE", "token_prefix"),
        ("strategy_tag", "bearer abc123", "shape_tag"),
        ("strategy_tag", "api_key=abc", "shape_tag"),
        ("strategy_tag", "api_key:abc", "credential_assignment"),
        ("strategy_tag", "tossp6-a1b2c3d4e5f6a7b8", "hex_secret"),
        ("venue", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8", "long_opaque_run"),
        ("order_ref", "0012345678", "shape_ref"),
        ("order_ref", "ord_short", "shape_ref"),
        ("correlation_ref", "ord_" + "a" * 32, "shape_ref"),
        ("account_alias", "87654321-01", "value_not_in_enum"),
        ("terminal_outcome", "closed", "value_not_in_enum"),
        ("created_at_utc", "87654321-01", "shape_ts_utc"),
        ("trading_day", "87654321-01", "shape_date"),
    ],
)
def test_a_planted_value_fails_the_run_and_publishes_nothing(
    tmp_path: Path, column: str, value: str, rule: str
) -> None:
    out = tmp_path / "export"
    with pytest.raises(LeakCheckFailed) as raised:
        _publish(_plant(column, value), out)
    assert (
        Violation("kis_live_order_ledger", column, 1, rule) in raised.value.violations
    )
    assert list(tmp_path.iterdir()) == []


def test_the_failure_never_repeats_the_value(tmp_path: Path) -> None:
    with pytest.raises(LeakCheckFailed) as raised:
        _publish(_plant("venue", "87654321-01"), tmp_path / "export")
    rendered = json.dumps([item.as_dict() for item in raised.value.violations])
    assert "87654321" not in rendered and "87654321" not in str(raised.value)


def test_an_order_id_read_from_another_row_is_refused_anywhere(tmp_path: Path) -> None:
    # No generic pattern matches this id; only having read it can refuse it.
    with pytest.raises(LeakCheckFailed) as raised:
        _publish(_plant("strategy_tag", f"retry-{TOSS_ORDER_ID}"), tmp_path / "export")
    assert [item.rule for item in raised.value.violations] == ["forbidden_value"]


def test_an_alphanumeric_order_id_in_a_symbol_cell_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LeakCheckFailed) as raised:
        _publish(_plant("symbol", TOSS_ORDER_ID), tmp_path / "export")
    assert [item.rule for item in raised.value.violations] == ["forbidden_value"]


def test_an_operator_supplied_value_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LeakCheckFailed):
        _publish(
            _plant("strategy_tag", "desk-acct-A77X21"),
            tmp_path / "export",
            extra_forbidden={"A77X21"},
        )


def test_the_salt_itself_is_refused_in_any_cell(tmp_path: Path) -> None:
    salt = "desk-salt-value-with-enough-length-for-a-tag"
    with pytest.raises(LeakCheckFailed) as raised:
        publish(
            _plant("strategy_tag", salt),
            out_dir=tmp_path / "export",
            window=WINDOW,
            salt=salt.encode(),
            salt_fingerprint="x",
            code_commit=COMMIT,
            code_dirty=False,
        )
    assert [item.rule for item in raised.value.violations] == ["forbidden_value"]


def test_a_ledger_value_arriving_through_projection_is_caught_end_to_end(
    tmp_path: Path,
) -> None:
    # Symbols are copied, not hashed, so the gate decides.
    projection = projected(kis_live_order_ledger=[kis_live_row(symbol="87654321-01")])
    with pytest.raises(LeakCheckFailed) as raised:
        _publish(projection, tmp_path / "export")
    assert {item.column for item in raised.value.violations} == {"symbol"}
    assert not (tmp_path / "export").exists()


def test_a_real_order_id_used_as_a_strategy_label_fails_end_to_end(
    tmp_path: Path,
) -> None:
    projection = projected(kis_live_order_ledger=[kis_live_row(strategy=TOSS_ORDER_ID)])
    with pytest.raises(LeakCheckFailed) as raised:
        _publish(projection, tmp_path / "export")
    assert {item.rule for item in raised.value.violations} == {"forbidden_value"}


def test_a_column_outside_the_allowlist_fails(tmp_path: Path) -> None:
    projection = _plant("broker_order_id", "abc")
    with pytest.raises(LeakCheckFailed) as raised:
        _publish(projection, tmp_path / "export")
    assert (
        Violation(
            "kis_live_order_ledger", "broker_order_id", 1, "column_not_allowlisted"
        )
        in raised.value.violations
    )


def test_a_missing_or_extra_header_column_is_a_violation() -> None:
    columns = [*ORDERS.column_names[1:], "account_no"]
    rules = {item.rule for item in scan_table(ORDERS, columns, [])}
    assert rules == {"column_not_allowlisted", "column_missing"}


def _write_then_tamper(tamper):
    def write(directory: Path, projection: Projection) -> None:
        _write_all(directory, projection)
        tamper(directory)

    return write


def test_the_written_csv_is_rechecked_not_just_the_rows(tmp_path: Path) -> None:
    def add_column(directory: Path) -> None:
        path = directory / "kis_live_order_ledger.csv"
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        rows[0].append("account_no")
        rows[1].append("87654321-01")
        with path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)

    out = tmp_path / "export"
    with pytest.raises(LeakCheckFailed) as raised:
        _publish(projected(), out, write_files=_write_then_tamper(add_column))
    assert any(
        item.table == "kis_live_order_ledger.csv"
        and item.rule == "column_not_allowlisted"
        for item in raised.value.violations
    )
    assert list(tmp_path.iterdir()) == []


def test_an_unexpected_file_in_the_output_fails(tmp_path: Path) -> None:
    def add_file(directory: Path) -> None:
        (directory / "raw_dump.json").write_text("{}", "utf-8")

    with pytest.raises(LeakCheckFailed) as raised:
        _publish(
            projected(), tmp_path / "export", write_files=_write_then_tamper(add_file)
        )
    assert any(item.rule == "file_not_allowlisted" for item in raised.value.violations)
    assert list(tmp_path.iterdir()) == []


def test_salt_bytes_in_a_written_csv_fail(tmp_path: Path) -> None:
    directory = tmp_path / "staged"
    directory.mkdir()
    projection = projected()
    _write_all(directory, projection)
    path = directory / "paper_trades.csv"
    path.write_bytes(path.read_bytes() + SALT)
    violations = check_written_files(
        directory,
        {spec.name: len(projection.tables[spec.name]) for spec in TABLES},
        forbidden=(),
        salt=SALT,
    )
    assert any(item.rule == "salt_in_output" for item in violations)


def test_legitimate_labels_and_symbols_pass() -> None:
    row = {
        "symbol": "BRK.B",
        "venue": "NASD",
        "strategy_tag": "expired_inference:kis_regular_day_order_no_broker_original",
        "status": "accepted",
        "qty": "150000000.00000000",
        "limit_price": "123456789",
    }
    assert scan_table(ORDERS, ORDERS.column_names, [row]) == []
    for symbol in ("005930", "KRW-BTC", "BTC/USD", "Q500001", "AAPL"):
        assert scan_table(ORDERS, ORDERS.column_names, [{"symbol": symbol}]) == []
    for tag in ("risk-reward", "sk-hynix-momentum", "h3-crypto-paper", "dip-v2"):
        cell = {"strategy_tag": tag}
        assert scan_table(ORDERS, ORDERS.column_names, [cell]) == []


def test_a_placeholder_word_used_as_an_order_id_does_not_block_a_status_tag(
    tmp_path: Path,
) -> None:
    projection = projected(
        kis_live_order_ledger=[kis_live_row(order_no="pending", status="pending")]
    )
    assert "pending" not in projection.forbidden
    _publish(projection, tmp_path / "export")


def test_every_rule_name_is_reported_in_the_manifest(tmp_path: Path) -> None:
    manifest = _publish(projected(), tmp_path / "export")
    assert set(manifest["leak_check"]["rules"]) == {name for name, _ in LEAK_PATTERNS}


def test_cli_exits_2_and_prints_locations_only(monkeypatch, capsys) -> None:
    async def fails(_args):
        raise LeakCheckFailed([Violation("t", "c", 3, "uuid")])

    monkeypatch.setattr(cli, "run", fails)
    code = cli.main(
        ["--database-url-env", "X", "--salt-env", "Y", "--out", "/nonexistent/out"]
    )
    captured = capsys.readouterr()
    assert code == cli.EXIT_LEAK
    payload = json.loads(captured.err)
    assert payload["error"] == "leak_check_failed"
    assert payload["violations"] == [
        {"table": "t", "column": "c", "row": 3, "rule": "uuid"}
    ]


def test_a_short_deny_value_is_refused_not_silently_ignored(tmp_path: Path) -> None:
    deny = tmp_path / "deny"
    deny.write_text("A77\n", "utf-8")
    with pytest.raises(ValueError, match="shorter") as raised:
        cli.load_deny_values(deny, repo_root=tmp_path / "repo")
    assert "A77" not in str(raised.value)
