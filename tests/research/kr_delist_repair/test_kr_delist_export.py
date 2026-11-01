"""Acceptance 3: a delisted symbol never appears after its delist date."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pyarrow as pa
import pyarrow.compute as pc
import pytest

from research.kr_delist_repair.causal import (
    PANEL_GATES,
    CausalTable,
    bar_available_at,
)
from research.kr_delist_repair.cli import presences
from research.kr_delist_repair.export import UNRESOLVED, build_panel, validate_panel
from research.kr_delist_repair.model import AvailabilityViolation, ExportConflict
from research.kr_delist_repair.resolve import Resolution

SESSIONS = ["2020-03-02", "2020-03-03", "2020-03-04", "2020-03-05", "2020-03-06"]
ALIVE, DELISTED, UNRES, LATER, REUSED = "000010", "000020", "000030", "000040", "000050"
LAST = {ALIVE: 4, DELISTED: 2, UNRES: 1, LATER: 4}
# 000050 trades 03-02..03-03, is delisted, and the code returns on 03-05.
REUSED_SESSIONS = [SESSIONS[0], SESSIONS[1], SESSIONS[3], SESSIONS[4]]


def _membership(extra: list[tuple[str, str]] = ()) -> pa.Table:  # type: ignore[assignment]
    rows = [(s, sym) for sym, last in LAST.items() for s in SESSIONS[: last + 1]]
    rows += [(s, REUSED) for s in REUSED_SESSIONS]
    rows += list(extra)
    return pa.table(
        {
            "session": [s for s, _ in rows],
            "market": ["KOSDAQ"] * len(rows),
            "ticker": [t for _, t in rows],
        }
    )


def _bars(membership: pa.Table) -> pa.Table:
    keep = pc.invert(
        pc.and_(
            pc.equal(membership.column("ticker"), DELISTED),
            pc.equal(membership.column("session"), "2020-03-04"),
        )
    )
    m = membership.filter(keep)
    n = m.num_rows
    return (
        m.append_column("open", pa.array([10] * n, pa.int64()))
        .append_column("high", pa.array([12] * n, pa.int64()))
        .append_column("low", pa.array([9] * n, pa.int64()))
        .append_column("close", pa.array([11] * n, pa.int64()))
        .append_column("volume", pa.array([5] * n, pa.int64()))
    )


def _no_trade() -> pa.Table:
    return pa.table(
        {
            "session": ["2020-03-04"],
            "market": ["KOSDAQ"],
            "ticker": [DELISTED],
            "close": pa.array([7], pa.int64()),
        }
    )


def _res(symbol: str, delist: str, *, resolved: bool, segment: int = 0) -> Resolution:
    return Resolution(
        listing_id=f"{symbol}#{segment}",
        symbol=symbol,
        segment=segment,
        name=symbol,
        markets="KOSDAQ",
        in_bar_frame=True,
        corpus_first_session=SESSIONS[0],
        corpus_last_session=SESSIONS[LAST.get(symbol, 1)],
        alive_at_main_end=LAST.get(symbol, 1) == 4,
        listing_date="",
        listing_date_basis="listed_before_corpus_start",
        delist_date=delist,
        terminal_outcome="statutory_cause" if resolved else "",
        payout_semantics="residual_after_liquidation_trading" if resolved else "",
        information_available_at=(
            datetime.fromisoformat(f"{delist}T00:00:00+09:00").isoformat()
            if delist
            else ""
        ),
        resolved=resolved,
    )


RESOLUTIONS = [
    _res(DELISTED, "2020-03-05", resolved=True),
    _res(UNRES, "", resolved=False),
    _res(LATER, "2025-06-02", resolved=True),
    _res(REUSED, "2020-03-04", resolved=True),
]


def _build(m: pa.Table, resolutions: list[Resolution] = RESOLUTIONS) -> pa.Table:
    pres, _ = presences(m, set())
    return build_panel(m, _bars(m), _no_trade(), resolutions, pres)


def _panel() -> pa.Table:
    return _build(_membership())


def _rows(panel: pa.Table, symbol: str) -> list[dict[str, object]]:
    return [r for r in panel.to_pylist() if r["symbol"] == symbol]


def test_delisted_symbol_has_no_row_on_or_after_its_delist_date() -> None:
    panel = _panel()
    for res in RESOLUTIONS:
        if not res.delist_date:
            continue
        cut = date.fromisoformat(res.delist_date)
        listing = [r for r in panel.to_pylist() if r["listing_id"] == res.listing_id]
        assert all(r["session"] < cut for r in listing)
    assert [r["session"] for r in _rows(panel, DELISTED)][-1] == date(2020, 3, 4)


def test_terminal_flag_is_on_the_last_row_only_and_not_forward_filled() -> None:
    rows = _rows(_panel(), DELISTED)
    flagged = [r for r in rows if r["is_terminal_session"]]
    assert len(flagged) == 1 and flagged[0] is rows[-1]
    assert flagged[0]["terminal_outcome"] == "statutory_cause"
    assert flagged[0]["delist_date"] == date(2020, 3, 5)
    assert flagged[0]["terminal_available_at"] == datetime(2020, 3, 4, 15, tzinfo=UTC)
    assert all(r["terminal_outcome"] is None for r in rows[:-1])


def test_last_row_before_delisting_keeps_its_no_trade_state() -> None:
    last = _rows(_panel(), DELISTED)[-1]
    assert last["session_state"] == "no_trade"
    assert last["close"] == 7 and last["open"] is None


def test_unresolved_ending_is_flagged_not_dropped() -> None:
    rows = _rows(_panel(), UNRES)
    assert rows[-1]["terminal_outcome"] == UNRESOLVED
    assert rows[-1]["delist_date"] is None
    # Observable once the next session (2020-03-04) publishes without it.
    assert rows[-1]["terminal_available_at"] == datetime(2020, 3, 4, 7, tzinfo=UTC)


def test_unresolved_terminal_row_is_readable_once_its_ending_is_observable() -> None:
    table = CausalTable.for_panel(_panel())
    key = [UNRES, date(2020, 3, 3)]
    assert table.lookup(key, bar_available_at(date(2020, 3, 3)), ["close"]) == {
        "close": 11
    }
    with pytest.raises(AvailabilityViolation):
        table.lookup(key, bar_available_at(date(2020, 3, 3)), ["terminal_outcome"])
    seen = table.lookup(key, bar_available_at(date(2020, 3, 4)))
    assert seen["terminal_outcome"] == UNRESOLVED and seen["is_terminal_session"]


def test_same_session_in_two_markets_is_not_a_gap() -> None:
    m = _membership()
    both = pa.table({"session": [SESSIONS[2]], "market": ["KOSPI"], "ticker": [ALIVE]})
    pres, _ = presences(pa.concat_tables([m, both]), set())
    alive = [p for p in pres.values() if p.symbol == ALIVE]
    assert [(p.listing_id, p.markets) for p in alive] == [
        ("000010#0", ("KOSDAQ", "KOSPI"))
    ]


def test_symbols_alive_at_scope_end_carry_no_terminal_row() -> None:
    panel = _panel()
    for sym in (ALIVE, LATER):
        assert not any(r["is_terminal_session"] for r in _rows(panel, sym))


def test_every_row_has_available_at_at_16_kst() -> None:
    panel = _panel()
    assert panel.column("available_at").null_count == 0
    first = panel.to_pylist()[0]
    assert first["available_at"] == datetime(2020, 3, 2, 7, tzinfo=UTC)


def test_build_refuses_corpus_membership_on_the_delist_date() -> None:
    m = _membership(extra=[("2020-03-05", DELISTED)])
    with pytest.raises(ExportConflict, match="delist date"):
        _build(m)


def test_validator_rejects_a_forward_filled_row_after_delisting() -> None:
    panel = _panel()
    extra = dict.fromkeys(panel.column_names)
    extra.update(
        symbol=DELISTED,
        listing_id=f"{DELISTED}#0",
        market="KOSDAQ",
        session=date(2020, 3, 6),
        session_state="valid",
        close=7,
        available_at=datetime(2020, 3, 6, 7, tzinfo=UTC),
        is_terminal_session=False,
    )
    tampered = pa.concat_tables(
        [panel, pa.Table.from_pylist([extra], schema=panel.schema)]
    )
    with pytest.raises(ExportConflict, match="on or after its delist date"):
        validate_panel(tampered, RESOLUTIONS)


def test_validator_rejects_terminal_outcome_copied_to_earlier_rows() -> None:
    rows = _panel().to_pylist()
    for r in rows:
        if r["symbol"] == DELISTED:
            r["terminal_outcome"] = "statutory_cause"
    tampered = pa.Table.from_pylist(rows, schema=_panel().schema)
    with pytest.raises(ExportConflict, match="non-terminal row"):
        validate_panel(tampered, RESOLUTIONS)


def test_reused_code_is_two_listings_each_with_its_own_terminal_handling() -> None:
    rows = _rows(_panel(), REUSED)
    assert [r["listing_id"] for r in rows] == [
        "000050#0",
        "000050#0",
        "000050#1",
        "000050#1",
    ]
    first = [r for r in rows if r["listing_id"] == "000050#0"]
    assert first[-1]["is_terminal_session"] and first[-1]["delist_date"] == date(
        2020, 3, 4
    )
    assert not any(
        r["is_terminal_session"] for r in rows if r["listing_id"] == "000050#1"
    )


def test_validator_rejects_a_gap_inside_one_listing() -> None:
    rows = _panel().to_pylist()
    for r in rows:
        if r["symbol"] == REUSED:
            r["listing_id"] = "000050#0"
            r["is_terminal_session"] = False
            for c in (
                "terminal_outcome",
                "payout_semantics",
                "delist_date",
                "terminal_available_at",
            ):
                r[c] = None
    tampered = pa.Table.from_pylist(rows, schema=_panel().schema)
    with pytest.raises(ExportConflict, match="gap inside one listing"):
        validate_panel(tampered, [r for r in RESOLUTIONS if r.symbol != REUSED])


def test_validator_rejects_a_missing_terminal_row() -> None:
    rows = _panel().to_pylist()
    for r in rows:
        if r["symbol"] == DELISTED and r["is_terminal_session"]:
            r["is_terminal_session"] = False
            for c in (
                "terminal_outcome",
                "payout_semantics",
                "delist_date",
                "terminal_available_at",
            ):
                r[c] = None
    tampered = pa.Table.from_pylist(rows, schema=_panel().schema)
    with pytest.raises(ExportConflict, match="ends on"):
        validate_panel(tampered, RESOLUTIONS)


def test_validator_rejects_available_at_shifted_earlier() -> None:
    panel = _panel()
    shifted = pc.subtract(
        panel.column("available_at"), pa.scalar(86_400_000_000, pa.duration("us"))
    )
    tampered = panel.set_column(
        panel.schema.get_field_index("available_at"), "available_at", shifted
    )
    with pytest.raises(ExportConflict, match="16:00"):
        validate_panel(tampered, RESOLUTIONS)


def test_every_terminal_column_of_the_real_panel_is_gated() -> None:
    table = CausalTable.for_panel(_panel())
    key = [DELISTED, date(2020, 3, 4)]
    at_close = bar_available_at(date(2020, 3, 4))
    assert table.lookup(key, at_close, ["close"]) == {"close": 7}
    terminal_columns = (
        "is_terminal_session",
        "terminal_outcome",
        "payout_semantics",
        "delist_date",
        "terminal_available_at",
    )
    assert set(terminal_columns) <= set(PANEL_GATES)
    for col in terminal_columns:
        with pytest.raises(AvailabilityViolation):
            table.lookup(key, at_close, [col])
    known = datetime(2020, 3, 4, 15, tzinfo=UTC)
    assert table.lookup(key, known, ["is_terminal_session"]) == {
        "is_terminal_session": True
    }


def test_panel_without_gates_is_refused() -> None:
    with pytest.raises(ValueError, match="need their gate"):
        CausalTable(_panel(), ["symbol", "session"])
