# ROB-352 Slice B — snapshot evidence & prior-report hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make snapshot-backed reports auditable — add a per-item `cited_snapshot_uuids` citation column populated from evidence, populate report-level `market_snapshot`/`portfolio_snapshot` with compact bundle provenance descriptors, and exclude `status='draft'` smoke boilerplate from prior-report context.

**Architecture:** Additive alembic column + ORM/schema/repository/response plumbing for `cited_snapshot_uuids` (mirrors ROB-308 `cited_dimension_report_uuids`). The generator derives citations deterministically from each item's `evidence_snapshot` and builds market/portfolio provenance descriptors from the frozen bundle's `market`/`portfolio` snapshots (pointer + freshness, never a full payload copy). The query service drops drafts before slicing `n_prior`.

**Tech Stack:** Python 3.13, SQLAlchemy async + alembic, Pydantic v2, pytest against the real-Postgres `session` fixture. `uv run pytest ...`. Spec: `docs/superpowers/specs/2026-05-29-rob-352-slice-b-snapshot-evidence-design.md`.

**Conventions:** end commit messages with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Current alembic head = `20260527_rob329`.

---

## File Structure

| File | Change |
|------|--------|
| `alembic/versions/<rev>_rob352_cited_snapshot_uuids.py` | NEW additive migration |
| `app/models/investment_reports.py` | `InvestmentReportItem.cited_snapshot_uuids` ARRAY column |
| `app/schemas/investment_reports.py` | `IngestReportItem` + `InvestmentReportItemResponse` field |
| `app/services/investment_reports/repository.py` | `insert_item` passthrough |
| `app/services/investment_reports/ingestion.py` | pass `cited_snapshot_uuids` to repo |
| `app/services/action_report/snapshot_backed/generator.py` | derive citations + section descriptors |
| `app/services/investment_reports/query_service.py` | drop `status='draft'` from prior_reports |
| `tests/_investment_reports_helpers.py` | idempotent ALTER for the new column |

---

## Task B1: Migration + ORM column

**Files:**
- Create: `alembic/versions/rob352_cited_snapshot_uuids.py`
- Modify: `app/models/investment_reports.py` (InvestmentReportItem, after `cited_dimension_report_uuids`)

- [ ] **Step 1: Write the migration**

Create `alembic/versions/rob352_cited_snapshot_uuids.py`:

```python
"""rob352 per-item cited_snapshot_uuids

Revision ID: 20260529_rob352
Revises: 20260527_rob329
Create Date: 2026-05-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260529_rob352"
down_revision: str | Sequence[str] | None = "20260527_rob329"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "investment_report_items",
        sa.Column(
            "cited_snapshot_uuids",
            sa.ARRAY(sa.UUID()),
            server_default=sa.text("ARRAY[]::uuid[]"),
            nullable=False,
        ),
        schema="review",
    )


def downgrade() -> None:
    op.drop_column(
        "investment_report_items", "cited_snapshot_uuids", schema="review"
    )
```

- [ ] **Step 2: Verify the migration applies + reverts**

Run:
```bash
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
```
Expected: no errors; head ends at `20260529_rob352`.

- [ ] **Step 3: Add the ORM column**

In `app/models/investment_reports.py`, `InvestmentReportItem`, immediately after the `cited_dimension_report_uuids` mapped_column (the ROB-308 block):

```python
    # ROB-352 Slice B — per-item snapshot provenance citations. Mirrors
    # cited_dimension_report_uuids; derived from the item's evidence_snapshot
    # by the generator unless the caller supplies them explicitly.
    cited_snapshot_uuids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
        server_default=text("ARRAY[]::uuid[]"),
    )
```

(`ARRAY`, `PG_UUID`, `text`, `uuid` are already imported — used by `cited_dimension_report_uuids`.)

- [ ] **Step 4: Verify import + model load**

Run: `uv run python -c "from app.models.investment_reports import InvestmentReportItem; print('cited_snapshot_uuids' in InvestmentReportItem.__table__.c)"`
Expected: `True`

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/rob352_cited_snapshot_uuids.py app/models/investment_reports.py
git commit -m "feat(ROB-352): cited_snapshot_uuids column + migration (Slice B)"
```

---

## Task B2: Schema + repository + response + test-fixture column

**Files:**
- Modify: `app/schemas/investment_reports.py` (`IngestReportItem` ~line 164; `InvestmentReportItemResponse` ~line 406)
- Modify: `app/services/investment_reports/ingestion.py` (`_insert_item`, the `insert_item(...)` call)
- Modify: `tests/_investment_reports_helpers.py` (idempotent ALTER block ~line 147)
- Test: `tests/test_investment_reports_ingestion.py`

- [ ] **Step 1: Write the failing round-trip test**

Append to `tests/test_investment_reports_ingestion.py`:

```python
@pytest.mark.asyncio
async def test_cited_snapshot_uuids_round_trip(session: AsyncSession) -> None:
    """ROB-352 Slice B — cited_snapshot_uuids persists and reads back."""
    import uuid as _uuid

    u1, u2 = _uuid.uuid4(), _uuid.uuid4()
    service = InvestmentReportIngestionService(session)
    report = await service.ingest(
        _base_request(
            items=[_action_item("a1", cited_snapshot_uuids=[u1, u2])]
        )
    )
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    assert items[0].cited_snapshot_uuids == [u1, u2]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_investment_reports_ingestion.py -k cited_snapshot_uuids -v`
Expected: FAIL — `IngestReportItem` has no field `cited_snapshot_uuids` (TypeError/ValidationError) or the column is missing.

- [ ] **Step 3: Add the schema fields**

In `app/schemas/investment_reports.py`, `IngestReportItem`, after `cited_dimension_report_uuids` (line ~164):

```python
    cited_snapshot_uuids: list[UUID] = Field(default_factory=list)
```

In `InvestmentReportItemResponse`, after `cited_dimension_report_uuids` (line ~406):

```python
    cited_snapshot_uuids: list[UUID] = Field(default_factory=list)
```

- [ ] **Step 4: Forward through the repository**

In `app/services/investment_reports/ingestion.py`, `_insert_item`, in the `await self._repo.insert_item(...)` call, after the `cited_dimension_report_uuids=...` line:

```python
            cited_snapshot_uuids=list(item_req.cited_snapshot_uuids),
```

- [ ] **Step 5: Add the idempotent ALTER to the test fixture**

In `tests/_investment_reports_helpers.py`, in the `investment_report_items` ALTER list, after the `cited_dimension_report_uuids` line (line ~147):

```python
                        "ALTER TABLE review.investment_report_items "
                        "ADD COLUMN IF NOT EXISTS cited_snapshot_uuids "
                        "UUID[] NOT NULL DEFAULT ARRAY[]::uuid[]",
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_investment_reports_ingestion.py -k cited_snapshot_uuids -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/schemas/investment_reports.py app/services/investment_reports/ingestion.py tests/_investment_reports_helpers.py tests/test_investment_reports_ingestion.py
git commit -m "feat(ROB-352): cited_snapshot_uuids schema/response/repository round-trip (Slice B)"
```

---

## Task B3: Generator derives citations from evidence_snapshot

**Files:**
- Modify: `app/services/action_report/snapshot_backed/generator.py` (`_build_ingest_request` normalized-items loop; new module helper)
- Test: `tests/services/action_report/snapshot_backed/test_generator.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/action_report/snapshot_backed/test_generator.py`:

```python
@pytest.mark.asyncio
async def test_cited_snapshot_uuids_derived_from_evidence() -> None:
    """ROB-352 Slice B — citations derived from evidence_snapshot UUIDs."""
    snap_a = str(uuid.uuid4())
    snap_b = str(uuid.uuid4())
    item = IngestReportItem(
        client_item_key="k1",
        item_kind="risk",
        intent="risk_review",
        rationale="r",
        evidence_snapshot={
            "snapshot_uuid": snap_a,
            "candidate_snapshot_uuid": snap_b,
            "snapshot_kind": "symbol",
            "not_a_uuid": "hello",
        },
    )
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    await gen.generate(_make_request(items=[item]))

    sent_items = ingest.calls[0].items
    cited = sent_items[0].cited_snapshot_uuids
    assert {str(u) for u in cited} == {snap_a, snap_b}


@pytest.mark.asyncio
async def test_cited_snapshot_uuids_caller_supplied_wins() -> None:
    """ROB-352 Slice B — explicit caller citations are not overwritten."""
    explicit = uuid.uuid4()
    item = IngestReportItem(
        client_item_key="k1",
        item_kind="risk",
        intent="risk_review",
        rationale="r",
        evidence_snapshot={"snapshot_uuid": str(uuid.uuid4())},
        cited_snapshot_uuids=[explicit],
    )
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    await gen.generate(_make_request(items=[item]))
    assert ingest.calls[0].items[0].cited_snapshot_uuids == [explicit]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_generator.py -k cited_snapshot_uuids -v`
Expected: FAIL — derived list empty (feature absent).

- [ ] **Step 3: Add the derivation helper**

In `app/services/action_report/snapshot_backed/generator.py`, add a module-level helper (near the other module helpers, after `_optional_kind_names`):

```python
def _extract_cited_snapshot_uuids(evidence_snapshot: Any) -> list[UUID]:
    """ROB-352 Slice B — collect snapshot UUIDs cited by an item's evidence.

    Picks the ``snapshot_uuid`` key plus any ``*_snapshot_uuid`` extra
    (candidate/portfolio/news). Skips non-UUID values; dedupes preserving
    first-seen order.
    """
    if not isinstance(evidence_snapshot, Mapping):
        return []
    out: list[UUID] = []
    seen: set[UUID] = set()
    for key, value in evidence_snapshot.items():
        if key != "snapshot_uuid" and not str(key).endswith("_snapshot_uuid"):
            continue
        if not isinstance(value, (str, UUID)):
            continue
        try:
            parsed = value if isinstance(value, UUID) else UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            continue
        if parsed not in seen:
            seen.add(parsed)
            out.append(parsed)
    return out
```

(`UUID`, `Any`, `Mapping` are already imported in this module.)

- [ ] **Step 4: Wire it into `_build_ingest_request`**

In `_build_ingest_request`, inside the `for item in request.items:` loop, after the `to_jsonable` key-normalisation block and before `normalized_items.append(item_dict)`:

```python
            # ROB-352 Slice B — derive snapshot citations from evidence unless
            # the caller supplied them explicitly.
            if not item_dict.get("cited_snapshot_uuids"):
                item_dict["cited_snapshot_uuids"] = _extract_cited_snapshot_uuids(
                    item_dict.get("evidence_snapshot") or {}
                )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_generator.py -k cited_snapshot_uuids -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add app/services/action_report/snapshot_backed/generator.py tests/services/action_report/snapshot_backed/test_generator.py
git commit -m "feat(ROB-352): derive per-item cited_snapshot_uuids from evidence (Slice B)"
```

---

## Task B4: Generator populates market/portfolio provenance descriptors

**Files:**
- Modify: `app/services/action_report/snapshot_backed/generator.py` (`generate`, `_build_ingest_request`, new helper)
- Test: `tests/services/action_report/snapshot_backed/test_generator.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/action_report/snapshot_backed/test_generator.py`:

```python
class _FakeSnap:
    def __init__(self, *, kind, symbol=None):
        self.snapshot_uuid = uuid.uuid4()
        self.snapshot_kind = kind
        self.symbol = symbol
        self.as_of = dt.datetime(2026, 5, 29, 12, 0, tzinfo=dt.timezone.utc)
        self.freshness_status = "fresh"
        self.coverage_json = {"rows": 3}
        self.payload_json = {"x": 1}


@pytest.mark.asyncio
async def test_market_portfolio_descriptors_from_bundle() -> None:
    """ROB-352 Slice B — present kinds → provenance descriptor (pointer)."""
    market = _FakeSnap(kind="market")
    portfolio = _FakeSnap(kind="portfolio")
    repo = _FakeSnapshotsRepository(
        bundle=type("B", (), {"id": 7})(),
        items=[(object(), market), (object(), portfolio)],
    )
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=repo,
    )
    await gen.generate(_make_request())
    sent = ingest.calls[0]
    assert sent.market_snapshot["snapshot_uuid"] == str(market.snapshot_uuid)
    assert sent.market_snapshot["freshness_status"] == "fresh"
    assert sent.portfolio_snapshot["snapshot_kind"] == "portfolio"
    # Provenance pointer only — never the full payload.
    assert "payload_json" not in sent.market_snapshot


@pytest.mark.asyncio
async def test_missing_portfolio_descriptor_is_unavailable() -> None:
    """ROB-352 Slice B — absent kind → explicit unavailable reason."""
    market = _FakeSnap(kind="market")
    repo = _FakeSnapshotsRepository(
        bundle=type("B", (), {"id": 7})(),
        items=[(object(), market)],
    )
    ensure = _FakeEnsureService(
        _ensure_response(missing_sources=["portfolio"])
    )
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=repo,
    )
    await gen.generate(_make_request())
    sent = ingest.calls[0]
    assert sent.portfolio_snapshot["status"] == "unavailable"
    assert "reason" in sent.portfolio_snapshot
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_generator.py -k "descriptor" -v`
Expected: FAIL — `market_snapshot` is `{}` (hardcoded).

- [ ] **Step 3: Add the descriptor helper**

In `generator.py`, add a method on `SnapshotBackedReportGenerator` (near `_build_classifier_context`):

```python
    async def _section_snapshot_descriptors(
        self,
        *,
        bundle_uuid: UUID,
        unavailable_sources: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """ROB-352 Slice B — compact provenance descriptors for the report's
        market / portfolio sections.

        Pointer + freshness into ``investment_snapshots`` (never a full
        payload copy). Absent kinds get an explicit unavailable reason.
        """

        def _unavailable(kind: str) -> dict[str, Any]:
            info = unavailable_sources.get(kind)
            reason = "not_collected"
            if isinstance(info, Mapping):
                reason = str(info.get("reason") or info.get("status") or reason)
            return {"status": "unavailable", "reason": reason}

        def _descriptor(snapshot: Any) -> dict[str, Any]:
            as_of = getattr(snapshot, "as_of", None)
            return {
                "snapshot_uuid": str(snapshot.snapshot_uuid),
                "snapshot_kind": snapshot.snapshot_kind,
                "as_of": as_of.isoformat() if as_of is not None else None,
                "freshness_status": getattr(snapshot, "freshness_status", None),
                "coverage": getattr(snapshot, "coverage_json", None) or {},
            }

        market = _unavailable("market")
        portfolio = _unavailable("portfolio")
        bundle = await self._snapshots_repo.get_bundle_by_uuid(bundle_uuid)
        if bundle is None:
            return market, portfolio
        pairs = await self._snapshots_repo.list_bundle_items_with_snapshots(bundle.id)
        for _item, snapshot in pairs:
            if snapshot.snapshot_kind == "market":
                market = _descriptor(snapshot)
            elif snapshot.snapshot_kind == "portfolio":
                portfolio = _descriptor(snapshot)
        return market, portfolio
```

- [ ] **Step 4: Compute descriptors in `generate` and thread them through**

In `generate`, after `unavailable_sources = self._build_unavailable_sources(...)` and before `ingest_request = self._build_ingest_request(...)`:

```python
        market_snapshot, portfolio_snapshot = await self._section_snapshot_descriptors(
            bundle_uuid=ensure_response.bundle_uuid,
            unavailable_sources=unavailable_sources,
        )
```

Change the `_build_ingest_request(...)` call to pass them:

```python
        ingest_request = self._build_ingest_request(
            request=request,
            bundle_uuid=ensure_response.bundle_uuid,
            coverage_summary=coverage_summary,
            freshness_summary=freshness_summary,
            unavailable_sources=unavailable_sources,
            source_conflicts=source_conflicts,
            report_diagnostics=report_diagnostics,
            symbol_derivation=derivation,
            market_snapshot=market_snapshot,
            portfolio_snapshot=portfolio_snapshot,
        )
```

In `_build_ingest_request`, add the two parameters to the signature (after `symbol_derivation`):

```python
        symbol_derivation: SymbolDerivation | None = None,
        market_snapshot: dict[str, Any] | None = None,
        portfolio_snapshot: dict[str, Any] | None = None,
    ) -> IngestReportRequest:
```

and replace the hardcoded lines:

```python
            market_snapshot=market_snapshot or {},
            portfolio_snapshot=portfolio_snapshot or {},
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_generator.py -k "descriptor" -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Run the full generator suite (no regression)**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_generator.py -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/action_report/snapshot_backed/generator.py tests/services/action_report/snapshot_backed/test_generator.py
git commit -m "feat(ROB-352): populate market/portfolio provenance descriptors from bundle (Slice B)"
```

---

## Task B5: prior_reports excludes drafts

**Files:**
- Modify: `app/services/investment_reports/query_service.py` (`previous_report_context`)
- Test: `tests/test_investment_reports_query_prior_drafts.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_investment_reports_query_prior_drafts.py`:

```python
"""ROB-352 Slice B — prior_reports excludes draft (smoke) reports."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
)
from app.services.investment_reports.repository import InvestmentReportsRepository


async def _make_report(repo, *, key, status, title):
    return await repo.insert_report(
        idempotency_key=key,
        report_type="snapshot_backed_advisory_v1",
        market="us",
        market_session=None,
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile="t",
        title=title,
        summary="s",
        status=status,
        report_metadata={},
    )


@pytest.mark.asyncio
async def test_prior_reports_excludes_drafts(session: AsyncSession) -> None:
    repo = InvestmentReportsRepository(session)
    await _make_report(repo, key="pub:1", status="published", title="real-1")
    await _make_report(repo, key="draft:1", status="draft", title="hermes-smoke-1")
    await _make_report(repo, key="draft:2", status="draft", title="hermes-smoke-2")
    await _make_report(repo, key="pub:2", status="published", title="real-2")

    svc = InvestmentReportQueryService(session)
    ctx = await svc.previous_report_context(
        market="us", account_scope="kis_live",
        report_type="snapshot_backed_advisory_v1", n_prior=3,
    )
    titles = {r.title for r in ctx["prior_reports"]}
    assert titles == {"real-1", "real-2"}
    assert all(r.status != "draft" for r in ctx["prior_reports"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_investment_reports_query_prior_drafts.py -v`
Expected: FAIL — draft titles present in `prior_reports`.

- [ ] **Step 3: Exclude drafts in `previous_report_context`**

In `app/services/investment_reports/query_service.py`, replace the prior-reports fetch/slice block (lines ~258-269):

```python
        # ROB-352 Slice B — fetch a buffer so dropping drafts (smoke
        # boilerplate ships as draft) + the excluded uuid still yields up to
        # n_prior published rows.
        _DRAFT_FETCH_BUFFER = 5
        prior_reports: list[InvestmentReport] = await self._repo.list_reports(
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            report_type=report_type,
            limit=n_prior + 1 + _DRAFT_FETCH_BUFFER,
        )
        if exclude_report_uuid is not None:
            prior_reports = [
                r for r in prior_reports if r.report_uuid != exclude_report_uuid
            ]
        prior_reports = [r for r in prior_reports if r.status != "draft"]
        prior_reports = prior_reports[:n_prior]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_investment_reports_query_prior_drafts.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_reports/query_service.py tests/test_investment_reports_query_prior_drafts.py
git commit -m "feat(ROB-352): exclude draft (smoke) reports from prior_reports context (Slice B)"
```

---

## Task B6: Full verification + lint + push

**Files:** none (verification only)

- [ ] **Step 1: Run the touched + broad suites**

Run:
```bash
uv run pytest \
  tests/test_investment_reports_ingestion.py \
  tests/test_investment_reports_query_prior_drafts.py \
  tests/services/action_report/snapshot_backed/test_generator.py \
  tests/services/action_report/ tests/ -k "investment_report or ingestion or snapshot_backed or hermes" -q
```
Expected: all PASS.

- [ ] **Step 2: Lint + format**

Run: `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/`
Expected: clean. (`uv run ruff format app/ tests/` if needed.)

- [ ] **Step 3: Import/host guards**

Run: `uv run pytest tests/ -k "guard or import_guard" -q`
Expected: PASS — no new in-process LLM import under the snapshot_backed/investment_stages trees.

- [ ] **Step 4: Typecheck (best-effort)**

Run: `uv run ty check app/services/action_report/snapshot_backed/ app/services/investment_reports/ app/models/investment_reports.py app/schemas/investment_reports.py`
Expected: no new errors.

- [ ] **Step 5: Push + open PR (base main)**

```bash
git push -u origin rob-352-slice-b
gh pr create --base main --title "feat(ROB-352): snapshot evidence + prior-report hygiene (Slice B)" --body "<summary + test plan + side-effect boundary; link spec + Slice A #994>"
```
Then confirm the CI Test workflow + lint are green before merge (branch protection does not gate them).

---

## Self-Review

**Spec coverage:**
- Change 1 (cited_snapshot_uuids column + derivation) → B1 (migration+ORM), B2 (schema/response/repo/fixture), B3 (derivation). ✓
- Change 2 (market/portfolio provenance descriptors, unavailable reason) → B4. ✓
- Change 3 (prior_reports draft exclusion) → B5. ✓
- Testing (migration round-trip, derivation, round-trip persist, descriptors, draft exclusion, fixture column) → B1S2, B2, B3, B4, B5. ✓

**Placeholder scan:** PR body in B6S5 is the only `<...>` — intentional, filled at push time from the spec. All code steps contain complete code.

**Type consistency:** `cited_snapshot_uuids: list[UUID]` consistent across ORM (ARRAY(PG_UUID)), `IngestReportItem`, `InvestmentReportItemResponse`, repo passthrough, and the `_extract_cited_snapshot_uuids -> list[UUID]` helper. `_section_snapshot_descriptors` returns `(market, portfolio)` dicts threaded as `market_snapshot`/`portfolio_snapshot` kwargs matching `_build_ingest_request`'s new params and `IngestReportRequest` JSONB dict fields. `_DRAFT_FETCH_BUFFER` local constant used once.

**Note on B4 DB reads:** `_section_snapshot_descriptors` issues its own `get_bundle_by_uuid` + `list_bundle_items_with_snapshots` (one extra read alongside the classifier-context read). Acceptable for clarity; a future refactor could share a single bundle-items read if it shows up in profiling.
