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
    # 10:03 is closed; 10:04 ends 10:05:00 (+10 s grace = 10:05:10) -> forming at 10:05:05.
    frame = _frame(["2026-11-24 10:03", "2026-11-24 10:04", "2026-11-24 10:05"])
    now = datetime(2026, 11, 24, 10, 5, 5, tzinfo=KST).astimezone(UTC)
    out = sc.bars_from_frame(
        frame, market="kr", symbol="005930", venue="KRX", observed_before=now
    )
    assert [b.time_utc.astimezone(KST).strftime("%H:%M") for b in out.bars] == ["10:03"]
    assert out.skipped_forming == 2


def test_bar_becomes_closed_exactly_at_start_plus_70_seconds() -> None:
    frame = _frame(["2026-11-24 10:04"])
    at = datetime(2026, 11, 24, 10, 5, 10, tzinfo=KST).astimezone(UTC)
    before = datetime(2026, 11, 24, 10, 5, 9, tzinfo=KST).astimezone(UTC)
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
    frame = _frame(["2026-11-24 10:00", "2026-11-24 10:00", "2026-11-24 10:01"])
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


# ------------------------------------------------------------------ collect --


NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)  # 21:00 KST


@pytest.mark.asyncio
async def test_nx_is_read_only_for_nxt_eligible_symbols() -> None:
    kis = FakeKis(
        kr_frames={
            ("005930", "J"): _frame(["2026-11-24 10:00"]),
            ("005930", "NX"): _frame(["2026-11-24 10:00"]),
            ("000660", "J"): _frame(["2026-11-24 10:00"]),
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
        kr_frames={("005930", "J"): _frame(["2026-11-24 10:00", "2026-11-24 10:01"])}
    )
    first_bar_time = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)
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
        kr_frames={("000660", "J"): _frame(["2026-11-24 10:00"])}, fail={"005930"}
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
    kis = FakeKis(kr_frames={("005930", "J"): _frame(["2026-11-24 10:00"])})
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
        _frame(["2026-11-24 10:00"]),
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
    "app.services.execution_ledger.kis_lots",  # us_trading_day_window (pure date helper)
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
