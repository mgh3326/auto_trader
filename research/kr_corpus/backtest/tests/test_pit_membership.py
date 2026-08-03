"""PIT membership universe — no is_active, no future rows, delist explicit."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import membership as memb
from terminal_events import force_exit_delisted_holdings


def _row(
    symbol: str,
    session: str,
    *,
    member: bool = True,
    status: str = "listed",
    market: str = "KOSPI",
) -> memb.MembershipRow:
    return memb.MembershipRow(
        symbol=symbol,
        session_date=date.fromisoformat(session),
        market=market,
        member=member,
        status=status,  # type: ignore[arg-type]
    )


def test_universe_uses_only_membership_le_session():
    rows = [
        _row("A", "2023-01-02"),
        _row("B", "2023-01-03"),  # future relative to 01-02
        _row("C", "2023-01-02", member=False),
    ]
    snap = memb.universe_at(rows, "2023-01-02")
    assert snap.symbols == frozenset({"A"})
    assert "B" not in snap.symbols


def test_delisted_excluded_and_surfaced():
    rows = [
        _row("A", "2023-01-02"),
        _row("A", "2023-01-10", member=False, status="delisted"),
        _row("B", "2023-01-02"),
    ]
    snap = memb.universe_at(rows, "2023-01-10")
    assert "A" not in snap.symbols
    assert "A" in snap.delisted_as_of
    assert "B" in snap.symbols


def test_force_exit_delisted_is_explicit_not_silent():
    held = {"A", "B"}
    residual, events = force_exit_delisted_holdings(
        session_date=date(2023, 2, 1),
        held_symbols=held,
        delisted_as_of=frozenset({"A"}),
        last_close_by_symbol={"A": 10.0},
    )
    assert residual == {"B"}
    assert len(events) == 1
    assert events[0].reason == "delisted"
    assert events[0].symbol == "A"
    assert events[0].last_close == 10.0


def test_no_db_universe_flag_tokens_in_package_source():
    """Static: package must not reference live DB universe flag tokens."""
    pkg = Path(__file__).resolve().parent.parent
    forbidden = ("is_active", "kr_symbol_universe")
    offenders: list[str] = []
    for path in pkg.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path}: {token}")
    assert offenders == []
