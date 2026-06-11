# ROB-514 Watch Execution Plan Alert Context Design

- Issue: ROB-514
- Date: 2026-06-11
- Status: approved for implementation planning

## Problem

Watch alerts already preserve the free-text `rationale`, and prior work added
`max_action`, `trigger_checklist`, and `watch_recommendation`. In practice, the
operator still has to re-read free text when a watch fires because the Hermes
payload does not carry a structured buy plan or checklist, and the MCP contract
does not clearly tell callers how to provide those fields.

Related completed work:

- ROB-402 consumes `max_action` for `auto_execute_mock`.
- ROB-403 made `max_action` the structured watch action contract.
- ROB-337 stores `watch_recommendation`.
- ROB-500 sends `watch_recommendation` as Hermes `price_guidance`.
- ROB-458/ROB-498 improved item input contracts and structured report fields.

## Goals

- Keep `max_action` as the canonical stored execution-plan JSON. Do not add a
  duplicate `planned_action` DB column.
- Add a Hermes-facing `planned_action` projection derived from alert
  `max_action`.
- Include the watch `trigger_checklist` in trigger and validity-review Hermes
  payloads.
- Expose `trigger_checklist: string[]` and the supported watch execution-plan
  keys in MCP descriptions, error notes, README, and runbook docs.
- Add an opt-in `investment_report_activate_watch(..., attach_recommendation=True)`
  path that computes and persists `watch_recommendation` if possible.

## Non-Goals

- No live or mock order submission changes.
- No DB migration.
- No new scheduler or recurring activation behavior.
- No Hermes renderer implementation in this repo.
- No replacement of existing `max_action.quantity/notional/limit_price` behavior
  used by ROB-402.

## Design

### Stored Contract

`investment_report_items.max_action` and `investment_watch_alerts.max_action`
remain the only persisted execution-plan fields. `planned_action` is a derived
payload shape for notifications and future UI/order-preview prefill.

Recommended `max_action` input for a KR buy watch:

```json
{
  "side": "buy",
  "quantity": "1",
  "amount_krw": "980000",
  "limit_price": "975000",
  "limit_price_hint": "975000",
  "ladder_level": "1"
}
```

`quantity` remains the ROB-402 key. `qty` is accepted as a convenience alias for
the notification projection only. `amount_krw`, `limit_price_hint`, and
`ladder_level` are additive keys. `MaxActionPayload.extra="allow"` keeps legacy
keys such as `notional_usd`.

### Hermes Payload

Add optional fields to `ReviewTriggerPayload`:

```json
{
  "planned_action": {
    "side": "buy",
    "qty": "1",
    "amount_krw": "980000",
    "limit_price_hint": "975000",
    "ladder_level": "1"
  },
  "trigger_checklist": [
    "Check latest quote spread",
    "Confirm thesis still valid"
  ]
}
```

Rules:

- `planned_action` is `null` if `max_action` is empty or malformed.
- The projection never fabricates missing amount, quantity, or price fields.
- `limit_price_hint` uses `max_action.limit_price_hint` first, then
  `max_action.limit_price`.
- `qty` uses `max_action.qty` first, then `max_action.quantity`.
- `trigger_checklist` is always a list of strings on new payloads.
- Missing or malformed optional notification context must not block alert
  delivery.

### Activation Recommendation Attachment

`investment_report_activate_watch` gets `attach_recommendation: bool = False`.

When false, behavior is unchanged. When true:

- If the source item already has `watch_recommendation`, activation skips the
  compute step and reports `recommendation_attached=True`.
- If missing, the handler computes the same deterministic ROB-337
  recommendation used by `investment_watch_recommend(commit=True)` and persists
  it to the source item.
- Compute or validation failures are fail-open: the watch still activates, and
  the response includes `recommendation_attached=False` plus a short
  `recommendation_attach_error`.
- The response model adds optional `recommendation_attached` and
  `recommendation_attach_error` fields.

### Validation

`IngestReportItem.trigger_checklist` narrows from `list[Any]` to `list[str]`.
Read-side response models can remain permissive if needed for historical rows,
but new MCP input should reject non-string checklist entries.

`MaxActionPayload` adds typed optional fields:

- `amount_krw: Decimal | None`
- `limit_price_hint: Decimal | None`
- `ladder_level: str | None`

The existing quantity/notional XOR remains unchanged. A caller may provide
`quantity + amount_krw`; `amount_krw` does not count as the XOR notional field.

### Docs and Tool Descriptions

Update:

- `CREATE_DESCRIPTION`
- `ADD_ITEMS_DESCRIPTION`
- `_validate_report_items` error notes
- `app/mcp_server/README.md`
- `docs/runbooks/watch-trigger-hermes-payload.md`

Docs must state that watch items can pass:

- `trigger_checklist: string[]`
- `max_action.side`
- `max_action.quantity` or `max_action.notional`
- optional `max_action.amount_krw`
- optional `max_action.limit_price` and `max_action.limit_price_hint`
- optional `max_action.ladder_level`

## Data Flow

1. Caller creates or appends a watch item with `trigger_checklist` and
   `max_action`.
2. `investment_report_activate_watch` copies both fields into
   `investment_watch_alerts`.
3. Scanner creates or reuses an event row.
4. Scanner builds Hermes `ReviewTriggerPayload` with:
   - existing trigger identity
   - existing `price_guidance`
   - new `planned_action`
   - new `trigger_checklist`
5. Watch validity review uses the same notification projection when it sends a
   review-required payload.

## Testing

Add focused tests for:

- `trigger_checklist` rejects non-string inputs.
- `MaxActionPayload` accepts `quantity + amount_krw + limit_price_hint +
  ladder_level`.
- `planned_action_from_max_action` maps canonical and alias keys.
- `planned_action_from_max_action` returns `None` for empty or malformed data.
- `ReviewTriggerPayload` accepts the new optional fields and still rejects
  unknown extras.
- Scanner payload includes planned action and checklist.
- Validity-review payload includes planned action and checklist.
- `activate_watch(attach_recommendation=True)` persists a recommendation when
  market data is available.
- `attach_recommendation=True` fails open when recommendation compute fails.
- MCP docs/error text mention the new input contract.

## Risk

This is not a live-trading mutation and does not alter DB schema. The riskiest
change is the optional market-data fetch during activation; it is default-off
and fail-open. If implementation scope expands to order submission, account
permission changes, or DB schema migration, tag the Linear work with
`high_risk_change` and `needs_stronger_model_review`.

## Self Review

- No DB migration is required because all persisted data already lives in JSONB.
- The design does not duplicate ROB-402 `max_action`; it exposes a derived
  Hermes-facing projection only.
- The recommendation attachment is opt-in and cannot block activation.
- Tests cover both new notification context and input-contract visibility.
