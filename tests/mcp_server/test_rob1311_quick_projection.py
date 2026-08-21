"""ROB-1311 regression tests for the quick analysis projection boundary."""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

try:
    from app.mcp_server.tooling import analysis_quick
except ImportError:
    # Keep the regression file baseline-compatible: the test-only commit must
    # collect against the pre-ROB-1311 tree and fail in the assertions, not at
    # import time because the new implementation module is absent.
    analysis_quick = None  # type: ignore[assignment]

from app.mcp_server.tooling import analysis_screening
from app.mcp_server.tooling import analysis_tool_handlers as handlers
from app.services.daily_candles.repository import DailyCandleRow, MarketKey


def _projection(symbol: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "market_type": "equity_kr",
        "source": "daily_candles_db",
        "current_price": 75000.0,
        "ohlcv": {
            "open": 74000.0,
            "high": 76000.0,
            "low": 73500.0,
            "close": 75000.0,
            "volume": 1000.0,
        },
        "rsi_14": 45.0,
        "supports": [],
        "resistances": [],
        "data_state": "stale",
        "fallback_source": "daily_candles_db",
        "provider_provenance": [],
    }


@pytest.mark.asyncio
async def test_quick_batch_read_models_attach_history_and_earnings_by_canonical_key() -> (
    None
):
    assert analysis_quick is not None

    class Result:
        def __init__(self, rows: list[object]) -> None:
            self.rows = rows

        def scalars(self) -> Result:
            return self

        def all(self) -> list[object]:
            return self.rows

    history_row = SimpleNamespace(
        symbol="AAPL",
        created_at=datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC),
        intent="buy_review",
        side="buy",
        decision_bucket="new_buy_candidate",
        confidence=0.8,
        rationale="cached decision",
    )
    earnings_row = SimpleNamespace(
        symbol="AAPL",
        event_date=datetime.date.today() + datetime.timedelta(days=3),
        time_hint="amc",
        fiscal_quarter=2,
        fiscal_year=2026,
        status="scheduled",
        raw_payload_json={"eps_estimate": 1.2},
    )

    class Session:
        def __init__(self) -> None:
            self.statements: list[str] = []

        async def execute(
            self, statement: object, params: object | None = None
        ) -> Result:
            sql = str(statement)
            self.statements.append(sql)
            if "investment_report_items" in sql:
                return Result([history_row])
            if "market_events" in sql:
                return Result([earnings_row])
            return Result([])

    session = Session()
    symbols = [("AAPL", "equity_us"), ("MSFT", "equity_us")]
    history = await analysis_quick._load_decision_history_batch(session, symbols)
    earnings = await analysis_quick._load_earnings_batch(session, symbols)

    assert history["AAPL"]["prior_decisions"][0]["intent"] == "buy_review"
    assert "MSFT" not in history
    assert earnings["AAPL"]["next_earnings"]["d_minus"] == 3
    assert earnings["MSFT"]["has_upcoming"] is False
    assert any("investment_report_items" in sql for sql in session.statements)
    assert any("market_events" in sql for sql in session.statements)


@pytest.mark.asyncio
async def test_quick_uses_batch_projection_instead_of_full_symbol_analyzer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_full_analyzer(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("quick must not invoke the deep single-symbol analyzer")

    async def fake_projection(
        symbols: list[str | int], *, market: str | None
    ) -> dict[str, dict[str, object]]:
        return {str(symbol): _projection(str(symbol)) for symbol in symbols}

    monkeypatch.setattr(analysis_screening, "_analyze_stock_impl", fail_full_analyzer)
    monkeypatch.setattr(
        handlers, "_load_quick_projection_batch", fake_projection, raising=False
    )

    result = await handlers.analyze_stock_batch_impl(
        ["005930", "000660"], market="kr", include_position=False
    )

    assert result["results"]["005930"]["rsi_14"] == 45.0
    assert result["results"]["000660"]["current_price"] == 75000.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_symbol", "market", "canonical"),
    [
        ("aapl", "us", "AAPL"),
        ("aapl", None, "AAPL"),
        ("A196170", None, "196170"),
        ("005930", "kr", "005930"),
        ("KRW-BTC", "crypto", "KRW-BTC"),
    ],
)
async def test_quick_result_keys_use_resolved_canonical_symbols(
    monkeypatch: pytest.MonkeyPatch,
    raw_symbol: str,
    market: str | None,
    canonical: str,
) -> None:
    seen: list[str] = []

    async def fake_projection(
        symbols: list[str | int], *, market: str | None
    ) -> dict[str, dict[str, object]]:
        seen.extend(str(symbol) for symbol in symbols)
        return {canonical: _projection(canonical)}

    monkeypatch.setattr(handlers, "_load_quick_projection_batch", fake_projection)
    result = await handlers.analyze_stock_batch_impl(
        [raw_symbol], market=market, include_position=False
    )

    assert seen == [canonical]
    assert list(result["results"]) == [canonical]
    assert result["results"][canonical]["symbol"] == canonical


@pytest.mark.asyncio
async def test_quick_skips_deep_provider_and_advisory_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_full_analyzer(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("quick must not invoke the deep single-symbol analyzer")

    async def fake_projection(
        symbols: list[str | int], *, market: str | None
    ) -> dict[str, dict[str, object]]:
        return {str(symbol): _projection(str(symbol)) for symbol in symbols}

    async def fail_attachment(*args: object, **kwargs: object) -> None:
        raise AssertionError("quick must not perform deep/advisory attachment work")

    monkeypatch.setattr(analysis_screening, "_analyze_stock_impl", fail_full_analyzer)
    monkeypatch.setattr(
        handlers, "_load_quick_projection_batch", fake_projection, raising=False
    )
    monkeypatch.setattr(handlers, "_attach_fresh_artifact_hints", fail_attachment)
    monkeypatch.setattr(handlers, "_attach_decision_history", fail_attachment)
    monkeypatch.setattr(handlers, "_attach_earnings", fail_attachment)

    result = await handlers.analyze_stock_batch_impl(
        ["005930"], market="kr", include_position=False
    )

    assert "consensus" not in result["results"]["005930"]
    assert "recommendation" not in result["results"]["005930"]
    assert "earnings" not in result["results"]["005930"]


@pytest.mark.asyncio
async def test_full_batch_still_calls_single_symbol_analyzer_and_preserves_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_payload = {
        "symbol": "AAPL",
        "market_type": "equity_us",
        "source": "yahoo",
        "quote": {"price": 185.5},
        "news": [{"title": "full payload"}],
        "profile": {"name": "Apple Inc."},
    }
    calls = 0

    async def fake_full_analyzer(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return full_payload

    monkeypatch.setattr(analysis_screening, "_analyze_stock_impl", fake_full_analyzer)

    result = await handlers.analyze_stock_batch_impl(
        ["AAPL"], market="us", quick=False, include_position=False
    )

    assert calls == 1
    assert result["results"]["AAPL"] == full_payload


@pytest.mark.asyncio
async def test_quick_db_batch_query_bound_is_independent_of_symbol_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_fetch(self: object, **kwargs: object) -> dict[str, list[object]]:
        calls.append(kwargs)
        return {str(symbol): [] for symbol in kwargs["symbols"]}  # type: ignore[index]

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(analysis_quick, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        analysis_quick.DailyCandlesRepository,
        "fetch_recent_batch",
        fake_fetch,
    )

    one_symbol = [("005930", "equity_kr")]
    ten_symbols = [
        ("005930", "equity_kr"),
        ("000660", "equity_kr"),
        ("035420", "equity_kr"),
        ("AAPL", "equity_us"),
        ("MSFT", "equity_us"),
        ("NVDA", "equity_us"),
        ("KRW-BTC", "crypto"),
        ("KRW-ETH", "crypto"),
        ("KRW-XRP", "crypto"),
        ("KRW-SOL", "crypto"),
    ]

    calls.clear()
    await analysis_quick.load_quick_projection_batch(one_symbol)
    one_count = len(calls)
    calls.clear()
    await analysis_quick.load_quick_projection_batch(ten_symbols)
    ten_count = len(calls)

    assert one_count == 1
    assert ten_count == 3
    assert ten_count <= analysis_quick.QUICK_DB_QUERY_LIMIT
    assert {call["market"] for call in calls} == {
        MarketKey.KR,
        MarketKey.US,
        MarketKey.CRYPTO,
    }
    assert {
        symbol
        for call in calls
        for symbol in call["symbols"]  # type: ignore[union-attr]
    } == {symbol for symbol, _market in ten_symbols}


@pytest.mark.asyncio
async def test_quick_entrypoint_executes_exact_bounded_actual_sql_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, rows: list[object] | None = None) -> None:
            self.rows = rows or []

        def mappings(self) -> Result:
            return self

        def scalars(self) -> Result:
            return self

        def all(self) -> list[object]:
            return []

        def __iter__(self):
            return iter(self.rows)

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(
            self, statement: object, params: object | None = None
        ) -> Result:
            self.calls += 1
            if "crypto_instruments" in str(statement):
                return Result([SimpleNamespace(venue_symbol="KRW-BTC", id=1)])
            return Result()

    class SessionContext(Session):
        async def __aenter__(self) -> SessionContext:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    session_context = SessionContext()
    monkeypatch.setattr(analysis_quick, "AsyncSessionLocal", lambda: session_context)

    result = await handlers.analyze_stock_batch_impl(
        [
            "005930",
            "AAPL",
            "KRW-BTC",
        ],
        include_position=False,
    )

    assert set(result["results"]) == {"005930", "AAPL", "KRW-BTC"}
    assert session_context.calls == 12
    assert session_context.calls <= analysis_quick.QUICK_DB_QUERY_LIMIT

    session_context.calls = 0
    await handlers.analyze_stock_batch_impl(
        [
            "005930",
            "000660",
            "035420",
            "AAPL",
            "MSFT",
            "NVDA",
            "KRW-BTC",
            "KRW-ETH",
            "KRW-XRP",
            "KRW-SOL",
        ],
        include_position=False,
    )
    assert session_context.calls == 12
    assert session_context.calls <= analysis_quick.QUICK_DB_QUERY_LIMIT


@pytest.mark.asyncio
async def test_quick_entrypoint_makes_zero_transport_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class ForbiddenHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            raise AssertionError("quick must not construct an HTTP client")

    monkeypatch.setattr(handlers.httpx, "AsyncClient", ForbiddenHttpClient)
    monkeypatch.setattr(
        analysis_screening,
        "_analyze_stock_impl",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("quick must not call deep analyzer")
        ),
    )
    monkeypatch.setattr(
        analysis_quick,
        "AsyncSessionLocal",
        lambda: _empty_session_context(),
    )

    await handlers.analyze_stock_batch_impl(
        ["005930", "000660", "035420"], market="kr", include_position=False
    )

    assert calls == 0


@pytest.mark.asyncio
async def test_quick_entrypoint_reads_halted_history_for_every_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC)
    rows = []
    for symbol in ("000880", "005930"):
        for offset in (0, 1, 2):
            rows.append(
                {
                    "time": base - datetime.timedelta(days=offset),
                    "symbol": symbol,
                    "partition": "KRX",
                    "open": 83800.0,
                    "high": 83800.0,
                    "low": 83800.0,
                    "close": 83800.0,
                    "adj_close": None,
                    "volume": 0.0,
                    "value": 0.0,
                    "source": "fixture",
                }
            )

    class Result:
        def mappings(self) -> Result:
            return self

        def all(self) -> list[dict[str, object]]:
            return rows

    class Session:
        async def execute(
            self, statement: object, params: object | None = None
        ) -> Result:
            return Result()

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(analysis_quick, "AsyncSessionLocal", Session)

    result = await handlers.analyze_stock_batch_impl(
        ["000880", "005930"], market="kr", include_position=False
    )

    for symbol in ("000880", "005930"):
        row = result["results"][symbol]
        assert row["data_state"] == "halted_suspect"
        assert row["rsi_14"] is None
        assert row["supports"] is None
        assert row["resistances"] is None
        assert row["halt_suspect"]["suspected"] is True


def _empty_session_context():
    class Result:
        def mappings(self) -> Result:
            return self

        def scalars(self) -> Result:
            return self

        def all(self) -> list[object]:
            return []

    class Session:
        async def execute(
            self, statement: object, params: object | None = None
        ) -> Result:
            return Result()

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    return Session()


@pytest.mark.asyncio
async def test_daily_candle_repository_uses_one_window_query_for_equity_batch() -> None:
    class Result:
        def mappings(self) -> Result:
            return self

        def scalars(self) -> Result:
            return self

        def all(self) -> list[object]:
            return []

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(
            self, statement: object, params: object | None = None
        ) -> Result:
            self.calls += 1
            return Result()

    from app.services.daily_candles.repository import DailyCandlesRepository

    session = Session()
    repo = DailyCandlesRepository(session=session)  # type: ignore[arg-type]
    await repo.fetch_recent_batch(
        market=MarketKey.KR,
        symbols=["005930", "000660", "035420", "051910"],
        partition="KRX",
        count=250,
    )

    assert session.calls == 1


def test_quick_projection_allowlist_excludes_deep_contract_fields() -> None:
    assert "news" not in analysis_quick.QUICK_PROJECTION_FIELDS
    assert "profile" not in analysis_quick.QUICK_PROJECTION_FIELDS
    assert "consensus" not in analysis_quick.QUICK_PROJECTION_FIELDS
    assert "recommendation" not in analysis_quick.QUICK_PROJECTION_FIELDS
    assert "decision_history" in analysis_quick.QUICK_PROJECTION_FIELDS
    assert "earnings" in analysis_quick.QUICK_PROJECTION_FIELDS
    assert analysis_quick.QUICK_HTTP_REQUEST_LIMIT == 0
    assert analysis_quick.QUICK_DB_QUERY_LIMIT == 12


def test_quick_projection_preserves_halted_suspect_semantics() -> None:
    base = datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC)
    rows = [
        DailyCandleRow(
            time_utc=base - datetime.timedelta(days=offset),
            symbol="000880",
            partition="KRX",
            open=83800.0,
            high=83800.0,
            low=83800.0,
            close=83800.0,
            adj_close=None,
            volume=0.0,
            value=0.0,
            source="fixture",
        )
        for offset in (2, 1, 0)
    ]

    result = analysis_quick._project_symbol("000880", "equity_kr", rows)

    assert result["data_state"] == "halted_suspect"
    assert result["rsi_14"] is None
    assert result["supports"] is None
    assert result["resistances"] is None
    assert result["halt_suspect"]["suspected"] is True
    assert result["halt_suspect"]["frozen_sessions"] == 3
