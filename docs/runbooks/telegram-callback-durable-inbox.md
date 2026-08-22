# Durable Telegram callback inbox (W5)

**Status: shipped inert.** All three gates default to `false` and the recovery
task ships scheduleless (its cron label is `[]` while the gate is off, so the
scheduler registers nothing for it).

To be precise about what that does and does not claim: this change adds no
worker or scheduler process and activates none. It does not claim the
repository starts no workers at all — the existing Makefile, compose files and
ops launchers do run TaskIQ worker and scheduler processes, and once this code
is deployed those processes **will discover both new tasks**. They are inert
when discovered: the per-job task returns `{"status": "disabled"}` before
opening a database session, and the recovery task carries no schedule to fire
on. What arms them is an operator setting the gates in §4, and a static guard
(`test_no_auto_activation.py`) asserts that no tracked compose file, ops
launcher, Makefile target, env template or CI workflow sets any of the three
to true.

The activation sequence in §4 is documentation; nothing in this PR performed
any step of it.

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
| tables | `review.telegram_callback_inbox` and the PII-free singleton `review.telegram_callback_recovery_cursor` (`app/models/telegram_callback_inbox.py`) |
| migration | `alembic/versions/20260821_w5_telegram_callback_inbox.py` |
| vocabularies | `app/services/order_proposals/callback_inbox/contracts.py` |
| repository (service-internal) | `.../callback_inbox/repository.py` |
| service (only writer) | `.../callback_inbox/service.py` |
| job advisory lock | `.../callback_inbox/locks.py` |
| ingress | `.../callback_inbox/ingress.py` |
| worker | `.../callback_inbox/worker.py` |
| recovery sweep | `.../callback_inbox/recovery.py` |
| telemetry allowlist | `.../callback_inbox/observability.py` |
| result boundary | `.../callback_inbox/result_boundary.py` |
| TaskIQ receiver boundary | `.../callback_inbox/taskiq_receiver_boundary.py` |
| TaskIQ surface | `app/tasks/telegram_callback_inbox_tasks.py` |
| HTTP surface | `app/routers/telegram_callback.py` |
| normalize/execute seam | `app/services/order_proposals/telegram_callback.py` |

For accepted canonical inputs, the post-normalization execution core and the
pre-existing authorization gates remain unchanged. Normalization also has the
R37 numeric identifier trust boundary: it validates the exact callback-query
shape and identifiers before durable ingress or execution. The durable ingress
normalizes without executing, and the worker executes a stored envelope.
Published-binding preflight, single-use nonce, commit lease, target-mutation
lock, approval hash, and fresh preview remain downstream gates.

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

The enqueue deadline is finite **`(0, 10]` seconds** (default 2). At most 16
best-effort producer tasks may be in flight in one web process. On deadline
expiry, ingress asks the producer to cancel and returns the durable ACK result
immediately; it does **not** wait for a producer that catches cancellation.
That task remains strongly held until its done callback consumes its late
success, failure, or cancellation, so it still occupies one of the 16 slots.
At capacity no producer is invoked; the row remains pending for the normal
R29 recovery path. A late result never changes row authority or delays an ACK;
duplicate kicks remain harmless because the database row and per-job advisory
lock are authoritative.

**Delivery identity and dedupe.** A callback-query id is primary when present;
a valid `update_id` is the fallback only when no callback-query id exists.
`update_identity_digest` is a separate active-row verification field, so a
changed update id on the same callback id is a `delivery_conflict`, not a second
job. `update_digest` is the one-way dedupe tombstone that survives terminal
scrubbing; `update_identity_digest` is scrubbed with the active authority.

### R37 identifier boundary

`callback_query.from` itself must be an exact built-in `dict`, and its `id` is
required and accepted only as an exact built-in `int` in `1..2**52-1`.
`update_id` is either `None` or an exact built-in `int` in
`1..2_147_483_647`; every present update id is validated even when the
callback-query id is the primary delivery identity. Booleans, subclasses,
strings, and coercible values are rejected before durable authority. The
database stores the user id as canonical decimal `Text`; the worker strictly
reconstructs an exact integer, and active rows require that value.

### TaskIQ receiver and result boundary

The job wire envelope is exactly one positional canonical lowercase hyphenated
UUID string with no kwargs. The recovery wire envelope is exactly empty. For
the two W5 task names, formatter-load sanitization runs before the first
decoded-message debug/Sentry surface, and the final W5 middleware re-sanitizes.
Incoming labels and label-type metadata are discarded, so SmartRetry never
receives retry authority for these callbacks. Malformed jobs return the fixed
`invalid_job_id` result; malformed recovery returns fixed `error`.
Endpoint-specific result fields and statuses are closed vocabularies, and
worker/recovery extras or exception strings never cross Redis, logs, or Sentry.
The task body converts only exact `CancelledError`, `KeyboardInterrupt`, and
`SystemExit` into private category-only signals. The final W5 `post_execute`
raises a fresh safe exact control after Receiver's task-exception catch but
before SmartRetry post-processing and result-backend save; retry/save see
nothing and no Receiver error log is emitted. Other failures collapse to fixed
safe results.

### Worker

```
lock -> classify -> attempt -> rebuild -> re-authorise -> enter -> run -> verdict -> terminal
```

- **Lock first.** A PostgreSQL *session* advisory lock on a *dedicated*
  connection, held for the whole handler execution. Two tasks for one job
  invoke the handler once.
- **The acquire commits immediately.** A session advisory lock survives
  `COMMIT`, so the transaction opened by `pg_try_advisory_lock` is closed as
  soon as the result is read and the lock is still held. The holder therefore
  shows as `idle` in `pg_stat_activity`, not `idle in transaction`, for the
  whole handler. **You do not need to configure
  `idle_in_transaction_session_timeout` for this path, and you must not rely
  on it**: if that timeout were shorter than a job, PostgreSQL would terminate
  the holder mid-job and release its advisory locks silently, while the
  coroutine that believes it owns the job kept running. The code closes the
  transaction so the guarantee does not depend on a server setting.
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

For every sweep with `limit >= 1`, recovery first opens a short, separate
transaction and atomically reserves `q = min(limit, 4)` positions from the
singleton cursor. The cursor is empty after migration and lazily self-seeds on
that first UPSERT; it contains only `id=1`, the next tier, and a timestamp —
no callback authority or PII. The reservation commits **before** the candidate
scan. Its returned start is passed unchanged to the scan; the service never
rereads the cursor. A reservation or commit error fails closed: recovery does
not scan, invoke a handler, or fall back to a fixed, time-based, or
process-local order. A process death after a successful reservation can burn
one block, which is safe because the next successful sweep advances normally.

The exact cyclic order is malformed active budgets, exhausted canonical
`retry_wait`, canonical `pending`/due `retry_wait`, then stale `processing`.
The scan still makes four separately bounded candidate SELECTs with
deterministic `received_at, job_id` ordering inside each tier, then starts its
round-robin emission at the committed tier. This is **cross-tier** fairness:
after the last interrupted or burned reservation, every persistent runnable
tier is offered within `ceil(4 / q)` consecutive sweeps that reach candidate
processing, where `q = min(limit, 4)`. It does not promise per-row fairness
behind a permanently lock-contended head; an offered row still requires its
advisory lock. The staleness window is a **scan filter, never an authority**:
a "stale" row whose lock is held is skipped. The candidate cap bounds returned
rows/queries, not the physical index work required to find them. Reports counts
by state plus one age — no identifiers.

`max_attempts` is fixed at `3` and is not configurable. Malformed active budgets terminalize as `dead_letter` / `attempt_budget_invalid`, normalize `max_attempts` to `3`, clamp `attempt_count` to `0..3`, and scrub authority before the handler.

Recovery UUID materialization accepts only an exact stdlib `uuid.UUID` or an
exact `asyncpg.pgproto.UUID`. It copies through the owning base descriptor into
a fresh stdlib UUID, rejects subclasses, spoofed values, and malformed storage
without rendering them, and imports asyncpg lazily only after the stdlib fast
path. A malformed candidate consumes one bounded scanned/claimed error slot;
the sweep then continues.

v39 now covers both `review.telegram_callback_inbox` and `review.telegram_callback_recovery_cursor`.

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

So the only re-runnable class is a failure that provably never entered the
core -- and "provably" means two independent things must agree: the worker
raised `PreCoreFailure` from the phase *above* the `mark_handler_entered`
commit, **and** `schedule_retry`'s conditional UPDATE re-confirmed in the
database that the row is still `processing` with `handler_entered_at`,
`handler_completed_at` and `terminal_state_pending` all NULL. A handler
cannot produce `PreCoreFailure` (by the time it runs, that phase is over, and
an exception escaping it is `handler_exception`), and no value it returns
grants anything:

| situation | state | error class | handler re-invoked? |
|---|---|---|---|
| pre-core failure (envelope rebuild, notifier resolution) | `retry_wait` | `pre_core_failure` | yes, up to 3 attempts |
| a handler-returned `mutation_not_started` / `retry` / `retryable` / `safe_to_retry` | **ignored — buys no authority** | as per the row it actually lands in | never |
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
`membership_digest`, `nonce`, `update_identity_digest`.

Two CHECK constraints make that mechanical:

- `ck_telegram_callback_inbox_terminal_scrubbed` — a terminal row with any of
  those retained is rejected by the database;
- `ck_telegram_callback_inbox_active_reconstructable` — an active row missing
  any reconstruction field is rejected.

Both are `CASE WHEN … THEN … ELSE true END` over `IS NULL`/`IS NOT NULL`, so no
operand can be SQL `UNKNOWN` — which a CHECK treats as satisfied, and is
exactly how a scrub constraint silently stops constraining anything.

What survives is `update_digest` (one-way, cannot reconstruct what it was
built from), one `outcome` from a closed category vocabulary, and a
closed-vocabulary `error_class`. Unknown raw reasons are retained only as the
fixed `unclassified` category; no regex-valid arbitrary slug or payload suffix
can survive in the row, logs, or Sentry.

**While a job is pending or processing the row does hold minimal PII** — chat
id, user id, and the single-use nonce. That is unavoidable: the worker cannot
re-authorise or execute without them. It is bounded by the job's lifetime plus
the retry backoff, and by `attempt_count <= 3`.

The W5 application payload carried by the producer contains only the canonical
job UUID; its producer wire shape is mechanically tested. Logs and Sentry
spans use the allowlist in `observability.py`.

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
- **A terminated holder still releases its lock.** Committing the acquire
  removes the `idle in transaction` reason for a backend to be killed, but any
  administrative `pg_terminate_backend`, an OOM kill or a server restart drops
  the lock the same way — see the second bullet above. This is the same
  extreme-tail window, not a new one, and the callback core's nonce, commit
  lease and idempotency key are what cover it.
- **Graceful enqueue shutdown is bounded, not magical.** Before the web
  process shuts its TaskIQ broker down it requests cancellation from any
  in-flight enqueue producers and gives cooperative tasks a short bounded reap
  window. A same-loop coroutine that perpetually catches `CancelledError`
  cannot be force-killed by Python and may still be in the strong registry at
  the end of that window. Do not claim a graceful zero-task exit in that case:
  the process supervisor's configured hard-stop / hard-kill fallback is the
  final containment mechanism.

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
