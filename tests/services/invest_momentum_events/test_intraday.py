"""hk 6154: intraday movers runner (no DB, no network)."""

from __future__ import annotations

import ast
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
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
        (_status_error(401), "auth"),
        (_status_error(403), "auth"),
        (_status_error(429), "rate_limited"),
        (_status_error(418), "http_4xx"),
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


@pytest.mark.parametrize(
    ("hhmm", "second", "expected"),
    [
        ("07:59", 59, None),
        ("08:00", 0, "nxt_pre"),
        ("08:49", 59, "nxt_pre"),
        ("08:50", 0, None),
        ("09:00", 0, "regular"),
        ("15:29", 59, "regular"),
        ("15:30", 0, None),
        ("15:59", 59, None),
        ("16:00", 0, "after"),
        ("19:59", 59, "after"),
        ("20:00", 0, None),
    ],
)
def test_observation_windows_have_exact_kst_boundaries(hhmm, second, expected) -> None:
    assert intraday.observation_window(_kst(TUESDAY, hhmm, second)) == expected


# ---------------------------------------------------------------- run (fakes) --


class FakeFetcher:
    def __init__(self, by_order: dict):
        self.by_order = by_order
        self.calls: list[str] = []
        self.call_args: list[dict] = []

    async def fetch_domestic_stock_default(self, **kwargs):
        order = kwargs["order_type"]
        self.calls.append(order)
        self.call_args.append(dict(kwargs))
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


class FakeKisFetcher:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fluctuation_rank(self, *, market, direction, limit):
        self.calls.append(("fluctuation_rank", market, direction, limit))
        return self.rows

    async def volume_rank(self, *, market, limit):
        self.calls.append(("volume_rank", market, limit))
        return self.rows


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
    assert fetcher.call_args == [
        {
            "trade_type": "KRX",
            "market_type": "ALL",
            "order_type": order,
            "start_idx": 0,
            "page_size": 50,
        }
        for order in intraday.ORDER_TYPES
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
async def test_nxt_pre_records_venue_and_session_in_each_surface() -> None:
    fetcher = FakeFetcher({"up": [ROW], "down": []})
    report = await _run(fetcher, now=_kst(TUESDAY, "08:00"), commit=True)
    assert [item.surface for item in report.surfaces] == [
        "stock:naver_stock:NXT:nxt_pre:ALL:up",
        "stock:naver_stock:NXT:nxt_pre:ALL:down",
    ]
    assert all(call["trade_type"] == "NXT" for call in fetcher.call_args)
    assert [row.trade_type for row in MemoryRepo.rows.values()] == ["NXT"]


@pytest.mark.asyncio
async def test_after_window_records_both_nxt_and_krx_surfaces() -> None:
    fetcher = FakeFetcher({"up": [ROW], "down": [], "priceTop": [ROW]})
    report = await _run(fetcher, now=_kst(TUESDAY, "16:00"), commit=True)
    surfaces = {item.surface for item in report.surfaces}
    assert surfaces == {
        *(f"stock:naver_stock:NXT:nxt_post:ALL:{side}" for side in ("up", "down")),
        *(
            f"stock:naver_stock:KRX:krx_after:ALL:{side}"
            for side in intraday.ORDER_TYPES
        ),
    }
    assert [call["trade_type"] for call in fetcher.call_args] == [
        "NXT",
        "NXT",
        "KRX",
        "KRX",
        "KRX",
    ]


@pytest.mark.asyncio
async def test_nxt_unsupported_ranking_falls_back_to_kis_nx_fixture() -> None:
    fixture = _fixture("../invest_momentum_events/kis_nx_fluctuation_rank.json")
    naver = FakeFetcher({"up": _status_error(400)})
    kis = FakeKisFetcher(fixture)
    outcome = await intraday.fetch_extended_surface(
        naver, kis, venue="NXT", order_type="up"
    )
    assert outcome.status == "ok" and outcome.source == intraday.KIS_SOURCE
    assert outcome.rows[0].symbol == "005930"
    assert outcome.rows[0].price == Decimal("171300")
    assert outcome.rows[0].change_rate == Decimal("-0.12")
    assert kis.calls == [("fluctuation_rank", "NX", "up", intraday.PAGE_SIZE)]


def test_kis_payload_accepts_alphanumeric_short_codes() -> None:
    payload = [
        {
            "stck_shrn_iscd": "0256c0",
            "hts_kor_isnm": "새코드",
            "stck_prpr": "1000",
        },
        {"mksc_shrn_iscd": "0009K0", "hts_kor_isnm": "알파", "stck_prpr": "2000"},
        {"stck_shrn_iscd": "bad!", "hts_kor_isnm": "무효"},
        {"stck_shrn_iscd": "12345", "hts_kor_isnm": "짧은코드"},
        # Unicode upper() can rewrite a non-ASCII char into ASCII ("ı" ->
        # "I"); the raw input must be six ASCII alphanumerics before
        # normalization, not the uppercased result.
        {"stck_shrn_iscd": "1234ı0", "hts_kor_isnm": "비ASCII"},
    ]
    outcome = intraday.classify_kis_payload(payload)
    assert outcome.status == "ok" and outcome.source == intraday.KIS_SOURCE
    assert [row.symbol for row in outcome.rows] == ["0256C0", "0009K0"]


def test_kis_payload_normalizes_hts_avls_to_krw() -> None:
    outcome = intraday.classify_kis_payload(
        [
            {
                "stck_shrn_iscd": "005930",
                "hts_kor_isnm": "삼성전자",
                "hts_avls": "10224000",
            }
        ]
    )
    assert outcome.status == "ok"
    row = outcome.rows[0]
    assert row.market_cap == Decimal("10224000") * 100_000_000
    assert row.raw_payload["hts_avls_unit_krw"] == "100000000"
    assert row.raw_payload["hts_avls"] == "10224000"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "reason"),
    [
        (400, None),
        (404, None),
        (401, "auth"),
        (403, "auth"),
        (429, "rate_limited"),
        (418, "http_4xx"),
    ],
)
async def test_nxt_falls_back_only_for_documented_unsupported_4xx(
    status_code, reason
) -> None:
    naver = FakeFetcher({"up": _status_error(status_code)})
    kis = FakeKisFetcher([{"stck_shrn_iscd": "005930"}])
    outcome = await intraday.fetch_extended_surface(
        naver, kis, venue="NXT", order_type="up"
    )
    if reason is None:
        assert outcome.status == "ok" and outcome.source == intraday.KIS_SOURCE
        assert kis.calls == [("fluctuation_rank", "NX", "up", intraday.PAGE_SIZE)]
    else:
        assert outcome.status == "error" and outcome.reason == reason
        assert outcome.source == "naver_stock"
        assert kis.calls == []


@pytest.mark.asyncio
async def test_outside_extended_windows_fetches_nothing() -> None:
    fetcher = FakeFetcher({})
    report = await intraday.run_intraday_movers(
        fetcher=fetcher,
        session=FakeSession(),
        now=_kst(TUESDAY, "15:45"),
        commit=True,
        is_session_day=lambda day: True,
    )
    assert report.status == "outside_session"
    assert fetcher.call_args == [] and MemoryRepo.statuses == {}


@pytest.mark.asyncio
async def test_first_capture_preserves_extended_surface_but_keeps_kr_golden() -> None:
    class FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [
                SimpleNamespace(
                    symbol="005930",
                    name="삼성전자",
                    snapshot_at=_kst(TUESDAY, "08:00"),
                    surface="stock:naver_stock:NXT:nxt_pre:ALL:up",
                    trade_type="NXT",
                    market_type="ALL",
                    order_type="up",
                    rank=1,
                    change_rate=Decimal("1.2"),
                    trade_value=Decimal("10"),
                    market="kr",
                ),
                SimpleNamespace(
                    symbol="000660",
                    name="SK하이닉스",
                    snapshot_at=_kst(TUESDAY, "10:00"),
                    surface="domestic_market_stock_default",
                    trade_type="KRX",
                    market_type="ALL",
                    order_type="up",
                    rank=2,
                    change_rate=Decimal("2.3"),
                    trade_value=Decimal("20"),
                    market="kr",
                ),
            ]

    class FakeReadSession:
        async def execute(self, statement):
            return FakeResult()

    captures = await intraday.first_captures(
        FakeReadSession(),
        dt.date(2026, 11, 24),
        market="kr",  # type: ignore[arg-type]
    )
    assert [capture.surface for capture in captures] == [
        "stock:naver_stock:NXT:nxt_pre:ALL:up",
        "stock:KRX:ALL:up",
    ]


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
    # KISClient is allowed only in the CLI adapter for the explicitly read-only
    # NX ranking fallback; the service remains broker-client-free.
    assert "app.services.brokers.kis.client" not in _imports(
        REPO / "app/services/invest_momentum_events/intraday.py"
    )
    script = (REPO / "scripts/intraday_movers.py").read_text(encoding="utf-8")
    for token in ("order_korea_stock", "cancel_korea_order", "modify_korea_order"):
        assert token not in script


def test_nothing_new_writes_the_screener_pick_log_or_registers_a_schedule() -> None:
    for relative in _NEW_FILES:
        text = (REPO / relative).read_text(encoding="utf-8")
        for token in ("screener_pick_log", "broker.task", "app.core.scheduler"):
            assert token not in text, (relative, token)
