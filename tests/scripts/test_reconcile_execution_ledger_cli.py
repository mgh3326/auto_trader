# tests/scripts/test_reconcile_execution_ledger_cli.py
from __future__ import annotations

from datetime import UTC, datetime

import scripts.reconcile_execution_ledger as cli


def test_parse_args_accepts_explicit_date_window(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "reconcile_execution_ledger.py",
            "--broker",
            "kis",
            "--start-date",
            "2026-02-01",
            "--end-date",
            "2026-02-08",
            "--max-pages",
            "25",
        ],
    )

    args = cli.parse_args()
    start_at, end_at = cli.resolve_window_args(args)

    assert start_at == datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
    assert end_at == datetime(2026, 2, 8, 23, 59, 59, 999999, tzinfo=UTC)
    assert args.max_pages == 25
