# ROB-352 Slice B — snapshot evidence & prior-report hygiene (design)

**Status:** approved 2026-05-29. Follows Slice A (PR #994, merged 93c3f8b0).

**Goal:** make "snapshot-backed" reports actually auditable — per-item snapshot citations, populated report-level market/portfolio provenance, and prior-report context that excludes smoke/draft boilerplate. Three independent changes shipped in one PR.

## Scope decisions (locked)

1. **Per-item citations** → new `cited_snapshot_uuids` ARRAY(UUID) column (not response-time derivation).
2. **market/portfolio_snapshot** → compact provenance descriptor (not full payload copy).
3. **Smoke filter** → exclude `status='draft'` reports from `prior_reports` auto-context.

Out of scope: Slice C (candidate quality, ROB-346); the supersede/revision model for overwrite (separate follow-up); production smoke. No broker/order/watch/order-intent/trade-journal mutation; no scheduler.

## Change 1 — per-item `cited_snapshot_uuids`

**Migration** (additive, mirrors ROB-308 `cited_dimension_report_uuids`): on `review.investment_report_items`,
`cited_snapshot_uuids ARRAY(sa.UUID())`, `server_default ARRAY[]::uuid[]`, `nullable=False`. down_revision = `20260527_rob329` (current head). up adds column; down drops it.

**ORM** (`app/models/investment_reports.py`, `InvestmentReportItem`): add
```python
cited_snapshot_uuids: Mapped[list[uuid.UUID]] = mapped_column(
    ARRAY(PG_UUID(as_uuid=True)), nullable=False, server_default=text("ARRAY[]::uuid[]"),
)
```
next to `cited_dimension_report_uuids`.

**Schema** (`IngestReportItem`): add `cited_snapshot_uuids: list[UUID] = Field(default_factory=list)`.
**Response** (`InvestmentReportItemResponse`): add `cited_snapshot_uuids: list[UUID]`.
**Repository** (`insert_item`): forward `cited_snapshot_uuids=list(item_req.cited_snapshot_uuids)`.

**Population (deterministic, in the generator):** in `_build_ingest_request`, for each item, if the caller did **not** supply `cited_snapshot_uuids`, derive it from the item's `evidence_snapshot`: collect the value at key `snapshot_uuid` plus any key matching `*_snapshot_uuid` (e.g. `candidate_snapshot_uuid`, `portfolio_snapshot_uuid`, `news_snapshot_uuid`). Parse each to `UUID`, skip non-UUID/None values, dedupe preserving first-seen order. Caller-supplied non-empty lists win (Hermes may set its own). Helper: `_extract_cited_snapshot_uuids(evidence_snapshot) -> list[UUID]`.

## Change 2 — report-level market/portfolio provenance

Currently `_build_ingest_request` hardcodes `market_snapshot={}` / `portfolio_snapshot={}`. Replace with a descriptor built from the frozen bundle.

New generator helper `_section_snapshot_descriptors(bundle_uuid, unavailable_sources) -> tuple[dict, dict]`: read `list_bundle_items_with_snapshots(bundle.id)`; for the snapshot of kind `market` (resp. `portfolio`), build
```python
{"snapshot_uuid": str(s.snapshot_uuid), "snapshot_kind": s.snapshot_kind,
 "as_of": iso(s.as_of), "freshness_status": s.freshness_status,
 "coverage": s.coverage_json or {}}
```
If the kind is absent from the bundle, store `{"status": "unavailable", "reason": <reason from unavailable_sources[kind] or "not_collected">}`. No full `payload_json` copy — snapshots are reusable artifacts (`investment_snapshots`); the report stores a pointer + freshness, not a duplicate.

The generator already loads bundle items for classifier context; reuse that read (one query) rather than adding a second pass where practical.

## Change 3 — prior_reports draft exclusion

`InvestmentReportQueryService.previous_report_context`: after fetching prior reports, drop any with `status == "draft"` before slicing to `n_prior`. Fetch a larger buffer (`n_prior + DRAFT_FETCH_BUFFER`, e.g. +5) from `list_reports` so dropping drafts/excluded still yields up to `n_prior` published rows. Published/decided/expired/superseded continue to flow. This removes `hermes-smoke-*` boilerplate (created as drafts) from automatic next-report context.

## Testing

- **Migration**: `alembic upgrade head` then `downgrade -1` round-trip clean (operator-run; CI applies upgrade).
- **Citation derivation** (generator unit): item whose `evidence_snapshot` has `snapshot_uuid` + `candidate_snapshot_uuid` → both in `cited_snapshot_uuids`, deduped, non-UUID skipped; caller-supplied list preserved.
- **Round-trip** (ingestion/repo, real DB): ingest item with `cited_snapshot_uuids` → persisted + read back.
- **Section descriptors** (generator unit): bundle with market+portfolio snapshots → descriptors with uuid/freshness; missing portfolio → `{status:"unavailable", reason}`.
- **prior_reports** (query_service, real DB): draft excluded, published retained, `n_prior` honored across a draft-heavy set.
- **Test fixture**: ensure the `tests/_investment_reports_helpers.py` session fixture creates/patches the new `cited_snapshot_uuids` column (it idempotently patches ROB-269/274/318 columns).

## Files

- Create: `alembic/versions/<rev>_rob352_cited_snapshot_uuids.py`
- Modify: `app/models/investment_reports.py`, `app/schemas/investment_reports.py`, `app/services/investment_reports/repository.py`, `app/services/action_report/snapshot_backed/generator.py`, `app/services/investment_reports/query_service.py`, `tests/_investment_reports_helpers.py`
- Tests: `tests/services/action_report/snapshot_backed/test_generator.py`, `tests/test_investment_reports_ingestion.py` (or repo test), `tests/` query_service prior_reports test.
