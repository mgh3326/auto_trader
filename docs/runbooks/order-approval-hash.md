# Runbook: Order Approval Hash & Intent Guard (ROB-653)

## Overview
This system prevents double-send incidents and ensures order content integrity between the time an order is previewed (dry-run) and when it is executed (live send). It applies to both `kis_live` (KR/US equities) and `upbit_live` (Crypto).

---

## 1. How the Approval Hash Guard Works

1. **Preview/Dry Run (`dry_run=True`)**:
   - The system generates a canonical payload matching the order parameters (symbol, side, quantity, price, etc.).
   - It hashes this payload into a secure `approval_hash` token with a **5-minute (300 seconds) Time-To-Live (TTL)**.
   - It also generates a content-based `idempotency_key`.
   - The MCP tool returns `approval_hash`, `approval_expires_at`, and `idempotency_key`.

2. **Live Order Send (`dry_run=False`)**:
   - The operator must pass the `approval_hash` parameter.
   - The system verifies the token's validity, checks that the TTL hasn't expired, and compares the placed order parameters against the previewed canonical payload.
   - If they match, execution proceeds. If not, it fails closed.

---

## 2. Enforcement Modes (`ORDER_APPROVAL_HASH_MODE`)
The rollout level of the approval hash guard is controlled by the environment variable `ORDER_APPROVAL_HASH_MODE` (or `settings.order_approval_hash_mode` in Python config).

- **`off`**: Disables the guard completely.
- **`optional`** (Default): Verifies the hash **if** one is provided. If no hash is provided, it allows execution without warnings.
- **`warn`**: Same as `optional`, but logs a warning when a live order is sent without a hash.
- **`required`**: Blocks any live order execution that does not provide a valid, matching, and unexpired `approval_hash`.

To adjust, update your `.env` or container environment:
```bash
ORDER_APPROVAL_HASH_MODE=required
```

---

## 3. Local Idempotency: KIS Pre-Send Intent Reservation
Korea Investment & Securities (KIS) API does not support broker-side idempotency keys. To prevent double-send/replay errors locally:

1. Prior to making the HTTP request to KIS, the system attempts to insert a record into the `review.order_send_intents` table.
2. The table has a `UNIQUE(account_scope, idempotency_key)` constraint.
3. If a duplicate order is sent on the same trading day (which shares the same salt and content, resulting in the same `idempotency_key`), the database insert raises an `IntegrityError`.
4. The system catches this, aborts the send, and raises a `DuplicateOrderIntent` error.

### Recovery / Troubleshooting
- **Replay Blocked**: If a KIS send fails or times out, the `idempotency_key` remains reserved for that trading day. Re-sending will be blocked.
- **Action**: Do **NOT** delete rows from `review.order_send_intents` during live trading. Reconcile the order status using `kis_live_reconcile_orders` to check if it was accepted by the broker.
- **Next Day**: The idempotency key includes a date-based salt (KST for KR, ET for US). On the next calendar day, the same order will resolve to a different key and is allowed.

---

## 4. Crypto Idempotency: Upbit Content Identifier
Upbit supports broker-side deduplication via the `identifier` query/body parameter:

- When executing Upbit live orders, the content-based `idempotency_key` is passed directly as the Upbit `identifier`.
- Upbit will reject any duplicate POST requests with the same identifier, providing broker-side protection.

---

## 5. Audit Ledgers
The fields `approval_hash` (the digest) and `idempotency_key` are persisted on:
- `review.kis_live_order_ledger`
- `review.live_order_ledger`

Reconciliation routines are untouched and operate independently from the idempotency records.
