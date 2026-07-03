# Toss KR Reconcile — Batch Rewrite + Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax.

**Goal:** Make Toss KR `toss_reconcile_orders` batch its broker evidence (absorbing ROB-632's deferred N+1 rewrite), stop turning transient "could-not-verify" failures into permanent `anomaly` rows, add a guarded recovery path to reopen the pre-ROB-631 anomaly backlog, and document the `save_trade_retrospective` enums.

**Architecture:** `toss_reconcile_orders_impl` loops unreconciled ledger rows and books fills only from confirmed broker evidence. Today it fetches evidence one-order-at-a-time with a fresh `TossReadClient` per row (N+1 → 90s timeout) and its per-row `except Exception` handler marks *any* failure as `status='anomaly'` (roach-motel: `list_open` never re-selects anomaly rows, no reset method). We introduce a `TossBatchEvidenceSource` that pre-fetches all open + windowed-closed orders into an `{order_id → TossOrder}` map in 2–4 list calls, reuse the unchanged `classify_toss_order_evidence` per row, split reconcile failures into *transient* (leave row open + retryable) vs *broker-confirmed anomaly* (mark_manual_review), and add a signature-guarded `reopen_anomalies_for_reconcile` service method that the reconcile pass calls **self-healingly** — folding recoverable no-fill anomaly rows back into the same work-list alongside `list_open` rows. Recovery stays entirely INTERNAL to `toss_live_ledger.py` + `toss_live_order_ledger_service.py`: the `toss_reconcile_orders` MCP tool signature is **unchanged**, so ROB-669 never collides with ROB-668's concurrent edits to `register_toss_live_order_tools` / `toss_preview_order` / `toss_place_order` / `suggest_order_account` in `orders_toss_variants.py`. Operators get recovery simply by running the existing reconcile tool (`dry_run=True` first to preview which rows would reopen).

**Tech Stack:** Python 3.13, uv, pytest (markers `unit`/`asyncio`), SQLAlchemy async, httpx, FastMCP tool registration, pydantic v2 (retrospective schema). Toss Open API `GET /api/v1/orders?status=OPEN|CLOSED`.

## Global Constraints

Copied verbatim; every task implicitly includes these.

- **Migration-0.** No new DB column, no new `status` enum value, no alembic revision. `review.toss_live_order_ledger.last_reconcile_error` (JSONB), `filled_qty`, `trade_id`, `requires_manual_review`, `manual_review_reason` already exist. The `status` CHECK constraint (`app/models/review.py:514-519`) is NOT modified — resilience reuses the existing `accepted/pending/partial` open statuses (retryable) and the existing `anomaly` status (broker-confirmed). If any reviewer insists on a distinct `reconcile_deferred` status, that is a separate follow-up ticket, not this plan.
- **Evidence-gated booking is inviolable.** Send-time rows stay accepted-only. Fills/journals/realized_pnl are booked ONLY from confirmed execution evidence. No broker mutation (place/modify/cancel) is ever issued from the reconcile path.
- **`classify_toss_order_evidence(order)` is reused UNCHANGED.** The batch source feeds it the same `TossOrder` DTO it already consumes from `get_order`; list rows carry the identical `execution.filledQuantity/averageFilledPrice/commission/tax` fields (`app/services/brokers/toss/dto.py:178` `_parse_execution`).
- **Backward-compatible signatures.** `_reconcile_one_toss_row(row, *, dry_run)` gains an optional `evidence_source=None`; when None it falls back to today's `TossEvidenceAdapter().fetch_evidence(row)` so all 15 existing `test_toss_live_ledger.py` tests stay green. The `toss_reconcile_orders_impl` public keyword signature (`symbol`/`order_id`/`market`/`dry_run`/`limit`) and the `toss_reconcile_orders` MCP tool signature are BOTH unchanged — anomaly recovery is self-healing inside the pass, not a new param — so the paused TaskIQ caller (`app/tasks/toss_live_reconcile_tasks.py:35`) and the ROB-668-owned `register_toss_live_order_tools` are unaffected.
- **Transient ≠ anomaly.** On `httpx` timeout/transport errors, Toss 429/500/502/503/504, or Toss transient codes (`rate-limit-exceeded`, `edge-rate-limit-exceeded`, `internal-error`, `maintenance`, `expired-token`, `invalid-token`), the row is left in its current open status (still selected by `list_open`) and only `last_reconcile_error` is recorded. `anomaly` is reserved for broker-confirmed contradictions: 404 order-not-found, 403/non-JSON access failure (existing documented behavior preserved), `TossLedgerIdempotencyConflict`, or an otherwise-unclassifiable code fault.
- **Window bound, never silent truncation.** The CLOSED query is bounded to `[oldest-open-ledger-row KST date, today KST]` and capped at `_TOSS_CLOSED_PAGE_CAP = 20` pages (×100). If the cap is hit, log it and echo `closed_pages_capped: true` + `single_fetch_fallbacks` in the response. Rows older than the window fall back to a single `get_order`, never dropped.
- **Guarded reopen only.** `reopen_anomalies_for_reconcile` reopens ONLY anomaly rows with no fill evidence (`filled_qty` NULL/0 AND `trade_id` NULL) whose `last_reconcile_error` matches the pre-ROB-631 `InstrumentType` signature OR a transient signature. 403/404/idempotency-conflict anomalies are never blindly reopened.
- Run tests: `uv run pytest <path> -v`. Lint: `make lint`. Do NOT commit unless the executing skill says so; each task lists its own commit message.

---

## File Structure

| File | Create/Modify | Responsibility |
|------|---------------|----------------|
| `app/mcp_server/tooling/trade_retrospective_registration.py` | Modify | Task 1 — document `root_cause_class` / `trigger_type` enums + next_actions rule in the `save_trade_retrospective` tool description. |
| `app/services/trade_journal/trade_retrospective_service.py` | Modify | Task 1 — enrich `RetrospectiveValidationError` messages to enumerate valid enum values. |
| `app/services/toss_live_order_ledger_service.py` | Modify | Task 2 — add `record_transient_reconcile_error`. Task 4 — add `reopen_anomalies_for_reconcile` + `_anomaly_error_is_reopenable`. |
| `app/mcp_server/tooling/toss_live_ledger.py` | Modify | Task 2 — `_is_transient_reconcile_error`, split row-error handler (`_handle_reconcile_row_error`). Task 3 — build `TossBatchEvidenceSource`, thread `evidence_source` through `_reconcile_one_toss_row`, window echo. Task 4 — self-healing reopen step (fold recoverable no-fill anomaly rows into the reconcile work-list); NO tool signature change. |
| `app/mcp_server/tooling/toss_live_evidence.py` | Modify | Task 3 — add `TossBatchEvidenceSource` (batched list → `{order_id: TossOrder}` map + single-fetch fallback). |
| `app/mcp_server/timeout_middleware.py` | Modify | Task 3 — update the `toss_reconcile_orders` budget comment (batched, no longer N+1). |
| `docs/runbooks/toss-live-order-reconcile.md` | Modify | Task 4 — replace raw-SQL remediation with the self-healing reconcile flow (`dry_run` preview → apply); document transient-vs-anomaly semantics + which anomalies are intentionally NOT auto-reopened. |
| `tests/mcp_server/tooling/test_trade_retrospective_registration_docs.py` | Create | Task 1 tests. |
| `tests/services/test_retrospective_validation_messages.py` | Create | Task 1 tests. |
| `tests/mcp_server/tooling/test_toss_reconcile_resilience.py` | Create | Task 2 tests. |
| `tests/mcp_server/tooling/test_toss_batch_evidence_source.py` | Create | Task 3 tests. |
| `tests/services/test_toss_reopen_anomalies.py` | Create | Task 4 tests. |

> **NOT touched: `app/mcp_server/tooling/orders_toss_variants.py`.** ROB-669 keeps its
> timeout/recovery fix internal to `toss_live_ledger.py` + `toss_live_order_ledger_service.py`.
> The `toss_reconcile_orders` MCP tool wrapper and its description inside
> `register_toss_live_order_tools` are left exactly as-is so ROB-669 does not collide with
> ROB-668 (which edits `toss_preview_order` / `toss_place_order` / `suggest_order_account` and
> the description block in that same function). Anomaly recovery is self-healing within the
> reconcile pass — no new MCP tool parameter, no wrapper edit, no description edit.

---

## Task 1 — Document `save_trade_retrospective` enums (Defect 3, doc-only, migration-0)

**Files:**
- Modify `app/mcp_server/tooling/trade_retrospective_registration.py:26` (description block for `save_trade_retrospective`).
- Modify `app/services/trade_journal/trade_retrospective_service.py:308` (trigger_type msg) and `:315` (root_cause_class msg).
- Test (create) `tests/mcp_server/tooling/test_trade_retrospective_registration_docs.py`.
- Test (create) `tests/services/test_retrospective_validation_messages.py`.

**Interfaces:**
- Consumes: `VALID_ROOT_CAUSE_CLASSES`, `VALID_TRIGGER_TYPES` (frozensets in `app/schemas/trade_retrospective.py:22,37`).
- Produces: no signature change. `register_trade_retrospective_tools(mcp)` still `(mcp: Any) -> None`; `save_retrospective(...)` unchanged signature, richer error strings.

Steps:

- [ ] **Write failing test — description documents both enums + the rule.** Create `tests/mcp_server/tooling/test_trade_retrospective_registration_docs.py`:
```python
from __future__ import annotations

import pytest

from app.mcp_server.tooling.trade_retrospective_registration import (
    register_trade_retrospective_tools,
)
from app.schemas.trade_retrospective import (
    VALID_ROOT_CAUSE_CLASSES,
    VALID_TRIGGER_TYPES,
)

pytestmark = pytest.mark.unit


class _FakeMCP:
    def __init__(self) -> None:
        self.descriptions: dict[str, str] = {}

    def tool(self, *, name: str, description: str):
        def _decorator(fn):
            self.descriptions[name] = description
            return fn

        return _decorator


def _register() -> str:
    mcp = _FakeMCP()
    register_trade_retrospective_tools(mcp)
    return mcp.descriptions["save_trade_retrospective"]


def test_description_enumerates_root_cause_class_values():
    desc = _register()
    for value in VALID_ROOT_CAUSE_CLASSES:
        assert value in desc, f"root_cause_class value {value!r} missing from description"


def test_description_enumerates_trigger_type_values():
    desc = _register()
    for value in VALID_TRIGGER_TYPES:
        assert value in desc, f"trigger_type value {value!r} missing from description"


def test_description_states_next_actions_required_with_trigger_type():
    desc = _register().lower()
    assert "next_actions" in desc
    assert "trigger_type" in desc
    assert "required" in desc
```

- [ ] **Run it — fails.** `uv run pytest tests/mcp_server/tooling/test_trade_retrospective_registration_docs.py -v`
  Expected: `test_description_enumerates_root_cause_class_values` and `..._trigger_type_values` FAIL (current description at `:26` names none of the enum members; `process_error` is not even valid), `..._next_actions_required...` FAIL (no such sentence).

- [ ] **Minimal impl — extend the description.** In `app/mcp_server/tooling/trade_retrospective_registration.py`, replace the `save_trade_retrospective` description string (currently ending `"...fx_rate_source, fx_pnl_accuracy)."`) by appending two sentences before the closing paren of the tuple:
```python
    _ = mcp.tool(
        name="save_trade_retrospective",
        description=(
            "Store a structured trade retrospective (outcome, absolute realized_pnl, "
            "fill/plan price, pnl_pct, rationale/result/lesson/next_strategy) for a "
            "trade. account_mode in {kis_mock, kiwoom_mock, kis_live, toss_live, "
            "alpaca_paper, upbit_live}. Idempotent per correlation_id (omit it to "
            "append). "
            "kiwoom_mock cannot supply realized_pnl/fill_price (no fill evidence, "
            "ROB-460). realized_pnl is caller-supplied, or derived from journal_id "
            "when entry/exit/qty are present. ROB-568: accepts US FX PnL fields "
            "(buy_fx_rate, sell_fx_rate, security_pnl_usd, security_pnl_krw, "
            "fx_pnl_krw, total_pnl_krw, fx_rate_source, fx_pnl_accuracy). "
            "Postmortem taxonomy (ROB-647): root_cause_class in {user_input, "
            "analysis, policy, execution, harness} (NOT process_error/etc.); "
            "trigger_type in {fill, partial_fill, rejected_order, cancelled, "
            "expired, thesis_change, policy_violation, stale_evidence, "
            "guardrail_block}. When trigger_type is set, a non-empty next_actions "
            "list is required in the same call (each next_action needs a non-empty "
            "action; optional owner/issue_id/status/due_kst_date, status in "
            "{open, in_progress, done})."
        ),
    )(save_trade_retrospective)
```

- [ ] **Run it — passes.** `uv run pytest tests/mcp_server/tooling/test_trade_retrospective_registration_docs.py -v` → 3 passed.

- [ ] **Write failing test — enriched validation messages.** Create `tests/services/test_retrospective_validation_messages.py`:
```python
from __future__ import annotations

import pytest

from app.services.trade_journal.trade_retrospective_service import (
    RetrospectiveValidationError,
    save_retrospective,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_invalid_root_cause_class_message_lists_valid_values(db_session):
    with pytest.raises(RetrospectiveValidationError) as exc:
        await save_retrospective(
            db_session,
            symbol="AAPL",
            instrument_type="equity_us",
            account_mode="toss_live",
            outcome="filled",
            root_cause_class="process_error",
        )
    msg = str(exc.value)
    assert "process_error" in msg
    assert "execution" in msg and "harness" in msg  # enumerates the valid set


async def test_invalid_trigger_type_message_lists_valid_values(db_session):
    with pytest.raises(RetrospectiveValidationError) as exc:
        await save_retrospective(
            db_session,
            symbol="AAPL",
            instrument_type="equity_us",
            account_mode="toss_live",
            outcome="filled",
            trigger_type="bogus",
        )
    msg = str(exc.value)
    assert "bogus" in msg
    assert "fill" in msg and "guardrail_block" in msg
```

- [ ] **Run it — fails.** `uv run pytest tests/services/test_retrospective_validation_messages.py -v`
  Expected: both FAIL — current messages are `f"invalid root_cause_class: {root_cause_class}"` / `f"invalid trigger_type: {trigger_type}"` (no valid-value enumeration).

- [ ] **Minimal impl — enumerate valid values in the two error messages.** In `app/services/trade_journal/trade_retrospective_service.py`, edit the two raises:
```python
    trigger_set = trigger_type is not _UNSET and trigger_type is not None
    if trigger_set and trigger_type not in VALID_TRIGGER_TYPES:
        raise RetrospectiveValidationError(
            f"invalid trigger_type: {trigger_type} "
            f"(allowed: {sorted(VALID_TRIGGER_TYPES)})"
        )
    if (
        root_cause_class is not _UNSET
        and root_cause_class is not None
        and root_cause_class not in VALID_ROOT_CAUSE_CLASSES
    ):
        raise RetrospectiveValidationError(
            f"invalid root_cause_class: {root_cause_class} "
            f"(allowed: {sorted(VALID_ROOT_CAUSE_CLASSES)})"
        )
```

- [ ] **Run it — passes.** `uv run pytest tests/services/test_retrospective_validation_messages.py -v` → 2 passed.

- [ ] **Regression guard.** `uv run pytest tests/mcp_server/tooling/ -k retrospective -q` → no failures (no existing test asserted the old exact strings — verified via `grep -rn "invalid root_cause_class" tests/` returning nothing).

- [ ] **Commit.** `git add -A && git commit -m "docs(ROB-669): document save_trade_retrospective root_cause_class/trigger_type enums (Defect 3)"`

---

## Task 2 — Resilience: transient failures stay retryable, never anomaly (R1, migration-0)

**Files:**
- Modify `app/services/toss_live_order_ledger_service.py` — add `record_transient_reconcile_error` after `mark_manual_review` (`:302`).
- Modify `app/mcp_server/tooling/toss_live_ledger.py` — add `_is_transient_reconcile_error` + `_handle_reconcile_row_error`, rewire the loop in `toss_reconcile_orders_impl` (`:462`–`:494`).
- Test (create) `tests/mcp_server/tooling/test_toss_reconcile_resilience.py`.

**Interfaces:**
- Produces `TossLiveOrderLedgerService.record_transient_reconcile_error(*, ledger_id: int, error: dict[str, Any]) -> None` — sets `last_reconcile_error` only; does NOT touch `status`, `requires_manual_review`, `manual_review_reason`, or `reconciled_at`.
- Produces `_is_transient_reconcile_error(exc: Exception) -> bool`.
- Produces `_handle_reconcile_row_error(row, exc, *, dry_run) -> dict[str, Any]` — returns the per-row outcome dict (verdict `deferred` or `anomaly`).
- Consumes `TossApiResponseError` (`app/services/brokers/toss/errors.py:38`, carries `.status_code`, `.envelope.code`), `TossRateLimitError` (`:48`), `httpx.TimeoutException`/`httpx.TransportError`.

Steps:

- [ ] **Write failing test — transient error leaves row open + records error, no anomaly.** Create `tests/mcp_server/tooling/test_toss_reconcile_resilience.py`:
```python
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.review import TossLiveOrderLedger
from app.services.brokers.toss.errors import (
    TossApiResponseError,
    TossErrorEnvelope,
    TossRateLimitError,
)
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


async def _accepted(db_session):
    return await TossLiveOrderLedgerService(db_session).record_send(
        operation_kind="place",
        market="kr",
        symbol="034020",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("3"),
        price=Decimal("85000"),
        order_amount=None,
        currency="KRW",
        client_order_id="cid-kr-buy",
        broker_order_id="ord-kr-buy",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
        thesis="t",
        strategy="s",
    )


def _toss_err(code: str, status: int):
    return TossApiResponseError(
        TossErrorEnvelope(request_id="ray", code=code, message="boom", data=None),
        status_code=status,
    )


@pytest.mark.parametrize(
    "exc",
    [
        TossRateLimitError(
            TossErrorEnvelope(request_id="r", code="rate-limit-exceeded", message="x", data=None),
            status_code=429,
        ),
        _toss_err("internal-error", 500),
        _toss_err("maintenance", 503),
        _toss_err("expired-token", 401),
        httpx.ReadTimeout("timeout"),
        httpx.ConnectError("refused"),
    ],
)
async def test_transient_error_leaves_row_retryable_not_anomaly(db_session, exc):
    from app.mcp_server.tooling import toss_live_ledger as mod

    row = await _accepted(db_session)
    with patch.object(mod, "_reconcile_one_toss_row", new=AsyncMock(side_effect=exc)):
        out = await mod.toss_reconcile_orders_impl(dry_run=False)

    assert out["counts"] == {"deferred": 1}
    entry = out["reconciled"][0]
    assert entry["verdict"] == "deferred"
    assert entry["retryable"] is True

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "accepted"  # still selected by list_open next pass
    assert refreshed.requires_manual_review is False
    assert refreshed.manual_review_reason is None
    assert refreshed.last_reconcile_error is not None  # error recorded for observability


async def test_404_order_not_found_still_marks_anomaly(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod

    row = await _accepted(db_session)
    exc = _toss_err("order-not-found", 404)
    with patch.object(mod, "_reconcile_one_toss_row", new=AsyncMock(side_effect=exc)):
        out = await mod.toss_reconcile_orders_impl(dry_run=False)

    assert out["counts"] == {"anomaly": 1}
    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "anomaly"
    assert refreshed.requires_manual_review is True
```

- [ ] **Run it — fails.** `uv run pytest tests/mcp_server/tooling/test_toss_reconcile_resilience.py -v`
  Expected: the 6 parametrized transient cases FAIL — today's handler marks every exception `anomaly` (`counts == {"anomaly": 1}`, row `status='anomaly'`). The 404 case passes coincidentally (still anomaly) but keep it to lock the boundary.

- [ ] **Minimal impl part A — service method.** In `app/services/toss_live_order_ledger_service.py`, append to `TossLiveOrderLedgerService` (after `mark_manual_review`):
```python
    async def record_transient_reconcile_error(
        self,
        *,
        ledger_id: int,
        error: dict[str, Any],
    ) -> None:
        """ROB-669 — a transient reconcile failure (rate-limit/5xx/token/network).

        Record the error for observability WITHOUT closing the row: status,
        requires_manual_review, manual_review_reason, and reconciled_at are left
        untouched so ``list_open`` re-selects the row and the next pass retries.
        This is the opposite of ``mark_manual_review`` (broker-confirmed anomaly).
        """
        row = await self._db.get(TossLiveOrderLedger, ledger_id)
        if row is None:
            return
        row.last_reconcile_error = error
        await self._db.commit()
```

- [ ] **Minimal impl part B — classify + split handler.** In `app/mcp_server/tooling/toss_live_ledger.py`, add `import httpx` at the top with the other imports, import `TossRateLimitError`:
```python
from app.services.brokers.toss.errors import TossApiResponseError, TossRateLimitError
```
Add the classifier + handler above `toss_reconcile_orders_impl`:
```python
# ROB-669 — transient reconcile failures (could-not-verify-right-now) must NOT
# become permanent anomalies. Reserve anomaly for broker-confirmed contradiction.
_TRANSIENT_TOSS_CODES = frozenset(
    {
        "rate-limit-exceeded",
        "edge-rate-limit-exceeded",
        "internal-error",
        "maintenance",
        "expired-token",
        "invalid-token",
    }
)
_TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


def _is_transient_reconcile_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    if isinstance(exc, TossRateLimitError):
        return True
    if isinstance(exc, TossApiResponseError):
        if exc.status_code in _TRANSIENT_HTTP_STATUSES:
            return True
        return exc.envelope.code in _TRANSIENT_TOSS_CODES
    # 404 order-not-found, 403/non-JSON, idempotency conflict, and any
    # unclassifiable code fault fall through to anomaly (surface it, recoverable
    # via reopen_anomalies once fixed) — never silently retried forever.
    return False


def _transient_outcome(
    row: TossLiveOrderLedger, exc: Exception, error_details: dict[str, Any]
) -> dict[str, Any]:
    return {
        "ledger_id": row.id,
        "order_id": row.broker_order_id,
        "client_order_id": row.client_order_id,
        "market": row.market,
        "symbol": row.symbol,
        "operation_kind": row.operation_kind,
        "verdict": "deferred",
        "action": "deferred_transient_retryable",
        "retryable": True,
        "error": str(exc) or exc.__class__.__name__,
        "error_details": error_details,
    }


async def _handle_reconcile_row_error(
    row: TossLiveOrderLedger, exc: Exception, *, dry_run: bool
) -> dict[str, Any]:
    error_details = _reconcile_error_payload(exc)
    if _is_transient_reconcile_error(exc):
        logger.warning(
            "toss reconcile transient (left retryable) order_id=%s: %s",
            row.broker_order_id,
            exc,
        )
        if not dry_run:
            async with _order_session_factory()() as db:
                await TossLiveOrderLedgerService(db).record_transient_reconcile_error(
                    ledger_id=row.id, error=error_details
                )
        return _transient_outcome(row, exc, error_details)

    logger.warning(
        "toss reconcile anomaly (broker-confirmed) order_id=%s: %s",
        row.broker_order_id,
        exc,
    )
    reason = _manual_review_reason(row, exc)
    if not dry_run:
        async with _order_session_factory()() as db:
            await TossLiveOrderLedgerService(db).mark_manual_review(
                ledger_id=row.id, reason=reason, error=error_details
            )
    return {
        "ledger_id": row.id,
        "order_id": row.broker_order_id,
        "client_order_id": row.client_order_id,
        "market": row.market,
        "symbol": row.symbol,
        "operation_kind": row.operation_kind,
        "verdict": "anomaly",
        "action": "requires_manual_review",
        "requires_manual_review": True,
        "manual_review_reason": reason,
        "error": str(exc) or exc.__class__.__name__,
        "error_details": error_details,
    }
```
Then replace the loop body inside `toss_reconcile_orders_impl` (`:462`–`:494`) so the `except` delegates to the new handler:
```python
    reconciled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for row in rows:
        try:
            outcome = await _reconcile_one_toss_row(row, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 — classified in the handler
            outcome = await _handle_reconcile_row_error(row, exc, dry_run=dry_run)
        reconciled.append(outcome)
        verdict = str(outcome.get("verdict", "anomaly"))
        counts[verdict] = counts.get(verdict, 0) + 1
```

- [ ] **Run it — passes.** `uv run pytest tests/mcp_server/tooling/test_toss_reconcile_resilience.py -v` → 7 passed.

- [ ] **Regression — existing 403 anomaly tests still pass.** `uv run pytest tests/mcp_server/tooling/test_toss_live_ledger.py -v`
  Expected: all pass, including `test_reconcile_impl_reports_manual_review_on_error_without_mutating_dry_run` and `test_reconcile_impl_marks_manual_review_on_error_when_not_dry_run` (403/non-json code `non-json-response` is NOT in `_TRANSIENT_TOSS_CODES` and status 403 is NOT in `_TRANSIENT_HTTP_STATUSES` → still classified anomaly).

- [ ] **Commit.** `git add -A && git commit -m "fix(ROB-669): transient toss reconcile failures stay retryable, not anomaly (R1)"`

---

## Task 3 — Batched broker evidence (T1, absorbs ROB-632, migration-0)

**Files:**
- Modify `app/mcp_server/tooling/toss_live_evidence.py` — add `TossBatchEvidenceSource` (and KST date helper).
- Modify `app/mcp_server/tooling/toss_live_ledger.py` — `_reconcile_one_toss_row(row, *, dry_run, evidence_source=None)`; build the source once in `toss_reconcile_orders_impl`; window echo; batch-build fail-open to per-row fallback.
- Modify `app/mcp_server/timeout_middleware.py:69` — update the `toss_reconcile_orders` budget comment.
- Test (create) `tests/mcp_server/tooling/test_toss_batch_evidence_source.py`.

**Interfaces:**
- Produces `TossBatchEvidenceSource.build(*, rows: list, symbol: str | None = None, client: TossReadClient | None = None) -> TossBatchEvidenceSource` (classmethod; builds `{order_id: TossOrder}` from `list_orders(status="OPEN")` + windowed `list_orders(status="CLOSED", ...)`).
- Produces `TossBatchEvidenceSource.evidence_for(row) -> TossFillEvidence` (map hit → `classify_toss_order_evidence`; miss → single `get_order` fallback, counted).
- Produces `.aclose()`, `.single_fetch_count: int`, `.closed_pages_capped: bool`, `.window_from: str`, `.window_to: str`.
- Consumes `TossReadClient.list_orders(*, status, symbol, from_date, to_date, cursor, limit)` → `TossOrdersPage` (`app/services/brokers/toss/client.py:257`, `dto.py:102` `TossOrdersPage`), `classify_toss_order_evidence` (`toss_live_evidence.py:74`).

Steps:

- [ ] **Write failing test — batch builds map from list calls, no per-row get_order.** Create `tests/mcp_server/tooling/test_toss_batch_evidence_source.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling.toss_live_evidence import TossBatchEvidenceSource
from app.services.brokers.toss.dto import TossOrder, TossOrdersPage

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _order(order_id: str, status: str, filled: str = "0", avg: str | None = None):
    execution = {"filledQuantity": Decimal(filled)}
    if avg is not None:
        execution["averageFilledPrice"] = Decimal(avg)
    return TossOrder(
        order_id=order_id,
        symbol="034020",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        status=status,
        price=Decimal("85000"),
        quantity=Decimal("3"),
        order_amount=None,
        currency="KRW",
        ordered_at="2026-07-01T00:00:00Z",
        canceled_at=None,
        execution=execution,
    )


def _row(order_id: str, days_ago: int = 1):
    return SimpleNamespace(
        broker_order_id=order_id,
        trade_date=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
    )


class _FakeClient:
    def __init__(self, *, open_orders, closed_pages):
        self._open = open_orders
        self._closed_pages = list(closed_pages)
        self.list_calls: list[dict] = []
        self.get_order = AsyncMock(
            return_value=_order("older", "FILLED", "3", "85000")
        )
        self.aclose = AsyncMock()

    async def list_orders(self, *, status, symbol=None, from_date=None,
                          to_date=None, cursor=None, limit=None):
        self.list_calls.append(
            {"status": status, "from": from_date, "to": to_date, "cursor": cursor}
        )
        if status == "OPEN":
            return TossOrdersPage(orders=self._open, next_cursor=None, has_next=False)
        page = self._closed_pages.pop(0)
        return page


async def test_build_maps_open_and_closed_without_per_row_get_order():
    client = _FakeClient(
        open_orders=[_order("open-1", "PENDING")],
        closed_pages=[
            TossOrdersPage(
                orders=[_order("closed-1", "FILLED", "3", "85000")],
                next_cursor=None,
                has_next=False,
            )
        ],
    )
    rows = [_row("open-1"), _row("closed-1")]
    source = await TossBatchEvidenceSource.build(rows=rows, client=client)

    ev_open = await source.evidence_for(_row("open-1"))
    ev_closed = await source.evidence_for(_row("closed-1"))

    assert ev_open.verdict == "pending"
    assert ev_closed.verdict == "filled"
    client.get_order.assert_not_awaited()  # everything came from the batch map
    assert source.single_fetch_count == 0
    # exactly: 1 OPEN call + 1 CLOSED page
    assert sum(1 for c in client.list_calls if c["status"] == "OPEN") == 1
    assert sum(1 for c in client.list_calls if c["status"] == "CLOSED") == 1


async def test_row_outside_window_falls_back_to_single_get_order():
    client = _FakeClient(
        open_orders=[],
        closed_pages=[
            TossOrdersPage(orders=[], next_cursor=None, has_next=False)
        ],
    )
    source = await TossBatchEvidenceSource.build(rows=[_row("open-1")], client=client)
    ev = await source.evidence_for(_row("older"))

    assert ev.verdict == "filled"
    client.get_order.assert_awaited_once_with("older")
    assert source.single_fetch_count == 1


async def test_closed_pagination_is_capped_and_flagged(monkeypatch):
    import app.mcp_server.tooling.toss_live_evidence as ev_mod

    monkeypatch.setattr(ev_mod, "_TOSS_CLOSED_PAGE_CAP", 2)
    # 3 pages available, each says has_next -> cap stops at 2
    pages = [
        TossOrdersPage(orders=[_order(f"c{i}", "FILLED", "3", "85000")],
                       next_cursor=f"cur{i}", has_next=True)
        for i in range(3)
    ]
    client = _FakeClient(open_orders=[], closed_pages=pages)
    source = await TossBatchEvidenceSource.build(rows=[_row("c0")], client=client)

    assert source.closed_pages_capped is True
    assert sum(1 for c in client.list_calls if c["status"] == "CLOSED") == 2
```

- [ ] **Run it — fails.** `uv run pytest tests/mcp_server/tooling/test_toss_batch_evidence_source.py -v`
  Expected: ImportError / AttributeError — `TossBatchEvidenceSource` and `_TOSS_CLOSED_PAGE_CAP` do not exist yet.

- [ ] **Minimal impl — add `TossBatchEvidenceSource`.** In `app/mcp_server/tooling/toss_live_evidence.py`, extend the imports and append the class:
```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.services.brokers.toss import TossReadClient

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
# Bound CLOSED pagination so a huge order history cannot blow the time budget.
_TOSS_CLOSED_PAGE_CAP = 20
```
(keep the existing `TossFillEvidence`, `_to_decimal`, `classify_toss_order_evidence`, and `TossEvidenceAdapter` intact) and add:
```python
def _kst_date_str(dt: datetime | None) -> str:
    ref = dt or datetime.now(_KST)
    return ref.astimezone(_KST).date().isoformat()


class TossBatchEvidenceSource:
    """ROB-669 (absorbs ROB-632) — batched broker evidence for reconcile.

    Replaces the per-row ``get_order`` N+1 (fresh client + OAuth + account-seq
    resolve + rate-limit wait per open ledger row) with a bounded set of list
    calls: one ``GET /orders?status=OPEN`` (all open orders in a single call —
    Toss ignores cursor/limit for OPEN) plus windowed ``GET /orders?status=CLOSED``
    cursor pagination from the oldest open ledger row's KST date to today. List
    rows carry the same execution fields the single-order classifier consumes, so
    ``classify_toss_order_evidence`` is reused unchanged. Rows older than the
    window fall back to a single ``get_order`` (never dropped).
    """

    def __init__(
        self,
        client: TossReadClient,
        *,
        order_map: dict[str, Any],
        window_from: str,
        window_to: str,
        closed_pages_capped: bool,
        owns_client: bool,
    ) -> None:
        self._client = client
        self._order_map = order_map
        self.window_from = window_from
        self.window_to = window_to
        self.closed_pages_capped = closed_pages_capped
        self._owns_client = owns_client
        self.single_fetch_count = 0

    @classmethod
    async def build(
        cls,
        *,
        rows: list[Any],
        symbol: str | None = None,
        client: TossReadClient | None = None,
    ) -> TossBatchEvidenceSource:
        owns_client = client is None
        client = client or TossReadClient.from_settings()
        oldest = min(
            (getattr(r, "trade_date", None) for r in rows if getattr(r, "trade_date", None)),
            default=None,
        )
        window_from = _kst_date_str(oldest)
        window_to = _kst_date_str(None)

        order_map: dict[str, Any] = {}
        # 1) OPEN — one call returns all open orders.
        open_page = await client.list_orders(status="OPEN", symbol=symbol)
        for order in open_page.orders:
            order_map[str(order.order_id)] = order

        # 2) CLOSED — windowed cursor pagination, capped.
        cursor: str | None = None
        pages = 0
        capped = False
        while True:
            page = await client.list_orders(
                status="CLOSED",
                symbol=symbol,
                from_date=window_from,
                to_date=window_to,
                cursor=cursor,
                limit=100,
            )
            for order in page.orders:
                order_map[str(order.order_id)] = order  # CLOSED wins over OPEN
            pages += 1
            if not page.has_next or not page.next_cursor:
                break
            if pages >= _TOSS_CLOSED_PAGE_CAP:
                capped = True
                logger.warning(
                    "toss reconcile CLOSED pagination capped at %d pages "
                    "(window %s..%s); older rows use single-order fallback",
                    _TOSS_CLOSED_PAGE_CAP,
                    window_from,
                    window_to,
                )
                break
            cursor = page.next_cursor

        return cls(
            client,
            order_map=order_map,
            window_from=window_from,
            window_to=window_to,
            closed_pages_capped=capped,
            owns_client=owns_client,
        )

    async def evidence_for(self, row: Any) -> TossFillEvidence:
        order = self._order_map.get(str(row.broker_order_id))
        if order is not None:
            return classify_toss_order_evidence(order)
        # Older than the window (or a pre-window replacement original): single fetch.
        self.single_fetch_count += 1
        order = await self._client.get_order(str(row.broker_order_id))
        return classify_toss_order_evidence(order)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
```

- [ ] **Run it — passes.** `uv run pytest tests/mcp_server/tooling/test_toss_batch_evidence_source.py -v` → 3 passed.

- [ ] **Write failing test — impl threads the batch source and echoes window.** Append to `tests/mcp_server/tooling/test_toss_reconcile_resilience.py` (session-factory + `_accepted` helper already defined there):
```python
async def test_impl_uses_batch_source_and_echoes_window(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)

    fake_source = AsyncMock()
    fake_source.evidence_for = AsyncMock(
        return_value=TossFillEvidence(
            verdict="pending",
            local_status="pending",
            broker_status="PENDING",
            filled_qty=Decimal("0"),
            avg_price=None,
            commission=None,
            tax=None,
            fee_total=Decimal("0"),
            settlement_date=None,
            raw_order={"status": "PENDING"},
            reason="pending",
        )
    )
    fake_source.aclose = AsyncMock()
    fake_source.single_fetch_count = 0
    fake_source.closed_pages_capped = False
    fake_source.window_from = "2026-07-01"
    fake_source.window_to = "2026-07-03"

    with patch.object(
        mod.TossBatchEvidenceSource, "build", new=AsyncMock(return_value=fake_source)
    ):
        out = await mod.toss_reconcile_orders_impl(dry_run=False)

    assert out["success"] is True
    assert out["counts"] == {"pending": 1}
    # evidence came from the batch source, not a per-row adapter
    fake_source.evidence_for.assert_awaited_once()
    fake_source.aclose.assert_awaited_once()
    assert out["window"]["from"] == "2026-07-01"
    assert out["window"]["closed_pages_capped"] is False


async def test_impl_batch_build_failure_falls_back_per_row(db_session):
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
            new=AsyncMock(side_effect=RuntimeError("toss disabled in test")),
        ),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
    ):
        out = await mod.toss_reconcile_orders_impl(dry_run=False)

    assert out["counts"] == {"pending": 1}  # per-row fallback still reconciles
```

- [ ] **Run it — fails.** `uv run pytest tests/mcp_server/tooling/test_toss_reconcile_resilience.py -k "batch" -v`
  Expected: FAIL — `toss_reconcile_orders_impl` does not build a batch source, does not pass `evidence_source`, and has no `window` key; `_reconcile_one_toss_row` has no `evidence_source` param.

- [ ] **Minimal impl part A — `_reconcile_one_toss_row` accepts `evidence_source`.** In `app/mcp_server/tooling/toss_live_ledger.py`, change the signature and the evidence line:
```python
async def _reconcile_one_toss_row(
    row: TossLiveOrderLedger,
    *,
    dry_run: bool,
    evidence_source: Any | None = None,
) -> dict[str, Any]:
    ...
    if evidence_source is not None:
        evidence = await evidence_source.evidence_for(row)
    else:
        evidence = await TossEvidenceAdapter().fetch_evidence(row)
    base["verdict"] = evidence.verdict
    ...
```
Add `TossBatchEvidenceSource` to the existing evidence import:
```python
from app.mcp_server.tooling.toss_live_evidence import (
    TossBatchEvidenceSource,
    TossEvidenceAdapter,
)
```

- [ ] **Minimal impl part B — build once, thread through, echo window, fail-open.** Rewrite the body of `toss_reconcile_orders_impl` after `list_open`:
```python
    reconciled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    evidence_source = None
    if rows:
        try:
            evidence_source = await TossBatchEvidenceSource.build(
                rows=rows, symbol=symbol
            )
        except Exception as exc:  # noqa: BLE001 — batch is an optimization
            # Any batch-build failure (disabled/network/transient) degrades to the
            # per-row single-fetch path; per-row R1 classification still applies.
            logger.warning(
                "toss reconcile batch evidence build failed; per-row fallback: %s",
                exc,
            )
            evidence_source = None

    try:
        for row in rows:
            try:
                outcome = await _reconcile_one_toss_row(
                    row, dry_run=dry_run, evidence_source=evidence_source
                )
            except Exception as exc:  # noqa: BLE001 — classified in the handler
                outcome = await _handle_reconcile_row_error(
                    row, exc, dry_run=dry_run
                )
            reconciled.append(outcome)
            verdict = str(outcome.get("verdict", "anomaly"))
            counts[verdict] = counts.get(verdict, 0) + 1
    finally:
        if evidence_source is not None:
            await evidence_source.aclose()

    result: dict[str, Any] = {
        "success": True,
        "dry_run": dry_run,
        "counts": counts,
        "reconciled": reconciled,
        "message": (
            f"Reconciled {len(reconciled)} Toss live order(s) "
            f"(dry_run={dry_run}): {counts}"
        ),
    }
    if evidence_source is not None:
        result["window"] = {
            "from": evidence_source.window_from,
            "to": evidence_source.window_to,
            "closed_pages_capped": evidence_source.closed_pages_capped,
            "single_fetch_fallbacks": evidence_source.single_fetch_count,
        }
    return result
```

- [ ] **Run it — passes.** `uv run pytest tests/mcp_server/tooling/test_toss_reconcile_resilience.py -v` → all pass (transient + batch tests).

- [ ] **Minimal impl part C — timeout comment.** In `app/mcp_server/timeout_middleware.py`, update the comment block above the reconcile budgets (`:64`–`:66`) and keep the 90.0 budget:
```python
    # Order reconcile fan-out over daily order history (KIS/live). Toss reconcile
    # is now batched (GET /orders?status=OPEN + windowed CLOSED pagination, ROB-669
    # absorbing ROB-632) so a KR pass is 2-4 list calls, well under budget; the 90s
    # budget stays as headroom for large windows + single-fetch fallbacks.
    "kis_live_reconcile_orders": 90.0,
    "live_reconcile_orders": 90.0,
    "toss_reconcile_orders": 90.0,
```

- [ ] **Regression — full ledger + evidence suites.** `uv run pytest tests/mcp_server/tooling/test_toss_live_ledger.py tests/mcp_server/tooling/test_toss_live_evidence.py -v` → all pass (existing `_reconcile_one_toss_row(row, dry_run=...)` callers use the `evidence_source=None` fallback; `test_adapter_fetches_single_order_detail` unchanged).

- [ ] **Commit.** `git add -A && git commit -m "perf(ROB-669): batch toss reconcile broker evidence, absorb ROB-632 N+1 (T1)"`

---

## Task 4 — Guarded anomaly recovery + runbook (R2, migration-0)

**Files:**
- Modify `app/services/toss_live_order_ledger_service.py` — add `_anomaly_error_is_reopenable` (module fn) + `reopen_anomalies_for_reconcile`.
- Modify `app/mcp_server/tooling/toss_live_ledger.py` — inside `toss_reconcile_orders_impl` (signature UNCHANGED), call `reopen_anomalies_for_reconcile` as a self-healing step and MERGE the returned recoverable rows into the work-list alongside `list_open` rows (deduped by ledger id); echo `reopened`.
- **Does NOT touch `app/mcp_server/tooling/orders_toss_variants.py`** — the `toss_reconcile_orders` MCP tool signature/description stay exactly as-is (avoids the ROB-668 collision in `register_toss_live_order_tools`). No new tool param.
- Modify `docs/runbooks/toss-live-order-reconcile.md` — replace raw-SQL remediation with the self-healing reconcile flow; document transient-vs-anomaly + which anomalies are intentionally NOT auto-reopened.
- Test (create) `tests/services/test_toss_reopen_anomalies.py`.

**Interfaces:**
- Produces `TossLiveOrderLedgerService.reopen_anomalies_for_reconcile(*, dry_run: bool = True, market: str | None = None, symbol: str | None = None, limit: int = 200) -> dict[str, Any]` → `{"dry_run", "reopened": int, "rows": [TossLiveOrderLedger, ...], "candidates": [ {ledger_id, symbol, market, broker_order_id, error_type, error_message} ]}`. `rows` are the recoverable ORM rows the reconcile pass folds into its work-list (mutated to `accepted` when not dry_run; left `anomaly` in dry_run for read-only preview). The impl pops `rows` out before echoing `reopened` in the response.
- Produces module fn `_anomaly_error_is_reopenable(err: dict[str, Any] | None) -> bool`.
- Consumes existing `TossLiveOrderLedger` columns (`status`, `requires_manual_review`, `filled_qty`, `trade_id`, `last_reconcile_error`).
- Signature UNCHANGED: `toss_reconcile_orders_impl(*, symbol, order_id, market, dry_run, limit)` gains NO new parameter; the self-healing reopen runs on every pass and is previewed via `dry_run=True`.

Steps:

- [ ] **Write failing test — reopen is signature-guarded, evidence-guarded, dry-run-safe.** Create `tests/services/test_toss_reopen_anomalies.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.review import TossLiveOrderLedger
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    await db_session.execute(delete(TossLiveOrderLedger))
    await db_session.commit()
    yield


async def _anomaly(db_session, *, cid, error, filled=None, trade_id=None, market="kr"):
    row = TossLiveOrderLedger(
        trade_date=datetime(2026, 7, 1, tzinfo=UTC),
        broker="toss",
        account_mode="toss_live",
        operation_kind="place",
        market=market,
        symbol="034020",
        side="buy",
        order_type="limit",
        client_order_id=cid,
        broker_order_id=f"ord-{cid}",
        status="anomaly",
        requires_manual_review=True,
        manual_review_reason="parked",
        last_reconcile_error=error,
        filled_qty=filled,
        trade_id=trade_id,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def test_dry_run_lists_instrument_type_anomaly_without_mutating(db_session):
    row = await _anomaly(
        db_session,
        cid="a",
        error={"type": "ValueError", "message": "'equity' is not a valid InstrumentType"},
    )
    out = await TossLiveOrderLedgerService(db_session).reopen_anomalies_for_reconcile(
        dry_run=True
    )
    assert out["reopened"] == 0
    assert [c["ledger_id"] for c in out["candidates"]] == [row.id]

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "anomaly"  # unchanged in dry run


async def test_apply_reopens_only_bug_signature_rows(db_session):
    good = await _anomaly(
        db_session,
        cid="good",
        error={"type": "ValueError", "message": "'equity' is not a valid InstrumentType"},
    )
    transient = await _anomaly(
        db_session,
        cid="transient",
        error={"type": "TossRateLimitError", "code": "rate-limit-exceeded", "status_code": 429},
    )
    forbidden = await _anomaly(
        db_session,
        cid="forbidden",
        error={"type": "TossApiResponseError", "code": "non-json-response", "status_code": 403},
    )
    with_fill = await _anomaly(
        db_session,
        cid="hasfill",
        error={"type": "ValueError", "message": "'equity' is not a valid InstrumentType"},
        filled=Decimal("3"),
        trade_id=99,
    )

    out = await TossLiveOrderLedgerService(db_session).reopen_anomalies_for_reconcile(
        dry_run=False
    )
    reopened_ids = {c["ledger_id"] for c in out["candidates"]}
    assert out["reopened"] == 2
    assert reopened_ids == {good.id, transient.id}

    for rid, expect in [
        (good.id, "accepted"),
        (transient.id, "accepted"),
        (forbidden.id, "anomaly"),   # 403 never blindly reopened
        (with_fill.id, "anomaly"),   # has fill evidence -> never reopened
    ]:
        r = await db_session.get(TossLiveOrderLedger, rid)
        assert r.status == expect
    reopened_good = await db_session.get(TossLiveOrderLedger, good.id)
    assert reopened_good.requires_manual_review is False
    assert reopened_good.manual_review_reason is None
    assert reopened_good.last_reconcile_error is None


async def test_market_filter_scopes_reopen(db_session):
    kr = await _anomaly(
        db_session, cid="kr",
        error={"type": "ValueError", "message": "'equity' is not a valid InstrumentType"},
        market="kr",
    )
    await _anomaly(
        db_session, cid="us",
        error={"type": "ValueError", "message": "'equity' is not a valid InstrumentType"},
        market="us",
    )
    out = await TossLiveOrderLedgerService(db_session).reopen_anomalies_for_reconcile(
        dry_run=False, market="kr"
    )
    assert {c["ledger_id"] for c in out["candidates"]} == {kr.id}
    assert out["reopened"] == 1
```

- [ ] **Run it — fails.** `uv run pytest tests/services/test_toss_reopen_anomalies.py -v`
  Expected: AttributeError — `reopen_anomalies_for_reconcile` does not exist.

- [ ] **Minimal impl — service method + signature guard.** In `app/services/toss_live_order_ledger_service.py`, add `or_` to the sqlalchemy import (`from sqlalchemy import or_, select`) and add a module-level function above the class plus the method:
```python
# ROB-669 — anomaly rows are only auto-reopenable when the recorded error is a
# known-safe signature: the pre-ROB-631 invalid-InstrumentType code fault, or a
# transient (rate-limit / 5xx / token / network) failure. 403/404/idempotency
# contradictions are NEVER auto-reopened (operator must verify broker detail).
_REOPENABLE_TRANSIENT_CODES = frozenset(
    {
        "rate-limit-exceeded",
        "edge-rate-limit-exceeded",
        "internal-error",
        "maintenance",
        "expired-token",
        "invalid-token",
    }
)
_REOPENABLE_TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})
_REOPENABLE_TRANSIENT_TYPES = frozenset(
    {
        "ReadTimeout",
        "ConnectTimeout",
        "TimeoutException",
        "ConnectError",
        "TransportError",
        "TossRateLimitError",
    }
)


def _anomaly_error_is_reopenable(err: dict[str, Any] | None) -> bool:
    if not err:
        return False
    message = str(err.get("message") or "")
    if "is not a valid InstrumentType" in message:
        return True
    code = str(err.get("code") or "")
    if code in _REOPENABLE_TRANSIENT_CODES:
        return True
    status = err.get("status_code")
    if isinstance(status, int) and status in _REOPENABLE_TRANSIENT_HTTP:
        return True
    return str(err.get("type") or "") in _REOPENABLE_TRANSIENT_TYPES
```
Add the method to `TossLiveOrderLedgerService`:
```python
    async def reopen_anomalies_for_reconcile(
        self,
        *,
        dry_run: bool = True,
        market: str | None = None,
        symbol: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """ROB-669 R2 — reopen recoverable anomaly rows for another reconcile.

        Guards (all required): status='anomaly', requires_manual_review, NO fill
        evidence (filled_qty NULL/0 AND trade_id NULL), and a reopenable
        last_reconcile_error signature. Reopened rows go back to 'accepted' so the
        next reconcile pass re-selects them via list_open; re-booking is idempotent
        (review.trades ON CONFLICT DO NOTHING; buy journal gated on journal_id IS
        NULL). Never reopens 403/404/idempotency-conflict anomalies.
        """
        stmt = select(TossLiveOrderLedger).where(
            TossLiveOrderLedger.status == "anomaly",
            TossLiveOrderLedger.requires_manual_review.is_(True),
            TossLiveOrderLedger.trade_id.is_(None),
            or_(
                TossLiveOrderLedger.filled_qty.is_(None),
                TossLiveOrderLedger.filled_qty == 0,
            ),
        )
        if market:
            stmt = stmt.where(TossLiveOrderLedger.market == market)
        if symbol:
            stmt = stmt.where(TossLiveOrderLedger.symbol == symbol)
        stmt = stmt.order_by(TossLiveOrderLedger.created_at.asc()).limit(limit)
        rows = list((await self._db.execute(stmt)).scalars().all())

        candidates = [
            r for r in rows if _anomaly_error_is_reopenable(r.last_reconcile_error)
        ]
        reopened = 0
        candidate_meta = [
            {
                "ledger_id": r.id,
                "symbol": r.symbol,
                "market": r.market,
                "broker_order_id": r.broker_order_id,
                "error_type": (r.last_reconcile_error or {}).get("type"),
                "error_message": (r.last_reconcile_error or {}).get("message"),
            }
            for r in candidates
        ]  # snapshot BEFORE mutation so the echo still shows the original error
        if not dry_run and candidates:
            for r in candidates:
                r.status = "accepted"
                r.requires_manual_review = False
                r.manual_review_reason = None
                r.last_reconcile_error = None
                reopened += 1
            await self._db.commit()
            # Re-load so the caller can still read attributes when it merges these
            # rows into the reconcile work-list within the same session block.
            for r in candidates:
                await self._db.refresh(r)

        return {
            "dry_run": dry_run,
            "reopened": reopened,
            "rows": candidates,  # ORM rows folded into the reconcile work-list
            "candidates": candidate_meta,
        }
```
The caller MUST invoke this inside the same session block it uses for `list_open`
(so the returned `rows` are live-session ORM objects that detach together with the
`list_open` rows at block exit — matching today's detached-row reconcile loop).

- [ ] **Run it — passes.** `uv run pytest tests/services/test_toss_reopen_anomalies.py -v` → 3 passed.

- [ ] **Write failing test — impl surfaces reopen before reconcile.** Append to `tests/mcp_server/tooling/test_toss_reconcile_resilience.py`:
```python
async def test_impl_reopen_anomalies_runs_before_list(db_session):
    from datetime import UTC, datetime

    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.models.review import TossLiveOrderLedger

    bug = TossLiveOrderLedger(
        trade_date=datetime(2026, 7, 1, tzinfo=UTC),
        broker="toss", account_mode="toss_live", operation_kind="place",
        market="kr", symbol="034020", side="buy", order_type="limit",
        client_order_id="cid-bug", broker_order_id="ord-bug",
        status="anomaly", requires_manual_review=True, manual_review_reason="x",
        last_reconcile_error={
            "type": "ValueError",
            "message": "'equity' is not a valid InstrumentType",
        },
    )
    db_session.add(bug)
    await db_session.commit()
    await db_session.refresh(bug)

    with patch.object(
        mod,
        "TossBatchEvidenceSource",
        **{"build": AsyncMock(side_effect=RuntimeError("no network"))},
    ), patch.object(mod, "_reconcile_one_toss_row", new=AsyncMock(
        return_value={"verdict": "pending", "action": "noop_pending"}
    )):
        # Self-healing: NO reopen_anomalies param — recovery runs on every pass.
        out = await mod.toss_reconcile_orders_impl(market="kr", dry_run=False)

    assert out["reopened"]["reopened"] == 1
    # reopen flipped the bug row to 'accepted' and it was folded into the
    # work-list, so it was reconciled this same pass (verdict pending here).
    reopened = await db_session.get(TossLiveOrderLedger, bug.id)
    assert reopened.status in {"accepted", "pending"}
```

- [ ] **Run it — fails.** `uv run pytest tests/mcp_server/tooling/test_toss_reconcile_resilience.py -k reopen -v`
  Expected: FAIL — `toss_reconcile_orders_impl` does not yet run the self-healing reopen step, so there is no `reopened` key and the bug row stays `anomaly`.

- [ ] **Minimal impl — MERGED FINAL `toss_reconcile_orders_impl` (self-healing, signature UNCHANGED).** In `app/mcp_server/tooling/toss_live_ledger.py`, replace the whole body of `toss_reconcile_orders_impl` with the version below. This is the single, complete implementation — it folds the Task-3 batch build/loop AND the Task-4 reopen together; do NOT leave any Task-3 body as an ellipsis. **The keyword signature is unchanged (no `reopen_anomalies` param); `orders_toss_variants.py` is not touched.**
```python
async def toss_reconcile_orders_impl(
    *,
    symbol: str | None = None,
    order_id: str | None = None,
    market: str | None = None,
    dry_run: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    # Self-healing reopen + list_open run in ONE session block so both the
    # recoverable-anomaly rows and the open rows are live-session ORM objects that
    # detach together at block exit (matching the existing detached-row loop).
    async with _order_session_factory()() as db:
        service = TossLiveOrderLedgerService(db)
        reopen_report = await service.reopen_anomalies_for_reconcile(
            dry_run=dry_run, market=market, symbol=symbol, limit=limit
        )
        reopened_rows = reopen_report.pop("rows")  # ORM rows, not echoed
        open_rows = await service.list_open(
            symbol=symbol,
            order_id=order_id,
            market=market,
            limit=limit,
        )
        # Work-list = list_open rows + reopened rows, deduped by ledger id
        # (a non-dry-run reopened row is now 'accepted' and may also be in
        # open_rows). Touch attributes here while the session is still open.
        seen: set[int] = set()
        rows: list[TossLiveOrderLedger] = []
        for row in [*open_rows, *reopened_rows]:
            if row.id in seen:
                continue
            seen.add(row.id)
            rows.append(row)

    reconciled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    evidence_source = None
    if rows:
        try:
            evidence_source = await TossBatchEvidenceSource.build(
                rows=rows, symbol=symbol
            )
        except Exception as exc:  # noqa: BLE001 — batch is an optimization
            # Any batch-build failure (disabled/network/transient) degrades to the
            # per-row single-fetch path; per-row R1 classification still applies.
            logger.warning(
                "toss reconcile batch evidence build failed; per-row fallback: %s",
                exc,
            )
            evidence_source = None

    try:
        for row in rows:
            try:
                outcome = await _reconcile_one_toss_row(
                    row, dry_run=dry_run, evidence_source=evidence_source
                )
            except Exception as exc:  # noqa: BLE001 — classified in the handler
                outcome = await _handle_reconcile_row_error(
                    row, exc, dry_run=dry_run
                )
            reconciled.append(outcome)
            verdict = str(outcome.get("verdict", "anomaly"))
            counts[verdict] = counts.get(verdict, 0) + 1
    finally:
        if evidence_source is not None:
            await evidence_source.aclose()

    result: dict[str, Any] = {
        "success": True,
        "dry_run": dry_run,
        "counts": counts,
        "reconciled": reconciled,
        "reopened": reopen_report,  # {dry_run, reopened, candidates}
        "message": (
            f"Reconciled {len(reconciled)} Toss live order(s) "
            f"(dry_run={dry_run}): {counts}"
        ),
    }
    if evidence_source is not None:
        result["window"] = {
            "from": evidence_source.window_from,
            "to": evidence_source.window_to,
            "closed_pages_capped": evidence_source.closed_pages_capped,
            "single_fetch_fallbacks": evidence_source.single_fetch_count,
        }
    return result
```
> This body supersedes the Task-3 "Minimal impl part B" body — Task 3 wires the
> batch source and window echo; Task 4 adds the leading self-healing reopen + the
> deduped work-list merge and the `reopened` echo. A worker doing Task 4 in
> isolation MUST paste this whole function (not just the reopen delta) so the
> Task-3 batch build/loop is never dropped.

- [ ] **Run it — passes.** `uv run pytest tests/mcp_server/tooling/test_toss_reconcile_resilience.py -v` → all pass.

- [ ] **No `orders_toss_variants.py` edit.** The `toss_reconcile_orders` MCP tool wrapper and its description in `register_toss_live_order_tools` are intentionally untouched (ROB-668 owns edits to that function). Recovery reaches operators through the existing tool: run `toss_reconcile_orders(market="kr", dry_run=True)` to preview reopen candidates, then `dry_run=False` to apply. Verify no diff: `git diff --quiet app/mcp_server/tooling/orders_toss_variants.py`.

- [ ] **Minimal impl — runbook.** In `docs/runbooks/toss-live-order-reconcile.md`, (a) under a new "## Transient vs Anomaly (ROB-669)" section state that transient failures leave the row in its open status with only `last_reconcile_error` recorded (retried next pass) while `anomaly` is reserved for 404/403/duplicate contradictions; (b) rewrite the "## Re-opening `'equity'` InstrumentType Anomalies (ROB-631)" remediation so it uses the **self-healing reconcile pass** (no new tool flag — the existing `toss_reconcile_orders` tool now folds recoverable anomaly rows into its work-list automatically):
```markdown
Remediation (operator, one-off after deploying the fix) — use the existing
reconcile tool; recovery is self-healing, no special flag. The reconcile pass only
reopens no-fill anomaly rows (filled_qty NULL/0 AND trade_id NULL) whose
last_reconcile_error is the pre-ROB-631 InstrumentType signature or a transient
(rate-limit/5xx/token/network) signature. It never reopens 403/404/duplicate
contradictions.

1. Preview which rows would reopen (no mutation):

   ```bash
   toss_reconcile_orders(market="kr", dry_run=True)
   ```

   Inspect the `reopened.candidates` list and confirm each is a bug/transient row.

2. Apply: run the same pass without dry_run — qualifying rows are reopened AND
   reconciled together:

   ```bash
   toss_reconcile_orders(market="kr", dry_run=False)
   ```

3. Confirm the rows now show `status='filled'` (or `partial`) with non-null
   `trade_id` / `journal_id`.

> **Operator note — anomalies that are NOT auto-reopened.** Anomaly rows whose
> `last_reconcile_error` does NOT match the auto-reopen signatures (e.g. a genuine
> 403 access failure, a 404 order-not-found, a duplicate/idempotency contradiction,
> or any row that already carries fill evidence) are **intentionally left as
> `anomaly`** and never appear in `reopened.candidates`. These still require manual
> review: verify each against the Toss broker order detail before taking any action.
```
Keep the identification SQL query as a reference for auditing.

- [ ] **Run full task suite.** `uv run pytest tests/services/test_toss_reopen_anomalies.py tests/mcp_server/tooling/test_toss_reconcile_resilience.py tests/mcp_server/tooling/test_toss_live_ledger.py -v` → all pass.

- [ ] **Lint.** `make lint` → clean (ruff + ty). Fix any import-ordering / unused-import findings introduced.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-669): guarded toss anomaly reopen + reconcile, runbook (R2)"`

---

## Self-Review

Spec-coverage mapping (acceptance criteria → task):

| Acceptance criterion | Task | Verified by |
|----------------------|------|-------------|
| (a) KR filled buys reconcile to `filled` with fill evidence, not anomaly | Task 3 (batch feeds unchanged `classify_toss_order_evidence`; the `'equity'`→`equity_kr` fix from ROB-631 is already present at `toss_live_ledger.py:309/325`) + Task 4 (reopen recovers the residue) | `test_toss_live_ledger.py::test_reconcile_filled_kr_buy_books_with_equity_kr_instrument_type` (regression) + `test_toss_batch_evidence_source.py` |
| (b) `toss_reconcile_orders(kr)` completes well under 90s via batched list | Task 3 | `test_toss_batch_evidence_source.py::test_build_maps_open_and_closed_without_per_row_get_order` (asserts exactly 1 OPEN + 1 CLOSED call, no per-row `get_order`) |
| (c) transient failures leave rows retryable, never permanent anomaly | Task 2 | `test_toss_reconcile_resilience.py::test_transient_error_leaves_row_retryable_not_anomaly` (6 transient cases) + `test_404_order_not_found_still_marks_anomaly` |
| (d) the 37 backlog is recoverable via guarded reopen + reconcile | Task 4 | `test_toss_reopen_anomalies.py` (dry-run/apply/signature+fill guards/market scope) + `test_impl_reopen_anomalies_runs_before_list` |
| (e) `save_trade_retrospective` description documents both enums | Task 1 | `test_trade_retrospective_registration_docs.py` (3 tests) + `test_retrospective_validation_messages.py` (2 tests) |
| Light window bound + no silent truncation | Task 3 | `test_toss_batch_evidence_source.py::test_closed_pagination_is_capped_and_flagged`, `test_row_outside_window_falls_back_to_single_get_order`; `window` echo asserted in `test_impl_uses_batch_source_and_echoes_window` |
| Migration-0 | all | No `app/models/review.py` CHECK change, no alembic revision added |
| US path not regressed | Task 3 | `test_toss_live_ledger.py` US FX tests (`test_toss_us_buy_reconcile_captures_buy_fx_rate`, `..._sell_...`) still pass; `capture_reconcile_spot_fx` untouched |

## Out of scope

- Introducing a distinct `reconcile_deferred` ledger `status` value (would require an alembic CHECK-constraint migration). Resilience deliberately reuses the existing open statuses + `last_reconcile_error`; a dedicated status is a separate ticket if a reviewer requires it.
- Registering a standalone `toss_reopen_reconcile_anomalies` MCP tool, OR adding a `reopen_anomalies` parameter / description change to the existing `toss_reconcile_orders` tool. Recovery is **self-healing within the reconcile pass** — folded into `toss_reconcile_orders_impl` in `toss_live_ledger.py` with NO change to the MCP tool signature. This both avoids expanding the MCP surface (per the ROB-488 surface-audit posture) and keeps ROB-669 off `orders_toss_variants.py` / `register_toss_live_order_tools`, which ROB-668 is editing concurrently. Operators preview via `dry_run=True` and apply via `dry_run=False` on the unchanged tool.
- KIS / Upbit / generic `live_reconcile_orders` batching. This plan is Toss-scoped; the KIS reconcile already reads order-id-keyed daily history and is not the N+1 pattern addressed here.
- Changing the paused TaskIQ `toss_live.reconcile_periodic` cadence or its env gates, and any Prefect deployment registration (owned by robin-prefect-automations).
- Executing the one-off operator remediation of the live 37-row backlog (that is an operator action against production data, gated on deploying this fix; the runbook documents the flow).
- Frontend / `/invest` surfacing of reconcile status.
