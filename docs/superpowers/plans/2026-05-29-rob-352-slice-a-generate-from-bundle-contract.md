# ROB-352 Slice A — generate_from_bundle contract / validation / idempotency

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `investment_report_generate_from_bundle` safe and predictable enough that Hermes/Claude Code can generate a US/KIS report without trial-and-error — fix the account_scope contract, thread `market_session`, auto-resolve `user_id`, give caller-friendly item validation, and make deterministic regeneration default to **reuse** with an explicit **overwrite** path (no stored/response mismatch).

**Architecture:** Changes are layered. (1) The MCP handler (`investment_reports_handlers.py`) gains pre-validation that returns structured `{success: False, ...}` errors *before* it builds the request — covering unsupported account scopes and malformed items — plus default `user_id` resolution and a new `market_session` param. (2) `ReportGenerationRequest`/`ReportGenerationResponse` gain `overwrite_existing`/`overwrite_reason`/`reused_existing`. (3) `SnapshotBackedReportGenerator.generate` short-circuits on an existing idempotency key (default path) and builds the response **from the stored row**, never from a freshly computed unstored payload; the overwrite path recomputes and transactionally replaces. (4) `InvestmentReportIngestionService.ingest` + repository gain an in-place overwrite (update report + replace items, stable `report_uuid`).

**Tech Stack:** Python 3.13, Pydantic v2, SQLAlchemy async, pytest/pytest-asyncio against a real PostgreSQL test DB (`session` fixture from `tests/_investment_reports_helpers.py`). `uv run pytest ...`.

**Decision (2026-05-29, 광현님):** Default = reuse (return the stored report; the stored-row/response mismatch is a bug to fix). Overwrite only via explicit `overwrite_existing=true` + `overwrite_reason`, which transactionally replaces report+items. The `report_type`/`created_by_profile`-mutation workaround to force a new row is forbidden — not implemented, not documented.

**Out of scope (Slice B/C):** populating `evidence_snapshot`/`market_snapshot`/`portfolio_snapshot`, per-item `cited_snapshot_uuids`, `hermes-smoke-*` prior-report filtering, candidate_universe quality. No broker/order/watch/order-intent/trade-journal mutation. No scheduler. No production smoke without separate operator approval.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `app/services/action_report/snapshot_backed/request.py` | generator request/response envelopes | Add `overwrite_existing`, `overwrite_reason` (request); `reused_existing` (response) |
| `app/services/investment_reports/repository.py` | DAO over investment_* tables | Add `delete_items_for_report`, `update_report` |
| `app/services/investment_reports/ingestion.py` | idempotent ingest | Add `overwrite`/`overwrite_reason` to `ingest()`; in-place replace path |
| `app/services/action_report/snapshot_backed/generator.py` | end-to-end generation | Early reuse short-circuit + `_response_from_stored`; thread `market_session` into ingest request; pass `overwrite` to `ingest()`; inject reports repo |
| `app/mcp_server/tooling/investment_reports_handlers.py` | MCP surface | Add `market_session`/`overwrite_existing`/`overwrite_reason` params; account_scope pre-validation; per-item pre-validation; default user_id resolution; tool description doc |
| `tests/test_investment_reports_ingestion.py` | ingest tests | overwrite path tests |
| `tests/services/action_report/snapshot_backed/test_generator.py` | generator tests | reuse-from-stored, overwrite, market_session, pair validation |
| `tests/mcp_server/test_investment_report_generate_from_bundle_handler.py` | NEW handler tests | account_scope reject, item pre-validation, user_id default, pair drift-guard |

---

## Task A1: `overwrite_existing` / `reused_existing` schema fields

**Files:**
- Modify: `app/services/action_report/snapshot_backed/request.py`
- Test: `tests/services/action_report/snapshot_backed/test_request_overwrite_fields.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/services/action_report/snapshot_backed/test_request_overwrite_fields.py`:

```python
"""ROB-352 Slice A — overwrite/reused contract fields on the generator envelopes."""

from __future__ import annotations

from app.services.action_report.snapshot_backed.request import (
    ReportGenerationRequest,
    ReportGenerationResponse,
)


def _req(**overrides):
    base = {
        "market": "kr",
        "account_scope": "kis_live",
        "created_by_profile": "t",
        "title": "t",
        "summary": "s",
        "kst_date": "2026-05-29",
    }
    base.update(overrides)
    return ReportGenerationRequest.model_validate(base)


def test_overwrite_defaults_to_false():
    req = _req()
    assert req.overwrite_existing is False
    assert req.overwrite_reason is None


def test_overwrite_can_be_set_with_reason():
    req = _req(overwrite_existing=True, overwrite_reason="restated US session")
    assert req.overwrite_existing is True
    assert req.overwrite_reason == "restated US session"


def test_response_reused_existing_defaults_false():
    import uuid

    resp = ReportGenerationResponse(
        report_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=uuid.uuid4(),
        snapshot_policy_version="p",
        snapshot_coverage_summary={},
        snapshot_freshness_summary={},
        source_conflicts={},
        unavailable_sources={},
        items_count=0,
        warnings=[],
        bundle_status="complete",
        bundle_reused=False,
        stale_gate={},
    )
    assert resp.reused_existing is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_request_overwrite_fields.py -v`
Expected: FAIL — `ValidationError: extra fields not permitted` for `overwrite_existing` (model_config has `extra="forbid"`), and `reused_existing` AttributeError.

- [ ] **Step 3: Add the fields**

In `app/services/action_report/snapshot_backed/request.py`, in `ReportGenerationRequest` (after the `user_id` field, before the `@field_validator("auto_compose")`):

```python
    # ROB-352 — deterministic regeneration semantics. Default is REUSE: when a
    # report already exists for this deterministic idempotency key, the stored
    # row is returned unchanged (the generator never emits a freshly-computed,
    # unstored payload). Set ``overwrite_existing=True`` (with a reason) to
    # transactionally replace the stored report + items in place. Mutating
    # report_type/created_by_profile to force a new row is NOT supported.
    overwrite_existing: bool = False
    overwrite_reason: str | None = None
```

In `ReportGenerationResponse` (after `stale_gate`, before `why_no_action`):

```python
    # ROB-352 — True when the response reflects an existing stored report that
    # was returned unchanged (default reuse path). When True, callers should
    # pass overwrite_existing=True + overwrite_reason to regenerate.
    reused_existing: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_request_overwrite_fields.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/snapshot_backed/request.py tests/services/action_report/snapshot_backed/test_request_overwrite_fields.py
git commit -m "feat(ROB-352): add overwrite_existing/reused_existing to generator envelopes"
```

---

## Task A2: Repository overwrite primitives

**Files:**
- Modify: `app/services/investment_reports/repository.py`
- Test: `tests/test_investment_reports_repository_overwrite.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_investment_reports_repository_overwrite.py`:

```python
"""ROB-352 Slice A — repository overwrite primitives (delete items + update report)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.investment_reports.repository import InvestmentReportsRepository


@pytest.mark.asyncio
async def test_delete_items_for_report_removes_only_that_reports_items(
    session: AsyncSession,
) -> None:
    repo = InvestmentReportsRepository(session)
    report = await repo.insert_report(
        idempotency_key="rep:a",
        report_type="t",
        market="kr",
        market_session=None,
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile="t",
        title="t",
        summary="s",
        status="draft",
        report_metadata={},
    )
    await repo.insert_item(
        report_id=report.id,
        idempotency_key="it:a",
        item_kind="action",
        symbol="005930",
        side="buy",
        intent="buy_review",
        target_kind="asset",
        priority=0,
        rationale="r",
        evidence_snapshot={},
    )
    assert len(await repo.list_items_for_report(report.id)) == 1

    await repo.delete_items_for_report(report.id)
    assert await repo.list_items_for_report(report.id) == []


@pytest.mark.asyncio
async def test_update_report_changes_scalar_and_jsonb_fields(
    session: AsyncSession,
) -> None:
    repo = InvestmentReportsRepository(session)
    report = await repo.insert_report(
        idempotency_key="rep:b",
        report_type="t",
        market="kr",
        market_session=None,
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile="t",
        title="old",
        summary="old",
        status="draft",
        report_metadata={"k": 1},
    )
    await repo.update_report(
        report.id,
        title="new",
        summary="new-summary",
        report_metadata={"k": 2, "overwrite_reason": "redo"},
    )
    refreshed = await repo.get_report_by_id(report.id)
    assert refreshed is not None
    assert refreshed.title == "new"
    assert refreshed.summary == "new-summary"
    assert refreshed.report_metadata == {"k": 2, "overwrite_reason": "redo"}
    # report_uuid stays stable across an in-place update.
    assert refreshed.report_uuid == report.report_uuid
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_investment_reports_repository_overwrite.py -v`
Expected: FAIL — `AttributeError: 'InvestmentReportsRepository' object has no attribute 'delete_items_for_report'`.

- [ ] **Step 3: Add the repository methods**

In `app/services/investment_reports/repository.py`, inside the `# Items` section (after `update_item_status`, before the `# Decisions` section):

```python
    async def delete_items_for_report(self, report_id: int) -> None:
        """ROB-352 — remove every item of one report (overwrite path).

        Used only by the ingestion service's explicit-overwrite branch. The
        caller owns the transaction; this flushes but never commits.
        """
        await self._session.execute(
            sa.delete(InvestmentReportItem).where(
                InvestmentReportItem.report_id == report_id
            )
        )
        await self._session.flush()
```

In the `# Reports` section (after `latest_report`, before `_apply_report_filters`):

```python
    async def update_report(self, report_id: int, **fields: Any) -> None:
        """ROB-352 — update an existing report row in place (overwrite path).

        Keeps ``report_uuid`` / ``idempotency_key`` stable. The caller owns the
        transaction; this flushes but never commits.
        """
        if not fields:
            return
        await self._session.execute(
            sa.update(InvestmentReport)
            .where(InvestmentReport.id == report_id)
            .values(**fields)
        )
        await self._session.flush()
```

(`sa`, `InvestmentReport`, `InvestmentReportItem`, and `Any` are already imported at the top of the file.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_investment_reports_repository_overwrite.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_reports/repository.py tests/test_investment_reports_repository_overwrite.py
git commit -m "feat(ROB-352): repository delete_items_for_report + update_report overwrite primitives"
```

---

## Task A3: Ingestion overwrite path (default reuse, explicit replace)

**Files:**
- Modify: `app/services/investment_reports/ingestion.py`
- Test: `tests/test_investment_reports_ingestion.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_investment_reports_ingestion.py`:

```python
@pytest.mark.asyncio
async def test_overwrite_replaces_items_and_keeps_uuid(session: AsyncSession) -> None:
    """ROB-352 — overwrite=True replaces items in place, report_uuid stable."""
    service = InvestmentReportIngestionService(session)
    first = await service.ingest(
        _base_request(title="v1", items=[_action_item("a1")])
    )
    repo = InvestmentReportsRepository(session)
    assert {i.client_item_key_unused for i in []} == set()  # no-op guard
    first_items = await repo.list_items_for_report(first.id)
    assert len(first_items) == 1

    second = await service.ingest(
        _base_request(
            title="v2",
            items=[_action_item("a1"), _action_item("a2", symbol="000660")],
        ),
        overwrite=True,
        overwrite_reason="restated",
    )
    assert second.report_uuid == first.report_uuid
    assert second.id == first.id
    assert second.title == "v2"
    assert second.report_metadata.get("overwrite_reason") == "restated"

    items = await repo.list_items_for_report(first.id)
    assert len(items) == 2


@pytest.mark.asyncio
async def test_default_reuse_does_not_replace_items(session: AsyncSession) -> None:
    """ROB-352 — without overwrite, a second ingest leaves the stored row intact."""
    service = InvestmentReportIngestionService(session)
    first = await service.ingest(
        _base_request(title="v1", items=[_action_item("a1")])
    )
    second = await service.ingest(
        _base_request(title="v2-ignored", items=[_action_item("a1"), _action_item("a2")])
    )
    assert second.id == first.id
    assert second.title == "v1"  # stored row unchanged

    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(first.id)
    assert len(items) == 1
```

Remove the bogus `client_item_key_unused` no-op guard line before running (it was illustrative); the real assertions are `len(first_items) == 1` onward. Final test body must not reference `client_item_key_unused`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_investment_reports_ingestion.py -k overwrite_replaces -v`
Expected: FAIL — `ingest()` got an unexpected keyword argument `overwrite`.

- [ ] **Step 3: Implement the overwrite branch**

Replace the `ingest` method body in `app/services/investment_reports/ingestion.py` (lines 40-106). New signature + branch:

```python
    async def ingest(
        self,
        request: IngestReportRequest,
        *,
        overwrite: bool = False,
        overwrite_reason: str | None = None,
    ) -> InvestmentReport:
        idempotency_key = report_key(
            report_type=request.report_type,
            market=request.market,
            market_session=request.market_session,
            account_scope=request.account_scope,
            execution_mode=request.execution_mode,
            kst_date=request.kst_date,
            generator_version=request.generator_version,
        )

        existing = await self._repo.get_report_by_idempotency_key(idempotency_key)
        # ROB-352 — default reuse: return the stored row unchanged. Only an
        # explicit overwrite transactionally replaces it (items + scalar/JSONB
        # fields) while keeping report_uuid / idempotency_key stable.
        if existing is not None and not overwrite:
            return existing

        # ROB-269 Phase 3 — gate runs for both insert and overwrite.
        gate_result = enforce_stale_gate_for_ingest(
            request,
            flag_enabled=settings.ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED,
        )
        report_metadata = dict(request.metadata)
        report_metadata.setdefault("stale_gate", gate_result.to_metadata_summary())
        if overwrite and overwrite_reason is not None:
            report_metadata["overwrite_reason"] = overwrite_reason

        if existing is not None:
            await self._repo.update_report(
                existing.id,
                report_type=request.report_type,
                market=request.market,
                market_session=request.market_session,
                account_scope=request.account_scope,
                execution_mode=request.execution_mode,
                created_by_profile=request.created_by_profile,
                title=request.title,
                summary=request.summary,
                risk_summary=request.risk_summary,
                thesis_text=request.thesis_text,
                no_action_note=request.no_action_note,
                market_snapshot=request.market_snapshot,
                portfolio_snapshot=request.portfolio_snapshot,
                previous_report_uuid=request.previous_report_uuid,
                status=request.status,
                report_metadata=report_metadata,
                valid_until=request.valid_until,
                published_at=request.published_at,
                snapshot_bundle_uuid=request.snapshot_bundle_uuid,
                snapshot_policy_version=request.snapshot_policy_version,
                snapshot_coverage_summary=request.snapshot_coverage_summary,
                snapshot_freshness_summary=request.snapshot_freshness_summary,
                source_conflicts=request.source_conflicts,
                unavailable_sources=request.unavailable_sources,
                snapshot_report_diagnostics=request.snapshot_report_diagnostics,
            )
            await self._repo.delete_items_for_report(existing.id)
            for item_req in request.items:
                await self._insert_item(existing, item_req)
            await self._session.flush()
            await self._session.refresh(existing)
            return existing

        report = await self._repo.insert_report(
            idempotency_key=idempotency_key,
            report_type=request.report_type,
            market=request.market,
            market_session=request.market_session,
            account_scope=request.account_scope,
            execution_mode=request.execution_mode,
            created_by_profile=request.created_by_profile,
            title=request.title,
            summary=request.summary,
            risk_summary=request.risk_summary,
            thesis_text=request.thesis_text,
            no_action_note=request.no_action_note,
            market_snapshot=request.market_snapshot,
            portfolio_snapshot=request.portfolio_snapshot,
            previous_report_uuid=request.previous_report_uuid,
            status=request.status,
            report_metadata=report_metadata,
            valid_until=request.valid_until,
            published_at=request.published_at,
            snapshot_bundle_uuid=request.snapshot_bundle_uuid,
            snapshot_policy_version=request.snapshot_policy_version,
            snapshot_coverage_summary=request.snapshot_coverage_summary,
            snapshot_freshness_summary=request.snapshot_freshness_summary,
            source_conflicts=request.source_conflicts,
            unavailable_sources=request.unavailable_sources,
            snapshot_report_diagnostics=request.snapshot_report_diagnostics,
        )

        for item_req in request.items:
            await self._insert_item(report, item_req)

        await self._session.flush()
        return report
```

Note: `_insert_item` composes a per-item idempotency key from `report.report_uuid` + `client_item_key` + natural fields. Because `delete_items_for_report` removed the old rows first, re-inserting with the same keys cannot collide.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_investment_reports_ingestion.py -v`
Expected: PASS — all existing tests plus the two new overwrite/reuse tests.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_reports/ingestion.py tests/test_investment_reports_ingestion.py
git commit -m "feat(ROB-352): ingestion default-reuse + explicit overwrite (in-place replace)"
```

---

## Task A4: Generator — reuse short-circuit + response-from-stored + overwrite

**Files:**
- Modify: `app/services/action_report/snapshot_backed/generator.py`
- Test: `tests/services/action_report/snapshot_backed/test_generator.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/services/action_report/snapshot_backed/test_generator.py`. Add a fake reports repository and tests:

```python
class _StoredReport:
    """Minimal stand-in for InvestmentReport on the reuse path."""

    def __init__(self, *, report_uuid: uuid.UUID, bundle_uuid: uuid.UUID) -> None:
        self.id = 42
        self.report_uuid = report_uuid
        self.snapshot_bundle_uuid = bundle_uuid
        self.snapshot_policy_version = "intraday_action_report_v1"
        self.snapshot_coverage_summary = {"required": {"portfolio": "fresh"}}
        self.snapshot_freshness_summary = {"overall": "fresh"}
        self.source_conflicts = {}
        self.unavailable_sources = {}
        self.report_metadata = {"stale_gate": {"reject": False}}
        self.snapshot_report_diagnostics = {"why_no_action": None}


class _FakeReportsRepository:
    def __init__(self, *, existing: _StoredReport | None = None, item_count: int = 0):
        self._existing = existing
        self._item_count = item_count
        self.get_by_key_calls: list[str] = []

    async def get_report_by_idempotency_key(self, key: str):
        self.get_by_key_calls.append(key)
        return self._existing

    async def list_items_for_report(self, report_id: int):
        return [object()] * self._item_count


@pytest.mark.asyncio
async def test_reuse_returns_stored_without_ensuring_bundle() -> None:
    """ROB-352 — existing key + default path returns stored report, skips ensure()."""
    stored = _StoredReport(report_uuid=uuid.uuid4(), bundle_uuid=uuid.uuid4())
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    reports_repo = _FakeReportsRepository(existing=stored, item_count=3)

    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
        reports_repository=reports_repo,
    )
    response = await gen.generate(_make_request())

    assert response.reused_existing is True
    assert response.report_uuid == stored.report_uuid
    assert response.items_count == 3
    assert response.bundle_reused is True
    assert ensure.calls == []          # never ensured a bundle
    assert ingest.calls == []          # never re-ingested


@pytest.mark.asyncio
async def test_overwrite_recomputes_and_forwards_overwrite_flag() -> None:
    """ROB-352 — overwrite_existing=True recomputes and ingests with overwrite."""
    stored = _StoredReport(report_uuid=uuid.uuid4(), bundle_uuid=uuid.uuid4())
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    reports_repo = _FakeReportsRepository(existing=stored, item_count=3)

    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
        reports_repository=reports_repo,
    )
    response = await gen.generate(
        _make_request(overwrite_existing=True, overwrite_reason="redo")
    )

    assert response.reused_existing is False
    assert len(ensure.calls) == 1                       # recomputed
    assert ingest.overwrite_calls == [("redo", True)]   # forwarded


@pytest.mark.asyncio
async def test_market_session_threads_into_ingest_request() -> None:
    """ROB-352 — market_session reaches the IngestReportRequest (idempotency key)."""
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
        reports_repository=_FakeReportsRepository(existing=None),
    )
    await gen.generate(_make_request(market_session="us_regular"))
    assert ingest.calls[0].market_session == "us_regular"
```

Also update `_FakeIngestionService.ingest` (top of file, lines 47-54) to accept + record the overwrite kwargs:

```python
class _FakeIngestionService:
    def __init__(self, *, report_uuid: uuid.UUID | None = None) -> None:
        self.report_uuid = report_uuid or uuid.uuid4()
        self.calls: list[IngestReportRequest] = []
        self.overwrite_calls: list[tuple[str | None, bool]] = []

    async def ingest(
        self,
        request: IngestReportRequest,
        *,
        overwrite: bool = False,
        overwrite_reason: str | None = None,
    ):
        self.calls.append(request)
        if overwrite:
            self.overwrite_calls.append((overwrite_reason, overwrite))
        return _FakeReport(self.report_uuid)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_generator.py -k "reuse or overwrite or market_session" -v`
Expected: FAIL — `SnapshotBackedReportGenerator.__init__()` got an unexpected keyword argument `reports_repository`.

- [ ] **Step 3: Implement generator changes**

In `app/services/action_report/snapshot_backed/generator.py`:

(a) Add imports near the existing investment_reports imports (after line 74):

```python
from app.services.investment_reports.idempotency import report_key
from app.services.investment_reports.repository import InvestmentReportsRepository
```

(b) Extend `__init__` to inject the reports repository. Add the parameter after `symbol_derivation_service` and store it:

```python
        symbol_derivation_service: SymbolDerivationService | None = None,
        reports_repository: InvestmentReportsRepository | None = None,
    ) -> None:
        self._session = session
        ...
        self._symbol_derivation = symbol_derivation_service or SymbolDerivationService(
            session
        )
        self._reports_repo = reports_repository or InvestmentReportsRepository(session)
```

(c) At the top of `generate()`, right after `self._validate_pair(request)` (line 214), add the reuse short-circuit:

```python
        self._validate_pair(request)

        # ROB-352 — deterministic regeneration: by default, an existing report
        # for this idempotency key is returned from the STORED row. We never
        # recompute a divergent, unstored payload on the default path. Only an
        # explicit overwrite recomputes and transactionally replaces.
        idempotency_key = report_key(
            report_type=request.report_type,
            market=request.market,
            market_session=request.market_session,
            account_scope=request.account_scope,
            execution_mode=request.execution_mode,
            kst_date=request.kst_date,
            generator_version=request.generator_version,
        )
        existing = await self._reports_repo.get_report_by_idempotency_key(
            idempotency_key
        )
        if existing is not None and not request.overwrite_existing:
            return await self._response_from_stored(existing, request)
```

(d) Change the `ingest` call (line 353) to forward the overwrite flag:

```python
        report = await self._ingestion_service.ingest(
            ingest_request,
            overwrite=request.overwrite_existing,
            overwrite_reason=request.overwrite_reason,
        )
```

(e) In `_build_ingest_request` (line 619 `IngestReportRequest(...)`), thread `market_session`:

```python
        return IngestReportRequest(
            report_type=request.report_type,
            market=request.market,
            market_session=request.market_session,
            account_scope=request.account_scope,
            ...
```

(f) Add the `_response_from_stored` helper (place it after `_validate_pair`, before `_auto_emit_items_from_bundle`):

```python
    async def _response_from_stored(
        self,
        report: Any,
        request: ReportGenerationRequest,
    ) -> ReportGenerationResponse:
        """ROB-352 — build a response that mirrors the persisted row.

        The default (non-overwrite) path returns this instead of recomputing,
        so the stored row and the returned payload can never disagree on the
        actionable/deferred/risk item set.
        """
        if report.snapshot_bundle_uuid is None:
            # Only this generator's rows share the key (report_type +
            # generator_version are in the key) and they always set a bundle
            # uuid. A None here means a foreign row collided — fail loudly
            # rather than fabricate a response.
            raise SnapshotBackedReportGeneratorError(
                "cannot reuse stored report: snapshot_bundle_uuid is missing; "
                "pass overwrite_existing=true with overwrite_reason to regenerate"
            )
        items = await self._reports_repo.list_items_for_report(report.id)
        freshness = report.snapshot_freshness_summary or {}
        metadata = report.report_metadata or {}
        diagnostics = report.snapshot_report_diagnostics or {}
        return ReportGenerationResponse(
            report_uuid=report.report_uuid,
            snapshot_bundle_uuid=report.snapshot_bundle_uuid,
            snapshot_policy_version=report.snapshot_policy_version
            or request.policy_version,
            snapshot_coverage_summary=report.snapshot_coverage_summary or {},
            snapshot_freshness_summary=freshness,
            source_conflicts=report.source_conflicts or {},
            unavailable_sources=report.unavailable_sources or {},
            items_count=len(items),
            warnings=[
                "reused_existing_report: pass overwrite_existing=true with "
                "overwrite_reason to regenerate"
            ],
            bundle_status=freshness.get("overall", "reused"),
            bundle_reused=True,
            stale_gate=metadata.get("stale_gate", {}),
            why_no_action=diagnostics.get("why_no_action"),
            reused_existing=True,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_generator.py -v`
Expected: PASS — existing generator tests plus the three new ones.

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/snapshot_backed/generator.py tests/services/action_report/snapshot_backed/test_generator.py
git commit -m "feat(ROB-352): generator reuse short-circuit, response-from-stored, market_session threading, overwrite forwarding"
```

---

## Task A5: MCP handler — params, account_scope + item pre-validation, user_id default

**Files:**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py`
- Test: `tests/mcp_server/test_investment_report_generate_from_bundle_handler.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/mcp_server/test_investment_report_generate_from_bundle_handler.py`:

```python
"""ROB-352 Slice A — generate_from_bundle MCP handler contract tests.

These cover the handler's pre-request validation, which short-circuits BEFORE
any DB session is opened — so no DB fixture is needed.
"""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import investment_reports_handlers as h
from app.services.action_report.snapshot_backed.generator import (
    _MARKET_ACCOUNT_PAIRS,
)


@pytest.fixture
def _enabled(monkeypatch: pytest.MonkeyPatch):
    from app.core.config import settings

    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )


def _kwargs(**overrides):
    base = dict(
        market="us",
        account_scope="kis_live",
        title="t",
        summary="s",
        kst_date="2026-05-29",
        created_by_profile="claude_code",
    )
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_disabled_returns_structured_error():
    res = await h.investment_report_generate_from_bundle_impl(**_kwargs())
    assert res["success"] is False
    assert res["error"] == "snapshot_backed_report_generator_disabled"


@pytest.mark.asyncio
async def test_unsupported_account_scope_fails_closed(_enabled):
    res = await h.investment_report_generate_from_bundle_impl(
        **_kwargs(account_scope="alpaca_paper")
    )
    assert res["success"] is False
    assert res["error"] == "unsupported_account_scope"
    assert "kis_live" in str(res["supported_pairs"])
    assert "hermes" in res["hint"].lower()


@pytest.mark.asyncio
async def test_invalid_item_reports_field_and_key(_enabled):
    res = await h.investment_report_generate_from_bundle_impl(
        **_kwargs(
            items=[
                {"item_kind": "action", "intent": "buy_review", "rationale": "r"}
            ]  # missing required client_item_key
        )
    )
    assert res["success"] is False
    assert res["error"] == "invalid_items"
    assert res["item_errors"][0]["index"] == 0
    assert "client_item_key" in str(res["item_errors"][0]["errors"])


@pytest.mark.asyncio
async def test_invalid_enum_item_names_the_field(_enabled):
    res = await h.investment_report_generate_from_bundle_impl(
        **_kwargs(
            items=[
                {
                    "client_item_key": "k1",
                    "item_kind": "action",
                    "intent": "not_a_real_intent",
                    "rationale": "r",
                }
            ]
        )
    )
    assert res["success"] is False
    assert res["error"] == "invalid_items"
    assert "intent" in str(res["item_errors"][0]["errors"])


def test_handler_supported_pairs_match_generator():
    """Drift-guard: the handler's allow-list must equal the generator's."""
    assert h._SUPPORTED_MARKET_ACCOUNT_PAIRS == _MARKET_ACCOUNT_PAIRS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/mcp_server/test_investment_report_generate_from_bundle_handler.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_SUPPORTED_MARKET_ACCOUNT_PAIRS'`; unsupported scope currently raises an uncaught `ValidationError` instead of returning `unsupported_account_scope`.

- [ ] **Step 3: Implement handler changes**

In `app/mcp_server/tooling/investment_reports_handlers.py`:

(a) Near the top of the module (after imports), add the supported-pairs mirror and a default-user helper:

```python
# ROB-352 — mirror of the generator's canonical market/account_scope pairs.
# A drift-guard test asserts this equals
# ``generator._MARKET_ACCOUNT_PAIRS``. Kept here as a literal so the handler
# can fail closed BEFORE importing/constructing the generator.
_SUPPORTED_MARKET_ACCOUNT_PAIRS: dict[str, str] = {
    "kr": "kis_live",
    "us": "kis_live",
    "crypto": "upbit_live",
}


def _default_generator_user_id() -> int:
    """ROB-352 — resolve the default operator user_id the same way the
    portfolio/holdings tools do (``MCP_USER_ID`` env, default 1), so a
    kis_live/upbit_live report no longer silently degrades to
    portfolio=unavailable when the caller omits user_id.
    """
    from app.mcp_server.tooling.shared import _MCP_USER_ID

    return _MCP_USER_ID
```

(b) Extend the handler signature (line 396-419) with the new params (insert after `report_type`, and after `user_id`):

```python
    report_type: str = "snapshot_backed_advisory_v1",
    market_session: str | None = None,
    symbols: list[str] | None = None,
    candidate_limit: int | None = None,
    requested_by: str = "claude_code",
    user_id: int | None = None,
    overwrite_existing: bool = False,
    overwrite_reason: str | None = None,
) -> dict:
```

(c) After the disabled-check block (after line 452, before building `payload`), add account_scope + item pre-validation and user_id resolution:

```python
    # ROB-352 — fail closed on unsupported account scopes BEFORE building the
    # request, with an actionable error that names the supported pairs and
    # routes paper accounts to the Hermes composition path.
    expected_scope = _SUPPORTED_MARKET_ACCOUNT_PAIRS.get(market)
    if expected_scope is None or account_scope != expected_scope:
        return {
            "success": False,
            "error": "unsupported_account_scope",
            "market": market,
            "account_scope": account_scope,
            "supported_pairs": _SUPPORTED_MARKET_ACCOUNT_PAIRS,
            "hint": (
                "This snapshot-backed generator only collects live KIS/Upbit "
                "data. For alpaca_paper / paper:<name> reports use the Hermes "
                "composition path (investment_report_create_from_hermes_"
                "composition)."
            ),
        }

    # ROB-352 — validate items with per-item, per-field errors instead of a
    # raw ValidationError. Names the offending item index/client_item_key and
    # the failing field so callers fix it without reading backend code.
    validated_items: list[IngestReportItem] = []
    item_errors: list[dict[str, Any]] = []
    for index, raw in enumerate(items or []):
        try:
            validated_items.append(IngestReportItem.model_validate(raw))
        except ValidationError as exc:
            item_errors.append(
                {
                    "index": index,
                    "client_item_key": (raw or {}).get("client_item_key"),
                    "errors": [
                        {
                            "field": ".".join(str(p) for p in err["loc"]),
                            "message": err["msg"],
                        }
                        for err in exc.errors()
                    ],
                }
            )
    if item_errors:
        return {
            "success": False,
            "error": "invalid_items",
            "item_errors": item_errors,
            "required_fields": ["client_item_key", "item_kind", "intent", "rationale"],
            "enums": {
                "item_kind": ["action", "watch", "risk"],
                "intent": [
                    "buy_review",
                    "sell_review",
                    "risk_review",
                    "trend_recovery_review",
                    "rebalance_review",
                ],
                "target_kind": ["asset", "index", "fx"],
                "side": ["buy", "sell"],
            },
            "notes": (
                "watch items require watch_condition + valid_until unless "
                "operation is 'review'; decision_bucket must be one of the "
                "DECISION_BUCKETS vocabulary."
            ),
        }

    # ROB-352 — resolve a default user_id for live account scopes so the
    # portfolio collector can read live holdings/cash (was a hidden required
    # dependency that degraded the bundle to failed → forced no_action).
    resolved_user_id = user_id if user_id is not None else _default_generator_user_id()
```

(d) Update the `payload` dict (lines 454-477) to use the validated items, thread `market_session`, the resolved user_id, and the overwrite fields:

```python
    payload: dict[str, Any] = {
        "market": market,
        "account_scope": account_scope,
        "market_session": market_session,
        "policy_version": policy_version,
        "status": status,
        "requested_by": requested_by,
        "report_type": report_type,
        "generator_version": generator_version,
        "created_by_profile": created_by_profile,
        "title": title,
        "summary": summary,
        "kst_date": kst_date,
        "risk_summary": risk_summary,
        "thesis_text": thesis_text,
        "no_action_note": no_action_note,
        "items": validated_items,
        "previous_report_uuid": previous_report_uuid,
        "valid_until": valid_until,
        "published_at": published_at,
        "metadata": metadata or {},
        "symbols": symbols,
        "candidate_limit": candidate_limit,
        "user_id": resolved_user_id,
        "overwrite_existing": overwrite_existing,
        "overwrite_reason": overwrite_reason,
    }
```

(e) Add the import for `ValidationError` at the top of the file if not present:

```python
from pydantic import ValidationError
```

Verify `IngestReportItem` and `Any` are already imported (they are — `IngestReportItem` is used at the old line 469; `Any` is used throughout). Confirm `_MARKET_ACCOUNT_PAIRS` is importable from the generator (it is a module-level dict).

(f) Add a response field so callers see which user_id was used. After the `response = await generator.generate(request)` succeeds (line 496 return), change:

```python
    return {
        "success": True,
        "resolved_user_id": resolved_user_id,
        **response.model_dump(mode="json"),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mcp_server/test_investment_report_generate_from_bundle_handler.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/investment_reports_handlers.py tests/mcp_server/test_investment_report_generate_from_bundle_handler.py
git commit -m "feat(ROB-352): handler account_scope/item pre-validation, user_id default, market_session + overwrite params"
```

---

## Task A6: Tool description doc reflects the real contract

**Files:**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py` (the `mcp.tool(... description=...)` for `investment_report_generate_from_bundle`, lines 550-564)
- Test: `tests/mcp_server/test_investment_report_generate_from_bundle_handler.py` (append)

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_tool_description_documents_contract():
    """ROB-352 — the registered tool description must surface the real
    constraints (supported scopes, required item fields, idempotency)."""
    from app.mcp_server.tooling.investment_reports_handlers import (
        GENERATE_FROM_BUNDLE_DESCRIPTION,
    )

    desc = GENERATE_FROM_BUNDLE_DESCRIPTION
    assert "kis_live" in desc
    assert "client_item_key" in desc
    assert "overwrite_existing" in desc
    assert "market_session" in desc
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/test_investment_report_generate_from_bundle_handler.py -k description -v`
Expected: FAIL — `ImportError: cannot import name 'GENERATE_FROM_BUNDLE_DESCRIPTION'`.

- [ ] **Step 3: Extract the description to a constant and enrich it**

In `app/mcp_server/tooling/investment_reports_handlers.py`, near the other module constants (after `_SUPPORTED_MARKET_ACCOUNT_PAIRS`), add:

```python
GENERATE_FROM_BUNDLE_DESCRIPTION = (
    "ROB-273/ROB-352 — generate a snapshot-backed advisory investment_report "
    "end-to-end. Ensures (or reuses) a snapshot bundle, runs the read-only "
    "collector registry, normalises payloads, and persists the report with "
    "snapshot metadata. Opt-in: returns {success:false, "
    "error:'snapshot_backed_report_generator_disabled'} unless "
    "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED is true. "
    "Supported market/account_scope pairs ONLY: kr/kis_live, us/kis_live, "
    "crypto/upbit_live — any other pair fails closed with "
    "error:'unsupported_account_scope' (use the Hermes composition path for "
    "alpaca_paper). user_id is auto-resolved from MCP_USER_ID when omitted so "
    "kis_live/upbit_live portfolios are readable; pass user_id to override. "
    "Optional market_session refines US/KR session reporting and is part of the "
    "idempotency key. items[] each require: client_item_key, item_kind "
    "(action|watch|risk), intent (buy_review|sell_review|risk_review|"
    "trend_recovery_review|rebalance_review), rationale; watch items also need "
    "watch_condition+valid_until unless operation='review'. Invalid items return "
    "error:'invalid_items' naming the offending index/field. "
    "Deterministic regeneration: by default an existing report for the same key "
    "is RETURNED FROM THE STORED ROW (reused_existing=true); pass "
    "overwrite_existing=true with overwrite_reason to transactionally replace it. "
    "No broker / order / watch mutation."
)
```

Then replace the inline `description=(...)` in the `mcp.tool(name="investment_report_generate_from_bundle", ...)` registration (lines 552-563) with:

```python
    mcp.tool(
        name="investment_report_generate_from_bundle",
        description=GENERATE_FROM_BUNDLE_DESCRIPTION,
    )(investment_report_generate_from_bundle_impl)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/test_investment_report_generate_from_bundle_handler.py -k description -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/investment_reports_handlers.py tests/mcp_server/test_investment_report_generate_from_bundle_handler.py
git commit -m "docs(ROB-352): tool description reflects real account_scope/items/idempotency contract"
```

---

## Task A7: Full-suite verification + lint + import guards

**Files:** none (verification only)

- [ ] **Step 1: Run the touched suites**

Run:
```bash
uv run pytest \
  tests/test_investment_reports_ingestion.py \
  tests/test_investment_reports_repository_overwrite.py \
  tests/services/action_report/snapshot_backed/test_generator.py \
  tests/services/action_report/snapshot_backed/test_request_overwrite_fields.py \
  tests/mcp_server/test_investment_report_generate_from_bundle_handler.py \
  tests/routers/test_investment_reports_snapshot_backed.py -v
```
Expected: all PASS.

- [ ] **Step 2: Lint**

Run: `uv run ruff check app/ tests/`
Expected: no errors on touched files. Run `uv run ruff format app/ tests/` if formatting drifts.

- [ ] **Step 3: Import guards (per memory: ROB-287 in-process-LLM guard + ROB-285 binance host guard)**

Run: `uv run pytest tests/ -k "guard or import_guard" -v`
Expected: PASS — no new in-process LLM import introduced under `app/services/action_report/snapshot_backed/` or `app/services/investment_stages/`.

- [ ] **Step 4: Type check (best-effort)**

Run: `uv run ty check app/services/action_report/snapshot_backed/ app/services/investment_reports/ app/mcp_server/tooling/investment_reports_handlers.py` (or `make typecheck`).
Expected: no new errors attributable to this change.

- [ ] **Step 5: Confirm Test workflow green before any merge**

Per the pre-merge full-CI gate: branch protection does NOT gate lint/test. Push the branch, open the PR, and confirm the GitHub Test workflow is green before merging. Do not `gh pr merge --auto` onto a red main.

---

## Self-Review

**Spec coverage (Slice A scope):**
- account_scope contract + actionable unsupported error → A5, A6.
- market_session input path → A4 (generator threading) + A5 (handler param).
- user_id default resolution (like get_holdings, via MCP_USER_ID) → A5.
- items schema/docs + per-item/per-field validation errors → A5, A6.
- deterministic regeneration: default reuse (stored row returned, mismatch fixed) + explicit overwrite (transactional replace, stable uuid), no report_type/created_by_profile workaround → A1, A2, A3, A4.
- focused tests for account_scope, market_session, user_id, item validation, idempotent regeneration → A3, A4, A5.

**Non-goals respected:** no Slice B (evidence/citation), no Slice C (candidate quality), no broker/order/watch mutation, no scheduler, no production smoke. The overwrite path only mutates the `investment_reports`/`investment_report_items` tables via the existing service/repository boundary.

**Type consistency:** `ingest(request, *, overwrite, overwrite_reason)` signature is identical in ingestion.py (A3), the generator call site (A4), and the test fake (A4). `_SUPPORTED_MARKET_ACCOUNT_PAIRS` (handler) is asserted equal to `_MARKET_ACCOUNT_PAIRS` (generator) by a drift-guard test (A5). `reused_existing`/`overwrite_existing`/`overwrite_reason` names match across request.py, generator.py, and handler.

**Open risk flagged in-code:** `_response_from_stored` raises if a matching stored row has `snapshot_bundle_uuid is None` rather than fabricate a response — acceptable because the idempotency key is scoped by report_type + generator_version, so only this generator's (bundle-bearing) rows can match.
