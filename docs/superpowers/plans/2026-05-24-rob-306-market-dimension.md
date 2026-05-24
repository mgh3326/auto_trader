# ROB-306 Market Dimension Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Market dimension flow end-to-end — auto_trader assembles a deterministic Market evidence bundle, Hermes writes a "Market analyst report", auto_trader persists it in a new `investment_dimension_reports` table, and a GET surface renders it — establishing the reusable Hermes dimension-report contract.

**Architecture:** Mirror the ROB-301 symbol-intermediate-report pattern on the **dimension** axis. auto_trader stays deterministic (evidence + persistence + freshness confidence cap); Hermes authors the prose (push-only, no in-process LLM). New table keyed `(run_uuid, dimension, market, symbol, artifact_version)` with nullable `symbol` (null = market-wide).

**Tech Stack:** Python 3.13, SQLAlchemy async, Pydantic v2, Alembic, FastAPI, pytest (`db_session` fixture), `uv`.

**Spec:** `docs/superpowers/specs/2026-05-24-invest-reports-market-dimension-design.md`
**Linear:** ROB-306 · **Branch:** `rob-306`

**Conventions:** `uv run pytest ... -v`; commit trailer `Co-Authored-By: Paperclip <noreply@paperclip.ing>`. Templates mirrored: `app/models/investment_symbol_intermediate_reports.py`, `app/services/investment_stages/symbol_report_ingest.py`, `app/schemas/investment_symbol_reports.py`, `app/routers/investment_hermes_http.py` (`/symbol-reports`).

---

## File Structure

**PR1 — data layer (deterministic, no HTTP):**
- Create `app/services/invest_screener_snapshots/repository.py` → add `breadth()` method (modify).
- Create `app/services/investment_dimensions/__init__.py`
- Create `app/services/investment_dimensions/market_evidence.py` — `MarketEvidenceBundle` + `build_market_evidence()`.
- Create `app/models/investment_dimension_reports.py` — `InvestmentDimensionReport` ORM + vocab tuples.
- Create `alembic/versions/<rev>_rob306_investment_dimension_reports.py` — migration.
- Create `app/schemas/investment_dimension_reports.py` — Hermes ingest schemas.
- Create `app/services/investment_dimensions/dimension_report_repository.py` — repository.
- Create `app/services/investment_dimensions/dimension_report_ingest.py` — ingest service (freshness cap + content_hash + upsert).
- Tests under `tests/services/investment_dimensions/` + `tests/test_investment_dimension_reports_model.py`.

**PR2 — Hermes contract + read surface:**
- Modify `app/routers/investment_hermes_http.py` — add `POST /dimension-reports`.
- Modify `app/services/investment_stages/hermes_context.py` — attach Market evidence bundle.
- Create `app/routers/investment_dimension_reports.py` — `GET .../runs/{run_uuid}/dimension-reports`.
- Create `app/services/invest_view_model/dimension_report_view.py` — Korean view-model.
- Tests for route + read surface + context export.

---

# PR1 — Data layer

## Task 1: `breadth()` repository method

**Files:**
- Modify: `app/services/invest_screener_snapshots/repository.py`
- Test: `tests/test_invest_screener_snapshots_repository.py`

- [ ] **Step 1: Write the failing test** (append)

```python
@pytest.mark.asyncio
async def test_breadth_counts_advancers_decliners_in_latest_partition(db_session):
    repo = InvestScreenerSnapshotsRepository(db_session)
    base = dict(market="us", snapshot_date=dt.date(2026, 5, 23), source="yahoo")
    await repo.upsert(SnapshotUpsert(symbol="T_BR_UP1", latest_close=Decimal("10"),
                                     change_rate=Decimal("2.0"), closes_window=[10], **base))
    await repo.upsert(SnapshotUpsert(symbol="T_BR_UP2", latest_close=Decimal("10"),
                                     change_rate=Decimal("0.5"), closes_window=[10], **base))
    await repo.upsert(SnapshotUpsert(symbol="T_BR_DN1", latest_close=Decimal("10"),
                                     change_rate=Decimal("-1.0"), closes_window=[10], **base))
    await db_session.commit()

    b = await repo.breadth(market="us")
    assert b.advancers >= 2
    assert b.decliners >= 1
    assert b.total == b.advancers + b.decliners + b.unchanged
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_invest_screener_snapshots_repository.py::test_breadth_counts_advancers_decliners_in_latest_partition -v`
Expected: FAIL — `AttributeError: ... has no attribute 'breadth'`.

- [ ] **Step 3: Implement** — add to `InvestScreenerSnapshotsRepository` (after `list_top_candidates`), plus a `Breadth` dataclass at module top (next to `CoverageCounts`):

```python
@dataclass(frozen=True)
class Breadth:
    market: str
    partition_date: dt.date | None
    total: int
    advancers: int
    decliners: int
    unchanged: int
```

```python
    async def breadth(self, *, market: str) -> Breadth:
        latest = await self.latest_partition(market=market)
        if latest is None:
            return Breadth(market=market, partition_date=None, total=0,
                           advancers=0, decliners=0, unchanged=0)
        result = await self._session.execute(
            select(
                func.count().label("total"),
                func.count().filter(InvestScreenerSnapshot.change_rate > 0).label("adv"),
                func.count().filter(InvestScreenerSnapshot.change_rate < 0).label("dec"),
            ).where(
                InvestScreenerSnapshot.market == market,
                InvestScreenerSnapshot.snapshot_date == latest,
            )
        )
        row = result.one()
        total, adv, dec = int(row.total or 0), int(row.adv or 0), int(row.dec or 0)
        return Breadth(market=market, partition_date=latest, total=total,
                       advancers=adv, decliners=dec, unchanged=total - adv - dec)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_invest_screener_snapshots_repository.py::test_breadth_counts_advancers_decliners_in_latest_partition -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_screener_snapshots/repository.py tests/test_invest_screener_snapshots_repository.py
git commit -m "feat(rob-306): screener breadth() aggregate

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: Market evidence assembler

**Files:**
- Create: `app/services/investment_dimensions/__init__.py` (empty)
- Create: `app/services/investment_dimensions/market_evidence.py`
- Create: `tests/services/investment_dimensions/__init__.py` (empty)
- Test: `tests/services/investment_dimensions/test_market_evidence.py`

The assembler reuses `screener_evidence.build_candidate_evidence` (ROB-304) for top movers, `breadth()` (Task 1) for advancers/decliners, and `coverage()` for freshness. Pure-ish: takes the repository, returns a JSON-able bundle. Held cross-check is left to the caller (portfolio not loaded here) — `held` is an optional symbol set.

- [ ] **Step 1: Write the failing test**

```python
import datetime as dt
from decimal import Decimal

import pytest

from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
    SnapshotUpsert,
)
from app.services.investment_dimensions.market_evidence import build_market_evidence


@pytest.mark.asyncio
async def test_build_market_evidence_bundle(db_session):
    from sqlalchemy import text

    await db_session.execute(
        text("DELETE FROM invest_screener_snapshots WHERE market = 'us'")
    )
    repo = InvestScreenerSnapshotsRepository(db_session)
    base = dict(market="us", snapshot_date=dt.date(2026, 5, 23), source="yahoo")
    await repo.upsert(SnapshotUpsert(symbol="AAA", latest_close=Decimal("10"),
                                     change_rate=Decimal("5.0"), closes_window=[10],
                                     consecutive_up_days=3, **base))
    await repo.upsert(SnapshotUpsert(symbol="BBB", latest_close=Decimal("10"),
                                     change_rate=Decimal("-2.0"), closes_window=[10], **base))
    await db_session.commit()

    bundle = await build_market_evidence(repo, market="us", held={"AAA"})
    assert bundle["market"] == "us"
    assert bundle["breadth"]["advancers"] >= 1
    assert bundle["breadth"]["decliners"] >= 1
    assert bundle["top_movers"][0]["symbol"] == "AAA"  # highest change_rate
    assert bundle["top_movers"][0]["is_held"] is True
    assert "freshness" in bundle and "partition_date" in bundle["freshness"]
    assert isinstance(bundle["data_health"]["fresh_count"], int)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/investment_dimensions/test_market_evidence.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement** `app/services/investment_dimensions/market_evidence.py`:

```python
"""Deterministic Market dimension evidence bundle (ROB-306).

Assembles breadth + top movers + freshness from the populated KR/US
``invest_screener_snapshots`` (reusing ROB-304 ``screener_evidence``). No prose,
no LLM — this is the raw material Hermes reads to write the Market report.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Set
from typing import Any

from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
)
from app.services.screener_evidence import build_candidate_evidence

TOP_MOVERS_N = 10


async def build_market_evidence(
    repo: InvestScreenerSnapshotsRepository,
    *,
    market: str,
    held: Set[str] = frozenset(),
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    today = (now or dt.datetime.now(tz=dt.UTC)).date()
    coverage = await repo.coverage(market=market, today_trading_date=today)
    breadth = await repo.breadth(market=market)
    rows = await repo.list_top_candidates(market=market, limit=TOP_MOVERS_N)
    evidence = build_candidate_evidence(
        market=market,
        preset="top_gainers",
        rows=[
            {
                "symbol": r.symbol,
                "name": r.symbol,
                "source": r.source,
                "change_rate": r.change_rate,
                "price": r.latest_close,
                "daily_volume": r.daily_volume,
                "consecutive_up_days": r.consecutive_up_days,
            }
            for r in rows
        ],
    )
    top_movers = []
    for ev in evidence:
        d = ev.to_payload_dict()
        d["is_held"] = ev.symbol in held
        top_movers.append(d)

    if coverage.fresh_count > 0:
        freshness_status = "fresh"
    elif coverage.stale_count > 0:
        freshness_status = "stale"
    else:
        freshness_status = "missing"

    return {
        "market": market,
        "breadth": {
            "total": breadth.total,
            "advancers": breadth.advancers,
            "decliners": breadth.decliners,
            "unchanged": breadth.unchanged,
            "advancer_ratio": round(breadth.advancers / breadth.total, 4)
            if breadth.total
            else None,
        },
        "top_movers": top_movers,
        "held_in_market": [m["symbol"] for m in top_movers if m["is_held"]],
        "freshness": {
            "partition_date": breadth.partition_date.isoformat()
            if breadth.partition_date
            else None,
            "status": freshness_status,
            "last_computed_at": coverage.last_computed_at.isoformat()
            if coverage.last_computed_at
            else None,
        },
        "data_health": {
            "fresh_count": coverage.fresh_count,
            "stale_count": coverage.stale_count,
        },
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/services/investment_dimensions/test_market_evidence.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_dimensions/ tests/services/investment_dimensions/
git commit -m "feat(rob-306): deterministic Market evidence bundle

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: `investment_dimension_reports` ORM model

**Files:**
- Create: `app/models/investment_dimension_reports.py`
- Test: `tests/test_investment_dimension_reports_model.py`

Mirror `InvestmentSymbolIntermediateReport` on the dimension axis. Vocab tuples are the single source of truth for DB CHECK + schema Literals.

- [ ] **Step 1: Write the failing test**

```python
from app.models.investment_dimension_reports import (
    DIMENSIONS,
    STANCES,
    InvestmentDimensionReport,
)


def test_model_table_and_vocab():
    assert InvestmentDimensionReport.__tablename__ == "investment_dimension_reports"
    assert InvestmentDimensionReport.__table_args__[-1] == {"schema": "review"}
    assert DIMENSIONS == ("market", "news", "fundamentals", "sentiment")
    assert STANCES == ("bullish", "neutral", "bearish")
    cols = InvestmentDimensionReport.__table__.c
    assert cols["symbol"].nullable is True  # market-wide
    assert cols["dimension"].nullable is False
    assert cols["report_text"].nullable is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_investment_dimension_reports_model.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement** `app/models/investment_dimension_reports.py`:

```python
"""Investment per-dimension analyst reports ORM (ROB-306).

Hermes-authored analyst reports on the DIMENSION axis (market/news/fundamentals/
sentiment), mirroring the symbol axis in
``app.models.investment_symbol_intermediate_reports``. ``symbol`` is nullable:
NULL = market-wide (Market dimension); set = per-symbol (future News/Fundamentals).

    UNIQUE(run_uuid, dimension, market, symbol, artifact_version)

Hermes writes the prose (push-only, no in-process LLM); auto_trader validates,
caps confidence by freshness, and persists.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

DIMENSIONS: tuple[str, ...] = ("market", "news", "fundamentals", "sentiment")
STANCES: tuple[str, ...] = ("bullish", "neutral", "bearish")
MARKETS: tuple[str, ...] = ("kr", "us", "crypto")


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class InvestmentDimensionReport(Base):
    __tablename__ = "investment_dimension_reports"
    __table_args__ = (
        CheckConstraint(
            f"dimension IN ({_sql_in(DIMENSIONS)})",
            name="ck_investment_dimension_reports_dimension",
        ),
        CheckConstraint(
            f"market IN ({_sql_in(MARKETS)})",
            name="ck_investment_dimension_reports_market",
        ),
        CheckConstraint(
            f"stance IS NULL OR stance IN ({_sql_in(STANCES)})",
            name="ck_investment_dimension_reports_stance",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 100)",
            name="ck_investment_dimension_reports_confidence_range",
        ),
        UniqueConstraint(
            "run_uuid",
            "dimension",
            "market",
            "symbol",
            "artifact_version",
            name="uq_investment_dimension_reports_run_dim_market_symbol_ver",
        ),
        Index("ix_investment_dimension_reports_run_uuid", "run_uuid"),
        Index("ix_investment_dimension_reports_run_dimension", "run_uuid", "dimension"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dimension_report_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, unique=True,
        server_default=text("gen_random_uuid()"),
    )
    run_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("review.investment_stage_runs.run_uuid", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_bundle_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    dimension: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    account_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NULL = market-wide (Market). Postgres treats NULLs as distinct in UNIQUE,
    # which is fine: a run has exactly one market-wide Market report per version.
    symbol: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    report_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_findings: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    signals: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    stance: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    missing_data: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    freshness_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    cited_snapshot_uuids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False,
        server_default=text("ARRAY[]::uuid[]"),
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_investment_dimension_reports_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models/investment_dimension_reports.py tests/test_investment_dimension_reports_model.py
git commit -m "feat(rob-306): investment_dimension_reports ORM model

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 4: Alembic migration

**Files:**
- Create: `alembic/versions/<rev>_rob306_investment_dimension_reports.py` (generate via autogenerate)

- [ ] **Step 1: Generate the migration**

Run: `uv run alembic revision --autogenerate -m "rob306 investment_dimension_reports"`
Then open the generated file and verify it creates `review.investment_dimension_reports` with all columns, the 4 CHECK constraints, the UNIQUE, and the 3 indexes from Task 3. Remove any unrelated autogenerated drift (clean-cut: this PR only adds the table).

- [ ] **Step 2: Apply + verify**

Run: `uv run alembic upgrade head`
Then: `uv run alembic downgrade -1 && uv run alembic upgrade head` (round-trip clean).
Expected: no errors; table exists.

- [ ] **Step 3: Commit**

```bash
git add alembic/versions/*rob306*
git commit -m "feat(rob-306): alembic migration for investment_dimension_reports

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 5: Hermes ingest schema

**Files:**
- Create: `app/schemas/investment_dimension_reports.py`
- Test: `tests/test_investment_dimension_reports_schema.py`

Mirror `app/schemas/investment_symbol_reports.py`. Unlike symbol reports, **`stance` and `confidence` ARE accepted** (a dimension report is Hermes's analysis, not a derived verdict). `extra="forbid"`. Reuse `HermesStageRunEnvelope`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from pydantic import ValidationError

from app.schemas.investment_dimension_reports import (
    HermesDimensionReport,
    HermesDimensionReportsIngestRequest,
)


def test_dimension_report_rejects_unknown_dimension():
    with pytest.raises(ValidationError):
        HermesDimensionReport(dimension="macro", report_text="x")


def test_dimension_report_accepts_market_wide_null_symbol():
    r = HermesDimensionReport(
        dimension="market", report_text="시장 개요", stance="bullish",
        confidence=70, key_findings=["상승 우위"], signals={"breadth": "60% adv"},
    )
    assert r.symbol is None and r.stance == "bullish"


def test_ingest_request_forbids_extra():
    with pytest.raises(ValidationError):
        HermesDimensionReportsIngestRequest(
            run_envelope={"run_uuid": "x"}, dimension_reports=[], bogus=1
        )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_investment_dimension_reports_schema.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement** `app/schemas/investment_dimension_reports.py`:

```python
"""Hermes dimension-report ingest schemas (ROB-306).

Push-only (Hermes pulls context, writes the per-dimension analyst report
out-of-process, PUSHES here). auto_trader validates + persists; never calls an
LLM in-process. Vocab tuples come from the ORM model (single source of truth).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.investment_dimension_reports import DIMENSIONS, MARKETS, STANCES
from app.schemas.hermes_composition import HermesStageRunEnvelope

HERMES_DIMENSION_REPORTS_VERSION = "hermes-dimension-reports.v1"
MAX_DIMENSION_REPORTS_PER_CALL = 50


class HermesDimensionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: str
    market: str = "us"
    symbol: str | None = None  # null = market-wide
    report_text: str | None = None
    key_findings: list[Any] | None = None
    signals: dict[str, Any] | None = None
    stance: str | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    missing_data: list[Any] | None = None
    freshness_summary: dict[str, Any] | None = None
    cited_snapshot_uuids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("dimension")
    @classmethod
    def _dim_in_vocab(cls, v: str) -> str:
        if v not in DIMENSIONS:
            raise ValueError(f"dimension={v!r} not in {DIMENSIONS!r}")
        return v

    @field_validator("market")
    @classmethod
    def _market_in_vocab(cls, v: str) -> str:
        if v not in MARKETS:
            raise ValueError(f"market={v!r} not in {MARKETS!r}")
        return v

    @field_validator("stance")
    @classmethod
    def _stance_in_vocab(cls, v: str | None) -> str | None:
        if v is not None and v not in STANCES:
            raise ValueError(f"stance={v!r} not in {STANCES!r}")
        return v


class HermesDimensionReportsIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_version: Literal["hermes-dimension-reports.v1"] = (
        HERMES_DIMENSION_REPORTS_VERSION
    )
    run_envelope: HermesStageRunEnvelope
    dimension_reports: list[HermesDimensionReport] = Field(
        min_length=1, max_length=MAX_DIMENSION_REPORTS_PER_CALL
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_investment_dimension_reports_schema.py -v`
Expected: PASS (3 cases).

- [ ] **Step 5: Commit**

```bash
git add app/schemas/investment_dimension_reports.py tests/test_investment_dimension_reports_schema.py
git commit -m "feat(rob-306): Hermes dimension-report ingest schema

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 6: Repository + ingest service (freshness cap + content_hash + upsert)

**Files:**
- Create: `app/services/investment_dimensions/dimension_report_repository.py`
- Create: `app/services/investment_dimensions/dimension_report_ingest.py`
- Test: `tests/services/investment_dimensions/test_dimension_report_ingest.py`

Mirror `symbol_report_ingest.py` minus verdict-derivation (dimension reports keep Hermes's `stance`). auto_trader **caps confidence by freshness** (reusing the PR1 policy: fresh→100, partial→60, stale→40, missing→20). `content_hash` drives idempotent upsert.

- [ ] **Step 1: Write the failing test** (uses `db_session` + a seeded stage run)

```python
import datetime as dt
import uuid

import pytest

from app.models.investment_stages import InvestmentStageRun
from app.schemas.investment_dimension_reports import (
    HermesDimensionReport,
    HermesDimensionReportsIngestRequest,
)
from app.schemas.hermes_composition import HermesStageRunEnvelope
from app.services.investment_dimensions.dimension_report_ingest import (
    DimensionReportIngestService,
)


async def _seed_run(db_session) -> InvestmentStageRun:
    run = InvestmentStageRun(
        run_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=uuid.uuid4(),
        market="us",
        account_scope=None,
        policy_version="v1",
        generator_version="v1",
        status="running",
        started_at=dt.datetime.now(tz=dt.UTC),
    )
    db_session.add(run)
    await db_session.commit()
    return run


def _request(run, *, confidence, freshness_status):
    return HermesDimensionReportsIngestRequest(
        run_envelope=HermesStageRunEnvelope(
            run_uuid=run.run_uuid,
            snapshot_bundle_uuid=run.snapshot_bundle_uuid,
            market="us",
            account_scope=None,
            market_session=None,
        ),
        dimension_reports=[
            HermesDimensionReport(
                dimension="market", market="us", report_text="미국 시장 개요",
                stance="bullish", confidence=confidence,
                key_findings=["상승 우위 60%"], signals={"breadth": "60% adv"},
                freshness_summary={"status": freshness_status},
            )
        ],
    )


@pytest.mark.asyncio
async def test_ingest_persists_and_caps_confidence_by_freshness(db_session):
    run = await _seed_run(db_session)
    svc = DimensionReportIngestService(db_session)
    resp = await svc.ingest_from_hermes(
        _request(run, confidence=90, freshness_status="stale")
    )
    await db_session.commit()
    rep = resp.results[0].report
    assert rep.dimension == "market"
    assert rep.symbol is None
    assert rep.stance == "bullish"
    assert rep.confidence == 40  # capped: stale → 40 (was 90)


@pytest.mark.asyncio
async def test_ingest_is_idempotent(db_session):
    run = await _seed_run(db_session)
    svc = DimensionReportIngestService(db_session)
    req = _request(run, confidence=50, freshness_status="fresh")
    r1 = await svc.ingest_from_hermes(req)
    await db_session.commit()
    r2 = await svc.ingest_from_hermes(req)
    await db_session.commit()
    assert r2.results[0].idempotent_existing is True
    assert r1.results[0].report.dimension_report_uuid == r2.results[0].report.dimension_report_uuid


@pytest.mark.asyncio
async def test_ingest_rejects_unknown_run(db_session):
    from app.services.investment_dimensions.dimension_report_ingest import (
        DimensionReportIngestError,
    )

    run = await _seed_run(db_session)
    svc = DimensionReportIngestService(db_session)
    bad = _request(run, confidence=50, freshness_status="fresh")
    object.__setattr__(bad.run_envelope, "run_uuid", uuid.uuid4())
    with pytest.raises(DimensionReportIngestError):
        await svc.ingest_from_hermes(bad)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/investment_dimensions/test_dimension_report_ingest.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the repository** `app/services/investment_dimensions/dimension_report_repository.py`:

```python
"""Repository for investment_dimension_reports (ROB-306). Service-internal."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_dimension_reports import InvestmentDimensionReport


class DimensionReportPersistRace(RuntimeError):
    pass


class DimensionReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_idempotency_key(
        self, key: str
    ) -> InvestmentDimensionReport | None:
        result = await self._session.execute(
            select(InvestmentDimensionReport).where(
                InvestmentDimensionReport.idempotency_key == key
            )
        )
        return result.scalar_one_or_none()

    async def next_version(
        self, *, run_uuid: uuid.UUID, dimension: str, market: str, symbol: str | None
    ) -> int:
        stmt = select(func.max(InvestmentDimensionReport.artifact_version)).where(
            InvestmentDimensionReport.run_uuid == run_uuid,
            InvestmentDimensionReport.dimension == dimension,
            InvestmentDimensionReport.market == market,
        )
        stmt = stmt.where(
            InvestmentDimensionReport.symbol.is_(None)
            if symbol is None
            else InvestmentDimensionReport.symbol == symbol
        )
        result = await self._session.execute(stmt)
        return int((result.scalar_one_or_none() or 0) + 1)

    async def persist(self, **fields: Any) -> InvestmentDimensionReport:
        row = InvestmentDimensionReport(**fields)
        self._session.add(row)
        await self._session.flush()
        return row
```

- [ ] **Step 4: Implement the ingest service** `app/services/investment_dimensions/dimension_report_ingest.py`:

```python
"""Hermes dimension-report ingest service (ROB-306).

Validates + persists Hermes-pushed per-dimension analyst reports (push-only —
never calls an LLM in-process). Mirrors symbol_report_ingest.py, minus verdict
derivation: a dimension report keeps Hermes's ``stance``. auto_trader caps
``confidence`` by the report's freshness status.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_stages import InvestmentStageRun
from app.models.investment_dimension_reports import InvestmentDimensionReport
from app.schemas.investment_dimension_reports import (
    HermesDimensionReport,
    HermesDimensionReportsIngestRequest,
)
from app.services.investment_dimensions.dimension_report_repository import (
    DimensionReportRepository,
)
from app.services.investment_stages.repository import InvestmentStagesRepository

# Reuse the ROB-304 freshness cap policy.
_FRESHNESS_CAP = {"fresh": 100, "partial": 60, "stale": 40, "missing": 20}


class DimensionReportIngestError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DimensionReportIngestResult:
    dimension: str
    report: InvestmentDimensionReport
    idempotent_existing: bool


@dataclass(frozen=True)
class DimensionReportsIngestResponse:
    run: InvestmentStageRun
    results: list[DimensionReportIngestResult]


def _freshness_status(payload: HermesDimensionReport) -> str:
    fs = payload.freshness_summary or {}
    status = fs.get("status")
    return status if status in _FRESHNESS_CAP else "partial"


def cap_confidence(payload: HermesDimensionReport) -> int | None:
    if payload.confidence is None:
        return None
    cap = _FRESHNESS_CAP.get(_freshness_status(payload), 40)
    return min(payload.confidence, cap)


def content_hash(payload: HermesDimensionReport, *, capped_confidence: int | None) -> str:
    canonical: dict[str, Any] = {
        "dimension": payload.dimension,
        "market": payload.market,
        "symbol": payload.symbol,
        "report_text": payload.report_text,
        "key_findings": payload.key_findings or [],
        "signals": payload.signals or {},
        "stance": payload.stance,
        "confidence": capped_confidence,
        "missing_data": payload.missing_data or [],
        "freshness_summary": payload.freshness_summary or {},
        "cited_snapshot_uuids": sorted(str(u) for u in payload.cited_snapshot_uuids),
    }
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class DimensionReportIngestService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        stages_repository: InvestmentStagesRepository | None = None,
        reports_repository: DimensionReportRepository | None = None,
    ) -> None:
        self._session = session
        self._stages = stages_repository or InvestmentStagesRepository(session)
        self._reports = reports_repository or DimensionReportRepository(session)

    async def ingest_from_hermes(
        self, request: HermesDimensionReportsIngestRequest
    ) -> DimensionReportsIngestResponse:
        envelope = request.run_envelope
        run = await self._stages.get_run(envelope.run_uuid)
        if run is None:
            raise DimensionReportIngestError(
                f"stage run not found: {envelope.run_uuid}",
                code="stage_run_not_found",
            )
        if run.snapshot_bundle_uuid != envelope.snapshot_bundle_uuid or (
            run.market != envelope.market
        ):
            raise DimensionReportIngestError(
                f"envelope inconsistent with stage run {envelope.run_uuid}",
                code="run_envelope_mismatch",
            )

        results: list[DimensionReportIngestResult] = []
        for payload in request.dimension_reports:
            report, idem = await self._persist_or_reuse(run=run, payload=payload)
            results.append(
                DimensionReportIngestResult(
                    dimension=payload.dimension, report=report, idempotent_existing=idem
                )
            )
        return DimensionReportsIngestResponse(run=run, results=results)

    async def _persist_or_reuse(
        self, *, run: InvestmentStageRun, payload: HermesDimensionReport
    ) -> tuple[InvestmentDimensionReport, bool]:
        capped = cap_confidence(payload)
        digest = content_hash(payload, capped_confidence=capped)
        key = (
            f"{run.run_uuid}:{payload.dimension}:{payload.market}:"
            f"{payload.symbol or ''}:{digest}"
        )
        existing = await self._reports.get_by_idempotency_key(key)
        if existing is not None:
            return existing, True

        version = await self._reports.next_version(
            run_uuid=run.run_uuid, dimension=payload.dimension,
            market=payload.market, symbol=payload.symbol,
        )
        report = await self._reports.persist(
            run_uuid=run.run_uuid,
            snapshot_bundle_uuid=run.snapshot_bundle_uuid,
            dimension=payload.dimension,
            market=payload.market,
            account_scope=run.account_scope,
            symbol=payload.symbol,
            artifact_version=version,
            report_text=payload.report_text,
            key_findings=payload.key_findings,
            signals=payload.signals,
            stance=payload.stance,
            confidence=capped,
            missing_data=payload.missing_data,
            freshness_summary=payload.freshness_summary,
            content_hash=digest,
            cited_snapshot_uuids=list(payload.cited_snapshot_uuids),
            idempotency_key=key,
        )
        return report, False
```

- [ ] **Step 5: Run to verify tests pass**

Run: `uv run pytest tests/services/investment_dimensions/test_dimension_report_ingest.py -v`
Expected: PASS (3 cases). Note: verify `InvestmentStageRun` constructor field names against `app/models/investment_stages.py` and `HermesStageRunEnvelope` fields against `app/schemas/hermes_composition.py` before running; adjust the test's `_seed_run`/`_request` kwargs to match exactly.

- [ ] **Step 6: Commit**

```bash
git add app/services/investment_dimensions/dimension_report_repository.py app/services/investment_dimensions/dimension_report_ingest.py tests/services/investment_dimensions/test_dimension_report_ingest.py
git commit -m "feat(rob-306): dimension-report repository + ingest service (freshness cap, idempotent)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 7: PR1 verification

- [ ] **Step 1:** `uv run pytest tests/services/investment_dimensions/ tests/test_investment_dimension_reports_model.py tests/test_investment_dimension_reports_schema.py tests/test_invest_screener_snapshots_repository.py -v` → all pass.
- [ ] **Step 2:** ROB-287 guard: `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -v` → pass (new modules introduce no LLM imports).
- [ ] **Step 3:** `make lint` → clean.
- [ ] **Step 4:** Open PR1 (data layer). Title `feat(rob-306): Market dimension data layer — evidence bundle + dimension_reports table/ingest (PR1)`. Note migration is operator-applied.

---

# PR2 — Hermes contract + read surface

## Task 8: Hermes `POST /dimension-reports` route

**Files:**
- Modify: `app/routers/investment_hermes_http.py`
- Test: `tests/test_investment_hermes_http_dimension_reports.py` (mirror the existing symbol-reports route test — find it via `grep -rl "symbol-reports" tests/`)

- [ ] **Step 1: Write the failing test** — mirror the existing `/symbol-reports` route test: assert (a) 403 when ingest token unset, (b) happy-path 200 with a seeded run + valid `HermesDimensionReportsIngestRequest` body returns `{"success": True, "dimension_reports": [...]}`. (Copy the auth/setup harness from the symbol-reports test file verbatim, swapping the body + endpoint.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_investment_hermes_http_dimension_reports.py -v`
Expected: FAIL — 404 (route not defined).

- [ ] **Step 3: Implement** — add to `app/routers/investment_hermes_http.py` (mirror `/symbol-reports`, lines 318-365). Add imports for `HermesDimensionReportsIngestRequest`, `DimensionReportIngestService`, `DimensionReportIngestError`, and extend `_INGEST_ERROR_HTTP_STATUS` with `"stage_run_not_found": 404, "run_envelope_mismatch": 409`:

```python
@router.post("/dimension-reports")
async def dimension_reports_ingest(
    body: HermesDimensionReportsIngestRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Hermes push-only ingest of per-dimension analyst reports (ROB-306).

    Same ``/trading/api/investment-reports/hermes`` prefix, so the
    AuthMiddleware token branch (403 unset / 401 wrong) + enable gate apply.
    """
    _require_enabled()

    svc = DimensionReportIngestService(db)
    try:
        response = await svc.ingest_from_hermes(body)
    except DimensionReportIngestError as exc:
        await db.rollback()
        http_status = _INGEST_ERROR_HTTP_STATUS.get(
            exc.code, status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=http_status,
            detail={"error": exc.code, "message": str(exc)},
        ) from exc
    await db.commit()
    return {
        "success": True,
        "run_uuid": str(response.run.run_uuid),
        "dimension_reports": [
            {
                "dimension": r.dimension,
                "dimension_report_uuid": str(r.report.dimension_report_uuid),
                "market": r.report.market,
                "symbol": r.report.symbol,
                "stance": r.report.stance,
                "confidence": r.report.confidence,
                "artifact_version": r.report.artifact_version,
                "idempotent_existing": r.idempotent_existing,
            }
            for r in response.results
        ],
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_investment_hermes_http_dimension_reports.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routers/investment_hermes_http.py tests/test_investment_hermes_http_dimension_reports.py
git commit -m "feat(rob-306): Hermes POST /dimension-reports ingest route

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 9: Context export — attach Market evidence bundle

**Files:**
- Modify: `app/services/investment_stages/hermes_context.py`
- Test: `tests/services/investment_stages/test_hermes_context_market_dimension.py`

Goal: when Hermes pulls context for a KR/US run, include `dimension_evidence: {"market": <build_market_evidence bundle>}` so it has the material to write the Market report. Gated by the existing enable flag (the context export already runs behind it).

- [ ] **Step 1: Read `hermes_context.py`** to find the context-payload assembly point and the `market`/held inputs available. Identify where to attach `dimension_evidence`.

- [ ] **Step 2: Write the failing test** — build a context for a seeded run with KR/US screener rows present; assert the exported payload contains `dimension_evidence["market"]["breadth"]` and `top_movers`. (Model the test on the existing hermes_context tests — `grep -rl "hermes_context\|HermesContext" tests/`.)

- [ ] **Step 3: Run to verify it fails** — Expected: KeyError / missing `dimension_evidence`.

- [ ] **Step 4: Implement** — in the context assembly, call `build_market_evidence(InvestScreenerSnapshotsRepository(session), market=run.market, held=<held set from portfolio snapshot if present else frozenset()>)` and attach under `dimension_evidence["market"]`. Guard with `if run.market in ("kr", "us")` (crypto deferred). Keep it best-effort: on exception, attach `dimension_evidence={"market": {"unavailable": reason}}` rather than failing the context.

- [ ] **Step 5: Run to verify it passes** — Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/investment_stages/hermes_context.py tests/services/investment_stages/test_hermes_context_market_dimension.py
git commit -m "feat(rob-306): attach Market evidence bundle to Hermes context export

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 10: Read surface — GET dimension-reports + view-model

**Files:**
- Create: `app/services/invest_view_model/dimension_report_view.py`
- Create: `app/routers/investment_dimension_reports.py`
- Modify: wherever routers are registered (find via `grep -rn "include_router" app/main.py app/routers/__init__.py`)
- Test: `tests/test_investment_dimension_reports_router.py`

- [ ] **Step 1: Write the failing test** — seed a run + ingest one market dimension report (reuse the Task 6 service), then `GET /trading/api/investment-reports/runs/{run_uuid}/dimension-reports?dimension=market` returns 200 with a view-model: `{"runUuid", "dimension":"market", "reports":[{"market","stance","confidenceLabel","reportText","keyFindings","signals","freshness"}]}`. Use the app TestClient harness from `tests/conftest.py`.

- [ ] **Step 2: Run to verify it fails** — Expected: 404 (route not registered).

- [ ] **Step 3: Implement view-model** `app/services/invest_view_model/dimension_report_view.py`:

```python
"""Read-only Korean view-model for dimension reports (ROB-306)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_dimension_reports import InvestmentDimensionReport

_STANCE_KO = {"bullish": "강세", "neutral": "중립", "bearish": "약세"}


async def build_dimension_reports_view(
    session: AsyncSession, *, run_uuid: uuid.UUID, dimension: str | None
) -> dict[str, Any]:
    stmt = select(InvestmentDimensionReport).where(
        InvestmentDimensionReport.run_uuid == run_uuid
    )
    if dimension is not None:
        stmt = stmt.where(InvestmentDimensionReport.dimension == dimension)
    stmt = stmt.order_by(
        InvestmentDimensionReport.dimension,
        InvestmentDimensionReport.artifact_version.desc(),
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return {
        "runUuid": str(run_uuid),
        "dimension": dimension,
        "reports": [
            {
                "dimension": r.dimension,
                "market": r.market,
                "symbol": r.symbol,
                "stance": r.stance,
                "stanceLabel": _STANCE_KO.get(r.stance or "", "-"),
                "confidence": r.confidence,
                "confidenceLabel": f"{r.confidence}%" if r.confidence is not None else "-",
                "reportText": r.report_text,
                "keyFindings": r.key_findings or [],
                "signals": r.signals or {},
                "freshness": r.freshness_summary or {},
                "artifactVersion": r.artifact_version,
            }
            for r in rows
        ],
    }
```

- [ ] **Step 4: Implement router** `app/routers/investment_dimension_reports.py`:

```python
"""GET read surface for dimension reports (ROB-306, read-only)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.services.invest_view_model.dimension_report_view import (
    build_dimension_reports_view,
)

router = APIRouter(prefix="/trading/api/investment-reports", tags=["investment-reports"])


@router.get("/runs/{run_uuid}/dimension-reports")
async def get_dimension_reports(
    run_uuid: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    dimension: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    return await build_dimension_reports_view(db, run_uuid=run_uuid, dimension=dimension)
```

Then register it where the other investment-reports routers are included (mirror the existing `include_router(...)` line; confirm the exact `get_db` import path against `app/routers/investment_dimension_reports.py` peers).

- [ ] **Step 5: Run to verify it passes** — Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/invest_view_model/dimension_report_view.py app/routers/investment_dimension_reports.py app/main.py tests/test_investment_dimension_reports_router.py
git commit -m "feat(rob-306): GET dimension-reports read surface + Korean view-model

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 11: PR2 verification

- [ ] **Step 1:** `uv run pytest -k "dimension_report or hermes_context or investment_hermes" -v` → all pass.
- [ ] **Step 2:** ROB-287 import guard → pass. `make lint` → clean.
- [ ] **Step 3:** broad regression: `uv run pytest tests/ -k "investment or hermes or screener" -q` → green.
- [ ] **Step 4:** Open PR2. Handoff comment (ROB-306 AC): branch, PR URLs, tests, migration (operator-applied), config flag (`SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED`), what's operator-gated (Hermes round-trip), and that final synthesis is the next slice.

---

## Self-Review (against spec)

**Spec coverage:**
- M1 Market evidence bundle → Tasks 1–2 (`breadth` + `build_market_evidence`, reuses screener_evidence + new/held split). ✓
- M2 `investment_dimension_reports` table → Tasks 3–4 (model + migration); repository/ingest + freshness cap + idempotent upsert → Task 6. ✓
- M3 Hermes ingest contract → Task 5 (schema) + Task 8 (route, token-auth via existing prefix) + Task 9 (context export carries the Market bundle). ✓
- M4 read surface → Task 10 (GET + Korean view-model). ✓
- Boundaries: no in-process LLM (Task 7/11 import guard); no broker mutation (read-only services); migration operator-applied (Task 4); no final synthesis (out of scope, noted). ✓

**Placeholder scan:** Tasks 8–10 carry "find/verify via grep" notes for the exact test harness + router-registration + envelope field names — these are explicit verification instructions against named files, not deferred work. Core new files (model, schema, repository, ingest, evidence, view-model, routes) have complete code.

**Type consistency:** `build_market_evidence(repo, *, market, held, now)` (T2) ↔ called in T9. `HermesDimensionReport`/`HermesDimensionReportsIngestRequest` (T5) ↔ ingest service (T6) ↔ route (T8). `DimensionReportIngestService.ingest_from_hermes` → `DimensionReportsIngestResponse.results[].report` used in T8/T10. `DIMENSIONS/STANCES/MARKETS` defined once in the model (T3), imported by schema (T5). `breadth()`/`Breadth` (T1) ↔ used in T2.
```
