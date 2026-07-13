# ROB-861 — Buying-Power Pre-Submit Gate Design

## Goal

Prevent a Toss live buy proposal from reaching the broker when click-time
buying power is already known to be below the order's required cash, tell the
operator the exact available/required/shortfall amounts in Telegram, and keep
the proposal retryable after a deposit. Add a non-blocking create-time advisory
that compares all pending buys for the same account and currency with current
buying power.

## Scope

This change implements the live buying-power reader for `toss_live` only.
`kis_live` and `upbit` keep their existing submit behavior because their
orderable-cash contracts differ by market and order type. The gate exposes a
broker-agnostic injected reader/reservation boundary so those brokers can be
added later without changing the revalidation state flow.

No real broker calls are allowed in tests. The existing `service.py` and
`state_machine.py` files remain unchanged: `revalidating -> needs_reconfirm`
and `needs_reconfirm -> pending_approval` are already legal, and Telegram's
re-approval path already performs the latter transition.

## Architecture

### Buying-power boundary

Add a focused order-proposal buying-power module that owns:

- normalized `Decimal` results for available, required, and shortfall amounts;
- market-to-currency mapping (`equity_kr -> KRW`, `equity_us -> USD`);
- a default broker reader that calls Toss
  `GET /api/v1/buying-power?currency=...` and returns unavailable for other
  account modes;
- a short process-local cache keyed by account mode, broker account, and
  currency;
- per-key single-flight reads and a one-second TTL;
- temporary subtraction of successfully submitted or ambiguous predecessor
  orders from the cached amount, so rapid multi-rung or multi-proposal approval
  does not reuse the same unadjusted snapshot.

The existing shared Toss limiter continues to serialize provider requests. The
short cache avoids repeated account resolution and buying-power reads during a
burst while expiring quickly enough for a deposit to become visible on retry.
Failures are not cached.

### Click-time gate

`revalidation.py` runs the gate only when all of these are true:

- action is `place`;
- rung side is `buy`;
- rung is a limit order with a positive quantity and limit price;
- the preview succeeded and its normalized quantity/price still match the
  approved rung.

Required cash uses the preview's `estimated_value + fee` when both values are
usable. It falls back to `quantity * limit_price` when the preview does not
expose costs, preserving injected-test and future-broker compatibility. The
gate then reads buying power through the injected boundary.

If the read succeeds and available cash is below required cash, the rung moves
from `revalidating` to `needs_reconfirm`; the result includes:

```json
{
  "reason": "insufficient_buying_power",
  "currency": "KRW",
  "available": "500000",
  "required": "1070300",
  "shortfall": "570300"
}
```

No submit function is invoked. If the read raises, returns unavailable, or is
not supported for that account mode, the gate deliberately fails open and the
existing broker submit path continues. This is an operator UX pre-check, not an
authoritative safety control; the broker remains the final source of truth.

After an accepted submit, the required amount is reserved in the short-lived
cache. Ambiguous submits are also reserved conservatively until TTL expiry,
because the broker may have accepted them. Explicit broker rejection does not
reserve cash.

### Loss-cut ordering

ROB-864's first click remains read-only and only builds loss-cut evidence. Its
second confirmation enters the normal `revalidate_and_submit` flow. Because
the buying-power gate lives after the fresh preview inside that flow, any
future buy-side two-step flow would run confirmation first and the gate second;
existing loss-cut proposals are sells and therefore skip the gate.

### Telegram rendering

A buying-power `needs_reconfirm` outcome is not rendered as a price/quantity
diff. `approval_message.py` recognizes the reason and emits the localized line:

`매수가능 Z원 / 필요 Y원 → 부족 X원 — 입금 후 재승인`

USD amounts use dollar formatting with two decimal places; KRW amounts use
whole won with thousands separators. The callback keeps the established fresh
nonce and new approval-message behavior, so the same proposal can be approved
again after a deposit. Multi-rung outcomes render every shortage rather than
only the first.

### Create-time advisory

After proposal persistence succeeds, the MCP create path performs a best-effort
read-only advisory in a fresh session. For each buy currency in the new
proposal, it sums `quantity * limit_price` for all `pending_approval` buy rungs
on the same `account_mode` and `broker_account_id`, including the new proposal.
Market orders without a stored limit price are omitted and reported in the
advisory metadata.

For Toss, the response adds a structured `buying_power_advisory` list with
`status` (`sufficient`, `insufficient`, or `unavailable`), currency, buying
power, pending required cash, shortfall, and an optional human-readable
warning. Insufficient entries are also copied into the top-level `warnings`
list for MCP callers. This advisory never rolls back or blocks the already
created proposal. Non-Toss account modes do not add an advisory until their
readers are implemented.

## Error Handling

- Buying-power lookup failure at submit time: log, skip the gate, continue to
  the existing broker path.
- Buying-power advisory/query failure at create time: log, return the successful
  create response with an `unavailable` advisory when possible.
- Telegram edit/send failure: retain the existing commit-before-notify and
  best-effort behavior.
- Invalid or missing preview cost fields: fall back to rung notional; never
  reject solely because cost metadata is malformed.
- Explicit insufficient buying power: never call broker POST and never use a
  terminal rung state.

## Testing

Use strict red-green-refactor cycles and injected fakes only.

- Insufficient Toss buying power: submit fake called zero times, rung becomes
  `needs_reconfirm`, outcome carries exact Z/Y/X values.
- Sufficient buying power: existing submit and classification behavior remains
  unchanged.
- Sell rung: no buying-power read and unchanged submit path.
- Reader failure/unavailable: fail open and submit through the existing path.
- Deposit retry: first click yields `needs_reconfirm`; second approval reads a
  fresh higher value and succeeds.
- Rapid approvals/multiple rungs: short cache and reservation prevent reuse of
  the same available amount.
- Telegram: KRW and USD shortfall copy renders exactly and buttons remain
  retryable.
- Create advisory: includes the new proposal plus existing same-account
  `pending_approval` buys, reports sufficient/insufficient status, and never
  blocks creation when the reader fails.
- ROB-864 regression: loss-cut first click still makes zero submit attempts and
  sell-side second click skips the buying-power gate.

Run the focused order-proposal suite, then the repository lint gate:

```bash
uv run pytest tests/services/order_proposals/ -q
make lint
```

Targeted MCP create tests are run as part of implementation verification even
though they live outside the requested focused directory.

## PR Coordination

The PR body records that Toss is implemented while KIS/Upbit are deferred behind
the broker-agnostic hook. It also records that no `service.py` or
`state_machine.py` changes were needed, avoiding the ROB-862 conflict and any
merge-order dependency.
