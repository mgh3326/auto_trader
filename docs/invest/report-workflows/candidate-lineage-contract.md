# Candidate lineage contract

Staged KR reports must not silently drop or rename candidates between sessions.
This contract defines the public vocabulary for carrying candidate decisions from
pre-market planning through NXT and regular-session confirmation.

## Candidate identity

A candidate should have a stable key within a report day and account scope:

```text
<market>:<account_scope>:<kst_date>:<symbol_or_asset_id>:<intent>
```

For UI display, a shorter `client_item_key` may be used, but it should remain
stable across re-composition attempts for the same bundle/report key.

Recommended identity fields:

- `symbol` or target asset identifier;
- `market`;
- `account_scope`;
- `intent` such as `buy_review`, `sell_review`, `risk_review`,
  `trend_recovery_review`, or `rebalance_review`;
- `lineage_source_stage` and prior candidate key when the item was carried from
  an earlier stage.

## Status vocabulary

| Status | Meaning | Typical next transition |
|---|---|---|
| `seeded` | Candidate introduced before sufficient live-session confirmation. | `confirmed`, `downgraded`, `rejected`, `deferred` |
| `confirmed` | Later session evidence supports keeping the candidate active. | Remain active or become final report item |
| `downgraded` | Evidence weakened the candidate, but it remains worth monitoring. | `deferred`, `rejected`, or risk watch |
| `rejected` | Evidence invalidated the candidate for this report cycle. | Terminal for the cycle |
| `deferred` | Evidence is missing/stale/conflicting, so no action-oriented review should be made. | Re-evaluate in a later session |
| `new_session_candidate` | Candidate first appeared in the current live session rather than in the earlier plan. | `confirmed`, `downgraded`, `deferred` |
| `risk_watch` | Candidate is primarily a risk-management item, not a buy/sell opportunity. | Remain watch or become sell/risk review |

A candidate may be absent from the final recommendation list, but it should still
appear in lineage or limitations if it was material in an earlier stage.

## Transition rules

1. `seeded` candidates from `pre` must be accounted for in `nxt` or `regular`.
2. `nxt` candidates carried into `regular` must retain their prior status and
   explain any change.
3. A `rejected` candidate must include at least one invalidation reason and a
   citation or missing-data explanation.
4. A `deferred` candidate must include the missing/stale/conflicting evidence
   that prevented a decision.
5. A `new_session_candidate` must explain why it was not visible in the earlier
   stage and must not erase earlier carried candidates.
6. `risk_watch` items must not be conflated with buy/sell conviction.
7. No lineage transition may imply an order was placed, previewed, or approved.

## Evidence requirements

Every material candidate transition should cite one or more of:

- frozen snapshot UUIDs or stable JSON paths;
- prior stage artifact IDs;
- report context coverage/freshness summaries;
- explicit unavailable-source records.

Do not cite operator-private artifacts such as browser profile paths, cookies,
raw generated private reports, or credential-bearing MCP config.

## Example transition record

```json
{
  "candidate_key": "kr:kis_live:YYYY-MM-DD:005930:buy_review",
  "symbol": "005930",
  "intent": "buy_review",
  "previous_stage": "pre",
  "current_stage": "nxt",
  "previous_status": "seeded",
  "current_status": "downgraded",
  "transition_reason": "NXT evidence was unavailable, so the pre-market thesis was not confirmed.",
  "confirmation_needed": [
    "regular-session price/volume confirmation",
    "fresh screener or orderbook snapshot"
  ],
  "invalidation_triggers": [
    "break below cited support level",
    "fresh negative account/news conflict"
  ],
  "cited_snapshots": [
    "snapshot_uuid_or_stable_path"
  ],
  "missing_data": [
    "nxt_orderbook unavailable"
  ],
  "advisory_only": true
}
```

## Relationship to report items

Final report items should reference lineage rather than recomputing it in prose.
Suggested fields when schemas allow:

- `lineage_status`
- `lineage_source_stage`
- `previous_candidate_key`
- `transition_reason`
- `confirmation_needed`
- `invalidation_triggers`
- `cited_snapshot_uuids`
- `missing_data`

If the current schema cannot store these fields directly, keep them in the stage
artifact payload or report metadata until a follow-up schema/UI issue promotes
them to first-class fields.
