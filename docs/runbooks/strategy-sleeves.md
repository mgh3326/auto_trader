# Strategy sleeves (ROB-1470)

A strategy sleeve is one strategy's slice of an existing real account (KIS,
Toss or Upbit). It keeps that strategy's money and results apart in the
database: its own budget, its own loss limit, its own lots and P&L. No new
broker account is created.

The operator sets every amount. Nothing in code, policy or this runbook picks
one; every amount below is a placeholder such as `<budget cap>`.

## What a sleeve is

| Field | Meaning |
| --- | --- |
| `sleeve_key` | Your name for it, lowercase, for example `<sleeve key>` |
| `rule_id` | The ROB-1458 rule it runs (list below) |
| `account_mode`, `market` | `kis_live`/`toss_live` with `equity_kr`/`equity_us`, or `upbit` with `crypto` |
| `currency` | Derived: KRW for `equity_kr` and `crypto`, USD for `equity_us` |
| `state` | `shadow`, `live`, `paused` or `stopped` |
| `budget_cap` | Most cash the sleeve may use, in its currency |
| `loss_limit` | Loss (realized + unrealized, net of fees) that stops the sleeve |
| `metric` | Pre-registered primary metric (ROB-1468), your words |
| `min_effect_pct` | Smallest effect worth acting on, in percent of the sleeve budget |
| `horizon_sessions` | Fixed horizon H: each signal is marked at the H-th completed session after it (crypto: day) |
| `max_duration_days` | Maximum duration, counted from the first `live` version |

The identity (first four rows) never changes. State, amounts and the
pre-registration are versioned: each change appends a new version with who set
it, when and why. The database refuses to edit or delete a version.

The pre-registration (last four rows) follows the ROB-1468 canary statistics
design. A sleeve cannot be `live` until all four are set, and once it has been
live they cannot change; a new metric, effect, horizon or duration is a new
sleeve (a new epoch).

Rules (from the canary design, `docs/research/rob-1465-trade-retrospective.md` §4):

| `rule_id` | Design | Markets |
| --- | --- | --- |
| `rob1458.us_near_market_entry` | §4.1 | `equity_us` |
| `rob1458.us_add_at_minus8` | §4.2 | `equity_us` |
| `rob1458.kr_cut_minus20` | §4.3 | `equity_kr` |
| `rob1458.parking_idle_cash` | §4.4 | `equity_kr`, `equity_us` |
| `rob1458.crypto_sell_expiry` | §4.5 | `crypto` |
| `rob1458.crypto_b3` | §4.6 | `crypto` |

## What changes for orders

- A proposal joins a sleeve when it is created with `sleeve_id`
  (`order_proposal_create(..., sleeve_id=<id>)`). The sleeve must have the same
  account mode and market. A revision keeps its sleeve. Proposals without
  `sleeve_id` behave exactly as before.
- When auto-approve is on, a tagged proposal is auto-approvable only if all of
  these hold. Otherwise it goes to the normal approval card, and the card names
  the reason:

  | Reason on the card | When |
  | --- | --- |
  | `sleeve_not_live` | The sleeve state is `shadow`, `paused` or `stopped` |
  | `sleeve_loss_limit_reached` | Loss ≥ loss limit. The sleeve is also moved to `stopped` (actor `system:sleeve_loss_gate`) |
  | `sleeve_pnl_unmeasured` | A held symbol has no current daily close, so P&L is unknown |
  | `sleeve_budget_exceeded` | Buy: used + this order > budget cap |
  | `sleeve_used_unmeasured` | Buy: an open sleeve order has no price, so used is unknown |
  | `sleeve_unreadable` | The sleeve could not be read |

  The card is the ordinary approval card: you can still approve it by hand.
  The gate runs before any broker call.
- Used = held cost basis + open buy orders + fills the order already reports
  that the ledger has not booked yet. Sells and cancels use no budget, but the
  state and loss checks apply to them too.
- Fills come from `execution_ledger` (reconciled rows only, not quarantined)
  matched to the sleeve's own orders. A fill appears in the sleeve after the
  reconcile books it.

## Commands

All commands name the database explicitly. Use `--database-url-env <ENV NAME>`;
the value is never printed. `create` and `set` only print a preview unless you
add `--commit`.

### Create a sleeve

Start in `shadow`. A shadow sleeve's tagged orders always go to the card.

```bash
uv run python scripts/strategy_sleeves.py --database-url-env <ENV NAME> create \
  --sleeve-key <sleeve key> \
  --rule-id <rule id> \
  --account-mode <kis_live|toss_live|upbit> \
  --market <equity_kr|equity_us|crypto> \
  --state shadow \
  --budget-cap <budget cap> \
  --loss-limit <loss budget per strategy> \
  --metric "<primary metric>" --min-effect-pct <minimum effect> \
  --horizon-sessions <H> --max-duration-days <maximum duration> \
  --reason "<why>" --actor <you>
# check the preview, then repeat with --commit
```

The output has `sleeve_id`. Use it in `order_proposal_create(sleeve_id=...)`.

### Change amounts

Give only what changes. The rest is copied from the current version.

```bash
uv run python scripts/strategy_sleeves.py --database-url-env <ENV NAME> set \
  --sleeve-key <sleeve key> --budget-cap <new budget cap> \
  --reason "<why>" --actor <you> --commit
```

`--loss-limit <new loss limit>` works the same way, and so do the
pre-registration flags while the sleeve has never been live.

### Flip shadow to live

Before you flip, check the design's gate for that rule (for example §4.3 opens
the KR live arm only after its shadow gate passes), that `report` shows the
expected budget and limit, and that the pre-registration is complete and final:
it freezes at this step.

```bash
uv run python scripts/strategy_sleeves.py --database-url-env <ENV NAME> set \
  --sleeve-key <sleeve key> --state live \
  --reason "<shadow gate result>" --actor <you> --commit
```

### Pause, stop, restart

- Pause (no new auto-approvals, nothing else changes): `--state paused`.
- Stop: `--state stopped`.
- A sleeve the loss gate stopped stays stopped until you set it again. To
  restart, raise the loss limit or accept the reset and set `--state live` with
  a reason. Per the design, a changed metric, horizon or stop rule is a new
  epoch: create a new sleeve rather than reusing the old one.

### History and report

```bash
uv run python scripts/strategy_sleeves.py --database-url-env <ENV NAME> history \
  --sleeve-key <sleeve key>
uv run python scripts/strategy_sleeves.py --database-url-env <ENV NAME> report
uv run python scripts/strategy_sleeves.py --database-url-env <ENV NAME> report --json
```

The report prints per sleeve: budget, used and what is left, realized,
unrealized and net P&L, fees, loss limit and whether it is reached, trades,
open positions with their marks, the safety state, the pre-registration, and
the canary statistics. It ends with totals per currency; KRW and USD are never
added together.

Safety and statistics are separate lines and never read each other:

| Safety state | Meaning |
| --- | --- |
| `shadow`, `paused`, `stopped` | The sleeve state |
| `loss_limit_reached` | Loss ≥ loss limit |
| `over_budget` | Used > budget cap |
| `unmeasured` | P&L or used is unknown |
| `ok` | None of the above |

Canary statistics (ROB-1468):

- One sample is one episode. A signal is a proposal with its revisions, rungs
  and partial fills; an episode is a signal plus every later signal on the same
  symbol raised while the sleeve still holds a position from it (adds, later
  ladders, the exit). A signal raised while the sleeve is flat starts a new
  episode.
- An episode's result is all its trades marked at the H-th close after its
  first signal, net of fees, in percent of the budget in effect when it began.
  A sell is measured against holding to H.
- An unfilled episode stays in the cohort with result 0. A matured episode
  whose H close is missing is `unmeasured`: counted, never averaged as zero.
- The comparison baseline is not recorded in auto_trader, so the report shows
  `baseline=not_recorded` and leaves D (result minus baseline) empty. Score D
  against `min_effect_pct` in the research loop.

| Verdict state | Meaning |
| --- | --- |
| `not_preregistered` | One of the four pre-registration fields is missing |
| `not_started` | Never live |
| `collecting` | Before the final date (first live + max duration) |
| `final_date_reached` | Score it against the pre-registration. This is not a pass |

`attribution skipped` lists ledger rows on the sleeve's order ids that were not
counted, and why (for example a KIS order number reused on a later day).

## Deploy and rollback

The migration `20261102_rob1470_sleeves` adds two tables and one nullable column
on `review.order_proposals`. It changes no existing row. The desk asks the
operator before running it.

```bash
uv run alembic upgrade head     # apply
uv run alembic downgrade -1     # roll back
```

- Nothing is tagged until a sleeve exists and a proposal names it, so deploying
  the code and the migration changes no order.
- To take one sleeve out of the auto lane, `set --state paused` (or `stopped`).
  Its tagged orders then go to the card.
- The downgrade refuses while any sleeve exists or any proposal carries a
  sleeve, because dropping the column would move those orders back into the
  untagged book silently. Export the sleeve rows first and remove the tags and
  sleeves deliberately before downgrading.

## Known limits

- Unrealized P&L uses the last completed daily close. A symbol without a
  current close makes P&L unknown and sends tagged orders to the card.
- A fill counts after the reconcile books it. Until then a filled-but-unbooked
  order still counts toward used (at its limit price), but not toward P&L.
- A replace proposal's new order and the order it replaces both count toward
  used until the old one is cancelled. This errs toward more cards.
- Direct order tools (`kis_live_place_order`, `toss_place_order`, ...) do not
  take a sleeve. Sleeve orders go through proposals.
- ROB-1460 exit plans are not linked to sleeves yet; that follows after #2266
  merges.
