"""Acceptance 3: a delisted symbol never appears after its delist date."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pyarrow as pa
import pyarrow.compute as pc
import pytest

from research.kr_delist_repair.export import UNRESOLVED, build_panel, validate_panel
from research.kr_delist_repair.model import ExportConflict
from research.kr_delist_repair.resolve import Resolution

SESSIONS = ["2020-03-02", "2020-03-03", "2020-03-04", "2020-03-05", "2020-03-06"]
ALIVE, DELISTED, UNRES, LATER = "000010", "000020", "000030", "000040"
LAST = {ALIVE: 4, DELISTED: 2, UNRES: 1, LATER: 4}


def _membership(extra: list[tuple[str, str]] = ()) -> pa.Table:  # type: ignore[assignment]
    rows = [(s, sym) for sym, last in LAST.items() for s in SESSIONS[: last + 1]]
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


def _res(symbol: str, delist: str, *, resolved: bool) -> Resolution:
    return Resolution(
        symbol=symbol,
        name=symbol,
        markets="KOSDAQ",
        in_bar_frame=True,
        corpus_first_session=SESSIONS[0],
        corpus_last_session=SESSIONS[LAST[symbol]],
        alive_at_main_end=LAST[symbol] == 4,
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
]


def _panel() -> pa.Table:
    m = _membership()
    return build_panel(m, _bars(m), _no_trade(), RESOLUTIONS)


def _rows(panel: pa.Table, symbol: str) -> list[dict[str, object]]:
    return [r for r in panel.to_pylist() if r["symbol"] == symbol]


def test_delisted_symbol_has_no_row_on_or_after_its_delist_date() -> None:
    panel = _panel()
    for res in RESOLUTIONS:
        if not res.delist_date:
            continue
        cut = date.fromisoformat(res.delist_date)
        assert all(r["session"] < cut for r in _rows(panel, res.symbol))
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
        build_panel(m, _bars(m), _no_trade(), RESOLUTIONS)


def test_validator_rejects_a_forward_filled_row_after_delisting() -> None:
    panel = _panel()
    extra = dict.fromkeys(panel.column_names)
    extra.update(
        symbol=DELISTED,
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
