"""hk 5622: outer pick-log observer for ``screen_stocks`` / ``screen_stocks_snapshot``."""

from __future__ import annotations

import ast
import copy
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.models.screener_pick_log import ScreenerPickLog
from app.services import screener_tool_pick_log as mod
from app.services.screener_pick_log import ScreenerPickRow

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)  # 08:00 KST next day
_SHA = "c" * 64


def _screen_result() -> dict[str, Any]:
    return {
        "results": [
            {"symbol": "005930", "name": "삼성전자", "price": 75000.0, "rsi": 31.2},
            {"symbol": "000660", "name": "SK하이닉스", "price": 201500.5},
        ],
        "total_count": 57,
        "filters_applied": {
            "market": "kr",
            "sort_by": "volume",
            "sort_order": "desc",
            "limit": 50,
        },
        "meta": {
            "source": "kis",
            "halted_suspect_excluded": [
                {"symbol": "000880", "frozen_sessions": 3, "reasons": ["zero_volume"]}
            ],
        },
        "timestamp": "2026-11-20T22:59:00+00:00",
    }


def _snapshot_payload() -> dict[str, Any]:
    return {
        "presetId": "multi",
        "presets": ["double_buy", "support_proximity"],
        "results": [
            {
                "rank": 1,
                "symbol": "BRK.B",
                "market": "us",
                "priceLabel": "$1,234.50",
                "matchedPresets": ["double_buy", "support_proximity"],
            },
            {
                "rank": 2,
                "symbol": "AAPL",
                "market": "us",
                "priceLabel": "-",
                "matchedPresets": ["support_proximity"],
            },
        ],
        "pagination": {"total_available": 9, "offset": 4, "limit": 2},
    }


_OBSERVATION: dict[str, Any] = {
    "dropped_rows": [
        (
            {
                "rank": 3,
                "symbol": "TSLA",
                "market": "us",
                "priceLabel": "$250.00",
                "matchedPresets": ["double_buy"],
            },
            "below_min_market_cap",
        )
    ],
    "freshness_by_preset": {
        "double_buy": {
            "fetchedAt": "2026-11-20T22:00:00+00:00",
            "dataState": "fresh",
            "primary": {"snapshotDate": "2026-11-19", "dataState": "fresh"},
        },
        "support_proximity": {
            "fetchedAt": "2026-11-20T22:00:00+00:00",
            "dataState": "stale",
            "primary": {"snapshotDate": "2026-11-18", "dataState": "stale"},
        },
    },
    "preset_ids": ["double_buy", "support_proximity"],
}


class _Sink:
    def __init__(self) -> None:
        self.batches: list[list[ScreenerPickRow]] = []

    async def __call__(self, rows: Any) -> None:
        self.batches.append(list(rows))


def test_screen_stocks_rows_are_a_full_record() -> None:
    call_id = uuid.uuid4()
    rows = mod.extract_screen_stocks_rows(
        _screen_result(),
        request={"market": "kr", "limit": 50, "sort_by": None},
        call_id=call_id,
        now=_NOW,
        code_sha256=_SHA,
    )
    by_symbol = {r.symbol: r for r in rows}
    assert set(by_symbol) == {"005930", "000660", "000880"}
    a = by_symbol["005930"]
    assert (a.admission, a.admission_reason, a.rank, a.selection_seq) == (
        "admitted",
        "returned_to_session",
        1,
        1,
    )
    assert a.decision_price_text == "75000.0"
    assert by_symbol["000660"].decision_price_text == "201500.5"
    assert a.collection_version == "screener-tools-v1"
    assert a.call_id == call_id and a.market == "kr"
    assert a.source == "screen_stocks" and a.source_status == "ok"
    assert a.source_sort_by == "volume" and a.source_limit == 50
    assert a.data_asof == "2026-11-20T22:59:00+00:00"
    assert a.raw_row == _screen_result()["results"][0]
    h = by_symbol["000880"]
    assert (h.admission, h.admission_reason) == ("not_admitted", "halted_suspect")
    assert h.rank is None and h.decision_price_text is None
    assert {r.call_id for r in rows} == {call_id}
    assert a.recorded_at_kst.startswith("2026-11-21T08:00")


def test_screen_stocks_market_aliases_and_unrecorded_markets() -> None:
    kosdaq = {**_screen_result(), "filters_applied": {"market": "kosdaq"}}
    rows = mod.extract_screen_stocks_rows(
        kosdaq, request={}, call_id=uuid.uuid4(), code_sha256=_SHA
    )
    assert {r.market for r in rows} == {"kr"}
    crypto = {**_screen_result(), "filters_applied": {"market": "crypto"}}
    assert (
        mod.extract_screen_stocks_rows(
            crypto, request={}, call_id=uuid.uuid4(), code_sha256=_SHA
        )
        == []
    )


def test_screen_stocks_us_symbols_use_db_format_and_floats_never_leak() -> None:
    result = {
        "results": [{"symbol": "BRK-B", "price": float("nan"), "x": float("inf")}],
        "filters_applied": {"market": "us"},
    }
    (row,) = mod.extract_screen_stocks_rows(
        result, request={}, call_id=uuid.uuid4(), code_sha256=_SHA
    )
    assert row.symbol == "BRK.B"
    assert row.decision_price_text is None
    assert row.raw_row == {"symbol": "BRK-B", "price": None, "x": None}


def test_snapshot_rows_one_per_matched_preset_with_drops_and_freshness() -> None:
    call_id = uuid.uuid4()
    rows = mod.extract_snapshot_rows(
        _snapshot_payload(),
        request={"market": "us", "limit": 2, "offset": 4},
        dropped_rows=_OBSERVATION["dropped_rows"],
        freshness_by_preset=_OBSERVATION["freshness_by_preset"],
        preset_ids=_OBSERVATION["preset_ids"],
        call_id=call_id,
        now=_NOW,
        code_sha256=_SHA,
    )
    key = {(r.source, r.symbol): r for r in rows}
    assert set(key) == {
        ("screen_stocks_snapshot:double_buy", "BRK.B"),
        ("screen_stocks_snapshot:support_proximity", "BRK.B"),
        ("screen_stocks_snapshot:support_proximity", "AAPL"),
        ("screen_stocks_snapshot:double_buy", "TSLA"),
    }
    first = key[("screen_stocks_snapshot:double_buy", "BRK.B")]
    assert (first.admission, first.rank, first.selection_seq) == ("admitted", 1, 5)
    assert first.decision_price_text == "1234.50"
    assert first.data_asof == "2026-11-19" and first.source_preset == "double_buy"
    second = key[("screen_stocks_snapshot:support_proximity", "BRK.B")]
    assert second.rank is None  # the merged row keeps only the first preset's rank
    # price/provenance belong to the first matched preset on every row carrying it
    assert second.data_asof == "2026-11-19"
    assert second.decision_price_text == "1234.50"
    assert second.source_params["price_provenance_preset"] == "double_buy"
    assert (
        second.source_params["matched_preset_freshness"]["primary"]["snapshotDate"]
        == "2026-11-18"
    )
    assert second.call_context is not None
    assert second.call_context["freshness"]["support_proximity"]["dataState"] == "stale"
    assert (
        key[("screen_stocks_snapshot:support_proximity", "AAPL")].decision_price_text
        is None
    )
    dropped = key[("screen_stocks_snapshot:double_buy", "TSLA")]
    assert (dropped.admission, dropped.admission_reason) == (
        "not_admitted",
        "below_min_market_cap",
    )
    assert dropped.selection_seq is None
    assert {r.call_id for r in rows} == {call_id}


def test_rows_satisfy_the_table_constraints_in_python() -> None:
    import re

    allowed = {"admitted", "not_admitted", "dropped_preselection"}
    rows = mod.extract_screen_stocks_rows(
        _screen_result(), request={}, call_id=uuid.uuid4(), code_sha256=_SHA
    ) + mod.extract_snapshot_rows(
        _snapshot_payload(),
        request={"market": "us"},
        dropped_rows=_OBSERVATION["dropped_rows"],
        freshness_by_preset=_OBSERVATION["freshness_by_preset"],
        preset_ids=_OBSERVATION["preset_ids"],
        call_id=uuid.uuid4(),
        code_sha256=_SHA,
    )
    for r in rows:
        assert r.admission in allowed and (r.admission_reason or "").strip()
        assert r.source.strip() and r.symbol.strip() and r.market in {"kr", "us"}
        assert r.source_status in {"ok", "empty", "stale_dropped", "error"}
        assert r.rank is None or r.rank >= 1
        assert r.decision_price_text is None or re.fullmatch(
            r"-?[0-9]+(\.[0-9]+)?", r.decision_price_text
        )
        assert re.fullmatch(r"[0-9a-f]{64}", r.fanout_code_sha256)
        assert isinstance(r.source_params, dict)
    assert ScreenerPickLog.__table__.c.collection_version.nullable


@pytest.mark.asyncio
async def test_gate_off_writes_nothing_and_returns_the_same_object() -> None:
    sink = _Sink()
    result = _screen_result()
    out = await mod.record_screen_stocks_picks(
        result, request={"market": "kr"}, enabled=False, write_rows=sink
    )
    assert out is result and sink.batches == []
    payload = _snapshot_payload()
    out2 = await mod.record_snapshot_picks(
        payload,
        request={"market": "us"},
        observation=_OBSERVATION,
        enabled=False,
        write_rows=sink,
    )
    assert out2 is payload and sink.batches == []


@pytest.mark.asyncio
async def test_gate_on_writes_once_and_returns_pick_log_call_id() -> None:
    sink = _Sink()
    result = _screen_result()
    before = copy.deepcopy(result)
    out = await mod.record_screen_stocks_picks(
        result,
        request={"market": "kr"},
        enabled=True,
        write_rows=sink,
        now=_NOW,
        code_sha256=_SHA,
    )
    assert len(sink.batches) == 1 and len(sink.batches[0]) == 3
    call_id = sink.batches[0][0].call_id
    assert out == {**before, "pick_log_call_id": str(call_id)}
    assert result == before  # the tool's own object is not mutated

    snap_sink = _Sink()
    payload = _snapshot_payload()
    out2 = await mod.record_snapshot_picks(
        payload,
        request={"market": "us"},
        observation=_OBSERVATION,
        enabled=True,
        write_rows=snap_sink,
        now=_NOW,
        code_sha256=_SHA,
    )
    assert len(snap_sink.batches) == 1 and len(snap_sink.batches[0]) == 4
    assert out2["pick_log_call_id"] == str(snap_sink.batches[0][0].call_id)
    assert "pick_log_call_id" not in payload


@pytest.mark.asyncio
async def test_no_rows_means_no_call_id() -> None:
    sink = _Sink()
    empty = {"results": [], "filters_applied": {"market": "kr"}}
    out = await mod.record_screen_stocks_picks(
        empty, request={}, enabled=True, write_rows=sink
    )
    assert out is empty and sink.batches == []
    err = {"error": "preset or presets must not be empty", "results": []}
    out2 = await mod.record_snapshot_picks(
        err, request={"market": "kr"}, observation={}, enabled=True, write_rows=sink
    )
    assert out2 is err and sink.batches == []


@pytest.mark.asyncio
async def test_recorder_failure_is_fail_open() -> None:
    async def boom(_rows: Any) -> None:
        raise RuntimeError("db down")

    result = _screen_result()
    out = await mod.record_screen_stocks_picks(
        result, request={"market": "kr"}, enabled=True, write_rows=boom
    )
    assert out is result and "pick_log_call_id" not in out
    payload = _snapshot_payload()
    out2 = await mod.record_snapshot_picks(
        payload,
        request={"market": "us"},
        observation=_OBSERVATION,
        enabled=True,
        write_rows=boom,
    )
    assert out2 is payload


@pytest.mark.asyncio
async def test_malformed_results_are_fail_open_not_raised() -> None:
    sink = _Sink()
    for bad in (None, "x", {"results": "nope"}, {"results": [None, 3]}):
        out = await mod.record_screen_stocks_picks(
            bad, request={"market": "kr"}, enabled=True, write_rows=sink
        )
        assert out is bad
    assert sink.batches == []


# --- registration wiring -----------------------------------------------------


class _FakeMCP:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def tool(self, *, name: str, **_: Any) -> Any:
        def deco(fn: Any) -> Any:
            self.handlers[name] = fn
            return fn

        return deco


def _register(monkeypatch: pytest.MonkeyPatch, **patches: Any) -> _FakeMCP:
    from app.mcp_server.tooling import analysis_registration as reg

    for name, value in patches.items():
        monkeypatch.setattr(reg, name, value)
    mcp = _FakeMCP()
    reg.register_analysis_tools(mcp)  # type: ignore[arg-type]
    return mcp


@pytest.mark.asyncio
async def test_screen_stocks_tool_gate_off_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SCREENER_PICK_LOG_ENABLED", raising=False)
    payload = _screen_result()

    async def fake_impl(**_: Any) -> dict[str, Any]:
        return payload

    mcp = _register(monkeypatch, screen_stocks_impl=fake_impl)
    assert await mcp.handlers["screen_stocks"]() is payload


@pytest.mark.asyncio
async def test_screen_stocks_tool_records_via_outer_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCREENER_PICK_LOG_ENABLED", "true")
    payload = _screen_result()
    sink = _Sink()

    async def fake_impl(**_: Any) -> dict[str, Any]:
        return payload

    async def fake_write(rows: Any) -> None:
        await sink(rows)

    monkeypatch.setattr(mod, "_default_write", fake_write)
    mcp = _register(monkeypatch, screen_stocks_impl=fake_impl)
    out = await mcp.handlers["screen_stocks"](market="kr", sort_by="volume", limit=50)
    assert len(sink.batches) == 1
    assert out["pick_log_call_id"] == str(sink.batches[0][0].call_id)
    assert {k: v for k, v in out.items() if k != "pick_log_call_id"} == payload
    ctx = sink.batches[0][0].call_context
    assert ctx is not None and ctx["request"]["limit"] == 50
    assert ctx["request"]["sort_by"] == "volume"


@pytest.mark.asyncio
async def test_snapshot_tool_records_and_passes_the_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCREENER_PICK_LOG_ENABLED", "true")
    payload = _snapshot_payload()
    sink = _Sink()

    async def fake_impl(*, observation_sink: dict[str, Any], **_: Any) -> Any:
        observation_sink.update(_OBSERVATION)
        return payload

    async def fake_write(rows: Any) -> None:
        await sink(rows)

    monkeypatch.setattr(mod, "_default_write", fake_write)
    mcp = _register(monkeypatch, screen_stocks_snapshot_impl=fake_impl)
    out = await mcp.handlers["screen_stocks_snapshot"](
        presets=["double_buy", "support_proximity"], market="us", limit=2, offset=4
    )
    assert len(sink.batches[0]) == 4
    assert out["pick_log_call_id"] == str(sink.batches[0][0].call_id)


@pytest.mark.asyncio
async def test_registered_tools_survive_a_recorder_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCREENER_PICK_LOG_ENABLED", "true")
    payload = _snapshot_payload()
    screen = _screen_result()

    async def fake_snap(*, observation_sink: dict[str, Any], **_: Any) -> Any:
        observation_sink.update(_OBSERVATION)
        return payload

    async def fake_screen(**_: Any) -> Any:
        return screen

    async def boom(_rows: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(mod, "_default_write", boom)
    mcp = _register(
        monkeypatch,
        screen_stocks_snapshot_impl=fake_snap,
        screen_stocks_impl=fake_screen,
    )
    assert await mcp.handlers["screen_stocks_snapshot"](preset="double_buy") is payload
    assert await mcp.handlers["screen_stocks"]() is screen


# --- real snapshot builder -> sink ------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_impl_reports_drops_and_freshness_without_changing_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.mcp_server.tooling import screener_snapshot_tool as tool

    rows = [
        {"rank": 1, "symbol": "AAA", "market": "us", "marketCapValue": 5e9},
        {"rank": 2, "symbol": "BBB", "market": "us", "marketCapValue": 1e6},
        {"rank": 3, "symbol": "CCC", "market": "us", "marketCapValue": None},
        {"rank": 4, "symbol": "DDD", "market": "us", "marketCapValue": 9e9},
    ]

    async def fake_build(**kwargs: Any) -> Any:
        assert kwargs["snapshot_only"] is True
        return SimpleNamespace(
            model_dump=lambda mode="json": {
                "results": copy.deepcopy(rows),
                "warnings": [],
                "freshness": {"dataState": "fresh", "primary": {"snapshotDate": "d"}},
            }
        )

    class _Ctx:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        fake_build,
    )
    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _Ctx())

    kwargs: dict[str, Any] = {
        "preset": "p",
        "market": "us",
        "exclude_symbols": ["DDD"],
        "min_market_cap": 1e9,
    }
    sink: dict[str, Any] = {}
    with_sink = await tool.screen_stocks_snapshot_impl(**kwargs, observation_sink=sink)
    without = await tool.screen_stocks_snapshot_impl(**kwargs)
    assert with_sink == without  # the sink never changes the response
    assert "dropped_rows" not in with_sink and "pick_log_call_id" not in with_sink
    assert [r["symbol"] for r in with_sink["results"]] == ["AAA"]
    assert [(r["symbol"], why) for r, why in sink["dropped_rows"]] == [
        ("DDD", "exclude_symbols"),
        ("BBB", "below_min_market_cap"),
        ("CCC", "market_cap_missing"),
    ]
    assert sink["preset_ids"] == ["p"]
    assert sink["freshness_by_preset"]["p"]["primary"]["snapshotDate"] == "d"


# --- boundaries --------------------------------------------------------------


def test_only_the_registration_layer_wires_the_observer() -> None:
    root = Path(__file__).resolve().parents[3] / "app"
    importers = {
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "screener_tool_pick_log" in path.read_text(encoding="utf-8")
        and path.name != "screener_tool_pick_log.py"
    }
    assert importers == {"mcp_server/tooling/analysis_registration.py"}


def test_impls_and_fanout_never_import_the_pick_log() -> None:
    tooling = Path(__file__).resolve().parents[3] / "app" / "mcp_server" / "tooling"
    for name in (
        "buy_candidate_fanout.py",
        "analysis_tool_handlers.py",
        "screener_snapshot_tool.py",
        "screener_enrich_tool.py",
    ):
        text = (tooling / name).read_text(encoding="utf-8")
        tree = ast.parse(text)
        imported = {
            n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
        }
        assert not any("pick_log" in m for m in imported), name


# --- hk 5622 review findings -------------------------------------------------


def test_none_jsonb_columns_persist_as_sql_null_not_json_null() -> None:
    from sqlalchemy.dialects import postgresql

    dialect = postgresql.dialect()
    for name in ("raw_row", "gate_features", "call_context"):
        column_type = ScreenerPickLog.__table__.c[name].type
        processor = column_type.bind_processor(dialect)
        # JSON ``null`` would fail the *_object CHECK and roll back the whole batch.
        assert processor is None or processor(None) is None, name
    # both observers really emit gate_features=None
    rows = mod.extract_screen_stocks_rows(
        _screen_result(), request={}, call_id=uuid.uuid4(), code_sha256=_SHA
    )
    assert all(r.gate_features is None for r in rows)


@pytest.mark.asyncio
async def test_stalled_write_is_bounded_and_returns_the_untagged_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    monkeypatch.setattr(mod, "WRITE_TIMEOUT_SECONDS", 0.05)

    async def stall(_rows: Any) -> None:
        await asyncio.sleep(30)

    result = _screen_result()
    out = await asyncio.wait_for(
        mod.record_screen_stocks_picks(
            result, request={"market": "kr"}, enabled=True, write_rows=stall
        ),
        timeout=5,
    )
    assert out is result
    payload = _snapshot_payload()
    out2 = await asyncio.wait_for(
        mod.record_snapshot_picks(
            payload,
            request={"market": "us"},
            observation=_OBSERVATION,
            enabled=True,
            write_rows=stall,
        ),
        timeout=5,
    )
    assert out2 is payload


@pytest.mark.asyncio
async def test_caller_cancellation_still_propagates_through_the_observer() -> None:
    import asyncio

    started = asyncio.Event()

    async def stall(_rows: Any) -> None:
        started.set()
        await asyncio.sleep(30)

    task = asyncio.ensure_future(
        mod.record_screen_stocks_picks(
            _screen_result(), request={"market": "kr"}, enabled=True, write_rows=stall
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_non_finite_max_market_cap_keeps_the_old_empty_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.mcp_server.tooling import screener_snapshot_tool as tool

    async def fake_build(**_: Any) -> Any:
        return SimpleNamespace(
            model_dump=lambda mode="json": {
                "results": [
                    {"rank": 1, "symbol": "AAA", "market": "us", "marketCapValue": 5.0}
                ],
                "warnings": [],
            }
        )

    class _Ctx:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        fake_build,
    )
    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _Ctx())
    out = await tool.screen_stocks_snapshot_impl(
        preset="p", market="us", max_market_cap_eok=float("nan")
    )
    # `cap <= NaN` is false for every row, so the original filter dropped them all.
    assert out["results"] == []
