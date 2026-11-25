"""hk 6322: Toss US day-market minutes — labels, conversion, gating, migration shape.

Pure and fixture-driven (no database): the Toss candles and the Toss US calendar
are built in memory. The DB round trip and the replay check live in
``test_us_toss_day_market_db.py`` (CI only).
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from importlib import util
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services.brokers.toss.dto import TossMinuteCandle
from app.services.brokers.toss.market_calendar import parse_us_market_calendar
from app.services.research_candles import session_minutes_collector as sc
from app.services.research_candles.session_coverage import EdgeRow, first_last_report
from app.services.research_candles.session_segment import (
    TOSS_US_VENUE,
    UNKNOWN,
    US_DAY,
    US_POST,
    US_PRE,
    US_REGULAR,
    session_segment_of,
    us_calendar_segment,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")
DATE = "2026-11-25"


def _kst(hhmm: str, day: str = DATE) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00+09:00")


def _window(start: str, end: str, day: str = DATE) -> dict | None:
    if start is None:
        return None
    return {
        "startTime": f"{day}T{start}:00+09:00",
        "endTime": f"{day}T{end}:00+09:00",
    }


def _calendar(
    *,
    day_market: tuple[str, str] | None = ("10:00", "17:50"),
    pre: tuple[str, str] | None = None,
    regular: tuple[str, str] | None = None,
    after: tuple[str, str] | None = None,
):
    """Toss US calendar with one listed day (the dayMarket etc. windows in KST)."""
    raw = {
        "today": {
            "date": DATE,
            "dayMarket": _window(*day_market) if day_market else None,
            "preMarket": _window(*pre) if pre else None,
            "regularMarket": _window(*regular) if regular else None,
            "afterMarket": _window(*after) if after else None,
        }
    }
    return parse_us_market_calendar(raw)


def _candle(end_kst: datetime, *, volume: str = "100", currency: str = "USD"):
    return TossMinuteCandle(
        timestamp=end_kst,
        open_price=Decimal("900.10"),
        high_price=Decimal("901.00"),
        low_price=Decimal("899.50"),
        close_price=Decimal("900.75"),
        volume=Decimal(volume),
        currency=currency,
    )


# ------------------------------------------------ acceptance 1 and 2: bars ----


def test_toss_bar_ending_1510_kst_is_stored_at_1509_as_us_day_and_forming_bar_skipped() -> (
    None
):
    calendar = _calendar(day_market=("10:00", "17:50"))
    observed = _kst("15:10").replace(second=30)  # 15:10:30 KST
    candles = [
        _candle(_kst("15:10")),  # bar 15:09-15:10, closed by 15:10:10
        _candle(_kst("15:11")),  # bar 15:10-15:11, still forming at 15:10:30
    ]

    result = sc.bars_from_toss_candles(
        candles, symbol="NVDA", observed_before=observed, us_calendar=calendar
    )

    assert result.skipped_forming == 1
    (bar,) = result.bars
    assert bar.time_utc == _kst("15:09").astimezone(UTC)
    assert bar.session_segment == US_DAY
    assert bar.source == "TOSS"
    assert bar.venue == TOSS_US_VENUE
    assert bar.value is None  # Toss reports no per-bar trade value


def test_closed_calendar_day_labels_unknown_and_nothing_is_guessed() -> None:
    closed = _calendar(day_market=None)  # every session null: a closed day
    assert us_calendar_segment(_kst("15:09"), closed) is None
    assert (
        session_segment_of(
            "us", TOSS_US_VENUE, _kst("15:09").astimezone(UTC), us_calendar=closed
        )
        == UNKNOWN
    )

    result = sc.bars_from_toss_candles(
        [_candle(_kst("15:10"))],
        symbol="NVDA",
        observed_before=_kst("16:00"),
        us_calendar=closed,
    )
    (bar,) = result.bars
    assert bar.session_segment == UNKNOWN


def test_toss_venue_without_a_calendar_is_unknown_never_a_kis_label() -> None:
    moment = _kst("15:09").astimezone(UTC)
    assert session_segment_of("us", TOSS_US_VENUE, moment) == UNKNOWN


def test_toss_bars_outside_day_market_take_calendar_pre_regular_after_labels() -> None:
    calendar = _calendar(
        day_market=("10:00", "17:50"),
        pre=("18:00", "18:30"),
        regular=("18:30", "19:30"),
        after=("19:30", "19:50"),
    )
    assert us_calendar_segment(_kst("18:10"), calendar) == US_PRE
    assert us_calendar_segment(_kst("18:40"), calendar) == US_REGULAR
    assert us_calendar_segment(_kst("19:40"), calendar) == US_POST
    assert us_calendar_segment(_kst("19:55"), calendar) is None  # between windows


def test_calendar_window_start_inclusive_end_exclusive() -> None:
    calendar = _calendar(day_market=("10:00", "17:50"))
    assert us_calendar_segment(_kst("10:00"), calendar) == US_DAY
    assert us_calendar_segment(_kst("17:49"), calendar) == US_DAY
    assert us_calendar_segment(_kst("17:50"), calendar) is None


def test_kis_venue_inside_day_market_is_us_day_only_with_a_calendar() -> None:
    calendar = _calendar(day_market=("10:00", "17:50"))
    inside = _kst("15:09").astimezone(UTC)
    # the existing label (no calendar) is untouched: 20:00-04:00 ET stays UNKNOWN
    assert session_segment_of("us", "NASD", inside) == UNKNOWN
    assert session_segment_of("us", "NASD", inside, us_calendar=calendar) == US_DAY
    # KIS regular-session labels stay the KIS windows even with a calendar
    regular = datetime(2026, 11, 25, 10, 0, tzinfo=ET).astimezone(UTC)
    assert session_segment_of("us", "NASD", regular, us_calendar=calendar) == US_REGULAR


def test_existing_us_label_without_calendar_is_unchanged() -> None:
    evening_et = datetime(2026, 11, 25, 22, 0, tzinfo=ET).astimezone(UTC)
    assert session_segment_of("us", "NASD", evening_et) == UNKNOWN
    premarket_et = datetime(2026, 11, 25, 5, 0, tzinfo=ET).astimezone(UTC)
    assert session_segment_of("us", "NASD", premarket_et) == US_PRE


def test_non_usd_candle_is_skipped_as_invalid() -> None:
    result = sc.bars_from_toss_candles(
        [_candle(_kst("15:10"), currency="KRW")],
        symbol="NVDA",
        observed_before=_kst("16:00"),
        us_calendar=_calendar(),
    )
    assert result.bars == [] and result.skipped_invalid == 1


# ------------------------------------------------------ collector (no DB) ----


class _FakeToss:
    def __init__(self, candles: Sequence[TossMinuteCandle] = (), fail: bool = False):
        self.candles = list(candles)
        self.fail = fail
        self.calls: list[dict] = []

    async def collect_minute_candles(
        self,
        symbol,
        *,
        adjusted,
        before,
        not_before,
        max_pages=16,
        page_count=200,
        pace=None,
    ):
        self.calls.append(
            {
                "symbol": symbol,
                "adjusted": adjusted,
                "before": before,
                "not_before": not_before,
            }
        )
        if self.fail:
            raise RuntimeError("boom")
        return list(self.candles)


class _FakeKis:
    def __init__(self) -> None:
        self.calls = 0

    async def inquire_overseas_minute_chart(
        self, symbol, exchange_code="NASD", n=120, keyb=""
    ):
        self.calls += 1
        return SimpleNamespace(frame=pd.DataFrame())


class _FakeWriter:
    def __init__(self) -> None:
        self.bars: list[sc.MinuteBar] = []

    async def insert(self, bars, *, batch_id, retrieved_at):
        self.bars.extend(bars)
        return len(bars), 0


def _run(**kwargs):
    import asyncio

    return asyncio.run(sc.collect(market="us", **kwargs))


def test_collect_reads_toss_for_day_market_and_skips_kis_inside_the_window() -> None:
    observed = _kst("15:10").replace(second=30)
    toss = _FakeToss(candles=[_candle(_kst("15:10"))])
    kis = _FakeKis()
    writer = _FakeWriter()

    report = _run(
        symbols=["NVDA"],
        kis=kis,
        writer=writer,
        us_exchanges={"NVDA": "NASD"},
        toss=toss,
        us_calendar=_calendar(day_market=("10:00", "17:50")),
        clock=lambda: observed,
    )

    assert kis.calls == 0  # KIS has no bars inside the day market
    assert report["kis_skipped_day_market"] == 1
    assert report["toss"] == "ran" and report["toss_reads"] == 1
    assert report["errors"] == 0
    (call,) = toss.calls
    assert call["adjusted"] is False
    assert call["not_before"] == observed - sc.TOSS_US_LOOKBACK
    assert datetime.fromisoformat(call["before"]) >= observed  # cursor past the clock
    (bar,) = writer.bars
    assert (bar.session_segment, bar.source, bar.venue) == (
        US_DAY,
        "TOSS",
        TOSS_US_VENUE,
    )


def test_collect_keeps_kis_reads_outside_the_day_market() -> None:
    # 18:30 KST is after the day-market window (10:00-17:50 KST): KIS is read.
    outside = _kst("18:30")
    kis = _FakeKis()
    report = _run(
        symbols=["NVDA"],
        kis=kis,
        writer=_FakeWriter(),
        us_exchanges={"NVDA": "NASD"},
        toss=None,
        us_calendar=_calendar(day_market=("10:00", "17:50")),
        clock=lambda: outside,
    )
    assert kis.calls == 1
    assert report["kis_skipped_day_market"] == 0


def test_missing_calendar_reads_no_toss_and_says_so() -> None:
    toss = _FakeToss()
    kis = _FakeKis()
    report = _run(
        symbols=["NVDA"],
        kis=kis,
        writer=_FakeWriter(),
        us_exchanges={"NVDA": "NASD"},
        toss=toss,
        us_calendar=None,
        clock=lambda: _kst("15:10"),
    )
    assert toss.calls == []
    assert report["toss"] == "calendar_unavailable"
    assert {"venue": TOSS_US_VENUE, "error_class": "CalendarUnavailable"} in report[
        "error_details"
    ]
    assert kis.calls == 1  # KIS pass is unchanged without a calendar


def test_calendar_with_no_window_near_the_clock_reads_no_toss() -> None:
    toss = _FakeToss()
    report = _run(
        symbols=["NVDA"],
        kis=_FakeKis(),
        writer=_FakeWriter(),
        us_exchanges={"NVDA": "NASD"},
        toss=toss,
        us_calendar=_calendar(day_market=("10:00", "17:50"), pre=None),
        clock=lambda: _kst("15:10", day="2026-12-20"),
    )
    assert toss.calls == []
    assert report["toss"] == "no_window"


def test_one_toss_failure_is_reported_per_symbol_and_the_run_continues() -> None:
    observed = _kst("15:10").replace(second=30)
    report = _run(
        symbols=["NVDA", "SPY"],
        kis=_FakeKis(),
        writer=_FakeWriter(),
        us_exchanges={},
        toss=_FakeToss(fail=True),
        us_calendar=_calendar(day_market=("10:00", "17:50")),
        clock=lambda: observed,
    )
    assert report["toss_reads"] == 0
    assert {d["symbol"] for d in report["error_details"]} == {"NVDA", "SPY"}
    assert all(d["venue"] == TOSS_US_VENUE for d in report["error_details"])


def test_toss_dry_run_writes_nothing_and_counts_the_would_write() -> None:
    observed = _kst("15:10").replace(second=30)
    writer = _FakeWriter()
    report = _run(
        symbols=["NVDA"],
        kis=_FakeKis(),
        writer=writer,
        us_exchanges={},
        toss=_FakeToss(candles=[_candle(_kst("15:10"))]),
        us_calendar=_calendar(day_market=("10:00", "17:50")),
        dry_run=True,
        clock=lambda: observed,
    )
    assert writer.bars == []
    assert report["would_write"] == 1


# ----------------------------------------------------- static boundary (no DB) ----


def test_collector_imports_no_toss_module_at_runtime() -> None:
    tree = ast.parse(
        (
            REPO / "app/services/research_candles/session_minutes_collector.py"
        ).read_text()
    )
    runtime = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and "brokers.toss" in ast.unparse(node)
    ]
    assert runtime == []  # only the TYPE_CHECKING block names Toss modules


def test_session_segment_imports_no_toss_module_at_runtime() -> None:
    tree = ast.parse(
        (REPO / "app/services/research_candles/session_segment.py").read_text()
    )
    runtime = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and "brokers.toss" in ast.unparse(node)
    ]
    assert runtime == []


def test_collector_module_registers_nothing_and_has_no_schedule() -> None:
    source = (
        (REPO / "app/services/research_candles/session_minutes_collector.py")
        .read_text()
        .lower()
    )
    for forbidden in ("taskiq", "prefect", "@broker", "crontab", "schedule="):
        assert forbidden not in source, forbidden


# ----------------------------------------------------- first/last check (no DB) ----


def test_first_last_report_groups_by_symbol_source_venue_and_segment() -> None:
    rows = [
        EdgeRow("NVDA", "TOSS", TOSS_US_VENUE, US_DAY, _kst("15:09").astimezone(UTC)),
        EdgeRow("NVDA", "TOSS", TOSS_US_VENUE, US_DAY, _kst("16:40").astimezone(UTC)),
        EdgeRow("NVDA", "KIS", "NASD", US_REGULAR, _kst("23:30").astimezone(UTC)),
    ]
    report = first_last_report(
        rows, market="us", session_date=datetime(2026, 11, 25).date()
    )
    assert report["mode"] == "first_last"
    entries = report["entries"]
    assert [e["source"] for e in entries] == ["KIS", "TOSS"]  # sorted, never merged
    toss = next(e for e in entries if e["source"] == "TOSS")
    assert toss["rows"] == 2
    assert toss["first_bar_start_kst"].startswith("2026-11-25T15:09:00")
    assert toss["last_bar_start_kst"].startswith("2026-11-25T16:40:00")


# ------------------------------------------------------- migration shape (no DB) ----

MIGRATION_A = REPO / "alembic/versions/20261125_hk6322_us_toss_venue.py"
MIGRATION_B = REPO / "alembic/versions/20261125_hk6322_us_venue_val.py"


def _load(path: Path, name: str):
    spec = util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_chain_revision_length_and_expand_contract_marker() -> None:
    a = _load(MIGRATION_A, "hk6322_a")
    b = _load(MIGRATION_B, "hk6322_b")
    assert a.down_revision == "20261124_hk6154_momentum_runs"
    assert b.down_revision == a.revision
    assert len(a.revision) <= 32 and len(b.revision) <= 32  # alembic_version width
    for path in (MIGRATION_A, MIGRATION_B):
        assert "# expand-contract: contract-step 6322" in path.read_text()


def test_venue_check_is_added_not_valid_and_validated_in_the_follow_up() -> None:
    a = _load(MIGRATION_A, "hk6322_a_sql")
    captured: list[str] = []
    a.op = SimpleNamespace(execute=lambda sql: captured.append(str(sql)))
    a.upgrade()
    joined = " ".join(" ".join(captured).split())
    assert "ADD CONSTRAINT ck_research_us_candles_1m_venue" in joined
    assert "'TOSS_INTEGRATED'" in joined and "NOT VALID" in joined
    assert "ALTER COLUMN value DROP NOT NULL" in joined
    assert "VALIDATE CONSTRAINT" not in joined

    b = _load(MIGRATION_B, "hk6322_b_sql")
    captured_b: list[str] = []
    b.op = SimpleNamespace(execute=lambda sql: captured_b.append(str(sql)))
    b.upgrade()
    assert any(
        "VALIDATE CONSTRAINT ck_research_us_candles_1m_venue" in s for s in captured_b
    )


def test_downgrade_refuses_when_toss_rows_or_null_values_exist() -> None:
    a = _load(MIGRATION_A, "hk6322_a_down")
    captured: list[str] = []
    a.op = SimpleNamespace(execute=lambda sql: captured.append(str(sql)))
    a.downgrade()
    guard = captured[0]
    assert "venue = 'TOSS_INTEGRATED'" in guard and "restrict_violation" in guard
    assert "value IS NULL" in guard
    # restored check after the guard uses the old list only
    restored = " ".join(captured[1:])
    assert "'TOSS_INTEGRATED'" not in restored


def test_hk6322_has_no_retention_or_scheduler_text() -> None:
    for path in (MIGRATION_A, MIGRATION_B):
        source = path.read_text()
        assert "add_retention_policy" not in source
        assert "add_continuous_aggregate_policy" not in source


# ------------------------------------------------------------ CLI wiring (no DB) ----


def test_first_last_check_is_us_only() -> None:
    import importlib

    diagnose = importlib.import_module("scripts.diagnose_session_coverage")
    args = diagnose._parse_args(
        ["--market", "us", "--date", "2026-11-25", "--first-last"]
    )
    assert args.first_last is True
    with pytest.raises(SystemExit) as refused:
        diagnose._parse_args(["--market", "kr", "--date", "2026-11-25", "--first-last"])
    assert refused.value.code == 2


def test_toss_inputs_are_not_requested_for_kr_and_disabled_without_the_flag(
    monkeypatch,
) -> None:
    import importlib

    from app.core.config import settings

    cli = importlib.import_module("scripts.collect_session_minutes")
    reader, calendar, status = asyncio_run(cli._toss_us_inputs("kr"))
    assert (reader, calendar, status) == (None, None, "not_requested")
    monkeypatch.setattr(settings, "toss_api_enabled", False)
    reader, calendar, status = asyncio_run(cli._toss_us_inputs("us"))
    assert (reader, calendar, status) == (None, None, "disabled")


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def test_alembic_script_directory_has_one_head_and_it_holds_the_follow_up() -> None:
    """The per-file tests above never resolve the chain; this one does (review r1)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(REPO / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    ancestry = {r.revision for r in script.walk_revisions("base", heads[0])}
    assert "20261125_hk6322_us_venue_val" in ancestry
    assert "20261125_hk6036_upbit_shadow" in ancestry
    # hk 6510 rides on that head (expand + validate revisions).
    assert "20261125_hk6510_us_movers" in ancestry
    assert "20261125_hk6510_us_movers_val" in ancestry
    assert script.get_revision("20261125_hk6322_us_toss_venue").down_revision == (
        "20261124_hk6154_momentum_runs"
    )


def test_unbuildable_toss_reader_is_an_error_not_a_silent_skip() -> None:
    import importlib

    cli = importlib.import_module("scripts.collect_session_minutes")
    errors = cli._toss_input_errors("reader_unavailable")
    assert errors == [{"venue": TOSS_US_VENUE, "error_class": "TossReaderUnavailable"}]
    assert cli._toss_input_errors("disabled") == []
    assert cli._toss_input_errors("ready") == []


def test_kis_tail_is_still_read_in_the_first_grace_minutes_after_day_market_opens() -> (
    None
):
    # Day market opens at 20:00 ET (= 10:00 KST on 2026-11-25). KIS after-hours
    # ends at the same instant, so the 19:59 ET bar must be read and stored at 20:00:30.
    calendar = _calendar(day_market=("10:00", "17:50"))
    observed = datetime(2026, 11, 24, 20, 0, 30, tzinfo=ET).astimezone(UTC)
    assert observed == datetime(2026, 11, 25, 1, 0, 30, tzinfo=UTC)
    bar_1959_et = datetime(2026, 11, 24, 19, 59, tzinfo=ET)
    frame = pd.DataFrame(
        {
            "datetime": pd.to_datetime(["2026-11-24 19:59"]),
            "open": [900.0],
            "high": [901.0],
            "low": [899.0],
            "close": [900.5],
            "volume": [50],
            "value": [45000.0],
        }
    )

    class _TailKis(_FakeKis):
        async def inquire_overseas_minute_chart(
            self, symbol, exchange_code="NASD", n=120, keyb=""
        ):
            self.calls += 1
            return SimpleNamespace(frame=frame)

    kis = _TailKis()
    writer = _FakeWriter()
    report = _run(
        symbols=["NVDA"],
        kis=kis,
        writer=writer,
        us_exchanges={"NVDA": "NASD"},
        toss=None,
        us_calendar=calendar,
        clock=lambda: observed,
    )
    assert kis.calls == 1
    assert report["kis_skipped_day_market"] == 0
    (bar,) = writer.bars
    assert bar.time_utc == bar_1959_et.astimezone(UTC)
    assert bar.session_segment == US_POST


def test_validate_revision_is_a_plain_statement_and_states_its_lock() -> None:
    """Review r3 (consult): no autocommit block in the validate revision (it breaks the
    DB harness, which applies migrations inside an open transaction), and the
    docstring states that the scan runs under the expand locks."""
    source = MIGRATION_B.read_text()
    assert "autocommit_block" not in source
    assert "VALIDATE CONSTRAINT" in source
    assert "ACCESS EXCLUSIVE" in source and "ONE transaction" in source


def test_unbuildable_toss_reader_counts_as_a_run_error_in_collect() -> None:
    """The CLI passes this error as prior_errors; collect counts it (exit 1 follows)."""
    import importlib

    cli = importlib.import_module("scripts.collect_session_minutes")
    report = _run(
        symbols=["NVDA"],
        kis=_FakeKis(),
        writer=_FakeWriter(),
        us_exchanges={"NVDA": "NASD"},
        toss=None,
        us_calendar=None,
        prior_errors=cli._toss_input_errors("reader_unavailable"),
        clock=lambda: _kst("15:10"),
    )
    assert report["errors"] == 1
    assert report["toss"] == "not_requested"  # collect never ran the Toss block
    assert report["error_details"][0]["error_class"] == "TossReaderUnavailable"
