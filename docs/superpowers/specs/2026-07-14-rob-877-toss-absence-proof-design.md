# ROB-877 Toss Absence-Proof Recovery Design

## Goal

Allow an explicit operator void to converge legacy Toss `unverified` order
proposal rungs to `voided_local_stale` only when two independent sources prove
that Toss never accepted the order. Preserve fail-closed behavior for every
incomplete, failed, or ambiguous lookup.

## Source-of-Truth Finding

The Toss `GET /api/v1/orders` list item schema (`Order`) does not contain
`clientOrderId`; only order creation responses expose it. The current
`requires_client_id_field` guard therefore cannot prove absence whenever the
list contains an unrelated order whose parsed `client_order_id` is `None`.

The same OpenAPI document is internally inconsistent about CLOSED history:
the endpoint operation, parameters, response example, and pagination contract
support `status=CLOSED`, while the `PaginatedOrderResponse` component
description says CLOSED currently returns `400 closed-not-supported`. The
implementation follows the endpoint operation contract and records the
contradiction in ROB-877 and the PR. Tests remain fully mocked; this session
does not access production.

## Architecture

### Evidence composition

Keep `OrderProposalsService.void_proposal` unchanged. It already requires:

1. conclusive broker evidence with outcome `absent`; and
2. zero accepted/non-rejected Toss ledger rows matching the rung's
   `client_order_id` or broker order ID.

Only after both checks does the service write `voided_local_stale`. The
existing evidence summary is retained in `void_reason`, so the gateway's
lookup scope will carry the scan window and combination-match result while the
service continues to append `toss_live_order_ledger rows=0`.

### Scan window

For each rung, define an inclusive instant window:

- start: `rung.created_at - 24 hours`;
- end: `max(group.valid_until, rung.updated_at) + 24 hours`, ignoring
  `valid_until` only when it is null.

This covers proposals created before the actual approval/submit attempt. The
MCP adapter already receives the proposal group, so it passes `valid_until` to
`fetch_operator_void_evidence` without changing `service.py`.

The broker gateway scans OPEN and every CLOSED page once over the union of all
rung windows. Toss `from` and `to` are inclusive KST calendar dates derived
after timezone conversion. Both OPEN and CLOSED receive the same date range;
CLOSED remains bounded by the existing 20-page cap.

Each rung is evaluated against its own instant window after the shared scan.
An order exactly at either boundary is inside the window. A matching order
outside the boundary is not evidence for that rung.

### Match semantics

Existing exact broker-order-ID and `clientOrderId` matches remain positive
evidence when those identifiers are available. When list items omit
`clientOrderId`, the gateway falls back to the required tuple:

- normalized symbol;
- case-normalized side;
- quantity parsed as finite `Decimal`;
- price parsed as finite `Decimal`, or `None` for market orders.

Decimal values are compared numerically, so `1`, `1.0`, and `1.00000000` are
equivalent. String equality is never used for quantity or price.

If the tuple matches and `orderedAt` lies inside the rung window, evidence is
`found` with the broker order ID and broker state. If no tuple matches after a
complete scan, evidence is `absent`. A malformed or timezone-naive
`orderedAt` on an otherwise matching tuple is `unknown`, because the window
membership cannot be proven.

## Fail-Closed Rules

The gateway returns `unknown` for:

- any OPEN or CLOSED request exception, including timeouts;
- `hasNext=true` without a cursor;
- repeated cursors;
- reaching the CLOSED page cap while another page remains;
- malformed temporal or numeric data that prevents classifying a potential
  tuple match.

The absence outcome is never produced from a partial scan. KIS and Upbit
branches are unchanged.

## Audit Evidence

Each Toss evidence scope includes:

- the union scan's KST `from` and `to` dates;
- the rung's inclusive instant window;
- CLOSED page count and completeness;
- `combination_matches=0` for successful absence, or the matching broker
  order's state/ID through the existing found-order rejection.

On success, `void_reason` therefore contains the operator reason, ledger
zero-row proof, scan window, and zero combination-match result.

## Tests

Add or update mocked tests that fix these contracts:

- missing `clientOrderId` plus zero tuple matches produces `absent`;
- numerically equivalent Decimal quantity/price representations match;
- a tuple match produces `found` and exposes broker state/order ID;
- lower and upper instant boundaries are inclusive, and values just outside
  are excluded;
- `created_at`, `valid_until`, and `updated_at` produce the expected KST
  `from`/`to` dates;
- timeout, invalid pagination, and CLOSED page-cap exhaustion stay `unknown`;
- an accepted Toss ledger row still blocks void;
- successful composite absence writes the complete evidence summary;
- existing KIS, Upbit, and Toss 4xx-to-rejected tests remain green.

Verification uses:

```bash
uv run pytest tests/services/order_proposals/ -q
make lint
```

After merge and deployment, an operator must retry the 13 legacy voids and
confirm convergence to `voided_local_stale`; that production check is the
final acceptance criterion and is outside this session.
