"""ROB-1487: batched ``us_symbol_universe`` exchange resolution.

The per-symbol resolver issued one single-row SELECT per symbol and, when
called without a session (e.g. from the KIS quote arm inside a concurrency-5
fan-out), also opened its own ``AsyncSessionLocal``. These tests pin the batched
path that replaces it: one query for N symbols, zero sessions on a cache hit,
and unchanged per-symbol results/errors on a miss.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.models.us_symbol_universe import USSymbolUniverse
from app.services import us_symbol_universe_service as universe


class _Rows:
    """Minimal ``Result`` for the batched (two-column) statement."""

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, str]]:
        return list(self._rows)


class _Row:
    """Minimal ``Result`` for the single-row (entity) statement."""

    def __init__(self, row: USSymbolUniverse | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> USSymbolUniverse | None:
        return self._row


class _BatchSession:
    """Records statements and answers the batched exchange lookup."""

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Rows:
        self.statements.append(statement)
        return _Rows([row for row in self.rows if row[0] in _in_list(statement)])


class _SingleSession:
    def __init__(self, row: USSymbolUniverse | None) -> None:
        self.row = row
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Row:
        self.statements.append(statement)
        return _Row(self.row)


def _in_list(statement: Any) -> set[str]:
    """Recover the ``IN (...)`` bind values from the compiled statement."""
    compiled = statement.compile()
    values: set[str] = set()
    for params in compiled.params.values():
        for value in params:
            values.add(str(value))
    return values


def _row(symbol: str, exchange: str, *, is_active: bool = True) -> USSymbolUniverse:
    return USSymbolUniverse(
        symbol=symbol,
        exchange=exchange,
        name_kr="",
        name_en="",
        is_active=is_active,
    )


@pytest.mark.asyncio
async def test_batch_lookup_issues_one_query_for_n_symbols() -> None:
    session = _BatchSession([("AAPL", "NASD"), ("BRK.B", "NYSE")])

    resolved = await universe.get_us_exchanges_by_symbols(
        ["AAPL", "BRK/B", "MSFT"], db=session
    )

    assert len(session.statements) == 1
    assert resolved == {"AAPL": "NASD", "BRK.B": "NYSE"}


@pytest.mark.asyncio
async def test_batch_lookup_canonicalizes_every_symbol_form() -> None:
    session = _BatchSession([("BRK.B", "NYSE")])

    resolved = await universe.get_us_exchanges_by_symbols(
        ["BRK/B", "brk-b", " BRK.B "], db=session
    )

    assert resolved == {"BRK.B": "NYSE"}
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_batch_lookup_with_no_symbols_queries_nothing() -> None:
    session = _BatchSession([])

    assert await universe.get_us_exchanges_by_symbols([], db=session) == {}
    assert await universe.get_us_exchanges_by_symbols(["", None], db=session) == {}
    assert session.statements == []


@pytest.mark.asyncio
async def test_prefetch_answers_every_symbol_without_opening_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm scope turns N per-symbol lookups into zero sessions and zero queries."""

    def _no_session() -> Any:  # pragma: no cover - must never run
        raise AssertionError("prefetch scope must not open a session on a hit")

    monkeypatch.setattr(universe, "AsyncSessionLocal", _no_session)
    prefetch_session = _BatchSession([("AAPL", "NASD"), ("MSFT", "NASD")])

    async with universe.us_exchange_prefetch(
        ["AAPL", "MSFT"], db=prefetch_session
    ) as resolved:
        assert resolved == {"AAPL": "NASD", "MSFT": "NASD"}
        assert len(prefetch_session.statements) == 1
        assert [
            await universe.get_us_exchange_by_symbol(sym) for sym in ("AAPL", "MSFT")
        ] == [
            "NASD",
            "NASD",
        ]

    assert len(prefetch_session.statements) == 1
    assert universe._us_exchange_cache.get() is None


@pytest.mark.asyncio
async def test_prefetch_result_matches_the_single_row_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batched answer is identical to the resolver it replaces."""

    single = _SingleSession(_row("AAPL", "NASD"))
    baseline = await universe.get_us_exchange_by_symbol("AAPL", db=single)

    prefetch_session = _BatchSession([("AAPL", "NASD")])
    async with universe.us_exchange_prefetch(["AAPL"], db=prefetch_session):
        batched = await universe.get_us_exchange_by_symbol("AAPL")

    assert batched == baseline == "NASD"


@pytest.mark.asyncio
async def test_symbol_missing_from_the_prefetch_still_raises_the_same_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A miss is not silently defaulted — it falls through to the strict resolver."""

    async def _universe_is_empty(*_args: Any, **_kwargs: Any) -> bool:
        return False

    missing_session = _SingleSession(None)

    class _EmptyUniverseSession:
        async def __aenter__(self) -> _SingleSession:
            return missing_session

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(universe, "AsyncSessionLocal", _EmptyUniverseSession)
    monkeypatch.setattr(universe, "has_any_rows", _universe_is_empty)

    async with universe.us_exchange_prefetch(["AAPL"], db=_BatchSession([])):
        with pytest.raises(universe.USSymbolUniverseEmptyError):
            await universe.get_us_exchange_by_symbol("NVDA")

    assert len(missing_session.statements) == 1


def test_public_surface_exports_the_batched_entry_points() -> None:
    assert "get_us_exchanges_by_symbols" in universe.__all__
    assert "us_exchange_prefetch" in universe.__all__
