# ROB-275 `/invest` Report Snapshot Evidence Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `/invest/reports/:reportUuid` snapshot evidence viewer that lists the snapshots actually linked to the report's bundle (grouped by role) and lazy-loads each snapshot's stored payload on row click.

**Architecture:** The domain model is unchanged — `investment_snapshots` is a globally reusable evidence entity, and `investment_snapshot_bundle_items` is the truth source for bundle↔snapshot membership/role. The report↔snapshot relationship is *derived* via `report.snapshot_bundle_uuid → bundle_items.snapshot_uuid`. Two new GET endpoints under `/invest/api/` and `/trading/api/` are added; the bundle-list endpoint returns a 200 legacy/no-snapshot shape when the report has no bundle, while the snapshot-detail endpoint returns 404 on missing bundle or bundle-membership violation. All membership/legacy logic lives in the report query service (router stays a thin handler). Frontend mounts a new panel inside the existing bundle detail view; payload fetch is deferred to a drawer opened on row click.

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy async / Pydantic v2 on the backend. React + TypeScript + Vitest on the frontend.

**Out-of-scope guardrails (do not violate):**
- No new column/FK on `investment_snapshots`. No alembic migration.
- No global `/invest/api/investment-snapshots/{uuid}` endpoint (existing `/trading/api/investment-snapshots/...` MCP-flag-gated routes are NOT touched).
- No Toss/Naver mandatory comparison. No change to report generation logic. No scheduler/TaskIQ.
- No broker / order / watch / order-intent mutation.
- Initial report-detail render must not fetch full payloads (`fetchReportSnapshotDetail` only fires on row click).
- A snapshot_uuid belonging to a *different* report's bundle must 404 when requested via this report's URL.
- `unavailable_sources` and `source_conflicts` render as separate sections, not mixed into the bundle-linked snapshot list.

**Sample report UUIDs for local smoke:**
- Report: `b65efa46-5ed9-4ac4-a724-4d5e7060b92c`
- Bundle: `648701c1-9ef2-4c40-ac8d-e5d8c30db32c` (status `partial`, 11 linked items, ~16KB total payload)

---

## File Structure

**Backend — modified files:**
- `app/services/investment_snapshots/repository.py` — add `get_bundle_item_with_snapshot(bundle_uuid, snapshot_uuid)` for the membership-checked detail fetch.
- `app/schemas/investment_reports.py` — add 4 response classes for the report-centric viewer (bundle summary, bundle item view, bundle response, detail response).
- `app/services/investment_reports/query_service.py` — add `get_report_snapshot_bundle(report_uuid)` and `get_report_snapshot_detail(report_uuid, snapshot_uuid)`.
- `app/routers/investment_reports.py` — add two GET routes mirrored under `/trading/api/` and `/invest/api/`.

**Backend — new test files:**
- `tests/test_investment_reports_snapshot_evidence_service.py` — service-level tests (bundle/legacy/membership/detail).
- `tests/routers/test_investment_reports_snapshot_evidence_router.py` — router 200/404 behaviour.

**Frontend — modified files:**
- `frontend/invest/src/types/investmentReports.ts` — add evidence types.
- `frontend/invest/src/api/investmentReports.ts` — add `fetchReportSnapshotBundle` + `fetchReportSnapshotDetail` + normalisers.
- `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx` — mount the new panel.
- `frontend/invest/src/__tests__/investmentReports.api.test.ts` — add API normalisation tests (incl. lazy-load assertion that initial bundle fetch does not issue a detail fetch).

**Frontend — new files:**
- `frontend/invest/src/hooks/useReportSnapshotBundle.ts` — eager fetch of the bundle list on report view.
- `frontend/invest/src/hooks/useSnapshotPayload.ts` — lazy fetch of an individual snapshot payload triggered by row click.
- `frontend/invest/src/components/investment-reports/SnapshotPayloadDrawer.tsx` — drawer-ish viewer that renders metadata + JSON payload.
- `frontend/invest/src/components/investment-reports/SnapshotEvidenceRow.tsx` — one row in the evidence list.
- `frontend/invest/src/components/investment-reports/ReportSnapshotEvidencePanel.tsx` — bundle summary + role-grouped rows + separate `unavailable_sources` / `source_conflicts` sections.
- `frontend/invest/src/__tests__/ReportSnapshotEvidencePanel.test.tsx` — role grouping, separation of unavailable/conflicts, lazy-load assertion (drawer click triggers detail fetch; mount alone does not).

---

## Task 1: Repository helper — `get_bundle_item_with_snapshot`

**Files:**
- Modify: `app/services/investment_snapshots/repository.py`
- Test: `tests/services/investment_snapshots/test_repository_reads.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/investment_snapshots/test_repository_reads.py`:

```python
@pytest.mark.asyncio
async def test_get_bundle_item_with_snapshot_returns_pair_when_membership_holds(
    db_session,
):
    """ROB-275 — fetch (item, snapshot) for a (bundle_uuid, snapshot_uuid) pair."""
    from app.services.investment_snapshots.repository import (
        InvestmentSnapshotsRepository,
    )

    bundle_uuid, snapshot_uuid = await _seed_one_pair(db_session)
    repo = InvestmentSnapshotsRepository(db_session)
    pair = await repo.get_bundle_item_with_snapshot(
        bundle_uuid=bundle_uuid, snapshot_uuid=snapshot_uuid
    )
    assert pair is not None
    item, snap = pair
    assert snap.snapshot_uuid == snapshot_uuid
    assert item.role == "required"


@pytest.mark.asyncio
async def test_get_bundle_item_with_snapshot_returns_none_for_foreign_pair(
    db_session,
):
    """A snapshot_uuid that belongs to a *different* bundle returns None."""
    import uuid as _uuid

    from app.services.investment_snapshots.repository import (
        InvestmentSnapshotsRepository,
    )

    bundle_uuid, _ = await _seed_one_pair(db_session)
    repo = InvestmentSnapshotsRepository(db_session)
    pair = await repo.get_bundle_item_with_snapshot(
        bundle_uuid=bundle_uuid, snapshot_uuid=_uuid.uuid4()
    )
    assert pair is None
```

If `_seed_one_pair` does not exist yet in the file, add the helper near the top (after existing imports):

```python
async def _seed_one_pair(session):
    """ROB-275 helper — insert one bundle with one required snapshot, return (bundle_uuid, snapshot_uuid)."""
    import datetime as dt
    import uuid as _uuid

    from app.schemas.investment_snapshots import (
        BundleCreate,
        BundleItemCreate,
        SnapshotCreate,
        SnapshotRunCreate,
    )
    from app.services.investment_snapshots.repository import (
        InvestmentSnapshotsRepository,
    )

    repo = InvestmentSnapshotsRepository(session)
    now = dt.datetime(2026, 5, 20, 11, 0, 0, tzinfo=dt.UTC)
    run = await repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="kr",
            account_scope="kis_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
        )
    )
    snap = await repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="portfolio",
            market="kr",
            account_scope="kis_live",
            source_kind="manual",
            payload_json={"cash_krw": 1, "u": str(_uuid.uuid4())},
            as_of=now,
            freshness_status="fresh",
        )
    )
    bundle = await repo.insert_bundle(
        BundleCreate(
            purpose=f"rob275_{_uuid.uuid4().hex[:8]}",
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=now,
            status="complete",
        )
    )
    await repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=snap.snapshot_uuid, role="required"),
    )
    await session.commit()
    return bundle.bundle_uuid, snap.snapshot_uuid
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/investment_snapshots/test_repository_reads.py -v -k "get_bundle_item_with_snapshot"`
Expected: FAIL with `AttributeError: 'InvestmentSnapshotsRepository' object has no attribute 'get_bundle_item_with_snapshot'`.

- [ ] **Step 3: Implement `get_bundle_item_with_snapshot`**

Add to `app/services/investment_snapshots/repository.py`, in the "Phase 2 — SELECT-only read methods" section (after `list_bundle_items_with_snapshots`):

```python
    async def get_bundle_item_with_snapshot(
        self,
        *,
        bundle_uuid: uuid.UUID,
        snapshot_uuid: uuid.UUID,
    ) -> tuple[InvestmentSnapshotBundleItem, InvestmentSnapshot] | None:
        """ROB-275 — return ``(bundle_item, snapshot)`` for a specific pair, or None.

        Used by the report-centric evidence viewer to enforce
        bundle↔snapshot membership before returning a payload: a snapshot
        that belongs to a different bundle returns None and the caller
        maps it to HTTP 404.
        """
        stmt = (
            sa.select(InvestmentSnapshotBundleItem, InvestmentSnapshot)
            .join(
                InvestmentSnapshotBundle,
                InvestmentSnapshotBundle.id == InvestmentSnapshotBundleItem.bundle_id,
            )
            .join(
                InvestmentSnapshot,
                InvestmentSnapshot.id == InvestmentSnapshotBundleItem.snapshot_id,
            )
            .where(
                InvestmentSnapshotBundle.bundle_uuid == bundle_uuid,
                InvestmentSnapshot.snapshot_uuid == snapshot_uuid,
            )
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        item, snap = row
        return item, snap
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/investment_snapshots/test_repository_reads.py -v -k "get_bundle_item_with_snapshot"`
Expected: PASS for both tests.

- [ ] **Step 5: Run the append-only surface test to confirm no mutation method leaked**

The snapshot repository has a surface-lock test that guards against accidental mutation method additions:

Run: `uv run pytest tests/services/investment_snapshots/test_append_only.py -v`
Expected: PASS. If this test enumerates public methods and asserts none new appeared as writes, update the expected-name list in that test in the SAME commit if it gates on a frozen set (read the test first; if it allow-lists by prefix `insert_`/`link_` for writes and is permissive about reads, no change is needed).

- [ ] **Step 6: Commit**

```bash
git add app/services/investment_snapshots/repository.py tests/services/investment_snapshots/test_repository_reads.py
git commit -m "feat(rob-275): add bundle-scoped snapshot lookup for evidence viewer

Adds InvestmentSnapshotsRepository.get_bundle_item_with_snapshot which
returns the (bundle_item, snapshot) pair only when the snapshot is a
member of the given bundle. This is the membership check used by the
report-centric evidence detail endpoint to refuse cross-report snapshot
fetches."
```

---

## Task 2: Response schemas for the report-centric evidence viewer

**Files:**
- Modify: `app/schemas/investment_reports.py`
- Test: `tests/test_investment_reports_schemas.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_investment_reports_schemas.py`:

```python
def test_report_snapshot_bundle_response_legacy_no_snapshot_shape() -> None:
    """ROB-275 — legacy/no-snapshot reports return an empty bundle response."""
    from app.schemas.investment_reports import ReportSnapshotBundleResponse

    response = ReportSnapshotBundleResponse(legacy_no_snapshot=True)
    assert response.bundle is None
    assert response.items == []
    assert response.unavailable_sources is None
    assert response.source_conflicts is None


def test_report_snapshot_detail_response_includes_full_payload() -> None:
    """ROB-275 — detail response carries role + payload."""
    import datetime as dt
    import uuid as _uuid

    from app.schemas.investment_reports import ReportSnapshotDetailResponse

    response = ReportSnapshotDetailResponse(
        snapshot_uuid=_uuid.uuid4(),
        role="required",
        snapshot_kind="portfolio",
        source_kind="manual",
        market="kr",
        symbol=None,
        account_scope="kis_live",
        source_table=None,
        source_id=None,
        source_uri=None,
        freshness_status="fresh",
        as_of=dt.datetime(2026, 5, 20, 11, 0, 0, tzinfo=dt.UTC),
        valid_until=None,
        source_timestamps_json={},
        coverage_json={},
        errors_json={},
        payload_json={"cash_krw": 1_000_000},
    )
    assert response.role == "required"
    assert response.payload_json == {"cash_krw": 1_000_000}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_investment_reports_schemas.py -v -k "report_snapshot"`
Expected: FAIL with `ImportError: cannot import name 'ReportSnapshotBundleResponse'`.

- [ ] **Step 3: Add the schemas**

Append to `app/schemas/investment_reports.py` (after the existing `InvestmentReportListResponse` class):

```python
# ---------------------------------------------------------------------------
# ROB-275 — Report-centric snapshot evidence viewer response shapes.
#
# These wrap the existing investment_snapshots read shapes for use under
# the /invest/api/investment-reports/{report_uuid}/snapshot-bundle and
# .../snapshots/{snapshot_uuid} endpoints. The /trading/api/investment-snapshots
# MCP-flag-gated routes are NOT touched.
# ---------------------------------------------------------------------------
from app.schemas.investment_snapshots import (  # noqa: E402
    BundleItemRole,
    BundleStatus,
    FreshnessStatus,
    SnapshotAccountScope,
    SnapshotKind,
    SnapshotMarket,
    SourceKind,
)


class ReportSnapshotBundleSummaryView(BaseModel):
    """Bundle header surfaced via the report-centric evidence endpoint."""

    bundle_uuid: UUID
    purpose: str
    policy_version: str
    status: BundleStatus
    as_of: datetime
    coverage_summary: dict[str, Any]
    freshness_summary: dict[str, Any]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReportSnapshotBundleItemView(BaseModel):
    """One row in the report's snapshot evidence list — metadata only.

    ``payload_size_bytes`` is computed from the snapshot's stored JSON in
    the service layer; clients use it to hint at how heavy a payload
    fetch will be without actually downloading it.
    """

    snapshot_uuid: UUID
    role: BundleItemRole
    snapshot_kind: SnapshotKind
    source_kind: SourceKind
    market: SnapshotMarket
    symbol: str | None
    account_scope: SnapshotAccountScope | None
    freshness_status: FreshnessStatus
    as_of: datetime
    valid_until: datetime | None
    source_table: str | None
    source_id: int | None
    source_uri: str | None
    payload_size_bytes: int | None

    model_config = ConfigDict(from_attributes=True)


class ReportSnapshotBundleResponse(BaseModel):
    """``GET /invest/api/investment-reports/{report_uuid}/snapshot-bundle``.

    ``legacy_no_snapshot=True`` means the report exists but has no
    ``snapshot_bundle_uuid`` — caller renders a legacy message.
    """

    bundle: ReportSnapshotBundleSummaryView | None = None
    items: list[ReportSnapshotBundleItemView] = Field(default_factory=list)
    unavailable_sources: dict[str, Any] | None = None
    source_conflicts: dict[str, Any] | None = None
    legacy_no_snapshot: bool = False


class ReportSnapshotDetailResponse(BaseModel):
    """``GET /invest/api/investment-reports/{report_uuid}/snapshots/{snapshot_uuid}``.

    Returned only after a successful membership check
    (snapshot_uuid ∈ this report's bundle_items). Carries the snapshot's
    full DB payload + metadata, plus the bundle item's role/context.
    """

    snapshot_uuid: UUID
    role: BundleItemRole
    snapshot_kind: SnapshotKind
    source_kind: SourceKind
    market: SnapshotMarket
    symbol: str | None
    account_scope: SnapshotAccountScope | None
    source_table: str | None
    source_id: int | None
    source_uri: str | None
    freshness_status: FreshnessStatus
    as_of: datetime
    valid_until: datetime | None
    source_timestamps_json: dict[str, Any]
    coverage_json: dict[str, Any]
    errors_json: dict[str, Any]
    payload_json: dict[str, Any]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_investment_reports_schemas.py -v -k "report_snapshot"`
Expected: PASS for both tests.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/investment_reports.py tests/test_investment_reports_schemas.py
git commit -m "feat(rob-275): add report-centric snapshot evidence response schemas

Adds ReportSnapshotBundleResponse / BundleSummaryView / BundleItemView /
DetailResponse. These describe the read-only viewer payloads served from
the /invest/api/investment-reports/{report_uuid}/snapshot-bundle and
.../snapshots/{snapshot_uuid} endpoints; legacy_no_snapshot=True lets
the bundle endpoint return 200 for reports without a bundle while the
detail endpoint stays strict (404 on missing membership)."
```

---

## Task 3: Query service — `get_report_snapshot_bundle`

**Files:**
- Modify: `app/services/investment_reports/query_service.py`
- Test: `tests/test_investment_reports_snapshot_evidence_service.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_investment_reports_snapshot_evidence_service.py`:

```python
"""ROB-275 — Snapshot evidence service tests.

Uses the global ``db_session`` fixture (creates every table via
``Base.metadata.create_all``) because the test exercises both
``review.investment_reports`` *and* ``review.investment_snapshot_*``
tables. The ``_investment_reports_helpers.session`` fixture only owns
the 5 investment-report tables and is not suitable here.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid

import pytest

from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
)
from app.services.investment_reports.repository import (
    InvestmentReportsRepository,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)


_NOW = dt.datetime(2026, 5, 20, 11, 0, 0, tzinfo=dt.UTC)


async def _seed_report_with_bundle(db_session):
    """Seed one report with snapshot_bundle_uuid → one required snapshot."""
    snap_repo = InvestmentSnapshotsRepository(db_session)
    run = await snap_repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="kr",
            account_scope="kis_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
        )
    )
    snap = await snap_repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="portfolio",
            market="kr",
            account_scope="kis_live",
            source_kind="manual",
            payload_json={"cash_krw": 1_000_000, "u": str(_uuid.uuid4())},
            as_of=_NOW,
            freshness_status="fresh",
        )
    )
    bundle = await snap_repo.insert_bundle(
        BundleCreate(
            purpose=f"rob275_{_uuid.uuid4().hex[:8]}",
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=_NOW,
            status="partial",
        )
    )
    await snap_repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=snap.snapshot_uuid, role="required"),
    )

    report_repo = InvestmentReportsRepository(db_session)
    report = await report_repo.insert_report(
        report_uuid=_uuid.uuid4(),
        idempotency_key=f"k-{_uuid.uuid4().hex[:8]}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="rob275-test",
        title="t",
        summary="s",
        snapshot_bundle_uuid=bundle.bundle_uuid,
        snapshot_policy_version="intraday_action_report_v1",
        unavailable_sources={"naver_remote_debug": "blocked"},
        source_conflicts={"market": {"kis_mcp": 1.0, "manual": 1.1}},
    )
    await db_session.commit()
    return report.report_uuid, bundle.bundle_uuid, snap.snapshot_uuid


async def _seed_report_without_bundle(db_session):
    report_repo = InvestmentReportsRepository(db_session)
    report = await report_repo.insert_report(
        report_uuid=_uuid.uuid4(),
        idempotency_key=f"k-{_uuid.uuid4().hex[:8]}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="rob275-test",
        title="t",
        summary="s",
    )
    await db_session.commit()
    return report.report_uuid


@pytest.mark.asyncio
async def test_get_report_snapshot_bundle_returns_none_for_unknown_report(db_session):
    svc = InvestmentReportQueryService(db_session)
    assert await svc.get_report_snapshot_bundle(_uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_get_report_snapshot_bundle_returns_legacy_for_no_bundle(db_session):
    report_uuid = await _seed_report_without_bundle(db_session)
    svc = InvestmentReportQueryService(db_session)
    response = await svc.get_report_snapshot_bundle(report_uuid)
    assert response is not None
    assert response["legacy_no_snapshot"] is True
    assert response["bundle"] is None
    assert response["items"] == []


@pytest.mark.asyncio
async def test_get_report_snapshot_bundle_returns_bundle_and_items(db_session):
    report_uuid, bundle_uuid, snap_uuid = await _seed_report_with_bundle(db_session)
    svc = InvestmentReportQueryService(db_session)
    response = await svc.get_report_snapshot_bundle(report_uuid)
    assert response is not None
    assert response["legacy_no_snapshot"] is False
    bundle = response["bundle"]
    assert bundle is not None
    assert bundle.bundle_uuid == bundle_uuid
    assert bundle.status == "partial"
    items = response["items"]
    assert len(items) == 1
    item = items[0]
    assert item.snapshot_uuid == snap_uuid
    assert item.role == "required"
    assert item.snapshot_kind == "portfolio"
    assert item.payload_size_bytes is not None
    assert item.payload_size_bytes > 0
    # unavailable_sources / source_conflicts come from the *report row*,
    # not from the bundle — the viewer surfaces them separately.
    assert response["unavailable_sources"] == {"naver_remote_debug": "blocked"}
    assert response["source_conflicts"] == {
        "market": {"kis_mcp": 1.0, "manual": 1.1}
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_investment_reports_snapshot_evidence_service.py -v -k "snapshot_bundle"`
Expected: FAIL with `AttributeError: 'InvestmentReportQueryService' object has no attribute 'get_report_snapshot_bundle'`.

- [ ] **Step 3: Implement `get_report_snapshot_bundle`**

Add to `app/services/investment_reports/query_service.py`:

At the top of the imports section (after the existing imports):

```python
import json
from uuid import UUID

from app.schemas.investment_reports import (
    ReportSnapshotBundleItemView,
    ReportSnapshotBundleSummaryView,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)
```

(If `json` / `UUID` are already imported, do not re-import — keep imports tidy.)

Extend `InvestmentReportQueryService.__init__` to optionally accept a snapshot repository:

```python
    def __init__(
        self,
        session: AsyncSession,
        repository: InvestmentReportsRepository | None = None,
        snapshot_repository: InvestmentSnapshotsRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or InvestmentReportsRepository(session)
        self._snap_repo = snapshot_repository or InvestmentSnapshotsRepository(
            session
        )
```

Then add the method (after `get_bundle`):

```python
    # ------------------------------------------------------------------
    # ROB-275 — Report-centric snapshot evidence read paths.
    # ------------------------------------------------------------------
    async def get_report_snapshot_bundle(
        self, report_uuid: UUID
    ) -> dict[str, Any] | None:
        """Return the bundle + linked items for a report, or a legacy shape.

        Returns:
          * ``None`` if the report doesn't exist (router → 404).
          * ``{"legacy_no_snapshot": True, "bundle": None, "items": [], ...}``
            if the report exists but has no ``snapshot_bundle_uuid`` (router → 200).
          * Full shape otherwise.

        Note: ``unavailable_sources`` and ``source_conflicts`` come from
        the report row, never from the bundle — they describe what the
        report's generator observed at write time, not what is *linked*
        to this bundle. UI renders them in separate sections.
        """
        report = await self._repo.get_report_by_uuid(report_uuid)
        if report is None:
            return None
        if report.snapshot_bundle_uuid is None:
            return {
                "legacy_no_snapshot": True,
                "bundle": None,
                "items": [],
                "unavailable_sources": report.unavailable_sources,
                "source_conflicts": report.source_conflicts,
            }

        bundle = await self._snap_repo.get_bundle_by_uuid(report.snapshot_bundle_uuid)
        if bundle is None:
            # Defensive: report.snapshot_bundle_uuid is a logical link
            # (no FK), so a deleted bundle is possible in theory. Treat
            # as legacy/no-snapshot rather than failing the page.
            return {
                "legacy_no_snapshot": True,
                "bundle": None,
                "items": [],
                "unavailable_sources": report.unavailable_sources,
                "source_conflicts": report.source_conflicts,
            }

        pairs = await self._snap_repo.list_bundle_items_with_snapshots(bundle.id)
        item_views = [
            ReportSnapshotBundleItemView(
                snapshot_uuid=snap.snapshot_uuid,
                role=item.role,  # type: ignore[arg-type]
                snapshot_kind=snap.snapshot_kind,  # type: ignore[arg-type]
                source_kind=snap.source_kind,  # type: ignore[arg-type]
                market=snap.market,  # type: ignore[arg-type]
                symbol=snap.symbol,
                account_scope=snap.account_scope,  # type: ignore[arg-type]
                freshness_status=snap.freshness_status,  # type: ignore[arg-type]
                as_of=snap.as_of,
                valid_until=snap.valid_until,
                source_table=snap.source_table,
                source_id=snap.source_id,
                source_uri=snap.source_uri,
                payload_size_bytes=_payload_size_bytes(snap.payload_json),
            )
            for item, snap in pairs
        ]
        bundle_view = ReportSnapshotBundleSummaryView(
            bundle_uuid=bundle.bundle_uuid,
            purpose=bundle.purpose,
            policy_version=bundle.policy_version,
            status=bundle.status,  # type: ignore[arg-type]
            as_of=bundle.as_of,
            coverage_summary=bundle.coverage_summary,
            freshness_summary=bundle.freshness_summary,
            created_at=bundle.created_at,
        )
        return {
            "legacy_no_snapshot": False,
            "bundle": bundle_view,
            "items": item_views,
            "unavailable_sources": report.unavailable_sources,
            "source_conflicts": report.source_conflicts,
        }
```

At the bottom of the file (module level), add the helper:

```python
def _payload_size_bytes(payload_json: dict[str, Any] | None) -> int | None:
    """Cheap UTF-8 byte count of a JSON-serialised payload. ``None`` if missing."""
    if payload_json is None:
        return None
    return len(json.dumps(payload_json, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_investment_reports_snapshot_evidence_service.py -v -k "snapshot_bundle"`
Expected: PASS for all three tests in this task.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_reports/query_service.py tests/test_investment_reports_snapshot_evidence_service.py
git commit -m "feat(rob-275): add report-centric snapshot bundle read in query service

InvestmentReportQueryService.get_report_snapshot_bundle returns:
  - None when the report doesn't exist (router -> 404)
  - {legacy_no_snapshot: True, ...} when the report has no bundle
    (router -> 200 with a legacy shape so the page does not break)
  - Full bundle + per-item metadata otherwise.

unavailable_sources / source_conflicts come from the report row and are
surfaced separately from bundle-linked items — the viewer renders them
in distinct sections (Toss/Naver unavailability is not a 'used snapshot')."
```

---

## Task 4: Query service — `get_report_snapshot_detail`

**Files:**
- Modify: `app/services/investment_reports/query_service.py`
- Test: `tests/test_investment_reports_snapshot_evidence_service.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investment_reports_snapshot_evidence_service.py`:

```python
@pytest.mark.asyncio
async def test_get_report_snapshot_detail_returns_none_for_unknown_report(db_session):
    svc = InvestmentReportQueryService(db_session)
    assert await svc.get_report_snapshot_detail(_uuid.uuid4(), _uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_get_report_snapshot_detail_returns_none_when_report_has_no_bundle(
    db_session,
):
    report_uuid = await _seed_report_without_bundle(db_session)
    svc = InvestmentReportQueryService(db_session)
    assert await svc.get_report_snapshot_detail(report_uuid, _uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_get_report_snapshot_detail_returns_payload_for_member(db_session):
    report_uuid, _bundle_uuid, snap_uuid = await _seed_report_with_bundle(db_session)
    svc = InvestmentReportQueryService(db_session)
    detail = await svc.get_report_snapshot_detail(report_uuid, snap_uuid)
    assert detail is not None
    assert detail.snapshot_uuid == snap_uuid
    assert detail.role == "required"
    assert detail.snapshot_kind == "portfolio"
    assert detail.payload_json["cash_krw"] == 1_000_000


@pytest.mark.asyncio
async def test_get_report_snapshot_detail_returns_none_for_non_member_snapshot(
    db_session,
):
    """A snapshot_uuid that exists but is NOT in this report's bundle → None (router → 404)."""
    report_uuid, _bundle_uuid, _snap_uuid = await _seed_report_with_bundle(db_session)

    # Create a second snapshot under a DIFFERENT bundle.
    snap_repo = InvestmentSnapshotsRepository(db_session)
    run = await snap_repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="kr",
            account_scope="kis_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
        )
    )
    other_snap = await snap_repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="market",
            market="kr",
            source_kind="domain_ref",
            source_table="market_quote_snapshots",
            source_id=99,
            source_uri=f"market_quote_snapshots:{_uuid.uuid4().hex[:6]}",
            payload_json={"kospi": 2700.0, "u": str(_uuid.uuid4())},
            as_of=_NOW,
            freshness_status="fresh",
        )
    )
    other_bundle = await snap_repo.insert_bundle(
        BundleCreate(
            purpose=f"rob275_other_{_uuid.uuid4().hex[:8]}",
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=_NOW,
            status="complete",
        )
    )
    await snap_repo.link_bundle_item(
        bundle_uuid=other_bundle.bundle_uuid,
        item=BundleItemCreate(
            snapshot_uuid=other_snap.snapshot_uuid, role="required"
        ),
    )
    await db_session.commit()

    svc = InvestmentReportQueryService(db_session)
    detail = await svc.get_report_snapshot_detail(
        report_uuid, other_snap.snapshot_uuid
    )
    assert detail is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_investment_reports_snapshot_evidence_service.py -v -k "snapshot_detail"`
Expected: FAIL with `AttributeError: 'InvestmentReportQueryService' object has no attribute 'get_report_snapshot_detail'`.

- [ ] **Step 3: Implement `get_report_snapshot_detail`**

Add the import (top of file, only if missing):

```python
from app.schemas.investment_reports import ReportSnapshotDetailResponse
```

Then add the method to `InvestmentReportQueryService` (after `get_report_snapshot_bundle`):

```python
    async def get_report_snapshot_detail(
        self, report_uuid: UUID, snapshot_uuid: UUID
    ) -> ReportSnapshotDetailResponse | None:
        """Return one snapshot's payload + bundle role/context for a report.

        Membership-checked: returns ``None`` (router → 404) when any of:
          * the report does not exist
          * the report has no ``snapshot_bundle_uuid``
          * the snapshot is not a member of this report's bundle
        Snapshots that exist globally but belong to a different bundle
        always return None — they are not addressable via this report's
        URL even though the underlying ``investment_snapshots`` row is
        globally reusable.
        """
        report = await self._repo.get_report_by_uuid(report_uuid)
        if report is None or report.snapshot_bundle_uuid is None:
            return None
        pair = await self._snap_repo.get_bundle_item_with_snapshot(
            bundle_uuid=report.snapshot_bundle_uuid,
            snapshot_uuid=snapshot_uuid,
        )
        if pair is None:
            return None
        item, snap = pair
        return ReportSnapshotDetailResponse(
            snapshot_uuid=snap.snapshot_uuid,
            role=item.role,  # type: ignore[arg-type]
            snapshot_kind=snap.snapshot_kind,  # type: ignore[arg-type]
            source_kind=snap.source_kind,  # type: ignore[arg-type]
            market=snap.market,  # type: ignore[arg-type]
            symbol=snap.symbol,
            account_scope=snap.account_scope,  # type: ignore[arg-type]
            source_table=snap.source_table,
            source_id=snap.source_id,
            source_uri=snap.source_uri,
            freshness_status=snap.freshness_status,  # type: ignore[arg-type]
            as_of=snap.as_of,
            valid_until=snap.valid_until,
            source_timestamps_json=snap.source_timestamps_json,
            coverage_json=snap.coverage_json,
            errors_json=snap.errors_json,
            payload_json=snap.payload_json,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_investment_reports_snapshot_evidence_service.py -v`
Expected: PASS for all seven service tests (3 bundle + 4 detail).

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_reports/query_service.py tests/test_investment_reports_snapshot_evidence_service.py
git commit -m "feat(rob-275): add membership-checked snapshot detail read

InvestmentReportQueryService.get_report_snapshot_detail returns None
unless the requested snapshot is a member of the report's bundle. This
is the only access path — there is no global /invest snapshot detail
endpoint. A snapshot that belongs to a *different* bundle is not
addressable via this report's URL even though the snapshot row itself
is globally reusable."
```

---

## Task 5: Router — two thin GET endpoints

**Files:**
- Modify: `app/routers/investment_reports.py`
- Test: `tests/routers/test_investment_reports_snapshot_evidence_router.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/routers/test_investment_reports_snapshot_evidence_router.py`:

```python
"""ROB-275 — Router-level tests for snapshot evidence endpoints.

Same direct-handler-invocation pattern as
``tests/test_investment_reports_router.py`` — no TestClient, dependencies
supplied manually. Uses ``db_session`` because we need both
investment_reports and investment_snapshot_* tables.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers.investment_reports import (
    get_investment_report_snapshot_bundle,
    get_investment_report_snapshot_detail,
)
from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
)
from app.services.investment_reports.repository import (
    InvestmentReportsRepository,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)

_USER = SimpleNamespace(username="operator-test", id=1)
_NOW = dt.datetime(2026, 5, 20, 11, 0, 0, tzinfo=dt.UTC)


async def _seed_report_with_bundle(db_session):
    snap_repo = InvestmentSnapshotsRepository(db_session)
    run = await snap_repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="kr",
            account_scope="kis_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
        )
    )
    snap = await snap_repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="portfolio",
            market="kr",
            account_scope="kis_live",
            source_kind="manual",
            payload_json={"cash_krw": 1_000, "u": str(_uuid.uuid4())},
            as_of=_NOW,
            freshness_status="fresh",
        )
    )
    bundle = await snap_repo.insert_bundle(
        BundleCreate(
            purpose=f"rob275_router_{_uuid.uuid4().hex[:8]}",
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=_NOW,
            status="complete",
        )
    )
    await snap_repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=snap.snapshot_uuid, role="required"),
    )
    report_repo = InvestmentReportsRepository(db_session)
    report = await report_repo.insert_report(
        report_uuid=_uuid.uuid4(),
        idempotency_key=f"k-{_uuid.uuid4().hex[:8]}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="rob275-router-test",
        title="t",
        summary="s",
        snapshot_bundle_uuid=bundle.bundle_uuid,
    )
    await db_session.commit()
    return report.report_uuid, snap.snapshot_uuid


async def _seed_report_without_bundle(db_session):
    report_repo = InvestmentReportsRepository(db_session)
    report = await report_repo.insert_report(
        report_uuid=_uuid.uuid4(),
        idempotency_key=f"k-{_uuid.uuid4().hex[:8]}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="rob275-router-test",
        title="t",
        summary="s",
    )
    await db_session.commit()
    return report.report_uuid


@pytest.mark.asyncio
async def test_snapshot_bundle_returns_404_for_unknown_report(db_session):
    service = InvestmentReportQueryService(db_session)
    with pytest.raises(HTTPException) as exc:
        await get_investment_report_snapshot_bundle(
            report_uuid=_uuid.uuid4(), _user=_USER, service=service
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_snapshot_bundle_returns_legacy_shape_for_report_without_bundle(
    db_session,
):
    report_uuid = await _seed_report_without_bundle(db_session)
    service = InvestmentReportQueryService(db_session)
    response = await get_investment_report_snapshot_bundle(
        report_uuid=report_uuid, _user=_USER, service=service
    )
    assert response.legacy_no_snapshot is True
    assert response.bundle is None
    assert response.items == []


@pytest.mark.asyncio
async def test_snapshot_bundle_returns_full_response_for_report_with_bundle(
    db_session,
):
    report_uuid, snap_uuid = await _seed_report_with_bundle(db_session)
    service = InvestmentReportQueryService(db_session)
    response = await get_investment_report_snapshot_bundle(
        report_uuid=report_uuid, _user=_USER, service=service
    )
    assert response.legacy_no_snapshot is False
    assert response.bundle is not None
    assert len(response.items) == 1
    assert response.items[0].snapshot_uuid == snap_uuid


@pytest.mark.asyncio
async def test_snapshot_detail_returns_404_for_unknown_report(db_session):
    service = InvestmentReportQueryService(db_session)
    with pytest.raises(HTTPException) as exc:
        await get_investment_report_snapshot_detail(
            report_uuid=_uuid.uuid4(),
            snapshot_uuid=_uuid.uuid4(),
            _user=_USER,
            service=service,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_snapshot_detail_returns_404_for_report_without_bundle(db_session):
    report_uuid = await _seed_report_without_bundle(db_session)
    service = InvestmentReportQueryService(db_session)
    with pytest.raises(HTTPException) as exc:
        await get_investment_report_snapshot_detail(
            report_uuid=report_uuid,
            snapshot_uuid=_uuid.uuid4(),
            _user=_USER,
            service=service,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_snapshot_detail_returns_404_for_non_member_snapshot(db_session):
    report_uuid, _snap_uuid = await _seed_report_with_bundle(db_session)
    service = InvestmentReportQueryService(db_session)
    with pytest.raises(HTTPException) as exc:
        await get_investment_report_snapshot_detail(
            report_uuid=report_uuid,
            snapshot_uuid=_uuid.uuid4(),
            _user=_USER,
            service=service,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_snapshot_detail_returns_200_with_payload_for_member(db_session):
    report_uuid, snap_uuid = await _seed_report_with_bundle(db_session)
    service = InvestmentReportQueryService(db_session)
    response = await get_investment_report_snapshot_detail(
        report_uuid=report_uuid,
        snapshot_uuid=snap_uuid,
        _user=_USER,
        service=service,
    )
    assert response.snapshot_uuid == snap_uuid
    assert response.role == "required"
    assert response.payload_json["cash_krw"] == 1_000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/routers/test_investment_reports_snapshot_evidence_router.py -v`
Expected: FAIL with `ImportError: cannot import name 'get_investment_report_snapshot_bundle' from 'app.routers.investment_reports'`.

- [ ] **Step 3: Add the routes**

Add to `app/routers/investment_reports.py`. First, add to the imports at the top:

```python
from app.schemas.investment_reports import (
    # ... existing imports ...
    ReportSnapshotBundleResponse,
    ReportSnapshotDetailResponse,
)
```

(Merge into the existing import block — do not duplicate the block. The existing imports already pull from `app.schemas.investment_reports`.)

Then, *after* the existing `get_investment_report` route and *before* the `# Snapshot-backed advisory report generator` section, add the two new routes:

```python
# ---------------------------------------------------------------------------
# ROB-275 — Snapshot evidence viewer (read-only).
#
# These endpoints surface the bundle of snapshots actually linked to a
# report (membership via investment_snapshot_bundle_items). They are NOT
# gated by ``INVESTMENT_SNAPSHOTS_MCP_ENABLED``: gating is by data
# presence — the bundle endpoint returns a legacy/no_snapshot shape when
# report.snapshot_bundle_uuid is null, and the detail endpoint returns
# 404 on missing report / missing bundle / non-member snapshot.
# ---------------------------------------------------------------------------
@router.get(
    "/trading/api/investment-reports/{report_uuid}/snapshot-bundle",
    response_model=ReportSnapshotBundleResponse,
    summary="Get snapshot bundle linked to an investment report (ROB-275)",
)
@router.get(
    "/invest/api/investment-reports/{report_uuid}/snapshot-bundle",
    response_model=ReportSnapshotBundleResponse,
    summary="Get snapshot bundle for /invest report viewer (ROB-275)",
)
async def get_investment_report_snapshot_bundle(
    report_uuid: UUID,
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[InvestmentReportQueryService, Depends(_build_query_service)],
) -> ReportSnapshotBundleResponse:
    result = await service.get_report_snapshot_bundle(report_uuid)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="report_not_found"
        )
    return ReportSnapshotBundleResponse(
        bundle=result["bundle"],
        items=result["items"],
        unavailable_sources=result["unavailable_sources"],
        source_conflicts=result["source_conflicts"],
        legacy_no_snapshot=result["legacy_no_snapshot"],
    )


@router.get(
    "/trading/api/investment-reports/{report_uuid}/snapshots/{snapshot_uuid}",
    response_model=ReportSnapshotDetailResponse,
    summary="Get one snapshot's payload via its report (ROB-275)",
)
@router.get(
    "/invest/api/investment-reports/{report_uuid}/snapshots/{snapshot_uuid}",
    response_model=ReportSnapshotDetailResponse,
    summary="Get one snapshot's payload for /invest report viewer (ROB-275)",
)
async def get_investment_report_snapshot_detail(
    report_uuid: UUID,
    snapshot_uuid: UUID,
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[InvestmentReportQueryService, Depends(_build_query_service)],
) -> ReportSnapshotDetailResponse:
    detail = await service.get_report_snapshot_detail(report_uuid, snapshot_uuid)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="snapshot_not_found_or_not_in_report_bundle",
        )
    return detail
```

- [ ] **Step 4: Run router tests to verify they pass**

Run: `uv run pytest tests/routers/test_investment_reports_snapshot_evidence_router.py -v`
Expected: PASS for all eight router tests.

- [ ] **Step 5: Verify existing router tests still pass**

Run: `uv run pytest tests/test_investment_reports_router.py -v`
Expected: All existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/routers/investment_reports.py tests/routers/test_investment_reports_snapshot_evidence_router.py
git commit -m "feat(rob-275): add /invest snapshot evidence endpoints

GET /invest/api/investment-reports/{report_uuid}/snapshot-bundle
  - 200 with legacy/no_snapshot shape if report has no snapshot_bundle_uuid
  - 200 with bundle + items + unavailable_sources/source_conflicts otherwise
  - 404 if report doesn't exist

GET /invest/api/investment-reports/{report_uuid}/snapshots/{snapshot_uuid}
  - 200 with metadata + payload if snapshot is in the report's bundle
  - 404 if report unknown, has no bundle, or snapshot is not a bundle member

Membership is enforced via investment_snapshot_bundle_items — a snapshot
that exists globally but belongs to a different bundle is 404 here. Not
gated by INVESTMENT_SNAPSHOTS_MCP_ENABLED; gating is by data presence on
the report row. Mirrored under /trading/api/ for parity with existing
investment-reports routes."
```

---

## Task 6: Frontend types

**Files:**
- Modify: `frontend/invest/src/types/investmentReports.ts`

- [ ] **Step 1: Append new types**

Append to `frontend/invest/src/types/investmentReports.ts`:

```typescript
// ROB-275 — Snapshot evidence viewer types. Mirrors
// ``app/schemas/investment_reports.py::ReportSnapshotBundle*`` and
// ``ReportSnapshotDetailResponse``. Snapshot literals duplicate the
// backend enums on purpose; if backend enums grow, update here too.

export type BundleStatus =
  | "complete"
  | "partial"
  | "stale_fallback"
  | "failed";

export type BundleItemRole =
  | "required"
  | "optional"
  | "fallback"
  | "conflict_evidence";

export type SnapshotKind =
  | "portfolio"
  | "market"
  | "news"
  | "symbol"
  | "candidate_universe"
  | "browser_probe"
  | "invest_page"
  | "journal"
  | "watch_context"
  | "naver_remote_debug"
  | "toss_remote_debug"
  | "llm_input_frozen";

export type SnapshotSourceKind =
  | "kis_mcp"
  | "auto_trader_mcp"
  | "invest_api"
  | "naver_remote_debug"
  | "toss_remote_debug"
  | "combined"
  | "news_ingestor"
  | "manual"
  | "domain_ref";

export type SnapshotFreshness =
  | "fresh"
  | "soft_stale"
  | "hard_stale"
  | "partial"
  | "unavailable";

export interface ReportSnapshotBundleSummary {
  bundleUuid: string;
  purpose: string;
  policyVersion: string;
  status: BundleStatus;
  asOf: string;
  coverageSummary: Record<string, unknown>;
  freshnessSummary: Record<string, unknown>;
  createdAt: string;
}

export interface ReportSnapshotBundleItem {
  snapshotUuid: string;
  role: BundleItemRole;
  snapshotKind: SnapshotKind;
  sourceKind: SnapshotSourceKind;
  market: Market;
  symbol: string | null;
  accountScope: AccountScope | null;
  freshnessStatus: SnapshotFreshness;
  asOf: string;
  validUntil: string | null;
  sourceTable: string | null;
  sourceId: number | null;
  sourceUri: string | null;
  payloadSizeBytes: number | null;
}

export interface ReportSnapshotBundle {
  bundle: ReportSnapshotBundleSummary | null;
  items: ReportSnapshotBundleItem[];
  unavailableSources: Record<string, unknown> | null;
  sourceConflicts: Record<string, unknown> | null;
  legacyNoSnapshot: boolean;
}

export interface ReportSnapshotDetail {
  snapshotUuid: string;
  role: BundleItemRole;
  snapshotKind: SnapshotKind;
  sourceKind: SnapshotSourceKind;
  market: Market;
  symbol: string | null;
  accountScope: AccountScope | null;
  sourceTable: string | null;
  sourceId: number | null;
  sourceUri: string | null;
  freshnessStatus: SnapshotFreshness;
  asOf: string;
  validUntil: string | null;
  sourceTimestampsJson: Record<string, unknown>;
  coverageJson: Record<string, unknown>;
  errorsJson: Record<string, unknown>;
  payloadJson: Record<string, unknown>;
}
```

- [ ] **Step 2: Type-check the frontend**

Run: `cd frontend/invest && npm run typecheck` (or `npx tsc --noEmit` — use whichever command the project uses; check `frontend/invest/package.json` scripts).
Expected: No type errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/types/investmentReports.ts
git commit -m "feat(rob-275): add frontend types for snapshot evidence viewer"
```

---

## Task 7: Frontend API client — fetchers + normalisers

**Files:**
- Modify: `frontend/invest/src/api/investmentReports.ts`
- Modify: `frontend/invest/src/__tests__/investmentReports.api.test.ts`

- [ ] **Step 1: Write the failing tests**

Append to `frontend/invest/src/__tests__/investmentReports.api.test.ts`:

```typescript
describe("fetchReportSnapshotBundle", () => {
  it("normalises the bundle list response", async () => {
    mockFetchOnce({
      bundle: {
        bundle_uuid: "bundle-1",
        purpose: "rob275_smoke",
        policy_version: "intraday_action_report_v1",
        status: "partial",
        as_of: "2026-05-20T11:00:00Z",
        coverage_summary: { portfolio: { count: 1 } },
        freshness_summary: { overall: "partial" },
        created_at: "2026-05-20T11:00:00Z",
      },
      items: [
        {
          snapshot_uuid: "snap-1",
          role: "required",
          snapshot_kind: "portfolio",
          source_kind: "manual",
          market: "kr",
          symbol: null,
          account_scope: "kis_live",
          freshness_status: "fresh",
          as_of: "2026-05-20T11:00:00Z",
          valid_until: null,
          source_table: null,
          source_id: null,
          source_uri: null,
          payload_size_bytes: 256,
        },
      ],
      unavailable_sources: { naver_remote_debug: "blocked" },
      source_conflicts: null,
      legacy_no_snapshot: false,
    });

    const { fetchReportSnapshotBundle } = await import(
      "../api/investmentReports"
    );
    const response = await fetchReportSnapshotBundle("uuid-1");
    expect(response.legacyNoSnapshot).toBe(false);
    expect(response.bundle?.bundleUuid).toBe("bundle-1");
    expect(response.items).toHaveLength(1);
    expect(response.items[0]!.snapshotUuid).toBe("snap-1");
    expect(response.items[0]!.payloadSizeBytes).toBe(256);
    expect(response.unavailableSources).toEqual({
      naver_remote_debug: "blocked",
    });
    expect(response.sourceConflicts).toBeNull();
  });

  it("normalises a legacy/no-snapshot response", async () => {
    mockFetchOnce({
      bundle: null,
      items: [],
      unavailable_sources: null,
      source_conflicts: null,
      legacy_no_snapshot: true,
    });
    const { fetchReportSnapshotBundle } = await import(
      "../api/investmentReports"
    );
    const response = await fetchReportSnapshotBundle("uuid-1");
    expect(response.legacyNoSnapshot).toBe(true);
    expect(response.bundle).toBeNull();
    expect(response.items).toEqual([]);
  });
});

describe("fetchReportSnapshotDetail", () => {
  it("normalises the detail payload and URL-encodes both UUIDs", async () => {
    mockFetchOnce({
      snapshot_uuid: "snap-1",
      role: "required",
      snapshot_kind: "portfolio",
      source_kind: "manual",
      market: "kr",
      symbol: null,
      account_scope: "kis_live",
      source_table: null,
      source_id: null,
      source_uri: null,
      freshness_status: "fresh",
      as_of: "2026-05-20T11:00:00Z",
      valid_until: null,
      source_timestamps_json: { collected_at: "2026-05-20T11:00:00Z" },
      coverage_json: {},
      errors_json: {},
      payload_json: { cash_krw: 1_000_000 },
    });

    const { fetchReportSnapshotDetail } = await import(
      "../api/investmentReports"
    );
    const detail = await fetchReportSnapshotDetail("uuid 1", "snap 1");
    expect(detail.snapshotUuid).toBe("snap-1");
    expect(detail.role).toBe("required");
    expect(detail.payloadJson).toEqual({ cash_krw: 1_000_000 });
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("uuid%201/snapshots/snap%201"),
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("throws on non-2xx (e.g. 404 for non-member snapshot)", async () => {
    mockFetchOnce({}, 404);
    const { fetchReportSnapshotDetail } = await import(
      "../api/investmentReports"
    );
    await expect(
      fetchReportSnapshotDetail("uuid-1", "snap-x"),
    ).rejects.toThrow(/404/);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend/invest && npx vitest run src/__tests__/investmentReports.api.test.ts`
Expected: FAIL with `fetchReportSnapshotBundle is not a function` (or similar import error).

- [ ] **Step 3: Implement the fetchers**

Append to `frontend/invest/src/api/investmentReports.ts`:

```typescript
import type {
  ReportSnapshotBundle,
  ReportSnapshotBundleItem,
  ReportSnapshotBundleSummary,
  ReportSnapshotDetail,
  BundleItemRole,
  BundleStatus,
  SnapshotFreshness,
  SnapshotKind,
  SnapshotSourceKind,
} from "../types/investmentReports";

const BUNDLE_SNAPSHOT_ENDPOINT = (reportUuid: string) =>
  `/invest/api/investment-reports/${encodeURIComponent(reportUuid)}/snapshot-bundle`;
const SNAPSHOT_DETAIL_ENDPOINT = (
  reportUuid: string,
  snapshotUuid: string,
) =>
  `/invest/api/investment-reports/${encodeURIComponent(reportUuid)}/snapshots/${encodeURIComponent(snapshotUuid)}`;

type ApiBundle = Record<string, unknown>;
type ApiBundleItem = Record<string, unknown>;
type ApiSnapshotDetail = Record<string, unknown>;

function normalizeBundleSummary(
  raw: ApiBundle,
): ReportSnapshotBundleSummary {
  return {
    bundleUuid: asString(raw.bundle_uuid),
    purpose: asString(raw.purpose),
    policyVersion: asString(raw.policy_version),
    status: asString(raw.status, "complete") as BundleStatus,
    asOf: asString(raw.as_of),
    coverageSummary: asRecord(raw.coverage_summary),
    freshnessSummary: asRecord(raw.freshness_summary),
    createdAt: asString(raw.created_at),
  };
}

function normalizeBundleItem(
  raw: ApiBundleItem,
): ReportSnapshotBundleItem {
  return {
    snapshotUuid: asString(raw.snapshot_uuid),
    role: asString(raw.role, "required") as BundleItemRole,
    snapshotKind: asString(raw.snapshot_kind, "portfolio") as SnapshotKind,
    sourceKind: asString(raw.source_kind, "manual") as SnapshotSourceKind,
    market: asString(raw.market, "kr") as ReportSnapshotBundleItem["market"],
    symbol: asOptionalString(raw.symbol),
    accountScope: asOptionalString(
      raw.account_scope,
    ) as ReportSnapshotBundleItem["accountScope"],
    freshnessStatus: asString(
      raw.freshness_status,
      "fresh",
    ) as SnapshotFreshness,
    asOf: asString(raw.as_of),
    validUntil: asOptionalString(raw.valid_until),
    sourceTable: asOptionalString(raw.source_table),
    sourceId:
      typeof raw.source_id === "number" && Number.isFinite(raw.source_id)
        ? (raw.source_id as number)
        : null,
    sourceUri: asOptionalString(raw.source_uri),
    payloadSizeBytes:
      typeof raw.payload_size_bytes === "number" &&
      Number.isFinite(raw.payload_size_bytes)
        ? (raw.payload_size_bytes as number)
        : null,
  };
}

function normalizeSnapshotDetail(
  raw: ApiSnapshotDetail,
): ReportSnapshotDetail {
  return {
    snapshotUuid: asString(raw.snapshot_uuid),
    role: asString(raw.role, "required") as BundleItemRole,
    snapshotKind: asString(raw.snapshot_kind, "portfolio") as SnapshotKind,
    sourceKind: asString(raw.source_kind, "manual") as SnapshotSourceKind,
    market: asString(raw.market, "kr") as ReportSnapshotDetail["market"],
    symbol: asOptionalString(raw.symbol),
    accountScope: asOptionalString(
      raw.account_scope,
    ) as ReportSnapshotDetail["accountScope"],
    sourceTable: asOptionalString(raw.source_table),
    sourceId:
      typeof raw.source_id === "number" && Number.isFinite(raw.source_id)
        ? (raw.source_id as number)
        : null,
    sourceUri: asOptionalString(raw.source_uri),
    freshnessStatus: asString(
      raw.freshness_status,
      "fresh",
    ) as SnapshotFreshness,
    asOf: asString(raw.as_of),
    validUntil: asOptionalString(raw.valid_until),
    sourceTimestampsJson: asRecord(raw.source_timestamps_json),
    coverageJson: asRecord(raw.coverage_json),
    errorsJson: asRecord(raw.errors_json),
    payloadJson: asRecord(raw.payload_json),
  };
}

export async function fetchReportSnapshotBundle(
  reportUuid: string,
  signal?: AbortSignal,
): Promise<ReportSnapshotBundle> {
  const raw = await readJson<{
    bundle?: ApiBundle | null;
    items?: ApiBundleItem[];
    unavailable_sources?: Record<string, unknown> | null;
    source_conflicts?: Record<string, unknown> | null;
    legacy_no_snapshot?: boolean;
  }>(BUNDLE_SNAPSHOT_ENDPOINT(reportUuid), signal);

  return {
    bundle:
      raw.bundle == null
        ? null
        : normalizeBundleSummary(raw.bundle as ApiBundle),
    items: asArray<ApiBundleItem>(raw.items).map(normalizeBundleItem),
    unavailableSources: asOptionalRecord(raw.unavailable_sources),
    sourceConflicts: asOptionalRecord(raw.source_conflicts),
    legacyNoSnapshot: Boolean(raw.legacy_no_snapshot),
  };
}

export async function fetchReportSnapshotDetail(
  reportUuid: string,
  snapshotUuid: string,
  signal?: AbortSignal,
): Promise<ReportSnapshotDetail> {
  const raw = await readJson<ApiSnapshotDetail>(
    SNAPSHOT_DETAIL_ENDPOINT(reportUuid, snapshotUuid),
    signal,
  );
  return normalizeSnapshotDetail(raw);
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend/invest && npx vitest run src/__tests__/investmentReports.api.test.ts`
Expected: PASS for all existing tests plus the new ones.

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/api/investmentReports.ts frontend/invest/src/__tests__/investmentReports.api.test.ts
git commit -m "feat(rob-275): add API client + tests for snapshot evidence viewer

fetchReportSnapshotBundle and fetchReportSnapshotDetail hit the
/invest/api/investment-reports/{report_uuid}/snapshot-bundle and
.../snapshots/{snapshot_uuid} endpoints respectively. Both URLs encode
their UUID path components. The detail fetcher is intended to be called
lazily on row click, never eagerly on report-detail mount."
```

---

## Task 8: Frontend hook — `useReportSnapshotBundle` (eager)

**Files:**
- Create: `frontend/invest/src/hooks/useReportSnapshotBundle.ts`

- [ ] **Step 1: Implement the hook**

Mirror the existing `useInvestmentReportBundle.ts` shape — same eager `useEffect`/`AbortController` pattern.

Create `frontend/invest/src/hooks/useReportSnapshotBundle.ts`:

```typescript
// ROB-275 — eager fetch of the snapshot evidence bundle for a report.
//
// Fires once on mount per (reportUuid). Loads only metadata + per-item
// summary; the heavy payload JSON for each snapshot is fetched lazily by
// useSnapshotPayload only when the user opens a row.

import { useEffect, useState } from "react";

import { fetchReportSnapshotBundle } from "../api/investmentReports";
import type {
  InvestmentReportRequestState,
  ReportSnapshotBundle,
} from "../types/investmentReports";

interface UseReportSnapshotBundleResult {
  status: InvestmentReportRequestState;
  bundle: ReportSnapshotBundle | null;
  error: string | null;
  reload: () => void;
}

export function useReportSnapshotBundle(
  reportUuid: string | undefined,
): UseReportSnapshotBundleResult {
  const [status, setStatus] =
    useState<InvestmentReportRequestState>("loading");
  const [bundle, setBundle] = useState<ReportSnapshotBundle | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!reportUuid) {
      setStatus("error");
      setError("report_uuid is required");
      return;
    }
    const controller = new AbortController();
    setStatus("loading");
    setError(null);

    fetchReportSnapshotBundle(reportUuid, controller.signal)
      .then((response) => {
        setBundle(response);
        setStatus("ready");
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
        setStatus("error");
      });

    return () => controller.abort();
  }, [reportUuid, tick]);

  return {
    status,
    bundle,
    error,
    reload: () => setTick((value) => value + 1),
  };
}
```

- [ ] **Step 2: Type-check**

Run: `cd frontend/invest && npm run typecheck` (or `npx tsc --noEmit`).
Expected: No type errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/hooks/useReportSnapshotBundle.ts
git commit -m "feat(rob-275): add useReportSnapshotBundle eager fetch hook"
```

---

## Task 9: Frontend hook — `useSnapshotPayload` (lazy, on demand)

**Files:**
- Create: `frontend/invest/src/hooks/useSnapshotPayload.ts`

- [ ] **Step 1: Implement the lazy hook**

Create `frontend/invest/src/hooks/useSnapshotPayload.ts`:

```typescript
// ROB-275 — Lazy fetcher for a single snapshot's payload.
//
// Does NOT issue a request on mount: callers pass ``snapshotUuid``
// only when a row is clicked. Re-fetches whenever ``snapshotUuid``
// changes; aborts on unmount or change.

import { useEffect, useState } from "react";

import { fetchReportSnapshotDetail } from "../api/investmentReports";
import type {
  InvestmentReportRequestState,
  ReportSnapshotDetail,
} from "../types/investmentReports";

interface UseSnapshotPayloadResult {
  status: InvestmentReportRequestState | "idle";
  detail: ReportSnapshotDetail | null;
  error: string | null;
}

export function useSnapshotPayload(
  reportUuid: string | undefined,
  snapshotUuid: string | null,
): UseSnapshotPayloadResult {
  const [status, setStatus] = useState<
    InvestmentReportRequestState | "idle"
  >("idle");
  const [detail, setDetail] = useState<ReportSnapshotDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!reportUuid || !snapshotUuid) {
      setStatus("idle");
      setDetail(null);
      setError(null);
      return;
    }
    const controller = new AbortController();
    setStatus("loading");
    setError(null);
    setDetail(null);

    fetchReportSnapshotDetail(reportUuid, snapshotUuid, controller.signal)
      .then((response) => {
        setDetail(response);
        setStatus("ready");
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
        setStatus("error");
      });

    return () => controller.abort();
  }, [reportUuid, snapshotUuid]);

  return { status, detail, error };
}
```

- [ ] **Step 2: Type-check**

Run: `cd frontend/invest && npx tsc --noEmit`.
Expected: No type errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/hooks/useSnapshotPayload.ts
git commit -m "feat(rob-275): add useSnapshotPayload lazy fetch hook

Triggered only when the user clicks a snapshot row; idle (no network
call) until a snapshotUuid is provided. Keeps the report detail initial
render off the heavy payload path."
```

---

## Task 10: Frontend component — `SnapshotPayloadDrawer`

**Files:**
- Create: `frontend/invest/src/components/investment-reports/SnapshotPayloadDrawer.tsx`

- [ ] **Step 1: Implement the drawer**

Create `frontend/invest/src/components/investment-reports/SnapshotPayloadDrawer.tsx`:

```typescript
// ROB-275 — Snapshot payload viewer rendered next to the evidence row.
//
// Receives the already-loaded ReportSnapshotDetail; rendering is pure.
// The fetch is handled by useSnapshotPayload in the parent panel so this
// component never knows about the network.

import type { JSX } from "react";

import type { ReportSnapshotDetail } from "../../types/investmentReports";

const ROLE_LABELS: Record<string, string> = {
  required: "필수",
  optional: "선택",
  fallback: "대체",
  conflict_evidence: "충돌 증거",
};

const FRESHNESS_LABELS: Record<string, string> = {
  fresh: "신선",
  soft_stale: "일부 지연",
  partial: "부분",
  hard_stale: "오래됨",
  unavailable: "확인 불가",
};

export interface SnapshotPayloadDrawerProps {
  status: "idle" | "loading" | "ready" | "error";
  detail: ReportSnapshotDetail | null;
  error: string | null;
  onClose: () => void;
}

export function SnapshotPayloadDrawer({
  status,
  detail,
  error,
  onClose,
}: SnapshotPayloadDrawerProps): JSX.Element {
  return (
    <div
      data-testid="snapshot-payload-drawer"
      style={{
        marginTop: 8,
        padding: 12,
        border: "1px solid var(--border)",
        borderRadius: 10,
        background: "var(--surface-2)",
        display: "grid",
        gap: 10,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          gap: 10,
        }}
      >
        <strong style={{ fontSize: 14 }}>스냅샷 페이로드</strong>
        <button
          type="button"
          onClick={onClose}
          style={{
            padding: "4px 10px",
            borderRadius: 8,
            border: "1px solid var(--border)",
            background: "transparent",
            color: "var(--fg-2)",
            cursor: "pointer",
            fontFamily: "inherit",
          }}
        >
          닫기
        </button>
      </div>

      {status === "loading" ? (
        <div style={{ color: "var(--fg-3)", fontSize: 13 }}>
          페이로드 불러오는 중…
        </div>
      ) : null}

      {status === "error" ? (
        <div style={{ color: "var(--danger)", fontSize: 13 }}>
          페이로드를 불러오지 못했습니다.{error ? ` (${error})` : ""}
        </div>
      ) : null}

      {status === "ready" && detail != null ? (
        <>
          <dl
            style={{
              display: "grid",
              gridTemplateColumns: "minmax(80px, max-content) 1fr",
              gap: "4px 12px",
              margin: 0,
              fontSize: 12,
              color: "var(--fg-3)",
            }}
          >
            <dt>역할</dt>
            <dd style={{ margin: 0 }}>{ROLE_LABELS[detail.role] ?? detail.role}</dd>
            <dt>종류</dt>
            <dd style={{ margin: 0 }}>{detail.snapshotKind}</dd>
            <dt>소스</dt>
            <dd style={{ margin: 0 }}>{detail.sourceKind}</dd>
            <dt>신선도</dt>
            <dd style={{ margin: 0 }}>
              {FRESHNESS_LABELS[detail.freshnessStatus] ?? detail.freshnessStatus}
            </dd>
            <dt>as_of</dt>
            <dd style={{ margin: 0 }}>
              {new Date(detail.asOf).toLocaleString("ko-KR")}
            </dd>
            {detail.sourceUri ? (
              <>
                <dt>출처 URI</dt>
                <dd style={{ margin: 0, wordBreak: "break-all" }}>
                  {detail.sourceUri}
                </dd>
              </>
            ) : null}
          </dl>
          <pre
            data-testid="snapshot-payload-json"
            style={{
              margin: 0,
              maxHeight: 320,
              overflow: "auto",
              fontFamily: "var(--mono, monospace)",
              fontSize: 12,
              background: "var(--surface-1)",
              padding: 10,
              borderRadius: 8,
              border: "1px solid var(--border)",
              color: "var(--fg-2)",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {JSON.stringify(detail.payloadJson, null, 2)}
          </pre>
        </>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 2: Type-check**

Run: `cd frontend/invest && npx tsc --noEmit`.
Expected: No type errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/components/investment-reports/SnapshotPayloadDrawer.tsx
git commit -m "feat(rob-275): add SnapshotPayloadDrawer for evidence viewer"
```

---

## Task 11: Frontend component — `SnapshotEvidenceRow`

**Files:**
- Create: `frontend/invest/src/components/investment-reports/SnapshotEvidenceRow.tsx`

- [ ] **Step 1: Implement the row**

Create `frontend/invest/src/components/investment-reports/SnapshotEvidenceRow.tsx`:

```typescript
// ROB-275 — One row in the report's snapshot evidence list.
//
// Pure render — clicking the row notifies the parent (which decides
// whether to mount a drawer / fetch the payload). The row itself never
// triggers a network call.

import type { JSX } from "react";

import type {
  ReportSnapshotBundleItem,
  SnapshotFreshness,
} from "../../types/investmentReports";

const FRESHNESS_LABELS: Record<SnapshotFreshness, string> = {
  fresh: "신선",
  soft_stale: "일부 지연",
  partial: "부분",
  hard_stale: "오래됨",
  unavailable: "확인 불가",
};

export interface SnapshotEvidenceRowProps {
  item: ReportSnapshotBundleItem;
  selected: boolean;
  onSelect: (snapshotUuid: string) => void;
}

export function SnapshotEvidenceRow({
  item,
  selected,
  onSelect,
}: SnapshotEvidenceRowProps): JSX.Element {
  const sizeLabel =
    item.payloadSizeBytes == null
      ? null
      : item.payloadSizeBytes < 1024
        ? `${item.payloadSizeBytes} B`
        : `${(item.payloadSizeBytes / 1024).toFixed(1)} KB`;

  return (
    <button
      type="button"
      data-testid={`snapshot-evidence-row-${item.snapshotUuid}`}
      onClick={() => onSelect(item.snapshotUuid)}
      aria-pressed={selected}
      style={{
        textAlign: "left",
        display: "grid",
        gap: 4,
        padding: 10,
        border: `1px solid ${selected ? "var(--fg-2)" : "var(--border)"}`,
        borderRadius: 10,
        background: selected ? "var(--surface-2)" : "transparent",
        color: "var(--fg-1)",
        cursor: "pointer",
        fontFamily: "inherit",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: 10,
          alignItems: "baseline",
          flexWrap: "wrap",
        }}
      >
        <span style={{ fontSize: 14, fontWeight: 700 }}>
          {item.snapshotKind}
          {item.symbol ? ` · ${item.symbol}` : ""}
        </span>
        <span style={{ fontSize: 12, color: "var(--fg-3)" }}>
          {FRESHNESS_LABELS[item.freshnessStatus] ?? item.freshnessStatus}
        </span>
      </div>
      <div
        style={{
          display: "flex",
          gap: 6,
          color: "var(--fg-3)",
          fontSize: 12,
          flexWrap: "wrap",
        }}
      >
        <span>{item.sourceKind}</span>
        <span>·</span>
        <span>{new Date(item.asOf).toLocaleString("ko-KR")}</span>
        {sizeLabel ? (
          <>
            <span>·</span>
            <span>{sizeLabel}</span>
          </>
        ) : null}
        {item.sourceUri ? (
          <>
            <span>·</span>
            <span style={{ wordBreak: "break-all" }}>{item.sourceUri}</span>
          </>
        ) : null}
      </div>
    </button>
  );
}
```

- [ ] **Step 2: Type-check**

Run: `cd frontend/invest && npx tsc --noEmit`.
Expected: No type errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/components/investment-reports/SnapshotEvidenceRow.tsx
git commit -m "feat(rob-275): add SnapshotEvidenceRow click target"
```

---

## Task 12: Frontend component — `ReportSnapshotEvidencePanel`

**Files:**
- Create: `frontend/invest/src/components/investment-reports/ReportSnapshotEvidencePanel.tsx`

- [ ] **Step 1: Implement the panel**

Create `frontend/invest/src/components/investment-reports/ReportSnapshotEvidencePanel.tsx`:

```typescript
// ROB-275 — Report snapshot evidence panel.
//
// Mounted under the existing report header on /invest/reports/:reportUuid.
// Renders the bundle summary + role-grouped item rows + separate
// `unavailable_sources` and `source_conflicts` sections (which are
// *report* observations, not bundle-linked snapshots — visually distinct).
//
// Click a row → drawer fetches that snapshot's payload via
// useSnapshotPayload. Initial render does NOT trigger payload fetches.

import { useState, type JSX } from "react";

import { Card } from "../../ds";
import { useReportSnapshotBundle } from "../../hooks/useReportSnapshotBundle";
import { useSnapshotPayload } from "../../hooks/useSnapshotPayload";
import type {
  BundleItemRole,
  ReportSnapshotBundleItem,
} from "../../types/investmentReports";
import { SnapshotEvidenceRow } from "./SnapshotEvidenceRow";
import { SnapshotPayloadDrawer } from "./SnapshotPayloadDrawer";

const ROLE_LABELS: Record<BundleItemRole, string> = {
  required: "필수",
  optional: "선택",
  fallback: "대체",
  conflict_evidence: "충돌 증거",
};

const ROLE_ORDER: readonly BundleItemRole[] = [
  "required",
  "optional",
  "fallback",
  "conflict_evidence",
];

function groupByRole(items: ReportSnapshotBundleItem[]) {
  const buckets: Record<BundleItemRole, ReportSnapshotBundleItem[]> = {
    required: [],
    optional: [],
    fallback: [],
    conflict_evidence: [],
  };
  for (const item of items) {
    buckets[item.role].push(item);
  }
  return buckets;
}

export interface ReportSnapshotEvidencePanelProps {
  reportUuid: string;
}

export function ReportSnapshotEvidencePanel({
  reportUuid,
}: ReportSnapshotEvidencePanelProps): JSX.Element | null {
  const { status, bundle, error, reload } = useReportSnapshotBundle(reportUuid);
  const [selectedSnapshotUuid, setSelectedSnapshotUuid] =
    useState<string | null>(null);
  const payload = useSnapshotPayload(reportUuid, selectedSnapshotUuid);

  if (status === "loading") {
    return (
      <Card>
        <div
          data-testid="snapshot-evidence-panel-loading"
          style={{ color: "var(--fg-3)", fontSize: 13 }}
        >
          스냅샷 근거를 불러오는 중…
        </div>
      </Card>
    );
  }
  if (status === "error" || !bundle) {
    return (
      <Card>
        <div
          data-testid="snapshot-evidence-panel-error"
          style={{
            display: "flex",
            gap: 10,
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <span style={{ color: "var(--danger)", fontSize: 13 }}>
            스냅샷 근거를 불러오지 못했습니다.{error ? ` (${error})` : ""}
          </span>
          <button
            type="button"
            onClick={reload}
            style={{
              padding: "4px 10px",
              borderRadius: 8,
              border: "1px solid var(--border)",
              background: "transparent",
              color: "var(--fg-2)",
              cursor: "pointer",
              fontFamily: "inherit",
            }}
          >
            재시도
          </button>
        </div>
      </Card>
    );
  }

  if (bundle.legacyNoSnapshot) {
    return (
      <Card>
        <div
          data-testid="snapshot-evidence-panel-legacy"
          style={{ color: "var(--fg-3)", fontSize: 13 }}
        >
          이 리포트는 스냅샷 번들이 연결되어 있지 않습니다 (legacy).
        </div>
      </Card>
    );
  }

  const buckets = groupByRole(bundle.items);
  const summary = bundle.bundle;

  return (
    <Card>
      <div
        data-testid="snapshot-evidence-panel"
        style={{ display: "grid", gap: 12 }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
            gap: 10,
            flexWrap: "wrap",
          }}
        >
          <h2 style={{ margin: 0, fontSize: 18 }}>스냅샷 근거</h2>
          {summary ? (
            <span style={{ color: "var(--fg-3)", fontSize: 12 }}>
              번들 {summary.status} · policy {summary.policyVersion} ·{" "}
              {new Date(summary.asOf).toLocaleString("ko-KR")}
            </span>
          ) : null}
        </div>

        {summary ? (
          <div
            style={{
              fontSize: 12,
              color: "var(--fg-3)",
              wordBreak: "break-all",
            }}
          >
            bundle_uuid: {summary.bundleUuid}
          </div>
        ) : null}

        {ROLE_ORDER.map((role) =>
          buckets[role].length > 0 ? (
            <section
              key={role}
              data-testid={`snapshot-evidence-role-${role}`}
              style={{ display: "grid", gap: 8 }}
            >
              <h3 style={{ margin: 0, fontSize: 14 }}>
                {ROLE_LABELS[role]} ({buckets[role].length})
              </h3>
              {buckets[role].map((item) => (
                <div key={item.snapshotUuid} style={{ display: "grid", gap: 0 }}>
                  <SnapshotEvidenceRow
                    item={item}
                    selected={selectedSnapshotUuid === item.snapshotUuid}
                    onSelect={(snapshotUuid) =>
                      setSelectedSnapshotUuid((prev) =>
                        prev === snapshotUuid ? null : snapshotUuid,
                      )
                    }
                  />
                  {selectedSnapshotUuid === item.snapshotUuid ? (
                    <SnapshotPayloadDrawer
                      status={payload.status}
                      detail={payload.detail}
                      error={payload.error}
                      onClose={() => setSelectedSnapshotUuid(null)}
                    />
                  ) : null}
                </div>
              ))}
            </section>
          ) : null,
        )}

        {/* unavailable_sources and source_conflicts are *report*
            observations — NOT bundle-linked snapshots. Render them in
            distinct sections so they are not mistaken for evidence rows. */}
        {bundle.unavailableSources &&
        Object.keys(bundle.unavailableSources).length > 0 ? (
          <section
            data-testid="snapshot-evidence-unavailable-sources"
            style={{
              display: "grid",
              gap: 6,
              padding: 10,
              borderRadius: 10,
              border: "1px solid var(--warn, var(--border))",
              background: "var(--surface-2)",
            }}
          >
            <h3 style={{ margin: 0, fontSize: 14, color: "var(--warn)" }}>
              확인 불가 소스
            </h3>
            <pre
              style={{
                margin: 0,
                fontFamily: "var(--mono, monospace)",
                fontSize: 12,
                color: "var(--fg-2)",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}
            >
              {JSON.stringify(bundle.unavailableSources, null, 2)}
            </pre>
          </section>
        ) : null}

        {bundle.sourceConflicts &&
        Object.keys(bundle.sourceConflicts).length > 0 ? (
          <section
            data-testid="snapshot-evidence-source-conflicts"
            style={{
              display: "grid",
              gap: 6,
              padding: 10,
              borderRadius: 10,
              border: "1px solid var(--danger, var(--border))",
              background: "var(--surface-2)",
            }}
          >
            <h3 style={{ margin: 0, fontSize: 14, color: "var(--danger)" }}>
              소스 충돌
            </h3>
            <pre
              style={{
                margin: 0,
                fontFamily: "var(--mono, monospace)",
                fontSize: 12,
                color: "var(--fg-2)",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}
            >
              {JSON.stringify(bundle.sourceConflicts, null, 2)}
            </pre>
          </section>
        ) : null}
      </div>
    </Card>
  );
}
```

- [ ] **Step 2: Type-check**

Run: `cd frontend/invest && npx tsc --noEmit`.
Expected: No type errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/components/investment-reports/ReportSnapshotEvidencePanel.tsx
git commit -m "feat(rob-275): add ReportSnapshotEvidencePanel

Groups bundle-linked snapshots by role (required / optional / fallback /
conflict_evidence) and surfaces unavailable_sources + source_conflicts in
separate sections so the operator can tell observed-but-not-used sources
from actual bundle membership. Row click opens an inline drawer that
lazy-loads the payload via useSnapshotPayload."
```

---

## Task 13: Mount the panel inside the report bundle content

**Files:**
- Modify: `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx`

- [ ] **Step 1: Add import and render the panel**

In `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx`, add the import near the existing component imports:

```typescript
import { ReportSnapshotEvidencePanel } from "./ReportSnapshotEvidencePanel";
```

Then, in the main `InvestmentReportBundleContent` return JSX, mount the panel directly after `<ReportHeader … />`. Replace:

```tsx
      <ReportHeader
        title={bundle.report.title}
        market={bundle.report.market}
        marketSession={bundle.report.marketSession}
        accountScope={bundle.report.accountScope}
        executionMode={bundle.report.executionMode}
        status={bundle.report.status}
        summary={bundle.report.summary}
        riskSummary={bundle.report.riskSummary}
        thesisText={bundle.report.thesisText}
        noActionNote={bundle.report.noActionNote}
        createdAt={bundle.report.createdAt}
        freshnessSummary={bundle.report.snapshotFreshnessSummary}
      />

      {(
        ["action", "watch", "risk"] as const
      ).map((kind) =>
```

with:

```tsx
      <ReportHeader
        title={bundle.report.title}
        market={bundle.report.market}
        marketSession={bundle.report.marketSession}
        accountScope={bundle.report.accountScope}
        executionMode={bundle.report.executionMode}
        status={bundle.report.status}
        summary={bundle.report.summary}
        riskSummary={bundle.report.riskSummary}
        thesisText={bundle.report.thesisText}
        noActionNote={bundle.report.noActionNote}
        createdAt={bundle.report.createdAt}
        freshnessSummary={bundle.report.snapshotFreshnessSummary}
      />

      <ReportSnapshotEvidencePanel reportUuid={bundle.report.reportUuid} />

      {(
        ["action", "watch", "risk"] as const
      ).map((kind) =>
```

- [ ] **Step 2: Type-check**

Run: `cd frontend/invest && npx tsc --noEmit`.
Expected: No type errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx
git commit -m "feat(rob-275): mount ReportSnapshotEvidencePanel on report detail"
```

---

## Task 14: Frontend test — role grouping + section separation + lazy-load guarantee

**Files:**
- Create: `frontend/invest/src/__tests__/ReportSnapshotEvidencePanel.test.tsx`

- [ ] **Step 1: Write the test**

Create `frontend/invest/src/__tests__/ReportSnapshotEvidencePanel.test.tsx`:

```typescript
// ROB-275 — ReportSnapshotEvidencePanel tests.
//
// Critical regression guard: mounting the panel must NOT trigger any
// payload-detail fetch. Only the bundle-list endpoint is fetched eagerly;
// detail fetches happen after a row click.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ReportSnapshotEvidencePanel } from "../components/investment-reports/ReportSnapshotEvidencePanel";

const originalFetch = global.fetch;

interface FetchResponseInit {
  status?: number;
  ok?: boolean;
  json: () => Promise<unknown>;
}

function makeResponse(payload: unknown, status: number = 200): FetchResponseInit {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => payload,
  };
}

beforeEach(() => {
  global.fetch = vi.fn();
});

afterEach(() => {
  global.fetch = originalFetch;
});

function mockBundleAndDetail() {
  const bundleResponse = {
    bundle: {
      bundle_uuid: "bundle-1",
      purpose: "rob275_smoke",
      policy_version: "intraday_action_report_v1",
      status: "partial",
      as_of: "2026-05-20T11:00:00Z",
      coverage_summary: {},
      freshness_summary: { overall: "partial" },
      created_at: "2026-05-20T11:00:00Z",
    },
    items: [
      {
        snapshot_uuid: "snap-required",
        role: "required",
        snapshot_kind: "portfolio",
        source_kind: "manual",
        market: "kr",
        symbol: null,
        account_scope: "kis_live",
        freshness_status: "fresh",
        as_of: "2026-05-20T11:00:00Z",
        valid_until: null,
        source_table: null,
        source_id: null,
        source_uri: null,
        payload_size_bytes: 128,
      },
      {
        snapshot_uuid: "snap-optional",
        role: "optional",
        snapshot_kind: "market",
        source_kind: "domain_ref",
        market: "kr",
        symbol: null,
        account_scope: null,
        freshness_status: "soft_stale",
        as_of: "2026-05-20T10:00:00Z",
        valid_until: null,
        source_table: "market_quote_snapshots",
        source_id: 42,
        source_uri: "market_quote_snapshots:abc",
        payload_size_bytes: 4096,
      },
    ],
    unavailable_sources: { naver_remote_debug: "blocked" },
    source_conflicts: null,
    legacy_no_snapshot: false,
  };
  const detailResponse = {
    snapshot_uuid: "snap-required",
    role: "required",
    snapshot_kind: "portfolio",
    source_kind: "manual",
    market: "kr",
    symbol: null,
    account_scope: "kis_live",
    source_table: null,
    source_id: null,
    source_uri: null,
    freshness_status: "fresh",
    as_of: "2026-05-20T11:00:00Z",
    valid_until: null,
    source_timestamps_json: {},
    coverage_json: {},
    errors_json: {},
    payload_json: { cash_krw: 1_000_000 },
  };
  (global.fetch as ReturnType<typeof vi.fn>).mockImplementation(
    (url: RequestInfo | URL) => {
      const u = typeof url === "string" ? url : url.toString();
      if (u.includes("/snapshot-bundle")) {
        return Promise.resolve(makeResponse(bundleResponse));
      }
      if (u.includes("/snapshots/")) {
        return Promise.resolve(makeResponse(detailResponse));
      }
      return Promise.resolve(makeResponse({}, 404));
    },
  );
}

describe("ReportSnapshotEvidencePanel", () => {
  it("mounts without triggering any snapshot detail fetch", async () => {
    mockBundleAndDetail();
    render(<ReportSnapshotEvidencePanel reportUuid="uuid-1" />);

    await waitFor(() =>
      expect(screen.getByTestId("snapshot-evidence-panel")).toBeInTheDocument(),
    );

    const fetchMock = global.fetch as ReturnType<typeof vi.fn>;
    const detailCalls = fetchMock.mock.calls.filter((args) => {
      const u = typeof args[0] === "string" ? args[0] : String(args[0]);
      return u.includes("/snapshots/");
    });
    expect(detailCalls).toHaveLength(0);
  });

  it("groups items by role and renders unavailable_sources in a separate section", async () => {
    mockBundleAndDetail();
    render(<ReportSnapshotEvidencePanel reportUuid="uuid-1" />);

    await waitFor(() =>
      expect(
        screen.getByTestId("snapshot-evidence-role-required"),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByTestId("snapshot-evidence-role-optional"),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId("snapshot-evidence-role-fallback"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByTestId("snapshot-evidence-unavailable-sources"),
    ).toBeInTheDocument();
    // source_conflicts was null → section is not rendered.
    expect(
      screen.queryByTestId("snapshot-evidence-source-conflicts"),
    ).not.toBeInTheDocument();
  });

  it("fetches the detail payload on row click and renders the drawer", async () => {
    mockBundleAndDetail();
    render(<ReportSnapshotEvidencePanel reportUuid="uuid-1" />);

    const row = await screen.findByTestId(
      "snapshot-evidence-row-snap-required",
    );
    await act(async () => {
      fireEvent.click(row);
    });

    await waitFor(() =>
      expect(
        screen.getByTestId("snapshot-payload-drawer"),
      ).toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(
        screen.getByTestId("snapshot-payload-json"),
      ).toHaveTextContent(/cash_krw/),
    );
  });

  it("renders a legacy message when the report has no snapshot bundle", async () => {
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      makeResponse({
        bundle: null,
        items: [],
        unavailable_sources: null,
        source_conflicts: null,
        legacy_no_snapshot: true,
      }),
    );

    render(<ReportSnapshotEvidencePanel reportUuid="legacy-uuid" />);

    await waitFor(() =>
      expect(
        screen.getByTestId("snapshot-evidence-panel-legacy"),
      ).toBeInTheDocument(),
    );
    // No payload fetch on legacy reports either.
    const fetchMock = global.fetch as ReturnType<typeof vi.fn>;
    const detailCalls = fetchMock.mock.calls.filter((args) => {
      const u = typeof args[0] === "string" ? args[0] : String(args[0]);
      return u.includes("/snapshots/");
    });
    expect(detailCalls).toHaveLength(0);
  });
});
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd frontend/invest && npx vitest run src/__tests__/ReportSnapshotEvidencePanel.test.tsx`
Expected: PASS for all four tests.

- [ ] **Step 3: Run the full frontend test suite to confirm no regression**

Run: `cd frontend/invest && npx vitest run`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add frontend/invest/src/__tests__/ReportSnapshotEvidencePanel.test.tsx
git commit -m "test(rob-275): cover role grouping + lazy-load + legacy panel paths

Critical regression guard: mounting the panel must NOT issue any
snapshots/{snapshot_uuid} fetch — only the bundle-list endpoint fires on
mount. Detail fetches are triggered only after a user clicks a row.
Also asserts unavailable_sources renders in a distinct section
(separate test-id) so it cannot be visually mistaken for a bundle-linked
snapshot row."
```

---

## Task 15: Smoke check the full path

**Files:**
- No code changes — this is a verification task.

- [ ] **Step 1: Run all backend tests touched by this work**

Run:
```bash
uv run pytest \
  tests/services/investment_snapshots/test_repository_reads.py \
  tests/test_investment_reports_schemas.py \
  tests/test_investment_reports_snapshot_evidence_service.py \
  tests/routers/test_investment_reports_snapshot_evidence_router.py \
  tests/test_investment_reports_router.py \
  tests/services/investment_snapshots/test_append_only.py \
  -v
```
Expected: All tests pass.

- [ ] **Step 2: Run lint / typecheck**

Run: `make lint` (Ruff + ty).
Expected: No new violations.

- [ ] **Step 3: Run frontend test + typecheck + build**

Run:
```bash
cd frontend/invest && npx tsc --noEmit && npx vitest run && npm run build
```
Expected: All commands succeed.

- [ ] **Step 4: Local smoke against the sample bundle**

If the dev server is up against a production-snapshot test DB containing report `b65efa46-5ed9-4ac4-a724-4d5e7060b92c`, exercise both endpoints. Otherwise, skip — the integration tests in Task 5 already cover the same paths.

```bash
# Bundle list endpoint — expect 200 with the 11 linked items + ~16KB total.
curl -sS -b cookies.txt \
  http://localhost:8000/invest/api/investment-reports/b65efa46-5ed9-4ac4-a724-4d5e7060b92c/snapshot-bundle | jq '.items | length'
# Expect: 11

# Detail endpoint with a member snapshot_uuid taken from the bundle list above — expect 200.
SNAP=$(curl -sS -b cookies.txt \
  http://localhost:8000/invest/api/investment-reports/b65efa46-5ed9-4ac4-a724-4d5e7060b92c/snapshot-bundle | jq -r '.items[0].snapshot_uuid')
curl -sS -b cookies.txt -o /dev/null -w '%{http_code}\n' \
  http://localhost:8000/invest/api/investment-reports/b65efa46-5ed9-4ac4-a724-4d5e7060b92c/snapshots/$SNAP
# Expect: 200

# Detail endpoint with a random non-member UUID — expect 404.
curl -sS -b cookies.txt -o /dev/null -w '%{http_code}\n' \
  http://localhost:8000/invest/api/investment-reports/b65efa46-5ed9-4ac4-a724-4d5e7060b92c/snapshots/00000000-0000-0000-0000-000000000000
# Expect: 404
```

- [ ] **Step 5: Open the report in the browser and click a row**

Navigate to `/invest/reports/b65efa46-5ed9-4ac4-a724-4d5e7060b92c`. Verify:
- Snapshot evidence panel renders under the report header.
- Bundle status / policy / 11 rows are visible, grouped by role.
- DevTools Network tab shows ONE call to `/snapshot-bundle` and ZERO calls to `/snapshots/{uuid}` on initial render.
- Clicking a row triggers exactly one `/snapshots/{uuid}` call and the drawer renders the JSON payload.
- `naver_remote_debug` / `toss_remote_debug` (or whatever is in `unavailable_sources`) appears in a distinct, visually separated `확인 불가 소스` section — NOT as a snapshot row.

- [ ] **Step 6: Push and open PR**

```bash
git push -u origin rob-275
gh pr create --title "feat(rob-275): /invest report snapshot evidence viewer" --body "$(cat <<'EOF'
## Summary
- Adds a read-only snapshot evidence panel to `/invest/reports/:reportUuid` listing the snapshots actually linked to the report's bundle, grouped by role.
- Row click lazy-loads the snapshot's stored payload via a new membership-checked detail endpoint — non-member snapshot UUIDs 404 even though `investment_snapshots` rows are globally reusable.
- `unavailable_sources` / `source_conflicts` render in distinct sections so Toss/Naver unavailability is never visually conflated with actually-used snapshots.
- No DB migration, no broker/order/watch mutation, no scheduler. Not gated by `INVESTMENT_SNAPSHOTS_MCP_ENABLED` — data presence on the report row is the gate.

## Test plan
- [x] Backend: repository reads, service membership/legacy paths, router 200/404 matrix
- [x] Frontend: API normalisation, role grouping, separated unavailable/conflict sections, mount-does-not-fetch-detail regression guard, lazy drawer fetch
- [x] Local smoke against report b65efa46-5ed9-4ac4-a724-4d5e7060b92c (bundle 648701c1-9ef2-4c40-ac8d-e5d8c30db32c, 11 items, ~16KB)
- [x] Verified initial render issues exactly one bundle fetch and zero detail fetches

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**1. Spec coverage:**
- Backend endpoint `GET /invest/api/investment-reports/{report_uuid}/snapshot-bundle` → Task 5.
- Backend detail endpoint with membership check → Task 5 (router), Task 4 (service).
- `legacy/no_snapshot` shape when `snapshot_bundle_uuid` is null → Tasks 3, 5.
- `unavailable_sources` / `source_conflicts` from report row, visually separated → Tasks 3, 12 (`-unavailable-sources`, `-source-conflicts` test-ids).
- Bundle list includes role / kind / source / freshness / symbol / as-of / payload_size_bytes → Tasks 2, 3.
- Detail returns full metadata + payload → Tasks 2, 4.
- Frontend snapshot evidence panel + row + drawer → Tasks 10–12.
- Frontend grouping by role with required / optional / fallback / conflict_evidence → Task 12.
- Initial report-detail load does NOT eagerly fetch all payloads → Tasks 9, 12, 14 (asserted in test).
- Lazy drawer / viewer with metadata + JSON → Tasks 9, 10.
- Backend tests cover bundle endpoint, no-bundle/legacy case, detail payload read, non-member 404 → Tasks 3, 4, 5.
- Frontend tests cover API normalisation, role grouping, unavailable/conflict sections, lazy-load → Tasks 7, 14.
- No broker/order/watch/order-intent mutation introduced → confirmed in non-goals + no model/migration changes.

**2. Placeholder scan:** No `TBD`, `TODO`, `implement later`, `similar to Task N`, or omitted code blocks. Every code step shows the exact code to write or the exact diff to make.

**3. Type consistency:**
- Service method `get_report_snapshot_bundle` is called consistently across Tasks 3, 5.
- Service method `get_report_snapshot_detail` is called consistently across Tasks 4, 5.
- Router function names `get_investment_report_snapshot_bundle` / `get_investment_report_snapshot_detail` are imported with the exact same names in the router test (Task 5).
- Schemas `ReportSnapshotBundleResponse`, `ReportSnapshotBundleSummaryView`, `ReportSnapshotBundleItemView`, `ReportSnapshotDetailResponse` are defined in Task 2 and used unchanged in Tasks 3, 4, 5.
- Frontend types `ReportSnapshotBundle`, `ReportSnapshotBundleItem`, `ReportSnapshotBundleSummary`, `ReportSnapshotDetail` are defined in Task 6 and used unchanged in Tasks 7–14.
- Hook names `useReportSnapshotBundle` / `useSnapshotPayload` are consistent across Tasks 8, 9, 12.
- Component names `ReportSnapshotEvidencePanel` / `SnapshotEvidenceRow` / `SnapshotPayloadDrawer` are consistent across Tasks 10–14.
- Test-ids `snapshot-evidence-panel`, `snapshot-evidence-row-{uuid}`, `snapshot-evidence-role-{role}`, `snapshot-evidence-unavailable-sources`, `snapshot-evidence-source-conflicts`, `snapshot-payload-drawer`, `snapshot-payload-json`, `snapshot-evidence-panel-legacy` are consistent between components (Tasks 10–12) and tests (Task 14).
