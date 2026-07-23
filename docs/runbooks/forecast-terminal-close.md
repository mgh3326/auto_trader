# Terminal-close forecast runbook (ROB-1038)

This runbook is for forecasts whose event is the official regular-session
close on the review date. It is separate from the legacy `price_target`
contract:

- `price_target`: any daily bar in the window touches the target (`high`/`low`).
- `terminal_close`: exactly one final review-date daily `close` is compared with
  the target. No window extreme, `adj_close`, or extended-hours price is used.

Never reinterpret an existing `price_target` row as terminal-close merely
because its prose mentions a closing price. Preserve that row and create a new
typed `terminal_close` forecast with a new `forecast_id`.

## Versioned event contract

`terminal-close-v1-up-gte-down-lt` defines complementary events:

```text
up   = review close >= effective target
down = review close <  effective target
```

Equality belongs only to `up`. `direction="at_or_below"` is not a valid
terminal direction.

The target may be preregistered before its review date in a deliberately
unresolvable state:

```json
{
  "kind": "terminal_close",
  "direction": "up",
  "target_price": 30.56,
  "outcome_rule_version": "terminal-close-v1-up-gte-down-lt",
  "price_adjustment_policy": "unverified_fail_closed"
}
```

`forecast_resolve` leaves this row open with
`status="requires_adjustment_evidence"`.

## Corporate-action evidence

The daily store does not have a first-class corporate-action ledger. Its close
basis is source-dependent:

| Stored source | `close` basis | Session contract |
|---|---|---|
| `kis` | provider-adjusted | regular daily |
| `toss`, `toss_fallback` | provider-adjusted | regular daily |
| `yahoo`, `yahoo_fallback` | raw (`auto_adjust=False`) | closed regular daily |

The US `adj_close` column is not a complete scheduled source contract, so the
terminal resolver never uses it. It also does not infer splits from price
jumps.

After the review session closes, verify corporate actions through the review
date using a durable source. Update the still-open typed terminal forecast with:

```json
{
  "kind": "terminal_close",
  "direction": "up",
  "target_price": 30.56,
  "outcome_rule_version": "terminal-close-v1-up-gte-down-lt",
  "price_adjustment_policy": "explicit-factor-v1",
  "target_to_close_factor": 1.0,
  "adjustment_provenance": {
    "source": "name of the corporate-action authority",
    "verified_through_date": "YYYY-MM-DD",
    "evidence_ref": "durable URL or artifact reference"
  }
}
```

The verified-through date must equal the forecast review date.
`target_to_close_factor` maps the originally stored target into the selected
daily row's `close` basis:

```text
effective_target = target_price * target_to_close_factor
```

Use `1.0` only when the evidence proves that no conversion is needed. For a
split or other action, calculate the factor from the authoritative action
record. If the mapping cannot be proved, keep
`price_adjustment_policy="unverified_fail_closed"` and do not resolve.

## Safe resolve sequence

1. Wait until the review-date regular session is final.
2. Retrieve and retain corporate-action evidence through that date.
3. For a new terminal forecast, update its open typed target to
   `explicit-factor-v1`. Do not update a legacy touch forecast.
4. Run `forecast_resolve(forecast_id=..., dry_run=true)`.
5. Confirm the preview contains:
   - `target_kind="terminal_close"`
   - `outcome_rule_version="terminal-close-v1-up-gte-down-lt"`
   - the expected comparison operator
   - original/effective target and factor
   - adjustment provenance
   - `source_date`, `source_timestamp`, `source_price`, `source_price_field`
   - `source`, `source_partition`, and `source_price_basis`
   - `regular_session_only=true` and `adj_close_used=false`
6. Confirm exactly one review-date candle was selected and the source price is
   the official regular-session close.
7. Only after that review, call the same forecast with `dry_run=false` to persist
   its outcome and Brier score.

The resolver fails closed and leaves the forecast open for a missing review
candle, stale-only data, multiple review-date candles, an untrusted/extended
source, a non-final session, an invalid close, or missing adjustment evidence.
Do not replace one of those failures with a free-form manual terminal outcome.

This workflow writes only the selected forecast when `dry_run=false`. It does
not create or mutate broker orders, watches, proposals, or order intents.

## Follow-up

A separate issue should add a first-class corporate-action event/factor store
that can automatically prove target-to-close basis conversion. Until that
exists, `explicit-factor-v1` is intentionally operator-verified and
fail-closed.
