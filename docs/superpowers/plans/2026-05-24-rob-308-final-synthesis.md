# ROB-308 Final Synthesis Composer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the `/invest/reports` synthesis loop — feed the persisted dimension (ROB-306) + symbol (ROB-301) reports into the Hermes context, let the composition cite dimension reports, and classify final items held-action vs new-candidate with per-item source citations.

**Architecture:** Additive extension of the ROB-287 Hermes composition contract. auto_trader reads the analyst reports into `HermesContextPayload` and validates+persists the richer composition; Hermes authors the synthesis (push, no in-process LLM). Items stay advisory-only.

**Tech Stack:** Python 3.13, SQLAlchemy async, Pydantic v2, Alembic, FastAPI, pytest (`db_session`), `uv`.

**Spec:** `docs/superpowers/specs/2026-05-24-invest-reports-final-synthesis-design.md` · **Linear:** ROB-308 · **Branch:** `rob-308`

**Conventions:** `uv run pytest ... -v`; commit trailer `Co-Authored-By: Paperclip <noreply@paperclip.ing>`. Key existing files: composition schema `app/schemas/hermes_composition.py:96` (`HermesCompositionResult`) + `:66` (`HermesContextPayload`); item schema `app/schemas/investment_reports.py:126` (`IngestReportItem`); item model `app/models/investment_reports.py:195` (`InvestmentReportItem`); item mapping `app/services/investment_reports/ingestion.py:106-140` (`_insert_item` → `insert_item`); composition ingest `app/services/investment_stages/hermes_ingest.py:401` (`_validate_symbol_report_refs`); context exporter `app/services/investment_stages/hermes_context.py`; symbol repo `app/services/investment_stages/symbol_report_repository.py` (`list_for_run`, `get_by_uuids`); dimension repo `app/services/investment_dimensions/dimension_report_repository.py` (ROB-306).

---

# PR1 — Context export (C1)

## Task 1: Dimension repo `list_for_run` + `get_by_uuids`

**Files:**
- Modify: `app/services/investment_dimensions/dimension_report_repository.py`
- Test: `tests/services/investment_dimensions/test_dimension_report_repository.py`

- [ ] **Step 1: Write the failing test**

```python
import datetime as dt
import uuid

import pytest

from app.models.investment_dimension_reports import InvestmentDimensionReport
from app.services.investment_dimensions.dimension_report_repository import (
    DimensionReportRepository,
)


async def _add(db_session, run_uuid, *, content_hash, dimension="market"):
    row = InvestmentDimensionReport(
        run_uuid=run_uuid, snapshot_bundle_uuid=uuid.uuid4(), dimension=dimension,
        market="us", symbol=None, artifact_version=1, report_text="x",
        stance="bullish", confidence=70, content_hash=content_hash,
        idempotency_key=f"{run_uuid}:{dimension}:us::{content_hash}",
    )
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.asyncio
async def test_list_for_run_and_get_by_uuids(db_session):
    repo = DimensionReportRepository(db_session)
    run = uuid.uuid4()
    r1 = await _add(db_session, run, content_hash="h1")
    listed = await repo.list_for_run(run)
    assert [r.dimension_report_uuid for r in listed] == [r1.dimension_report_uuid]
    got = await repo.get_by_uuids([r1.dimension_report_uuid, uuid.uuid4()])
    assert [r.dimension_report_uuid for r in got] == [r1.dimension_report_uuid]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/services/investment_dimensions/test_dimension_report_repository.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'list_for_run'`.

- [ ] **Step 3: Implement** — add to `DimensionReportRepository`:

```python
    async def list_for_run(
        self, run_uuid: uuid.UUID
    ) -> list[InvestmentDimensionReport]:
        result = await self._session.execute(
            select(InvestmentDimensionReport)
            .where(InvestmentDimensionReport.run_uuid == run_uuid)
            .order_by(
                InvestmentDimensionReport.dimension,
                InvestmentDimensionReport.artifact_version.desc(),
            )
        )
        return list(result.scalars().all())

    async def get_by_uuids(
        self, dimension_report_uuids: list[uuid.UUID]
    ) -> list[InvestmentDimensionReport]:
        if not dimension_report_uuids:
            return []
        result = await self._session.execute(
            select(InvestmentDimensionReport).where(
                InvestmentDimensionReport.dimension_report_uuid.in_(
                    dimension_report_uuids
                )
            )
        )
        return list(result.scalars().all())
```

- [ ] **Step 4: Run to verify it passes** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_dimensions/dimension_report_repository.py tests/services/investment_dimensions/test_dimension_report_repository.py
git commit -m "feat(rob-308): dimension report list_for_run + get_by_uuids

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: Context export carries dimension + symbol reports

**Files:**
- Modify: `app/schemas/hermes_composition.py` (add 2 summary models + 2 fields on `HermesContextPayload`)
- Modify: `app/services/investment_stages/hermes_context.py`
- Test: `tests/services/investment_stages/test_hermes_context_reports.py`

The exporter must know the `run_uuid`. Verify how it resolves the run (the ROB-306 market-evidence block already runs in the exporter; the run is reachable via the bundle/stage run). If the exporter has `run_uuid`/`run`, use it; otherwise resolve via `InvestmentStagesRepository.get_run_by_bundle(bundle.bundle_uuid)` — confirm the method name in `app/services/investment_stages/repository.py` during Step 3.

- [ ] **Step 1: Write the failing test** — seed a stage run + one dimension report + one symbol report for it, build the context, assert both summary lists are populated:

```python
# (mirror tests/services/investment_stages/test_hermes_context_market_dimension.py setup)
# After building the HermesContextPayload for the seeded run:
assert any(d["dimension"] == "market" for d in payload.dimension_reports)
assert payload.dimension_reports[0]["stance"] == "bullish"
assert any(s["symbol"] == "005930" for s in payload.symbol_intermediate_reports)
assert payload.symbol_intermediate_reports[0]["decision_bucket"]
```

- [ ] **Step 2: Run to verify it fails** — Expected: `AttributeError`/validation — `dimension_reports` not a field.

- [ ] **Step 3: Implement** — add to `app/schemas/hermes_composition.py` `HermesContextPayload` (after `dimension_evidence`):

```python
    dimension_reports: list[dict[str, Any]] = Field(default_factory=list)
    symbol_intermediate_reports: list[dict[str, Any]] = Field(default_factory=list)
```

In `hermes_context.py`, after resolving the run for the bundle, build the two lists (read-only) and pass them to `HermesContextPayload(...)`:

```python
        from app.services.investment_dimensions.dimension_report_repository import (
            DimensionReportRepository,
        )
        from app.services.investment_stages.symbol_report_repository import (
            SymbolIntermediateReportRepository,
        )

        dimension_reports: list[dict[str, Any]] = []
        symbol_intermediate_reports: list[dict[str, Any]] = []
        if run is not None:
            for d in await DimensionReportRepository(self._session).list_for_run(
                run.run_uuid
            ):
                dimension_reports.append({
                    "dimension_report_uuid": str(d.dimension_report_uuid),
                    "dimension": d.dimension,
                    "market": d.market,
                    "symbol": d.symbol,
                    "stance": d.stance,
                    "confidence": d.confidence,
                    "key_findings": d.key_findings or [],
                    "report_text": d.report_text,
                })
            for s in await SymbolIntermediateReportRepository(
                self._session
            ).list_for_run(run.run_uuid):
                symbol_intermediate_reports.append({
                    "symbol_report_uuid": str(s.symbol_report_uuid),
                    "symbol": s.symbol,
                    "decision_bucket": s.decision_bucket,
                    "verdict": s.verdict,
                    "confidence": s.confidence,
                    "summary": s.summary,
                })
```

Then add `dimension_reports=dimension_reports, symbol_intermediate_reports=symbol_intermediate_reports,` to the `HermesContextPayload(...)` constructor call. Keep it best-effort: if `run` is None, both stay empty (legacy parity).

- [ ] **Step 4: Run to verify it passes** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/hermes_composition.py app/services/investment_stages/hermes_context.py tests/services/investment_stages/test_hermes_context_reports.py
git commit -m "feat(rob-308): Hermes context carries dimension + symbol reports (C1)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

- [ ] **Step 6: PR1 verify + open** — `uv run pytest tests/services/investment_stages/ tests/services/investment_dimensions/ -q` → pass; `make lint` → clean; ROB-287 guard pass. Open PR1 `feat(rob-308): feed dimension + symbol reports into Hermes context (PR1)`.

---

# PR2 — Composition contract + item model + ingest + read (C2/C3/C4/C5)

## Task 3: `IngestReportItem` schema — classification + citations

**Files:**
- Modify: `app/schemas/investment_reports.py`
- Test: `tests/test_investment_reports_schemas.py` (append; confirm file via `grep -rl "IngestReportItem" tests/`)

- [ ] **Step 1: Write the failing test**

```python
import pytest
from pydantic import ValidationError

from app.schemas.investment_reports import IngestReportItem


def _base(**kw):
    d = dict(client_item_key="k1", item_kind="action", intent="buy_review",
             rationale="r", operation="review", apply_policy="requires_user_approval")
    d.update(kw)
    return d


def test_item_accepts_decision_bucket_and_citations():
    import uuid
    su = uuid.uuid4()
    du = uuid.uuid4()
    item = IngestReportItem(**_base(symbol="AAA", side="buy",
        decision_bucket="new_buy_candidate",
        cited_symbol_report_uuid=su, cited_dimension_report_uuids=[du]))
    assert item.decision_bucket == "new_buy_candidate"
    assert item.cited_symbol_report_uuid == su
    assert item.cited_dimension_report_uuids == [du]


def test_item_rejects_unknown_decision_bucket():
    with pytest.raises(ValidationError):
        IngestReportItem(**_base(decision_bucket="macro_call"))


def test_item_decision_bucket_optional():
    item = IngestReportItem(**_base())
    assert item.decision_bucket is None
    assert item.cited_dimension_report_uuids == []
```

- [ ] **Step 2: Run to verify it fails** — Expected: `ValidationError` (extra field not permitted) / field missing.

- [ ] **Step 3: Implement** — in `app/schemas/investment_reports.py`, add the import + fields to `IngestReportItem` (after `apply_policy`, ~line 158):

```python
from app.models.investment_symbol_intermediate_reports import DECISION_BUCKETS
```

```python
    # ROB-308 — final-item classification (held action vs new candidate) +
    # per-item source citations. All optional; legacy items omit them.
    decision_bucket: str | None = None
    cited_symbol_report_uuid: uuid.UUID | None = None
    cited_dimension_report_uuids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("decision_bucket")
    @classmethod
    def _decision_bucket_in_vocab(cls, v: str | None) -> str | None:
        if v is not None and v not in DECISION_BUCKETS:
            raise ValueError(f"decision_bucket={v!r} not in {DECISION_BUCKETS!r}")
        return v
```

Ensure `uuid` and `field_validator` are imported at the top of the file (add if missing).

- [ ] **Step 4: Run to verify it passes** — Expected: PASS (3 cases).

- [ ] **Step 5: Commit**

```bash
git add app/schemas/investment_reports.py tests/test_investment_reports_schemas.py
git commit -m "feat(rob-308): IngestReportItem decision_bucket + report citations

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 4: `InvestmentReportItem` model columns + migration

**Files:**
- Modify: `app/models/investment_reports.py` (`InvestmentReportItem`, after `apply_policy` ~line 341)
- Create: `alembic/versions/<rev>_rob308_report_item_classification.py`
- Test: `tests/test_investment_reports_model.py` (append; confirm via grep)

- [ ] **Step 1: Write the failing test**

```python
def test_report_item_has_classification_columns():
    from app.models.investment_reports import InvestmentReportItem
    cols = InvestmentReportItem.__table__.c
    assert "decision_bucket" in cols and cols["decision_bucket"].nullable is True
    assert "cited_symbol_report_uuid" in cols
    assert "cited_dimension_report_uuids" in cols
```

- [ ] **Step 2: Run to verify it fails** — Expected: KeyError / assertion fail.

- [ ] **Step 3: Implement** — add columns to `InvestmentReportItem` (import `ARRAY` + `PG_UUID` + `CheckConstraint` + `DECISION_BUCKETS` if not present):

```python
    decision_bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
    cited_symbol_report_uuid: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    cited_dimension_report_uuids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, server_default=text("ARRAY[]::uuid[]")
    )
```

Add a CHECK to `InvestmentReportItem.__table_args__` (build the IN-list from `DECISION_BUCKETS`, mirroring the dimension model's `_sql_in`):

```python
        CheckConstraint(
            "decision_bucket IS NULL OR decision_bucket IN "
            "('new_buy_candidate','open_action','completed_or_existing',"
            "'deferred_no_action','risk_watch')",
            name="ck_investment_report_items_decision_bucket",
        ),
```

- [ ] **Step 4: Run model test** — `uv run pytest tests/test_investment_reports_model.py -k classification -v` → PASS.

- [ ] **Step 5: Generate + verify migration**

Run: `uv run alembic revision --autogenerate -m "rob308 report item classification"`
Open the file; confirm it ADDs the 3 columns + the CHECK to `review.investment_report_items` and downgrade drops them. Strip unrelated drift.
Run: `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` → clean round-trip.

- [ ] **Step 6: Commit**

```bash
git add app/models/investment_reports.py alembic/versions/*rob308* tests/test_investment_reports_model.py
git commit -m "feat(rob-308): report item classification columns + migration

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 5: `HermesCompositionResult.dimension_report_uuids`

**Files:**
- Modify: `app/schemas/hermes_composition.py` (`HermesCompositionResult`, after `symbol_intermediate_report_uuids` ~line 125)
- Test: `tests/test_hermes_composition_schema.py` (append; confirm via grep `grep -rl HermesCompositionResult tests/`)

- [ ] **Step 1: Write the failing test**

```python
def test_composition_accepts_dimension_report_uuids():
    import uuid
    from app.schemas.hermes_composition import HermesCompositionResult
    c = HermesCompositionResult(
        snapshot_bundle_uuid=uuid.uuid4(), hermes_run_id="h1",
        title="t", summary="s", dimension_report_uuids=[uuid.uuid4()],
    )
    assert len(c.dimension_report_uuids) == 1
```

- [ ] **Step 2: Run to verify it fails** — Expected: extra-field ValidationError.

- [ ] **Step 3: Implement** — add to `HermesCompositionResult`:

```python
    # ROB-308: dimension reports (ROB-306) this composition consumed. Empty for
    # legacy composition. Validated for existence + run membership on ingest.
    dimension_report_uuids: list[uuid.UUID] = Field(default_factory=list)
```

- [ ] **Step 4: Run to verify it passes** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/hermes_composition.py tests/test_hermes_composition_schema.py
git commit -m "feat(rob-308): composition dimension_report_uuids field

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 6: Composition ingest — validate dimension refs + persist item fields

**Files:**
- Modify: `app/services/investment_stages/hermes_ingest.py` (mirror `_validate_symbol_report_refs` at line 401; inject `DimensionReportRepository`)
- Modify: `app/services/investment_reports/ingestion.py` (`_insert_item` ~line 106-140 — map the 3 new IngestReportItem fields into `insert_item`)
- Test: `tests/test_hermes_ingest_dimension_refs.py` + extend an item-persistence test

- [ ] **Step 1: Write the failing tests**

```python
# A) ingest validates dimension_report_uuids run membership
#    (mirror the existing symbol-ref validation test: seed run + dimension report,
#     happy path stores them in report_metadata["dimension_report_uuids"];
#     unknown UUID -> ingest error).
# B) item persistence carries the new fields:
#    ingest a composition with one item {decision_bucket:"new_buy_candidate",
#    cited_symbol_report_uuid: <uuid>, cited_dimension_report_uuids:[<uuid>]},
#    then read the persisted InvestmentReportItem and assert the 3 fields.
```
Write both concretely by copying the existing symbol-ref ingest test harness (`grep -rl "_validate_symbol_report_refs\|symbol_intermediate_report_uuids" tests/`) and the item-persistence test, swapping in the dimension fields.

- [ ] **Step 2: Run to verify they fail** — Expected: dimension refs not validated/stored; item fields not persisted.

- [ ] **Step 3a: Implement dimension-ref validation** — in `hermes_ingest.py`, add a `DimensionReportRepository` to `__init__` (mirror `self._symbol_reports`), add `_validate_dimension_report_refs` mirroring `_validate_symbol_report_refs` (uses `get_by_uuids`, checks run membership via `run_uuid`), and in the ingest flow (after the symbol-ref block ~line 365-370):

```python
        dimension_report_refs = await self._validate_dimension_report_refs(
            composition.dimension_report_uuids, run_uuid=stage_run_uuid
        )
        if dimension_report_refs:
            metadata["dimension_report_uuids"] = dimension_report_refs
```

`_validate_dimension_report_refs` (mirror the symbol version, lines 401-431):

```python
    async def _validate_dimension_report_refs(
        self, dimension_report_uuids: list[uuid.UUID], *, run_uuid: uuid.UUID | None
    ) -> list[str]:
        if not dimension_report_uuids:
            return []
        found = await self._dimension_reports.get_by_uuids(list(dimension_report_uuids))
        found_by_uuid = {r.dimension_report_uuid: r for r in found}
        missing = [str(u) for u in dimension_report_uuids if u not in found_by_uuid]
        if missing:
            raise HermesIngestError(  # use the same error type the symbol path raises
                f"dimension reports not found: {missing}",
                code="dimension_report_not_found",
            )
        if run_uuid is not None:
            wrong = [
                str(u) for u, r in found_by_uuid.items() if r.run_uuid != run_uuid
            ]
            if wrong:
                raise HermesIngestError(
                    f"dimension reports not in run {run_uuid}: {wrong}",
                    code="dimension_report_run_mismatch",
                )
        return [str(u) for u in dimension_report_uuids]
```
(Match the exact error class + constructor signature used by `_validate_symbol_report_refs` — read lines 401-431 first.)

- [ ] **Step 3b: Implement item field mapping** — in `app/services/investment_reports/ingestion.py` `_insert_item` (~line 134), add to the `insert_item(...)` kwargs:

```python
            decision_bucket=item_req.decision_bucket,
            cited_symbol_report_uuid=item_req.cited_symbol_report_uuid,
            cited_dimension_report_uuids=list(item_req.cited_dimension_report_uuids),
```

- [ ] **Step 4: Run to verify tests pass** — Expected: PASS (both A + B).

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/hermes_ingest.py app/services/investment_reports/ingestion.py tests/test_hermes_ingest_dimension_refs.py
git commit -m "feat(rob-308): ingest validates dimension refs + persists item classification

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 7: Read view-model — group held-action vs new-candidate

**Files:**
- Modify: the final-report bundle view-model behind `GET /trading/api/investment-reports/{report_uuid}` (`app/routers/investment_reports.py:169` → `service.get_bundle`). Locate the bundle assembler (`grep -rn "def get_bundle" app/services/`) and the item serialization.
- Test: extend the existing report-bundle router/service test (`grep -rl "get_bundle\|investment-reports/{report_uuid}\|report_bundle" tests/`)

- [ ] **Step 1: Write the failing test** — ingest a report with two items (`decision_bucket="new_buy_candidate"` and `="open_action"`), GET the bundle, assert the response exposes a grouping, e.g. `bundle["item_groups"]["new_buy_candidate"]` and `["open_action"]` each containing the right symbol, plus each item echoes `decision_bucket` + `cited_symbol_report_uuid` + `cited_dimension_report_uuids`.

- [ ] **Step 2: Run to verify it fails** — Expected: KeyError (no grouping / fields absent).

- [ ] **Step 3: Implement** — in the bundle assembler: (a) include `decision_bucket`, `cited_symbol_report_uuid`, `cited_dimension_report_uuids` in each serialized item; (b) add an `item_groups: dict[str, list]` keyed by `decision_bucket` (items with null bucket go under `"unclassified"`). Korean labels for the UI live in the frontend; the API returns the bucket keys + a `held_action`/`new_candidate` rollup:

```python
        _HELD_BUCKETS = {"open_action", "risk_watch", "completed_or_existing"}
        item_groups: dict[str, list] = {}
        for it in serialized_items:
            item_groups.setdefault(it["decision_bucket"] or "unclassified", []).append(it)
        rollup = {
            "new_candidate": [i for i in serialized_items
                              if i["decision_bucket"] == "new_buy_candidate"],
            "held_action": [i for i in serialized_items
                            if i["decision_bucket"] in _HELD_BUCKETS],
        }
```
Attach `item_groups` + `decision_rollup=rollup` to the bundle response.

- [ ] **Step 4: Run to verify it passes** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_reports/ app/routers/investment_reports.py tests/
git commit -m "feat(rob-308): final-report bundle groups held-action vs new-candidate (C5)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 8: PR2 verification

- [ ] **Step 1:** `uv run pytest -k "investment_report or hermes or dimension" -v` → all pass.
- [ ] **Step 2:** ROB-287 import guard pass; `make lint` clean.
- [ ] **Step 3:** Legacy-parity check — `uv run pytest -k "composition" -q` confirms a composition WITHOUT the new fields still ingests (empty lists / null bucket).
- [ ] **Step 4:** broad regression `uv run pytest tests/ -k "investment or hermes or screener" -q` → green.
- [ ] **Step 5:** Open PR2. Handoff comment (ROB-308 AC): branches, PR URLs, tests, migration (operator-applied), config flag, advisory-only confirmation, what's operator-gated (real Hermes synthesis round-trip).

---

## Self-Review (against spec)

**Spec coverage:**
- C1 context export (dimension + symbol summaries) → Tasks 1–2. ✓
- C2 composition contract (`dimension_report_uuids` + item fields) → Tasks 3 (item schema) + 5 (composition). ✓
- C3 item model + migration → Task 4. ✓
- C4 ingest validation + persistence → Task 6 (dimension-ref validation + item field mapping). ✓
- C5 read grouping → Task 7. ✓
- Boundaries: no in-process LLM (Task 8 guard); advisory-only (composition validator unchanged, Task 6 doesn't touch it); additive contract (all new fields optional, Task 8 legacy-parity check); migration operator-applied (Task 4). ✓

**Placeholder scan:** Tasks 2/6/7 carry explicit "confirm via grep / read lines X first" verification notes against named files + line numbers (run resolution, error class, bundle assembler) — these are verification instructions, not deferred work. New schema/model/repo code is complete.

**Type consistency:** `dimension_report_uuids` (Task 5) ↔ validated in Task 6. `decision_bucket`/`cited_symbol_report_uuid`/`cited_dimension_report_uuids` consistent across schema (T3), model (T4), ingest mapping (T6), read (T7). `DECISION_BUCKETS` imported from the ROB-301 model in both schema (T3) and model (T4). `list_for_run`/`get_by_uuids` defined in T1, used in T2/T6.
```
