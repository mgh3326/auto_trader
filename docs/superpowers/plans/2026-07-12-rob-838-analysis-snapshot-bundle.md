# ROB-838 Analysis Snapshot Bundle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture every re-analysis data surface once into an immutable, hash-verified bundle and expose a DB-only MCP read path that returns the stored document verbatim.

**Architecture:** Reuse ROB-287's append-only `InvestmentSnapshot` and the existing `llm_input_frozen` kind. A dedicated collector composes the full versioned document from existing read-only collectors plus full symbol analysis and decision history, while `SnapshotBundleEnsureService(mode="create_new")` persists a new bundle without reuse. A separate read service verifies `canonical_payload_hash`, projects optional section keys without transforming values, and computes age/stale metadata solely from persisted timestamps.

**Tech Stack:** Python 3.13+, Pydantic v2, SQLAlchemy async, FastMCP, pytest/pytest-asyncio, Ruff, ty, `uv`.

## Global Constraints

- `analysis_bundle_get` must perform zero provider calls and zero recomputation of stored data.
- Persist one `llm_input_frozen` snapshot per analysis bundle; expose its full 64-character canonical SHA-256 as `content_hash`.
- Existing bundle/snapshot rows remain append-only; corrections create a new bundle UUID.
- Every public section is present and records `status`, `collected_at`, `as_of`, `source`, and exact `data` or exact `error`.
- `sections=[...]` is projection-only; it must not reshape or recalculate section values.
- Freshness is response metadata. Reading stale data never refreshes or fills it.
- No order, proposal, watch, report, or execution mutation imports/calls.
- Real broker/provider reads are mocked in tests.
- New MCP surface is gated by `ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED=False`.
- Consumer/headless profile exposes `analysis_bundle_get` only; it does not expose `analysis_bundle_create`.
- No database migration: `llm_input_frozen`, canonical hash, and bundle linkage already exist.
- Keep existing ROB-287/Hermes behavior and `intraday_action_report_v1` unchanged.

---

## File map

- Create `app/schemas/analysis_snapshot_bundle.py`: request/document/response contracts and stable section names.
- Create `app/services/analysis_snapshot_bundle/__init__.py`: narrow public exports.
- Create `app/services/analysis_snapshot_bundle/capture.py`: one-shot section capture and frozen snapshot collector.
- Create `app/services/analysis_snapshot_bundle/read.py`: DB-only integrity verification, filtering, and age metadata.
- Create `app/mcp_server/tooling/analysis_bundle_handlers.py`: thin create/get MCP handlers and registration.
- Modify `app/services/investment_snapshots/policy.py`: add `analysis_snapshot_bundle_v1` policy for `llm_input_frozen` only.
- Modify `app/schemas/investment_snapshots_mcp.py`: add `create_new` ensure mode.
- Modify `app/services/action_report/common/snapshot_bundle.py`: bypass reuse only for `create_new`.
- Modify `app/core/config.py`: add the default-off feature gate.
- Modify `app/mcp_server/tooling/{__init__,registry,analysis_readonly_registration,route_request_lanes}.py`: registration and safety classification.
- Modify `app/mcp_server/README.md`: public contract, immutable/hash/freshness semantics, and ROB-833 handoff.
- Create `tests/services/analysis_snapshot_bundle/test_capture.py`: one-shot capture, partial errors, metadata, hash/persistence.
- Create `tests/services/analysis_snapshot_bundle/test_read.py`: exact read, hash verification, filtering, freshness.
- Create `tests/mcp_server/test_analysis_bundle_tools.py`: handler delegation and gate behavior.
- Modify `tests/test_mcp_tool_registration.py`, `tests/test_mcp_profiles.py`, `tests/test_route_request_registry_diff.py`, and `tests/services/investment_snapshots/test_bundle_ensure_service.py`.

---

### Task 1: Lock DTOs, create-new persistence mode, and bundle policy

**Files:**
- Create: `app/schemas/analysis_snapshot_bundle.py`
- Modify: `app/schemas/investment_snapshots_mcp.py`
- Modify: `app/services/investment_snapshots/policy.py`
- Modify: `app/services/action_report/common/snapshot_bundle.py`
- Test: `tests/services/investment_snapshots/test_bundle_ensure_service.py`
- Test: `tests/services/analysis_snapshot_bundle/test_schemas.py`

**Interfaces:**
- Produces: `ANALYSIS_SECTION_NAMES`, `AnalysisBundleCreateRequest`, `AnalysisSection`, `AnalysisFrozenDocument`, `AnalysisBundleCreateResponse`, `AnalysisBundleGetResponse`.
- Produces: `EnsureMode = Literal["ensure_fresh", "reuse_only", "create_new"]`.
- Produces: policy version `analysis_snapshot_bundle_v1` with required kind `llm_input_frozen` and 180/300-second soft/hard TTL.
- Consumes: existing `SnapshotBundleEnsureService.ensure()` persistence flow and canonical hash repository.

- [ ] **Step 1: Write failing schema and create-new tests**

Create `tests/services/analysis_snapshot_bundle/test_schemas.py` with these exact assertions:

```python
import datetime as dt

import pytest
from pydantic import ValidationError

from app.schemas.analysis_snapshot_bundle import (
    ANALYSIS_SECTION_NAMES,
    AnalysisBundleCreateRequest,
    AnalysisFrozenDocument,
    AnalysisSection,
)


def test_analysis_section_names_are_stable() -> None:
    assert ANALYSIS_SECTION_NAMES == (
        "portfolio",
        "quotes_orderbooks",
        "indicators_support_resistance",
        "market_gate_inputs",
        "investor_flow",
        "decision_history",
    )


def test_frozen_document_requires_every_section() -> None:
    now = dt.datetime(2026, 7, 12, tzinfo=dt.UTC)
    section = AnalysisSection(
        status="ok",
        collected_at=now,
        as_of=now,
        source={"service": "test"},
        soft_ttl_seconds=60,
        hard_ttl_seconds=180,
        data={"x": 1},
    )
    with pytest.raises(ValidationError):
        AnalysisFrozenDocument(
            captured_at=now,
            request=AnalysisBundleCreateRequest(
                market="kr", account_scope="kis_live", symbols=["005930"]
            ),
            sections={"portfolio": section},
        )


def test_unavailable_section_keeps_original_error() -> None:
    now = dt.datetime(2026, 7, 12, tzinfo=dt.UTC)
    section = AnalysisSection(
        status="unavailable",
        collected_at=now,
        as_of=now,
        source={"provider": "breadth"},
        soft_ttl_seconds=180,
        hard_ttl_seconds=300,
        error="RuntimeError: provider off",
    )
    assert section.data is None
    assert section.error == "RuntimeError: provider off"


def test_create_request_rejects_empty_symbols() -> None:
    with pytest.raises(ValidationError):
        AnalysisBundleCreateRequest(
            market="crypto", account_scope="upbit_live", symbols=[]
        )
```

Append to `tests/services/investment_snapshots/test_bundle_ensure_service.py` a test that seeds a fresh prior bundle, registers a fake `llm_input_frozen` collector, and calls this exact request:

```python
request = EnsureBundleRequest(
    purpose="analysis_recheck",
    market="kr",
    account_scope="kis_live",
    policy_version="analysis_snapshot_bundle_v1",
    mode="create_new",
    symbols=["005930"],
    requested_by="claude_code",
    user_id=7,
)
response = await service.ensure(request)
assert response.created is True
assert response.bundle_uuid != prior.bundle_uuid
assert collector.calls == 1
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/services/analysis_snapshot_bundle/test_schemas.py tests/services/investment_snapshots/test_bundle_ensure_service.py -q
```

Expected: collection fails because `app.schemas.analysis_snapshot_bundle` and `create_new` do not exist.

- [ ] **Step 3: Add the exact DTO contract**

Create `app/schemas/analysis_snapshot_bundle.py` with:

```python
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.investment_snapshots import SnapshotAccountScope, SnapshotMarket

AnalysisSectionName = Literal[
    "portfolio",
    "quotes_orderbooks",
    "indicators_support_resistance",
    "market_gate_inputs",
    "investor_flow",
    "decision_history",
]
ANALYSIS_SECTION_NAMES: tuple[AnalysisSectionName, ...] = (
    "portfolio",
    "quotes_orderbooks",
    "indicators_support_resistance",
    "market_gate_inputs",
    "investor_flow",
    "decision_history",
)


class AnalysisBundleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market: SnapshotMarket
    account_scope: SnapshotAccountScope | None = None
    symbols: list[str] = Field(min_length=1, max_length=10)
    user_id: int | None = None
    market_session: str | None = None
    requested_by: Literal["user", "claude_code", "reviewer"] = "claude_code"


class AnalysisSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok", "partial", "unavailable"]
    collected_at: dt.datetime
    as_of: dt.datetime
    source: dict[str, Any]
    soft_ttl_seconds: int = Field(gt=0)
    hard_ttl_seconds: int = Field(gt=0)
    data: Any | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_payload_or_error(self) -> Self:
        if self.status == "unavailable" and not self.error:
            raise ValueError("unavailable section requires error")
        if self.status != "unavailable" and self.data is None:
            raise ValueError("available section requires data")
        if self.hard_ttl_seconds < self.soft_ttl_seconds:
            raise ValueError("hard TTL must be >= soft TTL")
        return self


class AnalysisFrozenDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["analysis-snapshot-bundle.v1"] = (
        "analysis-snapshot-bundle.v1"
    )
    captured_at: dt.datetime
    request: AnalysisBundleCreateRequest
    sections: dict[AnalysisSectionName, AnalysisSection]

    @model_validator(mode="after")
    def require_all_sections(self) -> Self:
        if set(self.sections) != set(ANALYSIS_SECTION_NAMES):
            raise ValueError("frozen document must contain every analysis section")
        return self


class AnalysisBundleCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundle_id: uuid.UUID
    content_hash: str = Field(min_length=64, max_length=64)
    status: Literal["complete", "partial"]
    captured_at: dt.datetime
    unavailable_sections: list[AnalysisSectionName]
    partial_sections: list[AnalysisSectionName]


class AnalysisSectionFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: dt.datetime
    age_seconds: float = Field(ge=0)
    status: Literal["fresh", "soft_stale", "hard_stale"]
    source: dict[str, Any]
    capture_status: Literal["ok", "partial", "unavailable"]


class AnalysisBundleGetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundle_id: uuid.UUID
    content_hash: str
    integrity_verified: Literal[True] = True
    created_at: dt.datetime
    captured_at: dt.datetime
    read_at: dt.datetime
    age_seconds: float = Field(ge=0)
    status: Literal["complete", "partial"]
    completeness: dict[str, list[str]]
    stale_warning: bool
    section_freshness: dict[str, AnalysisSectionFreshness]
    document: dict[str, Any]
```

- [ ] **Step 4: Add create-new mode and the dedicated policy**

Change `EnsureMode` in `app/schemas/investment_snapshots_mcp.py` to include `"create_new"`.

In `app/services/investment_snapshots/policy.py`, add `ANALYSIS_SNAPSHOT_BUNDLE_V1` with bundle TTL 180/300 seconds and exactly one required `SnapshotKindPolicy(snapshot_kind="llm_input_frozen", freshness=FreshnessPolicy(180, 300), collector_timeout=60 seconds)`, then add it to `POLICIES`.

In `SnapshotBundleEnsureService.ensure`, wrap the latest-bundle reuse lookup in:

```python
latest = None
if request.mode != "create_new":
    latest = await self._repo.find_latest_bundle(
        purpose=request.purpose,
        market=request.market,
        account_scope=request.account_scope,
        policy_version=policy.policy_version,
    )
```

Leave `reuse_only` and `ensure_fresh` behavior byte-for-byte equivalent outside that branch.

- [ ] **Step 5: Run tests and verify GREEN**

Run the Task 1 command again. Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add app/schemas/analysis_snapshot_bundle.py app/schemas/investment_snapshots_mcp.py app/services/investment_snapshots/policy.py app/services/action_report/common/snapshot_bundle.py tests/services/analysis_snapshot_bundle/test_schemas.py tests/services/investment_snapshots/test_bundle_ensure_service.py
git commit -m "feat(ROB-838): define frozen analysis bundle contract"
```

---

### Task 2: Capture all sections once and persist one immutable snapshot

**Files:**
- Create: `app/services/analysis_snapshot_bundle/__init__.py`
- Create: `app/services/analysis_snapshot_bundle/capture.py`
- Modify: `app/services/action_report/snapshot_backed/collectors/market.py`
- Test: `tests/services/analysis_snapshot_bundle/test_capture.py`
- Test: `tests/services/action_report/snapshot_backed/test_collectors.py`
- Test: `tests/services/investment_snapshots/test_mutation_boundary.py`

**Interfaces:**
- Consumes: `SnapshotCollectorRegistry`, `CollectorRequest`, `SnapshotCollectResult`, `AnalysisFrozenDocument`, `SnapshotBundleEnsureService`.
- Produces: `AnalysisInputFrozenCollector.collect(request) -> list[SnapshotCollectResult]`.
- Produces: `AnalysisBundleCaptureService.capture(request) -> AnalysisBundleCreateResponse`.
- Dependency seams: `analysis_fn(symbols, market)`, `decision_history_fn(symbol, market, account_scope)`, and the existing registry's `portfolio`, `symbol`, `market`, `investor_flow` collectors.

- [ ] **Step 1: Write failing capture tests with mocked providers**

Create fakes implementing `snapshot_kind` and `collect()`, plus fixtures named `service`, `request`, `repo`, `bundle`, `analysis_fn`, `decision_history_fn`, and the four collector fakes. Test these behaviors:

```python
@pytest.mark.asyncio
async def test_capture_persists_one_frozen_snapshot_with_all_sections(
    service, request, repo, bundle
):
    response = await service.capture(request)
    assert response.status == "complete"
    pairs = await repo.list_bundle_items_with_snapshots(bundle.id)
    assert len(pairs) == 1
    snapshot = pairs[0][1]
    assert snapshot.snapshot_kind == "llm_input_frozen"
    assert snapshot.canonical_payload_hash == canonical_payload_hash(
        snapshot.payload_json
    )
    assert set(snapshot.payload_json["sections"]) == set(ANALYSIS_SECTION_NAMES)


@pytest.mark.asyncio
async def test_capture_stores_provider_error_without_retry(
    service, request, investor_flow, load_frozen_document
):
    investor_flow.collect.side_effect = RuntimeError("provider off")
    response = await service.capture(request)
    document = await load_frozen_document(response.bundle_id)
    section = document["sections"]["investor_flow"]
    assert section["status"] == "unavailable"
    assert section["error"] == "RuntimeError: provider off"
    investor_flow.collect.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_uses_full_analysis_and_separate_decision_history(
    service, request, analysis_fn, decision_history_fn
):
    await service.capture(request)
    analysis_fn.assert_awaited_once_with(
        ["005930"], market="kr", include_peers=False, quick=False,
        include_position=False, refresh=False
    )
    decision_history_fn.assert_awaited_once_with(
        "005930", "kr", "kis_live"
    )


@pytest.mark.asyncio
async def test_second_capture_returns_new_bundle_uuid(service, request):
    first = await service.capture(request)
    second = await service.capture(request)
    assert first.bundle_id != second.bundle_id
```

Add a mutation-boundary assertion that `capture.py` contains none of `place_order`, `cancel_order`, `modify_order`, `order_proposal`, `investment_report_create`, `watch_create`, or `execution_service` imports.

In `tests/services/action_report/snapshot_backed/test_collectors.py`, inject an `altseason_fn` that raises `RuntimeError("provider off")` and assert the market result has `freshness_status == "partial"` and `errors_json["altseason"] == "RuntimeError: provider off"` while its events/index payload remains present.

- [ ] **Step 2: Run capture tests and verify RED**

```bash
uv run pytest tests/services/analysis_snapshot_bundle/test_capture.py tests/services/investment_snapshots/test_mutation_boundary.py -q
```

Expected: import failure because the capture service does not exist.

- [ ] **Step 3: Implement section wrapping without altering source values**

In `capture.py`, define fixed TTLs:

```python
_SECTION_TTLS: dict[str, tuple[int, int]] = {
    "portfolio": (180, 300),
    "quotes_orderbooks": (60, 180),
    "indicators_support_resistance": (60, 180),
    "market_gate_inputs": (180, 300),
    "investor_flow": (900, 86400),
    "decision_history": (300, 900),
}
```

Define `_error_text(exc)` as `f"{type(exc).__name__}: {str(exc).strip() or type(exc).__name__}"`. Define `_unavailable_section(name, now, source, error)` and `_available_section(name, now, as_of, source, data, partial=False)` that return `AnalysisSection` without modifying `data`.

For collector-backed sections, call exactly one collector per source. Convert zero results, a raised exception, or results whose worst freshness is `unavailable` into one unavailable section. A result with `freshness_status="partial"`, non-empty `errors_json`, or a symbol quote whose nested `quote.status` is not `ok` makes the section `partial` and preserves the exact diagnostic in the section `error`. Preserve the raw list of each collector result as:

```python
{
    "payload_json": result.payload_json,
    "errors_json": result.errors_json,
    "coverage_json": result.coverage_json,
    "source_timestamps_json": result.source_timestamps_json,
    "freshness_status": result.freshness_status,
    "symbol": result.symbol,
}
```

Map sources exactly:

- `portfolio` collector → `portfolio`
- `symbol` collector → `quotes_orderbooks`
- full `analysis_fn(symbols, market=market, include_peers=False, quick=False, include_position=False, refresh=False)` → `indicators_support_resistance`
- `market` collector → `market_gate_inputs`
- `investor_flow` collector → `investor_flow`
- per-symbol `decision_history_fn` map → `decision_history`

`AnalysisInputFrozenCollector.collect()` must use `asyncio.gather` once for the six section coroutines with each coroutine catching its own exception. It returns exactly one `SnapshotCollectResult`:

```python
return [
    SnapshotCollectResult(
        snapshot_kind="llm_input_frozen",
        market=request.market,
        account_scope=request.account_scope,
        source_kind="combined",
        payload_json=document.model_dump(mode="json"),
        source_timestamps_json={
            name: section.as_of.isoformat()
            for name, section in document.sections.items()
        },
        coverage_json={
            "complete_sections": complete,
            "partial_sections": partial,
            "unavailable_sections": unavailable,
        },
        errors_json={
            name: section.error
            for name, section in document.sections.items()
            if section.error is not None
        },
        as_of=captured_at,
        freshness_status="partial" if unavailable or partial else "fresh",
    )
]
```

Before wiring the frozen collector, change `MarketEventsSnapshotCollector._collect_altseason()` to return `(payload, error)` instead of swallowing the exception. On exception, return `(None, _error_text(exc))`; on success return `(payload, None)`. `collect()` adds `errors={"altseason": error}` and `freshness_status="partial"` when error is non-null. This is additive for existing report bundles and is the only existing collector change required to retain the original provider-off reason.

The decision-history section runs one `build_decision_context` call per symbol with `asyncio.gather(return_exceptions=True)`. Store successful values keyed by symbol. Store each raised exception as `{"status": "unavailable", "error": _error_text(exc)}` under that symbol; mark the section `partial` when some symbols fail and `unavailable` only when all symbols fail.

- [ ] **Step 4: Implement `AnalysisBundleCaptureService` orchestration**

Its constructor receives the DB session, base collector registry, analysis function, decision-history function, and clock. `capture()` creates a fresh registry containing only `AnalysisInputFrozenCollector`, calls `SnapshotBundleEnsureService.ensure()` with:

```python
EnsureBundleRequest(
    purpose="analysis_recheck",
    market=request.market,
    account_scope=request.account_scope,
    policy_version="analysis_snapshot_bundle_v1",
    mode="create_new",
    symbols=request.symbols,
    market_session=request.market_session,
    requested_by=request.requested_by,
    user_id=request.user_id,
)
```

After ensure, load the single linked snapshot through the repository, assert kind/count/purpose, and return its stored canonical hash plus completeness derived from the stored payload. Do not commit inside the service; MCP owns transaction commit/rollback.

Export only `AnalysisBundleCaptureService` and `AnalysisInputFrozenCollector` from `app/services/analysis_snapshot_bundle/__init__.py`.

- [ ] **Step 5: Run Task 2 tests and verify GREEN**

Run the Task 2 command. Expected: all pass, broker/provider fakes each called once.

- [ ] **Step 6: Commit Task 2**

```bash
git add app/services/analysis_snapshot_bundle app/services/action_report/snapshot_backed/collectors/market.py tests/services/analysis_snapshot_bundle/test_capture.py tests/services/action_report/snapshot_backed/test_collectors.py tests/services/investment_snapshots/test_mutation_boundary.py
git commit -m "feat(ROB-838): capture immutable analysis bundle"
```

---

### Task 3: Add DB-only hash-verified read and projection-only section filter

**Files:**
- Create: `app/services/analysis_snapshot_bundle/read.py`
- Test: `tests/services/analysis_snapshot_bundle/test_read.py`

**Interfaces:**
- Produces: `AnalysisBundleReadService.get(bundle_id: UUID, sections: list[AnalysisSectionName] | None = None) -> AnalysisBundleGetResponse`.
- Produces exceptions: `AnalysisBundleNotFound`, `AnalysisBundleIntegrityError`, `UnknownAnalysisBundleSection`.
- Consumes only `InvestmentSnapshotsRepository` SELECT methods and `canonical_payload_hash`.

- [ ] **Step 1: Write failing read tests**

Seed a persisted `analysis_recheck` bundle and one frozen snapshot using repository inserts. Define fixtures `service`, `seed_bundle`, and `frozen_clock`; `seed_bundle` returns an object carrying `bundle_uuid`, `payload_json`, and `canonical_payload_hash`. Add tests:

```python
@pytest.mark.asyncio
async def test_get_returns_exact_stored_document(service, seed_bundle):
    stored = await seed_bundle()
    response = await service.get(stored.bundle_uuid)
    assert response.document == stored.payload_json
    assert response.content_hash == stored.canonical_payload_hash
    assert response.integrity_verified is True


@pytest.mark.asyncio
async def test_sections_filter_only_projects_stored_values(service, seed_bundle):
    stored = await seed_bundle()
    response = await service.get(stored.bundle_uuid, sections=["portfolio"])
    assert response.document["sections"] == {
        "portfolio": stored.payload_json["sections"]["portfolio"]
    }
    assert response.document["request"] == stored.payload_json["request"]


@pytest.mark.asyncio
async def test_tampered_payload_fails_closed(service, seed_bundle):
    stored = await seed_bundle()
    stored.payload_json["sections"]["portfolio"]["data"] = {"tampered": True}
    with pytest.raises(AnalysisBundleIntegrityError):
        await service.get(stored.bundle_uuid)


@pytest.mark.asyncio
async def test_unavailable_section_is_not_filled(service, seed_bundle):
    stored = await seed_bundle(investor_flow_unavailable=True)
    response = await service.get(stored.bundle_uuid)
    assert response.document["sections"]["investor_flow"] == (
        stored.payload_json["sections"]["investor_flow"]
    )


@pytest.mark.asyncio
async def test_age_and_stale_metadata_do_not_modify_document(
    service, seed_bundle, frozen_clock
):
    stored = await seed_bundle(
        captured_at=FROZEN_NOW - dt.timedelta(seconds=301)
    )
    before = copy.deepcopy(stored.payload_json)
    response = await service.get(stored.bundle_uuid)
    assert response.stale_warning is True
    assert response.section_freshness["portfolio"].status == "hard_stale"
    assert response.document == before
```

Add the following edge assertions using the same fixture controls:

```python
with pytest.raises(AnalysisBundleNotFound):
    await service.get(uuid.uuid4())

wrong = await seed_bundle(purpose="report_generation")
with pytest.raises(AnalysisBundleIntegrityError):
    await service.get(wrong.bundle_uuid)

empty = await seed_bundle(item_count=0)
with pytest.raises(AnalysisBundleIntegrityError):
    await service.get(empty.bundle_uuid)

multiple = await seed_bundle(item_count=2)
with pytest.raises(AnalysisBundleIntegrityError):
    await service.get(multiple.bundle_uuid)

malformed = await seed_bundle(malformed=True)
with pytest.raises(AnalysisBundleIntegrityError):
    await service.get(malformed.bundle_uuid)

stored = await seed_bundle()
with pytest.raises(UnknownAnalysisBundleSection):
    await service.get(stored.bundle_uuid, sections=["not_a_section"])
```

- [ ] **Step 2: Run read tests and verify RED**

```bash
uv run pytest tests/services/analysis_snapshot_bundle/test_read.py -q
```

Expected: import failure because `read.py` does not exist.

- [ ] **Step 3: Implement strict membership and integrity verification**

`get()` must:

1. load bundle by UUID or raise `AnalysisBundleNotFound`;
2. require `bundle.purpose == "analysis_recheck"`;
3. load items once with `list_bundle_items_with_snapshots`;
4. require exactly one item and `snapshot_kind == "llm_input_frozen"`;
5. recompute `canonical_payload_hash(snapshot.payload_json)` and compare with `snapshot.canonical_payload_hash` using `hmac.compare_digest`;
6. validate a deep copy with `AnalysisFrozenDocument.model_validate`;
7. reject unknown requested section names;
8. project by deep-copying only selected stored section values.

Do not import any collector, analysis tooling, provider, KIS, Upbit, or external-source module in `read.py`.

- [ ] **Step 4: Implement freshness metadata from persisted timestamps**

Use an injected UTC clock. Clamp negative age to `0.0`. Classification is:

```python
def _freshness(age_seconds: float, *, soft: int, hard: int) -> str:
    if age_seconds > hard:
        return "hard_stale"
    if age_seconds > soft:
        return "soft_stale"
    return "fresh"
```

Build `section_freshness` from each returned section's persisted `as_of`, `source`, capture status, and stored TTLs. Set `stale_warning=True` when bundle age exceeds 300 seconds or any returned section is not fresh. Completeness lists are computed from stored section statuses only and must not alter `document`.

- [ ] **Step 5: Run Task 3 tests and verify GREEN**

Run the Task 3 command. Expected: all pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add app/services/analysis_snapshot_bundle/read.py tests/services/analysis_snapshot_bundle/test_read.py
git commit -m "feat(ROB-838): verify and read frozen analysis bundles"
```

---

### Task 4: Expose default-off MCP create/get tools and isolate consumer sessions

**Files:**
- Create: `app/mcp_server/tooling/analysis_bundle_handlers.py`
- Modify: `app/core/config.py`
- Modify: `app/mcp_server/tooling/__init__.py`
- Modify: `app/mcp_server/tooling/registry.py`
- Modify: `app/mcp_server/tooling/analysis_readonly_registration.py`
- Modify: `app/mcp_server/tooling/route_request_lanes.py`
- Test: `tests/mcp_server/test_analysis_bundle_tools.py`
- Test: `tests/test_mcp_tool_registration.py`
- Test: `tests/test_mcp_profiles.py`
- Test: `tests/test_route_request_registry_diff.py`
- Test: `tests/test_route_request_lanes.py`
- Test: `tests/test_watch_triage_readonly_settings.py`
- Test: `tests/test_mcp_timeout_middleware.py`

**Interfaces:**
- Produces tools `analysis_bundle_create(market, account_scope, symbols, user_id=None, market_session=None)` and `analysis_bundle_get(bundle_id, sections=None)`.
- Default profile gets both only when the gate is enabled.
- Analysis-readonly/headless profile gets only `analysis_bundle_get` when the gate is enabled.

- [ ] **Step 1: Write failing registration and handler tests**

Test gate-off absence and gate-on presence in `build_tools()`. Test `register_analysis_bundle_tools(recorder, allow_create=False)` registers only get. Patch capture/read services and `AsyncSessionLocal` to assert handlers delegate once and commit only after successful create.

Required response mappings:

```python
assert await analysis_bundle_get_impl("not-a-uuid") == {
    "success": False,
    "error": "invalid_bundle_id",
    "bundle_id": "not-a-uuid",
}
```

Exceptions map to `analysis_bundle_not_found`, `analysis_bundle_integrity_error`, and `unknown_analysis_bundle_section`. Success is `{"success": True, **response.model_dump(mode="json")}`.

- [ ] **Step 2: Run MCP tests and verify RED**

```bash
uv run pytest tests/mcp_server/test_analysis_bundle_tools.py tests/test_mcp_tool_registration.py -q
```

Expected: tools/settings/handlers are missing.

- [ ] **Step 3: Implement thin handlers and registration**

Add `ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED: bool = False` near the existing snapshot gates in `app/core/config.py`.

In `analysis_bundle_handlers.py`:

- parse UUID before opening a session;
- create production base registry with `production_collector_registry(db)`;
- inject `analyze_stock_batch_impl` with `quick=False`, `include_position=False`, `refresh=False`;
- inject a decision-history adapter that calls `build_decision_context(db, symbol, market, account_mode=account_scope)`; do not invent an account default;
- call capture service, commit, return DTO;
- get handler instantiates only `AnalysisBundleReadService`;
- `register_analysis_bundle_tools(mcp, *, allow_create=True)` registers get always and create only when allowed.

Descriptions must say “stored payload verbatim”, “zero provider calls/recomputation on get”, “SHA-256 verified”, and “evidence append only; no order/proposal mutation”.

- [ ] **Step 4: Wire gated surfaces and safety partitions**

Add lazy exports `ANALYSIS_BUNDLE_TOOL_NAMES` and `register_analysis_bundle_tools` in tooling `__init__.py`. In default registry:

```python
if settings.ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED:
    register_analysis_bundle_tools(mcp)
```

In `analysis_readonly_registration.py`, add `analysis_bundle_get` to `ANALYSIS_READONLY_TOOL_NAMES`, import the registrar, and call it with `allow_create=False` only under the same setting. Do not add create to that profile.

Add both tools to `READ_ONLY_ADVISORY_TOOLS` because their only write is append-only evidence capture, consistent with existing `analysis_artifact_save`. Add both names to `_FLAG_GATED_OR_OPTIONAL` in `tests/test_route_request_registry_diff.py`, and update the exact `ANALYSIS_READONLY_TOOL_NAMES` expectation through the production constant exercised by `tests/test_mcp_profiles.py`.

Add `"analysis_bundle_create": 240.0` to `ELEVATED_TOOL_TIMEOUTS_S` in `app/mcp_server/timeout_middleware.py`. In `tests/test_mcp_timeout_middleware.py`, assert `ELEVATED_TOOL_TIMEOUTS_S["analysis_bundle_create"] == 240.0`.

- [ ] **Step 5: Run MCP/profile tests and verify GREEN**

Run:

```bash
uv run pytest tests/mcp_server/test_analysis_bundle_tools.py tests/test_mcp_tool_registration.py tests/test_mcp_profiles.py tests/test_route_request_registry_diff.py tests/test_route_request_lanes.py tests/test_watch_triage_readonly_settings.py tests/test_mcp_timeout_middleware.py -q
```

Expected: gate-off absent, gate-on default has create/get, gate-on readonly has get only, and the route partition remains total.

- [ ] **Step 6: Commit Task 4**

```bash
git add app/core/config.py app/mcp_server/timeout_middleware.py app/mcp_server/tooling/analysis_bundle_handlers.py app/mcp_server/tooling/__init__.py app/mcp_server/tooling/registry.py app/mcp_server/tooling/analysis_readonly_registration.py app/mcp_server/tooling/route_request_lanes.py tests/mcp_server/test_analysis_bundle_tools.py tests/test_mcp_tool_registration.py tests/test_mcp_profiles.py tests/test_route_request_registry_diff.py tests/test_route_request_lanes.py tests/test_watch_triage_readonly_settings.py tests/test_mcp_timeout_middleware.py
git commit -m "feat(ROB-838): expose gated frozen bundle MCP tools"
```

---

### Task 5: Synchronize MCP documentation and regression/safety contracts

**Files:**
- Modify: `app/mcp_server/README.md`
- Modify: `tests/test_watch_triage_readonly_settings.py` to keep create out of the consumer allowlist and permit get
- Modify: `app/mcp_server/timeout_middleware.py` to give capture a 240-second timeout
- Test: existing snapshot/Hermes suites

**Interfaces:**
- Documents the final public tool contract and ROB-833 runner handoff.
- Does not change runtime semantics.

- [ ] **Step 1: Write documentation assertions first**

Add a focused test in `tests/mcp_server/test_analysis_bundle_tools.py` that reads the registered descriptions and asserts:

```python
assert "verbatim" in get_description.lower()
assert "zero provider" in get_description.lower()
assert "sha-256" in get_description.lower()
assert "no order" in create_description.lower()
```

Keep README verification in the handler-description assertions above; this repository has no separate MCP README contract test.

- [ ] **Step 2: Run the new doc assertions and verify RED**

```bash
uv run pytest tests/mcp_server/test_analysis_bundle_tools.py -q
```

Expected: description/README assertions fail before documentation text is finalized.

- [ ] **Step 3: Document the exact API and safety semantics**

Add an MCP README section containing:

- gate name and default `false`;
- create arguments and returned `bundle_id`, `content_hash`, completeness;
- get arguments including `sections` and exact projection-only behavior;
- write-once correction rule: create a new bundle, never patch an old one;
- hash verification and integrity error behavior;
- freshness metadata/age/stale warning without refresh;
- unavailable provider error persistence without read-time fill;
- readonly/headless profile exposes get only;
- ROB-833 sequence verbatim:
  `watch/fill event → bundle create → same bundle_id into claude -p sessions → bundle get only`.

- [ ] **Step 4: Run targeted regression suite**

```bash
uv run pytest \
  tests/services/analysis_snapshot_bundle \
  tests/services/investment_snapshots/test_bundle_ensure_service.py \
  tests/services/investment_snapshots/test_append_only.py \
  tests/services/investment_snapshots/test_mutation_boundary.py \
  tests/mcp_server/test_analysis_bundle_tools.py \
  tests/mcp_server/test_investment_hermes_tools.py \
  tests/mcp_server/test_investment_snapshots_tools.py \
  tests/test_mcp_tool_registration.py -q
```

Expected: all pass with no live broker/provider calls.

- [ ] **Step 5: Commit Task 5**

```bash
git add app/mcp_server/README.md tests/mcp_server/test_analysis_bundle_tools.py
git commit -m "docs(ROB-838): document fixed-input runner contract"
```

---

### Task 6: Full verification, independent review, and PR handoff

**Files:**
- Verify all changed files
- PR body only; no merge

**Interfaces:**
- Produces a clean branch and PR number.
- PR body explicitly states ROB-833 reuse and default-off activation.

- [ ] **Step 1: Re-read requirements and inspect final diff**

```bash
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git status --short
```

Expected: no whitespace errors; only ROB-838 files/commits are present; worktree clean before shipping.

- [ ] **Step 2: Run focused tests with fresh output**

Run the Task 5 regression command. Expected: all pass.

- [ ] **Step 3: Run project lint gate**

```bash
make lint
```

Expected: Ruff format/check and ty complete with exit code 0.

- [ ] **Step 4: Run broader non-live tests proportionate to the change**

```bash
make test-unit
```

Expected: exit code 0. If repository test grouping omits DB-backed snapshot tests, the focused suite from Step 2 remains mandatory evidence.

- [ ] **Step 5: Perform independent code review**

Use the repository review skill against `BASE_SHA=$(git merge-base HEAD origin/main)` and `HEAD_SHA=$(git rev-parse HEAD)`. Fix every Critical/Important finding with a new RED/GREEN test, rerun Steps 2–4, and record any intentionally deferred Minor item in the PR body.

- [ ] **Step 6: Use the ship workflow without merging**

Create/push `feature/ROB-838-analysis-snapshot-bundle` and open a PR against `main`. The PR body must include:

```markdown
## ROB-833 reuse

This server-side bundle is the fixed input stage for ROB-833:
watch/fill event → create one bundle → pass the same bundle ID to each `claude -p`
session → sessions call `analysis_bundle_get` only. No live provider tools are
available in the frozen-input phase.

## Activation

`ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED` defaults to `false`; deployment does not
expose the new surface until operators opt in.
```

Do not merge. Report the PR number, focused test count/result, `make lint` result, unit-test result, content-hash invariant, and any remaining risk.

---

## Requirements traceability

- Existing ROB-287 audit/reuse: design + Tasks 1–3.
- Holdings/cash/quote/orderbook/indicators/support-resistance/flow/decision history/gate inputs: Task 2.
- Collection timestamp/source/as-of: Tasks 1–2.
- Read-only services and no trading mutation: Tasks 2 and 4 safety tests.
- Stored-only MCP read, zero live re-query: Task 3 and Task 4.
- Write-once plus content hash: Tasks 2–3.
- Freshness age/stale metadata: Task 3.
- Honest partial/unavailable/error payload: Task 2 and Task 3.
- `sections` filtering without transformation: Task 3.
- Default-off env gate: Task 4.
- ROB-833 PR-body handoff: Tasks 5–6.
- TDD and mocked broker calls: every implementation task.
- `make lint` clean, no merge, PR number: Task 6.
