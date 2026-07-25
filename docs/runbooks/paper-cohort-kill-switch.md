# Paper cohort kill-switch runbook

Use this path for a ROB-849 cohort that must stop immediately. The kill switch
is operational authority; it does not force an illegal ROB-848 state-machine
transition.

## Preconditions

- Start the bearer-authenticated `paper_execution` MCP profile with
  `PAPER_EXECUTION_ENABLED=true`.
- Set `PAPER_VALIDATION_AUTHENTICATED_ACTOR_ID` to a server-owned identity whose
  `PAPER_VALIDATION_ACTOR_ROLES` entry is `operator` or `system`.
- Never put credentials, account identifiers, or tokens in `reason_text`.
  Sensitive key/value patterns are redacted before persistence, and reason
  text/validation evidence are never returned by the MCP result.

## Stop sequence

1. Call `paper_cohort_kill_switch` with `cohort_id`, a stable idempotency key,
   `reason_code`, and a concise `reason_text`.
2. Confirm a `fence_id` and `status=fenced` (or the exact replay
   `status=already_fenced`). The fence commits before any broker cleanup and
   survives process/provider failure.
3. Disable `PAPER_COHORT_ENABLED` to remove future cohort schedules. Re-enabling
   it cannot bypass the persisted terminal fence.
4. If `cleanup_status=pending`, call the same request with the same idempotency
   key again. Recovery first resolves/links prepared native orders without a
   fresh POST; cleanup then cancels open orders and closes only persisted,
   proven fills.
5. Treat `manual_required` as an operator escalation. Do not submit an ad-hoc
   close from this runbook: reconcile the referenced native ledger/link first.
6. Only after cleanup is `complete` (or an accepted manual escalation exists),
   optionally disable `PAPER_EXECUTION_ENABLED`. Disabling it earlier removes
   the retry tool.

Any changed payload under an existing fence is an idempotency conflict. Unknown
native state, unavailable native lookup, an unconfirmed cancel, or missing fill
quantity remains `pending`; none is interpreted as proof that exposure is flat.
