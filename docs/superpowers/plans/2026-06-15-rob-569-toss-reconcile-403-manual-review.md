# ROB-569 Toss Reconcile 403 Manual Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Toss order reconcile fail closed and operator-visible when `GET /orders/{orderId}` returns `403 non-json-response`, while retrying only safe GET lookups once after token reissue.

**Architecture:** Keep retry behavior in the Toss transport client so all read paths share the same auth handling, but gate the new `403 non-json-response` retry to `method == "GET"`. Persist unresolved reconcile errors into `review.toss_live_order_ledger` with manual-review columns, and surface the same manual-review state in `toss_reconcile_orders` responses and the runbook. Existing `_TOKEN_CODES` retry and `429` backoff behavior remain unchanged.

**Tech Stack:** Python 3.13, httpx MockTransport, SQLAlchemy async ORM, Alembic, PostgreSQL JSONB, pytest/pytest-asyncio, Ruff.

---

## Risk And Scope

- This touches live-order bookkeeping and adds a DB migration. Treat ROB-569 as `high_risk_change`, `needs_stronger_model_review`, and `hold_for_final_review`.
- Do not broaden retry to live mutation POSTs. `place_order`, `modify_order`, and `cancel_order` must not retry on `403 non-json-response`; they should surface the error for anomaly/manual-review handling by callers.
- Do not change existing JSON token-code retry semantics. `_TOKEN_CODES = {"invalid-token", "expired-token"}` continues to work as today.
- Do not change existing `429` rate-limit behavior. Rate-limit responses use backoff and must not trigger OAuth token reissue loops.
- ROB-569 owns the migration baseline for Toss ledger manual-review fields. ROB-568 should stack on top of this migration.

## File Structure

- Modify `app/services/brokers/toss/client.py`
  - Add a small GET-only predicate for `403 non-json-response`.
  - Reuse the existing force-reissue path only when that predicate is true.
  - Leave `429` retry-before-parse behavior intact.
- Modify `tests/services/brokers/toss/test_client.py`
  - Add coverage for GET `403 non-json-response` retry after force reissue.
  - Add coverage proving mutation POSTs do not retry the same error.
  - Add coverage proving `429` uses backoff and does not force token reissue.
- Create `alembic/versions/20260615_rob569_toss_review.py`
  - Add `requires_manual_review`, `manual_review_reason`, and `last_reconcile_error` to `review.toss_live_order_ledger`.
  - Add an index on `requires_manual_review` for operator queries.
- Modify `app/models/review.py`
  - Map the three new Toss ledger columns.
- Modify `tests/test_rob538_toss_live_ledger_schema.py`
  - Assert the new columns exist.
- Modify `app/services/toss_live_order_ledger_service.py`
  - Add `mark_manual_review(...)`.
- Modify `tests/services/test_toss_live_order_ledger_service.py`
  - Verify `mark_manual_review` persists status, flag, reason, payload, and timestamp.
- Modify `app/mcp_server/tooling/toss_live_ledger.py`
  - Convert reconcile exceptions into DB-backed manual-review rows when `dry_run=False`.
  - Include `requires_manual_review`, `manual_review_reason`, and structured error fields in the tool response.
- Modify `tests/mcp_server/tooling/test_toss_live_ledger.py`
  - Verify failed reconcile marks DB manual-review only when not dry-run.
  - Verify dry-run reports manual-review without mutating the row.
- Modify `docs/runbooks/toss-live-order-reconcile.md`
  - Document GET-only retry, mutation non-retry, manual-review query, and operator procedure.
- Modify `app/mcp_server/README.md`
  - Update public Toss reconcile contract summary.

---

### Task 1: Lock GET-Only 403 Retry Behavior With Tests

**Files:**
- Modify: `tests/services/brokers/toss/test_client.py`

- [ ] **Step 1: Add failing GET retry test**

Append this test after `test_get_order_retries_once_after_invalid_token`:

```python
@pytest.mark.asyncio
async def test_get_order_retries_once_after_403_non_json_with_reissued_token() -> None:
    calls = 0
    token_calls: list[bool] = []
    failed_tokens: list[str | None] = []
    seen_authorizations: list[str] = []

    class TokenManager(_TokenManager):
        async def get_access_token(
            self, *, force_reissue: bool = False, failed_token: str | None = None
        ) -> str:
            token_calls.append(force_reissue)
            failed_tokens.append(failed_token)
            return "token-2" if force_reissue else "token-1"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        seen_authorizations.append(request.headers["Authorization"])
        if calls == 1:
            return httpx.Response(
                403,
                text="<html><body>Forbidden stale token</body></html>",
                headers={"cf-ray": "ray-403"},
                request=request,
            )
        return httpx.Response(
            200,
            json=_json(
                {
                    "orderId": "ord-403",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "orderType": "LIMIT",
                    "timeInForce": "DAY",
                    "status": "FILLED",
                    "price": "190",
                    "quantity": "1",
                    "orderAmount": None,
                    "currency": "USD",
                    "orderedAt": "2026-06-15T00:00:00Z",
                    "canceledAt": None,
                    "execution": {"filledQuantity": "1"},
                }
            ),
            request=request,
        )

    client = TossReadClient(
        token_manager=TokenManager(),
        account_seq=1,
        transport=httpx.MockTransport(handler),
    )
    try:
        order = await client.get_order("ord-403")
    finally:
        await client.aclose()

    assert order.order_id == "ord-403"
    assert calls == 2
    assert token_calls == [False, True]
    assert failed_tokens == [None, "token-1"]
    assert seen_authorizations == ["Bearer token-1", "Bearer token-2"]
```

- [ ] **Step 2: Add failing mutation non-retry test**

Add this import near the existing Toss client imports:

```python
from app.services.brokers.toss.errors import TossApiResponseError
```

Append this test after the GET retry test:

```python
@pytest.mark.asyncio
async def test_place_order_does_not_retry_403_non_json_for_mutation() -> None:
    calls = 0
    token_calls: list[bool] = []

    class TokenManager(_TokenManager):
        async def get_access_token(
            self, *, force_reissue: bool = False, failed_token: str | None = None
        ) -> str:
            token_calls.append(force_reissue)
            return "token-2" if force_reissue else "token-1"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            403,
            text="<html><body>Forbidden mutation</body></html>",
            headers={"cf-ray": "ray-post-403"},
            request=request,
        )

    client = TossReadClient(
        token_manager=TokenManager(),
        account_seq=999,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(TossApiResponseError) as exc_info:
            await client.place_order(
                {
                    "symbol": "AAPL",
                    "side": "BUY",
                    "orderType": "LIMIT",
                    "quantity": "1",
                    "price": "150.0",
                    "clientOrderId": "cid-post-403",
                }
            )
    finally:
        await client.aclose()

    assert calls == 1
    assert token_calls == [False]
    assert "status=403 code='non-json-response'" in str(exc_info.value)
```

- [ ] **Step 3: Add failing rate-limit no-token-reissue test**

Append this test near `test_prices_retries_once_after_429_retry_after`:

```python
@pytest.mark.asyncio
async def test_get_order_429_non_json_backs_off_without_token_reissue(monkeypatch) -> None:
    calls = 0
    sleeps: list[float] = []
    token_calls: list[bool] = []
    seen_authorizations: list[str] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class TokenManager(_TokenManager):
        async def get_access_token(
            self, *, force_reissue: bool = False, failed_token: str | None = None
        ) -> str:
            token_calls.append(force_reissue)
            return "token-2" if force_reissue else "token-1"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        seen_authorizations.append(request.headers["Authorization"])
        if calls == 1:
            return httpx.Response(
                429,
                text="<html><body>Too Many Requests</body></html>",
                headers={"Retry-After": "2"},
                request=request,
            )
        return httpx.Response(
            200,
            json=_json(
                {
                    "orderId": "ord-rate",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "orderType": "LIMIT",
                    "timeInForce": "DAY",
                    "status": "PENDING",
                    "price": "190",
                    "quantity": "1",
                    "orderAmount": None,
                    "currency": "USD",
                    "orderedAt": "2026-06-15T00:00:00Z",
                    "canceledAt": None,
                    "execution": {"filledQuantity": "0"},
                }
            ),
            request=request,
        )

    client = TossReadClient(
        token_manager=TokenManager(),
        account_seq=1,
        transport=httpx.MockTransport(handler),
    )
    try:
        order = await client.get_order("ord-rate")
    finally:
        await client.aclose()

    assert order.order_id == "ord-rate"
    assert calls == 2
    assert sleeps == [2.0]
    assert token_calls == [False]
    assert seen_authorizations == ["Bearer token-1", "Bearer token-1"]
```

- [ ] **Step 4: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_client.py \
  -k "403_non_json or 429_non_json" -q
```

Expected: the GET `403` test fails because the client raises immediately without force-reissue. The mutation non-retry and 429 tests may already pass; keep them as guardrails.

---

### Task 2: Implement GET-Only 403 Retry

**Files:**
- Modify: `app/services/brokers/toss/client.py`

- [ ] **Step 1: Add a narrow predicate**

Add this helper below `_TOKEN_CODES`:

```python
_GET_REISSUABLE_NON_JSON_STATUSES = {403}


def _should_retry_get_non_json_auth_error(
    method: str, exc: TossApiResponseError
) -> bool:
    return (
        method.upper() == "GET"
        and exc.status_code in _GET_REISSUABLE_NON_JSON_STATUSES
        and exc.envelope.code == "non-json-response"
    )
```

- [ ] **Step 2: Reuse the existing force-reissue branch**

Replace the `except TossApiResponseError as exc:` block in `_request` with:

```python
        except TossApiResponseError as exc:
            if exc.envelope.code in _TOKEN_CODES or _should_retry_get_non_json_auth_error(
                method, exc
            ):
                token = await self._token_manager.get_access_token(
                    force_reissue=True, failed_token=token
                )
                headers["Authorization"] = f"Bearer {token}"
                retry = await self._client.request(
                    method, path, params=params, json=json, headers=headers
                )
                return parse_toss_response(retry)
            raise
```

- [ ] **Step 3: Run focused client tests**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_client.py \
  -k "invalid_token or 403_non_json or 429_non_json or place_order_does_not_retry" -q
```

Expected: all selected tests pass.

- [ ] **Step 4: Commit**

```bash
git add app/services/brokers/toss/client.py tests/services/brokers/toss/test_client.py
git commit -m "fix: retry toss get order after 403 non-json auth error"
```

---

### Task 3: Add Toss Ledger Manual-Review Columns

**Files:**
- Create: `alembic/versions/20260615_rob569_toss_review.py`
- Modify: `app/models/review.py`
- Modify: `tests/test_rob538_toss_live_ledger_schema.py`

- [ ] **Step 1: Write the failing model-shape test**

In `tests/test_rob538_toss_live_ledger_schema.py`, add these names to the existing column loop:

```python
        "requires_manual_review",
        "manual_review_reason",
        "last_reconcile_error",
```

- [ ] **Step 2: Run the model-shape test to verify it fails**

Run:

```bash
uv run pytest tests/test_rob538_toss_live_ledger_schema.py -q
```

Expected: FAIL with `missing column requires_manual_review`.

- [ ] **Step 3: Add the Alembic migration**

Create `alembic/versions/20260615_rob569_toss_review.py`:

```python
"""ROB-569 add Toss reconcile manual-review fields.

Revision ID: 20260615_rob569_toss_review
Revises: ec2fbbc5898c
Create Date: 2026-06-15
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260615_rob569_toss_review"
down_revision: Union[str, Sequence[str], None] = "ec2fbbc5898c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "toss_live_order_ledger",
        sa.Column(
            "requires_manual_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema="review",
    )
    op.add_column(
        "toss_live_order_ledger",
        sa.Column("manual_review_reason", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "toss_live_order_ledger",
        sa.Column(
            "last_reconcile_error",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="review",
    )
    op.create_index(
        "ix_toss_live_ledger_manual_review",
        "toss_live_order_ledger",
        ["requires_manual_review"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_toss_live_ledger_manual_review",
        table_name="toss_live_order_ledger",
        schema="review",
    )
    op.drop_column("toss_live_order_ledger", "last_reconcile_error", schema="review")
    op.drop_column("toss_live_order_ledger", "manual_review_reason", schema="review")
    op.drop_column("toss_live_order_ledger", "requires_manual_review", schema="review")
```

- [ ] **Step 4: Map columns in the ORM**

In `app/models/review.py`, add these fields after `raw_response`:

```python
    requires_manual_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    manual_review_reason: Mapped[str | None] = mapped_column(Text)
    last_reconcile_error: Mapped[dict | None] = mapped_column(JSONB)
```

- [ ] **Step 5: Run model and migration smoke checks**

Run:

```bash
uv run pytest tests/test_rob538_toss_live_ledger_schema.py -q
uv run alembic heads
```

Expected: pytest passes; Alembic reports `20260615_rob569_toss_review (head)`.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/20260615_rob569_toss_review.py app/models/review.py tests/test_rob538_toss_live_ledger_schema.py
git commit -m "feat: add toss ledger manual review fields"
```

---

### Task 4: Add Service Method For Manual Review

**Files:**
- Modify: `app/services/toss_live_order_ledger_service.py`
- Modify: `tests/services/test_toss_live_order_ledger_service.py`

- [ ] **Step 1: Write failing service test**

Append this test to `tests/services/test_toss_live_order_ledger_service.py`:

```python
async def test_mark_manual_review_sets_operator_visible_error(db_session):
    svc = TossLiveOrderLedgerService(db_session)
    row = await svc.record_send(
        **_place_kwargs(
            client_order_id="cid-manual-review",
            broker_order_id="ord-manual-review",
        )
    )

    await svc.mark_manual_review(
        ledger_id=row.id,
        reason="reconcile failed; operator must verify Toss order detail",
        error={
            "type": "TossApiResponseError",
            "status_code": 403,
            "code": "non-json-response",
            "request_id": "ray-403",
            "message": "<html>Forbidden</html>",
        },
        broker_status=None,
    )

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed is not None
    assert refreshed.status == "anomaly"
    assert refreshed.requires_manual_review is True
    assert (
        refreshed.manual_review_reason
        == "reconcile failed; operator must verify Toss order detail"
    )
    assert refreshed.last_reconcile_error == {
        "type": "TossApiResponseError",
        "status_code": 403,
        "code": "non-json-response",
        "request_id": "ray-403",
        "message": "<html>Forbidden</html>",
    }
    assert refreshed.broker_status is None
    assert refreshed.reconciled_at is not None
```

- [ ] **Step 2: Run the service test to verify it fails**

Run:

```bash
uv run pytest tests/services/test_toss_live_order_ledger_service.py \
  -k "manual_review" -q
```

Expected: FAIL with `AttributeError: 'TossLiveOrderLedgerService' object has no attribute 'mark_manual_review'`.

- [ ] **Step 3: Implement `mark_manual_review`**

Add this method after `update_reconcile_outcome` in `app/services/toss_live_order_ledger_service.py`:

```python
    async def mark_manual_review(
        self,
        *,
        ledger_id: int,
        reason: str,
        error: dict[str, Any],
        broker_status: str | None = None,
    ) -> None:
        row = await self._db.get(TossLiveOrderLedger, ledger_id)
        if row is None:
            return
        row.status = "anomaly"
        row.broker_status = broker_status
        row.requires_manual_review = True
        row.manual_review_reason = reason
        row.last_reconcile_error = error
        row.reconciled_at = datetime.now(UTC)
        await self._db.commit()
```

- [ ] **Step 4: Run service tests**

Run:

```bash
uv run pytest tests/services/test_toss_live_order_ledger_service.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/toss_live_order_ledger_service.py tests/services/test_toss_live_order_ledger_service.py
git commit -m "feat: persist toss reconcile manual review state"
```

---

### Task 5: Persist Manual Review From `toss_reconcile_orders`

**Files:**
- Modify: `app/mcp_server/tooling/toss_live_ledger.py`
- Modify: `tests/mcp_server/tooling/test_toss_live_ledger.py`

- [ ] **Step 1: Write failing dry-run guard test**

Append this test to `tests/mcp_server/tooling/test_toss_live_ledger.py`:

```python
async def test_reconcile_impl_reports_manual_review_on_error_without_mutating_dry_run(
    db_session,
):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.services.brokers.toss.errors import TossApiResponseError, TossErrorEnvelope

    row = await _accepted(db_session)
    err = TossApiResponseError(
        TossErrorEnvelope(
            request_id="ray-dry",
            code="non-json-response",
            message="<html>Forbidden dry-run</html>",
            data=None,
        ),
        status_code=403,
    )

    with patch.object(mod, "_reconcile_one_toss_row", new=AsyncMock(side_effect=err)):
        out = await mod.toss_reconcile_orders_impl(dry_run=True)

    assert out["counts"] == {"anomaly": 1}
    assert out["reconciled"][0]["requires_manual_review"] is True
    assert out["reconciled"][0]["manual_review_reason"].startswith(
        "reconcile failed; operator must verify Toss order detail"
    )
    assert out["reconciled"][0]["error_details"] == {
        "type": "TossApiResponseError",
        "status_code": 403,
        "code": "non-json-response",
        "request_id": "ray-dry",
        "message": "<html>Forbidden dry-run</html>",
        "data": None,
    }

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "accepted"
    assert refreshed.requires_manual_review is False
    assert refreshed.last_reconcile_error is None
```

- [ ] **Step 2: Write failing non-dry-run persistence test**

Append this test below the dry-run test:

```python
async def test_reconcile_impl_marks_manual_review_on_error_when_not_dry_run(
    db_session,
):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.services.brokers.toss.errors import TossApiResponseError, TossErrorEnvelope

    row = await _accepted(db_session)
    err = TossApiResponseError(
        TossErrorEnvelope(
            request_id="ray-apply",
            code="non-json-response",
            message="<html>Forbidden apply</html>",
            data=None,
        ),
        status_code=403,
    )

    with patch.object(mod, "_reconcile_one_toss_row", new=AsyncMock(side_effect=err)):
        out = await mod.toss_reconcile_orders_impl(dry_run=False)

    assert out["counts"] == {"anomaly": 1}
    assert out["reconciled"][0]["action"] == "requires_manual_review"
    assert out["reconciled"][0]["requires_manual_review"] is True

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "anomaly"
    assert refreshed.requires_manual_review is True
    assert refreshed.manual_review_reason.startswith(
        "reconcile failed; operator must verify Toss order detail"
    )
    assert refreshed.last_reconcile_error == {
        "type": "TossApiResponseError",
        "status_code": 403,
        "code": "non-json-response",
        "request_id": "ray-apply",
        "message": "<html>Forbidden apply</html>",
        "data": None,
    }
```

- [ ] **Step 3: Run the reconcile tests to verify they fail**

Run:

```bash
uv run pytest tests/mcp_server/tooling/test_toss_live_ledger.py \
  -k "manual_review_on_error" -q
```

Expected: FAIL because the response does not include manual-review fields and the DB row is not updated.

- [ ] **Step 4: Add structured error helpers**

In `app/mcp_server/tooling/toss_live_ledger.py`, add this import:

```python
from app.services.brokers.toss.errors import TossApiResponseError
```

Add these helpers near the logger:

```python
def _reconcile_error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, TossApiResponseError):
        return {
            "type": exc.__class__.__name__,
            "status_code": exc.status_code,
            "code": exc.envelope.code,
            "request_id": exc.envelope.request_id,
            "message": exc.envelope.message,
            "data": exc.envelope.data,
        }
    return {
        "type": exc.__class__.__name__,
        "message": str(exc) or exc.__class__.__name__,
    }


def _manual_review_reason(row: TossLiveOrderLedger, exc: Exception) -> str:
    return (
        "reconcile failed; operator must verify Toss order detail "
        f"before booking or closing ledger_id={row.id} order_id={row.broker_order_id}: "
        f"{str(exc) or exc.__class__.__name__}"
    )
```

- [ ] **Step 5: Persist manual-review state in the reconcile loop**

Replace the generic `except Exception as exc:` branch inside `toss_reconcile_orders_impl` with:

```python
        except Exception as exc:
            logger.warning(
                "toss reconcile failed order_id=%s: %s", row.broker_order_id, exc
            )
            error_details = _reconcile_error_payload(exc)
            reason = _manual_review_reason(row, exc)
            if not dry_run:
                async with _order_session_factory()() as db:
                    await TossLiveOrderLedgerService(db).mark_manual_review(
                        ledger_id=row.id,
                        reason=reason,
                        error=error_details,
                    )
            outcome = {
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

- [ ] **Step 6: Run focused reconcile tests**

Run:

```bash
uv run pytest tests/mcp_server/tooling/test_toss_live_ledger.py \
  -k "manual_review_on_error or reconcile_impl_lists_only_toss_rows" -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/toss_live_ledger.py tests/mcp_server/tooling/test_toss_live_ledger.py
git commit -m "fix: mark toss reconcile errors for manual review"
```

---

### Task 6: Document Operator Procedure

**Files:**
- Modify: `docs/runbooks/toss-live-order-reconcile.md`
- Modify: `app/mcp_server/README.md`

- [ ] **Step 1: Update Toss reconcile runbook**

In `docs/runbooks/toss-live-order-reconcile.md`, add this section after "Status Semantics":

```markdown
## 403 / non-JSON Manual Review

`toss_reconcile_orders` fetches broker evidence with `GET /orders/{orderId}`.
When a GET order lookup returns `403` with a non-JSON body, the Toss client
force-reissues the OAuth token once and retries the same GET. If the retry still
fails, reconcile fails closed:

- the tool response returns `verdict="anomaly"`, `action="requires_manual_review"`,
  and structured `error_details`;
- `review.toss_live_order_ledger.status` becomes `anomaly`;
- `requires_manual_review=true`, `manual_review_reason`, and
  `last_reconcile_error` are persisted for operator lookup.

Mutation POSTs (`place`, `modify`, `cancel`) do not use this new 403 retry path.
They must not be repeated implicitly because a retry can create duplicate live
order side effects. Rate-limit (`429`) responses continue to use backoff and do
not trigger token reissue loops.
```

Add this section before "Operational Hold":

````markdown
## Manual Review Query

```sql
SELECT
    id,
    market,
    symbol,
    broker_order_id,
    operation_kind,
    status,
    manual_review_reason,
    last_reconcile_error,
    updated_at
FROM review.toss_live_order_ledger
WHERE requires_manual_review IS TRUE
ORDER BY updated_at DESC, id DESC;
```

For each row, verify the Toss broker UI/API order detail before booking a fill,
closing the row, or resetting it for another reconcile attempt. Do not infer a
cancel or fill from a missing/failed order-detail response.
````

- [ ] **Step 2: Update MCP README contract**

In `app/mcp_server/README.md`, extend the accepted-only ledger bullet:

```markdown
- **Accepted-only ledger and reconcile**: Real `toss_place_order` writes only an accepted/rejected row to `review.toss_live_order_ledger`. It does not create fills, journals, or realized PnL at send time. `toss_reconcile_orders(dry_run=True)` previews broker evidence from `GET /orders/{orderId}`; `dry_run=False` books only confirmed execution deltas. GET order-detail `403 non-json-response` failures are retried once after token reissue; unresolved failures are persisted as `requires_manual_review=true`. Mutation POSTs are not implicitly retried on that error.
```

- [ ] **Step 3: Run docs grep**

Run:

```bash
rg -n "requires_manual_review|403|non-JSON|Mutation POSTs" \
  docs/runbooks/toss-live-order-reconcile.md app/mcp_server/README.md
```

Expected: output shows the new runbook and README language.

- [ ] **Step 4: Commit**

```bash
git add docs/runbooks/toss-live-order-reconcile.md app/mcp_server/README.md
git commit -m "docs: document toss reconcile manual review"
```

---

### Task 7: Final Verification And Linear Bookkeeping

**Files:**
- Modify through Linear only: `ROB-569`

- [ ] **Step 1: Run targeted test suite**

Run:

```bash
uv run pytest \
  tests/services/brokers/toss/test_client.py \
  tests/services/test_toss_live_order_ledger_service.py \
  tests/mcp_server/tooling/test_toss_live_ledger.py \
  tests/test_rob538_toss_live_ledger_schema.py \
  -q
```

Expected: all tests pass.

- [ ] **Step 2: Run lint for touched files**

Run:

```bash
uv run ruff check \
  app/services/brokers/toss/client.py \
  app/services/toss_live_order_ledger_service.py \
  app/mcp_server/tooling/toss_live_ledger.py \
  app/models/review.py \
  tests/services/brokers/toss/test_client.py \
  tests/services/test_toss_live_order_ledger_service.py \
  tests/mcp_server/tooling/test_toss_live_ledger.py \
  tests/test_rob538_toss_live_ledger_schema.py \
  alembic/versions/20260615_rob569_toss_review.py
```

Expected: no Ruff violations.

- [ ] **Step 3: Run migration head check**

Run:

```bash
uv run alembic heads
```

Expected: exactly one head, `20260615_rob569_toss_review (head)`.

- [ ] **Step 4: Update Linear metadata**

Apply these labels/comments to `ROB-569`:

```markdown
Applying high_risk_change + needs_stronger_model_review + hold_for_final_review for ROB-569: this adds a DB migration and changes live Toss order reconcile failure handling. Implementation must not be deployed or used for live trading until stronger-model/CTO review clears the manual-review and retry boundaries.
```

- [ ] **Step 5: Final commit if any verification-only changes remain**

If formatting or docs changed during verification:

```bash
git status --short
git add app/services/brokers/toss/client.py \
  app/services/toss_live_order_ledger_service.py \
  app/mcp_server/tooling/toss_live_ledger.py \
  app/models/review.py \
  tests/services/brokers/toss/test_client.py \
  tests/services/test_toss_live_order_ledger_service.py \
  tests/mcp_server/tooling/test_toss_live_ledger.py \
  tests/test_rob538_toss_live_ledger_schema.py \
  alembic/versions/20260615_rob569_toss_review.py \
  docs/runbooks/toss-live-order-reconcile.md \
  app/mcp_server/README.md
git commit -m "chore: verify toss reconcile manual review"
```

Expected: no uncommitted implementation changes remain except intentionally ignored local environment files such as `.venv/`.

---

## Self-Review

- Spec coverage: GET-only `403 non-json-response` retry is covered by Task 1 and Task 2. Mutation non-retry is covered by Task 1. Manual review DB persistence and response fields are covered by Tasks 3-5. Runbook and README updates are covered by Task 6. Linear risk labels/comments are covered by Task 7.
- Placeholder scan: no placeholder-pattern text remains.
- Type consistency: the plan consistently uses `requires_manual_review: bool`, `manual_review_reason: str | None`, `last_reconcile_error: dict | None`, and `mark_manual_review(...)` across model, migration, service, tests, and docs.
