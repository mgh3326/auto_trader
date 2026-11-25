"""hk 6510: US intraday movers (no DB, no network)."""

from __future__ import annotations

import ast
import datetime as dt
import json
from datetime import UTC
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.services.invest_momentum_events import us_intraday, us_sources
from app.services.research_candles.session_segment import (
    UNKNOWN,
    US_POST,
    US_PRE,
    US_REGULAR,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "tests/fixtures/invest_momentum_events"
ET = ZoneInfo("America/New_York")
#: 2026-11-25 04:30 ET — a Wednesday inside the pre-market window.
PRE = dt.datetime(2026, 11, 25, 4, 30, tzinfo=ET)


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _movers():
    return _fixture("alpaca_screener_movers_2026-11-25.json")


def _actives():
    return _fixture("alpaca_screener_most_actives_2026-11-25.json")


def _kis_down():
    return _fixture("kis_overseas_updown_rate_nas_down_2026-11-25.json")


# ------------------------------------------------------------------- parsing --


def test_alpaca_movers_split_into_gainers_and_losers() -> None:
    up = us_sources.classify_alpaca_movers(_movers(), "up")
    down = us_sources.classify_alpaca_movers(_movers(), "down")
    assert up.status == "ok" and down.status == "ok"
    assert [r.symbol for r in up.rows] == ["NVDA", "AMD"]
    assert [r.symbol for r in down.rows] == ["DELL", "URBN"]
    assert [r.rank for r in down.rows] == [1, 2]
    dell = down.rows[0]
    assert dell.price == Decimal("118.42")
    assert dell.change_amount == Decimal("-9.53")
    assert dell.change_rate == Decimal("-7.45")
    # the payload carries no volume and no dollar volume: NULL with a reason
    assert dell.volume is None and dell.trade_value is None
    assert dell.reasons["volume"] == us_sources.REASON_NOT_IN_PAYLOAD
    assert dell.reasons["trade_value"] == us_sources.REASON_NOT_IN_PAYLOAD
    # the provider's own computation time is preserved, not replaced
    assert dell.raw["response"]["last_updated"] == "2026-11-24T21:00:00.123456789Z"


def test_alpaca_most_actives_keep_volume_and_leave_price_null() -> None:
    outcome = us_sources.classify_alpaca_most_actives(_actives())
    assert outcome.status == "ok"
    assert [r.symbol for r in outcome.rows] == ["NVDA", "QQQ", "AMD", "SMH"]
    nvda = outcome.rows[0]
    assert nvda.volume == 118000000
    assert nvda.price is None and nvda.trade_value is None
    assert nvda.reasons["price"] == us_sources.REASON_NOT_IN_PAYLOAD
    assert nvda.raw["row"]["trade_count"] == 412000


def test_kis_updown_derives_dollar_volume_from_the_same_row() -> None:
    outcome = us_sources.classify_kis_updown(_kis_down(), "NAS")
    assert outcome.status == "ok"
    assert [r.symbol for r in outcome.rows] == ["DELL", "URBN"]
    dell, urbn = outcome.rows
    assert dell.market_type == "NAS"
    assert dell.name == "델 테크놀로지스"
    assert dell.change_rate == Decimal("-7.45")
    assert dell.volume == 604000
    assert dell.trade_value == Decimal("118.42") * 604000
    assert dell.trade_value_basis == us_sources.BASIS_PRICE_TIMES_VOLUME
    assert urbn.trade_value == Decimal("43.11") * 1310
    assert dell.reasons == {}


@pytest.mark.parametrize(
    ("payload", "status", "reason"),
    [
        (None, "error", "bad_response"),
        ("<html>", "error", "bad_response"),
        ({"output1": {}}, "error", "unparseable"),
        ({"output2": []}, "empty", "no_rows"),
        ({"output2": [{"symb": ""}]}, "error", "unparseable"),
    ],
)
def test_kis_payload_classification(payload, status, reason) -> None:
    outcome = us_sources.classify_kis_updown(payload, "NAS")
    assert (outcome.status, outcome.reason, outcome.rows) == (status, reason, ())


def test_alpaca_classification_of_broken_payloads() -> None:
    assert us_sources.classify_alpaca_movers(None, "up").reason == "bad_response"
    assert us_sources.classify_alpaca_movers({"gainers": []}, "up").status == "empty"
    assert (
        us_sources.classify_alpaca_movers({"unexpected": 1}, "up").reason
        == "unparseable"
    )
    assert us_sources.classify_alpaca_most_actives({}).reason == "unparseable"


# ------------------------------------------------------------ asset classes --


@pytest.mark.parametrize(
    ("master", "expected"),
    [
        (us_sources.SymbolMaster(security_type="STOCK"), "common_stock"),
        (us_sources.SymbolMaster(security_type="REIT"), "common_stock"),
        (us_sources.SymbolMaster(security_type="ETF"), "etf"),
        (
            us_sources.SymbolMaster(security_type="ETF", leverage_factor=Decimal("3")),
            "leveraged_etf",
        ),
        (
            us_sources.SymbolMaster(security_type="ETF", leverage_factor=Decimal("-1")),
            "leveraged_etf",
        ),
        (
            us_sources.SymbolMaster(security_type="STOCK_WARRANTS"),
            "right_or_warrant",
        ),
        (
            us_sources.SymbolMaster(security_type=None, is_common_stock=True),
            "common_stock",
        ),
        (us_sources.SymbolMaster(security_type=None, is_common_stock=False), "etf"),
        (
            us_sources.SymbolMaster(
                security_type=None,
                is_common_stock=False,
                name_en="Acme Acquisition Warrants",
            ),
            "right_or_warrant",
        ),
        (
            us_sources.SymbolMaster(
                security_type=None,
                is_common_stock=False,
                name_en="Daily 3x Bull Fund",
            ),
            "leveraged_etf",
        ),
        (us_sources.SymbolMaster(), "unknown"),
        (us_sources.SymbolMaster(security_type="SOMETHING_NEW"), "unknown"),
    ],
)
def test_asset_type_is_a_closed_mapping(
    master: us_sources.SymbolMaster, expected: str
) -> None:
    assert us_sources.derive_asset_type(master) == expected


def test_credentials_are_read_from_the_paper_data_pairs_only() -> None:
    assert us_sources.alpaca_data_credentials({}) is None
    assert us_sources.alpaca_data_credentials(
        {
            us_sources.ALPACA_KEY_ENV: " key ",
            us_sources.ALPACA_SECRET_ENV: "secret",
        }
    ) == ("key", "secret")
    with pytest.raises(ValueError) as excinfo:
        us_sources.alpaca_data_credentials(
            {us_sources.ALPACA_KEY_ENV: " ", us_sources.ALPACA_SECRET_ENV: "s\n"}
        )
    assert us_sources.ALPACA_KEY_ENV in str(excinfo.value)
    assert "s\n" not in str(excinfo.value)


def test_alpaca_client_refuses_another_host() -> None:
    with pytest.raises(ValueError):
        us_sources.AlpacaScreenerClient(
            "key", "secret", base_url="https://paper-api.alpaca.markets"
        )


# ------------------------------------------------------------ session guard --


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        (US_PRE, "us_pre"),
        (US_REGULAR, "us_regular"),
        (US_POST, "us_post"),
        (UNKNOWN, None),
    ],
)
def test_session_labels_come_from_the_segment(segment, expected) -> None:
    assert us_intraday.us_session_label(PRE, segment_of=lambda now: segment) == expected


def test_session_label_uses_the_6145_segment_by_default() -> None:
    # 04:30 ET on an XNYS session is pre-market; the calendar decides the rest.
    assert us_intraday.us_session_label(PRE) == "us_pre"


def test_trading_date_is_the_et_calendar_date() -> None:
    assert us_intraday.us_trading_date(PRE) == dt.date(2026, 11, 25)
    # 19:55 ET is still that ET day (the trading day rolls at 20:00 ET)
    late = dt.datetime(2026, 11, 25, 19, 55, tzinfo=ET)
    assert us_intraday.us_trading_date(late) == dt.date(2026, 11, 25)


def test_surface_names_carry_source_session_and_side() -> None:
    assert (
        us_intraday.surface_key(us_sources.SOURCE_KIS, "us_pre", "down")
        == "us:kis_overseas_ranking:us_pre:down"
    )


# ------------------------------------------------------------------ run (fakes) --


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class MemoryRepo:
    statuses: dict = {}
    rows: dict = {}

    def __init__(self, session) -> None:
        self._session = session

    async def insert_run_status(
        self, *, run_id, surface, status, reason, row_count, **_
    ) -> bool:
        key = (run_id, surface)
        if key in self.statuses:
            return False
        self.statuses[key] = (status, reason, row_count)
        return True

    async def insert_momentum(self, payload) -> bool:
        key = (payload.snapshot_at, payload.surface, payload.symbol)
        if key in self.rows:
            return False
        self.rows[key] = payload
        return True


class FakeAlpaca:
    def __init__(self, movers=None, actives=None, error: Exception | None = None):
        self.movers = movers if movers is not None else _movers()
        self.actives = actives if actives is not None else _actives()
        self.error = error
        self.calls: list[str] = []

    async def screener_movers(self, *, top: int):
        self.calls.append("movers")
        if self.error is not None:
            raise self.error
        return self.movers

    async def screener_most_actives(self, *, top: int):
        self.calls.append("actives")
        if self.error is not None:
            raise self.error
        return self.actives


class FakeKis:
    """NAS carries the fixture; every other exchange answers with no rows."""

    def __init__(self, down=None, error: Exception | None = None):
        self.down = down if down is not None else _kis_down()
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def inquire_overseas_updown_rate(
        self, exchange_code, *, direction, nday="0", vol_rang="0", keyb=""
    ):
        self.calls.append((exchange_code, direction))
        if self.error is not None:
            raise self.error
        if direction == us_sources.KIS_GUBN_DOWN and exchange_code == "NAS":
            return self.down
        return {"output1": {"stat": "R"}, "output2": []}


ETHER_MASTER = {
    "NVDA": us_sources.SymbolMaster(security_type="STOCK"),
    "AMD": us_sources.SymbolMaster(security_type="STOCK"),
    "DELL": us_sources.SymbolMaster(security_type="STOCK"),
    "URBN": us_sources.SymbolMaster(security_type="STOCK"),
    "QQQ": us_sources.SymbolMaster(security_type="ETF"),
    "SMH": us_sources.SymbolMaster(security_type="ETF"),
}


async def _asset_types(symbols):
    return {s: ETHER_MASTER[s] for s in symbols if s in ETHER_MASTER}


@pytest.fixture(autouse=True)
def _memory_repo(monkeypatch):
    MemoryRepo.statuses = {}
    MemoryRepo.rows = {}
    monkeypatch.setattr(
        us_intraday, "InvestMomentumEventSnapshotsRepository", MemoryRepo
    )


async def _run(
    *,
    alpaca=None,
    kis=None,
    now: dt.datetime = PRE,
    commit: bool = True,
    segment: str | None = US_PRE,
    asset_types=_asset_types,
):
    session = FakeSession()
    report = await us_intraday.run_us_intraday_movers(
        alpaca=alpaca if alpaca is not None else FakeAlpaca(),
        kis=kis if kis is not None else FakeKis(),
        session=session,  # type: ignore[arg-type]
        now=now,
        commit=commit,
        clock=lambda: now.astimezone(UTC),
        asset_types=asset_types,
        segment_of=(lambda moment: segment) if segment is not None else None,
    )
    return report, session


@pytest.mark.asyncio
async def test_premarket_run_inserts_dell_and_urbn_and_a_replay_inserts_nothing() -> (
    None
):
    report, session = await _run()

    assert report.status == "recorded"
    assert report.trading_date == dt.date(2026, 11, 25)
    surfaces = {line.surface: line for line in report.surfaces}
    assert set(surfaces) == {
        "us:alpaca_screener:us_pre:up",
        "us:alpaca_screener:us_pre:down",
        "us:alpaca_screener:us_pre:active",
        "us:kis_overseas_ranking:us_pre:up",
        "us:kis_overseas_ranking:us_pre:down",
    }
    assert surfaces["us:kis_overseas_ranking:us_pre:up"].status == "empty"
    assert surfaces["us:alpaca_screener:us_pre:active"].source_updated_at == (
        "2026-11-24T21:00:05.987654321Z"
    )
    assert report.failed is False

    inserted = list(MemoryRepo.rows.values())
    dell = next(
        r for r in inserted if r.symbol == "DELL" and r.source == "kis_overseas_ranking"
    )
    assert dell.market == "us"
    assert dell.source == "kis_overseas_ranking"
    assert dell.surface == "us:kis_overseas_ranking:us_pre:down"
    assert dell.trading_date == dt.date(2026, 11, 25)
    assert dell.asset_type == "common_stock"
    assert dell.trade_value == Decimal("118.42") * 604000
    assert dell.volume == 604000
    assert dell.change_rate == Decimal("-7.45")
    assert dell.first_received_at is not None
    assert dell.raw_payload["trade_value_basis"] == "price_times_volume"
    urbn = next(
        r for r in inserted if r.symbol == "URBN" and r.source == "kis_overseas_ranking"
    )
    assert urbn.asset_type == "common_stock"
    assert urbn.trade_value == Decimal("43.11") * 1310
    # plus Alpaca rows for the same snapshot and the four actives
    assert {"NVDA", "AMD", "QQQ", "SMH"} <= {r.symbol for r in inserted}
    qqq = next(r for r in inserted if r.symbol == "QQQ")
    assert qqq.surface == "us:alpaca_screener:us_pre:active"
    assert qqq.asset_type == "etf"
    assert qqq.trade_value is None
    assert qqq.field_reasons["price"] == "not_in_payload"
    assert session.commits == 5

    count = len(MemoryRepo.rows)
    replay, _ = await _run()
    assert len(MemoryRepo.rows) == count
    assert all(line.already_recorded for line in replay.surfaces)


@pytest.mark.asyncio
async def test_a_symbol_outside_the_master_is_unknown_with_a_reason() -> None:
    kis = FakeKis(
        down={
            "output1": {"stat": "R"},
            "output2": [
                {
                    "symb": "NEWCO",
                    "last": "10.00",
                    "tvol": "100",
                    "rate": "5.0",
                    "rank": "1",
                }
            ],
        }
    )
    await _run(kis=kis)
    row = next(
        r
        for r in MemoryRepo.rows.values()
        if r.symbol == "NEWCO" and r.source == "kis_overseas_ranking"
    )
    assert row.asset_type == "unknown"
    assert row.field_reasons["asset_type"] == "not_in_symbol_universe"
    assert row.trade_value == Decimal("1000.00")


@pytest.mark.asyncio
async def test_a_source_error_records_the_run_and_writes_no_rows() -> None:
    report, _ = await _run(alpaca=FakeAlpaca(error=httpx.ConnectError("down")))

    surfaces = {line.surface: line for line in report.surfaces}
    for side in ("up", "down", "active"):
        line = surfaces[f"us:alpaca_screener:us_pre:{side}"]
        assert (line.status, line.reason, line.row_count) == ("error", "transport", 0)
    assert MemoryRepo.statuses
    sources = {payload.source for payload in MemoryRepo.rows.values()}
    assert sources == {"kis_overseas_ranking"}
    assert report.failed is True


@pytest.mark.asyncio
async def test_a_missing_reader_is_source_unavailable_never_empty() -> None:
    session = FakeSession()
    report = await us_intraday.run_us_intraday_movers(
        alpaca=None,
        kis=None,
        session=session,  # type: ignore[arg-type]
        now=PRE,
        commit=True,
        clock=lambda: PRE.astimezone(UTC),
        asset_types=_asset_types,
        segment_of=lambda moment: US_PRE,
    )
    assert report.failed is True
    assert all(line.status == "error" for line in report.surfaces)
    assert all(line.reason == "source_unavailable" for line in report.surfaces)
    assert MemoryRepo.rows == {}
    assert all(status == "error" for status, _, _ in MemoryRepo.statuses.values())


@pytest.mark.asyncio
async def test_outside_every_session_window_nothing_is_fetched_or_written() -> None:
    alpaca, kis = FakeAlpaca(), FakeKis()
    report, _ = await _run(alpaca=alpaca, kis=kis, segment=UNKNOWN)
    assert report.status == "outside_session"
    assert report.surfaces == []
    assert alpaca.calls == [] and kis.calls == []
    assert MemoryRepo.rows == {} and MemoryRepo.statuses == {}


@pytest.mark.asyncio
async def test_dry_run_reports_without_writing() -> None:
    report, session = await _run(commit=False)
    assert report.status == "dry_run"
    assert MemoryRepo.rows == {} and MemoryRepo.statuses == {}
    assert session.commits == 0
    assert sum(line.row_count for line in report.surfaces) > 0


@pytest.mark.asyncio
async def test_asset_lookup_failure_is_reported_as_unavailable_not_absent() -> None:
    async def boom(symbols):
        raise RuntimeError("master read failed")

    await _run(asset_types=boom)
    dell = next(
        r
        for r in MemoryRepo.rows.values()
        if r.symbol == "DELL" and r.source == "kis_overseas_ranking"
    )
    assert dell.asset_type == "unknown"
    assert dell.field_reasons["asset_type"] == "unavailable"


# --------------------------------------------------------------- static guard --

_NEW_FILES = (
    "app/services/invest_momentum_events/us_sources.py",
    "app/services/invest_momentum_events/us_intraday.py",
    "scripts/us_intraday_movers.py",
)
_FORBIDDEN_IMPORTS = (
    "app.services.order_proposals",
    "app.services.execution_ledger",
    "app.services.kis_trading_service",
    "app.services.brokers",
    "app.services.brokers.kis",
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


def test_us_modules_import_no_order_ledger_approval_or_broker_code() -> None:
    for relative in _NEW_FILES:
        for module in _imports(REPO / relative):
            allowed = relative == "scripts/us_intraday_movers.py" and module in {
                "app.services.brokers.kis.client"
            }
            if allowed:
                continue
            assert not module.startswith(_FORBIDDEN_IMPORTS), (relative, module)


def test_us_modules_never_write_the_pick_log_or_register_a_schedule() -> None:
    for relative in _NEW_FILES:
        text = (REPO / relative).read_text(encoding="utf-8")
        for token in ("screener_pick_log", "broker.task", "app.core.scheduler"):
            assert token not in text, (relative, token)
