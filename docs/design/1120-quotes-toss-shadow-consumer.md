# #1120 — `quotes:toss` shadow consumer (records only)

Acceptance-criteria review reference for the v0 data lane ordered under
`#1051` (operator decision **Q-109 = A: shadow**). This document fixes the
purpose, invariants, and the AC coverage map **before** implementation.

## 1. Purpose

Read the fillwire Redis stream `quotes:toss` (fillwire PR #8, merged
`8a5b52f`) with a consumer group, and persist two append-only evidence
tables so strategy-lab can score, four weeks later:

- trigger latency (minutes → seconds) for the four #906 R3 spike families,
- ladder rung approach/touch/fill quality (touch→fill rate, slippage,
  touch-to-fill latency).

v0 is **records only**: orders 0, policy 0, session kicks 0, LLM calls 0,
broker calls 0. Nothing is enabled by default — no scheduler, unit, or
runtime registration.

## 2. Inputs and their sources (no invented sources)

| Input | Source | Evaluable? |
|---|---|---|
| Trade tick price | `quotes:toss` `price` (trade ticks) | yes |
| Orderbook bid1/ask1/qty | `quotes:toss` orderbook ticks | parsed, counted, carried on touch rows when latest |
| `session` | stream value, verbatim; one of `nxt_pre, krx_regular, nxt_after, us_pre, us_regular, us_after` | yes — never re-classified; unknown labels dropped+counted |
| Previous close (holding/index reference) | `market_quote_snapshots.previous_close` (read-only; latest per market+symbol) | per-symbol; missing → `not_evaluable` |
| Index level (KOSPI/KOSDAQ/NQ…) | none in `quotes:toss` (stock symbols only) and no persisted index-level read model | **no → `not_evaluable` reason `index_level_unavailable`** |
| Holdings universe | `manual_holdings` (qty>0 on active accounts) ∪ `protected_positions` (qty>0) | yes |
| "core" holdings | `protected_positions` rows (`purpose='long_term'`, `protected_quantity>0`) — the operator-declared long-term floor = the existing holdings source for core inventory | yes |
| Own fills | `review.execution_ledger` (read-only, id watermark) | yes |
| Open rung anchors | non-terminal (`accepted`,`pending`,`partial`) rows in `review.kis_live_order_ledger`, `review.toss_live_order_ledger`, `review.live_order_ledger` where `order_type='limit'` and `price > 0`; anchor = the order's limit price | yes |

`received_at` = the order ledger row's `trade_date` (recorded at send).
`died_at` has no dedicated ledger source — `reconciled_at` is when the
order was *observed* terminal, not when it died, so `died_at` stays NULL.
`nxt_tradable` has no stored source → **always NULL**.  All three are
copied onto event rows when known, NULL otherwise — never guessed.

## 3. Trigger semantics (per-second evaluation)

- `holding_spike`: |price / prev_close − 1| ≥ 5% for **core** symbols,
  ≥ 7% for other held symbols. Window `day`.
- `vi_proxy`: |price / ref_price − 1| ≥ 3% where ref_price is the most
  recent trade tick at or before `ts − 60s` (rejected when the reference
  is older than 75s — a staler gap is a longer window, not the proxy);
  warm-up with no tick ≥60s old evaluates nothing (not a missing
  *source*). Window `60s`. All stream symbols (VI is market-wide).
- `index_spike`: ±3% vs reference — always `not_evaluable` in v0
  (`index_level_unavailable`); recorded once per KST day.
- `own_fill`: each new `review.execution_ledger` row past the id
  watermark is one firing. Window `fill`.

A firing is an **edge**: it is recorded when the trigger first crosses its
threshold, and can fire again only after the price moves back below the
threshold (re-arm). A held symbol missing prev_close yields one
`not_evaluable` row per symbol per KST day (`previous_close_unavailable`).

## 4. Shadow suppression fields (computed, never executed)

Each firing row stores `would_kick`, `suppress_reason`,
`daily_would_kick_count`, `last_would_kick_at`. The gate mirrors the #906
gate's arithmetic per **market** (`kr`, `us` from session;
`equity_kr/equity_us/crypto`→`kr/us/crypto` for ledger fills):
daily cap 2, 60-minute cooldown, counted as if `would_kick=true` rows had
actually kicked. The daily cap is keyed `(market, KST date)` and resets
at KST midnight; the cooldown is one timestamp per market and **carries
across midnight** — matching #906 `cooldowns[market]`. Startup re-seeds
both from committed firing rows (today's cap count, all-time latest
would-kick timestamp) and re-seeds in-breach keys so a replayed batch
during the same breach can only conflict, never write a second row.
Fills landed while the consumer was down are also recovered: the
restart watermark resumes from the last recorded `own_fill` firing, not
the current ledger max.

## 5. Ladder events

Per open rung (§2): `approach` = trade tick enters ±0.5% of anchor without
crossing (edge into band; re-arms when price leaves the band);
`touch` = trade tick crosses the anchor (buy: `price ≤ anchor`;
sell: `price ≥ anchor`), once per rung lifetime; `fill` = the rung's order
reaches `filled` in its ledger, or an `execution_ledger` row matching the
rung's `broker_order_id` (+ ledger-mapped broker when the ledger is
broker-specific) arrives — the second path to arrive conflicts on the
constant fill dedupe key. Runtime state (far/near/touched/done) is
re-seeded from committed event rows at startup, so replayed pending
entries cannot mint a second event. Rows carry symbol, side, market,
session (verbatim; NULL for ledger-derived fills), `anchor_price`,
`event_price`, `event_ts`, order refs (`order_ledger`,
`order_ledger_id`, `broker_order_id`, `client_order_id`,
`correlation_id`), the §2 order facts, `fill_ledger_id` when the fill came
from `execution_ledger`, `stream_entry_id` when the event came from a
tick, and `detail` JSONB (latest bid1/ask1 when known).

## 6. Idempotency

Every row carries a deterministic `dedupe_key` and the insert is
`INSERT … ON CONFLICT DO NOTHING`:

- firing (edge): `spike:{trigger}:{symbol}:{kst_date}:{stream_entry_id}`
- not_evaluable: `ne:{trigger}:{symbol-or-*}:{reason}:{kst_date}`
- own_fill: `ownfill:{execution_ledger_id}`
- ladder approach: `ladder:{ledger}:{ledger_id}:approach:{stream_entry_id}`
- ladder touch/fill: `ladder:{ledger}:{ledger_id}:{touch|fill}` (constant —
  once per rung lifetime, so no event identity is needed)

Stream entries are XACK-ed **after** the DB commit. Redelivery replays the
same dedupe keys → conflicts → zero new rows (A3). Pending entries are
reclaimed in two phases: entries pending under this consumer's own name
are claimed immediately (consumer names are host-derived, so a same-name
PEL means a dead same-host process), while foreign consumers' entries are
XAUTOCLAIM-ed only after `min_idle_time=30s` — long enough that a live
peer's in-flight batch can never be stolen — in bounded cursor passes.
The drain runs at startup and then every 30s so dead-peer work is
rescued rather than stranded.

## 7. Invariants (mutants — counted from disk)

1. No module under `app/services/quotes_consumer/`,
   `app/jobs/quotes_consumer.py`, or `app/tasks/quotes_consumer_tasks.py`
   imports `fill_event_handoff`, `ops_task_kick`, Prefect, a broker
   client, an order/ledger *write* service, a session/LLM surface, or
   TaskIQ scheduling — verified statically (AST/text scan).
2. The repository exposes only INSERT + SELECT; no UPDATE/DELETE code
   path exists anywhere in the package.
3. Both tables carry DB-level append-only triggers (UPDATE/DELETE/
   TRUNCATE rejected), mirrored in `tests/_schema_bootstrap.py`.
4. `session` is stored verbatim; `parse_quote_entry` drops and counts a
   label outside the six-value contract set instead of coercing it.
5. `quotes_toss_consumer_enabled` defaults `False`; the task returns
   without consuming when the flag is off; no `schedule=` label exists.
6. `nxt_tradable` is written only from an explicit known source — v0 has
   none, so it is always NULL; `received_at`/`died_at` may only be copied
   from ledger evidence, never synthesized.
7. Threshold edges are `>=` comparisons on exact `Decimal` arithmetic —
   one tick before the threshold does not fire.
8. The consumer never opens a Toss/KIS websocket or calls an HTTP quote
   source; inputs are the stream + DB reads only.
9. Symbol joins run through `app.core.symbol.to_db_symbol` — no ad-hoc
   string surgery.
10. `dedupe_key` is UNIQUE on both tables; redelivery cannot duplicate.

## 8. AC coverage map

| AC | Test |
|---|---|
| A1 threshold edge + tick-type field handling | `test_triggers.py`, `test_stream_parsing.py` |
| A2 shadow: fake kick path fails if called; cap 2/day + 60 min | `test_triggers.py` (+ `test_invariants.py` static no-kick) |
| A3 redelivery writes no duplicates | `test_consumer_idempotency.py` (fakeredis stream) |
| A4 ladder approach/touch/fill once each; unknown order fields NULL | `test_ladder.py` |
| A5 migration up/down on throwaway DB; append-only | `test_migration.py` |
| A6 session pass-through; unknown label dropped+counted | `test_stream_parsing.py` |
