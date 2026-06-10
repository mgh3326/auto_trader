# ROB-499 Draft Report Incremental Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add draft-only report mutation tools so intraday report changes can append new items and refresh header snapshots without recreating the whole report chain.

**Status:** Implemented on `rob-499`; final review follow-up adds parent report row locking for concurrent draft mutations.

**Architecture:** Keep `investment_report_create` insert-only and preserve existing 7-key report idempotency. Add narrow draft mutation methods to `InvestmentReportIngestionService`, backed by small repository helpers and exposed through two MCP tools: `investment_report_add_items` and `investment_report_update`. No DB migration: new writes mirror `client_item_key` into item metadata for client-key idempotency, while exact `idempotency_key` lookup protects legacy rows.

**Tech Stack:** Python 3.13, Pydantic v2, SQLAlchemy async, FastMCP, pytest, `uv`.

---

## Decisions Locked

- Build both issue options: `investment_report_add_items(report_uuid, items[])` and `investment_report_update(report_uuid, ...)`.
- Both tools are draft-only. `published`, `decided`, `expired`, and `superseded` reports return a structured `not_draft` error.
- Item append never edits or deletes an existing item. A duplicate `client_item_key` returns the existing row as an idempotent no-op.
- Draft report mutations and status transitions serialize on the parent `investment_reports` row with `SELECT ... FOR UPDATE`, so concurrent edits cannot bypass draft-only or client-key idempotency checks.
- No migration in this slice. `client_item_key` is mirrored into `InvestmentReportItem.item_metadata["client_item_key"]` for new rows. Existing rows without the mirror still dedupe when the exact computed `idempotency_key` matches.
- Header update can change `title`, `summary`, `risk_summary`, `thesis_text`, `no_action_note`, `market_snapshot`, `portfolio_snapshot`, `metadata`, and `valid_until`. It cannot change identity fields, status, chain links, account scope, or snapshot bundle linkage.
- This is not a live order, auth, DB schema, or strategy-policy change. Do not add `high_risk_change` labels for the implementation unless scope expands.

## File Structure

| File | Role | Change |
|---|---|---|
| `app/schemas/investment_reports.py` | MCP/service request validation | Add `AddReportItemsRequest` and `UpdateDraftReportRequest` |
| `app/services/investment_reports/repository.py` | DAO helpers only | Add item lookup by idempotency key and metadata client key |
| `app/services/investment_reports/ingestion.py` | Business rules | Add draft-only append/update methods and item metadata mirror |
| `app/mcp_server/tooling/investment_reports_handlers.py` | MCP surface | Register `investment_report_add_items` and `investment_report_update` |
| `tests/test_investment_reports_schemas.py` | Schema tests | New request validation tests |
| `tests/test_investment_reports_repository.py` | DAO tests | New lookup helper tests |
| `tests/test_investment_reports_ingestion.py` | Service tests | Draft gate, append idempotency, update audit tests |
| `tests/test_investment_reports_mcp.py` | Tool tests | End-to-end handler tests and tool-name set update |
| `tests/mcp_server/test_investment_report_tool_descriptions.py` | Contract docs tests | Description mentions draft-only and no broker mutation |
| `app/mcp_server/README.md` | Public tool reference | Add concise bullets for the two new tools |

---

## Task 1: Add Request Schemas

**Files:**
- Modify: `app/schemas/investment_reports.py`
- Test: `tests/test_investment_reports_schemas.py`

- [x] **Step 1: Write failing schema tests**

Append these tests to `tests/test_investment_reports_schemas.py` and add the two new imports.

```python
from app.schemas.investment_reports import (
    ActivateWatchRequest,
    AddReportItemsRequest,
    IngestReportItem,
    IngestReportRequest,
    MaxActionPayload,
    RecordDecisionRequest,
    ReportSnapshotBundleResponse,
    ReportSnapshotDetailResponse,
    UpdateDraftReportRequest,
    WatchConditionClause,
    WatchConditionPayload,
)


def test_add_report_items_request_requires_non_empty_items() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AddReportItemsRequest(report_uuid=uuid.uuid4(), items=[])
    assert "items" in str(exc_info.value)


def test_add_report_items_request_accepts_ingest_items() -> None:
    req = AddReportItemsRequest(
        report_uuid=uuid.uuid4(),
        items=[IngestReportItem(**_base_item_kwargs(client_item_key="increment-1"))],
        actor="operator",
    )
    assert req.items[0].client_item_key == "increment-1"
    assert req.actor == "operator"


def test_update_draft_report_request_requires_at_least_one_update_field() -> None:
    with pytest.raises(ValidationError) as exc_info:
        UpdateDraftReportRequest(report_uuid=uuid.uuid4(), actor="operator")
    assert "at least one draft report field" in str(exc_info.value)


def test_update_draft_report_request_accepts_summary_and_snapshots() -> None:
    req = UpdateDraftReportRequest(
        report_uuid=uuid.uuid4(),
        summary="fresh intraday summary",
        market_snapshot={"kospi": {"last": 2860.12}},
        portfolio_snapshot={"cash": 12345},
        metadata={"source": "intraday_update"},
        reason="market moved",
    )
    assert req.summary == "fresh intraday summary"
    assert req.market_snapshot == {"kospi": {"last": 2860.12}}
    assert req.reason == "market moved"
```

- [x] **Step 2: Run the focused failing tests**

Run:

```bash
uv run pytest tests/test_investment_reports_schemas.py -q
```

Expected: FAIL with import errors for `AddReportItemsRequest` and `UpdateDraftReportRequest`.

- [x] **Step 3: Add schema classes**

In `app/schemas/investment_reports.py`, add this immediately after `SetReportStatusRequest`.

```python
class AddReportItemsRequest(BaseModel):
    """ROB-499 - append new items to an existing draft report."""

    report_uuid: UUID
    items: list[IngestReportItem] = Field(min_length=1)
    actor: str | None = None


class UpdateDraftReportRequest(BaseModel):
    """ROB-499 - update draft report header fields without changing identity."""

    report_uuid: UUID
    title: str | None = None
    summary: str | None = None
    risk_summary: str | None = None
    thesis_text: str | None = None
    no_action_note: str | None = None
    market_snapshot: dict[str, Any] | None = None
    portfolio_snapshot: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    valid_until: datetime | None = None
    actor: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_has_update(self) -> UpdateDraftReportRequest:
        update_fields = (
            "title",
            "summary",
            "risk_summary",
            "thesis_text",
            "no_action_note",
            "market_snapshot",
            "portfolio_snapshot",
            "metadata",
            "valid_until",
        )
        if all(getattr(self, name) is None for name in update_fields):
            raise ValueError("at least one draft report field must be supplied")
        return self
```

- [x] **Step 4: Verify schema tests pass**

Run:

```bash
uv run pytest tests/test_investment_reports_schemas.py -q
```

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add app/schemas/investment_reports.py tests/test_investment_reports_schemas.py
git commit -m "feat: add draft report mutation request schemas"
```

---

## Task 2: Add Repository Lookup Helpers

**Files:**
- Modify: `app/services/investment_reports/repository.py`
- Test: `tests/test_investment_reports_repository.py`

- [x] **Step 1: Write failing repository tests**

Append these tests to `tests/test_investment_reports_repository.py`.

```python
@pytest.mark.asyncio
async def test_item_get_by_idempotency_key(session: AsyncSession) -> None:
    repo = InvestmentReportsRepository(session)
    report = await _insert_report(repo)
    item = await _insert_item(repo, report.id, idempotency_key="item:dedupe")

    fetched = await repo.get_item_by_idempotency_key("item:dedupe")

    assert fetched is not None
    assert fetched.id == item.id


@pytest.mark.asyncio
async def test_find_item_by_report_client_key_from_metadata(
    session: AsyncSession,
) -> None:
    repo = InvestmentReportsRepository(session)
    report = await _insert_report(repo)
    other = await _insert_report(repo)
    item = await _insert_item(
        repo,
        report.id,
        item_metadata={"client_item_key": "increment-1"},
    )
    await _insert_item(
        repo,
        other.id,
        item_metadata={"client_item_key": "increment-1"},
    )

    fetched = await repo.find_item_by_report_client_key(report.id, "increment-1")

    assert fetched is not None
    assert fetched.id == item.id


@pytest.mark.asyncio
async def test_find_item_by_report_client_key_ignores_missing_metadata(
    session: AsyncSession,
) -> None:
    repo = InvestmentReportsRepository(session)
    report = await _insert_report(repo)
    await _insert_item(repo, report.id, item_metadata={})

    fetched = await repo.find_item_by_report_client_key(report.id, "increment-1")

    assert fetched is None
```

- [x] **Step 2: Run the focused failing tests**

Run:

```bash
uv run pytest tests/test_investment_reports_repository.py::test_item_get_by_idempotency_key tests/test_investment_reports_repository.py::test_find_item_by_report_client_key_from_metadata tests/test_investment_reports_repository.py::test_find_item_by_report_client_key_ignores_missing_metadata -q
```

Expected: FAIL with `AttributeError` for the new repository methods.

- [x] **Step 3: Implement lookup helpers**

In `app/services/investment_reports/repository.py`, add these methods in the `# Items` section after `get_item_by_uuid`.

```python
    async def get_item_by_idempotency_key(
        self, idempotency_key: str
    ) -> InvestmentReportItem | None:
        return await self._session.scalar(
            sa.select(InvestmentReportItem).where(
                InvestmentReportItem.idempotency_key == idempotency_key
            )
        )

    async def find_item_by_report_client_key(
        self, report_id: int, client_item_key: str
    ) -> InvestmentReportItem | None:
        items = await self.list_items_for_report(report_id)
        for item in items:
            metadata = item.item_metadata if isinstance(item.item_metadata, dict) else {}
            if metadata.get("client_item_key") == client_item_key:
                return item
        return None
```

This deliberately avoids a JSONB SQL predicate so tests and local DB adapters stay aligned with existing repository patterns.

- [x] **Step 4: Verify repository tests pass**

Run:

```bash
uv run pytest tests/test_investment_reports_repository.py -q
```

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add app/services/investment_reports/repository.py tests/test_investment_reports_repository.py
git commit -m "feat: add investment report item lookup helpers"
```

---

## Task 3: Add Draft Mutation Service Methods

**Files:**
- Modify: `app/services/investment_reports/ingestion.py`
- Test: `tests/test_investment_reports_ingestion.py`

- [x] **Step 1: Write failing service tests**

Append these tests to `tests/test_investment_reports_ingestion.py`.

```python
@pytest.mark.asyncio
async def test_add_items_to_draft_appends_and_mirrors_client_key(
    session: AsyncSession,
) -> None:
    service = InvestmentReportIngestionService(session)
    report = await service.ingest(_base_request(items=[_action_item("base-1")]))

    stored_report, inserted, existing = await service.add_items_to_draft(
        report_uuid=report.report_uuid,
        items=[_action_item("increment-1", symbol="000660")],
    )

    assert stored_report is not None
    assert len(inserted) == 1
    assert existing == []

    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    assert len(items) == 2
    assert {it.item_metadata.get("client_item_key") for it in items} == {
        "base-1",
        "increment-1",
    }


@pytest.mark.asyncio
async def test_add_items_to_draft_is_idempotent_by_client_item_key(
    session: AsyncSession,
) -> None:
    service = InvestmentReportIngestionService(session)
    report = await service.ingest(_base_request(items=[]))
    item = _action_item("increment-1", symbol="000660")

    first_report, first_inserted, first_existing = await service.add_items_to_draft(
        report_uuid=report.report_uuid,
        items=[item],
    )
    second_report, second_inserted, second_existing = await service.add_items_to_draft(
        report_uuid=report.report_uuid,
        items=[item],
    )

    assert first_report is not None
    assert second_report is not None
    assert len(first_inserted) == 1
    assert first_existing == []
    assert second_inserted == []
    assert len(second_existing) == 1

    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    assert len(items) == 1


@pytest.mark.asyncio
async def test_add_items_to_draft_rejects_non_draft_report(
    session: AsyncSession,
) -> None:
    from app.services.investment_reports.ingestion import (
        DraftReportMutationBlockedError,
    )

    service = InvestmentReportIngestionService(session)
    report = await service.ingest(
        _base_request(status="published", items=[_action_item("base-1")])
    )

    with pytest.raises(DraftReportMutationBlockedError) as exc_info:
        await service.add_items_to_draft(
            report_uuid=report.report_uuid,
            items=[_action_item("increment-1", symbol="000660")],
        )

    assert exc_info.value.status == "published"


@pytest.mark.asyncio
async def test_update_draft_report_updates_header_and_audit_metadata(
    session: AsyncSession,
) -> None:
    service = InvestmentReportIngestionService(session)
    report = await service.ingest(_base_request(summary="old summary"))

    updated = await service.update_draft_report(
        report_uuid=report.report_uuid,
        updates={
            "summary": "new intraday summary",
            "market_snapshot": {"kospi": {"last": 2860.12}},
            "metadata": {"source": "intraday_update"},
        },
        actor="operator",
        reason="market moved",
    )

    assert updated is not None
    assert updated.summary == "new intraday summary"
    assert updated.market_snapshot == {"kospi": {"last": 2860.12}}
    assert updated.report_metadata["source"] == "intraday_update"
    assert updated.report_metadata["draft_updates"][-1] == {
        "fields": ["market_snapshot", "metadata", "summary"],
        "actor": "operator",
        "reason": "market moved",
    }


@pytest.mark.asyncio
async def test_update_draft_report_returns_none_for_unknown_report(
    session: AsyncSession,
) -> None:
    service = InvestmentReportIngestionService(session)

    updated = await service.update_draft_report(
        report_uuid=uuid.uuid4(),
        updates={"summary": "new"},
    )

    assert updated is None
```

- [x] **Step 2: Run failing service tests**

Run:

```bash
uv run pytest tests/test_investment_reports_ingestion.py::test_add_items_to_draft_appends_and_mirrors_client_key tests/test_investment_reports_ingestion.py::test_add_items_to_draft_is_idempotent_by_client_item_key tests/test_investment_reports_ingestion.py::test_add_items_to_draft_rejects_non_draft_report tests/test_investment_reports_ingestion.py::test_update_draft_report_updates_header_and_audit_metadata tests/test_investment_reports_ingestion.py::test_update_draft_report_returns_none_for_unknown_report -q
```

Expected: FAIL with missing `add_items_to_draft`, `update_draft_report`, and `DraftReportMutationBlockedError`.

- [x] **Step 3: Add service error class**

In `app/services/investment_reports/ingestion.py`, change the model import to include items.

```python
from app.models.investment_reports import InvestmentReport, InvestmentReportItem
```

In `app/services/investment_reports/ingestion.py`, add this after `ReportOverwriteBlockedError`.

```python
class DraftReportMutationBlockedError(RuntimeError):
    """Raised when a draft-only mutation targets a non-draft report."""

    def __init__(self, *, report_uuid: object, status: str) -> None:
        super().__init__(
            f"draft mutation blocked: report {report_uuid} has status {status!r}"
        )
        self.report_uuid = report_uuid
        self.status = status
```

- [x] **Step 4: Add a reusable item key helper and return inserted rows**

In `InvestmentReportIngestionService`, add this helper above `_insert_item`.

```python
    @staticmethod
    def _item_idempotency_key(
        report: InvestmentReport, item_req: IngestReportItem
    ) -> str:
        watch_condition_payload = (
            item_req.watch_condition.model_dump(mode="json")
            if item_req.watch_condition is not None
            else None
        )
        return item_key(
            report_uuid=str(report.report_uuid),
            client_item_key=item_req.client_item_key,
            item_kind=item_req.item_kind,
            symbol=item_req.symbol,
            side=item_req.side,
            intent=item_req.intent,
            watch_condition=watch_condition_payload,
        )
```

Then change `_insert_item` to return `InvestmentReportItem`, use the helper, and mirror `client_item_key` into metadata.

```python
    async def _insert_item(
        self, report: InvestmentReport, item_req: IngestReportItem
    ) -> InvestmentReportItem:
        watch_condition_payload = (
            item_req.watch_condition.model_dump(mode="json")
            if item_req.watch_condition is not None
            else None
        )
        target_ref_payload = (
            item_req.target_ref.model_dump(mode="json")
            if item_req.target_ref is not None
            else None
        )
        idempotency_key = self._item_idempotency_key(report, item_req)
        evidence_payload = dict(item_req.evidence_snapshot or {})
        if item_req.evidence:
            evidence_payload["structured_evidence"] = [
                e.model_dump(mode="json") for e in item_req.evidence
            ]
        if item_req.freshness is not None:
            evidence_payload["item_freshness"] = item_req.freshness
        item_metadata = dict(item_req.metadata or {})
        item_metadata["client_item_key"] = item_req.client_item_key
        return await self._repo.insert_item(
            report_id=report.id,
            idempotency_key=idempotency_key,
            item_kind=item_req.item_kind,
            symbol=item_req.symbol,
            side=item_req.side,
            intent=item_req.intent,
            target_kind=item_req.target_kind,
            priority=item_req.priority,
            confidence=item_req.confidence,
            rationale=item_req.rationale,
            evidence_snapshot=evidence_payload,
            watch_condition=watch_condition_payload,
            trigger_checklist=item_req.trigger_checklist,
            max_action=item_req.max_action,
            valid_until=item_req.valid_until,
            item_metadata=item_metadata,
            operation=item_req.operation,
            target_ref=target_ref_payload,
            current_state=item_req.current_state,
            proposed_state=item_req.proposed_state,
            diff=item_req.diff,
            apply_policy=item_req.apply_policy,
            decision_bucket=item_req.decision_bucket,
            cited_symbol_report_uuid=item_req.cited_symbol_report_uuid,
            cited_dimension_report_uuids=list(item_req.cited_dimension_report_uuids),
            cited_snapshot_uuids=list(item_req.cited_snapshot_uuids),
        )
```

- [x] **Step 5: Add draft append and update methods**

Add these methods before `_maybe_supersede_previous`.

```python
    async def add_items_to_draft(
        self, *, report_uuid: UUID, items: list[IngestReportItem]
    ) -> tuple[
        InvestmentReport | None,
        list[InvestmentReportItem],
        list[InvestmentReportItem],
    ]:
        report = await self._repo.get_report_by_uuid(report_uuid)
        if report is None:
            return None, [], []
        if report.status != "draft":
            raise DraftReportMutationBlockedError(
                report_uuid=report.report_uuid, status=report.status
            )

        inserted: list[InvestmentReportItem] = []
        existing: list[InvestmentReportItem] = []
        for item_req in items:
            by_client_key = await self._repo.find_item_by_report_client_key(
                report.id, item_req.client_item_key
            )
            if by_client_key is not None:
                existing.append(by_client_key)
                continue

            item_idempotency_key = self._item_idempotency_key(report, item_req)
            by_exact_key = await self._repo.get_item_by_idempotency_key(
                item_idempotency_key
            )
            if by_exact_key is not None:
                existing.append(by_exact_key)
                continue

            inserted.append(await self._insert_item(report, item_req))

        await self._session.flush()
        await self._session.refresh(report)
        return report, inserted, existing

    async def update_draft_report(
        self,
        *,
        report_uuid: UUID,
        updates: dict[str, Any],
        actor: str | None = None,
        reason: str | None = None,
    ) -> InvestmentReport | None:
        report = await self._repo.get_report_by_uuid(report_uuid)
        if report is None:
            return None
        if report.status != "draft":
            raise DraftReportMutationBlockedError(
                report_uuid=report.report_uuid, status=report.status
            )

        allowed = {
            "title",
            "summary",
            "risk_summary",
            "thesis_text",
            "no_action_note",
            "market_snapshot",
            "portfolio_snapshot",
            "valid_until",
        }
        fields = {k: v for k, v in updates.items() if k in allowed}
        metadata = dict(report.report_metadata or {})
        metadata_patch = updates.get("metadata")
        if isinstance(metadata_patch, dict):
            metadata.update(metadata_patch)

        audit_entry: dict[str, Any] = {"fields": sorted(updates.keys())}
        if actor is not None:
            audit_entry["actor"] = actor
        if reason is not None:
            audit_entry["reason"] = reason
        draft_updates = list(metadata.get("draft_updates") or [])
        draft_updates.append(audit_entry)
        metadata["draft_updates"] = draft_updates
        fields["report_metadata"] = metadata

        await self._repo.update_report(report.id, **fields)
        await self._session.refresh(report)
        return report
```

- [x] **Step 6: Verify service tests pass**

Run:

```bash
uv run pytest tests/test_investment_reports_ingestion.py -q
```

Expected: PASS.

- [x] **Step 7: Commit**

```bash
git add app/services/investment_reports/ingestion.py tests/test_investment_reports_ingestion.py
git commit -m "feat: add draft investment report mutation service"
```

---

## Task 4: Expose MCP Tools

**Files:**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py`
- Test: `tests/test_investment_reports_mcp.py`

- [x] **Step 1: Write failing MCP tests**

In `tests/test_investment_reports_mcp.py`, add imports for the two handlers.

```python
from app.mcp_server.tooling.investment_reports_handlers import (
    INVESTMENT_REPORT_TOOL_NAMES,
    investment_report_activate_watch_impl,
    investment_report_add_items_impl,
    investment_report_context_get_impl,
    investment_report_create_impl,
    investment_report_decide_item_impl,
    investment_report_delta_get_impl,
    investment_report_generate_from_bundle_impl,
    investment_report_get_impl,
    investment_report_list_impl,
    investment_report_set_status_impl,
    investment_report_update_impl,
    investment_watch_recommend_impl,
)
```

Update `test_tool_names_match_registered_set` to include:

```python
        "investment_report_add_items",
        "investment_report_update",
```

Append these tests near the existing ROB-455 status tests.

```python
@pytest.mark.asyncio
async def test_add_items_impl_appends_to_draft_report(session: AsyncSession) -> None:
    created = await investment_report_create_impl(
        items=[_action_item_dict("base-1")], **_create_kwargs()
    )
    report_uuid = created["report"]["report_uuid"]

    out = await investment_report_add_items_impl(
        report_uuid=report_uuid,
        items=[_action_item_dict("increment-1") | {"symbol": "000660"}],
        actor="operator",
    )

    assert out["success"] is True
    assert out["inserted_count"] == 1
    assert out["existing_count"] == 0

    fetched = await investment_report_get_impl(report_uuid)
    assert len(fetched["items"]) == 2
    assert {it["metadata"].get("client_item_key") for it in fetched["items"]} == {
        "base-1",
        "increment-1",
    }


@pytest.mark.asyncio
async def test_add_items_impl_replays_duplicate_as_existing(
    session: AsyncSession,
) -> None:
    created = await investment_report_create_impl(
        items=[], **_create_kwargs(kst_date="2026-05-20")
    )
    report_uuid = created["report"]["report_uuid"]
    item = _action_item_dict("increment-1") | {"symbol": "000660"}

    first = await investment_report_add_items_impl(report_uuid=report_uuid, items=[item])
    second = await investment_report_add_items_impl(report_uuid=report_uuid, items=[item])

    assert first["success"] is True
    assert first["inserted_count"] == 1
    assert second["success"] is True
    assert second["inserted_count"] == 0
    assert second["existing_count"] == 1

    fetched = await investment_report_get_impl(report_uuid)
    assert len(fetched["items"]) == 1


@pytest.mark.asyncio
async def test_add_items_impl_rejects_published_report(session: AsyncSession) -> None:
    created = await investment_report_create_impl(
        items=[_action_item_dict("base-1")], **_create_kwargs()
    )
    report_uuid = created["report"]["report_uuid"]
    await _publish_by_uuid(report_uuid)

    out = await investment_report_add_items_impl(
        report_uuid=report_uuid,
        items=[_action_item_dict("increment-1") | {"symbol": "000660"}],
    )

    assert out["success"] is False
    assert out["error"] == "not_draft"
    assert out["status"] == "published"


@pytest.mark.asyncio
async def test_update_impl_updates_draft_summary_and_snapshots(
    session: AsyncSession,
) -> None:
    created = await investment_report_create_impl(
        items=[_action_item_dict("base-1")],
        **_create_kwargs(summary="old summary", kst_date="2026-05-21"),
    )
    report_uuid = created["report"]["report_uuid"]

    out = await investment_report_update_impl(
        report_uuid=report_uuid,
        summary="new intraday summary",
        market_snapshot={"kospi": {"last": 2860.12}},
        portfolio_snapshot={"cash": 12345},
        metadata={"source": "intraday_update"},
        actor="operator",
        reason="market moved",
    )

    assert out["success"] is True
    assert out["report"]["summary"] == "new intraday summary"
    assert out["report"]["market_snapshot"] == {"kospi": {"last": 2860.12}}
    assert out["report"]["metadata"]["source"] == "intraday_update"
    assert out["report"]["metadata"]["draft_updates"][-1]["actor"] == "operator"


@pytest.mark.asyncio
async def test_update_impl_rejects_empty_update(session: AsyncSession) -> None:
    created = await investment_report_create_impl(
        items=[_action_item_dict("base-1")], **_create_kwargs(kst_date="2026-05-22")
    )

    out = await investment_report_update_impl(
        report_uuid=created["report"]["report_uuid"],
        actor="operator",
    )

    assert out["success"] is False
    assert out["error"] == "invalid_request"
```

- [x] **Step 2: Run failing MCP tests**

Run:

```bash
uv run pytest tests/test_investment_reports_mcp.py::test_tool_names_match_registered_set tests/test_investment_reports_mcp.py::test_add_items_impl_appends_to_draft_report tests/test_investment_reports_mcp.py::test_add_items_impl_replays_duplicate_as_existing tests/test_investment_reports_mcp.py::test_add_items_impl_rejects_published_report tests/test_investment_reports_mcp.py::test_update_impl_updates_draft_summary_and_snapshots tests/test_investment_reports_mcp.py::test_update_impl_rejects_empty_update -q
```

Expected: FAIL with import errors for the two new handlers.

- [x] **Step 3: Add imports and tool names**

In `app/mcp_server/tooling/investment_reports_handlers.py`, extend schema imports.

```python
    ActivateWatchRequest,
    AddReportItemsRequest,
    IngestReportItem,
    IngestReportRequest,
    InvestmentReportActivateWatchResponse,
    InvestmentReportBundle,
    InvestmentReportCreateResponse,
    InvestmentReportDecideItemResponse,
    InvestmentReportItemDecisionResponse,
    InvestmentReportItemResponse,
    InvestmentReportResponse,
    InvestmentWatchAlertResponse,
    InvestmentWatchEventResponse,
    PreviousReportContextResponse,
    RecordDecisionRequest,
    SetReportStatusRequest,
    UpdateDraftReportRequest,
)
```

Extend service imports.

```python
from app.services.investment_reports.ingestion import (
    DraftReportMutationBlockedError,
    InvestmentReportIngestionService,
)
```

Extend `INVESTMENT_REPORT_TOOL_NAMES`.

```python
    "investment_report_add_items",
    "investment_report_update",
```

- [x] **Step 4: Add MCP descriptions**

Add constants near `CREATE_DESCRIPTION`.

```python
ADD_ITEMS_DESCRIPTION = (
    "ROB-499 - append items to an existing draft investment_report without "
    "recreating the report. Draft-only: non-draft reports return "
    "error:'not_draft'. items[] use the same contract as investment_report_create; "
    "duplicate client_item_key rows are returned as existing items and are not "
    "rewritten. No broker / order / watch mutation."
)

UPDATE_DESCRIPTION = (
    "ROB-499 - update draft report header fields (title, summary, risk_summary, "
    "thesis_text, no_action_note, market_snapshot, portfolio_snapshot, metadata, "
    "valid_until). Draft-only: non-draft reports return error:'not_draft'. "
    "Does not change report identity, status, previous_report_uuid, account scope, "
    "generator_version, or items. No broker / order / watch mutation."
)
```

- [x] **Step 5: Add handler implementations**

Add these implementations after `investment_report_create_impl`.

```python
async def investment_report_add_items_impl(
    report_uuid: str,
    items: list[dict[str, Any]] | None = None,
    actor: str | None = None,
) -> dict:
    validated_items, item_error = _validate_report_items(items)
    if item_error is not None:
        return item_error
    try:
        request = AddReportItemsRequest.model_validate(
            {
                "report_uuid": report_uuid,
                "items": validated_items,
                "actor": actor,
            }
        )
    except ValidationError as exc:
        return {"success": False, "error": "invalid_request", "detail": str(exc)}

    async with AsyncSessionLocal() as db:
        service = InvestmentReportIngestionService(db)
        try:
            report, inserted, existing = await service.add_items_to_draft(
                report_uuid=request.report_uuid,
                items=request.items,
            )
        except DraftReportMutationBlockedError as exc:
            return {
                "success": False,
                "error": "not_draft",
                "report_uuid": str(exc.report_uuid),
                "status": exc.status,
            }
        if report is None:
            return {
                "success": False,
                "error": "not_found",
                "report_uuid": str(request.report_uuid),
            }
        await db.commit()

        return {
            "success": True,
            "report_uuid": str(report.report_uuid),
            "inserted_count": len(inserted),
            "existing_count": len(existing),
            "inserted_items": [
                InvestmentReportItemResponse.model_validate(it).model_dump(
                    mode="json", by_alias=True
                )
                for it in inserted
            ],
            "existing_items": [
                InvestmentReportItemResponse.model_validate(it).model_dump(
                    mode="json", by_alias=True
                )
                for it in existing
            ],
        }


async def investment_report_update_impl(
    report_uuid: str,
    title: str | None = None,
    summary: str | None = None,
    risk_summary: str | None = None,
    thesis_text: str | None = None,
    no_action_note: str | None = None,
    market_snapshot: dict[str, Any] | None = None,
    portfolio_snapshot: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    valid_until: str | None = None,
    actor: str | None = None,
    reason: str | None = None,
) -> dict:
    try:
        request = UpdateDraftReportRequest.model_validate(
            {
                "report_uuid": report_uuid,
                "title": title,
                "summary": summary,
                "risk_summary": risk_summary,
                "thesis_text": thesis_text,
                "no_action_note": no_action_note,
                "market_snapshot": market_snapshot,
                "portfolio_snapshot": portfolio_snapshot,
                "metadata": metadata,
                "valid_until": valid_until,
                "actor": actor,
                "reason": reason,
            }
        )
    except ValidationError as exc:
        return {"success": False, "error": "invalid_request", "detail": str(exc)}

    updates = request.model_dump(
        exclude={"report_uuid", "actor", "reason"},
        exclude_none=True,
    )
    async with AsyncSessionLocal() as db:
        service = InvestmentReportIngestionService(db)
        try:
            report = await service.update_draft_report(
                report_uuid=request.report_uuid,
                updates=updates,
                actor=request.actor,
                reason=request.reason,
            )
        except DraftReportMutationBlockedError as exc:
            return {
                "success": False,
                "error": "not_draft",
                "report_uuid": str(exc.report_uuid),
                "status": exc.status,
            }
        if report is None:
            return {
                "success": False,
                "error": "not_found",
                "report_uuid": str(request.report_uuid),
            }
        await db.commit()
        response = InvestmentReportResponse.model_validate(report)
    return {"success": True, "report": response.model_dump(mode="json", by_alias=True)}
```

- [x] **Step 6: Register tools and export handlers**

In `register_investment_report_tools`, register after `investment_report_create`.

```python
    mcp.tool(
        name="investment_report_add_items",
        description=ADD_ITEMS_DESCRIPTION,
    )(investment_report_add_items_impl)
    mcp.tool(
        name="investment_report_update",
        description=UPDATE_DESCRIPTION,
    )(investment_report_update_impl)
```

Extend `__all__`.

```python
    "investment_report_add_items_impl",
    "investment_report_update_impl",
```

- [x] **Step 7: Verify MCP tests pass**

Run:

```bash
uv run pytest tests/test_investment_reports_mcp.py -q
```

Expected: PASS.

- [x] **Step 8: Commit**

```bash
git add app/mcp_server/tooling/investment_reports_handlers.py tests/test_investment_reports_mcp.py
git commit -m "feat: expose draft investment report mutation tools"
```

---

## Task 5: Guard Tool Descriptions

**Files:**
- Modify: `tests/mcp_server/test_investment_report_tool_descriptions.py`

- [x] **Step 1: Add failing description tests**

Append these tests to `tests/mcp_server/test_investment_report_tool_descriptions.py`.

```python
def test_draft_mutation_descriptions_state_draft_only_and_no_broker_mutation():
    captured = _capture(handlers.register_investment_report_tools)

    for name in ("investment_report_add_items", "investment_report_update"):
        desc = captured[name]
        assert "Draft-only" in desc
        assert "No broker / order / watch mutation" in desc


def test_add_items_description_mentions_duplicate_client_item_key():
    desc = _capture(handlers.register_investment_report_tools)[
        "investment_report_add_items"
    ]
    assert "duplicate client_item_key" in desc
```

- [x] **Step 2: Run focused tests**

Run:

```bash
uv run pytest tests/mcp_server/test_investment_report_tool_descriptions.py -q
```

Expected: PASS after Task 4 descriptions are in place.

- [x] **Step 3: Commit**

```bash
git add tests/mcp_server/test_investment_report_tool_descriptions.py
git commit -m "test: guard draft report mutation tool descriptions"
```

---

## Task 6: Update MCP README and Run Full Verification

**Files:**
- Modify: `app/mcp_server/README.md`

- [x] **Step 1: Add README bullets**

In `app/mcp_server/README.md`, add these bullets in the Tools section near the other investment-report references.

```markdown
- `investment_report_add_items(report_uuid, items, actor=None)` - Append new proposal items to an existing draft investment report. The item payload contract matches `investment_report_create`. Duplicate `client_item_key` rows are returned as existing items and are not rewritten. Non-draft reports return `error="not_draft"`. No broker, order, or watch mutation is performed.
- `investment_report_update(report_uuid, title=None, summary=None, risk_summary=None, thesis_text=None, no_action_note=None, market_snapshot=None, portfolio_snapshot=None, metadata=None, valid_until=None, actor=None, reason=None)` - Update draft report header fields without changing report identity, lifecycle status, predecessor chain, account scope, generator version, or items. Each successful update appends an audit entry to `report.metadata.draft_updates`. Non-draft reports return `error="not_draft"`.
```

- [x] **Step 2: Run focused verification**

Run:

```bash
uv run pytest tests/test_investment_reports_schemas.py tests/test_investment_reports_repository.py tests/test_investment_reports_ingestion.py tests/test_investment_reports_mcp.py tests/mcp_server/test_investment_report_tool_descriptions.py -q
```

Expected: PASS.

- [x] **Step 3: Run static checks**

Run:

```bash
make lint
```

Expected: PASS.

- [x] **Step 4: Run the project test target if time allows**

Run:

```bash
make test-unit
```

Expected: PASS. If runtime is too long, record the last passing focused pytest command and the reason this broader target was skipped.

- [x] **Step 5: Commit**

```bash
git add app/mcp_server/README.md
git commit -m "docs: document draft report mutation tools"
```

---

## Self-Review Checklist

- Spec coverage:
  - `investment_report_add_items(report_uuid, items[])`: Task 3 service, Task 4 MCP, Task 6 README.
  - `client_item_key` idempotency: Task 2 metadata lookup, Task 3 metadata mirror and duplicate tests.
  - `investment_report_update(report_uuid, summary?/market_snapshot?/portfolio_snapshot?)`: Task 1 schema, Task 3 service, Task 4 MCP.
  - Draft-only gate: Task 3 service test and Task 4 MCP error test.
  - Audit preservation: Task 3 `draft_updates` metadata test.
  - MCP docs synchronized: Task 5 description tests and Task 6 README.
- Placeholder scan: no deferred implementation markers are intentionally left in this plan.
- Type consistency:
  - Handler names: `investment_report_add_items_impl`, `investment_report_update_impl`.
  - Schema names: `AddReportItemsRequest`, `UpdateDraftReportRequest`.
  - Service names: `add_items_to_draft`, `update_draft_report`.
  - Error name: `DraftReportMutationBlockedError`.
