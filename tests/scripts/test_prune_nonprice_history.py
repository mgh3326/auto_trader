"""Unit tests for scripts/prune_nonprice_history.py (hk 5751).

Selection, batch bounds, held-feed handling and the preview's no-write
property. No database: the batched steps are driven by a fake, and the preview
runs against a fake session that records every statement it executes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Delete

from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
    superseded_by_newer_row,
)
from scripts import prune_nonprice_history as prune

pytestmark = pytest.mark.unit

EXPECTED_RETIRED_FEEDS = {
    "rss_yahoo_finance_topstories",
    "http_naver_stock_aggregate",
    "browser_naver_mainnews",
    "rss_cnbc_us_markets",
    "browser_naver_research_company",
    "rss_marketwatch_topstories",
    "rss_cointelegraph",
    "rss_coindesk",
    "browser_naver_research",
    "browser_naver_research_industry",
    "rss_decrypt",
    "browser_naver_research_invest",
    "rss_bitcoin_magazine",
    "browser_naver_research_economy",
    "browser_investing_news",
    "browser_cnbc_news",
    "rss_cnbc_earnings",
    "rss_fed_press",
    "rss_cnbc_finance",
    "http_tvscreener_news_kr",
}
KEPT_FEEDS = {
    "naver_item_news",
    "finnhub_company_news",
    "finnhub_general_news",
    "research_on_demand_naver",
    "research_on_demand_finnhub",
}


# --- selection --------------------------------------------------------------


def test_default_news_selection_is_exactly_the_retired_list_minus_held() -> None:
    selected = prune.news_feeds_to_delete()
    assert set(selected) == EXPECTED_RETIRED_FEEDS - {"http_naver_stock_aggregate"}
    assert len(selected) == len(set(selected)) == 19


def test_allowing_the_held_feed_adds_exactly_it() -> None:
    selected = prune.news_feeds_to_delete(["http_naver_stock_aggregate"])
    assert set(selected) == EXPECTED_RETIRED_FEEDS
    assert len(selected) == 20


def test_kept_feeds_are_never_selected_even_when_everything_is_allowed() -> None:
    selected = set(prune.news_feeds_to_delete(["http_naver_stock_aggregate"]))
    assert selected.isdisjoint(KEPT_FEEDS)


def test_unknown_allow_name_is_an_input_error_not_a_silent_no_op() -> None:
    with pytest.raises(prune.PruneInputError):
        prune.news_feeds_to_delete(["http_naver_stok_aggregate"])  # typo
    with pytest.raises(prune.PruneInputError):
        prune.news_feeds_to_delete(["naver_item_news"])  # kept feed, not held


def test_held_feed_is_reported_as_kept_without_the_flag() -> None:
    assert prune.held_news_feeds_not_allowed() == ("http_naver_stock_aggregate",)
    assert prune.held_news_feeds_not_allowed(["http_naver_stock_aggregate"]) == ()


def test_market_event_triples_are_the_three_retired_ones() -> None:
    assert set(prune.MARKET_EVENT_RETIRED_TRIPLES) == {
        ("tradingview", "economic", "global"),
        ("forexfactory", "economic", "global"),
        ("wisefn", "earnings", "kr"),
    }


def test_dart_and_finnhub_market_events_are_not_selected() -> None:
    selected_sources = {source for source, _, _ in prune.MARKET_EVENT_RETIRED_TRIPLES}
    assert "dart" not in selected_sources
    assert "finnhub" not in selected_sources


def test_only_option_rejects_unknown_category() -> None:
    with pytest.raises(prune.PruneInputError):
        prune._selected_categories(["news", "snapshots"])
    assert prune._selected_categories(None) == prune.ALL_CATEGORIES
    assert prune._selected_categories(["toss_valuation"]) == ("toss_valuation",)


def test_batch_size_bounds() -> None:
    assert prune.batch_size_or_raise(1) == 1
    assert prune.batch_size_or_raise(prune.MAX_BATCH_SIZE) == prune.MAX_BATCH_SIZE
    for bad in (0, -1, prune.MAX_BATCH_SIZE + 1):
        with pytest.raises(prune.PruneInputError):
            prune.batch_size_or_raise(bad)


# --- batching ---------------------------------------------------------------


class _FakeTable:
    """Stands in for a table: ids ascending, steps honour cursor and size."""

    def __init__(self, ids: list[int], *, dependents_per_row: int = 0) -> None:
        self.remaining = sorted(ids)
        self.dependents_per_row = dependents_per_row
        self.step_sizes: list[int] = []

    async def step(self, after: int, size: int) -> prune.StepResult:
        self.step_sizes.append(size)
        chunk = [i for i in self.remaining if i > after][:size]
        if not chunk:
            return prune.StepResult(selected=0, deleted=0, last_id=None)
        self.remaining = [i for i in self.remaining if i not in chunk]
        return prune.StepResult(
            selected=len(chunk),
            deleted=len(chunk),
            last_id=chunk[-1],
            dependents={"dep": self.dependents_per_row * len(chunk)},
        )


def test_run_batches_walks_every_row_in_bounded_batches() -> None:
    table = _FakeTable(list(range(1, 24)), dependents_per_row=2)
    total, dependents, batches = asyncio.run(prune.run_batches(table.step, 5))
    assert total == 23
    assert batches == 5  # 5+5+5+5+3
    assert dependents == {"dep": 46}
    assert all(size == 5 for size in table.step_sizes)
    assert table.remaining == []


def test_run_batches_is_a_no_op_on_empty_input() -> None:
    table = _FakeTable([])
    assert asyncio.run(prune.run_batches(table.step, 10)) == (0, {}, 0)


def test_second_run_over_the_same_rows_deletes_nothing() -> None:
    table = _FakeTable([1, 2, 3])
    asyncio.run(prune.run_batches(table.step, 2))
    second = asyncio.run(prune.run_batches(table.step, 2))
    assert second == (0, {}, 0)


def test_run_batches_refuses_a_batch_that_deleted_fewer_rows_than_selected() -> None:
    async def racing_step(after: int, size: int) -> prune.StepResult:
        if after:
            return prune.StepResult(selected=0, deleted=0, last_id=None)
        return prune.StepResult(selected=3, deleted=2, last_id=3)

    with pytest.raises(RuntimeError, match="deleted 2 of 3"):
        asyncio.run(prune.run_batches(racing_step, 10))


def test_run_batches_refuses_a_cursor_that_does_not_advance() -> None:
    async def stuck_step(after: int, size: int) -> prune.StepResult:
        return prune.StepResult(selected=1, deleted=1, last_id=after)

    with pytest.raises(RuntimeError, match="cursor did not advance"):
        asyncio.run(prune.run_batches(stuck_step, 10))


def test_run_batches_refuses_a_batch_larger_than_its_bound() -> None:
    async def oversized(after: int, size: int) -> prune.StepResult:
        if after:
            return prune.StepResult(selected=0, deleted=0, last_id=None)
        return prune.StepResult(selected=size + 1, deleted=size + 1, last_id=9)

    with pytest.raises(RuntimeError, match="exceeded its bound"):
        asyncio.run(prune.run_batches(oversized, 4))


# --- preview writes nothing -------------------------------------------------


class _Result:
    def all(self) -> list[Any]:
        return []

    def scalar_one(self) -> int:
        return 0


class _FakeSession:
    def __init__(self, log: list[Any]) -> None:
        self._log = log

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, stmt: Any) -> _Result:
        self._log.append(stmt)
        return _Result()

    def begin(self) -> Any:  # a preview must never open a write transaction
        raise AssertionError("preview opened a transaction")


def test_preview_executes_only_selects() -> None:
    log: list[Any] = []
    report = asyncio.run(
        prune.run_preview(lambda: _FakeSession(log), allow_held=[])  # type: ignore[arg-type]
    )
    assert report["mode"] == "preview"
    assert log, "preview should have counted something"
    assert not any(isinstance(stmt, Delete) for stmt in log)
    assert report["categories"]["news"]["held_feeds_kept"] == [
        "http_naver_stock_aggregate"
    ]


# --- toss writer retention --------------------------------------------------


class _CapturingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> Any:
        self.statements.append(stmt)

        class _R:
            rowcount = 0

        return _R()


def _compiled(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_prune_superseded_deletes_only_older_rows_of_the_same_source() -> None:
    session = _CapturingSession()
    repo = MarketValuationSnapshotsRepository(session)  # type: ignore[arg-type]
    asyncio.run(
        repo.prune_superseded(
            source="Toss_OpenAPI ",
            market=" KR ",
            symbols=[" aaa ", "AAA", "bbb"],
        )
    )
    assert len(session.statements) == 1
    stmt = session.statements[0]
    assert isinstance(stmt, Delete)
    sql = _compiled(stmt)
    assert "DELETE FROM market_valuation_snapshots" in sql
    assert "EXISTS" in sql
    compiled = stmt.compile(
        dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True}
    )
    params = compiled.params
    assert params["source_1"] == "toss_openapi"
    assert params["market_1"] == "kr"
    symbol_values = {v for k, v in params.items() if k.startswith("symbol_1")}
    assert symbol_values == {"AAA", "BBB"}


def test_prune_superseded_with_no_symbols_executes_nothing() -> None:
    session = _CapturingSession()
    repo = MarketValuationSnapshotsRepository(session)  # type: ignore[arg-type]
    assert (
        asyncio.run(
            repo.prune_superseded(source="toss_openapi", market="kr", symbols=[])
        )
        == 0
    )
    assert session.statements == []


def test_superseded_predicate_is_correlated_and_same_source_only() -> None:
    sql = _compiled(
        sa.select(MarketValuationSnapshot.id).where(
            superseded_by_newer_row("toss_openapi")
        )
    )
    assert (
        "market_valuation_snapshots_1.market = market_valuation_snapshots.market" in sql
    )
    assert (
        "market_valuation_snapshots_1.symbol = market_valuation_snapshots.symbol" in sql
    )
    assert "snapshot_date >" in sql
    assert "market_valuation_snapshots_1.id > market_valuation_snapshots.id" in sql


# --- review r1 hardening ----------------------------------------------------


def test_batch_size_cap_stays_below_the_asyncpg_bind_ceiling() -> None:
    # asyncpg allows 32,767 bind parameters per statement; one per id in an IN list.
    assert prune.MAX_BATCH_SIZE < 32_767


def test_in_transaction_count_check_names_the_safe_counts_only() -> None:
    with pytest.raises(RuntimeError, match="deleted 2 of 3 selected rows"):
        prune._require_all_deleted(3, 2)
    prune._require_all_deleted(3, 3)


def test_plain_postgres_dsn_is_normalized_to_asyncpg(monkeypatch) -> None:
    monkeypatch.setenv(
        "PRUNE_TEST_DB_URL", "postgresql://user:secret@localhost:5432/db"
    )
    url = prune._database_url("PRUNE_TEST_DB_URL")
    assert url.startswith("postgresql+asyncpg://user:secret@localhost:5432/db")


def test_non_postgres_url_is_rejected_without_echoing_it(monkeypatch) -> None:
    monkeypatch.setenv("PRUNE_TEST_DB_URL", "mysql://user:secret@localhost/db")
    with pytest.raises(prune.PruneInputError) as excinfo:
        prune._database_url("PRUNE_TEST_DB_URL")
    assert "secret" not in str(excinfo.value)
