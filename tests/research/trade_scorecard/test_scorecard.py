import csv
import json
import subprocess
import sys
from datetime import date, timedelta

import pytest

from tests.research.trade_retro.retro_fixtures import (
    fill_row,
    order_row,
    weekdays,
    write_export,
    write_prices,
)

pytestmark = pytest.mark.unit


def test_offline_cli_writes_three_axes_and_public_counts(tmp_path):
    account, symbol = "kis_live", "123456"
    days = weekdays(date(2026, 8, 3), 25)
    until = days[9] + timedelta(days=1)
    fills = []
    orders = []
    for i, side, qty, price, limit in (
        (0, "buy", 1, 100, 105),
        (1, "buy", 2, 80, 85),
        (2, "sell", 1, 110, 110),
    ):
        order_day = days[i].isoformat()
        stamp = f"{order_day}T01:00:00Z"
        ref = f"private-order-{i}"
        fill_day = days[3].isoformat() if side == "sell" else order_day
        fills.append(
            fill_row(
                account,
                "kr",
                symbol,
                side,
                qty,
                price,
                f"{fill_day}T01:05:00Z",
                ref,
                fill_day,
            )
        )
        orders.append(
            order_row(
                account,
                "kr",
                symbol,
                side,
                qty,
                limit,
                stamp,
                ref,
                "filled",
                order_day,
                account_ref="private-account-789",
                strategy_tag="기밀전략",
            )
        )
    export = write_export(
        tmp_path / "export",
        {"execution_ledger": fills, "kis_live_order_ledger": orders},
    )
    benchmarks = [(day, 1_000 + 10 * i) for i, day in enumerate(days)]
    prices = write_prices(
        tmp_path / "prices",
        {
            **{f"benchmark/{m}": benchmarks for m in ("kr", "us", "crypto")},
            f"kr/{symbol}": [
                (day, 70 if i == 9 else 90 + i) for i, day in enumerate(days)
            ],
        },
    )
    public = tmp_path / "public.md"
    private = tmp_path / "private"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "research.trade_scorecard.run",
            "--export",
            str(export),
            "--prices",
            str(prices),
            "--account",
            account,
            "--until",
            until.isoformat(),
            "--public-out",
            str(public),
            "--private-dir",
            str(private),
        ],
        check=True,
    )
    with (private / "scorecard.csv").open() as handle:
        exit_row, open_row = list(csv.DictReader(handle))
    assert exit_row["date"] == days[3].isoformat()
    assert exit_row["kind"] == "exit"
    selection = json.loads(exit_row["selection"])
    assert selection["5"]["excess_index"] == pytest.approx(95 / 90 - 1.05)
    assert selection["20"]["excess_index"] == pytest.approx(110 / 90 - 1.2)
    assert selection["20"]["sector_missing_reason"] == "no_sector"
    first = json.loads(exit_row["entry"])
    assert first["type"] == "first"
    assert first["rationale"] == "근거 미기록"
    assert first["decision_missing_reason"] == "no_decision_evidence"
    assert first["fill_vs_order_limit"] == pytest.approx(100 / 105 - 1)
    assert first["fill_vs_open"] == pytest.approx(100 / 90 - 1)
    assert first["fill_vs_next_open"] == pytest.approx(100 / 91 - 1)
    assert json.loads(exit_row["exit_wait"])["realized"] == 10

    assert open_row["kind"] == "open"
    add = json.loads(open_row["entry"])
    assert add["type"] == "add"
    assert add["average_down"] is True
    wait = json.loads(open_row["exit_wait"])
    assert wait["unrealized"] == wait["add_money_gross_pnl"] == -20
    assert wait["dividend_missing_reason"] == "no_dividend"
    assert wait["cost_missing_reason"] == "no_cost"
    assert wait["net"] is None

    public_text = public.read_text()
    assert "no_declined_candidates" in public_text
    assert "| 실현 FIFO 조각 | 1 |" in public_text
    for private_value in (
        "private-account-789",
        symbol,
        account,
        "private-order",
        "기밀전략",
        "-20",
        "110",
    ):
        assert private_value not in public_text
