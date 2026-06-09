# ROB-346 — US 신규매수 후보 universe 필터·우선순위 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** US 신규매수 후보를 예산보다 넓은 pool에서 수집하고, 결정적 품질 게이트(penny/illiquid/abnormal_spike/non_common_stock/screener_stale)로 저품질 후보를 `watch_only`/`rejected`/`data_gap` + 사유로 강등하며, priority 순으로 정렬한다.

**Architecture:** classify_candidate_symbol signature는 **변경하지 않고** 순수 후처리 헬퍼 `demote_for_quality` 를 추가(PR-C와 무충돌). 품질 플래그/priority/confidence_cap은 **collector(US-only gate)** 에서 결정적으로 계산해 candidate evidence(JSONB)에 적재하고, auto_emit가 읽어 데모션·표시한다. migration-0, KR 경로 무회귀.

**Tech Stack:** Python 3.13, async SQLAlchemy, pytest. 스펙: `docs/superpowers/specs/2026-06-09-rob-346-us-candidate-universe-design.md`. 단위: `change_rate`/`week_change_rate` 는 **percent**(10.0=+10%), `latest_close` USD, `daily_volume` 주.

---

## File Structure
- Create: `app/services/action_report/snapshot_backed/candidate_quality.py` — 순수 품질/priority 계산.
- Modify: `app/services/action_report/snapshot_backed/action_verdict.py` — `demote_for_quality` 순수 헬퍼.
- Modify: `app/services/invest_screener_snapshots/repository.py` — `list_candidate_pool` + `common_stock_flags`.
- Modify: `app/services/action_report/snapshot_backed/collectors/candidate_universe.py` — US `_collect_top_gainers` 가 wide pool + 품질 계산 + `_build_candidate_result(quality_by_symbol=...)`.
- Modify: `app/services/action_report/snapshot_backed/auto_emit.py` — 후처리 데모션 + 사유/플래그 surface + 데모션 표시 cap.
- Tests: 각 파일 대응 신규/확장.

---

## Task 1: 순수 품질·priority 모듈

**Files:**
- Create: `app/services/action_report/snapshot_backed/candidate_quality.py`
- Test: `tests/services/action_report/snapshot_backed/test_candidate_quality.py`

- [ ] **Step 1: 실패 테스트**

`tests/services/action_report/snapshot_backed/test_candidate_quality.py`:
```python
from app.services.action_report.snapshot_backed.candidate_quality import (
    compute_quality_flags, compute_priority_score, confidence_cap_for, dollar_volume_usd,
)


def test_penny_and_illiquid_flags():
    qf = compute_quality_flags(latest_close=3.5, daily_volume=1_000_000,
        change_rate=2.0, week_change_rate=1.0, is_common_stock=True, screener_stale=False)
    assert "penny" in qf            # 3.5 < 5.0
    assert "illiquid" in qf         # 3.5 * 1e6 = 3.5e6 < 5e6


def test_liquid_large_price_clean():
    qf = compute_quality_flags(latest_close=150.0, daily_volume=10_000_000,
        change_rate=2.0, week_change_rate=1.0, is_common_stock=True, screener_stale=False)
    assert qf == frozenset()


def test_abnormal_spike_percent_units():
    assert "abnormal_spike" in compute_quality_flags(latest_close=50.0,
        daily_volume=10_000_000, change_rate=16.0, week_change_rate=1.0,
        is_common_stock=True, screener_stale=False)
    assert "abnormal_spike" in compute_quality_flags(latest_close=50.0,
        daily_volume=10_000_000, change_rate=1.0, week_change_rate=51.0,
        is_common_stock=True, screener_stale=False)


def test_common_stock_flag_tri_state():
    assert "non_common_stock" in compute_quality_flags(latest_close=50.0,
        daily_volume=10_000_000, change_rate=1.0, week_change_rate=1.0,
        is_common_stock=False, screener_stale=False)
    assert "common_stock_unknown" in compute_quality_flags(latest_close=50.0,
        daily_volume=10_000_000, change_rate=1.0, week_change_rate=1.0,
        is_common_stock=None, screener_stale=False)


def test_priority_score_orders_liquid_over_illiquid():
    big = compute_priority_score(latest_close=100.0, daily_volume=50_000_000,
        change_rate=5.0, quality_flags=frozenset())
    small = compute_priority_score(latest_close=4.0, daily_volume=100_000,
        change_rate=5.0, quality_flags=frozenset({"illiquid", "penny"}))
    assert big > small


def test_confidence_cap_for_stale():
    assert confidence_cap_for(frozenset({"screener_stale"})) == 40
    assert confidence_cap_for(frozenset()) is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_candidate_quality.py -v`
Expected: FAIL — module 없음.

- [ ] **Step 3: 모듈 구현**

`app/services/action_report/snapshot_backed/candidate_quality.py`:
```python
"""Pure US new-buy candidate quality gates + priority (ROB-346). No I/O.

Units: change_rate / week_change_rate are PERCENT (10.0 == +10%); latest_close
is USD; daily_volume is shares. Conservative thresholds (spec §3.3).
"""
from __future__ import annotations

import math
from typing import Any

PENNY_PRICE_USD = 5.0
ILLIQUID_DOLLAR_VOLUME_USD = 5_000_000.0
ABNORMAL_DAY_CHANGE_PCT = 15.0
ABNORMAL_WEEK_CHANGE_PCT = 50.0
STALE_CONFIDENCE_CAP = 40


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def dollar_volume_usd(latest_close: Any, daily_volume: Any) -> float | None:
    close = _f(latest_close)
    vol = _f(daily_volume)
    if close is None or vol is None:
        return None
    return close * vol


def compute_quality_flags(
    *, latest_close: Any, daily_volume: Any, change_rate: Any,
    week_change_rate: Any, is_common_stock: bool | None, screener_stale: bool,
) -> frozenset[str]:
    flags: set[str] = set()
    if is_common_stock is False:
        flags.add("non_common_stock")
    elif is_common_stock is None:
        flags.add("common_stock_unknown")
    close = _f(latest_close)
    if close is not None and close < PENNY_PRICE_USD:
        flags.add("penny")
    dv = dollar_volume_usd(latest_close, daily_volume)
    if dv is not None and dv < ILLIQUID_DOLLAR_VOLUME_USD:
        flags.add("illiquid")
    cr = _f(change_rate)
    wcr = _f(week_change_rate)
    if (cr is not None and cr > ABNORMAL_DAY_CHANGE_PCT) or (
        wcr is not None and wcr > ABNORMAL_WEEK_CHANGE_PCT
    ):
        flags.add("abnormal_spike")
    if screener_stale:
        flags.add("screener_stale")
    return frozenset(flags)


def compute_priority_score(
    *, latest_close: Any, daily_volume: Any, change_rate: Any,
    quality_flags: frozenset[str],
) -> float:
    dv = dollar_volume_usd(latest_close, daily_volume) or 0.0
    liquidity_term = min(1.0, math.log10(max(dv, 1.0)) / 9.0)  # 9 ≈ log10($1B)
    cr = _f(change_rate) or 0.0
    momentum_term = max(-5.0, min(10.0, cr)) / 10.0
    spike_penalty = 1.0 if "abnormal_spike" in quality_flags else 0.0
    stale_penalty = 1.0 if "screener_stale" in quality_flags else 0.0
    return 1.0 * liquidity_term + 0.5 * momentum_term - 0.5 * spike_penalty - 0.3 * stale_penalty


def confidence_cap_for(quality_flags: frozenset[str]) -> int | None:
    if "screener_stale" in quality_flags or "common_stock_unknown" in quality_flags:
        return STALE_CONFIDENCE_CAP
    return None
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_candidate_quality.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add app/services/action_report/snapshot_backed/candidate_quality.py \
        tests/services/action_report/snapshot_backed/test_candidate_quality.py
git commit -m "feat(ROB-346): pure US candidate quality flags + priority score"
```

---

## Task 2: `demote_for_quality` 순수 헬퍼

**Files:**
- Modify: `app/services/action_report/snapshot_backed/action_verdict.py`
- Test: `tests/services/action_report/snapshot_backed/test_action_verdict_demote.py`

- [ ] **Step 1: 실패 테스트**

`tests/services/action_report/snapshot_backed/test_action_verdict_demote.py`:
```python
from app.services.action_report.snapshot_backed.action_verdict import demote_for_quality


def test_non_common_always_rejected_even_if_buy():
    assert demote_for_quality("buy_review", frozenset({"non_common_stock"})) == (
        "rejected", "non_common_stock")


def test_unknown_common_is_data_gap_for_buy():
    assert demote_for_quality("buy_review", frozenset({"common_stock_unknown"})) == (
        "data_gap", "common_stock_unknown")


def test_quality_demotes_buy_to_watch_in_priority_order():
    assert demote_for_quality("buy_review", frozenset({"penny", "illiquid"})) == (
        "watch_only", "penny")
    assert demote_for_quality("buy_review", frozenset({"abnormal_spike"})) == (
        "watch_only", "abnormal_spike")


def test_clean_buy_unchanged():
    assert demote_for_quality("buy_review", frozenset()) == ("buy_review", None)


def test_non_buy_not_upgraded():
    # 이미 honest 하향된 verdict은 품질로 끌어올리지 않는다(non_common 제외).
    assert demote_for_quality("watch_only", frozenset({"penny"})) == ("watch_only", None)
    assert demote_for_quality("data_gap", frozenset()) == ("data_gap", None)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_action_verdict_demote.py -v`
Expected: FAIL — `demote_for_quality` 없음.

- [ ] **Step 3: 구현** — `action_verdict.py` 끝에 추가:
```python
_QUALITY_WATCH_ORDER: tuple[str, ...] = (
    "penny", "illiquid", "abnormal_spike", "screener_stale",
)


def demote_for_quality(
    verdict: str, quality_flags: frozenset[str]
) -> tuple[str, str | None]:
    """ROB-346 — post-verdict quality demotion. Quality only DEMOTES (never
    upgrades). non_common_stock is always rejected; otherwise only buy_review
    is touched. Returns (new_verdict, reason | None)."""
    if "non_common_stock" in quality_flags:
        return "rejected", "non_common_stock"
    if verdict != "buy_review":
        return verdict, None
    if "common_stock_unknown" in quality_flags:
        return "data_gap", "common_stock_unknown"
    for flag in _QUALITY_WATCH_ORDER:
        if flag in quality_flags:
            return "watch_only", flag
    return "buy_review", None
```

- [ ] **Step 4: 통과 확인 + 커밋**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_action_verdict_demote.py -v` → PASS
```bash
git add app/services/action_report/snapshot_backed/action_verdict.py \
        tests/services/action_report/snapshot_backed/test_action_verdict_demote.py
git commit -m "feat(ROB-346): demote_for_quality pure helper (no signature change)"
```

---

## Task 3: repository — wide pool + is_common_stock

**Files:**
- Modify: `app/services/invest_screener_snapshots/repository.py`
- Test: `tests/services/invest_screener_snapshots/test_repository_candidate_pool.py`

- [ ] **Step 1: 실패 테스트**

`tests/services/invest_screener_snapshots/test_repository_candidate_pool.py`:
```python
import datetime as dt

import pytest

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.us_symbol_universe import USSymbolUniverse
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
)


def _snap(symbol, change_rate, d):
    return InvestScreenerSnapshot(market="us", symbol=symbol, snapshot_date=d,
        latest_close=10, change_rate=change_rate, closes_window=[], source="yahoo",
        daily_volume=1_000_000)


@pytest.mark.asyncio
async def test_list_candidate_pool_returns_wide_unlimited(db_session):
    today = dt.date(2026, 6, 9)
    db_session.add_all([_snap(f"S{i}", float(i), today) for i in range(30)])
    await db_session.flush()
    repo = InvestScreenerSnapshotsRepository(db_session)
    rows = await repo.list_candidate_pool(market="us", limit=None)
    assert len(rows) == 30  # no early cap


@pytest.mark.asyncio
async def test_common_stock_flags_lookup(db_session):
    db_session.add_all([
        USSymbolUniverse(symbol="AAA", exchange="NASDAQ", is_active=True,
                         is_common_stock=True),
        USSymbolUniverse(symbol="ETF1", exchange="NYSE", is_active=True,
                         is_common_stock=False),
    ])
    await db_session.flush()
    repo = InvestScreenerSnapshotsRepository(db_session)
    flags = await repo.common_stock_flags(["AAA", "ETF1", "MISSING"])
    assert flags["AAA"] is True
    assert flags["ETF1"] is False
    assert flags.get("MISSING") is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/invest_screener_snapshots/test_repository_candidate_pool.py -v`
Expected: FAIL — 메서드 없음.

- [ ] **Step 3: 구현** — `repository.py` 의 `list_top_candidates` 아래에 추가:
```python
    async def list_candidate_pool(
        self, *, market: str, limit: int | None = None
    ) -> list[InvestScreenerSnapshot]:
        """ROB-346 — wide candidate pool from the latest partition. ``limit=None``
        returns the whole partition (no early cap); quality/priority filtering
        happens downstream in the collector."""
        latest = await self.latest_partition(market=market)
        if latest is None:
            return []
        stmt = (
            select(InvestScreenerSnapshot)
            .where(
                InvestScreenerSnapshot.market == market,
                InvestScreenerSnapshot.snapshot_date == latest,
            )
            .order_by(
                InvestScreenerSnapshot.change_rate.desc().nullslast(),
                InvestScreenerSnapshot.symbol.asc(),
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def common_stock_flags(
        self, symbols: list[str]
    ) -> dict[str, bool | None]:
        """ROB-346 — us_symbol_universe.is_common_stock by symbol (US-only).
        Missing symbols are absent from the dict (caller treats as unknown)."""
        if not symbols:
            return {}
        from app.models.us_symbol_universe import USSymbolUniverse

        result = await self._session.execute(
            select(USSymbolUniverse.symbol, USSymbolUniverse.is_common_stock).where(
                USSymbolUniverse.symbol.in_(symbols)
            )
        )
        return {sym: flag for sym, flag in result.all()}
```
(`select` 는 이미 import됨. 파일 상단 import 확인.)

- [ ] **Step 4: 통과 확인 + 커밋**

Run: `uv run pytest tests/services/invest_screener_snapshots/test_repository_candidate_pool.py -v` → PASS
```bash
git add app/services/invest_screener_snapshots/repository.py \
        tests/services/invest_screener_snapshots/test_repository_candidate_pool.py
git commit -m "feat(ROB-346): repository wide candidate pool + is_common_stock flags"
```

---

## Task 4: collector — US wide pool + 품질 계산 + evidence 적재

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/candidate_universe.py`
- Test: `tests/services/action_report/snapshot_backed/collectors/test_candidate_universe_quality.py`

- [ ] **Step 1: 실패 테스트 (US 품질/priority가 candidate dict에 실리는지)**

`tests/services/action_report/snapshot_backed/collectors/test_candidate_universe_quality.py`:
```python
import datetime as dt

import pytest

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.us_symbol_universe import USSymbolUniverse
from app.services.action_report.snapshot_backed.collectors.candidate_universe import (
    CandidateUniverseCollector,
)
from app.services.investment_snapshots.collectors import CollectorRequest


def _snap(symbol, *, close, vol, change, d):
    return InvestScreenerSnapshot(market="us", symbol=symbol, snapshot_date=d,
        latest_close=close, change_rate=change, week_change_rate=0,
        closes_window=[], source="yahoo", daily_volume=vol)


@pytest.mark.asyncio
async def test_us_candidates_carry_quality_flags_and_priority(db_session):
    today = dt.date(2026, 6, 9)
    db_session.add_all([
        _snap("GOOD", close=150.0, vol=20_000_000, change=3.0, d=today),
        _snap("PENNY", close=2.0, vol=100_000, change=3.0, d=today),
        USSymbolUniverse(symbol="GOOD", exchange="NASDAQ", is_active=True,
                         is_common_stock=True),
        USSymbolUniverse(symbol="PENNY", exchange="NYSE", is_active=True,
                         is_common_stock=True),
    ])
    await db_session.flush()
    collector = CandidateUniverseCollector(db_session)
    req = CollectorRequest(market="us", account_scope="kis_live", candidate_limit=5,
                           symbols=None)
    results = await collector.collect(req)
    cands = {c["symbol"]: c for c in results[0].payload_json["candidates"]}
    assert "priority_score" in cands["GOOD"]
    assert "penny" in cands["PENNY"]["quality_flags"]
    assert "illiquid" in cands["PENNY"]["quality_flags"]
    # priority: GOOD (liquid) ranks above PENNY
    assert cands["GOOD"]["candidate_rank"] < cands["PENNY"]["candidate_rank"]
```

> `CandidateUniverseCollector` 의 정확한 클래스명/생성자 인자는 `candidate_universe.py` 에서 확인(현재 `self._equity_repo` 를 들고 있음 — 생성자에서 repo를 주입하는지/세션을 받는지 확인 후 fixture 맞춤).

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/collectors/test_candidate_universe_quality.py -v`
Expected: FAIL — quality_flags/priority_score 부재.

- [ ] **Step 3: `_collect_top_gainers` US 분기 구현**

`candidate_universe.py` 상단 import 추가:
```python
from app.core.symbol import to_db_symbol  # 이미 있음
from app.services.action_report.snapshot_backed.candidate_quality import (
    compute_priority_score, compute_quality_flags, confidence_cap_for, dollar_volume_usd,
)
```
`_collect_top_gainers` 에서 US일 때 wide pool + 품질 계산. 현재 `rows = await self._equity_repo.list_top_candidates(...)` 부분을 다음으로 교체(US 분기):
```python
        is_us = request.market == "us"
        if is_us:
            pool_limit = max(limit * 5, 50)
            rows = await self._equity_repo.list_candidate_pool(
                market=request.market, limit=pool_limit
            )
        else:
            rows = await self._equity_repo.list_top_candidates(
                market=request.market, limit=limit
            )
        rows = _dedupe_rows(rows, key=lambda r: to_db_symbol(r.symbol))
```
`days_stale`/`usefulness` 계산은 기존 그대로(이미 위에서 `coverage`/`usefulness` 산출). 그 뒤 품질 맵 구성(US-only):
```python
        quality_by_symbol: dict[str, dict[str, Any]] | None = None
        if is_us:
            stale = days_stale > 0 or usefulness != "useful"
            flags_by_symbol = await self._equity_repo.common_stock_flags(
                [r.symbol for r in rows]
            )
            quality_by_symbol = {}
            for r in rows:
                qf = compute_quality_flags(
                    latest_close=r.latest_close, daily_volume=r.daily_volume,
                    change_rate=r.change_rate, week_change_rate=r.week_change_rate,
                    is_common_stock=flags_by_symbol.get(r.symbol),
                    screener_stale=stale,
                )
                quality_by_symbol[r.symbol] = {
                    "quality_flags": sorted(qf),
                    "dollar_volume_usd": dollar_volume_usd(r.latest_close, r.daily_volume),
                    "priority_score": compute_priority_score(
                        latest_close=r.latest_close, daily_volume=r.daily_volume,
                        change_rate=r.change_rate, quality_flags=qf,
                    ),
                    "confidence_cap": confidence_cap_for(qf),
                    "is_common_stock": flags_by_symbol.get(r.symbol),
                    "week_change_rate": float(r.week_change_rate)
                    if r.week_change_rate is not None else None,
                }
```
> `days_stale` 가 evidence 계산 *뒤*에 산출되면, 위 블록을 `latest_partition_date`/`days_stale` 계산 이후로 배치(파일의 기존 순서 확인 후 정렬).

`evidence = build_candidate_evidence(...)` 는 그대로. 반환 호출에 `quality_by_symbol` 전달:
```python
        return [
            self._build_candidate_result(
                request=request, now=now, market=request.market,
                preset="top_gainers", evidence=evidence, candidate_limit=limit,
                fresh_count=coverage.fresh_count, stale_count=coverage.stale_count,
                last_computed_at=coverage.last_computed_at, usefulness=usefulness,
                expected_baseline_date=baseline,
                latest_partition_date=latest_partition_date, days_stale=days_stale,
                quality_by_symbol=quality_by_symbol,
            )
        ]
```

- [ ] **Step 4: `_build_candidate_result` 가 품질 merge + priority 정렬**

시그니처에 추가: `quality_by_symbol: dict[str, dict[str, Any]] | None = None`.
`candidates` 빌드 직전 priority 정렬(US만; quality 있을 때):
```python
        ordered = list(evidence)
        if quality_by_symbol:
            ordered = sorted(
                evidence,
                key=lambda e: (
                    -(quality_by_symbol.get(e.symbol, {}).get("priority_score") or 0.0),
                    -(quality_by_symbol.get(e.symbol, {}).get("dollar_volume_usd") or 0.0),
                    e.symbol,
                ),
            )
```
`candidates` comprehension을 `ordered` 기반 + quality merge로 교체:
```python
        candidates = [
            {
                **e.to_payload_dict(),
                "rank": rank, "candidate_rank": rank,
                "data_state": freshness_status,
                "toss_parity_status": toss_parity_status,
                **((quality_by_symbol or {}).get(e.symbol, {})),
            }
            for rank, e in enumerate(ordered, start=1)
        ]
```

- [ ] **Step 5: 통과 확인 + 커밋**

Run: `uv run pytest tests/services/action_report/snapshot_backed/collectors/test_candidate_universe_quality.py -v` → PASS
```bash
git add app/services/action_report/snapshot_backed/collectors/candidate_universe.py \
        tests/services/action_report/snapshot_backed/collectors/test_candidate_universe_quality.py
git commit -m "feat(ROB-346): US candidate collector computes quality flags + priority (wide pool)"
```

---

## Task 5: auto_emit — 품질 데모션 + 사유/플래그 surface + 표시 cap

**Files:**
- Modify: `app/services/action_report/snapshot_backed/auto_emit.py`
- Test: `tests/services/action_report/snapshot_backed/test_auto_emit_quality.py`

- [ ] **Step 1: 실패 테스트**

`tests/services/action_report/snapshot_backed/test_auto_emit_quality.py`:
```python
import datetime as dt
from types import SimpleNamespace

from app.services.action_report.snapshot_backed.auto_emit import EvidenceAutoEmitter


def _snap(kind, payload, symbol=None):
    return SimpleNamespace(snapshot_uuid=None, snapshot_kind=kind,
                           payload_json=payload, symbol=symbol)


def _actionable_quote(sym):
    return {"status": "ok", "best_bid": 10, "best_ask": 10.1,
            "bid_depth": 100, "ask_depth": 100}


def test_penny_candidate_demoted_to_watch_with_reason():
    cands = [{"symbol": "PENNY", "rank": 1, "candidate_rank": 1,
              "data_state": "fresh", "quality_flags": ["penny", "illiquid"],
              "priority_score": 0.1, "confidence_cap": None}]
    snaps = [
        _snap("candidate_universe", {"usefulness": "useful", "candidates": cands}),
        _snap("symbol", {"symbol": "PENNY", "quote": _actionable_quote("PENNY")},
              symbol="PENNY"),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="us", account_scope="kis_live",
        now=dt.datetime(2026, 6, 9))
    item = next(i for i in items if i.symbol == "PENNY")
    assert item.evidence_snapshot["action_verdict"] == "watch_only"
    assert item.evidence_snapshot["reject_or_wait_reason"] == "penny"
    assert "penny" in item.evidence_snapshot["quality_flags"]


def test_non_common_candidate_rejected():
    cands = [{"symbol": "ETF1", "rank": 1, "candidate_rank": 1, "data_state": "fresh",
              "quality_flags": ["non_common_stock"], "priority_score": 0.5}]
    snaps = [
        _snap("candidate_universe", {"usefulness": "useful", "candidates": cands}),
        _snap("symbol", {"symbol": "ETF1", "quote": _actionable_quote("ETF1")},
              symbol="ETF1"),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="us", account_scope="kis_live",
        now=dt.datetime(2026, 6, 9))
    item = next(i for i in items if i.symbol == "ETF1")
    assert item.evidence_snapshot["action_verdict"] == "rejected"
    assert item.evidence_snapshot["reject_or_wait_reason"] == "non_common_stock"


def test_clean_candidate_stays_buy_review():
    cands = [{"symbol": "GOOD", "rank": 1, "candidate_rank": 1, "data_state": "fresh",
              "quality_flags": [], "priority_score": 0.9}]
    snaps = [
        _snap("candidate_universe", {"usefulness": "useful", "candidates": cands}),
        _snap("symbol", {"symbol": "GOOD", "quote": _actionable_quote("GOOD")},
              symbol="GOOD"),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="us", account_scope="kis_live",
        now=dt.datetime(2026, 6, 9))
    item = next(i for i in items if i.symbol == "GOOD")
    assert item.evidence_snapshot["action_verdict"] == "buy_review"
```
> `_stamp` 가 `evidence_snapshot["action_verdict"]` 를 채우는지 확인(verdict 인자로 stamp). 채움이 맞으므로 위 단언 사용. 아니라면 `_make_evidence` extra 경로로 단언 조정.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_auto_emit_quality.py -v`
Expected: FAIL — 품질 데모션 미적용.

- [ ] **Step 3: candidate 루프에 데모션 삽입**

`auto_emit.py` import에 추가:
```python
from app.services.action_report.snapshot_backed.action_verdict import (
    classify_candidate_symbol, classify_held_symbol, demote_for_quality,
)
```
(기존 import 라인 확장.)

candidate 루프(`for cand in sorted(candidate_order, key=_candidate_sort_key):`) 내부의
verdict/reason 블록을 교체. 현재:
```python
            verdict = classify_candidate_symbol(
                quote,
                universe_useful=candidate_actionable,
                quote_snapshot_present=symbol_pair is not None,
                candidate_fresh=(cand.get("data_state") or "fresh") == "fresh",
            )
            reject_or_wait_reason: str | None = None
            if verdict == "data_gap":
                reject_or_wait_reason = "quote_missing"
            elif verdict == "watch_only":
                reject_or_wait_reason = (
                    "low_liquidity"
                    if symbol_pair is not None and not _quote_is_actionable(quote)
                    else "screener_stale"
                )
            elif verdict == "buy_review":
                if buy_emitted >= self._max_buy_candidates:
                    verdict = "watch_only"
                    reject_or_wait_reason = "beyond_candidate_budget"
                else:
                    buy_emitted += 1
```
교체:
```python
            base_verdict = classify_candidate_symbol(
                quote,
                universe_useful=candidate_actionable,
                quote_snapshot_present=symbol_pair is not None,
                candidate_fresh=(cand.get("data_state") or "fresh") == "fresh",
            )
            # ROB-346 — quality demotion (pure, no signature change).
            quality_flags = frozenset(cand.get("quality_flags") or [])
            verdict, reject_or_wait_reason = demote_for_quality(
                base_verdict, quality_flags
            )
            if verdict == "data_gap" and reject_or_wait_reason is None:
                reject_or_wait_reason = "quote_missing"
            elif verdict == "watch_only" and reject_or_wait_reason is None:
                reject_or_wait_reason = (
                    "low_liquidity"
                    if symbol_pair is not None and not _quote_is_actionable(quote)
                    else "screener_stale"
                )
            elif verdict == "buy_review":
                if buy_emitted >= self._max_buy_candidates:
                    verdict = "watch_only"
                    reject_or_wait_reason = "beyond_candidate_budget"
                else:
                    buy_emitted += 1
```

- [ ] **Step 4: 표시 cap + quality 필드 surface**

candidate 루프 시작 전 카운터 추가:
```python
        buy_emitted = 0
        demoted_emitted = 0
        _MAX_DEMOTED_SHOWN = 10
```
`items.append(cand_item)` 직전에 데모션 표시 cap 적용(buy_review는 항상, 데모션은 상위
N개만 — candidate_order는 priority 순):
```python
            if verdict != "buy_review":
                if demoted_emitted >= _MAX_DEMOTED_SHOWN:
                    continue  # 노이즈 방지: 상위 N개 데모션만 카드화(집계는 candidate snapshot)
                demoted_emitted += 1
            items.append(cand_item)
```
`_candidate_item` 의 `extra` 에 quality 필드 추가(`extra` dict에 다음 키 삽입):
```python
        "quality_flags": cand.get("quality_flags"),
        "confidence_cap": cand.get("confidence_cap"),
        "candidate_priority_score": cand.get("priority_score"),
```
`_candidate_item` 의 rationale else-chain을 reason-맵으로 일반화. 현재 `elif reject_or_wait_reason == "low_liquidity": ... elif ... "beyond_candidate_budget": ... else: # screener_stale` 를
다음 모듈 상수 + 분기로 교체:
```python
_REASON_KO: dict[str, str] = {
    "low_liquidity": "저유동성",
    "beyond_candidate_budget": "후보 예산 초과",
    "screener_stale": "스크리너 stale",
    "penny": "저가주",
    "illiquid": "초저유동성",
    "abnormal_spike": "비정상 급등",
    "non_common_stock": "일반주 아님(ETF/우선주 등)",
    "common_stock_unknown": "종목 분류 미확인",
    "quote_missing": "호가 스냅샷 없음",
}
```
rationale 분기(is_buy/is_gap 외):
```python
    else:
        reason_ko = _REASON_KO.get(reject_or_wait_reason or "", "관망")
        rationale = f"신규 후보 관망 {priority}순위 — {sym} ({reason_ko})"
```

- [ ] **Step 5: 통과 확인 + 커밋**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_auto_emit_quality.py -v` → PASS
```bash
git add app/services/action_report/snapshot_backed/auto_emit.py \
        tests/services/action_report/snapshot_backed/test_auto_emit_quality.py
git commit -m "feat(ROB-346): auto_emit applies quality demotion, surfaces flags, caps demoted display"
```

---

## Task 6: lint / typecheck / KR 무회귀

- [ ] **Step 1:** `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/` → clean
- [ ] **Step 2:** `uv run ty check app/` → no new errors
- [ ] **Step 3 (KR 무회귀):** `uv run pytest tests/services/action_report/snapshot_backed tests/services/invest_screener_snapshots -v` → PASS. 특히 KR candidate 경로 기존 테스트가 quality 게이트 미적용으로 그대로 통과하는지 확인.
- [ ] **Step 4:** 필요 시 `git commit -m "style(ROB-346): ruff"`

---

## Self-review (작성자 체크)
- 스펙 §3.2~3.6 → Task 3/4(pool+collector), Task 1(품질/priority), Task 2/5(demote+surface) 매핑.
- 타입 일관: `quality_flags` = list(payload) / frozenset(헬퍼); `priority_score` float; `confidence_cap` int|None; reason 문자열 = `_REASON_KO` 키.
- 단위: change_rate/week_change_rate percent(>15.0/>50.0), dollar_volume USD(<5e6), penny<5.0.
- 무충돌: classify_candidate_symbol signature 불변 → PR-C는 같은 루프에 demote_for_budget만 추가.
- 안전: US-only gate, decision_bucket enum 불변, migration 없음, no broker/order mutation, KR 회귀 테스트.
- placeholder 없음.
