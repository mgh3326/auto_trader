# Mechanical loss-cut (ROB-1460)

Sells a lot automatically when its price falls through the stop recorded for it.
Ships in two stages: **shadow** records what it would sell and sends nothing, and
**live** is present in code but off until the operator turns it on.

Everything is off by default. The contract is in
`docs/agent-contracts/broker-and-ledger-contracts.md`, and the decision record is
`docs/contracts/policy-freeze-deviation-20261102-mechanical-loss-cut.md`.

## What it does

A **lot** is one account mode + market + symbol position.

1. A buy the broker accepted writes an **exit plan** version to
   `review.lot_exit_plans`: stop price, why that stop, average cost, and an
   optional averaging-down ladder. A later buy on the same lot writes the next
   version and records how it compared with the plan (`add_verdict`). That verdict
   is a record only and blocks no buy.
2. A lot with no plan gets one from the policy defaults, marked `backfilled=true`.
3. The job reads held lots and daily closes from the database. A lot is cut only
   when **all** of these hold:
   - the last completed close is current, and the series is not frozen;
   - the close is below average cost;
   - the close is below the plan's stop;
   - unrealized P&L is at or below `trigger.unrealized_floor_pct`;
   - the close is not above the close `rising_lookback_sessions` ago;
   - the `trigger.family` condition holds (see [Thresholds](#set-thresholds)).
4. Each cut is one row in `review.mechanical_loss_cut_decisions`, once per plan
   version, stop and unbroken below-stop run.

Parking instruments are never cut. Protected quantity and quantity on open sells
are subtracted first.

## Enable shadow

The desk asks the operator before running the migration.

1. Apply the migration:

   ```bash
   uv run alembic upgrade head
   ```

2. Set these in the runtime configuration and restart:

   ```bash
   LOT_EXIT_PLAN_WRITE_ENABLED=true
   MECHANICAL_LOSS_CUT_SHADOW_ENABLED=true
   ```

3. Do a dry run, which writes nothing, then a real run:

   ```bash
   uv run python -m scripts.mechanical_loss_cut evaluate
   uv run python -m scripts.mechanical_loss_cut evaluate --commit
   ```

### Registering a schedule

This PR registers none, and hard rule 6 needs explicit approval for one. The task
is `mechanical_loss_cut.evaluate`. To evaluate each market after its close, ask for
one run per day:

| Market | Suggested time | Why |
|---|---|---|
| KR | 16:10 KST, weekdays | Needs that day's `kr_candles_1d` row. |
| US | 07:10 KST, Tuesday to Saturday | Same, for `us_candles_1d`. |
| Crypto | 09:30 KST, daily | The UTC daily bar closes at 09:00 KST. |

A run covers every market. A market whose latest session is missing from the
candles skips as `price_stale`, so extra runs are harmless.

## Read shadow results

One session's summary:

```bash
uv run python -m scripts.mechanical_loss_cut summary --session 2026-11-02
```

`planned` and `backfilled` are always counted apart. `over_per_order_cap` counts
would-sells that the live path would send to a card instead.

The rows:

```sql
SELECT session, market, symbol, plan_version, backfilled, trigger_family,
       close_price, stop_price, unrealized_pct,
       intended_quantity, intended_limit_price, within_per_order_cap
FROM review.mechanical_loss_cut_decisions
WHERE mode = 'shadow'
ORDER BY session DESC, id DESC;
```

The plan history of one lot:

```sql
SELECT version, event, stop_price, stop_basis, stop_reason, average_cost,
       adds_used, max_adds, add_verdict, backfilled, created_at
FROM review.lot_exit_plans
WHERE account_mode = 'kis_live' AND market = 'equity_kr' AND symbol = '035720'
ORDER BY version;
```

Each `evaluate` run also prints `skipped`, a count per reason. Those counts are not
stored. The closed reasons are `SKIP_REASONS` in
`app/services/mechanical_loss_cut/rules.py`.

### What to check

- **Backfilled rows.** These stops are measured from today's average cost, so an
  old loser can already be below its stop. Read them apart from planned rows.
- **Position errors.** Positions come from `review.execution_ledger`. A sale in a
  broker's own app is invisible there, so compare a surprising row with the broker.
- **Lowered stops.** A buy can state a lower stop with an add. Look for a version
  whose `stop_price` is below the version before it.

## Set thresholds

The values under `mechanical_loss_cut` in `config/trading_policy.yaml` are
provisional. ROB-1458 evidence is expected to propose the real ones. Change them
by operator PR, per market:

| Key | Meaning |
|---|---|
| `default_stop.basis` | `pct_floor`, `support` or `volatility`, for a buy with no stated stop and for backfills. |
| `default_stop.floor_pct` | Percent below the reference. Also the widest stop any basis can produce. |
| `stop_after_add` | `keep`, or `recompute_from_average`, which lowers the stop as the average falls. |
| `trigger.family` | `plan_stop_close`, `plan_stop_and_n_day_low` or `plan_stop_and_trailing_drop`. |
| `trigger.unrealized_floor_pct` | The -X% floor that must also be reached. |
| `trigger.rising_lookback_sessions` | No cut if the close is above the close this many sessions ago. |
| `order.max_slippage_pct` | Limit distance below the price. Policy refuses a value wider than `sell.loss_cut_max_slip`. |
| `order.max_lot_fraction` | The most of a lot one cut can sell. |
| `cooldown_sessions` | Sessions after a decision before that lot can have another. |
| `live_include_backfilled` | Whether live may act on a backfilled plan. |
| `thresholds_confirmed` | Live refuses the market until this is true. |

Editing this block changes the policy version and hash, so update the version pins
in the tests.

## Turn on live

Only the operator does this, and only after reading shadow results.

1. In a policy PR, set the accepted thresholds and `thresholds_confirmed: true` for
   each market going live. Merge and deploy.
2. Confirm `ORDER_PROPOSALS_AUTO_APPROVE=true`,
   `ORDER_PROPOSALS_AUTO_APPROVE_MODE=expanded`, and that
   `ORDER_PROPOSALS_SUBMIT_AGENT_ID` is in `LOSS_CUT_ALLOWED_AGENT_IDS`. Without the
   agent identity, previews fail and proposals go to the human card.
3. Set these and restart. Start with one market and one account:

   ```bash
   MECHANICAL_LOSS_CUT_LIVE_ENABLED=true
   MECHANICAL_LOSS_CUT_LIVE_MARKETS=kr
   MECHANICAL_LOSS_CUT_LIVE_ACCOUNT_MODES=kis_live
   ```

Once on, a newly written shadow decision becomes a `loss_cut` proposal with a new
retrospective, tagged `mechanical_loss_cut`, and goes through `dispatch_proposal`.
It submits with no approval card only inside the caps and gates. Anything refused
becomes the ordinary human card, with the reason in `auto_approve_rejections` as
`mechanical_loss_cut_refused` and a `mechanical_refusal` code.

A trigger already recorded in shadow before the switch is not emitted later. Live
acts on triggers first recorded after it is on.

### If the human card appears

A refused proposal shows the normal two-click loss-cut card. Approving it sells; it
is a human decision like any other.

## Roll back

| Goal | Action |
|---|---|
| Stop automatic sells now | `MECHANICAL_LOSS_CUT_LIVE_ENABLED=false`, then restart. |
| Stop all auto-approval | `ORDER_PROPOSALS_AUTO_APPROVE=false`. |
| Stop shadow rows | `MECHANICAL_LOSS_CUT_SHADOW_ENABLED=false`. |
| Stop plan writes | `LOT_EXIT_PLAN_WRITE_ENABLED=false`. Buys carry on; missing plans get backfilled later. |
| Undo an automatic sell | The veto button on the card cancels an unfilled order. A filled order stays filled. Reconcile from broker evidence. |
| Remove the code | Revert the PR. Set the flags to false first. |
| Remove the tables | `uv run alembic downgrade -1`. It refuses while either table holds rows, because losing the decisions table lets a trigger fire twice. Both tables are append-only, so export and clear them deliberately first. |

## Known limits

- Daily closes only. An intraday fall is not cut until the close and the next run.
- Shadow positions and open sells come from the database, as described above.
- `add_verdict` records whether an add fits the plan and does not block it.
- A plan's average cost is the planned value at acceptance. The job measures
  against the position it reads at evaluation time.
- The ledger has no account ID, so one account mode counts as one account.
- The live session check runs at classification, not again inside the send.
