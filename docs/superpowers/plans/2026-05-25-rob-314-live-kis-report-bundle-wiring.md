# ROB-314: Live KIS Report Bundle Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Hermes/MCP/HTTP `/invest/reports` bundle-preparation entrypoints inject the production collector registry and propagate `user_id`, so a KR/`kis_live` prepare actually collects portfolio/market/journal/watch evidence instead of failing with empty-default collectors.

**Architecture:** Three report-generation entrypoints currently construct `SnapshotBundleEnsureService(db)` with the intentionally-empty `default_collector_registry()` and never set `EnsureBundleRequest.user_id`. The reference path (`SnapshotBackedReportGenerator`) already injects `production_collector_registry(session)` and passes `user_id`. This plan aligns the three entrypoints to that reference path. No service-layer change is needed: `_collect_for_kind` already forwards `EnsureBundleRequest.user_id` into `CollectorRequest.user_id`. The two additional empty-registry call sites (`investment_snapshots_refresh_flow`, MCP `investment_snapshot_bundle_ensure`) are deliberately left on the empty default and locked with a guard test (scope decision: report-generation entrypoints only).

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, Pydantic v2, pytest/pytest-asyncio, FastMCP, Prefect (worker-only, identity-decorator fallback in tests).

---

## Background facts (verified against code)

- `SnapshotBundleEnsureService.__init__(session, *, repository=None, collectors=None, clock=None)` — when `collectors is None` it falls back to `default_collector_registry()` (empty). `app/services/action_report/common/snapshot_bundle.py:60-72`.
- `production_collector_registry(session)` returns the populated registry; `app/services/action_report/snapshot_backed/collectors/registry.py:144-193`.
- `EnsureBundleRequest.user_id: int | None` already exists; `app/schemas/investment_snapshots_mcp.py:55`.
- `CollectorRequest.user_id` is already populated from `request.user_id` inside `_collect_for_kind`; `app/services/action_report/common/snapshot_bundle.py:277-284`. **No change needed there.**
- Reference path injecting both: `app/services/action_report/snapshot_backed/generator.py:164-167, 192-203`.
- The MCP tool schema is inferred from the `_impl` signature (registered via `mcp.tool(...)(investment_report_prepare_bundle_impl)`), so adding a kwarg to `_impl` auto-exposes it. `app/mcp_server/tooling/investment_hermes_handlers.py:342-351`.
- Import-layering guard (`tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py`) only forbids LLM-provider modules/names inside `snapshot_backed/` + `investment_stages/`. It does **not** restrict importing `production_collector_registry` into the MCP/HTTP/flow modules. Safe.
- No DB migration: no schema change.

## File Structure

**Modify (implementation):**
- `app/mcp_server/tooling/investment_hermes_handlers.py` — `investment_report_prepare_bundle_impl`: add `user_id` param, inject production registry. (~lines 39-49 imports, 103-140 impl)
- `app/routers/investment_hermes_http.py` — `_PrepareBundleBody`: add `user_id` field; `prepare_bundle` route: pass `user_id`, inject production registry. (~lines 44-48 imports, 106-116 body, 155-185 route)
- `app/flows/hermes_bundle_preparation_flow.py` — `run_hermes_bundle_preparation` + `hermes_bundle_preparation_task` + `hermes_bundle_preparation_flow`: add `user_id` param, inject production registry. (~lines 61-66 imports, 85-184)

**Modify (deferred-decision documentation):**
- `app/flows/investment_snapshots_refresh_flow.py` — add ROB-314 decision comment (stays on empty default).
- `app/mcp_server/tooling/investment_snapshots_tools.py` — add ROB-314 decision comment (stays on empty default).

**Create (tests):**
- `tests/test_rob314_deferred_call_sites.py` — guard test locking the two deferred call sites on empty default.

**Modify (tests):**
- `tests/mcp_server/test_investment_hermes_tools.py` — new injection/user_id test + patch registry in existing happy-path test.
- `tests/routers/test_investment_hermes_http.py` — new injection/user_id test + patch registry in existing happy-path test.
- `tests/test_hermes_bundle_preparation_flow.py` — new injection/user_id test + patch registry in existing gate-on test.

**Modify (docs):**
- `docs/runbooks/hermes-report-generation.md` — ROB-314 section: production-collector wiring, `user_id` input field, live read-only calls at prepare time, operator smoke command shape, diagnostics ladder, deferred call sites.

---

## Task 1: MCP `investment_report_prepare_bundle` — inject production registry + user_id

**Files:**
- Modify: `app/mcp_server/tooling/investment_hermes_handlers.py`
- Test: `tests/mcp_server/test_investment_hermes_tools.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/mcp_server/test_investment_hermes_tools.py` (uses the existing `_flag_on` fixture and `_FakeAsyncSession`/`_patched_session_local` helpers already in the file):

```python
@pytest.mark.asyncio
async def test_prepare_bundle_injects_production_registry_and_user_id(_flag_on) -> None:
    bundle_uuid = uuid.uuid4()
    ensure_response = SimpleNamespace(
        bundle_uuid=bundle_uuid, status="complete",
        coverage_summary={}, freshness_summary={}, missing_sources=[],
        warnings=[], created=True,
    )
    ensure_response.model_dump = lambda mode="json": {
        "bundle_uuid": str(bundle_uuid), "status": "complete",
        "coverage_summary": {}, "freshness_summary": {},
        "missing_sources": [], "warnings": [], "created": True,
    }
    ensure_svc = AsyncMock()
    ensure_svc.ensure = AsyncMock(return_value=ensure_response)
    sentinel_registry = object()

    with (
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.AsyncSessionLocal",
            _patched_session_local,
        ),
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.production_collector_registry",
            return_value=sentinel_registry,
        ) as mock_registry,
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.SnapshotBundleEnsureService",
            return_value=ensure_svc,
        ) as mock_cls,
    ):
        result = await investment_report_prepare_bundle_impl(
            market="kr", account_scope="kis_live", symbols=["005930"], user_id=7,
        )

    assert result["success"] is True
    mock_registry.assert_called_once()
    assert mock_cls.call_args.kwargs["collectors"] is sentinel_registry
    called_request = ensure_svc.ensure.call_args.args[0]
    assert called_request.user_id == 7
    assert called_request.market == "kr"
    assert called_request.account_scope == "kis_live"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/test_investment_hermes_tools.py::test_prepare_bundle_injects_production_registry_and_user_id -v`
Expected: FAIL — `production_collector_registry` is not an attribute of the handler module yet (AttributeError on patch), and `investment_report_prepare_bundle_impl` rejects `user_id` (unexpected keyword argument).

- [ ] **Step 3: Add the import**

In `app/mcp_server/tooling/investment_hermes_handlers.py`, add to the imports block (after the existing `from app.services.action_report.common.snapshot_bundle import (SnapshotBundleEnsureService,)` at lines 47-49):

```python
from app.services.action_report.snapshot_backed.collectors.registry import (
    production_collector_registry,
)
```

- [ ] **Step 4: Add `user_id` param and inject the registry**

In `investment_report_prepare_bundle_impl`, change the signature to add `user_id` (after `purpose`):

```python
async def investment_report_prepare_bundle_impl(
    market: str,
    account_scope: str | None = None,
    policy_version: str = "intraday_action_report_v1",
    mode: str = "ensure_fresh",
    symbols: list[str] | None = None,
    candidate_limit: int | None = None,
    requested_by: str = "hermes",
    purpose: str = "report_generation",
    user_id: int | None = None,
) -> dict[str, Any]:
```

Add `user_id=user_id,` to the `EnsureBundleRequest(...)` call (after `requested_by=...`):

```python
    request = EnsureBundleRequest(
        purpose=purpose,
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        policy_version=policy_version,
        mode=mode,  # type: ignore[arg-type]
        symbols=symbols,
        candidate_limit=candidate_limit,
        requested_by=requested_by,  # type: ignore[arg-type]
        user_id=user_id,
    )
```

Change the service construction (currently `svc = SnapshotBundleEnsureService(db)`):

```python
    async with AsyncSessionLocal() as db:
        svc = SnapshotBundleEnsureService(
            db, collectors=production_collector_registry(db)
        )
        response = await svc.ensure(request)
        await db.commit()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/mcp_server/test_investment_hermes_tools.py::test_prepare_bundle_injects_production_registry_and_user_id -v`
Expected: PASS

- [ ] **Step 6: Keep the existing happy-path test hermetic**

In the existing `test_prepare_bundle_routes_through_ensure_service`, add a registry patch so it does not construct a real KIS client. Inside its `with (...)` patch group, add a third patch:

```python
        patch(
            "app.mcp_server.tooling.investment_hermes_handlers.production_collector_registry",
            return_value=object(),
        ),
```

- [ ] **Step 7: Run the full MCP tool test module**

Run: `uv run pytest tests/mcp_server/test_investment_hermes_tools.py -v`
Expected: PASS (all tests, including the name/description lock tests).

- [ ] **Step 8: Commit**

```bash
git add app/mcp_server/tooling/investment_hermes_handlers.py tests/mcp_server/test_investment_hermes_tools.py
git commit -m "feat(rob-314): MCP prepare_bundle injects production collectors + user_id

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: HTTP `/hermes/prepare-bundle` — inject production registry + user_id

**Files:**
- Modify: `app/routers/investment_hermes_http.py`
- Test: `tests/routers/test_investment_hermes_http.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/routers/test_investment_hermes_http.py` (uses the existing `_build_app` helper):

```python
@pytest.mark.asyncio
async def test_prepare_bundle_injects_production_registry_and_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )
    bundle_uuid = uuid.uuid4()
    response = SimpleNamespace(bundle_uuid=bundle_uuid, status="partial")
    response.model_dump = lambda mode="json": {
        "bundle_uuid": str(bundle_uuid), "status": "partial",
        "coverage_summary": {}, "freshness_summary": {},
        "missing_sources": [], "warnings": [], "created": True,
    }
    ensure_svc = AsyncMock()
    ensure_svc.ensure = AsyncMock(return_value=response)
    sentinel_registry = object()

    app = _build_app()
    with (
        patch(
            "app.routers.investment_hermes_http.production_collector_registry",
            return_value=sentinel_registry,
        ) as mock_registry,
        patch(
            "app.routers.investment_hermes_http.SnapshotBundleEnsureService",
            return_value=ensure_svc,
        ) as mock_cls,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            resp = await client.post(
                "/trading/api/investment-reports/hermes/prepare-bundle",
                json={
                    "market": "kr",
                    "account_scope": "kis_live",
                    "symbols": ["005930"],
                    "user_id": 7,
                },
            )

    assert resp.status_code == 200, resp.text
    mock_registry.assert_called_once()
    assert mock_cls.call_args.kwargs["collectors"] is sentinel_registry
    called = ensure_svc.ensure.call_args.args[0]
    assert called.user_id == 7
    assert called.market == "kr"
    assert called.account_scope == "kis_live"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/routers/test_investment_hermes_http.py::test_prepare_bundle_injects_production_registry_and_user_id -v`
Expected: FAIL — `production_collector_registry` not importable from the router module, and `_PrepareBundleBody` rejects `user_id` (extra="forbid" → 400).

- [ ] **Step 3: Add the import**

In `app/routers/investment_hermes_http.py`, add after the existing `from app.services.action_report.common.snapshot_bundle import (SnapshotBundleEnsureService,)` (lines 46-48):

```python
from app.services.action_report.snapshot_backed.collectors.registry import (
    production_collector_registry,
)
```

- [ ] **Step 4: Add `user_id` to the body model**

In `_PrepareBundleBody`, add the field (after `purpose`):

```python
class _PrepareBundleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str
    account_scope: str | None = None
    policy_version: str = "intraday_action_report_v1"
    mode: str = "ensure_fresh"
    symbols: list[str] | None = None
    candidate_limit: int | None = None
    requested_by: str = "hermes"
    purpose: str = "report_generation"
    user_id: int | None = None
```

- [ ] **Step 5: Propagate user_id and inject the registry in the route**

In the `prepare_bundle` route, add `user_id=body.user_id,` to the `EnsureBundleRequest(...)` call (after `requested_by=...`), and change the service construction:

```python
        ensure_request = EnsureBundleRequest(
            purpose=body.purpose,
            market=body.market,  # type: ignore[arg-type]
            account_scope=body.account_scope,  # type: ignore[arg-type]
            policy_version=body.policy_version,
            mode=body.mode,  # type: ignore[arg-type]
            symbols=body.symbols,
            candidate_limit=body.candidate_limit,
            requested_by=body.requested_by,  # type: ignore[arg-type]
            user_id=body.user_id,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_prepare_bundle_request",
                "validation": exc.errors(),
            },
        ) from exc

    svc = SnapshotBundleEnsureService(db, collectors=production_collector_registry(db))
    response = await svc.ensure(ensure_request)
    await db.commit()
    return {"success": True, **response.model_dump(mode="json")}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/routers/test_investment_hermes_http.py::test_prepare_bundle_injects_production_registry_and_user_id -v`
Expected: PASS

- [ ] **Step 7: Keep the existing happy-path test hermetic**

In the existing `test_prepare_bundle_routes_through_ensure_service`, replace the single `with patch(...)` with a patch group that also patches the registry:

```python
    app = _build_app()
    with (
        patch(
            "app.routers.investment_hermes_http.production_collector_registry",
            return_value=object(),
        ),
        patch(
            "app.routers.investment_hermes_http.SnapshotBundleEnsureService",
            return_value=ensure_svc,
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            resp = await client.post(
                "/trading/api/investment-reports/hermes/prepare-bundle",
                json={"market": "kr", "account_scope": "kis_live"},
            )
```

- [ ] **Step 8: Run the full HTTP test module**

Run: `uv run pytest tests/routers/test_investment_hermes_http.py -v`
Expected: PASS (gate-off, routing, error-mapping cases all green).

- [ ] **Step 9: Commit**

```bash
git add app/routers/investment_hermes_http.py tests/routers/test_investment_hermes_http.py
git commit -m "feat(rob-314): Hermes HTTP prepare-bundle injects production collectors + user_id

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Prefect `hermes_bundle_preparation_flow` — inject production registry + user_id

**Files:**
- Modify: `app/flows/hermes_bundle_preparation_flow.py`
- Test: `tests/test_hermes_bundle_preparation_flow.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hermes_bundle_preparation_flow.py` (reuses the `_FakeAsyncSession` / `_session_factory_returning_fake` helpers already defined in the file):

```python
@pytest.mark.asyncio
async def test_gate_on_injects_production_registry_and_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    monkeypatch.setattr(
        settings, "HERMES_BUNDLE_PREPARATION_ENABLED", True, raising=False
    )
    from app.flows.hermes_bundle_preparation_flow import run_hermes_bundle_preparation

    bundle_uuid = uuid.uuid4()
    ensure_response = SimpleNamespace(
        bundle_uuid=bundle_uuid, status="partial",
        freshness_summary={}, coverage_summary={}, missing_sources=[],
        warnings=[], created=True,
    )
    ensure_svc = AsyncMock()
    ensure_svc.ensure = AsyncMock(return_value=ensure_response)
    sentinel_registry = object()

    with (
        patch(
            "app.flows.hermes_bundle_preparation_flow._session_factory",
            _session_factory_returning_fake,
        ),
        patch(
            "app.flows.hermes_bundle_preparation_flow.production_collector_registry",
            return_value=sentinel_registry,
        ) as mock_registry,
        patch(
            "app.flows.hermes_bundle_preparation_flow.SnapshotBundleEnsureService",
            return_value=ensure_svc,
        ) as mock_cls,
    ):
        result = await run_hermes_bundle_preparation(
            market="kr", account_scope="kis_live", symbols=["005930"], user_id=11,
        )

    assert result["status"] == "ok"
    mock_registry.assert_called_once()
    assert mock_cls.call_args.kwargs["collectors"] is sentinel_registry
    called = ensure_svc.ensure.call_args.args[0]
    assert called.user_id == 11
    assert called.market == "kr"
```

> Note: `_session_factory_returning_fake` already exists in this file (used by `test_gate_on_routes_through_ensure_service`). Verify it returns a zero-arg factory producing `_FakeAsyncSession`; if its signature differs, mirror the existing gate-on test's patch shape exactly.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_hermes_bundle_preparation_flow.py::test_gate_on_injects_production_registry_and_user_id -v`
Expected: FAIL — `production_collector_registry` not importable from the flow module, and `run_hermes_bundle_preparation` rejects `user_id`.

- [ ] **Step 3: Add the import**

In `app/flows/hermes_bundle_preparation_flow.py`, add after the existing `from app.services.action_report.common.snapshot_bundle import (SnapshotBundleEnsureService,)` (lines 64-66):

```python
from app.services.action_report.snapshot_backed.collectors.registry import (
    production_collector_registry,
)
```

- [ ] **Step 4: Thread `user_id` through all three callables + inject the registry**

In `run_hermes_bundle_preparation`, add `user_id: int | None = None,` to the signature (after `candidate_limit`), add `user_id=user_id,` to `EnsureBundleRequest(...)` (after `requested_by=...`), and change the service construction:

```python
async def run_hermes_bundle_preparation(
    *,
    market: str = "kr",
    account_scope: str | None = "kis_live",
    policy_version: str = "intraday_action_report_v1",
    purpose: str = "hermes_report_generation",
    requested_by: str = "hermes",
    symbols: list[str] | None = None,
    candidate_limit: int | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    ...
    request = EnsureBundleRequest(
        purpose=purpose,
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        policy_version=policy_version,
        mode="ensure_fresh",
        symbols=symbols,
        candidate_limit=candidate_limit,
        requested_by=requested_by,  # type: ignore[arg-type]
        user_id=user_id,
    )

    async with _session_factory()() as session:
        service = SnapshotBundleEnsureService(
            session, collectors=production_collector_registry(session)
        )
        response = await service.ensure(request)
        await session.commit()
```

Then add `user_id: int | None = None,` to `hermes_bundle_preparation_task` and `hermes_bundle_preparation_flow` signatures and forward it in their bodies:

```python
@task(name="hermes_bundle_preparation")
async def hermes_bundle_preparation_task(
    *,
    market: str = "kr",
    account_scope: str | None = "kis_live",
    policy_version: str = "intraday_action_report_v1",
    purpose: str = "hermes_report_generation",
    requested_by: str = "hermes",
    symbols: list[str] | None = None,
    candidate_limit: int | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    return await run_hermes_bundle_preparation(
        market=market,
        account_scope=account_scope,
        policy_version=policy_version,
        purpose=purpose,
        requested_by=requested_by,
        symbols=symbols,
        candidate_limit=candidate_limit,
        user_id=user_id,
    )
```

Apply the identical `user_id` addition + forwarding to `hermes_bundle_preparation_flow`.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_hermes_bundle_preparation_flow.py::test_gate_on_injects_production_registry_and_user_id -v`
Expected: PASS

- [ ] **Step 6: Keep the existing gate-on test hermetic**

In the existing `test_gate_on_routes_through_ensure_service`, add a registry patch to its `with (...)` group so it does not build a real KIS client:

```python
        patch(
            "app.flows.hermes_bundle_preparation_flow.production_collector_registry",
            return_value=object(),
        ),
```

- [ ] **Step 7: Run the full flow test module**

Run: `uv run pytest tests/test_hermes_bundle_preparation_flow.py -v`
Expected: PASS (static invariants + gate-off side-effect-free + gate-on routing).

- [ ] **Step 8: Commit**

```bash
git add app/flows/hermes_bundle_preparation_flow.py tests/test_hermes_bundle_preparation_flow.py
git commit -m "feat(rob-314): Hermes bundle-prep flow injects production collectors + user_id

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Lock the two deferred call sites on the empty default registry

Scope decision (recorded): only the three report-generation entrypoints get production collectors. The scheduler refresh flow and the generic `investment_snapshot_bundle_ensure` MCP primitive deliberately stay on `default_collector_registry()` — the refresh flow is gated to the separate scheduler-activation track, and the generic tool is a primitive callers feed manual data to. This task documents and locks that decision.

**Files:**
- Modify: `app/flows/investment_snapshots_refresh_flow.py`
- Modify: `app/mcp_server/tooling/investment_snapshots_tools.py`
- Create: `tests/test_rob314_deferred_call_sites.py`

- [ ] **Step 1: Write the failing guard test**

Create `tests/test_rob314_deferred_call_sites.py`:

```python
"""ROB-314 — lock the deferred bundle-ensure call sites on the empty default.

The report-generation entrypoints (MCP prepare_bundle, HTTP prepare-bundle,
hermes_bundle_preparation_flow) inject ``production_collector_registry``.
These two call sites intentionally do NOT — the refresh flow belongs to the
separate scheduler-activation track and the generic ensure tool is a manual
primitive. If a future change wires production collectors here, it must be a
deliberate decision that updates this test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_DEFERRED_FILES = [
    _REPO_ROOT / "app" / "flows" / "investment_snapshots_refresh_flow.py",
    _REPO_ROOT / "app" / "mcp_server" / "tooling" / "investment_snapshots_tools.py",
]


@pytest.mark.parametrize("path", _DEFERRED_FILES, ids=lambda p: p.name)
def test_deferred_call_site_does_not_wire_production_collectors(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "production_collector_registry" not in text, (
        f"{path.name} now references production_collector_registry. If this is "
        "intentional, update ROB-314 scope and this guard."
    )
    assert "ROB-314" in text, (
        f"{path.name} must carry a ROB-314 decision marker explaining why it "
        "stays on the empty default collector registry."
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rob314_deferred_call_sites.py -v`
Expected: FAIL on the `"ROB-314" in text` assertion (the marker comments don't exist yet). The `production_collector_registry not in text` assertion already passes.

- [ ] **Step 3: Add the decision marker comment to the refresh flow**

In `app/flows/investment_snapshots_refresh_flow.py`, immediately above the `service = SnapshotBundleEnsureService(session)` line (~line 103), add:

```python
        # ROB-314: deliberately NOT wired to production_collector_registry.
        # The scheduler refresh path belongs to the separate scheduler-
        # activation track; only the report-generation entrypoints (MCP
        # prepare_bundle, HTTP prepare-bundle, hermes_bundle_preparation_flow)
        # inject production collectors. Locked by
        # tests/test_rob314_deferred_call_sites.py.
        service = SnapshotBundleEnsureService(session)
```

- [ ] **Step 4: Add the decision marker comment to the generic ensure tool**

In `app/mcp_server/tooling/investment_snapshots_tools.py`, immediately above the `svc = SnapshotBundleEnsureService(db)` line (~line 75), add:

```python
        # ROB-314: deliberately NOT wired to production_collector_registry.
        # This is the generic bundle-ensure primitive — callers feed manual
        # snapshots or rely on reuse. Production collectors are injected only
        # at the report-generation entrypoints. Locked by
        # tests/test_rob314_deferred_call_sites.py.
        svc = SnapshotBundleEnsureService(db)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_rob314_deferred_call_sites.py -v`
Expected: PASS (both parametrized files).

- [ ] **Step 6: Commit**

```bash
git add app/flows/investment_snapshots_refresh_flow.py app/mcp_server/tooling/investment_snapshots_tools.py tests/test_rob314_deferred_call_sites.py
git commit -m "test(rob-314): lock deferred bundle-ensure call sites on empty default registry

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Runbook — production-collector wiring, user_id, live calls, smoke, diagnostics

**Files:**
- Modify: `docs/runbooks/hermes-report-generation.md`

- [ ] **Step 1: Confirm the runbook exists and find an insertion point**

Run: `ls -1 docs/runbooks/hermes-report-generation.md && grep -n '^## ' docs/runbooks/hermes-report-generation.md`
Expected: file exists; note the section headings so the new section lands after the existing prepare/generate description (before the prod-cutover section).

- [ ] **Step 2: Add the ROB-314 section**

Insert this section at the noted location:

```markdown
## ROB-314 — production-collector bundle preparation (real KR/kis_live evidence)

The three report-generation entrypoints now inject `production_collector_registry(session)` and accept a `user_id`:

- MCP `investment_report_prepare_bundle` (`user_id` is an optional tool arg)
- HTTP `POST /trading/api/investment-reports/hermes/prepare-bundle` (`user_id` is an optional body field)
- Prefect `hermes_bundle_preparation_flow` (`user_id` is an optional flow param)

Because these paths are token-authed (HTTP) or have no user context (MCP), `user_id`
must be supplied explicitly by the caller — it is never derived from an authenticated
session here. The REST `SnapshotBackedReportGenerator` path keeps deriving it from its
own request.

**Behaviour change — prepare is no longer DB-only.** With production collectors injected,
prepare-bundle now performs live, read-only external calls in addition to DB reads:
KIS quote/orderbook (`SymbolSnapshotCollector`) and KIS/Upbit open-orders
(`PendingOrdersSnapshotCollector`). No order/watch/order-intent mutation occurs. Live
read credentials must be present on the host; absent/misconfigured credentials make those
collectors emit per-source `unavailable` rather than crashing. Expect added latency and
broker rate-limit sensitivity.

**Operator smoke (read-only, placeholders only — do not paste real secrets):**

```bash
# Gate must be on for the endpoint to do anything.
export SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true
# HTTP transport (token-authed):
curl -sS -X POST "$HOST/trading/api/investment-reports/hermes/prepare-bundle" \
  -H "X-Hermes-Ingest-Token: $HERMES_INGEST_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"market":"kr","account_scope":"kis_live","symbols":["<HELD_SYMBOL>","<CANDIDATE>"],"user_id":<USER_ID>}'
```

Inspect `coverage_summary` / `missing_sources` in the response to confirm portfolio,
market, journal, and watch coverage.

**Diagnostics ladder — interpreting a still-empty / blocked bundle:**

| Symptom | Likely cause | Operator action |
|---|---|---|
| 503 `snapshot_backed_report_generator_disabled` | feature flag off | set `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true` |
| 403 / 401 on HTTP | token misconfig / wrong token | check `HERMES_INGEST_TOKEN[_HEADER]` |
| portfolio `unavailable` but holdings exist | `user_id` not supplied to this entrypoint | pass `user_id` in the request |
| all required sources `unavailable` | wrong/empty collector registry (regression) | confirm this entrypoint injects `production_collector_registry` |
| specific source `unavailable` w/ credentials absent | missing live read precondition | provision read creds for that broker/source |
| source present but `hard_stale` | stale data precondition | refresh upstream data; not a code bug |
| `complete`/`partial` with a real no-action verdict | genuine no-action report | none — this is a valid outcome |

**Deferred (ROB-314 scope decision):** `investment_snapshots_refresh_flow` and the generic
MCP `investment_snapshot_bundle_ensure` tool intentionally stay on the empty default
registry; the refresh path is part of the separate scheduler-activation track. Locked by
`tests/test_rob314_deferred_call_sites.py`.
```

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/hermes-report-generation.md
git commit -m "docs(rob-314): runbook — production-collector prepare, user_id, smoke + diagnostics

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: Full verification gate

**Files:** none (verification only)

- [ ] **Step 1: Run the changed/related test modules together**

Run:
```bash
uv run pytest \
  tests/mcp_server/test_investment_hermes_tools.py \
  tests/routers/test_investment_hermes_http.py \
  tests/routers/test_investment_hermes_http_auth.py \
  tests/test_hermes_bundle_preparation_flow.py \
  tests/test_rob314_deferred_call_sites.py \
  -v
```
Expected: all PASS.

- [ ] **Step 2: Run the ROB-309 Hermes round-trip smoke (acceptance criterion)**

Run: `uv run pytest tests/test_hermes_roundtrip_smoke.py -v`
Expected: PASS — the entrypoint changes must not break the contract round-trip.

- [ ] **Step 3: Run the in-process-LLM import guard (must stay green)**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -v`
Expected: PASS — no LLM provider reintroduced; importing `production_collector_registry` into entrypoints does not trip the guard.

- [ ] **Step 4: Lint + typecheck**

Run: `make lint`
Expected: clean (Ruff + ty). Fix any issues introduced by the new params/imports, then re-run.

- [ ] **Step 5: Final broad test sweep (excluding integration/slow)**

Run: `uv run pytest tests/ -m "not integration and not slow" -q`
Expected: PASS, no regressions.

- [ ] **Step 6: Commit any lint/format fixups**

```bash
git add -A
git commit -m "chore(rob-314): lint/format fixups after entrypoint wiring

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>" || echo "nothing to commit"
```

---

## Operator-gated (NOT executed by this plan)

Per the issue's non-goals, the following are explicitly deferred to an operator step:

- Live KR/`kis_live` real-data smoke against production DB + live broker read creds (command shape provided in the runbook, Task 5). Requires the feature flag on and real `user_id` + held/candidate symbols.
- Any Prefect deployment registration / unpause (`robin-prefect-automations`).
- Any production DB backfill or scheduler activation.

## Handoff checklist (fill in at PR time)

- Branch: `rob-314`
- PR URL: _(after `gh pr create`)_
- Tests run: Task 6 commands + outputs.
- Migrations: none.
- Feature flags/config: `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED` (MCP/HTTP), `HERMES_BUNDLE_PREPARATION_ENABLED` (flow), `HERMES_INGEST_TOKEN[_HEADER]` (HTTP transport). All default-off / unchanged defaults.
- Smoke command shape: runbook ROB-314 section (placeholders only).
- Remaining operator-gated steps: live smoke, Prefect activation (above).

---

## Self-review notes

- **Spec coverage:** Scope §1 (entrypoint alignment) → Tasks 1-3 + deferred decision Task 4. §2 (source coverage / diagnostics) → covered by injecting the real registry (collectors already emit per-source status) + runbook diagnostics ladder (Task 5). §3 (smoke + diagnostics) → runbook + operator-gated section. §4 (tests) → Tasks 1-4 tests + Task 6 guard/round-trip. AC user_id-as-explicit-input → Tasks 1-3 tests assert `called.user_id`. AC deferred-call-sites decision → Task 4. AC ROB-309 still passes → Task 6 Step 2.
- **No service-layer edit:** `CollectorRequest.user_id` is already populated from `EnsureBundleRequest.user_id`; verified at `snapshot_bundle.py:277-284`. Plan intentionally touches only entrypoints.
- **Type consistency:** `user_id: int | None` used identically across `_impl`, `_PrepareBundleBody`, and all three flow callables, matching `EnsureBundleRequest.user_id: int | None`.
