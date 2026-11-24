"""hk 6148: futures minute collector (fake view and writer, fixtures use the
official sample field names)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.services.brokers.kis.futures_quotes import FuturesQuotesUnsupported
from app.services.kis_futures_quotes import contracts as c
from app.services.research_candles import futures_minutes_collector as fc

pytestmark = pytest.mark.unit

KST = ZoneInfo("Asia/Seoul")
TUE = date(2026, 11, 24)


def _master() -> list[dict[str, str]]:
    def row(ptype, short, name):
        return {
            "product_type": ptype,
            "short_code": short,
            "standard_code": "KR4X",
            "name": name,
            "atm": "",
            "strike": "00000.00",
            "month_type": "1",
            "underlying_code": "2001",
            "underlying_name": "KOSPI200",
        }

    return [
        row("B", "A05612", "미니F 202612"),
        row("B", "A05703", "미니F 202703"),
        row("1", "A01612", "F 202612"),
        row("1", "A01703", "F 202703"),
    ]


def _row(hhmmss: str, price: str = "400.10", vol: str = "5", day="20261124"):
    # field names of the official inquire-time-fuopchartprice output2
    return {
        "stck_bsop_date": day,
        "stck_cntg_hour": hhmmss,
        "futs_prpr": price,
        "futs_oprc": "400.00",
        "futs_hgpr": "400.20",
        "futs_lwpr": "399.90",
        "cntg_vol": vol,
        "acml_tr_pbmn": "123456789",
    }


def at(hhmm: str, second: int = 0) -> datetime:
    hour, minute = (int(p) for p in hhmm.split(":"))
    return datetime(2026, 11, 24, hour, minute, second, tzinfo=KST).astimezone(UTC)


class FakeView:
    def __init__(self, rows, master=None, night_error=None):
        self._rows = rows
        self._master = _master() if master is None else master
        self.calls: list[tuple] = []
        self._night_error = night_error or FuturesQuotesUnsupported("night")

    async def master(self):
        self.calls.append(("master",))
        return self._master

    async def minute_bars(self, symbol, *, session="day", day):
        self.calls.append(("minutes", symbol, session, day))
        if session == "night":
            raise self._night_error
        rows = self._rows[symbol] if isinstance(self._rows, dict) else self._rows
        if isinstance(rows, Exception):
            raise rows
        return rows


class MemoryWriter:
    """Insert-only semantics of ON CONFLICT (time_utc, symbol) DO NOTHING."""

    def __init__(self):
        self.rows: dict[tuple[datetime, str], tuple[fc.FuturesBar, datetime, str]] = {}

    async def insert(self, bars, *, batch_id, retrieved_at):
        new = 0
        for bar in bars:
            key = (bar.time_utc, bar.symbol)
            if key not in self.rows:
                self.rows[key] = (bar, retrieved_at, batch_id)
                new += 1
        return new, len(bars) - new


def contract(symbol="MF200.2612") -> c.ResolvedContract:
    contracts, _ = c.resolve_front_next(_master(), TUE)
    return next(x for x in contracts if x.symbol == symbol)


def test_forming_bar_is_excluded_and_kst_labels_become_utc_starts() -> None:
    rows = [_row("100000"), _row("100100"), _row("100200")]
    # 10:02:05 KST: the 10:01 bar is still inside the 10 s grace, 10:02 is forming
    result = fc.bars_from_rows(
        rows, contract=contract(), observed_before=at("10:02", 5)
    )
    assert [b.time_utc for b in result.bars] == [at("10:00")]
    assert result.skipped_forming == 2
    bar = result.bars[0]
    assert bar.time_utc == datetime(2026, 11, 24, 1, 0, tzinfo=UTC)  # 10:00 KST - 9h
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        Decimal("400.00"),
        Decimal("400.20"),
        Decimal("399.90"),
        Decimal("400.10"),
        Decimal("5"),
    )
    result = fc.bars_from_rows(
        rows, contract=contract(), observed_before=at("10:02", 10)
    )
    assert [b.time_utc for b in result.bars] == [at("10:00"), at("10:01")]


def test_bar_fields_segment_session_date_and_roll_state() -> None:
    (bar,) = fc.bars_from_rows(
        [_row("090000")], contract=contract(), observed_before=at("15:00")
    ).bars
    assert bar.symbol == "MF200.2612"
    assert bar.product == "MF200"
    assert bar.contract_month == "2612"
    assert bar.roll_state == "front"
    assert bar.session_segment == "FUT_DAY"
    assert bar.session_date == TUE
    assert bar.source == "KIS"
    (nxt,) = fc.bars_from_rows(
        [_row("090000")], contract=contract("MF200.2703"), observed_before=at("15:00")
    ).bars
    assert nxt.roll_state == "next"


def test_outside_the_session_window_is_stored_unknown_with_null_date() -> None:
    (bar,) = fc.bars_from_rows(
        [_row("084400")], contract=contract(), observed_before=at("15:00")
    ).bars
    assert bar.session_segment == "UNKNOWN"
    assert bar.session_date is None


def test_invalid_rows_are_counted_not_stored_and_duplicates_collapse() -> None:
    bad_price = _row("100000")
    bad_price["futs_prpr"] = ""
    rows = [
        bad_price,
        {"stck_bsop_date": "x", "stck_cntg_hour": "y"},
        _row("100100"),
        _row("100100"),
    ]
    result = fc.bars_from_rows(rows, contract=contract(), observed_before=at("11:00"))
    assert len(result.bars) == 1
    assert result.skipped_invalid == 2


def test_collect_reports_counts_and_night_unsupported() -> None:
    view = FakeView([_row("100000"), _row("100100"), _row("100200")])
    writer = MemoryWriter()
    clock_now = [at("10:02", 5)]

    report = asyncio.run(
        fc.collect(view=view, writer=writer, clock=lambda: clock_now[0], batch_id="b1")
    )
    assert report["products_resolved"] == 2
    assert report["contracts"] == [
        "F200.2612",
        "F200.2703",
        "MF200.2612",
        "MF200.2703",
    ]
    assert report["inserted"] == 4  # one closed bar per contract
    assert report["skipped_forming"] == 8
    assert report["already_present"] == 0
    assert report["night"] == "unsupported"
    assert report["errors"] == 0
    json.dumps(report, default=str)  # JSON-serialisable
    assert all(call[3] == TUE for call in view.calls if call[0] == "minutes")
    assert [call[2] for call in view.calls if call[0] == "minutes"].count("night") == 1


def test_rerun_inserts_zero_and_keeps_the_first_retrieved_at() -> None:
    view = FakeView([_row("100000")])
    writer = MemoryWriter()
    first = at("10:05")
    later = first + timedelta(minutes=5)
    asyncio.run(
        fc.collect(view=view, writer=writer, clock=lambda: first, batch_id="b1")
    )
    second = asyncio.run(
        fc.collect(view=view, writer=writer, clock=lambda: later, batch_id="b2")
    )
    assert second["inserted"] == 0
    assert second["already_present"] == 4
    assert {r[1] for r in writer.rows.values()} == {first}
    assert {r[2] for r in writer.rows.values()} == {"b1"}


def test_dry_run_writes_nothing() -> None:
    view = FakeView([_row("100000")])
    report = asyncio.run(
        fc.collect(view=view, writer=None, clock=lambda: at("10:05"), dry_run=True)
    )
    assert report["would_write"] == 4
    assert report["inserted"] == 0
    # smoke evidence: stored UTC start next to its KST label, per contract
    assert {x["symbol"] for x in report["samples"]} == {
        "F200.2612",
        "F200.2703",
        "MF200.2612",
        "MF200.2703",
    }
    first = report["samples"][0]
    assert first["kst"] == "2026-11-24 10:00"
    assert first["time_utc"] == "2026-11-24T01:00:00+00:00"
    assert (first["open"], first["volume"]) == ("400.00", "5")


def test_a_real_run_has_no_samples() -> None:
    report = asyncio.run(
        fc.collect(
            view=FakeView([_row("100000")]),
            writer=MemoryWriter(),
            clock=lambda: at("10:05"),
        )
    )
    assert report["samples"] is None


def test_one_contract_failure_is_an_error_and_does_not_stop_the_others() -> None:
    view = FakeView(
        {
            "A01612": RuntimeError("boom"),
            "A01703": [_row("100000")],
            "A05612": [_row("100000")],
            "A05703": [_row("100000")],
        }
    )
    writer = MemoryWriter()
    report = asyncio.run(
        fc.collect(view=view, writer=writer, clock=lambda: at("10:05"))
    )
    assert report["errors"] == 1
    assert report["error_details"] == [
        {"symbol": "F200.2612", "error_class": "RuntimeError"}
    ]
    assert report["inserted"] == 3


def test_master_failure_and_unresolved_products_are_errors() -> None:
    class BrokenMaster(FakeView):
        async def master(self):
            raise ValueError("bad master")

    report = asyncio.run(
        fc.collect(
            view=BrokenMaster([]), writer=MemoryWriter(), clock=lambda: at("10:05")
        )
    )
    assert report["errors"] == 1 and report["products_resolved"] == 0

    only_regular = [r for r in _master() if r["product_type"] == "1"]
    report = asyncio.run(
        fc.collect(
            view=FakeView([_row("100000")], master=only_regular),
            writer=MemoryWriter(),
            clock=lambda: at("10:05"),
        )
    )
    assert report["products_resolved"] == 1
    assert {"product": "MF200", "error_class": "UnresolvedProduct"} in report[
        "error_details"
    ]


def test_cli_gate_off_exits_2_before_any_view(monkeypatch, capsys) -> None:
    from scripts import collect_futures_minutes as cli

    monkeypatch.delenv("KIS_FUTURES_QUOTES_ENABLED", raising=False)
    monkeypatch.setattr(cli, "setup_logging_and_sentry", lambda **kw: None)
    assert cli.main(["--dry-run"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "refused"


def test_cli_exit_code_follows_errors(monkeypatch, capsys) -> None:
    from scripts import collect_futures_minutes as cli

    async def fake_run(args: Any) -> dict[str, Any]:
        return {"errors": 1}

    monkeypatch.setattr(cli, "setup_logging_and_sentry", lambda **kw: None)
    monkeypatch.setattr(cli, "run", fake_run)
    assert cli.main([]) == 1
    capsys.readouterr()
    monkeypatch.setattr(cli, "run", lambda args: _clean())
    assert cli.main([]) == 0


async def _clean() -> dict[str, Any]:
    return {"errors": 0}
