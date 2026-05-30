# ROB-380 Deterministic Single-Collection (mock_preview reuses live evidence) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `mock_preview` report path **reuse** the live report's account-independent (`market`/`news`/`candidate_universe`/`symbol`) snapshot rows instead of re-collecting them, so the live and mock reports cite the **same** `snapshot_uuid` while account-bound evidence (`portfolio`/`journal`/`watch_context`/`pending_orders`) stays collected fresh per scope.

**Architecture:** ROB-373 normalized account-independent snapshots to `account_scope=NULL` (necessary), but ROB-379 proved the mock path still **re-collects** its own copy — time-varying collector payloads hash differently → dedup miss → distinct rows (the "necessary-not-sufficient" finding). This plan adds a new `SnapshotBundleEnsureService.ensure_reusing_account_independent()` that **links** the live bundle's NULL-scope snapshot rows into a new kis_mock bundle (fail-closed: account-independent kinds are *never* collected on this path) and collects only account-bound kinds fresh. The mock runner reads `live.snapshot_bundle_uuid` directly off the live report row (already persisted by the generator at `generator.py:413`), so **no CLI or MCP-handler plumbing changes are needed**. No DB migration: existing `investment_snapshot_*` tables and the already-idempotent `link_bundle_item` carry the reuse.

**Tech Stack:** Python 3.13, SQLAlchemy async, Pydantic v2, pytest (`pytest-asyncio`), `uv` for deps, Ruff + ty for lint/type.

---

## Background / Key Facts (read before starting)

- **Live report already records its bundle.** `app/models/investment_reports.py:183` defines `InvestmentReport.snapshot_bundle_uuid`; the snapshot-backed generator persists it at `app/services/action_report/snapshot_backed/generator.py:413`. The mock runner already fetches the live report row (`runner.py:81`), so `live.snapshot_bundle_uuid` is available with **zero** new plumbing.
- **Account-independent kinds** are the single source of truth in `app/services/investment_snapshots/scope_policy.py:16` — `frozenset({"market", "news", "candidate_universe", "symbol"})`, helper `is_account_independent(kind)`.
- **Linking is already idempotent & cross-scope.** `InvestmentSnapshotsRepository.link_bundle_item` (`repository.py:163`) takes a `snapshot_uuid`, looks the row up globally, and links it to *any* bundle (reusing if already linked). A NULL-scope row from the live bundle links cleanly into a kis_mock bundle.
- **Policy kinds & required flags** (`app/services/investment_snapshots/policy.py`): required = `portfolio`, `journal`, `watch_context`, `market`. Optional = `symbol`, `candidate_universe`, `news`, `pending_orders`, plus remote-debug/browser kinds. So `market` is the only **required** account-independent kind — it must be reusable from the live bundle for a healthy mock bundle status.
- **Why ROB-373's `test_cross_scope_reuse.py` passed but runtime failed:** that test feeds **identical** manual payloads to both ensures, so dedup matches. Production collectors emit time-varying payloads. ROB-380's new invariant test must prove reuse holds **even when the independent payload available to the mock path differs** from the live one.
- **Repository surface is locked** by `tests/services/investment_snapshots/test_append_only.py:39-52` — adding a read method requires updating that list (forces reviewer awareness). The new read method must NOT use a `update_/delete_/remove_/mutate_/patch_` prefix.

## File Structure

- **Modify** `app/services/investment_snapshots/repository.py` — add one SELECT-only read method `list_account_independent_bundle_snapshots(bundle_uuid)`.
- **Modify** `tests/services/investment_snapshots/test_append_only.py` — add the new read method to the locked surface list.
- **Modify** `app/services/action_report/common/snapshot_bundle.py` — extract a small private `_insert_collected_snapshot()` helper (DRY, behavior-preserving), add typed exception `LiveBundleNotFoundForReuse`, add public `ensure_reusing_account_independent()`.
- **Create** `tests/services/investment_snapshots/test_mock_reuse_ensure.py` — unit tests for the new service method.
- **Modify** `app/services/investment_reports/mock_preview/runner.py` — branch on `live.snapshot_bundle_uuid`: reuse path when present, legacy `ensure()` fallback when absent.
- **Modify** `tests/services/investment_reports/mock_preview/test_runner.py` — add the runtime invariant test (shared rows across live+mock) and a fallback-branch test.
- **Modify** `docs/runbooks/invest-reports-us-schedule.md` — replace the ROB-379 "known limitation" caveat with the resolved-by-ROB-380 behavior.

---

## Task 1: Repository read method to fetch a bundle's account-independent snapshots

**Files:**
- Modify: `app/services/investment_snapshots/repository.py`
- Modify: `tests/services/investment_snapshots/test_append_only.py:39-52`
- Test: `tests/services/investment_snapshots/test_repository_reads.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/investment_snapshots/test_repository_reads.py`:

```python
@pytest.mark.asyncio
async def test_list_account_independent_bundle_snapshots(db_session) -> None:
    """ROB-380 — returns only the bundle's account-independent (NULL-scope) snapshots."""
    import uuid as _uuid

    from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
    from app.services.action_report.common.snapshot_bundle import (
        SnapshotBundleEnsureService,
    )
    from app.services.investment_snapshots.collectors import SnapshotCollectResult
    from app.services.investment_snapshots.repository import (
        InvestmentSnapshotsRepository,
    )

    def _manual(kind: str, *, account_scope: str | None) -> SnapshotCollectResult:
        return SnapshotCollectResult(
            snapshot_kind=kind,  # type: ignore[arg-type]
            market="us",  # type: ignore[arg-type]
            account_scope=account_scope,  # type: ignore[arg-type]
            source_kind="manual",
            payload_json={"k": kind, "v": "x"},
            as_of=dt.datetime(2025, 1, 15, 9, 0, tzinfo=dt.UTC),
            freshness_status="fresh",
        )

    svc = SnapshotBundleEnsureService(
        db_session, clock=lambda: dt.datetime(2025, 1, 15, 9, 0, tzinfo=dt.UTC)
    )
    resp = await svc.ensure(
        EnsureBundleRequest(
            purpose=f"rob380_read_{_uuid.uuid4().hex[:8]}",
            market="us",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            mode="ensure_fresh",
            manual_snapshots={
                "market": [_manual("market", account_scope="kis_live")],
                "news": [_manual("news", account_scope="kis_live")],
                "portfolio": [_manual("portfolio", account_scope="kis_live")],
                "journal": [_manual("journal", account_scope="kis_live")],
                "watch_context": [_manual("watch_context", account_scope="kis_live")],
            },
        )
    )
    await db_session.commit()
    assert resp.bundle_uuid is not None

    repo = InvestmentSnapshotsRepository(db_session)
    snaps = await repo.list_account_independent_bundle_snapshots(resp.bundle_uuid)
    kinds = {s.snapshot_kind for s in snaps}
    # market + news are account-independent → returned; portfolio/journal/watch are not.
    assert kinds == {"market", "news"}
    assert all(s.account_scope is None for s in snaps)
```

Ensure the test module imports `datetime as dt` at the top (add if missing).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/investment_snapshots/test_repository_reads.py::test_list_account_independent_bundle_snapshots -v`
Expected: FAIL with `AttributeError: 'InvestmentSnapshotsRepository' object has no attribute 'list_account_independent_bundle_snapshots'`

- [ ] **Step 3: Add the import and the read method**

In `app/services/investment_snapshots/repository.py`, extend the existing scope_policy import (currently `from app.services.investment_snapshots.scope_policy import normalize_account_scope`) to:

```python
from app.services.investment_snapshots.scope_policy import (
    ACCOUNT_INDEPENDENT_SNAPSHOT_KINDS,
    normalize_account_scope,
)
```

Then add this method right after `list_bundle_items_with_snapshots` (after line 251), inside the class:

```python
    async def list_account_independent_bundle_snapshots(
        self, bundle_uuid: uuid.UUID
    ) -> list[InvestmentSnapshot]:
        """ROB-380 — account-independent snapshots linked to a bundle.

        SELECT-only. Returns the ``market/news/candidate_universe/symbol``
        snapshots (the kinds normalized to ``account_scope=NULL``) so the
        mock_preview path can LINK them into a kis_mock bundle instead of
        re-collecting them. Account-bound kinds are intentionally excluded.
        """
        stmt = (
            sa.select(InvestmentSnapshot)
            .join(
                InvestmentSnapshotBundleItem,
                InvestmentSnapshotBundleItem.snapshot_id == InvestmentSnapshot.id,
            )
            .join(
                InvestmentSnapshotBundle,
                InvestmentSnapshotBundle.id == InvestmentSnapshotBundleItem.bundle_id,
            )
            .where(
                InvestmentSnapshotBundle.bundle_uuid == bundle_uuid,
                InvestmentSnapshot.snapshot_kind.in_(
                    tuple(ACCOUNT_INDEPENDENT_SNAPSHOT_KINDS)
                ),
            )
            .order_by(InvestmentSnapshot.id.asc())
        )
        result = await self._session.scalars(stmt)
        return list(result.all())
```

- [ ] **Step 4: Update the locked surface list**

In `tests/services/investment_snapshots/test_append_only.py`, add an entry to the sorted list at lines 39-52 (keep it alphabetically sorted) and a comment line. The list becomes:

```python
    # ROB-380 added list_account_independent_bundle_snapshots — a SELECT-only
    # read used by the mock_preview reuse path. Still no mutation methods.
    assert public_methods == [
        "find_latest_bundle",
        "get_bundle_by_uuid",
        "get_bundle_item_with_snapshot",
        "get_run_by_uuid",
        "get_snapshot_by_uuid",
        "insert_bundle",
        "insert_run",
        "insert_snapshot",
        "link_bundle_item",
        "list_account_independent_bundle_snapshots",
        "list_bundle_items_with_snapshots",
        "list_bundles",
        "list_snapshots",
    ]
```

- [ ] **Step 5: Run both tests to verify they pass**

Run: `uv run pytest tests/services/investment_snapshots/test_repository_reads.py::test_list_account_independent_bundle_snapshots tests/services/investment_snapshots/test_append_only.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add app/services/investment_snapshots/repository.py tests/services/investment_snapshots/test_repository_reads.py tests/services/investment_snapshots/test_append_only.py
git commit -m "feat(ROB-380): repo read for a bundle's account-independent snapshots

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Extract a behavior-preserving `_insert_collected_snapshot` helper (DRY prep)

This extraction lets Task 3 reuse the exact `SnapshotCreate` shape without duplicating it, and keeps `ensure()`'s behavior identical. The existing `test_bundle_ensure_service.py` + `test_cross_scope_reuse.py` are the safety net.

**Files:**
- Modify: `app/services/action_report/common/snapshot_bundle.py:197-234`
- Test (safety net, no new test): `tests/services/investment_snapshots/test_bundle_ensure_service.py`, `tests/services/investment_snapshots/test_cross_scope_reuse.py`

- [ ] **Step 1: Add the helper method**

In `app/services/action_report/common/snapshot_bundle.py`, add this method to `SnapshotBundleEnsureService` immediately before `_collect_for_kind` (before line 288):

```python
    async def _insert_collected_snapshot(
        self,
        *,
        run_uuid,  # uuid.UUID
        kind_policy: SnapshotKindPolicy,
        result: SnapshotCollectResult,
    ) -> tuple[Any, str]:
        """Insert one collected snapshot; return (snapshot_row, effective_status).

        Shared by ``ensure`` (account-bound + independent collection) and
        ``ensure_reusing_account_independent`` (account-bound only) so the
        ``SnapshotCreate`` shape and freshness reclassification live in ONE place.
        """
        # Collectors run after ``now`` is captured for the reuse gate. Live
        # collectors can stamp results a few seconds after the ensure started,
        # so classify against the post-collect clock instead of treating long
        # collection time as future data.
        classification_now = self._clock()
        computed_status: FreshnessStatus = classify_freshness(
            as_of=result.as_of,
            now=classification_now,
            policy=kind_policy.freshness,
        )
        # Caller-supplied status can downgrade but never upgrade past policy.
        effective_status = _worse_of(result.freshness_status, computed_status)
        snap = await self._repo.insert_snapshot(
            SnapshotCreate(
                run_uuid=run_uuid,
                snapshot_kind=result.snapshot_kind,
                market=result.market,
                account_scope=result.account_scope,
                symbol=result.symbol,
                source_table=result.source_table,
                source_id=result.source_id,
                source_uri=result.source_uri,
                source_kind=result.source_kind,
                payload_json=result.payload_json,
                source_timestamps_json=result.source_timestamps_json,
                coverage_json=result.coverage_json,
                errors_json=result.errors_json,
                as_of=result.as_of,
                valid_until=classification_now + kind_policy.freshness.hard_ttl,
                freshness_status=effective_status,
            )
        )
        return snap, effective_status
```

- [ ] **Step 2: Rewrite the `ensure()` per-result loop to call the helper**

In `ensure()`, replace the body of the `for result in results:` loop (lines 197-234) so it delegates to the helper. The new loop:

```python
            kind_statuses: list[str] = []
            status_errors: list[tuple[str, dict[str, Any]]] = []
            last_as_of: dt.datetime | None = None
            for result in results:
                snap, effective_status = await self._insert_collected_snapshot(
                    run_uuid=run.run_uuid,
                    kind_policy=kind_policy,
                    result=result,
                )
                linked_items.append((snap.snapshot_uuid, role))
                kind_statuses.append(effective_status)
                status_errors.append((effective_status, result.errors_json or {}))
                last_as_of = result.as_of
```

(Everything after the loop — `worst_status`, `coverage`, `summary_entry` — stays exactly as is.)

- [ ] **Step 3: Run the ensure-service safety-net tests**

Run: `uv run pytest tests/services/investment_snapshots/test_bundle_ensure_service.py tests/services/investment_snapshots/test_cross_scope_reuse.py -v`
Expected: PASS (all existing tests still green — behavior unchanged)

- [ ] **Step 4: Commit**

```bash
git add app/services/action_report/common/snapshot_bundle.py
git commit -m "refactor(ROB-380): extract _insert_collected_snapshot helper (no behavior change)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `ensure_reusing_account_independent()` — link live independent rows, collect account-bound fresh

**Files:**
- Modify: `app/services/action_report/common/snapshot_bundle.py`
- Test: `tests/services/investment_snapshots/test_mock_reuse_ensure.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/services/investment_snapshots/test_mock_reuse_ensure.py`:

```python
"""ROB-380 — ensure_reusing_account_independent reuses live NULL-scope rows.

Distinct from ROB-373's test_cross_scope_reuse: here the mock path is given a
DIFFERENT account-independent payload than the live bundle, and we still expect
the mock bundle to cite the LIVE snapshot rows (because it LINKS, not collects).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
from app.services.action_report.common.snapshot_bundle import (
    LiveBundleNotFoundForReuse,
    SnapshotBundleEnsureService,
)
from app.services.investment_snapshots.collectors import SnapshotCollectResult
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository

_FIXED_NOW = dt.datetime(2025, 1, 15, 9, 0, tzinfo=dt.UTC)


def _frozen_clock():
    return lambda: _FIXED_NOW


def _manual(
    kind: str, *, account_scope: str | None, payload: dict
) -> SnapshotCollectResult:
    return SnapshotCollectResult(
        snapshot_kind=kind,  # type: ignore[arg-type]
        market="us",  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        source_kind="manual",
        payload_json=payload,
        as_of=_FIXED_NOW,
        freshness_status="fresh",
    )


async def _uuids_by_kind(repo, bundle_uuid):
    bundle = await repo.get_bundle_by_uuid(bundle_uuid)
    pairs = await repo.list_bundle_items_with_snapshots(bundle.id)
    return {snap.snapshot_kind: snap.snapshot_uuid for _i, snap in pairs}


@pytest.mark.asyncio
async def test_reuse_links_live_independent_rows_even_when_mock_payload_differs(
    db_session,
) -> None:
    repo = InvestmentSnapshotsRepository(db_session)
    svc = SnapshotBundleEnsureService(db_session, clock=_frozen_clock())
    purpose = f"rob380_reuse_{uuid.uuid4().hex[:8]}"

    # 1. Live bundle with account-independent + account-bound evidence.
    live = await svc.ensure(
        EnsureBundleRequest(
            purpose=purpose,
            market="us",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            mode="ensure_fresh",
            manual_snapshots={
                "market": [_manual("market", account_scope="kis_live",
                                   payload={"idx": "live-market"})],
                "news": [_manual("news", account_scope="kis_live",
                                 payload={"n": "live-news"})],
                "portfolio": [_manual("portfolio", account_scope="kis_live",
                                      payload={"p": "live"})],
                "journal": [_manual("journal", account_scope="kis_live",
                                    payload={"j": "live"})],
                "watch_context": [_manual("watch_context", account_scope="kis_live",
                                          payload={"w": "live"})],
            },
        )
    )
    await db_session.commit()
    assert live.bundle_uuid is not None

    # 2. Mock bundle reuses live independent rows; account-bound supplied fresh
    #    for kis_mock. CRUCIALLY the mock's market/news manual data DIFFERS — it
    #    must be IGNORED in favor of linking the live rows.
    mock = await svc.ensure_reusing_account_independent(
        EnsureBundleRequest(
            purpose="mock_preview_report",
            market="us",
            account_scope="kis_mock",
            policy_version="intraday_action_report_v1",
            mode="ensure_fresh",
            manual_snapshots={
                "market": [_manual("market", account_scope="kis_mock",
                                   payload={"idx": "MOCK-DIFFERENT"})],
                "news": [_manual("news", account_scope="kis_mock",
                                 payload={"n": "MOCK-DIFFERENT"})],
                "portfolio": [_manual("portfolio", account_scope="kis_mock",
                                      payload={"p": "mock"})],
                "journal": [_manual("journal", account_scope="kis_mock",
                                    payload={"j": "mock"})],
                "watch_context": [_manual("watch_context", account_scope="kis_mock",
                                          payload={"w": "mock"})],
            },
        ),
        reuse_from_bundle_uuid=live.bundle_uuid,
    )
    await db_session.commit()
    assert mock.bundle_uuid is not None
    assert mock.bundle_uuid != live.bundle_uuid

    live_uuids = await _uuids_by_kind(repo, live.bundle_uuid)
    mock_uuids = await _uuids_by_kind(repo, mock.bundle_uuid)

    # Account-INDEPENDENT: SAME row, despite the mock's differing manual payload.
    for kind in ("market", "news"):
        assert mock_uuids[kind] == live_uuids[kind], (
            f"{kind} must be the reused live row, not a re-collected one"
        )

    # Account-BOUND: DIFFERENT rows per scope.
    for kind in ("portfolio", "journal", "watch_context"):
        assert mock_uuids[kind] != live_uuids[kind]


@pytest.mark.asyncio
async def test_reuse_raises_when_live_bundle_missing(db_session) -> None:
    svc = SnapshotBundleEnsureService(db_session, clock=_frozen_clock())
    with pytest.raises(LiveBundleNotFoundForReuse):
        await svc.ensure_reusing_account_independent(
            EnsureBundleRequest(
                purpose="mock_preview_report",
                market="us",
                account_scope="kis_mock",
                policy_version="intraday_action_report_v1",
                mode="ensure_fresh",
            ),
            reuse_from_bundle_uuid=uuid.uuid4(),
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/services/investment_snapshots/test_mock_reuse_ensure.py -v`
Expected: FAIL with `ImportError: cannot import name 'LiveBundleNotFoundForReuse'` (and the method does not exist)

- [ ] **Step 3: Implement the exception + method**

In `app/services/action_report/common/snapshot_bundle.py`:

(a) Add the import for the account-independence helper near the other `investment_snapshots` imports (after line 57):

```python
from app.services.investment_snapshots.scope_policy import is_account_independent
```

(b) Add the typed exception just above the `SnapshotBundleEnsureService` class (before line 64):

```python
class LiveBundleNotFoundForReuse(Exception):
    """ROB-380 — the live bundle whose account-independent rows should be reused
    could not be resolved. Fail-closed: callers must NOT silently re-collect."""
```

(c) Add the public method to the class, immediately after `ensure()` (after line 286):

```python
    async def ensure_reusing_account_independent(
        self,
        request: EnsureBundleRequest,
        *,
        reuse_from_bundle_uuid,  # uuid.UUID
    ) -> EnsureBundleResponse:
        """ROB-380 — build a bundle that REUSES ``reuse_from_bundle_uuid``'s
        account-independent (NULL-scope) snapshot rows and collects ONLY
        account-bound kinds fresh for ``request.account_scope``.

        Fail-closed: account-independent kinds (market/news/candidate_universe/
        symbol) are NEVER collected here — they are LINKED from the live bundle,
        guaranteeing the live and mock reports cite the SAME ``snapshot_uuid``.
        Account-bound kinds (portfolio/journal/watch_context/pending_orders) are
        collected fresh so they reflect the mock account.
        """
        policy = get_policy(request.policy_version)
        now = self._clock()

        reuse_snaps = await self._repo.list_account_independent_bundle_snapshots(
            reuse_from_bundle_uuid
        )
        if not reuse_snaps:
            # Either the bundle does not exist or it carries no independent rows.
            # Distinguish so callers can fail-closed instead of re-collecting.
            live_bundle = await self._repo.get_bundle_by_uuid(reuse_from_bundle_uuid)
            if live_bundle is None:
                raise LiveBundleNotFoundForReuse(
                    f"live bundle not found for reuse: {reuse_from_bundle_uuid}"
                )
        reuse_by_kind: dict[str, list[Any]] = {}
        for snap in reuse_snaps:
            reuse_by_kind.setdefault(snap.snapshot_kind, []).append(snap)

        run = await self._repo.insert_run(
            SnapshotRunCreate(
                purpose="report_generation",
                market=request.market,
                account_scope=request.account_scope,
                requested_by=request.requested_by,
                policy_version=policy.policy_version,
                policy_snapshot_json=policy.to_snapshot_json(),
                refresh_reason=(
                    f"ensure_mock_reuse purpose={request.purpose} "
                    f"reuse_from={reuse_from_bundle_uuid}"
                ),
                run_metadata={
                    "ensure_request": {
                        "purpose": request.purpose,
                        "mode": "reuse_account_independent",
                        "reuse_from_bundle_uuid": str(reuse_from_bundle_uuid),
                        "user_id": request.user_id,
                    }
                },
            )
        )

        coverage: dict[str, dict[str, str]] = {"required": {}, "optional": {}}
        freshness_summary: dict[str, dict[str, Any]] = {}
        missing_sources: list[str] = []
        warnings: list[str] = []
        linked_items: list[tuple[Any, str]] = []

        for kind_policy in policy.kinds:
            kind = kind_policy.snapshot_kind
            bucket = "required" if kind_policy.required else "optional"
            role = bucket

            if is_account_independent(kind):
                # REUSE branch — fail-closed: never call a collector here.
                reused = reuse_by_kind.get(kind, [])
                if not reused:
                    # Independent kind absent from the live bundle. Mark a gap only
                    # if it was required; optional absences are silent (the live
                    # bundle may legitimately lack e.g. news).
                    if kind_policy.required:
                        coverage[bucket][kind] = "unavailable"
                        freshness_summary[kind] = {
                            "status": "unavailable",
                            **build_kind_diagnostic("unavailable", None),
                        }
                        missing_sources.append(kind)
                    continue
                statuses: list[str] = []
                last_as_of: dt.datetime | None = None
                for snap in reused:
                    linked_items.append((snap.snapshot_uuid, role))
                    statuses.append(snap.freshness_status)
                    last_as_of = snap.as_of
                worst_status = _worst_status(statuses)
                coverage[bucket][kind] = worst_status
                freshness_summary[kind] = {
                    "status": worst_status,
                    "as_of": last_as_of.isoformat() if last_as_of else None,
                    "result_count": str(len(reused)),
                    "reused_from_bundle": str(reuse_from_bundle_uuid),
                }
                continue

            # ACCOUNT-BOUND branch — collect fresh for request.account_scope.
            results, kind_warnings, attempted = await self._collect_for_kind(
                kind_policy=kind_policy,
                request=request,
                policy_snapshot=policy.to_snapshot_json(),
            )
            warnings.extend(kind_warnings)
            if not results:
                if kind_policy.required or attempted:
                    coverage[bucket][kind] = "unavailable"
                    freshness_summary[kind] = {
                        "status": "unavailable",
                        **build_kind_diagnostic("unavailable", None),
                    }
                    missing_sources.append(kind)
                continue
            kind_statuses: list[str] = []
            last_bound_as_of: dt.datetime | None = None
            for result in results:
                snap, effective_status = await self._insert_collected_snapshot(
                    run_uuid=run.run_uuid,
                    kind_policy=kind_policy,
                    result=result,
                )
                linked_items.append((snap.snapshot_uuid, role))
                kind_statuses.append(effective_status)
                last_bound_as_of = result.as_of
            worst_bound = _worst_status(kind_statuses)
            coverage[bucket][kind] = worst_bound
            freshness_summary[kind] = {
                "status": worst_bound,
                "as_of": last_bound_as_of.isoformat() if last_bound_as_of else None,
                "result_count": str(len(results)),
            }

        bundle_status = _derive_bundle_status(coverage)
        bundle = await self._repo.insert_bundle(
            BundleCreate(
                purpose=request.purpose,
                market=request.market,
                account_scope=request.account_scope,
                policy_version=policy.policy_version,
                policy_snapshot_json=policy.to_snapshot_json(),
                as_of=now,
                status=bundle_status,
                coverage_summary=coverage,
                freshness_summary=freshness_summary,
            )
        )
        for snapshot_uuid, role in linked_items:
            await self._repo.link_bundle_item(
                bundle_uuid=bundle.bundle_uuid,
                item=BundleItemCreate(snapshot_uuid=snapshot_uuid, role=role),
            )

        return EnsureBundleResponse(
            bundle_uuid=bundle.bundle_uuid,
            status=bundle_status,
            created=True,
            coverage_summary=coverage,
            freshness_summary=freshness_summary,
            missing_sources=missing_sources,
            warnings=warnings,
            run_uuid=run.run_uuid,
        )
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run pytest tests/services/investment_snapshots/test_mock_reuse_ensure.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the ensure-service safety net again**

Run: `uv run pytest tests/services/investment_snapshots/ -v`
Expected: PASS (all snapshot service tests green)

- [ ] **Step 6: Commit**

```bash
git add app/services/action_report/common/snapshot_bundle.py tests/services/investment_snapshots/test_mock_reuse_ensure.py
git commit -m "feat(ROB-380): ensure_reusing_account_independent links live NULL-scope rows

Account-independent evidence is collected once (on the live bundle) and LINKED
into the kis_mock bundle; only account-bound kinds collected fresh. Fail-closed:
independent kinds are never re-collected on this path.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Wire the mock runner to the reuse path (read `live.snapshot_bundle_uuid`)

**Files:**
- Modify: `app/services/investment_reports/mock_preview/runner.py:90-103`
- Test: `tests/services/investment_reports/mock_preview/test_runner.py`

- [ ] **Step 1: Write the failing runtime-invariant test**

Append to `tests/services/investment_reports/mock_preview/test_runner.py`:

```python
@pytest.mark.asyncio
async def test_runner_reuses_live_bundle_account_independent_rows(db_session) -> None:
    """ROB-380 runtime invariant: when the live report carries a snapshot_bundle_uuid,
    the mock bundle cites the SAME account-independent snapshot rows (shared), while
    account-bound rows stay distinct per scope."""
    import datetime as _dt

    from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
    from app.services.action_report.common.snapshot_bundle import (
        SnapshotBundleEnsureService,
    )
    from app.services.investment_snapshots.collectors import SnapshotCollectResult
    from app.services.investment_snapshots.repository import (
        InvestmentSnapshotsRepository,
    )

    fixed_now = _dt.datetime(2025, 1, 15, 9, 0, tzinfo=_dt.UTC)

    def _manual(kind, scope, payload):
        return SnapshotCollectResult(
            snapshot_kind=kind,
            market="us",
            account_scope=scope,
            source_kind="manual",
            payload_json=payload,
            as_of=fixed_now,
            freshness_status="fresh",
        )

    snap_repo = InvestmentSnapshotsRepository(db_session)
    ensure = SnapshotBundleEnsureService(db_session, clock=lambda: fixed_now)

    # 1. Build a live bundle with account-independent + account-bound evidence.
    live_bundle = await ensure.ensure(
        EnsureBundleRequest(
            purpose="snapshot_backed_report",
            market="us",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            mode="ensure_fresh",
            manual_snapshots={
                "market": [_manual("market", "kis_live", {"m": "L"})],
                "news": [_manual("news", "kis_live", {"n": "L"})],
                "portfolio": [_manual("portfolio", "kis_live", {"p": "L"})],
                "journal": [_manual("journal", "kis_live", {"j": "L"})],
                "watch_context": [_manual("watch_context", "kis_live", {"w": "L"})],
            },
        )
    )
    await db_session.flush()
    assert live_bundle.bundle_uuid is not None

    # 2. Seed a live report that LINKS to that bundle (as the generator does).
    live_req = IngestReportRequest(
        report_type="snapshot_backed_advisory_v1",
        market="us",
        market_session="regular",
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile="seed",
        title="t",
        summary="s",
        status="draft",
        generator_version="v2-snapshot-backed",
        kst_date="2026-05-30",
        snapshot_bundle_uuid=live_bundle.bundle_uuid,
        items=[
            IngestReportItem(
                client_item_key="seed1",
                item_kind="action",
                side="buy",
                intent="buy_review",
                rationale="seed",
                symbol="AAPL",
                evidence_snapshot={"reference_price_usd": 200.0},
                max_action={"notional_usd": 50.0},
            )
        ],
    )
    live_report = await InvestmentReportIngestionService(db_session).ingest(live_req)
    await db_session.flush()

    # 3. Run the mock runner with a reuse-capable ensure service whose account-bound
    #    collection comes from manual snapshots (kis_mock).
    class _ReuseEnsure(SnapshotBundleEnsureService):
        async def ensure_reusing_account_independent(self, request, *, reuse_from_bundle_uuid):  # noqa: ANN001
            request = request.model_copy(
                update={
                    "manual_snapshots": {
                        "portfolio": [_manual("portfolio", "kis_mock", {"p": "M"})],
                        "journal": [_manual("journal", "kis_mock", {"j": "M"})],
                        "watch_context": [_manual("watch_context", "kis_mock", {"w": "M"})],
                    }
                }
            )
            return await super().ensure_reusing_account_independent(
                request, reuse_from_bundle_uuid=reuse_from_bundle_uuid
            )

    runner = MockPreviewReportRunner(
        db_session, ensure_service=_ReuseEnsure(db_session, clock=lambda: fixed_now)
    )
    mock_report, _reused, _count = await runner.run(
        live_report_uuid=live_report.report_uuid,
        market="us",
        market_session="regular",
        policy_version="intraday_action_report_v1",
        kst_date="2026-05-30",
        created_by_profile="schedule",
    )
    await db_session.flush()

    # 4. Assert shared independent rows, distinct account-bound rows.
    async def _by_kind(bundle_uuid):
        b = await snap_repo.get_bundle_by_uuid(bundle_uuid)
        pairs = await snap_repo.list_bundle_items_with_snapshots(b.id)
        return {s.snapshot_kind: s.snapshot_uuid for _i, s in pairs}

    live_uuids = await _by_kind(live_bundle.bundle_uuid)
    mock_uuids = await _by_kind(mock_report.snapshot_bundle_uuid)

    assert mock_uuids["market"] == live_uuids["market"]
    assert mock_uuids["news"] == live_uuids["news"]
    assert mock_uuids["portfolio"] != live_uuids["portfolio"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest "tests/services/investment_reports/mock_preview/test_runner.py::test_runner_reuses_live_bundle_account_independent_rows" -v`
Expected: FAIL — the runner currently calls `self._ensure.ensure(...)` (re-collect) so `mock_uuids["market"] != live_uuids["market"]`.

- [ ] **Step 3: Implement the runner branch**

In `app/services/investment_reports/mock_preview/runner.py`, replace lines 90-103 (the single `ensure_resp = await self._ensure.ensure(...)` block) with:

```python
        # ROB-380 — reuse the live bundle's account-independent (NULL-scope)
        # snapshot rows instead of re-collecting them, so the live and mock
        # reports cite the SAME snapshot_uuid. Account-bound kinds are still
        # collected fresh for kis_mock. Fall back to independent collection only
        # when the live report has no bundle to reuse (legacy / pre-ROB-373 rows).
        if live.snapshot_bundle_uuid is not None:
            ensure_resp = await self._ensure.ensure_reusing_account_independent(
                EnsureBundleRequest(
                    purpose="mock_preview_report",
                    market=market,  # type: ignore[arg-type]
                    account_scope="kis_mock",
                    policy_version=policy_version,
                    mode="ensure_fresh",
                    requested_by="claude_code",
                    user_id=user_id,
                ),
                reuse_from_bundle_uuid=live.snapshot_bundle_uuid,
            )
        else:
            ensure_resp = await self._ensure.ensure(
                EnsureBundleRequest(
                    purpose="mock_preview_report",
                    market=market,  # type: ignore[arg-type]
                    account_scope="kis_mock",
                    policy_version=policy_version,
                    mode="ensure_fresh",
                    requested_by="claude_code",
                    user_id=user_id,
                )
            )
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `uv run pytest "tests/services/investment_reports/mock_preview/test_runner.py::test_runner_reuses_live_bundle_account_independent_rows" -v`
Expected: PASS

- [ ] **Step 5: Run the full mock_preview suite (existing tests use the fallback branch since seeded reports have no bundle_uuid)**

Run: `uv run pytest tests/services/investment_reports/mock_preview/ -v`
Expected: PASS — existing tests (`_StubEnsureService` only implements `ensure`) still pass because their seeded reports have `snapshot_bundle_uuid=None` and take the fallback branch.

- [ ] **Step 6: Commit**

```bash
git add app/services/investment_reports/mock_preview/runner.py tests/services/investment_reports/mock_preview/test_runner.py
git commit -m "feat(ROB-380): mock runner reuses live bundle's account-independent rows

Reads live.snapshot_bundle_uuid (persisted by the generator) and routes to
ensure_reusing_account_independent; falls back to independent collection only
when no live bundle exists. Adds the runtime shared-row invariant test.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Update the runbook caveat (ROB-379 limitation → ROB-380 resolved)

**Files:**
- Modify: `docs/runbooks/invest-reports-us-schedule.md`

- [ ] **Step 1: Find the ROB-379 known-limitation caveat**

Run: `grep -n "known limitation\|reuse\|NULL-scope\|ROB-379\|double" docs/runbooks/invest-reports-us-schedule.md`
Expected: locate the caveat paragraph added by ROB-379 PR #1041 stating cross-report reuse does not happen at runtime.

- [ ] **Step 2: Replace the caveat with the resolved behavior**

Edit the located section so it reads (adapt heading/anchor to the file's existing style):

```markdown
### Account-independent evidence reuse (ROB-380)

The mock_preview report **reuses** the live advisory report's account-independent
snapshots instead of re-collecting them. In one `--run` pass:

- `market` / `news` / `candidate_universe` / `symbol` snapshots are collected
  **once** (on the live bundle) and the **same** `snapshot_uuid` rows are linked
  into the kis_mock bundle.
- `portfolio` / `journal` / `watch_context` / `pending_orders` are collected
  **fresh** for the kis_mock account (distinct rows per scope).

The mock runner reads the live report's `snapshot_bundle_uuid` and routes through
`SnapshotBundleEnsureService.ensure_reusing_account_independent`. If a live report
predates ROB-373 and has no bundle, the runner falls back to independent
collection (the only option when there is nothing to reuse).

**Operator verification (read-only)** after a `--run`: confirm the live and mock
reports' account-independent snapshots share rows:

​```sql
-- shared independent rows should be > 0
SELECT s.snapshot_kind, COUNT(DISTINCT bi.bundle_id) AS bundles
FROM review.investment_snapshots s
JOIN review.investment_snapshot_bundle_items bi ON bi.snapshot_id = s.id
JOIN review.investment_snapshot_bundles b ON b.id = bi.bundle_id
WHERE s.account_scope IS NULL
  AND s.snapshot_kind IN ('market','news','candidate_universe','symbol')
  AND b.bundle_uuid IN (:live_bundle_uuid, :mock_bundle_uuid)
GROUP BY s.snapshot_kind
HAVING COUNT(DISTINCT bi.bundle_id) = 2;  -- a row here = shared across both bundles
​```
```

(Remove the stale "this reuse does NOT happen at runtime / known limitation" text.)

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/invest-reports-us-schedule.md
git commit -m "docs(ROB-380): runbook — account-independent evidence reuse now realized

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Full verification (lint + type + targeted tests + import guards)

Per the project's pre-merge gate (branch protection does NOT gate lint/test): run the exact CI commands and confirm green before any PR.

- [ ] **Step 1: Ruff lint AND format check (both — `ruff check` alone is not enough)**

Run:
```bash
uv run ruff check app/ tests/ scripts/
uv run ruff format --check app/ tests/ scripts/
```
Expected: both pass. If `format --check` fails, run `uv run ruff format app/ tests/ scripts/` and re-commit.

- [ ] **Step 2: Type check the touched modules**

Run: `uv run ty check app/services/action_report/common/snapshot_bundle.py app/services/investment_snapshots/repository.py app/services/investment_reports/mock_preview/runner.py`
Expected: PASS (no new errors).

- [ ] **Step 3: Run the full set of affected tests**

Run:
```bash
uv run pytest \
  tests/services/investment_snapshots/ \
  tests/services/investment_reports/mock_preview/ \
  -v
```
Expected: PASS (all green, including the no-mutation-imports guard `test_no_mutation_imports.py`).

- [ ] **Step 4: Confirm no broker/order/watch mutation imports were introduced**

Run: `uv run pytest tests/services/investment_reports/mock_preview/test_no_mutation_imports.py -v`
Expected: PASS — the reuse path adds only read/link calls; this guard proves no order/submit surface leaked in.

- [ ] **Step 5: Final commit if formatting changed anything**

```bash
git add -A
git commit -m "chore(ROB-380): apply ruff format + verification fixups

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>" || echo "nothing to commit"
```

---

## Operator-Gated Acceptance (post-merge, not part of the code PR)

The runtime "shared-row count > 0" acceptance criterion is only closeable on the operator host with KIS creds (per ROB-379: creds at `~/services/auto_trader/shared/.env.prod.native`, inject `KIS_MOCK_*` via dotenv only, never source wholesale). After merge, an operator runs:

```bash
INVEST_REPORTS_US_SCHEDULE_ENABLED=true \
  uv run python -m scripts.invest_reports_us_schedule --run --kst-date <YYYY-MM-DD>
```

then runs the read-only SQL from Task 5 Step 2 against the live + mock bundle UUIDs printed in the logs and confirms the shared-row query returns rows for the independent kinds. This is the same evidence shape ROB-379 used to prove the bug; ROB-380 is done when it returns shared rows (was 0).

---

## Self-Review (completed during planning)

- **Spec coverage** — AC1 (same `snapshot_uuid` for independent kinds): Task 3 + Task 4 tests. AC2 (account-bound distinct per scope): asserted in both tests. AC3 (no double-collection): the reuse branch never calls a collector for independent kinds (fail-closed) — asserted by the "differing mock payload is ignored" test. AC4 (operator `--run` re-verifies shared rows): Operator-Gated section + runbook SQL. AC5 (unit + runtime invariant tests): Tasks 1, 3, 4. Non-goals (no mutation, no migration): Task 6 Step 4 guard; no `alembic` change anywhere in the plan.
- **Placeholder scan** — no TBD/"handle edge cases"/"similar to": every code step shows full code.
- **Type consistency** — method name `ensure_reusing_account_independent` and `list_account_independent_bundle_snapshots` used identically across Tasks 1/3/4; exception `LiveBundleNotFoundForReuse` defined in Task 3 and imported in the Task 3 test; `_insert_collected_snapshot` defined in Task 2 and consumed in Tasks 2 (ensure) and 3.
