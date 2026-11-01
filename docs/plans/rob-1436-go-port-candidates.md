# Go port candidates (fillwire pattern) — ROB-1436

Status: evaluation for the operator. Nothing is ported in ROB-1436. Pattern under evaluation: a Go process that
holds a socket or poll loop, writes to its own Redis Stream, and enters auto_trader only through a
token-authed HTTP route into the service layer (no direct DB writes), as fillwire does today
(`docs/runbooks/execution-ledger-ingest.md:1-13`, fillwire `README.md:11-24`).

Citation key: `AT:` = this repo at the ROB-1436 branch, `FW:` = the fillwire repo
(local `main` at `8a5b52f`; newer commits seen on the
`fillwire.rob-1434` worktree are marked `FW-1434:`), `S90` = Sentry 90-day tools/call counts
(the operator's 90-day Sentry export for ROB-1436, 71 tools seen, sampled about 1/8, relative only),
`BRIEF` = operator brief, `LOCAL` = measured on this Mac in this session (not prod).

## Bottom line

The load is small and almost all I/O. S90 lists 15,200 sampled tool calls in 90 days. At about 1/8
sampling that is about 120k real calls, or about 1 call a minute (*inference from the sampling
ratio*). Every latency incident on record is network waits, rate-limiter sleeps or N+1 HTTP, and
each one was fixed or scoped in Python. A rewrite in Go does not shorten time spent waiting on a
broker. Go helps in three places: (a) long-lived socket holders, where memory, restart time and
broker session rules matter; (b) process count and memory, because each Python unit costs about
250 MB; (c) anything that must run all session on a tick stream. Only (a) has a fillwire-shaped
next step with clear evidence.

## Summary table

| Rank | Path | What Go would buy | Evidence | Confidence | Cost / risk |
|---|---|---|---|---|---|
| 1 | Finish fillwire KIS fills: add overseas `H0GSCNI0`, retire `at-kis-ws` | Closes a real coverage gap (US KIS fills have no websocket tap while fillwire holds the session); removes one Python container (same `websocket_monitor.py` graph, LOCAL import peak 216.3 MB) | One KIS websocket session per app key (FW-1434 `docs/digest-pin-deploy.md:158-163`); fillwire subscribes domestic only (FW `README.md:9`, `internal/reader/reader.go:229-231`); Python covers both (AT `app/services/kis_websocket_internal/protocol.py:1-4`) | High | Low to medium: same daemon, same ingest route; needs the overseas decoder and its parity tests |
| 2 | Upbit myOrder fill feed into fillwire (retire `at-upbit-ws`) | About 1 Python process less; one fill-ingest path for all live websocket brokers | Python monitor imports the whole app graph (AT `websocket_monitor.py:22-56`): LOCAL import peak 216.3 MB with pandas+numpy+fastmcp loaded. Fill rate is low, so CPU is not a factor | Medium | Medium: the process needs order-capable Upbit keys, so it must be shown to have no order code path |
| 3 | Quote reads served from `quotes:toss` (already Go) instead of per-call broker HTTP | Lower `get_quote` latency for symbols on the 40-symbol list; fewer KIS/Upbit REST calls | `get_quote`+`get_ohlcv`+`get_orderbook`+`get_support_resistance` are 32% of S90 (4,880 of 15,200). The Go reader exists and is off by default (FW `README.md:134-141`) | Medium (gain) / Low (it needs no new Go) | Low in Go, medium in Python. Risk: ticks are lossy (FW `README.md` "Lossy by design"); the change must keep the `data_state` honesty |
| 4 | Watch trigger evaluation on the tick stream (replace the 5-minute poll) | Trigger latency from minutes to seconds | Watch scan runs on `*/5` cron (AT `app/tasks/watch_scan_tasks.py:16-18`); #1120 exists to measure exactly this (AT `docs/design/1120-quotes-toss-shadow-consumer.md:1-20`) | Low until #1120 has data | Medium. Gain depends on the shadow result, and Python can consume the stream too |
| 5 | MCP unit process count / memory | Each closed-world unit could be a small Go proxy instead of a 250-600 MB Python process | BRIEF: about 250 MB baseline per unit, h3-us-paper about 600 MB after use; 30-day tools/call: h3-crypto-paper 1,168, kiwoom 365, h3-us-paper 281 vs default 11,732 | Medium on the problem; low on Go as the fix | High: re-implements tool logic and safety guards in a second language. ROB-1436 is already cutting units and making imports lazy |
| — | Not recommended: Telegram callback, order dispatch, Toss/KIS `place_order`, `toss_reconcile_orders`, `screen_stocks_snapshot`, `analyze_stock_batch` | Little or nothing | All I/O-bound or already DB-only; see "Not recommended" | High | High (safety contracts) |

## 1. Finish the KIS fills cutover (fillwire P1 leftovers)

**Current shape.** fillwire reads `go-kis` ws → decode → `XADD fills:kis` → one consumer → `POST
/trading/api/execution-ledger/fills/ingest` → `XACK` only on inserted/updated/unchanged (FW
`README.md:11-26`). The route, schema and shared service already exist (AT
`app/routers/execution_ledger_ingest.py`, `app/services/execution_ledger/fill_ingest.py`). fillwire
subscribes only the domestic pair: `EndpointLive`+`H0STCNI0`, `EndpointVTS`+`H0STCNI9` (FW
`README.md:9`, `internal/reader/reader.go:229-231`). The Python monitor also subscribes the
overseas pair `H0GSCNI0`/`H0GSCNI9` (AT `app/services/kis_websocket_internal/protocol.py:1-4`). KIS
allows one websocket session per app key, so `at-kis-ws` must be stopped while fillwire runs (FW-1434
`docs/digest-pin-deploy.md:158-178`), and deploys use `--skip-kis-ws` in that window (AT
`docs/runbooks/ncp-pull-deploy.md:112-114,247-254`).

**Evidence.** The gap is functional, not about performance. *Inference:* while fillwire holds the
session, KIS overseas fills reach the ledger only through reconcile. Reconcile runs every 30 minutes
(AT `app/core/config.py:1159`) or on `reconcile/trigger`. The acceptance-notice phantom fix
(`CNTG_YN=2` only) has landed in fillwire (FW-1434 log `6274b5a`, task 1172). That fix was the
precondition the #1175 quarantine contract named (AT
`docs/agent-contracts/broker-and-ledger-contracts.md` "Execution Ledger Quarantine").

**Why first.** Lowest cost, because the daemon, stream, sink, alerting and digest-pin deploy all
exist. The gain is a correctness fix, and it lets one Python container be deleted, not just
stopped.

**Preconditions.** `go-kis` support for the overseas execution TR; decoder parity tests against the
Python parser. That parser has KST handling for the H0GSCNI0 back-office clock (AT
`app/services/kis_websocket_internal/parsers.py:393`). US venue codes must match what the ROB-1379
supersede and #1173 US lots expect. The #180 observation window must close before `at-kis-ws` is
removed.

**Safety contracts to keep.** Hard rule 16 as a whole. Keep the token header semantics (403 when
unset, 401 when wrong). Keep the idempotency key `(broker, account_mode, venue, broker_order_id,
fill_seq)`. The server forces row `source=websocket`, and the outer `source_run_id` wins. Keep the
per-item savepoint, and keep the ledger commit as the authority. Never XACK a `rejected` item. Never
add order code (FW `README.md:5`; AT `tests/services/execution_ledger/test_no_broker_mutation.py`).
For hard rule 5: fills are evidence-first and go through the service layer only. ROB-1379
cross-source dedupe only works if `fill_seq`/order identity stays as it is now.

## 2. Upbit myOrder feed into fillwire

**Current shape.** `at-upbit-ws` runs `websocket_monitor.py --mode upbit` (AT
`scripts/deploy-ncp-pull.sh:385`). It is a 1,011-line monitor that imports settings, the DB session,
notifier, fill enrichment, `OrderProposalsService` and the KIS websocket client at module load (AT
`websocket_monitor.py:22-56`). Compose caps it at 256 MB / 0.25 CPU (AT
`docker-compose.prod.yml:180-187`). The NCP deploy runs it with no `--memory` flag (AT
`scripts/deploy-ncp-pull.sh:385`; `grep -c -- --memory` = 0), so *inference:* prod has no cap.
LOCAL one-off import probe (placeholder env, macOS `ru_maxrss`, which is noisy under memory pressure): importing
`websocket_monitor` alone peaks at **216.3 MB** and loads pandas, numpy, fastmcp and sqlalchemy
(2,497 modules), against 17.0 MB for a bare `import json`. For comparison: `app.main` 242.1 MB,
TaskIQ worker graph 165.2 MB. A fill reader carries pandas and fastmcp that it never uses, and on
this Mac its import peak already sits close to its own 256 MB compose cap. Linux RSS will differ.

**Evidence.** Fill volume is low. *Inference:* tens of fills a day. No count is in the repo; see
gaps. That puts the per-event CPU near zero, so the gain is memory plus one less copy of the app
graph to keep in sync. Downstream (notifications, rung projection) already moves to the API when
the sink is `http` (AT `docs/runbooks/execution-ledger-ingest.md:162-172`), so a Go reader would only
need decode, stream and POST.

**Why rank 2.** The move is cheap because it reuses fillwire, but the gain is modest: one process
that peaks at about 216 MB on import (LOCAL). Risk goes up because Upbit API keys are order-capable.

**Preconditions.** Measure prod RSS and fill counts first. Parity tests for the Upbit cumulative
`executed_volume` that rung projection depends on (send `raw_payload_json`; AT
`docs/runbooks/execution-ledger-ingest.md:81-86`). Keep the ROB-868 `trade`/`done` semantics
(AT `CHANGELOG.md:21-22`).

**Safety contracts.** Same as item 1, plus a static test. Like the Toss non-owner test (FW `README.md`
"Access token: read-only"), it should scan the Go tree for Upbit order endpoints (`/v1/orders` POST
or DELETE) and fail if any appears. Upbit JWT signing stays inside the reader.

## 3. Quote reads from `quotes:toss`

**Current shape.** `get_quote` resolves the market, then calls KIS (KR, plus NXT tradability, the
KRX-after fields and the NXT overlay), Upbit (crypto) or the US path, all per call (AT
`app/mcp_server/tooling/market_data_quotes.py:1653-1700`). fillwire already has a Toss websocket
quote reader for at most 40 symbols into `quotes:toss`. It is off by default and owns the account's
only allowed Toss connection (FW `README.md:134-156`). The #1120 Python shadow consumer reads that
stream (AT `app/jobs/quotes_consumer.py`, `docs/design/1120-quotes-toss-shadow-consumer.md`).

**Evidence.** The quote family is the largest share of calls: `get_quote` 1,976, `get_ohlcv` 1,952,
`get_support_resistance` 848, `get_orderbook` 104 sampled (S90). No latency percentile for
`get_quote` is in the repo.

**Why / why not.** The Go work is done. What is left is a Python read path (latest tick from Redis
with a freshness bound) plus a decision on honesty: lossy ticks, 40-symbol coverage, and no
`previous_close` on the stream. A new Go process adds nothing here.

**Safety contracts.** The only Toss websocket client stays fillwire; auto_trader must never open one
(FW `README.md:150-160`). Never issue a Toss token from Go: one valid token per client, so a new
token breaks order placement (FW `README.md` "Access token"). Keep `data_state` and the
`analyze quick` rule that quick output is not a live quote (AT
`docs/agent-contracts/market-data-and-screener-contracts.md` ROB-1311).

## 4. Watch triggers on the tick stream

**Current shape.** `scan.investment_watch_alerts` runs every 5 minutes on the single TaskIQ worker
(AT `app/tasks/watch_scan_tasks.py:16-18`; `--workers 1` at `scripts/deploy-ncp-pull.sh:384`).

**Evidence.** #1120 was ordered to measure trigger latency (minutes to seconds) and touch/fill
quality. It records only, and its four-week data is not in yet (AT
`docs/design/1120-quotes-toss-shadow-consumer.md:7-20`).

**Why not yet.** The latency comes from the polling design, not from Python. At 80 subscriptions,
the tick rate is within what an asyncio consumer handles (*inference; no tick-rate measurement*).
Decide after #1120 scores. If it moves to Go, Go should emit candidate events through a token-authed
route. Trigger evaluation and notification stay in the service layer, and the scheduler stays as it
is (hard rule 6).

## 5. MCP process count and memory

**Current shape.** Each MCP unit is a separate Python process from the same image (AT
`scripts/deploy-ncp-pull.sh:483`). Before ROB-1436 there were 10 fixed units plus default
blue/green (AT `git show HEAD:scripts/deploy-ncp-pull.sh:106-107`). ROB-1436 cuts that to 3
(`kiwoom h3-crypto-paper h3-us-paper`; AT `scripts/deploy-ncp-pull.sh:107-109`)
and defers pandas, numpy, yfinance and the calendar packages on the low-traffic units (AT
`app/mcp_server/lazy_imports.py:1-24,39-56`).

**Evidence.** BRIEF: about 250 MB per unit, h3-us-paper about 600 MB after use. LOCAL (macOS
physical footprint): pandas +47.5 MB and yfinance +22.2 MB over a 6.9 MB bare interpreter. LOCAL (`scripts/mcp_profile_import_rss.py --compare --repeat 3`,
macOS physical footprint): h3-us-paper, kiwoom and h3-crypto-paper all boot at about 221 MB eager and
175 MB lazy, and return to about 221 MB after first use (AT `docs/runbooks/mcp-unit-retirement-rob1436.md` §4).
Three of those profiles in one process come to about 224 MB (AT `docs/plans/rob-1436-mcp-single-process-evaluation.md`).
These are tool-registration footprints. The BRIEF's 250-600 MB
in prod includes request-time state that this probe does not exercise. Compose caps MCP at 512 MB (AT
`docker-compose.prod.yml:118-125`), but the NCP deploy sets no caps.

**Why not Go.** The tool bodies are the product, and they are what the Python footprint pays for. A Go MCP
front would have to reimplement closed-world registration, the credential firewall and the route
contracts (AT `docs/agent-contracts/mcp-and-analysis-contracts.md` h3 sections). Those contracts
are pinned by snapshot tests that only exist in Python. Cheaper levers: finish ROB-1436, then
consider one process serving several closed-world profiles (evaluated in
AT `docs/plans/rob-1436-mcp-single-process-evaluation.md`).

## Evidence gaps and how to close them

| Gap | Why it matters | Measurement that closes it |
|---|---|---|
| No prod RSS per container | Items 2 and 5 rest on BRIEF and LOCAL numbers only | `docker stats --no-stream` on the NCP host for `at-*` and `fillwire` over one trading day (peak and steady) |
| No fillwire RSS/CPU number | The memory gain from items 1 and 2 is assumed (*inference: tens of MB for a static Go binary*) | Same `docker stats` while fillwire holds the session |
| No CPU profile anywhere in the repo | Claims that Go buys no CPU rest on span data (http.client-dominated), not on profiles | `py-spy record` on `at-mcp-*` and `at-worker` for 10 minutes at the KR open; `py-spy dump` during a screener snapshot build |
| No fill-rate counts per broker | Sizes item 2 | `SELECT broker, account_mode, date_trunc('day', filled_at), count(*) FROM review.execution_ledger GROUP BY 1,2,3` (read-only, operator) |
| No `get_quote`/`get_ohlcv` latency percentiles | Sizes item 3 | Sentry: p50/p95 of `tools/call` spans by `gen_ai.tool.name`, plus child `http.client` share |
| No tick rate on `quotes:toss` | Sizes item 4 | `XLEN` delta per minute during KR and US windows (FW `README.md` "Verifying") |
| Single-worker event-loop stalls unknown | 65 tasks share one worker (`grep -c broker.task app/tasks` = 65); one CPU-heavy task could delay callback jobs (*inference*) | Sentry queue-delay on TaskIQ spans; `py-spy dump` when a delay is seen |
| Sentry counts are sampled about 1/8 | Ranks only, not absolute load | Read unsampled tool-call counts from `tool_call_log_middleware` logs if they are persisted |

## Not recommended

- **Telegram approval callback** (p50 2.738 s, p95 12.707 s, n=44; `http.client` 359 spans / 86.90 s
  vs DB 3,106 spans / 4.47 s — AT `docs/runbooks/telegram-callback-durable-inbox.md:26-41`). The
  time is broker and Telegram I/O. The fix is the W5 durable inbox (target p95 ≤ 1 s, same file
  `:474`). Porting it would have to reproduce hard rule 11 and the retry rules in Go. Highest risk,
  no gain.
- **Order dispatch / `place_order` (Toss, KIS, Upbit) / order_proposals.** About 504 sampled
  mutation-type calls in 90 days (S90, sum of place/submit/cancel/create/reconcile tools). They are
  I/O-bound and carry hard rules 4, 7 and 14, approval-hash and idempotency contracts. A second
  implementation doubles the audit surface. broker-edge and gatewayd already isolate credentials
  where needed (AT `docs/runbooks/broker-token-ownership.md`, `app/services/brokers/edge_client.py`).
- **`toss_reconcile_orders`** (avg 26.3 s, p95 82.5 s; about 78% is time asleep in the ACCOUNT 1-TPS
  rate limiter — AT `docs/plans/ROB-687-toss-reconcile-account-seq-nplus1.md:5`). The cause was an
  N+1 against a broker rate limit. Go hits the same limit.
- **`screen_stocks_snapshot`** (p50 38.11 s, p95 54.73 s, about 214 HTTP calls per call — AT
  `docs/agent-contracts/market-data-and-screener-contracts.md:96`). ROB-1309 made it DB-only with 0
  HTTP calls. It does not appear in the S90 list at all (`screen_stocks` = 136).
- **`analyze_stock_batch(quick=True)`**: DB-only, at most 12 DB executions per batch, 0 HTTP calls
  (ROB-1311 contract). The Wilder RSI on 250 bars is microseconds of CPU (*inference*).
- **`/invest/api/market-parity`** (`http.client` about 76% — AT
  `docs/plans/ROB-689-market-parity-serial-cards.md:5`): fixed with `asyncio.gather`.
