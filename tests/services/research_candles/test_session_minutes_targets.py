"""hk 6145: target selection (holdings > fixed > screener snapshot top N > pick log)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.research_candles import session_minutes_collector as sc

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)


def _preset(preset_id: str, availability: str = "active"):
    return SimpleNamespace(id=preset_id, availability=availability)


class FakeSnapshot:
    def __init__(self, by_preset: dict[str, list[str]], fail: set[str] | None = None):
        self.by_preset = by_preset
        self.fail = fail or set()
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        preset = kwargs["preset"]
        if preset in self.fail:
            raise RuntimeError("detail that must not leak")
        rows = [
            {"symbol": symbol, "rank": index}
            for index, symbol in enumerate(self.by_preset.get(preset, []), start=1)
        ]
        return {"results": rows[: kwargs["limit"]]}


# ------------------------------------------------------ snapshot top symbols --


@pytest.mark.asyncio
async def test_top_n_of_every_available_preset_is_unioned_by_best_rank() -> None:
    snapshot = FakeSnapshot(
        {
            "alpha": ["000001", "000002", "000003"],
            "beta": ["000009", "000001", "000004"],
            "pending": ["000099"],
        }
    )
    presets = lambda market: [  # noqa: E731
        _preset("alpha"),
        _preset("beta"),
        _preset("pending", "data_pending"),
        _preset("nope", "unsupported"),
    ]
    symbols, errors = await sc.snapshot_top_symbols(
        market="kr", top_n=2, snapshot_fn=snapshot, presets_fn=presets
    )
    # only available presets were called, each with limit=N and market, nothing else
    assert [c["preset"] for c in snapshot.calls] == ["alpha", "beta"]
    assert all(set(c) == {"preset", "market", "limit"} for c in snapshot.calls)
    assert all(c["limit"] == 2 and c["market"] == "kr" for c in snapshot.calls)
    assert errors == []
    # rank 1: 000001 (alpha), 000009 (beta); rank 2: 000002 (alpha), 000001 already seen at rank 1
    assert symbols == ["000001", "000009", "000002"]
    assert "000003" not in symbols and "000004" not in symbols  # beyond top 2
    assert "000099" not in symbols  # data_pending preset is not used


@pytest.mark.asyncio
async def test_a_failing_preset_is_reported_and_the_rest_still_count() -> None:
    snapshot = FakeSnapshot({"ok": ["000010"]}, fail={"bad"})
    symbols, errors = await sc.snapshot_top_symbols(
        market="kr",
        top_n=5,
        snapshot_fn=snapshot,
        presets_fn=lambda market: [_preset("bad"), _preset("ok")],
    )
    assert symbols == ["000010"]
    assert errors == [
        {"origin": "screener_snapshot", "preset": "bad", "error_class": "RuntimeError"}
    ]


@pytest.mark.asyncio
async def test_default_path_calls_the_impl_not_the_registered_tool(monkeypatch) -> None:
    from app.mcp_server.tooling import screener_snapshot_tool

    snapshot = FakeSnapshot({})
    monkeypatch.setattr(screener_snapshot_tool, "screen_stocks_snapshot_impl", snapshot)
    symbols, errors = await sc.snapshot_top_symbols(market="us", top_n=3)
    assert symbols == [] and errors == []
    assert snapshot.calls, "no active US preset was queried"
    assert all(set(c) == {"preset", "market", "limit"} for c in snapshot.calls)
    assert all(c["market"] == "us" and c["limit"] == 3 for c in snapshot.calls)


# --------------------------------------------------------- cap and priority --


def test_cap_keeps_priority_order_and_reports_cut_per_origin() -> None:
    tiers = [
        ("kis_holdings", ["H1", "H2"]),
        ("manual_holdings", ["H2", "H3"]),  # H2 stays with the higher tier
        ("fixed_list", ["F1"]),
        ("screener_snapshot", ["S1", "S2", "S3"]),
        ("screener_pick_log", ["P1", "S1"]),
    ]
    kept, truncated = sc.select_with_cap(tiers, 5)
    assert kept == ["H1", "H2", "H3", "F1", "S1"]
    assert truncated == {"screener_snapshot": 2, "screener_pick_log": 1}
    kept, truncated = sc.select_with_cap(tiers, 2)
    assert kept == ["H1", "H2"] and sum(truncated.values()) == 6
    kept, truncated = sc.select_with_cap(tiers, 100)
    assert len(kept) == 8 and truncated == {}


class _Result:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _RecordingSession:
    """Dispatches by statement text: pick log, intraday movers, minute rows today."""

    def __init__(
        self,
        pick_symbols,
        mover_rows=(),
        have_today=(),
        mover_fail: bool = False,
    ):
        self.pick_symbols = pick_symbols
        self.mover_rows = list(mover_rows)
        self.have_today = list(have_today)
        self.mover_fail = mover_fail
        self.statements: list[str] = []
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        text = str(statement)
        self.statements.append(text)
        if "invest_momentum_event_snapshots" in text:
            if self.mover_fail:
                raise RuntimeError("detail that must not leak")
            return _Result(self.mover_rows)
        if "kr_candles_1m" in text:
            return _Result(self.have_today)
        return _Result(self.pick_symbols)

    async def rollback(self):
        self.rollbacks += 1


class _Kis:
    async def fetch_my_stocks(self):
        return [{"pdno": "000001"}, {"pdno": "000002"}]

    async def fetch_my_us_stocks(self):  # pragma: no cover
        return []


@pytest.mark.asyncio
async def test_build_targets_includes_snapshot_top_n_respects_cap_and_writes_no_pick_log(
    monkeypatch,
) -> None:
    import app.services.manual_holdings_service as manual_module

    class _Manual:
        def __init__(self, session):
            pass

        async def get_holdings_by_user(self, user_id, market_type=None):
            return [SimpleNamespace(ticker="000003")]

    monkeypatch.setattr(manual_module, "ManualHoldingsService", _Manual)
    session = _RecordingSession(["000500", "000501", "000502"])
    snapshot = FakeSnapshot({"alpha": ["000100", "000101", "000102", "000103"]})
    targets = await sc.build_targets(
        market="kr",
        session=session,  # type: ignore[arg-type]
        kis=_Kis(),  # type: ignore[arg-type]
        user_id=1,
        fixed_symbols_raw="000200",
        now=NOW,
        screener_top_n=3,
        max_symbols=7,
        snapshot_fn=snapshot,
        presets_fn=lambda market: [_preset("alpha")],
    )
    # holdings (3) + fixed (1) + snapshot top 3 = 7 -> pick log is the part that is cut
    assert targets.symbols == sorted(
        ["000001", "000002", "000003", "000200", "000100", "000101", "000102"]
    )
    assert targets.truncated == {"screener_pick_log": 3}
    assert targets.by_origin["screener_snapshot"] == 3
    assert targets.errors == []
    # pick log is only ever read (SELECT) — zero writes
    assert session.statements and all(
        s.lstrip().upper().startswith("SELECT") for s in session.statements
    )
    assert all("INSERT" not in s.upper() for s in session.statements)
    assert session.rollbacks == 0
    assert "intraday_movers" not in targets.by_origin and targets.backfill == []


def test_snapshot_selection_cannot_write_the_pick_log() -> None:
    collector = (
        REPO / "app/services/research_candles/session_minutes_collector.py"
    ).read_text()
    for forbidden in (
        "screener_tool_pick_log",
        "analysis_registration",
        "observation_sink",
        "INSERT INTO review",
    ):
        assert forbidden not in collector, forbidden
    impl = (REPO / "app/mcp_server/tooling/screener_snapshot_tool.py").read_text()
    assert (
        "screener_tool_pick_log" not in impl
        and "INSERT" not in impl.upper().replace("INSERTED", "")
    )
    # the only module that wires the observer is the registration layer (hard rule 13)
    wired = [
        str(p.relative_to(REPO))
        for p in (REPO / "app").rglob("*.py")
        if "screener_tool_pick_log" in p.read_text()
        and p.name != "screener_tool_pick_log.py"
    ]
    assert wired == ["app/mcp_server/tooling/analysis_registration.py"]


# ----------------------------------------------- intraday_movers origin (hk 6154)


def _mover(symbol: str):
    return SimpleNamespace(
        symbol=symbol,
        name=None,
        market="kr",
        surface="domestic_market_stock_default",
        snapshot_at=NOW,
        trade_type="KRX",
        market_type="ALL",
        order_type="up",
        rank=1,
        change_rate=None,
        trade_value=None,
    )


class _NoHoldingsKis:
    async def fetch_my_stocks(self):
        return []

    async def fetch_my_us_stocks(self):  # pragma: no cover
        return []


async def _kr_targets(monkeypatch, session, **kwargs):
    import app.services.manual_holdings_service as manual_module

    class _Manual:
        def __init__(self, session):
            pass

        async def get_holdings_by_user(self, user_id, market_type=None):
            return []

    monkeypatch.setattr(manual_module, "ManualHoldingsService", _Manual)
    params = {
        "market": "kr",
        "session": session,
        "kis": _NoHoldingsKis(),
        "user_id": 1,
        "fixed_symbols_raw": None,
        "now": NOW,
        "screener_top_n": 3,
        "max_symbols": 150,
        "snapshot_fn": FakeSnapshot({}),
        "presets_fn": lambda market: [],
    }
    params.update(kwargs)
    return await sc.build_targets(**params)


@pytest.mark.asyncio
async def test_intraday_movers_origin_is_reported_and_backfills_only_new_symbols(
    monkeypatch,
) -> None:
    session = _RecordingSession(
        [], mover_rows=[_mover("000700"), _mover("000701")], have_today=["000701"]
    )
    targets = await _kr_targets(monkeypatch, session)
    assert targets.by_origin["intraday_movers"] == 2
    assert targets.symbols == ["000700", "000701"]
    assert targets.backfill == ["000700"]  # 000701 already has rows today
    assert targets.errors == []
    assert all(s.lstrip().upper().startswith("SELECT") for s in session.statements)


@pytest.mark.asyncio
async def test_intraday_movers_outrank_the_pick_log_under_the_cap(monkeypatch) -> None:
    # hk 6299: movers are the second tier, ahead of the pick log (was the last tier).
    # cap 2: the fixed list takes one slot; the first mover (symbol order, same
    # capture time and rank) takes the other; the second mover and the pick-log
    # symbol are cut, each with its tier.
    session = _RecordingSession(
        ["000500"], mover_rows=[_mover("000700"), _mover("000701")]
    )
    targets = await _kr_targets(
        monkeypatch, session, fixed_symbols_raw="000200", max_symbols=2
    )
    assert targets.symbols == ["000200", "000700"]
    assert targets.truncated == {"intraday_movers": 1, "screener_pick_log": 1}
    assert targets.cuts == [
        {"symbol": "000701", "tier": "intraday_movers", "reason": "budget_cut"},
        {"symbol": "000500", "tier": "screener_pick_log", "reason": "budget_cut"},
    ]
    assert targets.backfill == ["000700"]  # the kept mover has no row yet today


@pytest.mark.asyncio
async def test_intraday_movers_query_failure_is_reported_and_the_run_continues(
    monkeypatch,
) -> None:
    session = _RecordingSession(["000500"], mover_fail=True)
    targets = await _kr_targets(monkeypatch, session)
    assert targets.symbols == ["000500"]
    assert targets.errors == [
        {"origin": "intraday_movers", "error_class": "RuntimeError"}
    ]
    assert session.rollbacks == 1
    assert "detail that must not leak" not in str(targets.errors)


@pytest.mark.asyncio
async def test_us_targets_query_us_intraday_movers_only(monkeypatch) -> None:
    import app.services.manual_holdings_service as manual_module

    class _Manual:
        def __init__(self, session):
            pass

        async def get_holdings_by_user(self, user_id, market_type=None):
            return []

    monkeypatch.setattr(manual_module, "ManualHoldingsService", _Manual)
    calls: list[tuple[str, object]] = []

    async def _captures(session, trading_date, market="kr"):
        calls.append((market, trading_date))
        return [_mover("DELL")]

    from app.services.invest_momentum_events import intraday as movers

    monkeypatch.setattr(movers, "first_captures", _captures)
    session = _RecordingSession([], mover_rows=[_mover("000700")])
    targets = await sc.build_targets(
        market="us",
        session=session,  # type: ignore[arg-type]
        kis=_Kis(),  # type: ignore[arg-type]
        user_id=1,
        fixed_symbols_raw=None,
        now=NOW,
        snapshot_fn=FakeSnapshot({}),
        presets_fn=lambda market: [],
    )
    # hk 6510: the US run reads its own captures (market='us'), never the KR ones.
    assert targets.by_origin["intraday_movers"] == 1
    assert "DELL" in targets.symbols
    assert calls and calls[0][0] == "us"
