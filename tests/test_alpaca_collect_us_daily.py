from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.services.brokers.alpaca.us_daily_collect import (
    MIN_RATE_SECONDS,
    AlpacaUsDailyCandle,
    AlpacaUsDailyCandleRepository,
    AlpacaUsDailyCollectionDisabled,
    AlpacaUsDailyCollector,
    HttpxAlpacaBarsClient,
    ResumeCheckpoint,
    arm_scoped_environment,
)
from scripts.alpaca_collect_us_daily import parse_args


class StubBarsClient:
    def __init__(self, pages: dict[tuple[str, ...], list[dict[str, object]]]) -> None:
        self.pages = pages
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    async def fetch_bars(
        self, *, symbols: list[str], bars: int, page_token: str | None = None
    ) -> dict[str, object]:
        key = tuple(symbols)
        self.calls.append((key, page_token))
        if not self.pages[key]:
            raise RuntimeError("stub batch exhausted")
        return self.pages[key].pop(0)


def _bar(symbol: str, day: int, close: str = "12") -> dict[str, str]:
    return {
        "S": symbol,
        "t": f"2026-01-{day:02d}T00:00:00Z",
        "o": "10",
        "h": "13",
        "l": "9",
        "c": close,
        "v": "100",
    }


@pytest.fixture(autouse=True)
def _collect_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_US_COLLECT_ENABLED", "true")


def _write_env(path: Path, *, mode: int = 0o600, gate: str = "true") -> None:
    path.write_text(
        "\n".join(
            [
                f"ALPACA_US_COLLECT_ENABLED={gate}",
                "ALPACA_DATA_API_KEY_ID=stub-key",
                "ALPACA_DATA_API_SECRET_KEY=stub-secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(mode)


def test_env_mode_and_gate_reject_before_collector_network(tmp_path: Path) -> None:
    env_file = tmp_path / "alpaca.native.env"
    _write_env(env_file, mode=0o644)
    with pytest.raises(ValueError, match="0600"):
        arm_scoped_environment(env_file=env_file)

    _write_env(env_file, gate="false")
    arm_scoped_environment(env_file=env_file)
    collector = AlpacaUsDailyCollector(client=StubBarsClient({}))
    with pytest.raises(AlpacaUsDailyCollectionDisabled):
        asyncio_run(collector.collect(symbols=[("AAPL", "NASD")]))
    with pytest.raises(AlpacaUsDailyCollectionDisabled):
        asyncio_run(
            HttpxAlpacaBarsClient(
                api_key="stub-key", api_secret="stub-secret"
            ).fetch_bars(symbols=["AAPL"], bars=1)
        )


def test_http_client_requests_sufficient_start_window_on_every_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = 3
    minimum_start = datetime.now(UTC).date() - timedelta(days=5)
    requests: list[httpx.Request] = []

    async def fake_get(
        self: httpx.AsyncClient,
        url: str,
        *,
        params: dict[str, str | int],
        headers: dict[str, str],
    ) -> httpx.Response:
        request = httpx.Request(
            "GET", "https://data.alpaca.markets/v2/stocks/bars", params=params
        )
        requests.append(request)
        start = request.url.params.get("start")
        assert start is not None, "bars request must include start in its query string"
        assert date_fromisoformat(start) <= minimum_start, (
            "bars request start window is too narrow for --bars"
        )
        assert request.url.params.get("end") is None
        return httpx.Response(200, json={"bars": {}}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    client = HttpxAlpacaBarsClient(api_key="stub-key", api_secret="stub-secret")

    asyncio_run(client.fetch_bars(symbols=["AAPL"], bars=bars))
    asyncio_run(
        client.fetch_bars(symbols=["AAPL"], bars=bars, page_token="second-page")
    )

    assert [request.url.params.get("page_token") for request in requests] == [
        None,
        "second-page",
    ]


def date_fromisoformat(value: str) -> date:
    return datetime.fromisoformat(f"{value}T00:00:00+00:00").date()


def asyncio_run(awaitable: object) -> object:
    import asyncio

    return asyncio.run(awaitable)  # type: ignore[arg-type]


def test_rate_floor_rejected_before_any_request() -> None:
    client = StubBarsClient({})
    collector = AlpacaUsDailyCollector(client=client)
    with pytest.raises(ValueError, match="at least"):
        asyncio_run(
            collector.collect(
                symbols=[("AAPL", "NASD")], rate_seconds=MIN_RATE_SECONDS - 0.01
            )
        )
    assert client.calls == []


def test_cli_arguments_reject_unsafe_resume_and_rate() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--env-file", "alpaca.env", "--resume"])
    with pytest.raises(SystemExit):
        parse_args(["--env-file", "alpaca.env", "--rate-seconds", "0.29"])


def test_repository_counts_returned_rows_not_executemany_rowcount() -> None:
    class Result:
        rowcount = 0

        def fetchall(self) -> list[object]:
            return [object(), object()]

    class Session:
        async def execute(self, statement: object, params: object) -> Result:
            assert "RETURNING time" in str(statement)
            assert params is not None
            return Result()

    rows = [
        AlpacaUsDailyCandle(
            time_utc=datetime(2026, 1, day, tzinfo=UTC),
            symbol="AAPL",
            exchange="NASD",
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
            adj_close=Decimal("10"),
            volume=Decimal("100"),
            value=Decimal("1000"),
        )
        for day in (1, 2)
    ]
    assert (
        asyncio_run(
            AlpacaUsDailyCandleRepository(session=Session()).insert_missing(rows)
        )
        == 2
    )  # type: ignore[arg-type]


def test_dry_run_then_commit_reports_exact_insert_and_skip_counts() -> None:
    rows: list[object] = []

    async def write_missing(batch: list[object]) -> int:
        inserted = 0
        known = {(row.time_utc, row.symbol, row.exchange) for row in rows}  # type: ignore[attr-defined]
        for row in batch:
            key = (row.time_utc, row.symbol, row.exchange)  # type: ignore[attr-defined]
            if key not in known:
                rows.append(row)
                known.add(key)
                inserted += 1
        return inserted

    symbols = [("AAPL", "NASD"), ("BRK.B", "NYSE")]
    payload = {"bars": {"AAPL": [_bar("AAPL", 2)], "BRK.B": [_bar("BRK.B", 2)]}}
    dry = AlpacaUsDailyCollector(
        client=StubBarsClient({tuple(s for s, _ in symbols): [payload]})
    )
    dry_result = asyncio_run(dry.collect(symbols=symbols, batch_size=2))
    assert dry_result.rows_received == 2
    assert dry_result.rows_inserted == dry_result.rows_conflict_skipped == 0
    assert rows == []

    first = AlpacaUsDailyCollector(
        client=StubBarsClient({tuple(s for s, _ in symbols): [payload]}),
        write_rows=write_missing,
    )
    first_result = asyncio_run(
        first.collect(symbols=symbols, batch_size=2, commit=True)
    )
    assert (first_result.rows_inserted, first_result.rows_conflict_skipped) == (2, 0)

    second = AlpacaUsDailyCollector(
        client=StubBarsClient({tuple(s for s, _ in symbols): [payload]}),
        write_rows=write_missing,
    )
    second_result = asyncio_run(
        second.collect(symbols=symbols, batch_size=2, commit=True)
    )
    assert (second_result.rows_inserted, second_result.rows_conflict_skipped) == (0, 2)


def test_resume_retries_failed_batch_and_checkpoint_is_atomic(tmp_path: Path) -> None:
    symbols = [("AAPL", "NASD"), ("MSFT", "NASD"), ("BRK.B", "NYSE")]
    checkpoint = ResumeCheckpoint(tmp_path / "checkpoint.json")
    client = StubBarsClient(
        {
            ("AAPL", "MSFT"): [{"bars": {"AAPL": [_bar("AAPL", 2)]}}],
            ("BRK.B",): [RuntimeError("offline")],
        }
    )

    async def fetch(
        *, symbols: list[str], bars: int, page_token: str | None = None
    ) -> dict[str, object]:
        next_item = client.pages[tuple(symbols)].pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item  # type: ignore[return-value]

    client.fetch_bars = fetch  # type: ignore[method-assign]
    collector = AlpacaUsDailyCollector(
        client=client, write_rows=lambda rows: _insert_all(rows)
    )
    result = asyncio_run(
        collector.collect(
            symbols=symbols, batch_size=2, commit=True, checkpoint=checkpoint
        )
    )
    assert result.failed_symbols == ("BRK.B",)
    assert checkpoint.path.exists()

    resumed = AlpacaUsDailyCollector(
        client=StubBarsClient({("BRK.B",): [{"bars": {"BRK.B": [_bar("BRK.B", 2)]}}]}),
        write_rows=lambda rows: _insert_all(rows),
    )
    resumed_result = asyncio_run(
        resumed.collect(
            symbols=symbols,
            batch_size=2,
            commit=True,
            resume=True,
            checkpoint=checkpoint,
        )
    )
    assert resumed_result.resumed_from == "MSFT"
    assert resumed_result.failed_symbols == ()
    assert not checkpoint.path.exists()


async def _insert_all(rows: list[object]) -> int:
    return len(rows)


def test_failed_batch_is_isolated_and_pagination_accumulates() -> None:
    symbols = [("AAPL", "NASD"), ("MSFT", "NASD"), ("BRK.B", "NYSE")]
    client = StubBarsClient(
        {
            ("AAPL", "MSFT"): [RuntimeError("transport")],
            ("BRK.B",): [
                {"bars": {"BRK.B": [_bar("BRK.B", 1)]}, "next_page_token": "next"},
                {"bars": {"BRK.B": [_bar("BRK.B", 2)]}},
            ],
        }
    )

    async def fetch(
        *, symbols: list[str], bars: int, page_token: str | None = None
    ) -> dict[str, object]:
        item = client.pages[tuple(symbols)].pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]

    client.fetch_bars = fetch  # type: ignore[method-assign]
    result = asyncio_run(
        AlpacaUsDailyCollector(client=client).collect(symbols=symbols, batch_size=2)
    )
    assert result.failed_symbols == ("AAPL", "MSFT")
    assert result.rows_received == 2
