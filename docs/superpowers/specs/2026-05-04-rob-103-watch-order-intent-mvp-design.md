# ROB-103 Watch Order Intent MVP — Design

**Status:** approved (brainstorm)
**Date:** 2026-05-04
**Linear:** https://linear.app/mgh3326/issue/ROB-103/watch-order-intent-mvp-for-approval-required-kis-mock-actions
**Depends on:** ROB-100 / PR #670 — `app/schemas/execution_contracts.py` (merged: `af1ff1d4`)
**Parent:** ROB-40

## 1. Goal

Extend watch alerts so a triggered watch can produce an **approval-required `OrderPreviewLine` intent** for KIS mock actions, while keeping the existing alert-only behavior unchanged by default. This issue adds the **intent creation** step only — broker submission is explicitly out of scope.

The current watch flow is alert-only: condition hit → batched n8n alert → all triggered watches removed. ROB-103 introduces a second, guarded branch that produces an audit-recorded preview intent and emits it alongside the existing alert.

## 2. Scope

### In scope
- Per-watch action policy stored in the existing Redis hash (`watch:alerts:{market}`) payload, JSON-encoded.
- Strict validation of the policy at watch add time and at scan/trigger time.
- New DB ledger `watch_order_intent_ledger` (service-only writes) holding `previewed` / `failed` rows.
- ROB-100 contract reuse: `OrderPreviewLine`, `ExecutionGuard`, `OrderBasketPreview`, `AccountMode`, `ExecutionSource`, `OrderLifecycleState`.
- KST-day idempotency keyed by `{market}:{target_kind}:{symbol}:{condition_type}:{threshold_key}:{action}:{side}:{kst_date}`, enforced via PostgreSQL **partial unique index** scoped to `lifecycle_state = 'previewed'`.
- US watch cap evaluated by FX-converting USD notional to KRW using `exchange_rate_service.get_usd_krw_quote()`.
- Additive `intents` block in the n8n batched alert payload and message body (no second alert).
- `manage_watch_alerts` MCP tool extended with optional policy args; missing args → `notify_only` (full backward compatibility).
- Read-only MCP tools and HTTP GET routes for the ledger.

### Out of scope (non-goals)
- KIS mock or live broker submission.
- Automatic watch execution.
- Live-account approval bypass.
- Production scheduler changes.
- `crypto + create_order_intent` and `alpaca_paper` matching rules (left as future work; AccountMode enum kept open).
- `cooldown_minutes`, intraday re-entry, price-bucket dedupe.
- `update` action on watch policy (use remove + add).
- Additional `price_policy` enum values beyond `threshold_as_limit` + optional static `limit_price` override.

## 3. Allowed / rejected combinations

| Case | Result |
|---|---|
| `market=kr/us`, `condition_type ∈ {price_above, price_below}`, `action=create_order_intent`, `account_mode=kis_mock` | allowed |
| `market=crypto`, `action=create_order_intent` | reject (validation error) |
| `condition_type ∈ {rsi_*, trade_value_*}`, `action=create_order_intent` | reject (validation error) |
| `account_mode != kis_mock` with `action=create_order_intent` | reject (validation error) |
| Any market, `action=notify_only` (or absent) | unchanged behavior |

ROB-100 `AccountMode` enum is **not narrowed**. The matching rule is the only gate today; future PRs may relax it. The codebase carries `# TODO(ROB-future): widen account_mode/market combos here` markers at the gate site.

## 4. Architecture & file impact

### 4.1 New files

```
app/models/watch_order_intent.py
app/services/watch_intent_policy.py
app/services/watch_order_intent_preview_builder.py
app/services/watch_order_intent_service.py
app/routers/watch_order_intent_ledger.py
app/mcp_server/tooling/watch_order_intent_ledger_registration.py
alembic/versions/<timestamp>_add_watch_order_intent_ledger.py
docs/runbooks/watch-order-intent-ledger.md

tests/test_watch_intent_policy.py
tests/test_watch_order_intent_preview_builder.py
tests/test_watch_order_intent_service.py
tests/test_watch_order_intent_ledger_router.py
tests/test_mcp_watch_order_intent_ledger.py
```

### 4.2 Modified files

```
app/services/watch_alerts.py                              # extend payload, validate, surface policy in list_watches
app/jobs/watch_scanner.py                                 # branch to intent service; build intents block
app/mcp_server/tooling/watch_alerts_registration.py       # extend manage_watch_alerts add args
app/services/openclaw_client.py                           # additive `intents` field in n8n payload

tests/test_watch_alerts.py                                # backward compat + new validation
tests/test_watch_scanner.py                               # previewed / failed / dedupe_hit branches
tests/test_mcp_watch_alerts.py                            # extended add args
```

### 4.3 Responsibility split (pure → side-effect)

- `watch_intent_policy.py` — **pure**. Parses raw Redis payload (or in-memory dict) into a `WatchActionPolicy` value object; raises `WatchPolicyError` with a stable string code on validation failure. No DB, Redis, HTTP, settings, or logging side effects.
- `watch_order_intent_preview_builder.py` — **pure**. Inputs: `(WatchActionPolicy, watch_record, triggered_value, fx_quote_or_none, kst_date)`. Outputs: an `IntentBuildResult` containing either a fully-formed `OrderPreviewLine` + `OrderBasketPreview` (with KRW evaluation) or a structured failure (`blocked_by`, `blocking_reasons`, `detail`). No I/O.
- `watch_order_intent_service.py` — **only writer**. Calls FX service if needed, calls the builder, INSERTs the ledger row, handles `IntegrityError` from the partial unique index by reading back the existing `previewed` row and returning `dedupe_hit`. Returns `IntentEmissionResult`.
- `watch_alerts.py` — extends payload schema and validation at `add_watch`; `list_watches` surfaces the parsed policy on each row (additive fields). No new external calls.
- `watch_scanner.py` — orchestrates: parse policy from each watch, evaluate condition, branch to service for `create_order_intent`, aggregate intents, decide watch deletion per outcome, send n8n alert.

The watch scanner does **not** import the ledger model, the FX service, or the builder directly; it goes through `watch_order_intent_service.py`.

## 5. Redis payload schema & validation

### 5.1 Payload shape

The hash field value at `HGET watch:alerts:{market} {field}` is now JSON of:

```jsonc
{
  "created_at": "2026-05-04T...",                  // existing
  "action": "notify_only" | "create_order_intent", // optional, default notify_only
  // create_order_intent only:
  "side": "buy" | "sell",
  "quantity": 1,                                   // positive integer; XOR notional_krw
  "notional_krw": 1000000.0,                       // KR-only; XOR quantity
  "limit_price": 70000.0,                          // optional static override (Decimal-safe)
  "max_notional_krw": 1500000.0                    // optional cap
}
```

Backward compatibility:
- A payload of `{"created_at": "..."}` (no `action` key) → `action="notify_only"`.
- A non-JSON / unparsable payload → also `notify_only` with a warning log; the watch keeps its existing behavior. This matches `list_watches`'s current tolerance for malformed payloads.

### 5.2 Validation rules (`watch_intent_policy.py`)

`parse_policy(market, target_kind, condition_type, raw_payload) -> WatchActionPolicy`:

| Condition | Action |
|---|---|
| `action` missing or `notify_only` | If `side`/`quantity`/`notional_krw`/`limit_price`/`max_notional_krw` present → `WatchPolicyError("notify_only_must_be_bare")`. Else return `NotifyOnlyPolicy()`. |
| `action="create_order_intent"` + `market=crypto` | `WatchPolicyError("intent_market_unsupported")`. |
| `action="create_order_intent"` + `condition_type ∉ {price_above, price_below}` | `WatchPolicyError("intent_condition_unsupported")`. |
| `action="create_order_intent"` + `side ∉ {buy, sell}` | `WatchPolicyError("intent_side_invalid")`. |
| `action="create_order_intent"` + both `quantity` and `notional_krw` (or neither) present | `WatchPolicyError("intent_sizing_xor")`. |
| `action="create_order_intent"` + `market=us` + `notional_krw` present | `WatchPolicyError("intent_us_notional_krw_unsupported")`. |
| `action="create_order_intent"` + `quantity` non-integer or `<= 0` | `WatchPolicyError("intent_quantity_invalid")`. |
| `action="create_order_intent"` + `limit_price <= 0` | `WatchPolicyError("intent_limit_price_invalid")`. |
| `action="create_order_intent"` + `max_notional_krw <= 0` | `WatchPolicyError("intent_max_notional_invalid")`. |
| `action="create_order_intent"` + `notional_krw <= 0` | `WatchPolicyError("intent_notional_krw_invalid")`. |

`add_watch` calls `parse_policy` before HSET; failures are returned to the MCP tool as `{"success": false, "error": "<code>: <message>"}` and **the watch is not stored**. Trigger-time parsing reuses the same function; legacy payloads pre-dating this PR continue to parse as `NotifyOnlyPolicy`.

`account_mode` is not stored in the Redis payload — it is a code-level constant `"kis_mock"` for any `create_order_intent` watch in this MVP. Future PRs may add `account_mode` to the policy and widen the gate.

## 6. Scanner flow

```python
async def scan_market(self, market: str) -> dict:
    watches = await self._watch_service.get_watches_for_market(market)
    market_open = self._is_market_open(market)
    ...

    triggered: list[dict] = []          # existing
    triggered_fields: list[str] = []    # existing — fields that should be deleted on alert success
    intents: list[dict] = []            # new — n8n payload intents block

    for watch in watches:
        if not market_open and target_kind != "fx":
            continue
        try:
            policy = parse_policy(market, target_kind, condition_type, raw_payload)
        except WatchPolicyError as exc:
            logger.warning("Skipping watch with invalid policy: %s field=%s err=%s", ...)
            continue

        current = await self._get_current_value(...)
        if not self._is_triggered(current, operator, threshold):
            continue

        if isinstance(policy, NotifyOnlyPolicy):
            triggered.append({...})         # existing
            triggered_fields.append(field)  # existing
            continue

        # IntentPolicy
        result = await self._intent_service.emit_intent(
            watch=watch,
            policy=policy,
            triggered_value=current,
            kst_date=now_kst().date().isoformat(),
            correlation_id=uuid4().hex,
        )
        intents.append(result.to_alert_dict())

        if result.status in {"previewed", "dedupe_hit"}:
            triggered_fields.append(field)
        # status == "failed" → keep watch
        # We still consider the trigger "noteworthy" enough to alert; the failed row sits in intents.

    # Build batched message + payload (additive intents block)
    # On n8n success, delete each field in triggered_fields
```

`emit_intent` flow (in `watch_order_intent_service.py`):

1. Compute KST date and `idempotency_key`.
2. Resolve `limit_price`: payload override if present, else watch threshold (price-based trigger guarantees this is meaningful).
3. If `notional_krw` sizing: `qty = floor(notional_krw / limit_price)`. If `qty < 1` → record `failed/qty_zero`.
4. Compute `notional_krw_evaluated`:
   - KR: `qty * limit_price`. `currency = "KRW"`.
   - US: fetch FX. On failure → `failed/fx_unavailable`. On success: `qty * limit_price * fx`. `currency = "USD"`.
5. If `max_notional_krw` is set and `notional_krw_evaluated > max_notional_krw` → `failed/max_notional_krw_cap`.
6. Build `OrderPreviewLine` (`lifecycle_state="previewed"`, `guard.execution_allowed=False`, `guard.approval_required=True`, `account_mode="kis_mock"`, `execution_source="watch"`) and wrap in `OrderBasketPreview`.
7. INSERT row (`lifecycle_state="previewed"`).
   - On `IntegrityError` against `uq_watch_intent_previewed_idempotency`: SELECT the existing `previewed` row by `idempotency_key`, return `dedupe_hit` with that row's `correlation_id` and `id`.
8. Failure paths from steps 3–5 INSERT a `failed` row (no idempotency conflict, since the partial unique index excludes `failed`). The row records `blocked_by`, `blocking_reasons`, and the original sizing inputs.

`IntentEmissionResult` shape:

```python
@dataclass
class IntentEmissionResult:
    status: Literal["previewed", "failed", "dedupe_hit"]
    ledger_id: int | None
    correlation_id: str | None
    idempotency_key: str
    market: str
    symbol: str
    side: str
    quantity: Decimal | None
    limit_price: Decimal | None
    blocked_by: str | None
    reason: str | None
```

`to_alert_dict()` returns the n8n-shaped dict.

## 7. Ledger schema

```python
# app/models/watch_order_intent.py
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP, BigInteger, Boolean, Index, Numeric, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class WatchOrderIntentLedger(Base):
    __tablename__ = "watch_order_intent_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    correlation_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    market: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    condition_type: Mapped[str] = mapped_column(Text, nullable=False)
    threshold: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    threshold_key: Mapped[str] = mapped_column(Text, nullable=False)

    action: Mapped[str] = mapped_column(Text, nullable=False)              # create_order_intent
    side: Mapped[str] = mapped_column(Text, nullable=False)                # buy | sell
    account_mode: Mapped[str] = mapped_column(Text, nullable=False)        # kis_mock
    execution_source: Mapped[str] = mapped_column(Text, nullable=False)    # watch
    lifecycle_state: Mapped[str] = mapped_column(Text, nullable=False)     # previewed | failed

    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    notional: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    currency: Mapped[str | None] = mapped_column(Text, nullable=True)

    notional_krw_input: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    max_notional_krw: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    notional_krw_evaluated: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    fx_usd_krw_used: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)

    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    execution_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocking_reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    blocked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    preview_line: Mapped[dict] = mapped_column(JSONB, nullable=False)
    triggered_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    kst_date: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_watch_intent_kst_date", "kst_date"),
        Index("ix_watch_intent_market_symbol", "market", "symbol"),
        Index("ix_watch_intent_state_created_at", "lifecycle_state", "created_at"),
    )
```

`blocked_by` value set: `max_notional_krw_cap | fx_unavailable | qty_zero | validation_error`. `detail` always carries `basket_preview` (full `OrderBasketPreview` model dump) plus failure context (`{"input_notional_krw": ..., "evaluated_notional_krw": ..., "fx_quote_attempted_at": ..., ...}`).

`preview_line` mirrors the ROB-100 `OrderPreviewLine` dump even on `failed` rows (with `lifecycle_state="failed"`), so a single column is the canonical preview shape regardless of outcome.

### 7.1 Alembic migration

```python
def upgrade() -> None:
    op.create_table(
        "watch_order_intent_ledger",
        # columns as above
    )
    op.create_index("ix_watch_intent_kst_date", ...)
    op.create_index("ix_watch_intent_market_symbol", ...)
    op.create_index("ix_watch_intent_state_created_at", ...)
    op.execute(
        """
        CREATE UNIQUE INDEX uq_watch_intent_previewed_idempotency
        ON watch_order_intent_ledger (idempotency_key)
        WHERE lifecycle_state = 'previewed';
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_watch_intent_previewed_idempotency;")
    op.drop_index("ix_watch_intent_state_created_at", ...)
    op.drop_index("ix_watch_intent_market_symbol", ...)
    op.drop_index("ix_watch_intent_kst_date", ...)
    op.drop_table("watch_order_intent_ledger")
```

The partial unique index is created via raw SQL (Alembic's `create_index` does support `postgresql_where`, but raw SQL keeps the predicate explicit and grep-friendly).

## 8. n8n payload extension

`OpenClawClient.send_watch_alert_to_n8n` adds a top-level `intents: list[dict]` field (default `[]`). The existing `triggered`, `as_of`, `correlation_id`, `market` fields are unchanged. Consumers that ignore unknown fields keep working.

Each item in `intents`:

```jsonc
{
  "market": "kr",
  "symbol": "005930",
  "side": "buy",
  "quantity": 1,
  "limit_price": 70000,
  "status": "previewed",                  // previewed | failed | dedupe_hit
  "ledger_id": "<id-or-uuid>",
  "correlation_id": "<uuid4>",
  "idempotency_key": "kr:asset:005930:price_below:70000:create_order_intent:buy:2026-05-04",
  "blocked_by": null,                     // string when status=failed
  "reason": null
}
```

`_build_batched_message` appends an "Order intents (kis_mock)" section after the existing trigger lines, formatted line-per-intent:

```
- previewed: 005930 buy qty=1 limit=70000 ledger=<id>
- failed: AAPL buy qty=10 limit=180.0 (blocked_by=max_notional_krw_cap, watch kept)
- dedupe_hit: 005930 buy (already previewed today, ledger=<id>)
```

If there are zero intents the section is omitted (no behavioral change for pure notify-only scans).

## 9. MCP tool surface

### 9.1 Existing tool extended: `manage_watch_alerts`

`add` action accepts new optional kwargs:

- `action: "notify_only" | "create_order_intent"` — default `notify_only`.
- `side: "buy" | "sell"` — required when `action="create_order_intent"`.
- `quantity: int` — XOR with `notional_krw`.
- `notional_krw: float` — XOR with `quantity`. KR only.
- `limit_price: float` — optional static override.
- `max_notional_krw: float` — optional cap.

Validation calls `parse_policy` and surfaces `WatchPolicyError.code` directly. Calls without these kwargs are byte-for-byte identical to today's behavior.

### 9.2 New read-only tools

`watch_order_intent_ledger_list_recent(market: str | None = None, lifecycle_state: str | None = None, kst_date: str | None = None, limit: int = 20) -> dict`

Returns recent rows ordered `created_at DESC`. Maximum `limit` clamped to 100.

`watch_order_intent_ledger_get(correlation_id: str) -> dict`

Returns a single row by correlation_id, or `{"success": false, "error": "not_found"}`.

Both tools are read-only, dump-able JSON shapes. They live in `app/mcp_server/tooling/watch_order_intent_ledger_registration.py`.

### 9.3 New HTTP routes (read-only)

`app/routers/watch_order_intent_ledger.py`:

- `GET /trading/api/watch/order-intent/ledger/recent` — same query semantics as the MCP `list_recent` tool.
- `GET /trading/api/watch/order-intent/ledger/{correlation_id}` — same as the MCP `get` tool.

No POST/PUT/DELETE endpoints are added in this PR.

## 10. Testing strategy

Each test file matches the responsibility split.

- `tests/test_watch_intent_policy.py` (pure unit) — every validation rule from §5.2; backward compatibility for the `{"created_at": ...}` legacy payload; malformed/non-JSON payload returns `NotifyOnlyPolicy` with a warning.
- `tests/test_watch_order_intent_preview_builder.py` (pure unit) — six fixtures: KR + qty (success), KR + notional_krw (success), KR + notional_krw → `qty_zero`, US + qty (success), KR + cap blocked, US + fx unavailable. Asserts `OrderPreviewLine` fields including `guard.execution_allowed=False` and `guard.approval_required=True`.
- `tests/test_watch_order_intent_service.py` (real DB, async) — INSERT happy paths, `IntegrityError` from partial unique index returns `dedupe_hit`, `failed` rows do not block subsequent `previewed`. Mocks the FX service at the boundary only.
- `tests/test_watch_alerts.py` (existing, extended) — adds policy validation cases at `add_watch`; legacy payload reads still work.
- `tests/test_watch_scanner.py` (existing, extended) — every branch: notify_only unchanged, previewed deletes watch, failed keeps watch, dedupe_hit deletes watch and includes ledger reference. Asserts the additive `intents` block in the n8n payload via the existing `OpenClawClient` test seam.
- `tests/test_mcp_watch_alerts.py` (existing, extended) — adds policy kwargs; `notify_only` calls byte-identical to before.
- `tests/test_watch_order_intent_ledger_router.py` and `tests/test_mcp_watch_order_intent_ledger.py` — 1–2 cases each (list + get, with one not-found case).

Acceptance-criteria mapping (issue §Acceptance criteria):

| Criterion | Covered by |
|---|---|
| Existing watch alert-only tests continue to pass | `tests/test_watch_alerts.py`, `tests/test_watch_scanner.py`, `tests/test_mcp_watch_alerts.py` (notify_only paths) |
| A watch can be configured to produce an approval-required mock order intent preview | `tests/test_watch_order_intent_service.py` happy paths + `test_watch_scanner.py` previewed branch |
| Duplicate trigger / idempotency behavior covered | `tests/test_watch_order_intent_service.py` partial-unique-index dedupe case |
| Live account actions remain approval-only / non-executing | Code-level: only `kis_mock` matches the gate; live broker code path is absent. Asserted by `test_watch_intent_policy.py` (rejects `kis_live` if any future caller passes it) and by absence of broker submit calls in `watch_order_intent_service.py`. |

## 11. Operator runbook (sketch)

`docs/runbooks/watch-order-intent-ledger.md` covers:

- Adding a watch with policy via MCP (`manage_watch_alerts add ... action=create_order_intent side=buy quantity=1 max_notional_krw=1500000`).
- Reading recent ledger rows (`watch_order_intent_ledger_list_recent`).
- Investigating a `failed` row by `blocked_by` value.
- The "watch kept on fail, deleted on previewed/dedupe_hit" mental model.
- Reminder that the ledger is service-only-write; direct SQL `INSERT/UPDATE/DELETE` is forbidden (mirrors the Alpaca paper ledger runbook).

## 12. Open follow-ups (deferred)

- `cooldown_minutes`, intraday re-entry, price-bucket dedupe.
- `update` action on `manage_watch_alerts`.
- Widening matching rule for `crypto + alpaca_paper` and `kis_live` (after broker submit lands).
- Additional `price_policy` values (`last_close`, anchored ranges).
- UI surface for the ledger (depends on Decision Desk roadmap).
