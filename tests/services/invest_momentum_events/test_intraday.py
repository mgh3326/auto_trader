"""hk 6154: intraday movers runner (no DB, no network)."""

from __future__ import annotations

import ast
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.services.invest_momentum_events import intraday

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "tests/fixtures/naver_stock"
KST = ZoneInfo("Asia/Seoul")
TUESDAY = (2026, 11, 24)
HOLIDAY = (2026, 12, 25)  # 성탄절, XKRX closed


def _kst(day: tuple[int, int, int], hhmm: str, second: int = 0) -> dt.datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return dt.datetime(*day, hour, minute, second, tzinfo=KST)


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ parsing --


def test_falling_ranking_fixture_parses_with_negative_change_rates() -> None:
    outcome = intraday.classify_payload(
        _fixture("domestic_stock_default_down_2026-11-24.json")
    )
    assert outcome.status == "ok" and outcome.reason is None
    assert [r.symbol for r in outcome.rows] == ["498150", "009140", "025750"]
    assert [r.rank for r in outcome.rows] == [1, 2, 3]
    assert all(r.change_rate < 0 for r in outcome.rows)
    assert outcome.rows[0].change_rate == Decimal("-31.93")
    assert outcome.rows[0].trade_value == Decimal("103053890000")


def test_rising_ranking_fixture_parses_with_positive_change_rates() -> None:
    outcome = intraday.classify_payload(
        _fixture("domestic_stock_default_up_2026-11-24.json")
    )
    assert outcome.status == "ok"
    assert all(r.change_rate > 0 for r in outcome.rows)


def test_falling_ranking_is_the_naver_down_order_type() -> None:
    assert intraday.ORDER_TYPES == ("up", "down", "priceTop")
    assert intraday.surface_key("down") == "stock:KRX:ALL:down"


@pytest.mark.parametrize(
    ("payload", "status", "reason"),
    [
        ([], "empty", "no_rows"),
        ({"result": {"stocks": []}}, "empty", "no_rows"),
        (None, "error", "bad_response"),
        ("<html>", "error", "bad_response"),
        ({"unexpected": "shape"}, "error", "unparseable"),
        ([{"itemname": "no code"}], "error", "unparseable"),
    ],
)
def test_payload_classification(payload, status, reason) -> None:
    outcome = intraday.classify_payload(payload)
    assert (outcome.status, outcome.reason, outcome.rows) == (status, reason, ())


def test_duplicate_symbols_in_one_ranking_are_counted_once() -> None:
    row = {"itemcode": "005930", "itemname": "삼성전자", "nowPrice": "1"}
    outcome = intraday.classify_payload([row, dict(row), {**row, "itemcode": "000660"}])
    assert [r.symbol for r in outcome.rows] == ["005930", "000660"]


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://stock.naver.com/x")
    return httpx.HTTPStatusError(
        "secret url detail",
        request=request,
        response=httpx.Response(code, request=request),
    )


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (_status_error(400), "http_4xx"),
        (_status_error(404), "http_4xx"),
        (_status_error(500), "http_5xx"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("down"), "transport"),
        (ValueError("not json"), "bad_response"),
        (RuntimeError("secret detail"), "unexpected"),
    ],
)
def test_error_reasons_are_the_closed_vocabulary(exc, reason) -> None:
    from app.models.invest_momentum_event_snapshot import RUN_ERROR_REASONS

    assert intraday.classify_error(exc) == reason
    assert reason in RUN_ERROR_REASONS


# ------------------------------------------------------------ session guard --


@pytest.mark.parametrize(
    ("hhmm", "second", "expected"),
    [
        ("08:59", 59, False),
        ("09:00", 0, True),
        ("15:29", 59, True),
        ("15:30", 0, False),
    ],
)
def test_session_window_boundaries(hhmm, second, expected) -> None:
    now = _kst(TUESDAY, hhmm, second)
    assert intraday.krx_session_open(now, is_session_day=lambda day: True) is expected


def test_session_guard_follows_the_calendar() -> None:
    assert intraday.krx_session_open(_kst(TUESDAY, "10:00")) is True
    assert intraday.krx_session_open(_kst(HOLIDAY, "10:00")) is False
    # the calendar is not consulted outside the window
    called: list[dt.date] = []
    intraday.krx_session_open(
        _kst(TUESDAY, "16:00"), is_session_day=lambda d: called.append(d) or True
    )
    assert called == []


# ---------------------------------------------------------------- run (fakes) --


class FakeFetcher:
    def __init__(self, by_order: dict):
        self.by_order = by_order
        self.calls: list[str] = []

    async def fetch_domestic_stock_default(self, **kwargs):
        order = kwargs["order_type"]
        self.calls.append(order)
        outcome = self.by_order[order]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class MemoryRepo:
    """In-memory stand-in for the repository (the DB contract is tested in CI)."""

    statuses: dict = {}
    rows: dict = {}

    def __init__(self, session):
        self._session = session

    async def insert_run_status(
        self, *, run_id, surface, status, reason, row_count, **_
    ):
        key = (run_id, surface)
        if key in self.statuses:
            return False
        self.statuses[key] = (status, reason, row_count)
        return True

    async def insert_momentum(self, payload):
        key = (payload.snapshot_at, payload.order_type, payload.symbol)
        if key in self.rows:
            return False
        self.rows[key] = payload
        return True


@pytest.fixture(autouse=True)
def _memory_repo(monkeypatch):
    MemoryRepo.statuses = {}
    MemoryRepo.rows = {}
    monkeypatch.setattr(intraday, "InvestMomentumEventSnapshotsRepository", MemoryRepo)


NOW = _kst(TUESDAY, "10:00")
ROW = {"itemcode": "005930", "itemname": "삼성전자", "nowPrice": "78500"}


async def _run(fetcher, *, now=NOW, commit=True, session=None):
    return await intraday.run_intraday_movers(
        fetcher=fetcher,
        session=session if session is not None else FakeSession(),  # type: ignore[arg-type]
        now=now,
        commit=commit,
        is_session_day=lambda day: True,
    )


@pytest.mark.asyncio
async def test_error_empty_and_ok_surfaces_each_get_the_right_status_row() -> None:
    fetcher = FakeFetcher(
        {
            "up": _status_error(500),
            "down": [],
            "priceTop": [ROW, {**ROW, "itemcode": "000660"}],
        }
    )
    report = await _run(fetcher)
    by_surface = {s.surface: s for s in report.surfaces}
    assert report.status == "recorded" and report.failed is True
    assert by_surface["stock:KRX:ALL:up"].status == "error"
    assert by_surface["stock:KRX:ALL:up"].reason == "http_5xx"
    assert by_surface["stock:KRX:ALL:down"].status == "empty"
    assert by_surface["stock:KRX:ALL:priceTop"].status == "ok"
    stored = {surface: v for (_, surface), v in MemoryRepo.statuses.items()}
    assert stored["stock:KRX:ALL:up"] == ("error", "http_5xx", 0)
    assert stored["stock:KRX:ALL:down"] == ("empty", "no_rows", 0)
    assert stored["stock:KRX:ALL:priceTop"] == ("ok", None, 2)
    # an error / empty surface never writes ranking rows; ok writes row_count rows
    assert sorted(order for (_, order, _) in MemoryRepo.rows) == [
        "priceTop",
        "priceTop",
    ]


@pytest.mark.asyncio
async def test_rerunning_the_same_snapshot_writes_nothing() -> None:
    fetcher = FakeFetcher({"up": [ROW], "down": [ROW], "priceTop": [ROW]})
    first = await _run(fetcher)
    rows_after_first = dict(MemoryRepo.rows)
    second = await _run(fetcher)
    assert all(s.written for s in first.surfaces)
    assert all(s.already_recorded and not s.written for s in second.surfaces)
    assert MemoryRepo.rows == rows_after_first and len(MemoryRepo.statuses) == 3


@pytest.mark.asyncio
async def test_outside_session_fetches_nothing_and_writes_nothing() -> None:
    fetcher = FakeFetcher({})
    report = await intraday.run_intraday_movers(
        fetcher=fetcher,
        session=FakeSession(),  # type: ignore[arg-type]
        now=_kst(HOLIDAY, "10:00"),
        commit=True,
    )
    assert report.status == "outside_session" and report.surfaces == []
    assert fetcher.calls == [] and MemoryRepo.statuses == {} and MemoryRepo.rows == {}


@pytest.mark.asyncio
async def test_dry_run_fetches_but_never_touches_the_database() -> None:
    fetcher = FakeFetcher({"up": [ROW], "down": [], "priceTop": [ROW]})
    report = await intraday.run_intraday_movers(
        fetcher=fetcher,
        session=None,
        now=NOW,
        commit=False,
        is_session_day=lambda day: True,
    )
    assert report.status == "dry_run"
    assert [s.status for s in report.surfaces] == ["ok", "empty", "ok"]
    assert MemoryRepo.statuses == {} and MemoryRepo.rows == {}


@pytest.mark.asyncio
async def test_a_write_failure_rolls_back_that_surface_and_the_rest_continue(
    monkeypatch,
) -> None:
    class FlakyRepo(MemoryRepo):
        async def insert_momentum(self, payload):
            if payload.order_type == "down":
                raise RuntimeError("secret detail")
            return await super().insert_momentum(payload)

    monkeypatch.setattr(intraday, "InvestMomentumEventSnapshotsRepository", FlakyRepo)
    session = FakeSession()
    report = await _run(
        FakeFetcher({"up": [ROW], "down": [ROW], "priceTop": [ROW]}), session=session
    )
    by_surface = {s.surface: s for s in report.surfaces}
    assert by_surface["stock:KRX:ALL:down"].db_error is True
    assert (
        by_surface["stock:KRX:ALL:up"].written
        and by_surface["stock:KRX:ALL:priceTop"].written
    )
    assert session.rollbacks == 1 and report.failed is True
    assert "secret" not in json.dumps(report.as_dict(), default=str)


# ------------------------------------------------------------- static guard --

_NEW_FILES = (
    "app/services/invest_momentum_events/intraday.py",
    "scripts/intraday_movers.py",
)
_FORBIDDEN_IMPORTS = (
    "app.services.order_proposals",
    "app.services.execution_ledger",
    "app.services.kis_trading_service",
    "app.services.brokers.kis.client",
    "app.services.brokers.kis.domestic_orders",
    "app.services.brokers.kis.overseas_orders",
    "app.services.brokers.upbit",
    "app.services.brokers.toss",
    "app.services.brokers.alpaca",
    "app.services.brokers.binance",
    "app.services.brokers.kiwoom",
    "app.services.nhplug_mock",
    "app.mcp_server",
    "app.services.screener_tool_pick_log",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_new_modules_import_no_order_ledger_approval_or_broker_code() -> None:
    for relative in _NEW_FILES:
        imports = _imports(REPO / relative)
        for module in imports:
            assert not module.startswith(_FORBIDDEN_IMPORTS), (relative, module)


def test_nothing_new_writes_the_screener_pick_log_or_registers_a_schedule() -> None:
    for relative in _NEW_FILES:
        text = (REPO / relative).read_text(encoding="utf-8")
        for token in ("screener_pick_log", "broker.task", "app.core.scheduler"):
            assert token not in text, (relative, token)
