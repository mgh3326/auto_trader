# ROB-199 Naver discussion signal PoC

## Goal

Evaluate Naver Securities community/discussion ranking data as a bounded, aggregate-only activity signal for `/invest/stocks/:market/:symbol`.

This PoC is intentionally fixture-backed. It does not fetch Naver from the product request path, does not clone community posts, does not schedule collectors, does not write production DB rows, and does not expose broker/order/watch mutations.

## Signal source

Endpoint observed in ROB-197/199 one-off probes:

| Surface | Probe URL | Result | Notes |
| --- | --- | --- | --- |
| Discussion rankings | `https://stock.naver.com/api/community/discussion/rankings?size=5` | 200 JSON | Contains rank, item codes, post/reaction counts. Use only aggregate metrics; no post text, titles, author ids. |

## Aggregate-only contract

**Allowed fields (aggregate signal metrics):**

- `activityRank` — numeric rank (lower = more active)
- `postCount` — total post count in window
- `commentCount` — total comment count in window
- `reactionCount` — total reaction count in window
- `momentum` — `rising | flat | falling | unknown`

**No-go fields (never stored, rendered, or transmitted):**

- Public discussion post text
- Post title or body
- Author nickname, user ID, or any identifier
- Comment text
- Raw reactions by user
- Any scheduled collector without explicit approval

## Safety boundaries (ROB-199)

- `liveFetchEnabled` is always `False` in this PoC
- `StockDetailDiscussionSignal.enforce_aggregate_only_contract` validator rejects any attempt to expose UGC field labels
- No Naver scheduled collector is deployed; no production DB writes
- Crypto market returns `null` (Naver KR/US coverage only)
- US market returns `status=no_go_pending_review` until endpoint/rate-limit contract is reviewed

## Integration point

`StockDetailDiscussionSignal` is surfaced in `StockDetailResponse.discussionSignal`. The field is `null` for crypto and returns a fixture-backed PoC object for KR and US markets.

The service wires `build_naver_discussion_signal_poc` as the default `discussion_signal_provider` in `build_stock_detail()`.
