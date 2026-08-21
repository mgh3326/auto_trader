# Durable Telegram callback inbox (W5)

**Status: shipped inert.** All three gates default to `false`, the recovery
task ships scheduleless, and no process in this repository starts a worker. The
activation sequence in §4 is documentation; nothing in this PR performed any
step of it.

---

## 1. Why this exists

`POST /trading/api/telegram/callback` runs the entire approval workflow —
re-validation, broker submit, Telegram message edit — inside the webhook
request. Sentry, production, 7 days, `n=44`:

| metric | value |
|---|---|
| avg | 3.365 s |
| p50 | 2.738 s |
| p95 | 12.707 s |
| max | 13.593 s |

The child aggregate is dominated by `http.client` (359 spans, 86.90 s) rather
than DB (3,106 spans, 4.47 s) — i.e. the request thread is waiting on Telegram
and the broker.

The durability problem is worse than the latency one. The TaskIQ broker is
`taskiq-redis` `ListQueueBroker`, which is `LPUSH`/`BRPOP`: a message is
*removed* from Redis before the worker finishes with it. For an order-adjacent
approval click that is not durable enough.

So: **PostgreSQL is the authority. TaskIQ is a best-effort wake-up carrying an
opaque job UUID.** Losing Redis loses latency, never a click.

---

## 2. The pieces

| piece | file |
|---|---|
| table | `review.telegram_callback_inbox` (`app/models/telegram_callback_inbox.py`) |
| migration | `alembic/versions/20260821_w5_telegram_callback_inbox.py` |
| vocabularies | `app/services/order_proposals/callback_inbox/contracts.py` |
| repository (service-internal) | `.../callback_inbox/repository.py` |
| service (only writer) | `.../callback_inbox/service.py` |
| job advisory lock | `.../callback_inbox/locks.py` |
| ingress | `.../callback_inbox/ingress.py` |
| worker | `.../callback_inbox/worker.py` |
| recovery sweep | `.../callback_inbox/recovery.py` |
| telemetry allowlist | `.../callback_inbox/observability.py` |
| TaskIQ surface | `app/tasks/telegram_callback_inbox_tasks.py` |
| HTTP surface | `app/routers/telegram_callback.py` |
| normalize/execute seam | `app/services/order_proposals/telegram_callback.py` |

The callback *core* is unchanged. `handle_callback_update` was split into
`normalize_callback_update` (shape → chat allowlist → the existing callback-data
parser) and `handle_normalized_callback` (everything else, in the same order).
The inline path is `normalize` then `execute`, exactly as before; the durable
ingress normalizes without executing, and the worker executes a stored
envelope. Every authorisation gate — published-binding preflight, single-use
nonce, commit lease, target-mutation lock, approval hash, fresh preview — still
lives where it lived.

---

## 3. Flow

### Ingress (request thread)

1. `AuthMiddleware` validates the Telegram webhook secret token (unchanged).
2. `normalize_callback_update`: shape, chat allowlist, callback-data parser.
   A rejection here means **200, no row, no kick** — the same external effect
   the inline path has today for the same input.
3. Insert the normalized envelope and **commit**.
4. Bounded, best-effort Redis kick
   (`ORDER_PROPOSALS_TELEGRAM_CALLBACK_ENQUEUE_TIMEOUT_SECONDS`, default 2 s).
5. `200 {"ok": true}`.

A failure at step 3 raises `CallbackInboxUnavailable`, the route answers a
**generic** 503, and step 4 never runs. Telegram retries; nothing is
half-accepted. A failure at step 4 changes nothing — the committed row is the
durable acknowledgement and the recovery sweep will find it.

**Dedupe.** `update_digest` is a domain-separated SHA-256 over
`(update_id, callback_query_id)` only — never the payload. A re-delivery of the
same call is a benign `duplicate`. A *different* envelope reusing the same
delivery identity is `delivery_conflict`: not stored, not queued, not reported
as accepted.

### Worker

```
lock -> classify -> attempt -> rebuild -> re-authorise -> enter -> run -> verdict -> terminal
```

- **Lock first.** A PostgreSQL *session* advisory lock on a *dedicated*
  connection, held for the whole handler execution. Two tasks for one job
  invoke the handler once.
- **Attempt before work.** `processing` + `attempt_count + 1` are committed
  before the handler runs, so a crash loop converges on the dead-letter.
- **Re-authorise.** The chat allowlist is checked again against *current*
  settings; a chat revoked while the job was queued gives `discarded` /
  `chat_revoked`, handler not called.
- **`handler_entered_at`** is committed immediately before the call — see §5.
- **Verdict then terminal**, in two commits — see §5.
- **Processing time is the clock.** `now = now_kst()` at execution start, and
  `now_fn=now_kst` is passed through, so queue delay never extends
  `valid_until`, the loss-cut confirmation window, or a batch TTL. A callback
  that expires while queued comes back `expired` with the nonce unspent and
  zero broker calls.

### Recovery

Scans `pending`, due `retry_wait`, and `processing` rows older than
`PROCESSING_STALE_AFTER_SECONDS` (300), then runs each through the same
`process_callback_job` with a wider claimable-state set. The staleness window
is a **scan filter, never an authority**: a "stale" row whose lock is held is
skipped. Reports counts by state plus one age — no identifiers.

---

## 4. Activation (operator-only; not performed in this PR)

Strictly in order. Each step is independently reversible.

1. **Migration.** `alembic upgrade head`. Additive; changes no behaviour on its
   own, because all three gates are still false.
2. **Deploy the code.** Still inert.
3. **Worker gate.** `ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED=true`,
   restart the TaskIQ worker. Nothing is producing jobs yet, so this is a no-op
   you can verify is running before it matters.
4. **Recovery gate.**
   `ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED=true`, then
   **restart the scheduler and the worker**. The `schedule` label is evaluated
   at import; without a restart the cron is not registered and you will
   believe you have a safety net you do not have. Confirm with an
   empty-inbox drain: the task should run and report
   `{"status": "ok", "claimed": 0, ...}`.
5. **Ingress gate.** `ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED=true`.

The ingress refuses traffic (generic 503) unless the worker and recovery gates
are both true, so steps 3–4 cannot be skipped by configuration. **That guard
checks settings, not processes.** Before step 5, verify the processes are
actually up — a worker with the gate set but the process dead would satisfy the
config guard and still drain nothing.

### Readiness: database connection headroom

Each concurrently processing job holds **one dedicated connection for the
advisory lock** plus whatever the callback core's own sessions use. Budget at
least `2 x max_concurrent_jobs` connections per worker process, on top of the
web application's pool, and check it against `max_connections` before step 3.
Exhausting the pool here fails jobs, it does not corrupt them — but it fails
them as `pre_core_failure`, which retries, which uses more connections.

---

## 5. Retry algebra — read this before changing it

The callback core catches every exception and reports
`{"handled": False, "reason": "internal_error"}`. **That string is not evidence
that the broker leg never started.** An exception raised after
`revalidate_and_submit` reached the broker and before the transaction committed
produces exactly that result — and the rollback leaves the nonce *unconsumed*
and the published binding *still valid*. A retry would look perfectly legal and
submit a second time.

So the only re-runnable class is a failure that provably never entered the core:

| situation | state | error class | handler re-invoked? |
|---|---|---|---|
| pre-core failure (envelope rebuild, notifier resolution) | `retry_wait` | `pre_core_failure` | yes, up to 3 attempts |
| typed `mutation_not_started: True` from a handler | `retry_wait` | `pre_core_failure` | yes (today's core never sets it) |
| `handled: True` — including `results: ["unverified"]` | `succeeded` | — | never |
| explicit business rejection (`nonce_replay`, `expired`, `guard_blocked`, …) | `discarded` | — | never |
| `internal_error` after core entry | `dead_letter` | `handler_ambiguous` | never |
| crash after core entry (`handler_entered_at` set, no verdict) | `dead_letter` | `handler_ambiguous` | never |
| core raised (contract violation) | `dead_letter` | `handler_exception` | never |
| chat left the allowlist | `discarded` | `chat_revoked` | never |
| stored envelope no longer rebuilds | `discarded` | `envelope_invalid` | never |
| 3 re-runnable attempts spent | `dead_letter` | `attempts_exhausted` | never |

`handled: True, results: ["unverified"]` is a **success** for the job. An
ambiguous *send* is already modelled by the proposal/order state machine, which
owns it; re-running the callback would not resolve it and could duplicate it.

**Two markers make this decidable after the process is gone:**

- `handler_entered_at` — committed immediately before the core is invoked. The
  only durable difference between "died before the mutating region" and "died
  inside it".
- `handler_completed_at` + `terminal_state_pending` — committed after the core
  returns and before the terminal state is applied. If that last commit is
  lost, recovery finds a row that already says what the handler decided and
  **repairs the paperwork instead of re-running the handler**. No attempt is
  spent on a repair.

**A dead-lettered job is not replayable and must not be made replayable.** The
authority fields are gone (§6). Issue a fresh approval card instead.

---

## 6. What is stored, and for how long

Only the fields the existing `CallbackEnvelope` needs plus what the worker
must re-gate on. The raw Telegram `Update` is never stored, and the table has
no JSON/JSONB/ARRAY column to put one in.

On reaching `succeeded`, `discarded` or `dead_letter`, these are set to `NULL`
in the same write that applies the terminal state:

`callback_query_id`, `chat_id`, `message_id`, `telegram_user_id`, `action`,
`subject_short`, `dispatch_attempt_id`, `membership_revision`,
`membership_digest`, `nonce`.

Two CHECK constraints make that mechanical:

- `ck_telegram_callback_inbox_terminal_scrubbed` — a terminal row with any of
  those retained is rejected by the database;
- `ck_telegram_callback_inbox_active_reconstructable` — an active row missing
  any reconstruction field is rejected.

Both are `CASE WHEN … THEN … ELSE true END` over `IS NULL`/`IS NOT NULL`, so no
operand can be SQL `UNKNOWN` — which a CHECK treats as satisfied, and is
exactly how a scrub constraint silently stops constraining anything.

What survives is `update_digest` (one-way, cannot reconstruct what it was
built from), a slug `outcome` label constrained to `^[a-z0-9_]{1,64}$`, and a
closed-vocabulary `error_class`.

**While a job is pending or processing the row does hold minimal PII** — chat
id, user id, and the single-use nonce. That is unavoidable: the worker cannot
re-authorise or execute without them. It is bounded by the job's lifetime plus
the retry backoff, and by `attempt_count <= 3`.

Redis sees `{"job_id": "<uuid>"}` going in and `{"status": …, "job_id": …}`
coming out. Nothing else. Logs and Sentry spans use the allowlist in
`observability.py`.

---

## 7. Limits, honestly

- **The advisory lock is not broker fencing.** It serialises *this
  application's* processing of one job. It says nothing about what a broker
  received.
- **It is not a distributed lock across a PostgreSQL restart.** If the server
  restarts, every advisory lock is gone while an old coroutine may still be
  running. The window is extreme-tail, and the callback core's own nonce,
  commit lease and rung-stable idempotency key remain in force underneath —
  but the inbox lock alone does not cover it.
- **The consumers-armed guard checks configuration, not liveness.** See §4.
- **Real process activation is a post-deploy risk**, not something this PR
  validated. No live Telegram, broker or production database was touched.
- **The staleness window is a heuristic** for *when to look*, never for
  whether a job is claimable.

---

## 8. Rollback

Order matters, and it is not the reverse of §4.

1. `ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED=false`. The webhook goes
   back to the inline path immediately. Stop here for an incident — this alone
   restores today's behaviour.
2. **Drain.** Leave the worker and recovery gates ON until the backlog is
   terminal. Check with `recover_telegram_callback_jobs`' `backlog` block, or:

   ```sql
   SELECT state, count(*) FROM review.telegram_callback_inbox GROUP BY state;
   ```

   Every row must be `succeeded`, `discarded` or `dead_letter`. Rows still
   `pending`/`processing`/`retry_wait` are **unanswered operator clicks**;
   dropping them silently discards approvals.
3. Turn the worker and recovery gates off.
4. Only then deploy older code.

**Do not `alembic downgrade` this table while jobs are in flight.** The
downgrade drops it, and with it every unprocessed click and every terminal
tombstone that dedupes re-deliveries. A downgrade is a last resort after step 2
proves the table is fully drained.

---

## 9. Dead letters

```sql
SELECT job_id, error_class, outcome, attempt_count, received_at, finished_at
FROM review.telegram_callback_inbox
WHERE state = 'dead_letter'
ORDER BY finished_at DESC;
```

`error_class` tells you which of §5's rows you are in. There is deliberately
nothing else to see: the authority is scrubbed, so a dead letter cannot be
replayed by hand.

The action is always the same — **look at the proposal's own state** (it is
authoritative), decide whether the intent still holds, and if so issue a fresh
approval card. Never reconstruct a callback.

`handler_ambiguous` is the one that deserves a real look: it means a callback
entered the mutating region and did not report back. Check the proposal's rungs
and the relevant order ledger for broker evidence before deciding anything.

---

## 10. Post-deploy targets (not PR acceptance)

- callback root p95 ≤ 1 s, and `http.client` child spans on that transaction 0
- Telegram/broker spans appear under the worker transaction instead
- after ≥ 20 jobs or 7 days: review queue delay, retry counts, dead letters and
  oldest pending
