"""Conflict report for the DART receipt identity switch (hk 6408). No database."""

from __future__ import annotations

import pytest

from scripts import report_dart_identity_conflicts as report


def test_clean_database_reports_clean_and_exits_zero():
    body, code = report.build_report(
        summary={"dart_rows": 10, "receipts": 10},
        conflicts=[],
        malformed_rows=0,
    )

    assert code == 0
    assert body["status"] == "clean"
    assert body["multi_row_receipt_count"] == 0
    assert body["malformed_receipts"] == 0


def test_multi_row_receipt_is_listed_by_id_and_category_only():
    body, code = report.build_report(
        summary={"dart_rows": 3, "receipts": 2},
        conflicts=[
            {
                "source_event_id": "20261127000777",
                "row_count": 2,
                "categories": ["earnings", "disclosure"],
                "row_ids": [5, 9],
            }
        ],
        malformed_rows=0,
    )

    assert code == 1
    assert body["status"] == "conflicts"
    assert body["multi_row_receipts"] == [
        {
            "source_event_id": "20261127000777",
            "row_count": 2,
            "categories": ["disclosure", "earnings"],
            "row_ids": [5, 9],
        }
    ]


def test_malformed_receipt_id_is_a_conflict_too():
    body, code = report.build_report(
        summary={"dart_rows": 1, "receipts": 1},
        conflicts=[],
        malformed_rows=1,
    )

    assert code == 1
    assert body["malformed_receipts"] == 1


def test_reads_are_read_only_select_statements():
    for statement in (
        report.SUMMARY_SQL,
        report.CONFLICT_SQL,
        report.MALFORMED_SQL,
    ):
        sql = str(statement).upper()
        assert sql.lstrip().startswith("SELECT")
        for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "DROP"):
            assert verb not in sql


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
