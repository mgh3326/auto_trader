import csv
import json
import subprocess
import sys
from datetime import date, timedelta

import pytest

from research.trade_retro.export import load_export
from research.trade_retro.prices import load_snapshot
from research.trade_scorecard.score import Evidence, score, selection
from tests.research.trade_retro.retro_fixtures import (
    fill_row,
    order_row,
    weekdays,
    write_export,
    write_prices,
)

pytestmark = pytest.mark.unit


def _book(tmp_path, symbol_bars):
    """Write a price snapshot and return its directory."""
    days = weekdays(date(2026, 8, 3), 25)
    benchmarks = [(day, 1_000 + 10 * i) for i, day in enumerate(days)]
    return write_prices(
        tmp_path / "prices",
        {
            **{f"benchmark/{m}": benchmarks for m in ("kr", "us", "crypto")},
            **symbol_bars,
        },
    )


def test_offline_cli_writes_three_axes_and_public_counts(tmp_path):
    account, symbol = "kis_live", "123456"
    days = weekdays(date(2026, 8, 3), 25)
    until = days[9] + timedelta(days=1)
    fills = []
    orders = []
    for i, side, qty, price, limit in (
        (0, "buy", 1, 100, 105),
        (1, "buy", 2, 80, 85),
        (2, "sell", 2, 110, 110),
    ):
        order_day = days[i].isoformat()
        stamp = f"{order_day}T01:00:00Z"
        ref = f"private-order-{i}"
        # The sell's ledger day is one day after its DAY order's trading day:
        # the default Cleaning gives it the order's day, not the ledger stamp.
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
    prices = _book(
        tmp_path,
        {
            f"kr/{symbol}": [
                (day, 70 if i == 9 else 90 + i) for i, day in enumerate(days)
            ]
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
        exit_row, add_exit_row, open_row = list(csv.DictReader(handle))
    # The sell took the order's day (days[2]), not its later ledger day.
    assert exit_row["date"] == days[2].isoformat()
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

    # The second realized slice exits one share of the add tranche: its slice
    # P&L (1 @ 80 -> 110) differs from the whole tranche's P&L at that price.
    add_exit = json.loads(add_exit_row["entry"])
    assert add_exit["type"] == "add"
    assert add_exit["average_down"] is True
    add_exit_wait = json.loads(add_exit_row["exit_wait"])
    assert add_exit_wait["realized"] == 30
    assert add_exit_wait["add_money_gross_pnl"] == 60
    assert add_exit_wait["average_down_effect"] == 40

    assert open_row["kind"] == "open"
    add = json.loads(open_row["entry"])
    assert add["type"] == "add"
    assert add["average_down"] is True
    wait = json.loads(open_row["exit_wait"])
    # Remaining 1 @ 80 marked at 70: slice P&L -10 vs the whole 2-share
    # tranche's -20; the basis effect is 2 * (100 - 80) = 40 either way.
    assert wait["unrealized"] == -10
    assert wait["add_money_gross_pnl"] == -20
    assert wait["average_down_effect"] == 40
    assert wait["add_money_gross_pnl"] != wait["unrealized"]
    assert wait["dividend_missing_reason"] == "no_dividend"
    assert wait["cost_missing_reason"] == "no_cost"
    assert wait["net"] is None

    public_text = public.read_text()
    assert "no_declined_candidates" in public_text
    assert "| 실현 FIFO 조각 | 2 |" in public_text
    assert "| 미청산 FIFO 조각 | 1 |" in public_text
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


def test_seed_cutover_is_shared_across_accounts_and_markets(tmp_path):
    """An account whose first KR seed is a later batch still scores earlier buys.

    trade_retro's cutover is the first manual_import batch shared by every
    broker and market; scoping it to this account's own seeds would hide real
    in-window buys inside a later batch (hk 6773).
    """
    account, symbol = "kis_live", "123456"
    days = weekdays(date(2026, 8, 3), 25)
    until = days[24] + timedelta(days=1)
    early, buy_day, late = days[0], days[5], days[10]
    export = write_export(
        tmp_path / "export",
        {
            "execution_ledger": [
                # Another account's earlier seed batch sets the shared cutover.
                fill_row(
                    "toss_live",
                    "us",
                    "AAA",
                    "buy",
                    5,
                    90,
                    f"{early}T01:05:00Z",
                    "seed-us",
                    early,
                    source="manual_import",
                ),
                fill_row(
                    account,
                    "kr",
                    symbol,
                    "buy",
                    1,
                    100,
                    f"{buy_day}T01:05:00Z",
                    "o1",
                    buy_day,
                ),
                fill_row(
                    account,
                    "kr",
                    symbol,
                    "buy",
                    5,
                    90,
                    f"{late}T01:05:00Z",
                    "seed-kr",
                    late,
                    source="manual_import",
                ),
            ],
            "kis_live_order_ledger": [
                order_row(
                    account,
                    "kr",
                    symbol,
                    "buy",
                    1,
                    105,
                    f"{buy_day}T01:00:00Z",
                    "o1",
                    "filled",
                    buy_day,
                )
            ],
        },
    )
    book = load_snapshot(
        _book(tmp_path, {f"kr/{symbol}": [(day, 90 + i) for i, day in enumerate(days)]})
    )
    cards = score(load_export(export), book, account, until)
    entries = [card["entry"] for card in cards if card.get("entry")]
    assert any(entry["type"] == "first" for entry in entries)


def test_implausible_fill_is_an_opening_lot_not_a_scored_trade(tmp_path):
    """A fill >30% from its session close becomes an opening lot at the close
    basis (trade_retro's reclassify_implausible), never a scored trade, and it
    does not poison a later add's prior average with the bogus price."""
    account, symbol = "kis_live", "123456"
    days = weekdays(date(2026, 8, 3), 25)
    until = days[24] + timedelta(days=1)
    d0, d1 = days[0], days[1]
    export = write_export(
        tmp_path / "export",
        {
            "execution_ledger": [
                # 100 vs a 60 close: implausible booked price, kept as a lot.
                fill_row(
                    account,
                    "kr",
                    symbol,
                    "buy",
                    1,
                    100,
                    f"{d0}T01:05:00Z",
                    "o1",
                    d0,
                ),
                # 70 vs 60 is within tolerance: a real trade.
                fill_row(
                    account,
                    "kr",
                    symbol,
                    "buy",
                    1,
                    70,
                    f"{d1}T01:05:00Z",
                    "o2",
                    d1,
                ),
            ],
            "kis_live_order_ledger": [
                order_row(
                    account,
                    "kr",
                    symbol,
                    "buy",
                    1,
                    105,
                    f"{d0}T01:00:00Z",
                    "o1",
                    "filled",
                    d0,
                ),
                order_row(
                    account,
                    "kr",
                    symbol,
                    "buy",
                    1,
                    75,
                    f"{d1}T01:00:00Z",
                    "o2",
                    "filled",
                    d1,
                ),
            ],
        },
    )
    book = load_snapshot(_book(tmp_path, {f"kr/{symbol}": [(day, 60) for day in days]}))
    cards = score(load_export(export), book, account, until)
    entries = [card["entry"] for card in cards if card.get("entry")]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["type"] == "add"
    assert entry["prior_average"] == pytest.approx(60)
    assert entry["average_down"] is False
    unscored = [card for card in cards if card.get("entry") is None]
    assert unscored and all(
        card["entry_missing_reason"] == "no_decision_evidence" for card in unscored
    )


def test_unmatured_horizon_reports_not_matured_not_no_price(tmp_path):
    days = weekdays(date(2026, 8, 3), 25)
    book = load_snapshot(_book(tmp_path, {}))
    # Third-to-last benchmark session: neither the 5- nor the 20-session
    # horizon can mature, which is pending, not a missing price.
    result = selection(book, "123456", days[-3], Evidence())
    assert result["5"]["missing_reason"] == "not_matured"
    assert result["20"]["missing_reason"] == "not_matured"
    matured = selection(book, "123456", days[5], Evidence())
    assert matured["5"]["missing_reason"] == "no_price"
