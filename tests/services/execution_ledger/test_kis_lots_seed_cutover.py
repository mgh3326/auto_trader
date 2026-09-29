"""Task #973 — seed cutover: pre-seed reconciler rows must not double count.

The seed CLI writes one ``manual_import`` row per symbol stamped ``SEED-*``
with ``filled_at`` equal to the ``--cutover`` instant (UTC midnight,
``scripts/seed_execution_ledger_opening_lots.parse_cutover``) and sizes it as
``current_qty - net(filled_at >= cutover)`` over non-seed rows
(``ExecutionLedgerRepository.net_quantity_by_match_key_since``,
repository.py:260). So the seed already covers every authoritative row strictly
before that instant — recounting them is the double count desk observed on
#963. A re-seed at a newer cutover inserts a second ``SEED-*`` row (the order
id embeds the cutover date), so the latest seed and its cutover govern.

The main fixture is shaped like the operator-desk table on hk task 973
comment 811. From the comment: per (symbol, source) row counts, signed nets,
first/last ``coalesce(filled_at, created_at)::date``, and broker quantities.
NOT in the comment — synthesized here: per-row quantities, prices, order ids
and times-of-day (every synthesized row is stamped 03:00 UTC = 12:00 KST on its
date). Synthesized rows honor the desk aggregates exactly: each symbol's
reconciler row count, net, and first/last dates match the table, and each
pre-cutover reconciler net equals the desk seed quantity (that identity is
desk's own finding — the reconciler history is complete back to position
origin, so the ledger-booked pre-seed position IS the seed).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.execution_ledger.kis_lots import (
    UNKNOWN_QTY_MISMATCH,
    Freshness,
    LedgerFill,
    build_symbol_block,
)

pytestmark = pytest.mark.unit

# 2026-09-29 14:30 KST — same rep instant as test_kis_lots.py.
NOW = datetime(2026, 9, 29, 5, 30, tzinfo=UTC)
FRESH = Freshness("fresh", NOW - timedelta(minutes=20), 20.0)
D = Decimal

# Desk's seed cutover for every symbol: 2026-05-10, written by the CLI as the
# UTC-midnight-aware instant (parse_cutover) in filled_at.
CUTOVER = datetime(2026, 5, 10, tzinfo=UTC)
CUTOVER_ISO = "2026-05-10T00:00:00+00:00"

# Re-seed cutover used by the Q-88 re-seed scenario (a later date so its
# SEED-<yyyymmdd>- order id and filled_at differ from the first generation).
RECUTOVER = datetime(2026, 10, 1, tzinfo=UTC)


def _at(date: str, hour_utc: int = 3) -> datetime:
    """Synthesized timestamp: 03:00 UTC = 12:00 KST on the desk-table date."""
    return datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC, hour=hour_utc)


def _seed(symbol: str, qty: str, *, cutover: datetime = CUTOVER) -> LedgerFill:
    """A manual_import opening seed exactly as the seed CLI writes it."""
    return LedgerFill(
        id=-1,
        source="manual_import",
        side="buy",
        quantity=D(qty),
        price=D("50000"),
        filled_at=cutover,
        broker_order_id=f"SEED-{cutover:%Y%m%d}-kis-krx-{symbol}",
    )


def _row(fid: int, side: str, qty: str, date: str, symbol: str) -> LedgerFill:
    return LedgerFill(
        id=fid,
        source="reconciler",
        side=side,
        quantity=D(qty),
        price=D("50000"),
        filled_at=_at(date),
        broker_order_id=f"R-{symbol}-{fid:03d}",
    )


def _ws(fid: int, side: str, qty: str, date: str, symbol: str) -> LedgerFill:
    f = _row(fid, side, qty, date, symbol)
    return LedgerFill(
        id=f.id,
        source="websocket",
        side=f.side,
        quantity=f.quantity,
        price=f.price,
        filled_at=f.filled_at,
        broker_order_id=f"WS-{symbol}-{fid:03d}",
    )


@dataclass(frozen=True)
class DeskCase:
    """One row of the desk table. Nets/dates/counts are desk's; row detail is
    synthesized. ``pre``/``post`` = reconciler (date, side, qty) rows strictly
    before / on-or-after the 05-10 cutover; ``ws`` = provisional websocket rows."""

    symbol: str
    broker_qty: str
    seed_qty: str | None
    pre: tuple[tuple[str, str, str], ...]
    post: tuple[tuple[str, str, str], ...]
    ws: tuple[tuple[str, str, str], ...] = ()
    state: str = "known"
    gap_reason: str = ""


DESK_CASES: list[DeskCase] = [
    # --- the 9 double-counted symbols: reconciler-only net == broker qty ---
    DeskCase(
        "000270",
        broker_qty="11",
        seed_qty="12",
        pre=(
            ("2026-03-11", "buy", "5"),
            ("2026-03-20", "buy", "4"),
            ("2026-04-02", "buy", "3"),
        ),
        post=(
            ("2026-05-12", "sell", "2"),
            ("2026-06-01", "sell", "1"),
            ("2026-07-01", "buy", "1"),
            ("2026-07-07", "buy", "1"),
        ),
        ws=(("2026-07-07", "buy", "2"),),
    ),
    DeskCase(
        "004020",
        broker_qty="2",
        seed_qty="7",
        pre=(("2026-03-11", "buy", "5"), ("2026-04-20", "buy", "2")),
        post=(("2026-05-12", "sell", "3"), ("2026-05-14", "sell", "2")),
        ws=(("2026-05-14", "sell", "1"),),
    ),
    DeskCase(
        "015760",
        broker_qty="100",
        seed_qty="70",
        pre=(
            ("2026-03-11", "buy", "20"),
            ("2026-03-18", "buy", "15"),
            ("2026-04-01", "buy", "15"),
            ("2026-04-20", "buy", "10"),
            ("2026-05-08", "buy", "10"),
        ),
        post=(
            ("2026-05-12", "buy", "10"),
            ("2026-05-20", "buy", "10"),
            ("2026-05-26", "buy", "10"),
            ("2026-05-28", "sell", "5"),
            ("2026-06-01", "buy", "5"),
        ),
        ws=(("2026-06-01", "buy", "10"),),
    ),
    DeskCase(
        "024110",
        broker_qty="10",
        seed_qty="10",
        pre=(("2026-03-12", "buy", "10"),),
        post=(),
    ),
    DeskCase(
        "034220",
        broker_qty="2",
        seed_qty="25",
        pre=(("2026-03-20", "buy", "15"), ("2026-04-10", "buy", "10")),
        post=(
            ("2026-05-15", "sell", "10"),
            ("2026-06-01", "sell", "10"),
            ("2026-06-15", "sell", "3"),
        ),
        ws=(
            ("2026-06-09", "sell", "3"),
            ("2026-06-12", "sell", "3"),
            ("2026-06-15", "sell", "2"),
        ),
    ),
    DeskCase(
        "035420",
        broker_qty="3",
        seed_qty="62",
        pre=(
            ("2026-02-23", "buy", "10"),
            ("2026-03-02", "buy", "8"),
            ("2026-03-09", "buy", "10"),
            ("2026-03-16", "buy", "10"),
            ("2026-03-23", "buy", "8"),
            ("2026-04-06", "buy", "6"),
            ("2026-04-20", "buy", "6"),
            ("2026-05-08", "buy", "4"),
        ),
        post=(
            ("2026-05-19", "sell", "10"),
            ("2026-05-22", "sell", "8"),
            ("2026-05-27", "sell", "8"),
            ("2026-05-29", "sell", "8"),
            ("2026-06-02", "sell", "8"),
            ("2026-06-05", "sell", "8"),
            ("2026-06-09", "sell", "8"),
            ("2026-06-12", "sell", "1"),
        ),
        ws=(
            ("2026-05-19", "sell", "5"),
            ("2026-05-26", "sell", "6"),
            ("2026-06-02", "sell", "6"),
            ("2026-06-05", "sell", "5"),
            ("2026-06-09", "sell", "4"),
            ("2026-06-12", "sell", "5"),
        ),
    ),
    DeskCase(
        "035720",
        broker_qty="100",
        seed_qty="100",
        pre=(
            ("2026-03-16", "buy", "25"),
            ("2026-03-20", "buy", "25"),
            ("2026-03-26", "buy", "25"),
            ("2026-04-01", "buy", "25"),
        ),
        post=(),
    ),
    DeskCase(
        "064350",
        broker_qty="7",
        seed_qty="10",
        pre=(
            ("2026-03-16", "buy", "3"),
            ("2026-03-25", "buy", "3"),
            ("2026-04-10", "buy", "2"),
            ("2026-05-06", "buy", "2"),
        ),
        post=(
            ("2026-05-11", "sell", "1"),
            ("2026-05-11", "sell", "1"),
            ("2026-05-11", "sell", "1"),
        ),
    ),
    DeskCase(
        "316140",
        broker_qty="19",
        seed_qty="19",
        pre=(("2026-03-11", "buy", "10"), ("2026-03-11", "buy", "9")),
        post=(),
    ),
    # --- the 2 genuine ledger gaps: must stay unknown ---
    DeskCase(
        "005930",
        broker_qty="1",
        seed_qty=None,  # no seed: nothing is superseded
        pre=(
            ("2026-03-30", "buy", "3"),
            ("2026-04-06", "buy", "2"),
            ("2026-04-15", "buy", "2"),
            ("2026-04-28", "buy", "1"),
            ("2026-05-08", "buy", "1"),
        ),
        post=(
            ("2026-05-20", "buy", "1"),
            ("2026-05-29", "buy", "1"),
            ("2026-06-05", "buy", "1"),
            ("2026-06-12", "sell", "1"),
        ),
        ws=(("2026-06-12", "buy", "1"),),
        state="unknown",
        gap_reason="10 shares sold outside the ledger (desk)",
    ),
    DeskCase(
        "196170",
        broker_qty="6",
        seed_qty="4",
        pre=(("2026-02-23", "buy", "3"), ("2026-04-15", "buy", "1")),
        post=(("2026-05-18", "buy", "1"),),
        state="unknown",
        gap_reason="1 share bought outside the ledger after 05-18 (desk)",
    ),
]


def _desk_fills(case: DeskCase) -> list[LedgerFill]:
    fills: list[LedgerFill] = []
    fid = 1
    if case.seed_qty is not None:
        fills.append(_seed(case.symbol, case.seed_qty))
    for date, side, qty in case.pre:
        fills.append(_row(fid, side, qty, date, case.symbol))
        fid += 1
    for date, side, qty in case.post:
        fills.append(_row(fid, side, qty, date, case.symbol))
        fid += 1
    for date, side, qty in case.ws:
        fills.append(_ws(fid, side, qty, date, case.symbol))
        fid += 1
    return fills


def _block(case: DeskCase, fills: list[LedgerFill] | None = None):
    return build_symbol_block(
        symbol=case.symbol,
        reference_quantity=D(case.broker_qty),
        current_price=None,
        fills=_desk_fills(case) if fills is None else fills,
        orders=(),
        freshness=FRESH,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# desk table: 9 seeded symbols reconcile, the 2 real gaps stay unknown
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", DESK_CASES, ids=lambda c: c.symbol)
def test_desk_table_case(case: DeskCase) -> None:
    out = _block(case)
    assert out["ledger_state"] == case.state, (
        case.symbol,
        case.gap_reason,
        out["unknown_reasons"],
    )
    diag = out["diagnostics"]
    if case.state == "known":
        assert out["unknown_reasons"] == []
        assert out["net_quantity"] == case.broker_qty
        assert out["quantity_reconciles"] is True
        assert sum(D(lot["quantity"]) for lot in out["lots"]) == D(case.broker_qty)
    else:
        assert UNKNOWN_QTY_MISMATCH in out["unknown_reasons"]
        assert out["quantity_reconciles"] is False
        assert out["lots"] is None
        assert out["net_quantity"] is None
    # the superseded set is exactly the pre-cutover reconciler rows, and only
    # when a seed exists — a seedless symbol counts every authoritative row
    if case.seed_qty is None:
        assert diag["pre_seed_rows_superseded"] == []
        assert diag["seed_cutover"] is None
        assert diag["counted_row_count"] == len(case.pre) + len(case.post)
    else:
        assert [r["broker_order_id"] for r in diag["pre_seed_rows_superseded"]] == [
            f"R-{case.symbol}-{i + 1:03d}" for i in range(len(case.pre))
        ]
        assert diag["seed_cutover"] == CUTOVER_ISO
        assert diag["counted_row_count"] == len(case.post) + 1


def test_seeded_symbols_report_the_cutover_instant() -> None:
    case = DESK_CASES[0]
    diag = _block(case)["diagnostics"]
    assert diag["seed_cutover"] == CUTOVER_ISO
    superseded_net = sum(
        D(row["quantity"]) * (1 if row["side"] == "buy" else -1)
        for row in diag["pre_seed_rows_superseded"]
    )
    assert superseded_net == D(case.seed_qty)


def test_seedless_symbol_reports_no_cutover_and_supersedes_nothing() -> None:
    diag = _block(DESK_CASES[9])["diagnostics"]  # 005930: no manual_import row
    assert diag["seed_cutover"] is None
    assert diag["pre_seed_rows_superseded"] == []
    assert diag["authoritative_row_count"] == diag["counted_row_count"]


def test_desk_blocks_remain_json_serializable() -> None:
    for case in DESK_CASES:
        json.dumps(_block(case))


# ---------------------------------------------------------------------------
# cutover boundary: the seed's own rule is filled_at >= cutover -> counted
# ---------------------------------------------------------------------------
def test_row_stamped_exactly_at_the_cutover_instant_counts() -> None:
    """The seeder carved out ``filled_at >= cutover``, so a boundary-stamped
    row was never inside the seed — superseding it would undercount."""
    fills = [
        _seed("196170", "4"),
        _row(1, "buy", "1", "2026-02-23", "196170"),  # superseded
        LedgerFill(  # stamped exactly at the cutover instant
            id=2,
            source="reconciler",
            side="buy",
            quantity=D("2"),
            price=D("50000"),
            filled_at=CUTOVER,
            broker_order_id="R-196170-edge",
        ),
    ]
    out = _block(DESK_CASES[10], fills=fills)
    assert out["ledger_state"] == "known", out["unknown_reasons"]
    assert out["net_quantity"] == "6"
    diag = out["diagnostics"]
    assert [r["broker_order_id"] for r in diag["pre_seed_rows_superseded"]] == [
        "R-196170-001"
    ]
    assert diag["counted_row_count"] == 2


def test_row_one_instant_before_the_cutover_is_superseded() -> None:
    fills = [
        _seed("196170", "4"),
        LedgerFill(
            id=1,
            source="reconciler",
            side="buy",
            quantity=D("1"),
            price=D("50000"),
            filled_at=CUTOVER - timedelta(microseconds=1),
            broker_order_id="R-196170-pre",
        ),
        _row(2, "buy", "1", "2026-05-18", "196170"),
    ]
    out = _block(DESK_CASES[10], fills=fills)
    diag = out["diagnostics"]
    assert [r["broker_order_id"] for r in diag["pre_seed_rows_superseded"]] == [
        "R-196170-pre"
    ]
    assert out["ledger_state"] == "unknown"  # 4 + 1 = 5 != broker 6
    assert diag["counted_row_count"] == 2


def test_same_utc_day_after_the_cutover_instant_counts() -> None:
    """A row on the cutover DATE but after the cutover instant is outside the
    seed (it was carved out by ``filled_at >= cutover``) and must count."""
    fills = [
        _seed("196170", "4"),
        LedgerFill(
            id=1,
            source="reconciler",
            side="buy",
            quantity=D("1"),
            price=D("50000"),
            filled_at=datetime(2026, 5, 10, 5, 0, tzinfo=UTC),  # 14:00 KST 05-10
            broker_order_id="R-196170-day",
        ),
        _row(2, "buy", "1", "2026-05-18", "196170"),
    ]
    out = _block(DESK_CASES[10], fills=fills)
    assert out["ledger_state"] == "known", out["unknown_reasons"]
    assert out["net_quantity"] == "6"
    assert out["diagnostics"]["pre_seed_rows_superseded"] == []


def test_comparison_is_the_utc_instant_not_the_kst_date() -> None:
    """2026-05-09 15:00 UTC = 2026-05-10 00:00 KST: inside the seed despite
    sharing the cutover's KST calendar date."""
    fills = [
        _seed("196170", "4"),
        LedgerFill(
            id=1,
            source="reconciler",
            side="buy",
            quantity=D("1"),
            price=D("50000"),
            filled_at=datetime(2026, 5, 9, 15, 0, tzinfo=UTC),  # KST 05-10
            broker_order_id="R-196170-kst",
        ),
        _row(2, "buy", "1", "2026-05-18", "196170"),
    ]
    out = _block(DESK_CASES[10], fills=fills)
    assert [
        r["broker_order_id"] for r in out["diagnostics"]["pre_seed_rows_superseded"]
    ] == ["R-196170-kst"]
    assert out["ledger_state"] == "unknown"


def test_naive_filled_at_is_read_as_utc() -> None:
    """TIMESTAMP(tz) column → aware stamps; a naive one reads as UTC per the
    module's convention (_aware_utc). The fixture is all-naive so FIFO ordering
    stays comparable."""
    naive = lambda date: datetime.strptime(date, "%Y-%m-%d")  # noqa: E731
    fills = [
        LedgerFill(
            id=0,
            source="manual_import",
            side="buy",
            quantity=D("4"),
            price=D("50000"),
            filled_at=naive("2026-05-10"),  # naive == the UTC cutover instant
            broker_order_id="SEED-20260510-kis-krx-196170",
        ),
        LedgerFill(
            id=1,
            source="reconciler",
            side="buy",
            quantity=D("3"),
            price=D("50000"),
            filled_at=naive("2026-02-23"),
            broker_order_id="R-196170-naive",
        ),
        LedgerFill(
            id=2,
            source="reconciler",
            side="buy",
            quantity=D("1"),
            price=D("50000"),
            filled_at=naive("2026-05-18"),
            broker_order_id="R-196170-naive2",
        ),
    ]
    out = _block(DESK_CASES[10], fills=fills)
    assert out["diagnostics"]["seed_cutover"] == CUTOVER_ISO
    assert out["diagnostics"]["counted_row_count"] == 2
    assert [
        r["broker_order_id"] for r in out["diagnostics"]["pre_seed_rows_superseded"]
    ] == ["R-196170-naive"]


# ---------------------------------------------------------------------------
# re-seed: the latest seed generation governs (Q-88 desk scenario)
# ---------------------------------------------------------------------------
def test_reseeded_symbol_supersedes_the_older_seed_and_its_history() -> None:
    """196170 re-seeded at the current broker quantity (6) on 10-01: the new
    seed row is a second manual_import generation (new SEED- id, new
    filled_at). Everything the old seed covered — the old seed row itself and
    all pre-cutover reconciler rows — is superseded."""
    fills = [
        _seed("196170", "4"),  # first generation (05-10)
        _row(1, "buy", "3", "2026-02-23", "196170"),
        _row(2, "buy", "1", "2026-04-15", "196170"),
        _row(3, "buy", "1", "2026-05-18", "196170"),  # <= new cutover: superseded
        _seed("196170", "6", cutover=RECUTOVER),  # second generation
    ]
    out = _block(DESK_CASES[10], fills=fills)
    diag = out["diagnostics"]
    assert diag["seed_cutover"] == RECUTOVER.isoformat()
    assert out["ledger_state"] == "known", out["unknown_reasons"]
    assert out["net_quantity"] == "6"
    assert [lot["origin"] for lot in out["lots"]] == ["opening_seed"]
    assert {r["broker_order_id"] for r in diag["pre_seed_rows_superseded"]} == {
        "SEED-20260510-kis-krx-196170",
        "R-196170-001",
        "R-196170-002",
        "R-196170-003",
    }


def test_reseeded_symbol_keeps_rows_after_the_latest_cutover() -> None:
    fills = [
        _seed("196170", "4"),
        _row(1, "buy", "3", "2026-02-23", "196170"),
        _row(2, "buy", "1", "2026-05-18", "196170"),
        _seed("196170", "6", cutover=RECUTOVER),
        _row(3, "sell", "2", "2026-10-05", "196170"),
    ]
    out = build_symbol_block(
        symbol="196170",
        reference_quantity=D("4"),
        current_price=None,
        fills=fills,
        orders=(),
        freshness=FRESH,
        now=NOW,
    )
    assert out["ledger_state"] == "known", out["unknown_reasons"]
    assert out["net_quantity"] == "4"  # seed 6 minus the post-cutover sell 2
    assert out["diagnostics"]["counted_row_count"] == 2


def test_reseed_at_the_same_date_updates_not_supersedes() -> None:
    """A re-seed at the same cutover upserts onto the same order id, so the
    ledger can only ever hold one row of that generation."""
    fills = [
        _seed("196170", "4"),
        _row(1, "buy", "3", "2026-02-23", "196170"),
        _row(2, "buy", "1", "2026-05-18", "196170"),
    ]
    out = _block(DESK_CASES[10], fills=fills)
    assert out["diagnostics"]["seed_cutover"] == CUTOVER_ISO
    assert out["diagnostics"]["counted_row_count"] == 2


# ---------------------------------------------------------------------------
# seed detection: only a real seed row governs a cutover
# ---------------------------------------------------------------------------
def test_manual_import_row_without_seed_prefix_does_not_govern() -> None:
    """A non-SEED manual_import row is an authoritative fill, not a seed: it
    must not silently define a cutover. (All seeds today come from the CLI's
    SEED-<date>- order id — opening_lots._seed_order_id.)"""
    fills = [
        LedgerFill(
            id=1,
            source="manual_import",
            side="buy",
            quantity=D("4"),
            price=D("50000"),
            filled_at=CUTOVER,
            broker_order_id="MANUAL-FIX-1",
        ),
        _row(2, "buy", "3", "2026-02-23", "196170"),
    ]
    out = _block(DESK_CASES[10], fills=fills)
    diag = out["diagnostics"]
    assert diag["seed_cutover"] is None
    assert diag["pre_seed_rows_superseded"] == []
    # nothing was superseded: the 4+3 ledger net stays 7, mismatch vs broker 6
    assert out["ledger_state"] == "unknown"
    assert diag["ledger_net_quantity"] == "7"
    assert diag["counted_row_count"] == 2


def test_post_cutover_oversold_history_still_fails_closed() -> None:
    """Superseding pre-seed rows must not launder a real post-cutover gap:
    selling more than the post-seed book stays oversold_history_gap."""
    fills = [
        _seed("196170", "4"),
        _row(1, "buy", "3", "2026-02-23", "196170"),  # superseded
        _row(2, "sell", "10", "2026-05-20", "196170"),  # oversells the seed
    ]
    out = _block(DESK_CASES[10], fills=fills)
    assert out["ledger_state"] == "unknown"
    assert "oversold_history_gap" in out["unknown_reasons"]
    assert out["diagnostics"]["oversold_quantity"] == "6"


def test_symbol_with_only_a_seed_row_is_known_at_seed_qty() -> None:
    fills = [_seed("196170", "6")]
    out = _block(DESK_CASES[10], fills=fills)
    assert out["ledger_state"] == "known"
    assert out["net_quantity"] == "6"
    assert out["lots"][0]["origin"] == "opening_seed"
