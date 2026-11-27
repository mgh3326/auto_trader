"""Conflict report for the DART receipt identity switch (hk 6408). No database.

The report prints counts only. Nothing in its output may name a receipt, a
category or a row id.
"""

from __future__ import annotations

import json

import pytest

from scripts import report_dart_identity_conflicts as report


def test_clean_database_reports_clean_and_exits_zero():
    body, code = report.build_report(
        summary={"dart_rows": 10, "receipts": 10},
        conflicts={"multi_row_receipts": 0, "max_rows_per_receipt": 0},
        malformed_rows=0,
    )

    assert code == 0
    assert body == {
        "status": "clean",
        "dart_rows": 10,
        "receipts": 10,
        "multi_row_receipts": 0,
        "max_rows_per_receipt": 0,
        "malformed_receipts": 0,
    }


def test_multi_row_receipts_are_counted_not_listed():
    body, code = report.build_report(
        summary={"dart_rows": 3, "receipts": 2},
        conflicts={"multi_row_receipts": 1, "max_rows_per_receipt": 2},
        malformed_rows=0,
    )

    assert code == 1
    assert body["status"] == "conflicts"
    assert body["multi_row_receipts"] == 1
    assert body["max_rows_per_receipt"] == 2


def test_malformed_receipt_id_is_a_conflict_too():
    body, code = report.build_report(
        summary={"dart_rows": 1, "receipts": 1},
        conflicts={"multi_row_receipts": 0, "max_rows_per_receipt": 0},
        malformed_rows=1,
    )

    assert code == 1
    assert body["malformed_receipts"] == 1


def test_output_has_only_counts_no_identifiers_or_categories():
    body, _ = report.build_report(
        summary={"dart_rows": 3, "receipts": 2},
        conflicts={"multi_row_receipts": 1, "max_rows_per_receipt": 2},
        malformed_rows=0,
    )

    assert all(isinstance(value, (int, str)) for value in body.values())
    rendered = json.dumps(body)
    assert "2026" not in rendered
    assert "disclosure" not in rendered and "earnings" not in rendered


def test_reads_are_read_only_select_statements():
    for statement in (report.SUMMARY_SQL, report.CONFLICT_SQL, report.MALFORMED_SQL):
        sql = str(statement).upper()
        assert sql.lstrip().startswith("SELECT")
        for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "DROP"):
            assert verb not in sql


def test_the_read_opens_a_read_only_transaction_with_a_statement_timeout():
    import inspect

    source = inspect.getsource(report._read)
    assert "SET TRANSACTION READ ONLY" in source
    assert "statement_timeout = '8s'" in source


def test_missing_database_url_env_exits_before_any_connection(monkeypatch, capsys):
    monkeypatch.delenv("DART_IDENTITY_REPORT_TEST_URL", raising=False)

    code = report.main(["--database-url-env", "DART_IDENTITY_REPORT_TEST_URL"])

    assert code == 2
    assert "missing_database_url" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["DATABASE_URL", "OPENDART_API_KEY"])
def test_report_never_prints_the_url_value(monkeypatch, capsys, name):
    monkeypatch.setenv(name, "postgresql+asyncpg://user:SECRET@example/x")
    monkeypatch.setattr(report, "_read", lambda _url: (_ for _ in ()).throw(OSError()))

    code = report.main(["--database-url-env", name])

    out = capsys.readouterr().out
    assert code == 2
    assert "SECRET" not in out


def test_a_null_receipt_number_is_counted_as_malformed_and_as_a_dart_row():
    malformed = str(report.MALFORMED_SQL)
    summary = str(report.SUMMARY_SQL)

    assert "source_event_id IS NULL" in malformed
    assert "source_event_id IS NOT NULL" not in summary
