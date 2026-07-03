# Toss Reconcile — Eliminate per-row /accounts N+1 via one shared client + diagnose the swallowed batch build failure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax.

**Goal:** Cut `toss_reconcile_orders` wall time (Sentry 7d: avg 26.3s, p95 82.5s — worst of all transactions) by removing the per-ledger-row `GET /api/v1/accounts` N+1. The bottleneck is an *uninstrumented* rate-limiter sleep: the ACCOUNT group is capped at **1 TPS** (`rate_limiter.py:27`), and ~27 fresh `TossReadClient` instances per run each resolve their own account-seq header via `/accounts`, serializing into ~22.7s of `acquire()` sleep (187 `/accounts` calls across 7 runs; `http.client` sums 40.8s of a 183.7s transaction total → ~78% is uninstrumented ACCOUNT sleep). The N+1 is a *fallback* path that only runs because ROB-669's `TossBatchEvidenceSource.build` (which resolves account-seq ONCE and batches evidence) is failing every run and being silently swallowed (`toss_live_ledger.py:590-597` → `evidence_source=None`). This plan (1) makes the swallowed build failure observable and DIAGNOSES + fixes its real root cause, and (2) makes the whole reconcile run reuse ONE `TossReadClient` so account-seq is resolved at most once even when batch build is unavailable — defense-in-depth that neutralizes the N+1 regardless of the batch path. `asyncio.gather` over rows is explicitly rejected (the shared 1-TPS ACCOUNT limiter serializes concurrent calls anyway — the fix must reduce call COUNT, not add concurrency).

**Architecture (current → target):** Today `toss_reconcile_orders_impl` (`toss_live_ledger.py:547`) builds a batch source with `TossBatchEvidenceSource.build(rows=rows, symbol=symbol)` — **no client passed**, so `build` news its own `TossReadClient.from_settings()` (`toss_live_evidence.py:215`). When that build raises, the `except` at `toss_live_ledger.py:590-597` logs a one-line WARNING (`"...: %s", exc` — no stack, not sent to Sentry) and sets `evidence_source=None`. The per-row loop then falls back to `TossEvidenceAdapter().fetch_evidence(row)` (`toss_live_ledger.py:231`), and that adapter news a **fresh** `TossReadClient.from_settings()` **per row** (`toss_live_evidence.py:157`). Each fresh client has an empty per-instance `_account_seq` cache (`client.py:63`), so its first `account_required` call triggers `_resolve_account_seq()` → `GET /api/v1/accounts` (`client.py:98-99, 127-136`), each gated by the 1-TPS ACCOUNT limiter (`rate_limiter.py:57-73`). N clients ⇒ N `/accounts` ⇒ N seconds of serialized sleep. **Target:** `toss_reconcile_orders_impl` constructs exactly one `TossReadClient` per run, passes it into `TossBatchEvidenceSource.build(..., client=shared)` (so `build` no longer news its own and `owns_client=False`), and threads it into the per-row fallback via `TossEvidenceAdapter(client=shared)` — which reuses the injected client and does **not** close it. Because a single client instance caches `_account_seq` after the first resolution (`client.py:128-129,135`), `/accounts` is called **at most once** per run (0 if the operator sets `toss_api_account_seq`), regardless of whether batch build succeeds or falls back. Separately, the swallowed build failure is upgraded to `logger.exception` + `sentry_sdk.capture_exception` + a `batch_build_error` echo in the tool result, and its real root cause is reproduced, named, regression-tested, and fixed.

**Tech Stack:** Python 3.13, uv, pytest (markers `unit`/`asyncio`), SQLAlchemy async, httpx (`httpx.MockTransport` for client-level tests), FastMCP tool registration, pydantic v2, Redis (OAuth token cache), `sentry_sdk`. Toss Open API read-only GETs: `GET /api/v1/accounts`, `GET /api/v1/orders`, `GET /api/v1/orders/{orderId}`.

## Global Constraints

Copied verbatim; every task implicitly includes these.

- **fix (3) requires DIAGNOSING the actual swallowed exception, not assuming — include a task that reproduces `TossBatchEvidenceSource.build` failure and names the root cause before patching.**
- **hardcoding `toss_api_account_seq` (fix 1) bypasses the "exactly one account" guard in `client.py:~131-134` — the configured value must match the intended single account; the plan must preserve or re-assert that guard.**
- **All three legs touch only read-only GETs (accounts/get_order/list_orders); reconcile DB writes and evidence-gated booking are unchanged; no broker mutation.**
- **migration-0.** No new DB column, no new `status` enum value, no alembic revision. The `status` CHECK constraint (`app/models/review.py:513-519`) and every ledger column are untouched. All changes are in the reconcile read/evidence path.
- **Read-only path / no broker-order-watch mutation.** No `place_order` / `modify_order` / `cancel_order` / watch-trigger mutation is reachable from any code changed here. Send-time rows stay accepted-only; fills/journals/realized_pnl are still booked ONLY from confirmed execution evidence via the existing `_reconcile_one_toss_row` booking calls.
- **Approach decision (config vs runtime resolution): prefer runtime single-resolution over hardcoding config.** We do NOT set `settings.toss_api_account_seq` to a literal. The single shared client resolves account-seq once at runtime (instance cache), which keeps the `_resolve_account_seq` "exactly one account" guard (`client.py:131-134`) ACTIVE and untouched. Config `toss_api_account_seq` (`config.py:246`) remains an optional operator fast-path (if set → 0 `/accounts`, and the operator is responsible for it matching the single account). This is lower-risk than hardcoding: no config change is required to fix the bug, and the guard cannot be silently bypassed.
- **`classify_toss_order_evidence(order)` is reused UNCHANGED.** The shared-client wiring changes only *which client instance* fetches evidence, never how evidence is classified or booked.
- **Backward-compatible signatures.** `TossEvidenceAdapter.__init__` gains an optional `client=None` (legacy new-and-close path preserved for `client=None`). `_reconcile_one_toss_row(row, *, dry_run, evidence_source=None)` gains an optional `fallback_client=None`. The public `toss_reconcile_orders_impl(*, symbol, order_id, market, dry_run, limit)` keyword signature and the `toss_reconcile_orders` MCP tool signature are BOTH unchanged, so the paused TaskIQ caller (`app/tasks/toss_live_reconcile_tasks.py:35`) and the MCP wrapper (`orders_toss_variants.py:1698`) are unaffected.
- Run tests: `uv run pytest <path> -v`. Lint: `make lint`. Do NOT commit unless the executing skill says so; each task lists its own commit message.

---

## File Structure

| File | Create/Modify | Responsibility (Task) |
|------|---------------|-----------------------|
| `app/mcp_server/tooling/toss_live_ledger.py` | Modify | Task 1 — surface the swallowed batch-build failure (`logger.exception` + `sentry_sdk.capture_exception` + `batch_build_error` echo). Task 3 — construct one shared `TossReadClient`, pass to `build(..., client=shared)`, thread `fallback_client` into `_reconcile_one_toss_row`, close once. |
| `app/mcp_server/tooling/toss_live_evidence.py` | Modify | Task 2 — `TossEvidenceAdapter.__init__(client=None)`: reuse an injected client without closing it; keep the legacy new-and-close path for `client=None`. |
| `docs/runbooks/toss-live-order-reconcile.md` | Modify | Task 4 — document the diagnosed root cause, the shared-client / account-seq behavior, and how to read `batch_build_error` in the tool output. |
| `tests/mcp_server/tooling/test_toss_reconcile_resilience.py` | Modify | Task 1 tests (build-failure surfaced + captured + still falls back). |
| `tests/mcp_server/tooling/test_toss_live_evidence.py` | Modify | Task 2 tests (adapter reuses injected client, does not close it; legacy path unchanged). |
| `tests/mcp_server/tooling/test_toss_reconcile_account_seq.py` | Create | Task 3 run-level test (one shared client across a multi-row run; reused by batch build + per-row fallback; closed once). |
| `tests/services/brokers/toss/test_client.py` | Modify | Task 3 client-level regression locks (account-seq resolved once then cached; exactly-one-account guard preserved). |
| `tests/mcp_server/tooling/test_toss_batch_evidence_source.py` | Modify | Task 4 regression test reproducing the diagnosed real build failure (fake-client shape/error). |

> **NOT touched:**
> - **`app/services/brokers/toss/client.py`** — `_resolve_account_seq` and its "exactly one account" guard (`:131-134`) and per-instance cache (`:135`) are the *mechanism* we rely on; they are correct and stay verbatim. The fix is to reuse one client instance, not to change resolution.
> - **`app/services/brokers/toss/rate_limiter.py`** — the ACCOUNT=1-TPS bucket (`:27`) is a real Toss limit; we reduce the number of ACCOUNT calls, we do not touch the limiter.
> - **`app/mcp_server/tooling/orders_toss_variants.py`** — the `toss_reconcile_orders` MCP wrapper / `register_toss_live_order_tools` are left exactly as-is (no signature or description change).
> - **`app/config` / `.env`** — we deliberately do NOT hardcode `toss_api_account_seq` (see the approach decision); runtime single-resolution keeps the guard active.
> - **`TossBatchEvidenceSource` windowing/pagination logic** — only its `build(..., client=)` injection is exercised; the CLOSED window + cap logic (`toss_live_evidence.py:227-270`) is unchanged except as required by Task 4's diagnosed fix.

---

## Task 1 — Surface the swallowed `TossBatchEvidenceSource.build` failure (Fix 3a, observability, migration-0)

The batch-build `except` (`toss_live_ledger.py:590-597`) currently emits a single-line WARNING with `%s` of the exception — no stack trace, not sent to Sentry, not echoed in the tool result. That is exactly why the failure has been invisible for so long (Sentry sees only the downstream N+1, never the cause). This task makes the failure observable so Task 4 can diagnose it, WITHOUT changing the fail-open fallback behavior.

**Files:**
- Modify `app/mcp_server/tooling/toss_live_ledger.py` — add `import sentry_sdk` near the top imports (`:1-33`); replace the `except` body at `:590-597` with `logger.exception(...)` + `sentry_sdk.capture_exception(exc)` + set a local `batch_build_error` dict; initialize `batch_build_error = None` before the `if rows:` block (`:584`); echo it in `result` (`:614-624`).
- Test (modify) `tests/mcp_server/tooling/test_toss_reconcile_resilience.py`.

**Interfaces:**
- Consumes: `sentry_sdk.capture_exception(exc)` (same library already used in `app/mcp_server/tooling/analysis_analyze.py:10`).
- Produces: `toss_reconcile_orders_impl(...)` result gains an optional key `"batch_build_error": {"type": str, "message": str} | None`. No signature change. Fail-open fallback (`evidence_source=None` → per-row) is unchanged.

Steps:

- [ ] **Write failing test — a batch-build failure is captured to Sentry + echoed, and per-row fallback still reconciles.** Append to `tests/mcp_server/tooling/test_toss_reconcile_resilience.py` (the `_patch_session_factory`, `_clean`, `_accepted` fixtures/helpers already exist in that file):
```python
async def test_batch_build_failure_is_surfaced_and_captured(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    await _accepted(db_session)

    pending = TossFillEvidence(
        verdict="pending", local_status="pending", broker_status="PENDING",
        filled_qty=Decimal("0"), avg_price=None, commission=None, tax=None,
        fee_total=Decimal("0"), settlement_date=None,
        raw_order={"status": "PENDING"}, reason="pending",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=pending)

    with (
        patch.object(
            mod.TossBatchEvidenceSource,
            "build",
            new=AsyncMock(side_effect=RuntimeError("boom-build-fails")),
        ),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(mod.sentry_sdk, "capture_exception") as capture,
    ):
        out = await mod.toss_reconcile_orders_impl(dry_run=False)

    # fail-open: per-row fallback still reconciles the row
    assert out["counts"] == {"pending": 1}
    # observability: the real exception reaches Sentry + is echoed for operators
    capture.assert_called_once()
    assert out["batch_build_error"]["type"] == "RuntimeError"
    assert "boom-build-fails" in out["batch_build_error"]["message"]
```

- [ ] **Run it — fails.** `uv run pytest tests/mcp_server/tooling/test_toss_reconcile_resilience.py -k surfaced -v`
  Expected: `AttributeError: <module ...> does not have the attribute 'sentry_sdk'` (not imported yet) and `KeyError: 'batch_build_error'` (not echoed).

- [ ] **Minimal impl — import sentry, upgrade the except, echo the error.** In `app/mcp_server/tooling/toss_live_ledger.py`:
  1. Add `import sentry_sdk` alongside the existing top-level imports (with `import httpx` / `import logging` near `:1-8`).
  2. Before the `if rows:` batch build (currently `:584`), initialize `batch_build_error: dict[str, Any] | None = None`.
  3. Replace the `except` body (`:590-597`) with:
```python
        except Exception as exc:  # noqa: BLE001 — batch is an optimization
            # Any batch-build failure (disabled/network/transient/bug) degrades to
            # the per-row single-fetch path; per-row R1 classification still applies.
            # ROB-687: this was previously a silent one-line WARNING, which hid the
            # root cause of the /accounts N+1 for weeks — surface it with a stack
            # trace + Sentry capture + a result echo so it can be diagnosed.
            logger.exception(
                "toss reconcile batch evidence build failed; per-row fallback"
            )
            sentry_sdk.capture_exception(exc)
            batch_build_error = {"type": type(exc).__name__, "message": str(exc)}
            evidence_source = None
```
  4. In the `result` dict assembly (`:614-624`), add after `"reopened": reopen_report,`:
```python
        "batch_build_error": batch_build_error,
```

- [ ] **Run it — passes.** `uv run pytest tests/mcp_server/tooling/test_toss_reconcile_resilience.py -k surfaced -v` → 1 passed.

- [ ] **Regression — existing resilience + fallback tests unaffected.** `uv run pytest tests/mcp_server/tooling/test_toss_reconcile_resilience.py -v`
  Expected: all pass, including `test_impl_batch_build_failure_falls_back_per_row` (fail-open behavior preserved) and `test_impl_uses_batch_source_and_echoes_window` (success path echoes `batch_build_error: None`).

- [ ] **Lint.** `make lint`

- [ ] **Commit.** `git add -A && git commit -m "obs(ROB-687): surface swallowed toss batch-build failure (sentry + echo)"`

---

## Task 2 — `TossEvidenceAdapter` reuses an injected client without closing it (Fix 2 enabler, migration-0)

To share one client across the run, the per-row fallback adapter must accept an injected client that it does **not** own (the run owns and closes it). Today the adapter unconditionally news + closes a fresh client per call (`toss_live_evidence.py:156-162`), which is the N+1. This task adds the injection point while preserving the legacy new-and-close behavior for `client=None`.

**Files:**
- Modify `app/mcp_server/tooling/toss_live_evidence.py` — add `TossEvidenceAdapter.__init__(self, client: TossReadClient | None = None)` and branch `fetch_evidence` on the injected client (`:155-162`).
- Test (modify) `tests/mcp_server/tooling/test_toss_live_evidence.py`.

**Interfaces:**
- Produces `TossEvidenceAdapter(client: TossReadClient | None = None)`. When `client` is provided: `fetch_evidence(row)` calls `client.get_order(...)` and does NOT `aclose()` it (caller-owned). When `client is None`: unchanged — news `TossReadClient.from_settings()` and `aclose()`s it in `finally`.
- Consumes `TossReadClient.get_order(order_id: str) -> TossOrder` (`client.py:289`), `classify_toss_order_evidence` (`toss_live_evidence.py:78`).

Steps:

- [ ] **Write failing test — injected client is reused and NOT closed; legacy path unchanged.** Append to `tests/mcp_server/tooling/test_toss_live_evidence.py`:
```python
@pytest.mark.asyncio
async def test_adapter_reuses_injected_client_without_closing():
    from app.mcp_server.tooling import toss_live_evidence as ev

    class _Row:
        broker_order_id = "ord-9"

    injected = SimpleNamespace(
        get_order=AsyncMock(
            return_value=_order(
                "FILLED",
                {"filledQuantity": Decimal("1"), "averageFilledPrice": Decimal("10")},
            )
        ),
        aclose=AsyncMock(),
    )

    # from_settings must NOT be called when a client is injected.
    with patch.object(
        ev.TossReadClient, "from_settings", side_effect=AssertionError("newed a client")
    ):
        evidence = await ev.TossEvidenceAdapter(client=injected).fetch_evidence(_Row())

    assert evidence.verdict == "filled"
    injected.get_order.assert_awaited_once_with("ord-9")
    injected.aclose.assert_not_awaited()  # caller owns the shared client
```
  (The existing `test_adapter_fetches_single_order_detail` at the bottom of this file exercises the legacy `client=None` path — keep it; it must stay green.)

- [ ] **Run it — fails.** `uv run pytest tests/mcp_server/tooling/test_toss_live_evidence.py -k injected -v`
  Expected: `TypeError: TossEvidenceAdapter() takes no arguments` (no `__init__` yet).

- [ ] **Minimal impl — inject the client.** Replace `TossEvidenceAdapter` (`toss_live_evidence.py:155-162`) with:
```python
class TossEvidenceAdapter:
    def __init__(self, client: TossReadClient | None = None) -> None:
        # An injected client is caller-owned (shared across the reconcile run) and
        # must NOT be closed here. Only a self-newed client (client=None, legacy
        # path) is closed in fetch_evidence. ROB-687: sharing one client removes
        # the per-row /accounts N+1.
        self._client = client

    async def fetch_evidence(self, row: Any) -> TossFillEvidence:
        if self._client is not None:
            order = await self._client.get_order(str(row.broker_order_id))
            return classify_toss_order_evidence(order)
        client = TossReadClient.from_settings()
        try:
            order = await client.get_order(str(row.broker_order_id))
            return classify_toss_order_evidence(order)
        finally:
            await client.aclose()
```

- [ ] **Run it — passes.** `uv run pytest tests/mcp_server/tooling/test_toss_live_evidence.py -v` → all pass (new injected test + legacy `test_adapter_fetches_single_order_detail`).

- [ ] **Lint.** `make lint`

- [ ] **Commit.** `git add -A && git commit -m "refactor(ROB-687): TossEvidenceAdapter accepts a caller-owned shared client"`

---

## Task 3 — One shared `TossReadClient` per reconcile run; account-seq resolved once (Fix 1 + Fix 2, migration-0)

Construct exactly one `TossReadClient` at the start of the run, hand it to `TossBatchEvidenceSource.build(..., client=shared)` (so `build` no longer news its own and `owns_client=False`), and thread it into the per-row fallback via `_reconcile_one_toss_row(..., fallback_client=shared)`. Because a single client instance caches `_account_seq` after the first resolution (`client.py:128-129,135`), `/accounts` is called at most once per run — even when batch build fails and every row falls back. The shared client is closed exactly once in `finally`.

**Files:**
- Modify `app/mcp_server/tooling/toss_live_ledger.py` — add `from app.services.brokers.toss import TossReadClient` to the imports (`:1-33`); construct the shared client (defensively) before the batch build (`:584`); pass `client=shared_client` to `TossBatchEvidenceSource.build` (`:587-589`); add `fallback_client` to `_reconcile_one_toss_row` (`:214-219`) and use it in the fallback branch (`:228-231`); thread `fallback_client=shared_client` at the call site (`:602-604`); close the shared client in `finally` (`:610-612`).
  > **Line anchors are for the pre-Task-1 file.** Task 1 already edited this same region (added `import sentry_sdk`, a `batch_build_error = None` init, an expanded `except` body, and a `"batch_build_error"` echo), so by the time Task 3 runs the `:584-612` region has drifted down a few lines. The `:214-231` / `:602-604` anchors (in `_reconcile_one_toss_row` and its call site) are unaffected by Task 1. Task 3 supplies a **full-region replacement** for `:584-612`, so locate the region by content (the `evidence_source = None` / `if rows:` batch-build block through the `finally: … aclose()`), not by absolute line number.
- Test (create) `tests/mcp_server/tooling/test_toss_reconcile_account_seq.py`.
- Test (modify) `tests/services/brokers/toss/test_client.py` — client-level regression locks for resolve-once + guard.

**Interfaces:**
- `_reconcile_one_toss_row(row, *, dry_run, evidence_source=None, fallback_client=None)` — the fallback branch becomes `TossEvidenceAdapter(client=fallback_client).fetch_evidence(row)`.
- `toss_reconcile_orders_impl` builds `shared_client = TossReadClient.from_settings()` inside a `try/except` (degrade to `None` if Toss is disabled/misconfigured), passes it to `build(client=shared_client)` and each `_reconcile_one_toss_row(..., fallback_client=shared_client)`, and closes it once (`await shared_client.aclose()`) alongside `evidence_source.aclose()` (which now no-ops for a run-owned client, `owns_client=False`).
- Consumes `TossBatchEvidenceSource.build(*, rows, symbol=None, client=None)` (`toss_live_evidence.py:206-213`) — the `client` kwarg + `owns_client` gate (`:214`, `:281-283`) already exist.

Steps:

- [ ] **Write failing test — the whole run uses ONE shared client (reused by batch build + per-row fallback) and closes it once.** Create `tests/mcp_server/tooling/test_toss_reconcile_account_seq.py`:
```python
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.review import TossLiveOrderLedger
from app.services.brokers.toss.dto import TossOrder
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    await db_session.execute(delete(TossLiveOrderLedger))
    await db_session.commit()
    yield


@pytest.fixture(autouse=True)
def _patch_session_factory(db_session):
    from app.mcp_server.tooling import toss_live_ledger

    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = db_session
    mock_cm.__aexit__.return_value = None
    with patch.object(
        toss_live_ledger, "_order_session_factory", return_value=lambda: mock_cm
    ):
        yield


async def _accepted(db_session, *, cid: str, oid: str):
    return await TossLiveOrderLedgerService(db_session).record_send(
        operation_kind="place", market="kr", symbol="034020", side="buy",
        order_type="limit", time_in_force="DAY", quantity=Decimal("3"),
        price=Decimal("85000"), order_amount=None, currency="KRW",
        client_order_id=cid, broker_order_id=oid, original_order_id=None,
        status="accepted", broker_status=None, response_code="0",
        response_message=None, raw_response={}, thesis="t", strategy="s",
    )


def _pending_order(order_id: str) -> TossOrder:
    return TossOrder(
        order_id=order_id, symbol="034020", side="buy", order_type="limit",
        time_in_force="DAY", status="PENDING", price=Decimal("85000"),
        quantity=Decimal("3"), order_amount=None, currency="KRW",
        ordered_at="2026-07-01T00:00:00Z", canceled_at=None,
        execution={"filledQuantity": Decimal("0")},
    )


async def test_run_uses_one_shared_client_reused_by_batch_and_fallback(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod

    await _accepted(db_session, cid="c1", oid="o1")
    await _accepted(db_session, cid="c2", oid="o2")

    # A single fake client. list_orders raises so batch build fails and the run
    # MUST fall back per-row AND reuse this exact client (never new one per row).
    fake_client = SimpleNamespace(
        list_orders=AsyncMock(side_effect=RuntimeError("force-build-fail")),
        get_order=AsyncMock(side_effect=lambda oid: _pending_order(oid)),
        aclose=AsyncMock(),
    )
    from_settings = MagicMock(return_value=fake_client)

    with patch.object(mod.TossReadClient, "from_settings", from_settings):
        out = await mod.toss_reconcile_orders_impl(dry_run=False)

    assert from_settings.call_count == 1          # ONE client for the whole run
    assert fake_client.get_order.await_count == 2  # both rows via the shared client
    fake_client.aclose.assert_awaited_once()       # closed exactly once
    assert out["counts"] == {"pending": 2}
```

- [ ] **Run it — fails.** `uv run pytest tests/mcp_server/tooling/test_toss_reconcile_account_seq.py -v`
  Expected: `AttributeError: <module 'app.mcp_server.tooling.toss_live_ledger'> does not have the attribute 'TossReadClient'` (not imported / not constructed in the impl yet). Even after import, on the current impl `from_settings.call_count` would be 2 (one per fresh per-row adapter client) and `aclose` would not be the shared client — locking the N+1 regression.

- [ ] **Minimal impl part A — `_reconcile_one_toss_row` accepts `fallback_client`.** In `app/mcp_server/tooling/toss_live_ledger.py` change the signature (`:214-219`) and the fallback branch (`:228-231`):
```python
async def _reconcile_one_toss_row(
    row: TossLiveOrderLedger,
    *,
    dry_run: bool,
    evidence_source: Any | None = None,
    fallback_client: Any | None = None,
) -> dict[str, Any]:
    ...
    if evidence_source is not None:
        evidence = await evidence_source.evidence_for(row)
    else:
        evidence = await TossEvidenceAdapter(client=fallback_client).fetch_evidence(row)
```

- [ ] **Minimal impl part B — build one shared client, thread it, close it once.** Add the import near the other service imports (`:31`):
```python
from app.services.brokers.toss import TossReadClient
```
Rewrite the region `:584-612` so the shared client is built once, passed to `build`, threaded into the fallback, and closed once:
```python
    batch_build_error: dict[str, Any] | None = None

    # ROB-687 — one TossReadClient for the whole run so account-seq is resolved at
    # most once (per-instance cache; client.py:128-129,135) instead of once per
    # fresh per-row client. The ACCOUNT group is 1 TPS (rate_limiter.py:27), so a
    # per-row /accounts N+1 serializes into ~1s of sleep per open row. Defensive:
    # if Toss is disabled/misconfigured, degrade exactly as before (per-row/batch
    # construct their own; the per-row error handler classifies the failure).
    shared_client: TossReadClient | None = None
    if rows:
        try:
            shared_client = TossReadClient.from_settings()
        except Exception as exc:  # noqa: BLE001 — degrade to legacy path
            logger.warning(
                "toss reconcile: shared client unavailable (%s); legacy per-row path",
                exc,
            )
            shared_client = None

    evidence_source = None
    if rows:
        try:
            evidence_source = await TossBatchEvidenceSource.build(
                rows=rows, symbol=symbol, client=shared_client
            )
        except Exception as exc:  # noqa: BLE001 — batch is an optimization
            logger.exception(
                "toss reconcile batch evidence build failed; per-row fallback"
            )
            sentry_sdk.capture_exception(exc)
            batch_build_error = {"type": type(exc).__name__, "message": str(exc)}
            evidence_source = None

    try:
        for row in rows:
            try:
                outcome = await _reconcile_one_toss_row(
                    row,
                    dry_run=dry_run,
                    evidence_source=evidence_source,
                    fallback_client=shared_client,
                )
            except Exception as exc:  # noqa: BLE001 — classified in the handler
                outcome = await _handle_reconcile_row_error(row, exc, dry_run=dry_run)
            reconciled.append(outcome)
            verdict = str(outcome.get("verdict", "anomaly"))
            counts[verdict] = counts.get(verdict, 0) + 1
    finally:
        if evidence_source is not None:
            await evidence_source.aclose()  # no-op for a run-owned client
        if shared_client is not None:
            await shared_client.aclose()
```
  Notes: (1) `batch_build_error` initialization + echo were added in Task 1; keep the single initialization here and do not duplicate. (2) When `build` receives `client=shared_client` (not None), `TossBatchEvidenceSource.build` sets `owns_client=False` (`toss_live_evidence.py:214`), so `evidence_source.aclose()` no-ops and the run's `finally` owns the close. (3) When Toss is disabled, `shared_client` is `None`; `build(client=None)` internally news+owns its own client (legacy behavior), so nothing regresses.

- [ ] **Run it — passes.** `uv run pytest tests/mcp_server/tooling/test_toss_reconcile_account_seq.py -v` → 1 passed.

- [ ] **Write client-level regression locks — resolve-once + guard preserved.** Append to `tests/services/brokers/toss/test_client.py` (mirrors the existing `test_holdings_auto_resolves_single_account_header` MockTransport pattern; `_TokenManager` and `_json` helpers already exist there):
```python
@pytest.mark.asyncio
async def test_account_seq_resolved_once_then_cached_across_calls() -> None:
    accounts_hits = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal accounts_hits
        if request.url.path == "/api/v1/accounts":
            accounts_hits += 1
            return httpx.Response(
                200,
                json=_json([{"accountNo": "1", "accountSeq": 7, "accountType": "B"}]),
                request=request,
            )
        # `/api/v1/orders/{id}` (single-order detail) is parsed by `parse_order`,
        # which requires a FULL flat order row (orderId/symbol/side/orderType/
        # timeInForce/status/quantity/currency/orderedAt). Returning the list shape
        # `{"orders": []}` here would raise KeyError('orderId') inside parse_order
        # (parse_order delegates to parse_orders([raw])) — so branch on the path and
        # return a valid single-order body. The `/api/v1/orders` LIST path still
        # returns the `{"orders": []}` page shape parse_orders expects.
        if request.url.path.startswith("/api/v1/orders/"):
            return httpx.Response(
                200,
                json=_json(
                    {
                        "orderId": "ord-1",
                        "symbol": "034020",
                        "side": "BUY",
                        "orderType": "LIMIT",
                        "timeInForce": "DAY",
                        "status": "PENDING",
                        "quantity": "3",
                        "currency": "KRW",
                        "orderedAt": "2026-07-01T00:00:00Z",
                    }
                ),
                request=request,
            )
        return httpx.Response(200, json=_json({"orders": []}), request=request)

    # account_seq=None → resolution goes through /accounts; the instance caches it.
    client = TossReadClient(
        token_manager=_TokenManager(),
        account_seq=None,
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.list_orders(status="OPEN")
        await client.list_orders(status="CLOSED")
        await client.get_order("ord-1")
    finally:
        await client.aclose()

    assert accounts_hits == 1  # ROB-687: one /accounts for the whole client lifetime


@pytest.mark.asyncio
async def test_account_seq_guard_rejects_multiple_accounts() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/accounts":
            return httpx.Response(
                200,
                json=_json(
                    [
                        {"accountNo": "1", "accountSeq": 7, "accountType": "B"},
                        {"accountNo": "2", "accountSeq": 8, "accountType": "B"},
                    ]
                ),
                request=request,
            )
        return httpx.Response(200, json=_json({"orders": []}), request=request)

    client = TossReadClient(
        token_manager=_TokenManager(),
        account_seq=None,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ValueError, match="exactly one account"):
            await client.list_orders(status="OPEN")
    finally:
        await client.aclose()
```
  (These lock the guard/cache the runtime-resolution decision depends on. They pass on the current `client.py` — that is intentional: they guarantee no future refactor silently drops the guard or re-introduces per-call resolution.)

- [ ] **Run it — passes.** `uv run pytest tests/services/brokers/toss/test_client.py -k "account_seq" -v` → 2 passed.

- [ ] **Regression — full ledger + evidence + batch suites unchanged.** `uv run pytest tests/mcp_server/tooling/test_toss_live_ledger.py tests/mcp_server/tooling/test_toss_live_evidence.py tests/mcp_server/tooling/test_toss_batch_evidence_source.py tests/mcp_server/tooling/test_toss_reconcile_resilience.py -v`
  Expected: all pass. In particular `test_impl_uses_batch_source_and_echoes_window` (from_settings NOT patched → `shared_client` degrades to `None` via `TossApiDisabled`; `build` is mocked → `fake_source`; `fake_source.aclose` awaited once; `shared_client` is `None` so not closed) and `test_impl_batch_build_failure_falls_back_per_row` (`mod.TossEvidenceAdapter` patched to a `MagicMock`, invoked with `client=...`, returns `_Adapter()`) stay green.

- [ ] **Lint.** `make lint`

- [ ] **Commit.** `git add -A && git commit -m "perf(ROB-687): share one TossReadClient per reconcile run, kill /accounts N+1"`

---

## Task 4 — Diagnose + fix the real `TossBatchEvidenceSource.build` failure (Fix 3b, migration-0)

> **CLOSED — NO REPRO (Step B live diagnosis, 2026-07-04):** The premise that
> `TossBatchEvidenceSource.build` fails every run was an **incorrect inference from
> stale Sentry data**. A read-only live probe (GET-only: `/accounts`, `/orders`
> OPEN/CLOSED, `build()`) against the prod Toss env confirmed `build()` **succeeds**
> — both an empty-window build and a realistic 60-day window that paginated
> multiple CLOSED pages into 276 orders (no cap, no exception; OAuth OK, exactly 1
> account so the guard passes). Timeline evidence is decisive: all 7 Sentry
> `toss_reconcile_orders` runs (the source of the `187 /accounts`) occurred
> 2026-06-30 → 2026-07-03 00:20 KST — **before ROB-669 even existed** (its commit
> is 2026-07-03 20:08 KST; deployed to production ~2026-07-04 06:30 KST). So the
> N+1 was the **pre-ROB-669 per-row code**, which ROB-669's batch source already
> fixes. There is no `build` failure to diagnose. Task 4 is dropped.

**Re-scoped outcome:** Tasks 1–3 (this PR) are retained as **observability +
defense-in-depth**, not as the fix for an active bug:
- Task 1 (observability) is the lasting value — if `build` ever *does* fail in the
  future (transient/network/disabled), it is now surfaced (`logger.exception` +
  `sentry_sdk.capture_exception` + `batch_build_error` echo) instead of silently
  reverting to the per-row `/accounts` N+1.
- Tasks 2–3 (one shared client threaded into the per-row fallback) ensure that even
  in that degraded path the `/accounts` call happens at most once, not per row.

The active N+1 the ticket was opened for is already resolved by ROB-669 (deployed).
The next real post-deploy `toss_reconcile_orders` run should show ≤1 `/accounts`;
there have been no runs since deploy, so this is confirmed by the live `build()`
probe rather than by a fresh Sentry sample.

**Files:**
- Modify `app/mcp_server/tooling/toss_live_evidence.py` and/or `app/services/brokers/toss/client.py` / `dto.py` — the fix, scoped to whatever the diagnosis names. (Anticipated to be a small, read-path-only change; migration-0.)
- Test (modify) `tests/mcp_server/tooling/test_toss_batch_evidence_source.py` — a regression test reproducing the diagnosed failure with a fake client.
- Modify `docs/runbooks/toss-live-order-reconcile.md` — record the root cause + the shared-client/account-seq behavior + how to read `batch_build_error`.

**Diagnosis procedure (do this BEFORE writing any fix):**

- [ ] **Reproduce and capture the real exception.** Obtain the actual `TossBatchEvidenceSource.build` exception from ONE of (in order of preference):
  1. The Sentry issue newly produced by Task 1's `sentry_sdk.capture_exception` (the WARN→exception upgrade means the stack + type now land in Sentry) — read the top frame + exception type/message.
  2. A `batch_build_error` echo from a real `toss_reconcile_orders` call in a non-prod/staging run (`docs/runbooks/toss-live-order-reconcile.md`) or via `scripts/toss_live_smoke.py` (`toss_live_smoke` imports `toss_reconcile_orders_impl` and already has `_has_reconcile_anomaly` handling) with `TOSS_API_ENABLED=true`.
  Record the exact exception type, message, and the offending call (which `list_orders`/parse step) in the PR description and the runbook. **Do not proceed to the fix until the exception is named.**

- [ ] **Confirm which hypothesis holds (verify, do not assume).** Candidate causes to check against the captured exception — treat each as a hypothesis to confirm or reject, not a conclusion:
  - **H1 — `list_orders(status="OPEN")` request rejected.** Toss may reject `status=OPEN` (casing/param) or the `symbol=None` omission for the OPEN listing; a 4xx/`TossApiResponseError` here would abort `build` at `toss_live_evidence.py:229`.
  - **H2 — `parse_orders` field mismatch.** The list-endpoint row shape may differ from the single-order shape `parse_order` handles (e.g. execution nesting, missing `orderId`), so `parse_orders` raises even though per-row `get_order`/`parse_order` succeeds — which precisely matches the Sentry evidence (per-row `get_order` works, batch list does not).
  - **H3 — CLOSED window params.** `from`/`to` date format from `_kst_date_str` (`toss_live_evidence.py:224-225`) or the `limit=100` param may be rejected by `GET /orders?status=CLOSED` (422).
  - **H4 — account guard `!= 1`.** UNLIKELY (Sentry shows per-row `get_order` succeeding, so `/accounts` returns exactly one) — but confirm it is not intermittent multi-account.
  - Note: `trade_date` is a tz-aware `TIMESTAMP` (`app/models/review.py:529-531`), so `_kst_date_str`'s `.astimezone` is safe — H "naive-date" is already ruled out.

**Steps (after the cause is named — example shown for the most-likely H2; adapt to the confirmed cause):**

- [ ] **Write failing regression test reproducing the diagnosed failure.** Append to `tests/mcp_server/tooling/test_toss_batch_evidence_source.py` a test that drives `TossBatchEvidenceSource.build` with a fake client returning the real failing shape/error, asserting build currently raises (or drops the row) the way production does. Example skeleton (fill in with the confirmed cause):
```python
async def test_build_handles_real_open_list_shape():
    # Reproduce the diagnosed root cause (e.g. the OPEN list row shape that made
    # parse_orders raise / the OPEN status param Toss rejected). Assert build now
    # succeeds and maps the row instead of raising.
    ...
```

- [ ] **Run it — fails.** `uv run pytest tests/mcp_server/tooling/test_toss_batch_evidence_source.py -k real -v` → reproduces the production failure.

- [ ] **Minimal impl — fix the named root cause only.** Apply the smallest read-path change that makes `build` succeed for the real Toss response (e.g. correct the OPEN `status` value/param, make `parse_orders` tolerate the list row shape, or fix the CLOSED window param). Keep `classify_toss_order_evidence`, the CLOSED windowing/cap, and all booking unchanged. No broker mutation, migration-0.

- [ ] **Run it — passes.** `uv run pytest tests/mcp_server/tooling/test_toss_batch_evidence_source.py -v` → all pass.

- [ ] **Runbook — document the diagnosis + behavior.** In `docs/runbooks/toss-live-order-reconcile.md`, add a short section: the named root cause and its fix; that a healthy run now issues ONE `/accounts` (0 if `toss_api_account_seq` is set) and 2–4 `list_orders`; that a non-`None` `batch_build_error` in the tool output means the run silently fell back to per-row fetches (investigate the echoed type/message + the Sentry issue); and the explicit note that `toss_api_account_seq` is NOT hardcoded so the `_resolve_account_seq` "exactly one account" guard stays active — if an operator sets it, the value must match the single account.

- [ ] **Regression — batch source + full toss reconcile path.** `uv run pytest tests/mcp_server/tooling/test_toss_batch_evidence_source.py tests/mcp_server/tooling/test_toss_reconcile_account_seq.py tests/mcp_server/tooling/test_toss_reconcile_resilience.py tests/mcp_server/tooling/test_toss_live_ledger.py -v` → all pass.

- [ ] **Lint.** `make lint`

- [ ] **Commit.** `git add -A && git commit -m "fix(ROB-687): fix diagnosed TossBatchEvidenceSource.build failure + runbook"`

---

## Verification checklist (whole plan)

- [ ] `uv run pytest tests/mcp_server/tooling/test_toss_reconcile_account_seq.py tests/mcp_server/tooling/test_toss_reconcile_resilience.py tests/mcp_server/tooling/test_toss_live_evidence.py tests/mcp_server/tooling/test_toss_batch_evidence_source.py tests/mcp_server/tooling/test_toss_live_ledger.py tests/services/brokers/toss/test_client.py -v` → all green.
- [ ] `make lint` clean.
- [ ] Manual reasoning check: a healthy run issues **one** `GET /api/v1/accounts` (0 with `toss_api_account_seq` set) and **2–4** `list_orders`; a batch-build failure now emits a Sentry event + `batch_build_error` echo AND still resolves account-seq once (shared client), so even the degraded path is ≤1 `/accounts` — down from ~27.
- [ ] No migration, no broker mutation, no MCP tool signature change, no config hardcoding; `_resolve_account_seq` guard intact.
