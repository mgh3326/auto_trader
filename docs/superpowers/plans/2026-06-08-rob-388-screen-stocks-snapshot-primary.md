# ROB-388 — screen_stocks(kr) snapshot-primary 발굴 경로 복구 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `screen_stocks(market="kr")`가 KRX live가 죽어도 0건으로 끝나지 않도록, `kr_market_ranking` durable read-model을 primary로 소비하고 stale도 정직하게 반환한다.

**Architecture:** 신규 모듈 `screening/kr_ranking_snapshot.py`가 `MomentumRankingQueryService`(기존 read-model)에서 랭킹을 읽어 screen_stocks 행 모양으로 매핑·enrich하고 freshness를 정직 메타로 변환한다. 이 헬퍼는 `screener_snapshot_tool.py`처럼 `AsyncSessionLocal`로 **자체 세션을 획득**(dispatch 시그니처 변경 없음)하고 **fail-open**(오류·미커버 sort_by·행 0 → `None`)이다. `_screen_kr_with_fallback` 맨 앞에 사다리로 끼워, 스냅샷에 행이 있으면(fresh/stale) 반환하고, 그렇지 않으면 기존 tvscreener→legacy 경로로 흐른다.

**Tech Stack:** Python 3.13, SQLAlchemy async (`AsyncSessionLocal`), pytest, ruff. 기존 `app/services/invest_momentum_events/{query_service,repository}.py` 재사용. migration 없음, read-only.

---

## File Structure

- **Create** `app/mcp_server/tooling/screening/kr_ranking_snapshot.py` — 매핑/정렬/freshness 순수 함수 + `load_kr_ranking_snapshot` 오케스트레이터 + `KrRankingSnapshotResult` dataclass. (단일 책임: kr_market_ranking → screen_stocks 어댑터.)
- **Modify** `app/mcp_server/tooling/screening/kr.py` — `_screen_kr_with_fallback` 맨 앞에 snapshot-primary 사다리 삽입.
- **Create** `tests/test_kr_ranking_snapshot.py` — 순수 함수 + 오케스트레이터(주입 query_service) 단위 테스트.
- **Modify** `tests/test_mcp_screen_stocks_kr.py` — 사다리 회귀(스냅샷 행 있음→반환 / stale 정직 / None→live fallthrough / 미커버 sort_by→live).

기존 참조(읽기 전용, 변경 없음):
- `app/services/invest_momentum_events/query_service.py` — `MomentumRankingQueryService.get_ranking(order_type, market, limit, now)`, `RankingRow(rank, symbol, name, price, change_rate, volume, trade_value, market_cap)`, `Freshness(overall, latest_snapshot_at, stale_reason)`, `MomentumRanking(market, order_type, trading_date, rows, freshness)`.
- `app/services/invest_momentum_events/repository.py` — `InvestMomentumEventSnapshotsRepository(session)`.
- `app/core/db.py` — `AsyncSessionLocal`.
- `app/services/krx.py` — `fetch_stock_all_cached(market="STK"|"KSQ")`, `fetch_valuation_all_cached(market="ALL")`.
- `app/mcp_server/tooling/screening/instrument_type.py` — `classify_kr_instrument(code, name, subtype)`.
- `app/mcp_server/tooling/screening/common.py` — `_build_screen_response(results, total_count, filters_applied, market, warnings=None, meta_fields=None)`.

---

## Task 1: sort_by → order_type 매핑 + 모듈 스캐폴드

**Files:**
- Create: `app/mcp_server/tooling/screening/kr_ranking_snapshot.py`
- Test: `tests/test_kr_ranking_snapshot.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kr_ranking_snapshot.py
import pytest

from app.mcp_server.tooling.screening import kr_ranking_snapshot as krs


@pytest.mark.unit
def test_snapshot_eligible_sorts_membership():
    assert krs.is_snapshot_eligible_sort("change_rate") is True
    assert krs.is_snapshot_eligible_sort("volume") is True
    assert krs.is_snapshot_eligible_sort("trade_amount") is True
    assert krs.is_snapshot_eligible_sort("market_cap") is True
    # not covered by the momentum ranking read-model -> must go live
    assert krs.is_snapshot_eligible_sort("dividend_yield") is False
    assert krs.is_snapshot_eligible_sort("week_change_rate") is False
    assert krs.is_snapshot_eligible_sort("rsi") is False
    assert krs.is_snapshot_eligible_sort("score") is False


@pytest.mark.unit
def test_order_types_for_sort():
    # direct single-bucket dimensions
    assert krs.order_types_for_sort("change_rate") == ("up",)
    assert krs.order_types_for_sort("volume") == ("quantTop",)
    # re-sort dimensions union both default buckets
    assert krs.order_types_for_sort("trade_amount") == ("up", "quantTop")
    assert krs.order_types_for_sort("market_cap") == ("up", "quantTop")
    # ineligible -> empty
    assert krs.order_types_for_sort("rsi") == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kr_ranking_snapshot.py -v`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError` (module/functions not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# app/mcp_server/tooling/screening/kr_ranking_snapshot.py
"""ROB-388: screen_stocks(kr) snapshot-primary adapter.

Reads the kr_market_ranking durable read-model (InvestMomentumEvent snapshots via
MomentumRankingQueryService) and adapts it to the screen_stocks row/response shape,
with honest freshness. Self-acquires its DB session (mirrors screener_snapshot_tool)
and is fail-open: any error / ineligible sort_by / zero rows -> None, so the caller
falls through to the live (tvscreener -> legacy KRX) path.
"""

from __future__ import annotations

# sort_by values the kr_market_ranking read-model can serve. Everything else
# (dividend_yield, week_change_rate, rsi, score, ...) must go to the live path.
SNAPSHOT_ELIGIBLE_SORTS: frozenset[str] = frozenset(
    {"change_rate", "volume", "trade_amount", "market_cap"}
)

# Momentum read-model is bucketed by order_type. "up" = 상승(change_rate),
# "quantTop" = 거래량(volume). trade_amount / market_cap have no native bucket, so we
# union the two default-collected buckets and re-sort by the requested field.
_ORDER_TYPE_BY_SORT: dict[str, tuple[str, ...]] = {
    "change_rate": ("up",),
    "volume": ("quantTop",),
    "trade_amount": ("up", "quantTop"),
    "market_cap": ("up", "quantTop"),
}


def is_snapshot_eligible_sort(sort_by: str) -> bool:
    return sort_by in SNAPSHOT_ELIGIBLE_SORTS


def order_types_for_sort(sort_by: str) -> tuple[str, ...]:
    return _ORDER_TYPE_BY_SORT.get(sort_by, ())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kr_ranking_snapshot.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/screening/kr_ranking_snapshot.py tests/test_kr_ranking_snapshot.py
git commit -m "feat(ROB-388): kr_ranking_snapshot sort_by->order_type mapping + eligibility"
```

---

## Task 2: RankingRow → screen_stocks 행 매핑 (순수)

**Files:**
- Modify: `app/mcp_server/tooling/screening/kr_ranking_snapshot.py`
- Test: `tests/test_kr_ranking_snapshot.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_kr_ranking_snapshot.py
from app.services.invest_momentum_events.query_service import RankingRow


@pytest.mark.unit
def test_ranking_row_to_screen_row_maps_fields():
    row = RankingRow(
        rank=1, symbol="005930", name="삼성전자", price=71000.0,
        change_rate=3.5, volume=12_000_000, trade_value=8.5e11, market_cap=4.2e14,
    )
    out = krs.ranking_row_to_screen_row(row)
    assert out["symbol"] == "005930"
    assert out["short_code"] == "005930"
    assert out["code"] == "005930"
    assert out["name"] == "삼성전자"
    assert out["price"] == 71000.0
    assert out["change_rate"] == 3.5
    assert out["volume"] == 12_000_000.0  # int -> float
    assert out["trade_amount"] == 8.5e11   # trade_value -> trade_amount
    assert out["market_cap"] == 4.2e14
    assert out["market"] == "kr"
    # not provided by the ranking read-model -> explicit null (no fabrication)
    assert out["per"] is None
    assert out["pbr"] is None
    assert out["dividend_yield"] is None


@pytest.mark.unit
def test_ranking_row_to_screen_row_null_safe():
    row = RankingRow(
        rank=2, symbol="000660", name=None, price=None,
        change_rate=None, volume=None, trade_value=None, market_cap=None,
    )
    out = krs.ranking_row_to_screen_row(row)
    assert out["symbol"] == "000660"
    assert out["name"] == "000660"  # falls back to symbol when name missing
    assert out["price"] is None
    assert out["volume"] is None
    assert out["trade_amount"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kr_ranking_snapshot.py -k ranking_row_to_screen_row -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'ranking_row_to_screen_row'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add imports at top of kr_ranking_snapshot.py
from typing import Any

from app.services.invest_momentum_events.query_service import RankingRow


def _opt_float(value: float | int | None) -> float | None:
    return float(value) if value is not None else None


def ranking_row_to_screen_row(row: RankingRow) -> dict[str, Any]:
    """Map one RankingRow to the screen_stocks result-row shape. Pure; null-safe;
    never fabricates valuation fields (per/pbr/dividend_yield default to None and
    are filled best-effort later by enrichment)."""
    code = row.symbol
    return {
        "symbol": code,
        "short_code": code,
        "code": code,
        "name": row.name or code,
        "price": _opt_float(row.price),
        "change_rate": _opt_float(row.change_rate),
        "volume": _opt_float(row.volume),
        "trade_amount": _opt_float(row.trade_value),
        "market_cap": _opt_float(row.market_cap),
        "market": "kr",
        "per": None,
        "pbr": None,
        "dividend_yield": None,
        "instrument_type": "stock",
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kr_ranking_snapshot.py -k ranking_row_to_screen_row -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/screening/kr_ranking_snapshot.py tests/test_kr_ranking_snapshot.py
git commit -m "feat(ROB-388): RankingRow -> screen_stocks row mapping (null-safe, no fabrication)"
```

---

## Task 3: 행 정렬(trade_amount/market_cap) + freshness → 정직 메타 (순수)

**Files:**
- Modify: `app/mcp_server/tooling/screening/kr_ranking_snapshot.py`
- Test: `tests/test_kr_ranking_snapshot.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_kr_ranking_snapshot.py
from app.services.invest_momentum_events.query_service import Freshness


@pytest.mark.unit
def test_dedupe_and_sort_rows_by_trade_amount_desc():
    rows = [
        {"symbol": "A", "trade_amount": 100.0, "market_cap": 5.0},
        {"symbol": "B", "trade_amount": 300.0, "market_cap": 1.0},
        {"symbol": "A", "trade_amount": 100.0, "market_cap": 5.0},  # dup symbol
    ]
    out = krs.dedupe_and_sort_rows(rows, sort_by="trade_amount", sort_order="desc")
    assert [r["symbol"] for r in out] == ["B", "A"]  # deduped + sorted desc


@pytest.mark.unit
def test_dedupe_and_sort_rows_market_cap_asc_nulls_last():
    rows = [
        {"symbol": "A", "market_cap": None},
        {"symbol": "B", "market_cap": 2.0},
        {"symbol": "C", "market_cap": 1.0},
    ]
    out = krs.dedupe_and_sort_rows(rows, sort_by="market_cap", sort_order="asc")
    assert [r["symbol"] for r in out] == ["C", "B", "A"]  # None sorts last


@pytest.mark.unit
def test_freshness_to_meta_fresh():
    fr = Freshness(overall="fresh", latest_snapshot_at=None, stale_reason=None)
    data_state, meta, warnings = krs.freshness_to_meta(fr, row_count=20)
    assert data_state == "fresh"
    assert meta["source"] == "kr_market_ranking"
    assert meta["data_state"] == "fresh"
    # coverage caveat is always present (top-movers, not full universe)
    assert any("전체 KRX 스캔" in w for w in warnings)


@pytest.mark.unit
def test_freshness_to_meta_stale_adds_warning_and_not_retryable():
    fr = Freshness(overall="stale", latest_snapshot_at=None, stale_reason="older_than_ttl")
    data_state, meta, warnings = krs.freshness_to_meta(fr, row_count=10)
    assert data_state == "stale"
    assert meta["data_state"] == "stale"
    assert meta["stale_reason"] == "older_than_ttl"
    assert meta["retryable"] is False  # stale snapshot won't recover by immediate retry
    assert any("오래" in w for w in warnings)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kr_ranking_snapshot.py -k "dedupe_and_sort or freshness_to_meta" -v`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to kr_ranking_snapshot.py
from app.services.invest_momentum_events.query_service import Freshness


def dedupe_and_sort_rows(
    rows: list[dict[str, Any]], *, sort_by: str, sort_order: str
) -> list[dict[str, Any]]:
    """Dedupe by symbol (first wins) and sort by the requested field. None sorts last
    regardless of order. Used for trade_amount / market_cap which have no native
    momentum bucket (we union 'up'+'quantTop' then re-rank)."""
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for r in rows:
        sym = r.get("symbol")
        if sym in seen:
            continue
        seen.add(sym)
        deduped.append(r)

    reverse = sort_order != "asc"

    def key(r: dict[str, Any]) -> tuple[int, float]:
        v = r.get(sort_by)
        if v is None:
            # None always last: sort to the extreme opposite of the active direction
            return (1, float("-inf") if reverse else float("inf"))
        return (0, float(v))

    return sorted(deduped, key=key, reverse=reverse)


def freshness_to_meta(
    freshness: Freshness, *, row_count: int
) -> tuple[str, dict[str, Any], list[str]]:
    """Map momentum Freshness to (data_state, meta_fields, warnings) for screen_stocks.
    'unavailable' is handled by the caller (returns None -> live), so only fresh/stale
    produce a response here."""
    data_state = freshness.overall
    meta: dict[str, Any] = {
        "data_state": data_state,
        "source": "kr_market_ranking",
        "latest_snapshot_at": (
            freshness.latest_snapshot_at.isoformat()
            if freshness.latest_snapshot_at is not None
            else None
        ),
    }
    warnings: list[str] = [
        f"모멘텀 랭킹 상위 {row_count}종목 기반 — 전체 KRX 스캔이 아닙니다."
    ]
    if data_state == "stale":
        meta["stale_reason"] = freshness.stale_reason
        meta["retryable"] = False
        meta["reason"] = "kr_market_ranking_stale"
        warnings.append(
            "모멘텀 랭킹 스냅샷이 오래되었습니다"
            f"({freshness.stale_reason}) — 신규 후보 발굴에 주의하세요."
        )
    return data_state, meta, warnings
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kr_ranking_snapshot.py -k "dedupe_and_sort or freshness_to_meta" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/screening/kr_ranking_snapshot.py tests/test_kr_ranking_snapshot.py
git commit -m "feat(ROB-388): snapshot row dedupe/sort + freshness->honest meta"
```

---

## Task 4: `load_kr_ranking_snapshot` 오케스트레이터 (async, fail-open)

**Files:**
- Modify: `app/mcp_server/tooling/screening/kr_ranking_snapshot.py`
- Test: `tests/test_kr_ranking_snapshot.py`

`KrRankingSnapshotResult`를 정의하고, 주입 가능한 `query_service`로 단위 테스트한다 (DB 불필요). 미커버 sort_by → `None`; `unavailable`(행 0) → `None`; fresh/stale → 결과. enrichment는 다음 Task에서 추가(여기서는 `enrich=False` 기본 경로로 매핑만).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_kr_ranking_snapshot.py
import datetime as dt

from app.services.invest_momentum_events.query_service import MomentumRanking


class _FakeQS:
    """Fake MomentumRankingQueryService: returns canned MomentumRanking per order_type."""
    def __init__(self, by_order_type: dict[str, MomentumRanking]):
        self._by = by_order_type
        self.calls: list[str] = []

    async def get_ranking(self, *, order_type, market, limit, now, **_):
        self.calls.append(order_type)
        return self._by[order_type]


def _ranking(order_type, overall, rows):
    return MomentumRanking(
        market="kr", order_type=order_type, trading_date=None,
        rows=tuple(rows), freshness=Freshness(overall, None, None if overall == "fresh" else "older_than_ttl"),
    )


@pytest.mark.unit
async def test_load_returns_none_for_ineligible_sort():
    qs = _FakeQS({})
    out = await krs.load_kr_ranking_snapshot(
        sort_by="rsi", sort_order="desc", limit=20, query_service=qs, enrich=False
    )
    assert out is None
    assert qs.calls == []  # never queried


@pytest.mark.unit
async def test_load_returns_none_when_unavailable():
    qs = _FakeQS({"up": _ranking("up", "unavailable", [])})
    out = await krs.load_kr_ranking_snapshot(
        sort_by="change_rate", sort_order="desc", limit=20, query_service=qs, enrich=False
    )
    assert out is None  # zero rows -> live fallthrough


@pytest.mark.unit
async def test_load_fresh_change_rate_returns_rows():
    rows = [RankingRow(1, "005930", "삼성전자", 71000.0, 3.5, 100, 5e11, 4e14)]
    qs = _FakeQS({"up": _ranking("up", "fresh", rows)})
    out = await krs.load_kr_ranking_snapshot(
        sort_by="change_rate", sort_order="desc", limit=20, query_service=qs, enrich=False
    )
    assert out is not None
    assert out.data_state == "fresh"
    assert out.total_count == 1
    assert out.rows[0]["symbol"] == "005930"
    assert out.source == "kr_market_ranking"
    assert qs.calls == ["up"]


@pytest.mark.unit
async def test_load_stale_returned_honestly_not_dropped():
    rows = [RankingRow(1, "005930", "삼성", 71000.0, 1.0, 100, 5e11, 4e14)]
    qs = _FakeQS({"quantTop": _ranking("quantTop", "stale", rows)})
    out = await krs.load_kr_ranking_snapshot(
        sort_by="volume", sort_order="desc", limit=20, query_service=qs, enrich=False
    )
    assert out is not None and out.rows  # stale still returns rows (no hard-0)
    assert out.data_state == "stale"
    assert any("오래" in w for w in out.warnings)


@pytest.mark.unit
async def test_load_trade_amount_unions_buckets_and_resorts():
    up = [RankingRow(1, "A", "A", 1.0, 9.0, 10, 100.0, 5.0)]
    qt = [RankingRow(1, "B", "B", 1.0, 1.0, 99, 300.0, 1.0)]
    qs = _FakeQS({"up": _ranking("up", "fresh", up), "quantTop": _ranking("quantTop", "fresh", qt)})
    out = await krs.load_kr_ranking_snapshot(
        sort_by="trade_amount", sort_order="desc", limit=20, query_service=qs, enrich=False
    )
    assert out is not None
    assert [r["symbol"] for r in out.rows] == ["B", "A"]  # 300 > 100
    assert set(qs.calls) == {"up", "quantTop"}


@pytest.mark.unit
async def test_load_fail_open_on_query_error():
    class _Boom:
        async def get_ranking(self, **_):
            raise RuntimeError("db down")
    out = await krs.load_kr_ranking_snapshot(
        sort_by="volume", sort_order="desc", limit=20, query_service=_Boom(), enrich=False
    )
    assert out is None  # fail-open -> live fallthrough
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kr_ranking_snapshot.py -k load_ -v`
Expected: FAIL — `KrRankingSnapshotResult` / `load_kr_ranking_snapshot` not defined.

- [ ] **Step 3: Write minimal implementation**

```python
# add to kr_ranking_snapshot.py
import datetime as dt
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KrRankingSnapshotResult:
    rows: list[dict[str, Any]]
    total_count: int
    data_state: str  # "fresh" | "stale"
    source: str
    latest_snapshot_at: str | None
    warnings: list[str] = field(default_factory=list)
    meta_fields: dict[str, Any] = field(default_factory=dict)


def _build_query_service(session: Any) -> Any:
    from app.services.invest_momentum_events.query_service import (
        MomentumRankingQueryService,
    )
    from app.services.invest_momentum_events.repository import (
        InvestMomentumEventSnapshotsRepository,
    )

    return MomentumRankingQueryService(InvestMomentumEventSnapshotsRepository(session))


async def load_kr_ranking_snapshot(
    *,
    sort_by: str,
    sort_order: str,
    limit: int,
    now: dt.datetime | None = None,
    query_service: Any | None = None,
    enrich: bool = True,
) -> KrRankingSnapshotResult | None:
    """Primary KR discovery source for screen_stocks. Returns a result with rows
    (fresh OR stale, honestly labeled) or None (ineligible sort_by / zero rows /
    any error) so the caller falls through to the live path. Fail-open by design."""
    if not is_snapshot_eligible_sort(sort_by):
        return None
    if now is None:
        now = dt.datetime.now(dt.UTC)

    order_types = order_types_for_sort(sort_by)
    try:
        if query_service is not None:
            return await _run(
                query_service, sort_by, sort_order, limit, now, order_types, enrich, None
            )
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            qs = _build_query_service(session)
            return await _run(
                qs, sort_by, sort_order, limit, now, order_types, enrich, session
            )
    except Exception as exc:  # fail-open: never break screen_stocks
        logger.debug("kr_market_ranking snapshot unavailable, falling back: %s", exc)
        return None


async def _run(
    qs: Any,
    sort_by: str,
    sort_order: str,
    limit: int,
    now: dt.datetime,
    order_types: tuple[str, ...],
    enrich: bool,
    session: Any | None,
) -> KrRankingSnapshotResult | None:
    collected: list[dict[str, Any]] = []
    freshnesses: list[Freshness] = []
    for ot in order_types:
        ranking = await qs.get_ranking(
            order_type=ot, market="kr", limit=max(limit, 50), now=now
        )
        freshnesses.append(ranking.freshness)
        collected.extend(ranking_row_to_screen_row(r) for r in ranking.rows)

    if not collected:
        return None  # unavailable -> live fallthrough

    if sort_by in {"trade_amount", "market_cap"}:
        rows = dedupe_and_sort_rows(collected, sort_by=sort_by, sort_order=sort_order)
    else:
        rows = dedupe_and_sort_rows(collected, sort_by=sort_by, sort_order=sort_order)
    rows = rows[:limit]

    # Worst freshness wins (stale beats fresh when buckets disagree).
    overall = "stale" if any(f.overall == "stale" for f in freshnesses) else "fresh"
    base = next((f for f in freshnesses if f.overall == overall), freshnesses[0])

    if enrich and session is not None:
        rows = await _enrich_rows(rows)  # defined in Task 5

    data_state, meta, warnings = freshness_to_meta(base, row_count=len(rows))
    return KrRankingSnapshotResult(
        rows=rows,
        total_count=len(rows),
        data_state=data_state,
        source="kr_market_ranking",
        latest_snapshot_at=meta.get("latest_snapshot_at"),
        warnings=warnings,
        meta_fields=meta,
    )
```

> Note: Task 5 adds `_enrich_rows`. Until then, `_run` references it only when `enrich and session is not None`; all Task 4 tests pass `enrich=False`, so it is never called yet. Define a temporary stub now to keep the module importable:

```python
async def _enrich_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return rows  # replaced in Task 5
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kr_ranking_snapshot.py -k load_ -v`
Expected: PASS (6 tests). Note: async tests rely on the repo's existing `asyncio_mode=auto` (pytest-asyncio); if a test needs an explicit marker, add `@pytest.mark.asyncio` — check a neighboring async unit test in `tests/` for the project convention.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/screening/kr_ranking_snapshot.py tests/test_kr_ranking_snapshot.py
git commit -m "feat(ROB-388): load_kr_ranking_snapshot orchestrator (fail-open, stale-honest)"
```

---

## Task 5: 행 enrichment (universe + valuation, fail-open)

**Files:**
- Modify: `app/mcp_server/tooling/screening/kr_ranking_snapshot.py`
- Test: `tests/test_kr_ranking_snapshot.py`

RankingRow에 없는 식별/밸류에이션 필드(`code`/`sector`/`instrument_type`, best-effort `per`/`pbr`/`dividend_yield`)를 KRX universe + valuation 캐시에서 보강한다. 위조 금지(없으면 null). 캐시 조회는 주입 가능하게 하여 DB/IO 없이 테스트한다.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_kr_ranking_snapshot.py
@pytest.mark.unit
async def test_enrich_rows_fills_code_sector_valuation_best_effort():
    rows = [
        {"symbol": "005930", "code": "005930", "per": None, "pbr": None,
         "dividend_yield": None, "instrument_type": "stock", "name": "삼성전자"},
    ]
    universe = {"005930": {"code": "KR7005930003", "sector": "반도체", "name": "삼성전자"}}
    valuation = {"005930": {"per": 12.3, "pbr": 1.1, "dividend_yield": 2.5}}

    out = await krs._enrich_rows(
        rows,
        universe_by_code=universe,
        valuation_by_code=valuation,
    )
    assert out[0]["code"] == "KR7005930003"
    assert out[0]["sector"] == "반도체"
    assert out[0]["per"] == 12.3
    assert out[0]["pbr"] == 1.1
    assert out[0]["dividend_yield"] == 2.5


@pytest.mark.unit
async def test_enrich_rows_no_fabrication_when_missing():
    rows = [{"symbol": "999999", "code": "999999", "per": None, "pbr": None,
             "dividend_yield": None, "instrument_type": "stock", "name": "x"}]
    out = await krs._enrich_rows(rows, universe_by_code={}, valuation_by_code={})
    assert out[0]["per"] is None
    assert out[0]["pbr"] is None
    assert out[0]["dividend_yield"] is None
    assert out[0]["code"] == "999999"  # unchanged when no universe match
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kr_ranking_snapshot.py -k enrich_rows -v`
Expected: FAIL — `_enrich_rows` does not accept `universe_by_code` / current stub returns rows unchanged.

- [ ] **Step 3: Replace the stub with the real implementation**

```python
# replace the temporary _enrich_rows stub in kr_ranking_snapshot.py
async def _load_universe_by_code() -> dict[str, dict[str, Any]]:
    from app.services.krx import fetch_stock_all_cached

    out: dict[str, dict[str, Any]] = {}
    for mkt in ("STK", "KSQ"):
        for item in await fetch_stock_all_cached(market=mkt):
            code = str(item.get("short_code") or item.get("code") or "").strip()
            if code:
                out[code] = item
    return out


async def _load_valuation_by_code() -> dict[str, dict[str, Any]]:
    from app.services.krx import fetch_valuation_all_cached

    return await fetch_valuation_all_cached(market="ALL")


async def _enrich_rows(
    rows: list[dict[str, Any]],
    *,
    universe_by_code: dict[str, dict[str, Any]] | None = None,
    valuation_by_code: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Best-effort enrich snapshot rows with code/sector/instrument_type + per/pbr/
    dividend_yield from KRX universe/valuation caches. Fail-open and no fabrication:
    on any cache error or missing key, leave fields as-is (null)."""
    from app.mcp_server.tooling.screening.instrument_type import classify_kr_instrument

    try:
        if universe_by_code is None:
            universe_by_code = await _load_universe_by_code()
        if valuation_by_code is None:
            valuation_by_code = await _load_valuation_by_code()
    except Exception:  # noqa: BLE001 — enrichment is best-effort
        return rows

    for r in rows:
        code = r.get("symbol") or ""
        base = universe_by_code.get(code) or {}
        val = valuation_by_code.get(code) or {}
        if base.get("code"):
            r["code"] = base["code"]
        if base.get("sector") and not r.get("sector"):
            r["sector"] = base["sector"]
        r["instrument_type"] = classify_kr_instrument(
            code, r.get("name"), base.get("subtype")
        )
        if r.get("per") is None:
            r["per"] = val.get("per")
        if r.get("pbr") is None:
            r["pbr"] = val.get("pbr")
        if r.get("dividend_yield") is None:
            r["dividend_yield"] = val.get("dividend_yield")
    return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kr_ranking_snapshot.py -k enrich_rows -v`
Expected: PASS (2 tests). Then run the whole file: `uv run pytest tests/test_kr_ranking_snapshot.py -v` — all green.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/screening/kr_ranking_snapshot.py tests/test_kr_ranking_snapshot.py
git commit -m "feat(ROB-388): best-effort universe/valuation enrichment (fail-open, no fabrication)"
```

---

## Task 6: 사다리 배선 — `_screen_kr_with_fallback` + 회귀 테스트

**Files:**
- Modify: `app/mcp_server/tooling/screening/kr.py:649` (`_screen_kr_with_fallback`)
- Test: `tests/test_mcp_screen_stocks_kr.py`

스냅샷-primary 사다리를 dispatcher 맨 앞에 끼운다. 스냅샷에 행이 있으면(fresh/stale) `_build_screen_response`로 반환하고, `None`이면 기존 tvscreener→legacy 경로로 흐른다.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_mcp_screen_stocks_kr.py
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling.screening import kr as kr_mod
from app.mcp_server.tooling.screening.kr_ranking_snapshot import KrRankingSnapshotResult


@pytest.mark.unit
async def test_fallback_uses_snapshot_when_available():
    snap = KrRankingSnapshotResult(
        rows=[{"symbol": "005930", "name": "삼성전자", "change_rate": 3.5, "market": "kr"}],
        total_count=1, data_state="fresh", source="kr_market_ranking",
        latest_snapshot_at="2026-06-08T00:00:00+00:00",
        warnings=["모멘텀 랭킹 상위 1종목 기반 — 전체 KRX 스캔이 아닙니다."],
        meta_fields={"data_state": "fresh", "source": "kr_market_ranking"},
    )
    with patch.object(
        kr_mod, "load_kr_ranking_snapshot", new=AsyncMock(return_value=snap)
    ):
        resp = await kr_mod._screen_kr_with_fallback(
            market="kr", asset_type=None, category=None, sector=None,
            min_market_cap=None, max_per=None, max_pbr=None, min_dividend_yield=None,
            min_analyst_buy=None, max_rsi=None, sort_by="change_rate",
            sort_order="desc", limit=20,
        )
    assert resp["meta"]["data_state"] == "fresh"
    assert resp["meta"]["source"] == "kr_market_ranking"
    assert resp["stocks"][0]["symbol"] == "005930"


@pytest.mark.unit
async def test_fallback_stale_snapshot_returned_not_hard_zero():
    snap = KrRankingSnapshotResult(
        rows=[{"symbol": "005930", "name": "삼성", "market": "kr"}],
        total_count=1, data_state="stale", source="kr_market_ranking",
        latest_snapshot_at=None,
        warnings=["모멘텀 랭킹 스냅샷이 오래되었습니다(older_than_ttl) — 신규 후보 발굴에 주의하세요."],
        meta_fields={"data_state": "stale", "source": "kr_market_ranking",
                     "retryable": False, "reason": "kr_market_ranking_stale"},
    )
    with patch.object(
        kr_mod, "load_kr_ranking_snapshot", new=AsyncMock(return_value=snap)
    ):
        resp = await kr_mod._screen_kr_with_fallback(
            market="kr", asset_type=None, category=None, sector=None,
            min_market_cap=None, max_per=None, max_pbr=None, min_dividend_yield=None,
            min_analyst_buy=None, max_rsi=None, sort_by="volume",
            sort_order="desc", limit=20,
        )
    assert resp["meta"]["data_state"] == "stale"
    assert len(resp["stocks"]) == 1  # NOT hard-0


@pytest.mark.unit
async def test_fallback_none_snapshot_goes_live():
    """When the snapshot helper returns None (ineligible sort / zero rows / error),
    the legacy live path must still run (here it returns an empty legacy response)."""
    with (
        patch.object(kr_mod, "load_kr_ranking_snapshot", new=AsyncMock(return_value=None)),
        patch.object(
            kr_mod, "_get_tvscreener_stock_capability_snapshot",
            new=AsyncMock(return_value={}),
        ),
        patch.object(kr_mod, "_can_use_tvscreener_stock_path", return_value=False),
        patch.object(
            kr_mod, "_screen_kr",
            new=AsyncMock(return_value={"meta": {"source": "legacy"}, "stocks": []}),
        ),
    ):
        resp = await kr_mod._screen_kr_with_fallback(
            market="kr", asset_type=None, category=None, sector=None,
            min_market_cap=None, max_per=None, max_pbr=None, min_dividend_yield=None,
            min_analyst_buy=None, max_rsi=None, sort_by="rsi",
            sort_order="desc", limit=20,
        )
    assert resp["meta"]["source"] == "legacy"  # fell through to live
```

> Before writing the impl, open `tests/test_mcp_screen_stocks_kr.py` and confirm the response shape these assertions use (`resp["meta"]["data_state"]`, `resp["stocks"]`). `_build_screen_response` puts `meta_fields` under the `meta` key and rows under `stocks` — verify against an existing assertion in that file and adjust key names if the project uses different ones.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_screen_stocks_kr.py -k "fallback_uses_snapshot or fallback_stale or fallback_none" -v`
Expected: FAIL — `kr_mod` has no `load_kr_ranking_snapshot` (not imported/wired yet).

- [ ] **Step 3: Wire the snapshot-primary ladder**

In `app/mcp_server/tooling/screening/kr.py`, add the import near the other screening imports (after line ~32):

```python
from app.mcp_server.tooling.screening.kr_ranking_snapshot import (
    load_kr_ranking_snapshot,
)
```

Then insert the ladder at the very top of `_screen_kr_with_fallback`'s body, before the `try:`/capability snapshot block (after the docstring, ~line 670):

```python
    # ROB-388: snapshot-primary. Serve the durable kr_market_ranking read-model first
    # (fresh OR stale, honestly labeled) so screen_stocks never hard-0s when the live
    # KRX session is down. None => ineligible sort_by / zero rows / error => live path.
    snapshot = await load_kr_ranking_snapshot(
        sort_by=sort_by, sort_order=sort_order, limit=limit
    )
    if snapshot is not None and snapshot.rows:
        return _build_screen_response(
            snapshot.rows,
            snapshot.total_count,
            {
                "market": market,
                "sort_by": sort_by,
                "sort_order": sort_order,
                "asset_type": asset_type,
                "category": category,
            },
            market,
            warnings=snapshot.warnings or None,
            meta_fields=snapshot.meta_fields,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_screen_stocks_kr.py -k "fallback_uses_snapshot or fallback_stale or fallback_none" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the full KR screening regression + lint**

Run: `uv run pytest tests/test_mcp_screen_stocks_kr.py tests/test_kr_ranking_snapshot.py -v`
Expected: all PASS (existing live-path tests still green because snapshot defaults to the real DB and is fail-open → None in unit context; the 3 new tests cover the wired ladder).
Run: `uv run ruff check app/mcp_server/tooling/screening/kr_ranking_snapshot.py app/mcp_server/tooling/screening/kr.py tests/test_kr_ranking_snapshot.py tests/test_mcp_screen_stocks_kr.py`
Expected: `All checks passed!`

> If any pre-existing test in `test_mcp_screen_stocks_kr.py` calls `_screen_kr_with_fallback` for an eligible sort_by (change_rate/volume/trade_amount/market_cap) and asserts the *legacy/tvscreener* path, it may now hit the snapshot branch. Such tests must patch `load_kr_ranking_snapshot` to return `None` (snapshot absent) to assert the live path — update them as part of this step. List each one you change in the commit body.

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/screening/kr.py tests/test_mcp_screen_stocks_kr.py
git commit -m "feat(ROB-388): wire snapshot-primary ladder into _screen_kr_with_fallback"
```

---

## Self-Review

**1. Spec coverage:**
- §3 D1 snapshot-primary → Task 6 (ladder). ✓
- §3 D3 stale 정직 반환 / live는 완전 부재 시 → Task 4 (`unavailable`→None) + Task 6 (`None`→live, rows→return). ✓
- §6 sort_by↔order_type (미커버→live) → Task 1 + Task 4 (`is_snapshot_eligible_sort` → None). ✓
- §6 trade_amount/market_cap union+resort + top-movers warning → Task 3 (`dedupe_and_sort_rows`) + Task 4 (union) + Task 3 (`freshness_to_meta` coverage warning). ✓
- §7 enrichment best-effort null-safe → Task 5. ✓
- §8 정직 리포팅 (data_state/source/latest_snapshot_at/stale_reason/warnings) → Task 3 (`freshness_to_meta`) + Task 6 (meta_fields wired). ✓
- §9 테스트 (단위 + 회귀) → Tasks 1–6 tests. ✓
- §10 비범위 (ROB-446 cron / #2 classifier / investor_flow / live re-auth / migration) → not touched by any task. ✓

**2. Placeholder scan:** No TBD/TODO. The only forward reference is `_enrich_rows` in Task 4 — explicitly stubbed in Task 4 and replaced in Task 5 (called out inline). ✓

**3. Type consistency:** `load_kr_ranking_snapshot` returns `KrRankingSnapshotResult | None` (Task 4) — consumed in Task 6 via `.rows`/`.total_count`/`.warnings`/`.meta_fields`. `freshness_to_meta` returns `(data_state, meta, warnings)` (Task 3), used by `_run` (Task 4). `ranking_row_to_screen_row` field names (`trade_amount`, `per`, `pbr`, `dividend_yield`, `code`, `sector`, `instrument_type`) match what `_enrich_rows` (Task 5) mutates and what `_build_screen_response` consumes (per `_screen_kr`). ✓

**Open verification flags for the implementer (resolve while implementing, do not guess):**
- Response key names: `_build_screen_response` output uses `meta`/`stocks` keys per `_screen_kr`; Task 6 assertions assume this. Confirm against an existing assertion in `test_mcp_screen_stocks_kr.py` (noted inline at Task 6 Step 1).
- pytest async marker convention (`asyncio_mode=auto` vs explicit `@pytest.mark.asyncio`) — match a neighboring async test (noted at Task 4 Step 4).
- Pre-existing eligible-sort tests that assert the live path must patch the snapshot to `None` (noted at Task 6 Step 5).

---

## Execution Handoff

Plan complete. Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task, two-stage review between tasks.
2. **Inline Execution** — execute tasks in this session with checkpoints.
