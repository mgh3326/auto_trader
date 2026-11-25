# tests/test_forecast_resolve_batch.py
"""hk 6377 — forecast_resolve N+1 removal: batched resolve identity + statements.

DB-free. A recording fake ``AsyncSession`` serves the real
``DailyCandlesRepository`` / universe-lookup code paths with canned rows, so
statement counts below are real ``session.execute`` calls through the
production queries — not stubbed repository methods.

- Identity: the per-row ``resolve_forecast`` loop (before) and
  ``resolve_forecasts_batch`` (after) return identical outputs for the same
  fake rows.
- Statements: resolving 3 vs 30 due forecasts issues a constant number of
  statements; the batch path never re-selects ``trade_forecasts`` (the two
  listing queries already hold the rows).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.review import TradeForecast
from app.models.trading import InstrumentType
from app.services.trade_journal import forecast_service as svc

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 8, 1, tzinfo=UTC)
_REVIEW = date(2026, 7, 28)
_START = date(2026, 7, 20)

_PRICE_TOUCH_RULE = "window-touch-v1-high-gte-low-lte"
_TERMINAL_RULE = "terminal-close-v1-up-gte-down-lt"


def _candle(
    day: date,
    *,
    symbol: str,
    partition: str,
    high: float,
    low: float,
    close: float,
    adj_close: float | None = None,
    iid: int | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "time": datetime(day.year, day.month, day.day, tzinfo=UTC),
        "symbol": symbol,
        "partition": partition,
        "open": (high + low) / 2,
        "high": high,
        "low": low,
        "close": close,
        "adj_close": adj_close,
        "volume": 1000.0,
        "value": 100.0,
        "source": "kis",
    }
    if iid is not None:
        row["instrument_id"] = iid
    return row


def _window_rows(
    symbol: str,
    partition: str,
    *,
    days: list[date],
    high: float,
    low: float,
    close: float,
) -> list[dict[str, Any]]:
    return [
        _candle(d, symbol=symbol, partition=partition, high=high, low=low, close=close)
        for d in days
    ]


def _days(start: date, end: date) -> list[date]:
    out: list[date] = []
    day = start
    while day <= end:
        out.append(day)
        day += timedelta(days=1)
    return out


_ALL_DAYS = _days(_START, _REVIEW)


class _FakeResult:
    """Minimal asyncpg/SQLAlchemy result shape the production code consumes."""

    def __init__(
        self,
        *,
        mappings_rows: list[dict[str, Any]] | None = None,
        all_rows: Any = None,
        scalar: Any = None,
        iter_rows: list[Any] | None = None,
    ) -> None:
        self._mappings_rows = mappings_rows or []
        self._all_rows = all_rows
        self._scalar = scalar
        self._iter_rows = iter_rows or []

    def mappings(self) -> _FakeResult:
        return self

    def all(self) -> Any:
        if self._all_rows is not None:
            return self._all_rows
        return self._mappings_rows

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def __iter__(self):  # crypto instrument-id lookup iterates the result
        return iter(self._iter_rows)


class _FakeSession:
    """Recording stand-in for ``AsyncSession`` serving canned store rows."""

    def __init__(
        self,
        *,
        candles: dict[str, list[dict[str, Any]]],
        us_exchanges: dict[str, str],
        forecasts: dict[str, TradeForecast] | None = None,
    ) -> None:
        self.candles = candles
        self.us_exchanges = us_exchanges
        self.forecasts = forecasts or {}
        self.statements: list[str] = []
        self.flushes = 0
        self.refreshes = 0
        self._crypto_ids: dict[int, str] = {}

    def _label(self, text: str) -> str:
        for table in (
            "trade_forecasts",
            "us_symbol_universe",
            "kr_candles_1d",
            "us_candles_1d",
            "crypto_candles_1d",
            "crypto_instruments",
        ):
            if table in text:
                return table
        raise AssertionError(f"unexpected statement: {text[:200]}")

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        text = str(stmt)
        self.statements.append(self._label(text))
        params = dict(params or {})

        if "trade_forecasts" in text:
            bound = stmt.compile().params  # type: ignore[union-attr]
            for value in bound.values():
                row = self.forecasts.get(str(value))
                if row is not None:
                    return _FakeResult(scalar=row)
            return _FakeResult(scalar=None)

        if "us_symbol_universe" in text:
            if "POSTCOMPILE" in text:
                # get_us_exchanges_by_symbols: one IN query; unknown symbols
                # are simply absent, exactly like the real query.
                return _FakeResult(
                    all_rows=[(s, e) for s, e in self.us_exchanges.items()]
                )
            bound = stmt.compile().params  # type: ignore[union-attr]
            for value in bound.values():
                exchange = self.us_exchanges.get(str(value))
                if exchange is not None:
                    return _FakeResult(
                        scalar=SimpleNamespace(exchange=exchange, is_active=True)
                    )
            return _FakeResult(scalar=None)

        if "crypto_instruments" in text and "venue_symbol" in text:
            iter_rows = []
            for i, symbol in enumerate(params.get("symbols", [])):
                norm = str(symbol).strip().upper()
                iid = 1000 + i
                self._crypto_ids[iid] = norm
                if norm in self.candles:
                    iter_rows.append(SimpleNamespace(venue_symbol=norm, id=iid))
            return _FakeResult(iter_rows=iter_rows)

        if "crypto_candles_1d" in text:
            start, end = params["start"], params["end"]
            iids = params.get("instrument_ids")
            if iids is None:  # single-row fetch_range uses :iid
                iids = [params["iid"]]
            out_rows: list[dict[str, Any]] = []
            for iid in iids:
                symbol = self._crypto_ids.get(iid)
                if symbol is None:
                    continue
                for row in self.candles.get(symbol, []):
                    if start <= row["time"] <= end:
                        out_rows.append({**row, "instrument_id": iid})
            out_rows.sort(key=lambda r: (r["instrument_id"], r["time"]))
            return _FakeResult(mappings_rows=out_rows)

        # kr_candles_1d / us_candles_1d, single (=) or batched (IN) shape.
        symbols = params.get("symbols") or (
            [params["symbol"]] if "symbol" in params else []
        )
        start, end = params["start"], params["end"]
        out_rows = []
        for symbol in symbols:
            for row in self.candles.get(symbol, []):
                if start <= row["time"] <= end:
                    out_rows.append(row)
        out_rows.sort(key=lambda r: (r["symbol"], r["time"]))
        return _FakeResult(mappings_rows=out_rows)

    async def flush(self) -> None:
        self.flushes += 1

    async def refresh(self, _row: Any) -> None:
        self.refreshes += 1

    async def commit(self) -> None:
        return None


def _make_row(
    symbol: str,
    instrument: InstrumentType,
    target: dict[str, Any],
    *,
    review_date: date = _REVIEW,
    start_date: date | None = _START,
    status: str = "open",
) -> TradeForecast:
    return TradeForecast(
        forecast_id=uuid.uuid4(),
        symbol=symbol,
        instrument_type=instrument,
        forecast_target=target,
        probability=Decimal("0.6"),
        review_date=review_date,
        forecast_start_date=start_date,
        created_by="test",
        created_at=datetime(2026, 7, 19, tzinfo=UTC),
        status=status,
    )


def _clone_row(row: TradeForecast) -> TradeForecast:
    """A pristine twin of ``row``: same identity and inputs, unmutated."""
    return TradeForecast(
        forecast_id=row.forecast_id,
        symbol=row.symbol,
        instrument_type=row.instrument_type,
        forecast_target=dict(row.forecast_target),
        probability=Decimal(str(row.probability)),
        review_date=row.review_date,
        forecast_start_date=row.forecast_start_date,
        created_by=row.created_by,
        created_at=row.created_at,
        status=row.status,
    )


def _price_target(
    direction: str = "at_or_above", price: float = 70000.0
) -> dict[str, Any]:
    return {
        "kind": "price_target",
        "outcome_rule_version": _PRICE_TOUCH_RULE,
        "direction": direction,
        "target_price": price,
    }


def _terminal_close(direction: str = "up", price: float = 100.0) -> dict[str, Any]:
    return {
        "kind": svc._TERMINAL_CLOSE_KIND,
        "outcome_rule_version": _TERMINAL_RULE,
        "direction": direction,
        "target_price": price,
    }


def _fixture_rows() -> list[TradeForecast]:
    return [
        # 0: KR price_target hit.
        _make_row("005930", InstrumentType.equity_kr, _price_target(price=70000.0)),
        # 1: KR price_target miss.
        _make_row("000660", InstrumentType.equity_kr, _price_target(price=200000.0)),
        # 2: US price_target hit.
        _make_row("AAPL", InstrumentType.equity_us, _price_target(price=150.0)),
        # 3: KR terminal_close up hit.
        _make_row("035720", InstrumentType.equity_kr, _terminal_close("up", 100.0)),
        # 4: crypto terminal_close down hit.
        _make_row("KRW-BTC", InstrumentType.crypto, _terminal_close("down", 90000.0)),
        # 5: placeholder with no resolvable claim.
        _make_row(
            "005930",
            InstrumentType.equity_kr,
            {"kind": "no_resolvable_forecast"},
        ),
        # 6: legacy versionless touch -> quarantined.
        _make_row(
            "005930",
            InstrumentType.equity_kr,
            {"kind": "price_target", "direction": "at_or_above", "target_price": 1.0},
        ),
        # 7: forex price_target -> requires_manual (no candle store).
        _make_row("USDJPY", InstrumentType.forex, _price_target(price=150.0)),
        # 8: KR price_target with no loaded candles.
        _make_row("999999", InstrumentType.equity_kr, _price_target(price=10.0)),
        # 9: already closed.
        _make_row(
            "005930",
            InstrumentType.equity_kr,
            _price_target(price=70000.0),
            status="closed",
        ),
    ]


def _fixture_candles() -> dict[str, list[dict[str, Any]]]:
    candles: dict[str, list[dict[str, Any]]] = {
        "005930": _window_rows(
            "005930", "KRX", days=_ALL_DAYS, high=72000.0, low=69000.0, close=71000.0
        ),
        "000660": _window_rows(
            "000660", "KRX", days=_ALL_DAYS, high=150000.0, low=140000.0, close=145000.0
        ),
        "AAPL": [
            _candle(
                d,
                symbol="AAPL",
                partition="XNYS",
                high=160.0,
                low=150.0,
                close=155.0,
                adj_close=155.0,
            )
            for d in _ALL_DAYS
        ],
        "035720": _window_rows(
            "035720", "KRX", days=_ALL_DAYS, high=120.0, low=95.0, close=110.0
        ),
        "KRW-BTC": _window_rows(
            "KRW-BTC",
            "upbit_krw",
            days=_ALL_DAYS,
            high=95000.0,
            low=85000.0,
            close=88000.0,
        ),
    }
    return candles


def _session(
    rows: list[TradeForecast] | None = None,
    candles: dict[str, list[dict[str, Any]]] | None = None,
) -> _FakeSession:
    rows = rows if rows is not None else _fixture_rows()
    return _FakeSession(
        candles=candles if candles is not None else _fixture_candles(),
        us_exchanges={"AAPL": "XNYS"},
        forecasts={str(r.forecast_id): r for r in rows},
    )


async def _sequential(
    db: _FakeSession, rows: list[TradeForecast], *, persist: bool
) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        out.append(
            await svc.resolve_forecast(
                db,  # type: ignore[arg-type]
                forecast_id=row.forecast_id,
                persist=persist,
                backfill_missing=False,
                now=_NOW,
            )
        )
    return out


@pytest.mark.asyncio
async def test_batch_matches_sequential_preview():
    rows = _fixture_rows()
    seq_db = _session(rows)
    seq = await _sequential(seq_db, rows, persist=False)

    batch_db = _session(rows)
    batched = await svc.resolve_forecasts_batch(
        batch_db,  # type: ignore[arg-type]
        rows,
        persist=False,
        backfill_missing=False,
        now=_NOW,
    )
    assert batched == seq
    statuses = [r["status"] for r in batched]
    assert statuses == [
        "previewed",  # KR touch hit
        "previewed",  # KR touch miss
        "previewed",  # US touch hit
        "previewed",  # KR terminal up hit
        "previewed",  # crypto terminal down hit
        "would_close_no_claim",
        "quarantined_legacy_price_target",
        "requires_manual",
        "unresolved_no_data",
        "already_closed",
    ]
    # A hit and a miss must not collapse to the same outcome.
    assert batched[0]["computed"]["outcome"] is True
    assert batched[1]["computed"]["outcome"] is False
    assert batched[0]["computed"]["brier_score"] == pytest.approx(0.16)
    assert batched[2]["computed"]["outcome"] is True
    assert batched[4]["computed"]["outcome"] is True


@pytest.mark.asyncio
async def test_batch_matches_sequential_persist():
    rows = _fixture_rows()
    rows2 = [_clone_row(r) for r in rows]
    seq_db = _session(rows)
    batch_db = _session(rows2)
    seq = await _sequential(seq_db, rows, persist=True)

    batched = await svc.resolve_forecasts_batch(
        batch_db,  # type: ignore[arg-type]
        rows2,
        persist=True,
        backfill_missing=False,
        now=_NOW,
    )
    assert batched == seq
    # Five scored rows plus the no-claim auto-close persist.
    assert seq_db.flushes == batch_db.flushes == 6
    assert [r.status for r in rows2] == [r.status for r in rows]


def _kr_rows(n: int) -> tuple[list[TradeForecast], dict[str, list[dict[str, Any]]]]:
    rows = [
        _make_row(f"{100000 + i}", InstrumentType.equity_kr, _price_target(price=10.0))
        for i in range(n)
    ]
    candles = {
        r.symbol: _window_rows(
            r.symbol, "KRX", days=_ALL_DAYS, high=20.0, low=15.0, close=18.0
        )
        for r in rows
    }
    return rows, candles


@pytest.mark.asyncio
async def test_statement_count_constant_for_kr():
    seen: list[list[str]] = []
    for n in (3, 30):
        rows, candles = _kr_rows(n)
        db = _FakeSession(candles=candles, us_exchanges={})
        await svc.resolve_forecasts_batch(
            db,  # type: ignore[arg-type]
            rows,
            persist=False,
            backfill_missing=False,
            now=_NOW,
        )
        # Forecasts come from the listing queries (zero re-fetch here) and
        # KR candles load in one query for the whole symbol set.
        assert db.statements == ["kr_candles_1d"]
        seen.append(list(db.statements))
    assert seen[0] == seen[1]

    rows30, candles30 = _kr_rows(30)
    seq_db = _FakeSession(candles=candles30, us_exchanges={})
    for row in rows30:
        await svc._resolve_open_row(
            seq_db,  # type: ignore[arg-type]
            row,
            persist=False,
            resolved_now=_NOW,
            load_candles=await _single_loader(seq_db, row),
        )
    assert seq_db.statements == ["kr_candles_1d"] * 30


async def _single_loader(db: _FakeSession, row: TradeForecast):
    instrument = svc._row_instrument(row)

    async def _load(kind: str, start_date: date, review_date: date):
        return await svc._load_candles_single(
            db,  # type: ignore[arg-type]
            symbol=row.symbol,
            instrument_type=instrument,
            kind=kind,
            start_date=start_date,
            review_date=review_date,
            backfill_missing=False,
        )

    return _load


def _mixed_rows(
    n_each: int,
) -> tuple[list[TradeForecast], dict[str, list[dict[str, Any]]], dict[str, str]]:
    rows: list[TradeForecast] = []
    candles: dict[str, list[dict[str, Any]]] = {}
    us_exchanges: dict[str, str] = {}
    for i in range(n_each):
        kr = _make_row(
            f"{200000 + i}", InstrumentType.equity_kr, _price_target(price=10.0)
        )
        us = _make_row(f"SYM{i}", InstrumentType.equity_us, _price_target(price=10.0))
        co = _make_row(
            f"KRW-T{i:03d}", InstrumentType.crypto, _price_target(price=10.0)
        )
        rows.extend([kr, us, co])
        candles[kr.symbol] = _window_rows(
            kr.symbol, "KRX", days=_ALL_DAYS, high=20.0, low=15.0, close=18.0
        )
        candles[us.symbol] = [
            _candle(
                d,
                symbol=us.symbol,
                partition="XNYS",
                high=20.0,
                low=15.0,
                close=18.0,
                adj_close=18.0,
            )
            for d in _ALL_DAYS
        ]
        candles[co.symbol.strip().upper()] = _window_rows(
            co.symbol, "upbit_krw", days=_ALL_DAYS, high=20.0, low=15.0, close=18.0
        )
        us_exchanges[us.symbol] = "XNYS"
    return rows, candles, us_exchanges


@pytest.mark.asyncio
async def test_statement_count_constant_for_mixed_markets():
    first: list[str] | None = None
    for n_each in (1, 10):
        rows, candles, us_exchanges = _mixed_rows(n_each)
        db = _FakeSession(candles=candles, us_exchanges=us_exchanges)
        await svc.resolve_forecasts_batch(
            db,  # type: ignore[arg-type]
            rows,
            persist=False,
            backfill_missing=False,
            now=_NOW,
        )
        # One US partition lookup + one candle query per (market, partition)
        # group (crypto needs its instrument-id lookup): constant in N.
        assert sorted(db.statements) == sorted(
            [
                "us_symbol_universe",
                "kr_candles_1d",
                "us_candles_1d",
                "crypto_instruments",
                "crypto_candles_1d",
            ]
        )
        assert "trade_forecasts" not in db.statements
        if first is None:
            first = sorted(db.statements)
        else:
            assert sorted(db.statements) == first
