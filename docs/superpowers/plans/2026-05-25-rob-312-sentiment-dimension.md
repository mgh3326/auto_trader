# ROB-312 Sentiment Dimension Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic, DB-only, KR-only per-symbol Sentiment evidence bundle (`sentiment_evidence.py`, reading `investor_flow_snapshots` foreign/institution flows) and wire it into the Hermes context export, so Hermes can write a Sentiment dimension report — reusing the generic ROB-306/308 dimension contract (no new table/endpoint/migration).

**Architecture:** Mirror `fundamentals_evidence`. `build_sentiment_evidence` uses the existing `InvestorFlowSnapshotsRepository.latest_by_symbols` to get newest KR investor-flow per symbol, returns a JSON-able per-symbol bundle (non-KR → `unavailable`); the context exporter attaches it under `dimension_evidence["sentiment"]` (best-effort), reusing the same `holdings ∪ top_movers` symbol set the Fundamentals block uses (hoisted to one shared local). DB-only, no LLM. `investor_flow_snapshots` is already populated (ROB-205).

**Tech Stack:** Python 3.13, SQLAlchemy async, Pydantic v2, pytest (`db_session`), `uv`.

**Spec:** `docs/superpowers/specs/2026-05-25-invest-reports-sentiment-dimension-design.md` · **Linear:** ROB-312 · **Branch:** `rob-312`

**Conventions:** `uv run pytest ... -v`; commit trailer `Co-Authored-By: Paperclip <noreply@paperclip.ing>`. Mirror targets: `app/services/investment_dimensions/fundamentals_evidence.py`, `tests/services/investment_dimensions/test_fundamentals_evidence.py`, the `dimension_evidence["fundamentals"]` block in `app/services/investment_stages/hermes_context.py` (~line 143-168), `tests/services/investment_stages/test_hermes_context_fundamentals_dimension.py`. Repo (existing, no new method): `app/services/investor_flow_snapshots/repository.py` `InvestorFlowSnapshotsRepository(session).latest_by_symbols(*, market, symbols, as_of=None) -> list[InvestorFlowSnapshot]`. Model: `app/models/investor_flow_snapshot.py` `InvestorFlowSnapshot` cols `market/symbol/snapshot_date/foreign_net/institution_net/double_buy/double_sell/foreign_consecutive_buy_days/institution_consecutive_buy_days/source` (KR only: `market IN ('kr')`).

---

## File Structure
- Create: `app/services/investment_dimensions/sentiment_evidence.py`
- Create: `tests/services/investment_dimensions/test_sentiment_evidence.py`
- Modify: `app/services/investment_stages/hermes_context.py` (hoist symbol set; add `dimension_evidence["sentiment"]`)
- Test: `tests/services/investment_stages/test_hermes_context_sentiment_dimension.py`

---

## Task 1: Sentiment evidence assembler

**Files:**
- Create: `app/services/investment_dimensions/sentiment_evidence.py`
- Test: `tests/services/investment_dimensions/test_sentiment_evidence.py`

- [ ] **Step 1: Write the failing test**

```python
import datetime as dt

import pytest

from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotsRepository,
)
from app.services.investment_dimensions.sentiment_evidence import (
    build_sentiment_evidence,
)


async def _clear(db_session):
    from sqlalchemy import text
    await db_session.execute(text("DELETE FROM investor_flow_snapshots"))
    await db_session.commit()


def _flow(symbol, *, snapshot_date, foreign_net, double_buy=False):
    return InvestorFlowSnapshot(
        market="kr", symbol=symbol, snapshot_date=snapshot_date,
        foreign_net=foreign_net, institution_net=5000,
        double_buy=double_buy, double_sell=False,
        foreign_consecutive_buy_days=3, institution_consecutive_buy_days=2,
        source="naver_finance",
    )


@pytest.mark.asyncio
async def test_build_sentiment_evidence_kr_covered(db_session):
    await _clear(db_session)
    db_session.add(_flow("005930", snapshot_date=dt.date(2026, 5, 23),
                         foreign_net=120000, double_buy=True))
    await db_session.commit()

    bundle = await build_sentiment_evidence(
        InvestorFlowSnapshotsRepository(db_session),
        market="kr", symbols={"005930", "000660"},
        now=dt.datetime(2026, 5, 24, tzinfo=dt.UTC),
    )
    assert bundle["market"] == "kr"
    assert bundle["data_health"] == {"requested": 2, "covered": 1}
    assert bundle["covered_count"] == 1
    row = bundle["per_symbol"][0]
    assert row["symbol"] == "005930"
    assert row["foreign_net"] == 120000
    assert row["double_buy"] is True
    assert row["foreign_consecutive_buy_days"] == 3
    assert bundle["freshness"]["status"] in {"fresh", "stale"}
    assert bundle["freshness"]["latest_snapshot_date"] == "2026-05-23"


@pytest.mark.asyncio
async def test_build_sentiment_evidence_non_kr_is_unavailable(db_session):
    bundle = await build_sentiment_evidence(
        InvestorFlowSnapshotsRepository(db_session),
        market="us", symbols={"AAPL"},
        now=dt.datetime(2026, 5, 24, tzinfo=dt.UTC),
    )
    assert bundle["market"] == "us"
    assert bundle["per_symbol"] == []
    assert bundle["covered_count"] == 0
    assert bundle["freshness"]["status"] == "unavailable"
    assert bundle["data_health"] == {"requested": 1, "covered": 0}


@pytest.mark.asyncio
async def test_build_sentiment_evidence_empty_kr_is_unavailable(db_session):
    await _clear(db_session)
    bundle = await build_sentiment_evidence(
        InvestorFlowSnapshotsRepository(db_session),
        market="kr", symbols={"005930"},
        now=dt.datetime(2026, 5, 24, tzinfo=dt.UTC),
    )
    assert bundle["covered_count"] == 0
    assert bundle["freshness"]["status"] == "unavailable"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/investment_dimensions/test_sentiment_evidence.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement** `app/services/investment_dimensions/sentiment_evidence.py`:

```python
"""Deterministic Sentiment dimension evidence bundle (ROB-312).

Assembles per-symbol KR investor-flow consensus (foreign/institution net,
double_buy/sell, consecutive-buy streaks from ``investor_flow_snapshots``) into
a market+symbol bundle, mirroring ``fundamentals_evidence``. DB-ONLY, no LLM.

KR-ONLY: investor-flow data is KR-only (``market IN ('kr')``). For non-KR
markets the assembler returns ``unavailable`` (no query). ``investor_flow_
snapshots`` is populated (ROB-205), unlike the other dimensions' sources.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Set
from typing import Any

from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotsRepository,
)

FRESH_WINDOW_DAYS = 5


def _unavailable(market: str, requested: int) -> dict[str, Any]:
    return {
        "market": market,
        "per_symbol": [],
        "covered_count": 0,
        "freshness": {"status": "unavailable", "latest_snapshot_date": None},
        "data_health": {"requested": requested, "covered": 0},
    }


async def build_sentiment_evidence(
    flow_repo: InvestorFlowSnapshotsRepository,
    *,
    market: str,
    symbols: Set[str],
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    requested = len(symbols)
    # KR-only source (investor_flow_snapshots.market IN ('kr')). Non-KR markets
    # have no distinct DB sentiment signal yet → unavailable (no query).
    if market.strip().lower() != "kr":
        return _unavailable(market, requested)

    now_dt = now or dt.datetime.now(tz=dt.UTC)
    rows = await flow_repo.latest_by_symbols(market="kr", symbols=set(symbols))

    per_symbol: list[dict[str, Any]] = []
    latest_date: dt.date | None = None
    for row in rows:
        per_symbol.append(
            {
                "symbol": row.symbol,
                "foreign_net": row.foreign_net,
                "institution_net": row.institution_net,
                "double_buy": bool(row.double_buy),
                "double_sell": bool(row.double_sell),
                "foreign_consecutive_buy_days": row.foreign_consecutive_buy_days,
                "institution_consecutive_buy_days": (
                    row.institution_consecutive_buy_days
                ),
            }
        )
        if latest_date is None or row.snapshot_date > latest_date:
            latest_date = row.snapshot_date

    if not per_symbol:
        return _unavailable(market, requested)

    if latest_date is not None and latest_date >= (
        now_dt.date() - dt.timedelta(days=FRESH_WINDOW_DAYS)
    ):
        status = "fresh"
    else:
        status = "stale"

    return {
        "market": market,
        "per_symbol": per_symbol,
        "covered_count": len(per_symbol),
        "freshness": {
            "status": status,
            "latest_snapshot_date": latest_date.isoformat() if latest_date else None,
        },
        "data_health": {"requested": requested, "covered": len(per_symbol)},
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/investment_dimensions/test_sentiment_evidence.py -v`
Expected: PASS (3 cases).

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_dimensions/sentiment_evidence.py tests/services/investment_dimensions/test_sentiment_evidence.py
git commit -m "feat(rob-312): deterministic Sentiment dimension evidence (KR investor-flow, DB-only)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: Wire Sentiment evidence into the Hermes context export

**Files:**
- Modify: `app/services/investment_stages/hermes_context.py`
- Test: `tests/services/investment_stages/test_hermes_context_sentiment_dimension.py`

To avoid drift, hoist the `holdings ∪ top_movers` symbol set (currently built inside the Fundamentals `try`) into a single local computed once, used by both the Fundamentals and Sentiment blocks.

- [ ] **Step 1: Write the failing test** — mirror `test_hermes_context_fundamentals_dimension.py` (read it first for the exact bundle/run/portfolio-snapshot harness). Seed an `investor_flow_snapshots` KR row for a symbol that is held (in the portfolio snapshot) for a `kr` bundle, build the context, and assert:

```python
# (reuse the fundamentals context test harness: BundleCreate/SnapshotCreate/
#  InvestmentStageRun + HermesContextExporter; portfolio snapshot holds SYMBOL)
payload = await exporter.export(...)   # same invocation as the fundamentals test
assert "sentiment" in payload.dimension_evidence
sent = payload.dimension_evidence["sentiment"]
assert sent["market"] == "kr"
assert any(r["symbol"] == SEEDED_SYMBOL for r in sent["per_symbol"])
assert sent["per_symbol"][0]["double_buy"] in (True, False)
```

Add a second test asserting a `us` bundle yields `dimension_evidence["sentiment"]["freshness"]["status"] == "unavailable"`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/investment_stages/test_hermes_context_sentiment_dimension.py -v`
Expected: FAIL — `"sentiment" not in dimension_evidence`.

- [ ] **Step 3a: Hoist the symbol set** — in `hermes_context.py`, the Fundamentals `try` currently builds `fundamentals_symbols`. Replace that local construction with a shared `dimension_symbols` computed once, immediately before the Fundamentals `try` block:

```python
            dimension_symbols: set[str] = set()
            for snap in snapshots_by_kind.get("portfolio", []):
                for h in (snap.payload_json or {}).get("holdings", []):
                    ticker = h.get("ticker")
                    if ticker:
                        dimension_symbols.add(ticker)
            market_dim = dimension_evidence.get("market")
            if isinstance(market_dim, dict):
                for mover in market_dim.get("top_movers", []):
                    sym = mover.get("symbol")
                    if sym:
                        dimension_symbols.add(sym)
```

Then in the Fundamentals `try`, delete its inline `fundamentals_symbols` gathering and pass `symbols=dimension_symbols` to `build_fundamentals_evidence`.

- [ ] **Step 3b: Add the Sentiment block** — add imports near the fundamentals import:

```python
from app.services.investment_dimensions.sentiment_evidence import (
    build_sentiment_evidence,
)
from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotsRepository,
)
```

And, immediately after the Fundamentals `try/except`, add:

```python
            try:
                sentiment_evidence = await build_sentiment_evidence(
                    InvestorFlowSnapshotsRepository(self._session),
                    market=bundle.market,
                    symbols=dimension_symbols,
                )
                dimension_evidence["sentiment"] = sentiment_evidence
            except Exception as exc:  # noqa: BLE001 — best-effort, like the others
                _logger.exception(
                    "Failed to build sentiment evidence for context export"
                )
                dimension_evidence["sentiment"] = {"unavailable": str(exc)}
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/investment_stages/test_hermes_context_sentiment_dimension.py tests/services/investment_stages/test_hermes_context_fundamentals_dimension.py -v`
Expected: PASS (sentiment new tests + fundamentals still green after the hoist).

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/hermes_context.py tests/services/investment_stages/test_hermes_context_sentiment_dimension.py
git commit -m "feat(rob-312): attach Sentiment evidence to Hermes context export (shared dimension symbols)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: Verification

- [ ] **Step 1:** `uv run pytest tests/services/investment_dimensions/ tests/services/investment_stages/ -q` → all pass (Sentiment + existing Market/News/Fundamentals/dimension; confirms the symbol-hoist didn't regress Fundamentals).
- [ ] **Step 2:** ROB-287 guard: `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -q` → pass. Confirm the assembler imports no broker/LLM: `grep -nE "kis|yahoo|broker|gemini|openai|llm" app/services/investment_dimensions/sentiment_evidence.py` → no matches.
- [ ] **Step 3:** `make lint` → clean.
- [ ] **Step 4:** broad regression: `uv run pytest tests/ -k "hermes or dimension or investor_flow or sentiment" -q` → green.
- [ ] **Step 5:** Open PR. Handoff: branch, PR URL, tests; note KR-only limitation (US/crypto → unavailable), `investor_flow_snapshots` is already populated (ROB-205), and the Sentiment report prose is produced by Hermes via the existing `/hermes/dimension-reports` (dimension="sentiment").

---

## Self-Review (against spec)

**Spec coverage:**
- S1 `build_sentiment_evidence` (KR per-symbol investor-flow, non-KR → unavailable, soft-fail, coverage, freshness) → Task 1. ✓
- S2 context wiring `dimension_evidence["sentiment"]` (shared holdings ∪ top_movers, soft-fail, us → unavailable) → Task 2 (incl. symbol-set hoist to avoid drift). ✓
- S3 tests (KR covered / non-KR unavailable / empty / context export kr+us) → Tasks 1-2. ✓
- Boundaries: no live calls / no LLM (Task 3 grep + guard); no new table/endpoint/migration; reuse existing `latest_by_symbols` (no new repo method); KR-only by design. ✓

**Placeholder scan:** Task 2 Step 1 says "mirror the fundamentals context test, read it first" — explicit instruction against a named file, not deferred work. Assembler (Task 1) is complete code. The hoist (Task 3a) shows exact replacement code.

**Type consistency:** `build_sentiment_evidence(flow_repo, *, market, symbols, now)` identical in Task 1 (def+tests) and Task 2 (call). Return keys `{market, per_symbol, covered_count, freshness:{status, latest_snapshot_date}, data_health:{requested, covered}}` asserted consistently. `latest_by_symbols(*, market, symbols)` matches the real signature (verified). `dimension_symbols` defined once (Task 2 3a) and used by both Fundamentals (rewired) and Sentiment (3b).
```
