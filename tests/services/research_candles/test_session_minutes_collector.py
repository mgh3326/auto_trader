"""hk 6145: session minute collector (fake KIS client, no DB, no network)."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services.research_candles import session_minutes_collector as sc

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")


def _frame(labels: list[str], base: int = 100) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": pd.to_datetime(labels),
            "open": [base + i for i in range(len(labels))],
            "high": [base + i + 1 for i in range(len(labels))],
            "low": [base + i - 1 for i in range(len(labels))],
            "close": [base + i for i in range(len(labels))],
            "volume": [10 + i for i in range(len(labels))],
            "value": [1000 + i for i in range(len(labels))],
        }
    )


class FakeKis:
    def __init__(self, kr_frames=None, us_frames=None, fail=()):
        self.kr_frames = kr_frames or {}
        self.us_frames = us_frames or {}
        self.fail = set(fail)
        self.kr_calls: list[tuple[str, str]] = []
        self.us_calls: list[tuple[str, str]] = []

    async def inquire_minute_chart(self, code, market="J", time_unit=1, n=200):
        self.kr_calls.append((code, market))
        if code in self.fail:
            raise RuntimeError("secret detail must not leak")
        return self.kr_frames.get((code, market), _frame([]))

    async def inquire_overseas_minute_chart(
        self, symbol, exchange_code="NASD", n=120, keyb=""
    ):
        self.us_calls.append((symbol, exchange_code))
        if symbol in self.fail:
            raise RuntimeError("boom")
        return SimpleNamespace(
            frame=self.us_frames.get((symbol, exchange_code), _frame([]))
        )


class RecordingWriter:
    def __init__(self, present: set | None = None):
        self.calls: list[tuple[list[sc.MinuteBar], str, datetime]] = []
        self.present = present or set()

    async def insert(self, bars, *, batch_id, retrieved_at):
        self.calls.append((list(bars), batch_id, retrieved_at))
        already = sum(
            1 for b in bars if (b.symbol, b.venue, b.time_utc) in self.present
        )
        return len(bars) - already, already


def _clock(value: datetime):
    return lambda: value


# ------------------------------------------------------------- closed bars --


def test_forming_bar_is_excluded_and_counted() -> None:
    # 09:00 is closed; 09:01 ends 09:02:00 (+10 s grace = 09:02:10) -> forming at 09:02:05.
    frame = _frame(["2026-11-24 09:00", "2026-11-24 09:01", "2026-11-24 09:02"])
    now = datetime(2026, 11, 24, 9, 2, 5, tzinfo=KST).astimezone(UTC)
    out = sc.bars_from_frame(
        frame, market="kr", symbol="005930", venue="KRX", observed_before=now
    )
    assert [b.time_utc.astimezone(KST).strftime("%H:%M") for b in out.bars] == ["09:00"]
    assert out.skipped_forming == 2


def test_bar_becomes_closed_exactly_at_start_plus_70_seconds() -> None:
    frame = _frame(["2026-11-24 09:00"])
    at = datetime(2026, 11, 24, 9, 1, 10, tzinfo=KST).astimezone(UTC)
    before = datetime(2026, 11, 24, 9, 1, 9, tzinfo=KST).astimezone(UTC)
    assert (
        len(
            sc.bars_from_frame(
                frame, market="kr", symbol="s", venue="KRX", observed_before=at
            ).bars
        )
        == 1
    )
    assert (
        sc.bars_from_frame(
            frame, market="kr", symbol="s", venue="KRX", observed_before=before
        ).skipped_forming
        == 1
    )


# ----------------------------------------------------------- label -> UTC ---


def test_kr_start_label_is_kst_local() -> None:
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    out = sc.bars_from_frame(
        _frame(["2026-11-24 09:00"]),
        market="kr",
        symbol="005930",
        venue="KRX",
        observed_before=now,
    )
    (bar,) = out.bars
    assert bar.time_utc == datetime(2026, 11, 24, 0, 0, tzinfo=UTC)
    assert bar.session_segment == "KRX_REGULAR"
    assert bar.session_date.isoformat() == "2026-11-24"
    assert bar.source == "KIS"


@pytest.mark.parametrize(
    ("label", "utc_hour", "segment"),
    [
        ("2026-07-15 09:30", 13, "US_REGULAR"),  # EDT
        ("2026-12-15 09:30", 14, "US_REGULAR"),  # EST
        ("2026-07-15 04:00", 8, "US_PRE"),
        ("2026-12-15 16:00", 21, "US_POST"),
    ],
)
def test_us_start_label_is_et_local(label: str, utc_hour: int, segment: str) -> None:
    now = datetime(2027, 1, 1, tzinfo=UTC)
    out = sc.bars_from_frame(
        _frame([label]), market="us", symbol="NVDA", venue="NASD", observed_before=now
    )
    (bar,) = out.bars
    assert bar.time_utc.hour == utc_hour and bar.time_utc.minute == (
        0 if label.endswith("04:00") or label.endswith("16:00") else 30
    )
    assert bar.session_segment == segment


def test_us_session_date_rolls_at_2000_et() -> None:
    now = datetime(2027, 1, 1, tzinfo=UTC)
    out = sc.bars_from_frame(
        _frame(["2026-12-15 19:59", "2026-12-15 20:00"]),
        market="us",
        symbol="NVDA",
        venue="NASD",
        observed_before=now,
    )
    assert [b.session_date.isoformat() for b in out.bars] == [
        "2026-12-15",
        "2026-12-16",
    ]
    assert out.bars[1].session_segment == "UNKNOWN"


def test_invalid_or_duplicate_rows_are_dropped() -> None:
    frame = _frame(["2026-11-24 09:00", "2026-11-24 09:00", "2026-11-24 09:01"])
    frame.loc[2, "close"] = float("nan")
    out = sc.bars_from_frame(
        frame,
        market="kr",
        symbol="s",
        venue="KRX",
        observed_before=datetime(2026, 11, 24, 6, tzinfo=UTC),
    )
    assert len(out.bars) == 1 and out.skipped_invalid == 1
    assert isinstance(out.bars[0].open, Decimal)


# ------------------------------------------------- KR accumulated value ---


def _cum_frame(labels: list[str], cumulative: list[int]) -> pd.DataFrame:
    frame = _frame(labels)
    frame["value"] = cumulative
    return frame


def _kr_bars(frame: pd.DataFrame, venue: str = "KRX"):
    return sc.bars_from_frame(
        frame,
        market="kr",
        symbol="005930",
        venue=venue,
        observed_before=datetime(2026, 12, 1, tzinfo=UTC),
    )


def _hhmm(out) -> list[str]:
    return [b.time_utc.astimezone(KST).strftime("%H:%M") for b in out.bars]


def test_kr_value_is_the_difference_of_accumulated_values() -> None:
    out = _kr_bars(
        _cum_frame(
            [
                "2026-11-24 09:00",
                "2026-11-24 09:01",
                "2026-11-24 09:02",
                "2026-11-24 09:05",
            ],
            [100, 250, 250, 400],
        )
    )
    # first minute of the KRX day = accumulated itself; a gap minute uses the previous row
    assert [b.value for b in out.bars] == [
        Decimal(100),
        Decimal(150),
        Decimal(0),
        Decimal(150),
    ]
    assert out.null_value == 0


def test_kr_not_first_minute_and_no_predecessor_keeps_the_bar_with_null_value() -> None:
    # NTX first traded minute is 08:03, not the venue's first clock minute 08:00.
    out = _kr_bars(
        _cum_frame(["2026-11-24 08:03", "2026-11-24 08:04"], [40, 55]), venue="NTX"
    )
    assert _hhmm(out) == ["08:03", "08:04"]
    assert [b.value for b in out.bars] == [None, Decimal(15)]
    assert out.null_value == 1
    # frame length proves nothing: a short frame does not turn the oldest row's
    # accumulated value into a per-minute value
    assert out.bars[0].open is not None and out.bars[0].volume is not None


def test_kr_oldest_row_of_a_longer_frame_is_kept_with_null_value() -> None:
    labels = [f"2026-11-24 10:{minute:02d}" for minute in range(30)]
    out = _kr_bars(_cum_frame(labels, [1000 + 10 * i for i in range(len(labels))]))
    assert len(out.bars) == 30
    assert out.bars[0].value is None and {b.value for b in out.bars[1:]} == {
        Decimal(10)
    }
    assert out.null_value == 1


def test_kr_decreasing_accumulated_value_is_null_and_so_is_the_next_row() -> None:
    out = _kr_bars(
        _cum_frame(
            [
                "2026-11-24 09:00",
                "2026-11-24 09:01",
                "2026-11-24 09:02",
                "2026-11-24 09:03",
            ],
            [500, 400, 450, 470],
        )
    )
    # 09:01 decreased -> NULL; 09:02 uses a suspect baseline -> NULL; 09:03 = 470 - 450
    assert [b.value for b in out.bars] == [Decimal(500), None, None, Decimal(20)]
    assert out.null_value == 2


def test_kr_row_after_a_dropped_invalid_row_has_null_value() -> None:
    frame = _cum_frame(
        [
            "2026-11-24 09:00",
            "2026-11-24 09:01",
            "2026-11-24 09:02",
            "2026-11-24 09:03",
        ],
        [100, 250, 400, 460],
    )
    frame.loc[1, "close"] = float("nan")
    out = _kr_bars(frame)
    # 09:01 is dropped (invalid OHLC); 09:02 would absorb its value -> NULL; 09:03 normal
    assert _hhmm(out) == ["09:00", "09:02", "09:03"]
    assert [b.value for b in out.bars] == [Decimal(100), None, Decimal(60)]
    assert out.skipped_invalid == 1 and out.null_value == 1


def test_kr_invalid_first_row_of_the_day_makes_the_next_value_null() -> None:
    frame = _cum_frame(["2026-11-24 09:00", "2026-11-24 09:01"], [100, 250])
    frame.loc[0, "open"] = float("nan")
    out = _kr_bars(frame)
    assert _hhmm(out) == ["09:01"] and out.bars[0].value is None
    assert out.skipped_invalid == 1 and out.null_value == 1


def test_kr_unreadable_value_keeps_the_bar_and_breaks_the_chain() -> None:
    frame = _cum_frame(
        ["2026-11-24 09:00", "2026-11-24 09:01", "2026-11-24 09:02"], [100, 250, 400]
    )
    frame["value"] = frame["value"].astype(float)
    frame.loc[1, "value"] = float("nan")
    out = _kr_bars(frame)
    assert _hhmm(out) == ["09:00", "09:01", "09:02"]
    assert [b.value for b in out.bars] == [Decimal(100), None, None]


def test_kr_accumulation_does_not_carry_across_days() -> None:
    out = _kr_bars(_cum_frame(["2026-11-23 15:29", "2026-11-24 09:00"], [9000, 70]))
    # 11-23 15:29 has no provable value; 11-24 09:00 is its day's first minute (70, not 70-9000)
    assert [b.value for b in out.bars] == [None, Decimal(70)]


def test_kr_null_value_is_written_as_sql_null() -> None:
    import asyncio

    bars = _kr_bars(_cum_frame(["2026-11-24 10:00"], [5000])).bars
    assert bars[0].value is None
    captured: list[dict] = []

    class _Session:
        async def execute(self, statement, params=None):
            captured.append(params)
            return SimpleNamespace(rowcount=1)

        async def commit(self):
            pass

        async def rollback(self):  # pragma: no cover
            raise AssertionError

    asyncio.run(
        sc.SqlMinuteWriter(_Session(), "kr").insert(
            bars, batch_id="b", retrieved_at=NOW
        )
    )
    assert captured[0]["value"] is None


def test_kr_ntx_first_minute_is_0800_and_us_value_is_untouched() -> None:
    out = _kr_bars(
        _cum_frame(["2026-11-24 08:00", "2026-11-24 08:01"], [30, 80]), venue="NTX"
    )
    assert [b.value for b in out.bars] == [Decimal(30), Decimal(50)]
    us = sc.bars_from_frame(
        _cum_frame(["2026-12-15 10:00", "2026-12-15 10:01"], [700, 900]),
        market="us",
        symbol="NVDA",
        venue="NASD",
        observed_before=datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert [b.value for b in us.bars] == [Decimal(700), Decimal(900)]  # per-bar, as is


# ------------------------------------------------------------------ collect --


NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)  # 21:00 KST


@pytest.mark.asyncio
async def test_nx_is_read_only_for_nxt_eligible_symbols() -> None:
    kis = FakeKis(
        kr_frames={
            ("005930", "J"): _frame(["2026-11-24 09:00"]),
            ("005930", "NX"): _frame(["2026-11-24 08:00"]),
            ("000660", "J"): _frame(["2026-11-24 09:00"]),
        }
    )
    writer = RecordingWriter()
    report = await sc.collect(
        market="kr",
        symbols=["000660", "005930"],
        kis=kis,
        writer=writer,
        nxt_eligible={"005930"},
        clock=_clock(NOW),
    )
    assert sorted(kis.kr_calls) == [("000660", "J"), ("005930", "J"), ("005930", "NX")]
    assert report["inserted"] == 3 and report["errors"] == 0
    venues = sorted((b.symbol, b.venue) for call in writer.calls for b in call[0])
    assert venues == [("000660", "KRX"), ("005930", "KRX"), ("005930", "NTX")]


@pytest.mark.asyncio
async def test_rerun_reports_already_present_and_uses_one_batch_per_run() -> None:
    kis = FakeKis(
        kr_frames={("005930", "J"): _frame(["2026-11-24 09:00", "2026-11-24 09:01"])}
    )
    first_bar_time = datetime(2026, 11, 24, 0, 0, tzinfo=UTC)
    writer = RecordingWriter(present={("005930", "KRX", first_bar_time)})
    report = await sc.collect(
        market="kr",
        symbols=["005930"],
        kis=kis,
        writer=writer,
        clock=_clock(NOW),
        batch_id="b1",
    )
    assert (report["inserted"], report["already_present"]) == (1, 1)
    assert {call[1] for call in writer.calls} == {"b1"}
    assert writer.calls[0][2] == NOW  # retrieved_at = write-time clock


@pytest.mark.asyncio
async def test_us_reads_resolved_exchange_and_counts_forming() -> None:
    kis = FakeKis(
        us_frames={("NVDA", "NASD"): _frame(["2026-11-24 06:58", "2026-11-24 06:59"])}
    )
    # observed_before = 11:59:30Z = 06:59:30 ET: 06:58 ends 06:59:00 +10 s -> closed; 06:59 forming.
    now = datetime(2026, 11, 24, 11, 59, 30, tzinfo=UTC)
    writer = RecordingWriter()
    report = await sc.collect(
        market="us",
        symbols=["AAPL", "NVDA"],
        kis=kis,
        writer=writer,
        us_exchanges={"NVDA": "NASD"},
        unresolved_symbols=["AAPL"],
        clock=_clock(now),
    )
    assert kis.us_calls == [("NVDA", "NASD")]
    assert report["inserted"] == 1 and report["skipped_forming"] == 1
    assert report["errors"] == 1  # AAPL unresolved is an error, not silence
    assert report["error_details"][0]["error_class"] == "UnresolvedSymbol"


@pytest.mark.asyncio
async def test_one_failing_symbol_does_not_stop_the_run_and_does_not_leak_message() -> (
    None
):
    kis = FakeKis(
        kr_frames={("000660", "J"): _frame(["2026-11-24 09:00"])}, fail={"005930"}
    )
    report = await sc.collect(
        market="kr",
        symbols=["000660", "005930"],
        kis=kis,
        writer=RecordingWriter(),
        clock=_clock(NOW),
    )
    assert report["inserted"] == 1 and report["errors"] == 1
    assert report["error_details"] == [
        {"symbol": "005930", "venue": "KRX", "error_class": "RuntimeError"}
    ]
    assert "secret" not in str(report)


@pytest.mark.asyncio
async def test_dry_run_never_calls_the_writer() -> None:
    kis = FakeKis(kr_frames={("005930", "J"): _frame(["2026-11-24 09:00"])})
    writer = RecordingWriter()
    report = await sc.collect(
        market="kr",
        symbols=["005930"],
        kis=kis,
        writer=writer,
        clock=_clock(NOW),
        dry_run=True,
    )
    assert writer.calls == [] and report["would_write"] == 1 and report["inserted"] == 0


def test_symbol_normalisation() -> None:
    assert sc.parse_symbol_list("kr", "5930, 000660 ,,bad!") == ["000660", "005930"]
    assert sc.parse_symbol_list("us", "nvda,BRK-B,BRK.B") == ["BRK.B", "NVDA"]
    assert sc.parse_symbol_list("us", None) == []


# --------------------------------------------- writes only to research.* ----


class _RecordingSession:
    def __init__(self):
        self.statements: list[str] = []
        self.commits = 0

    async def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return SimpleNamespace(rowcount=1)

    async def commit(self):
        self.commits += 1

    async def rollback(self):  # pragma: no cover
        raise AssertionError("unexpected rollback")


@pytest.mark.parametrize(
    ("market", "table"),
    [("kr", "research.kr_candles_1m"), ("us", "research.us_candles_1m")],
)
@pytest.mark.asyncio
async def test_sql_writer_targets_only_the_research_table(
    market: str, table: str
) -> None:
    session = _RecordingSession()
    bar = sc.bars_from_frame(
        _frame(["2026-11-24 09:00"]),
        market=market,
        symbol="X",
        venue="KRX" if market == "kr" else "NASD",
        observed_before=datetime(2027, 1, 1, tzinfo=UTC),
    ).bars
    assert bar
    inserted, present = await sc.SqlMinuteWriter(session, market).insert(
        bar, batch_id="b", retrieved_at=NOW
    )
    assert (inserted, present) == (1, 0)
    (statement,) = session.statements
    assert f"INSERT INTO {table}" in statement
    assert "ON CONFLICT (time_utc, symbol, venue) DO NOTHING" in statement
    assert "public." not in statement and "UPDATE" not in statement.upper().replace(
        "DO NOTHING", ""
    )
    assert session.commits == 1


def test_collector_sources_never_write_public_candle_tables() -> None:
    for relative in (
        "app/services/research_candles/session_minutes_collector.py",
        "scripts/collect_session_minutes.py",
    ):
        source = (REPO / relative).read_text()
        assert "INSERT INTO public" not in source
        assert "UPDATE public" not in source
        assert (
            "public.kr_candles_1m" not in source
            and "public.us_candles_1m" not in source
        )


# ------------------------------------------------------ static import guard ---

FORBIDDEN_FRAGMENTS = (
    "order",
    "ledger",
    "approval",
    "proposal",
    "mandate",
    "execution_intent",
    "place",
)
ALLOWED_MODULES = (
    "app.services.brokers.kis.client",  # CLI constructs the client; see attribute allowlist
    "app.services.brokers.kis.live_order_expiry",  # stdlib-only KR window constants
)
ALLOWED_KIS_ATTRIBUTES = {
    "inquire_minute_chart",
    "inquire_overseas_minute_chart",
    "fetch_my_stocks",
    "fetch_my_us_stocks",
}
GUARDED = (
    "app/services/research_candles/session_minutes_collector.py",
    "app/services/research_candles/session_segment.py",
    "app/services/research_candles/session_coverage.py",
    "scripts/collect_session_minutes.py",
    "scripts/diagnose_session_coverage.py",
)


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


@pytest.mark.parametrize("relative", GUARDED)
def test_no_order_ledger_approval_or_broker_mutation_imports(relative: str) -> None:
    path = REPO / relative
    if not path.exists():
        pytest.skip(f"{relative} not present")
    modules = _imported_modules(ast.parse(path.read_text()))
    offenders = {
        m
        for m in modules
        if m.startswith("app.")
        and not m.startswith(ALLOWED_MODULES)
        and any(fragment in m for fragment in FORBIDDEN_FRAGMENTS)
    }
    assert not offenders, offenders


def test_kis_object_is_used_only_through_read_methods() -> None:
    for relative in GUARDED:
        path = REPO / relative
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "kis"
            ):
                assert node.attr in ALLOWED_KIS_ATTRIBUTES, (relative, node.attr)


def test_modules_register_nothing() -> None:
    for relative in GUARDED:
        path = REPO / relative
        if not path.exists():
            continue
        source = path.read_text().lower()
        for forbidden in ("taskiq", "prefect", "@broker", "crontab", "schedule="):
            assert forbidden not in source, (relative, forbidden)


def _module_file(name: str) -> Path | None:
    base = REPO / name.replace(".", "/")
    if (base / "__init__.py").exists():
        return base / "__init__.py"
    if base.with_suffix(".py").exists():
        return base.with_suffix(".py")
    return None


def _import_time_imports(path: Path) -> set[str]:
    """``app.*`` modules imported when ``path`` is imported (function bodies are lazy)."""
    found: set[str] = set()

    def visit(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, ast.Import):
                found.update(a.name for a in node.names if a.name.startswith("app."))
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module.startswith("app"):
                    found.add(node.module)
                    found.update(f"{node.module}.{a.name}" for a in node.names)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            else:
                for field_name in ("body", "orelse", "finalbody", "handlers"):
                    inner = getattr(node, field_name, None)
                    if isinstance(inner, list):
                        visit([n for n in inner if isinstance(n, ast.stmt)])
                        for handler in inner:
                            if isinstance(handler, ast.ExceptHandler):
                                visit(handler.body)

    visit(ast.parse(path.read_text()).body)
    return found


def _import_closure(roots: list[str]) -> set[str]:
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        name = todo.pop()
        # importing a.b.c imports the packages a, a.b first
        parts = name.split(".")
        for end in range(1, len(parts) + 1):
            parent = ".".join(parts[:end])
            if parent in seen:
                continue
            path = _module_file(parent)
            if path is None:
                continue
            seen.add(parent)
            todo.extend(_import_time_imports(path))
    return seen


def test_import_closure_reaches_no_execution_ledger_package() -> None:
    """Closure, not just direct imports: importing the collector, the segment
    helper and the coverage math must not (statically, at import time) reach the
    execution-ledger package, where the ledger writer lives."""
    closure = _import_closure(
        [
            "app.services.research_candles.session_minutes_collector",
            "app.services.research_candles.session_coverage",
            "app.services.research_candles.session_segment",
        ]
    )
    assert "app.services.research_candles.session_segment" in closure
    assert len(closure) > 10  # the walk really followed imports
    offenders = sorted(
        m for m in closure if m.startswith("app.services.execution_ledger")
    )
    assert offenders == []
