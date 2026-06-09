# ROB-469 — auto_trader MCP server resilience (PR1 + PR2 + PR3)

- **Issue:** ROB-469 `[Bug/Infra] auto_trader MCP 세션 중 연결 끊김 (128 tools 일괄 다운, get_news 포함) — 자동 재연결·뉴스 폴백 부재`
- **Date:** 2026-06-09
- **Status:** Design approved (pre-implementation)
- **Author:** Hermes (with code-grounded multi-agent investigation)
- **Scope decision:** PR1 (Observe + Detect) → PR2 (Harden the loop) → PR3 (Self-heal watchdog). `get_news` fallback (proposal #3) and the CDP read-only degrade guide (proposal #4) are **deferred**.

---

## 1. Problem

During a live trading session (2026-06-09), the **entire auto_trader MCP server connection dropped** and all **128 tools became unavailable simultaneously** (`mcp__auto_trader_local__* (128) no longer available — MCP server disconnected`). The outage was **sustained**, not a transient blip. The CDP (9222 Naver) fallback could read quotes/flows/community but **not** news headlines.

The server is a single **FastMCP `streamable-http`** uvicorn process (`app/mcp_server/main.py`), default port 8765 / path `/mcp`, that the Claude Code harness connects to over HTTP. In production it runs via the **native launchd blue/green** path (`scripts/deploy-native.sh`): ports 8766 (blue) / 8767 (green) → HAProxy → stable 8765. (`docker-compose.prod.yml` is the legacy path, retired by ROB-263.)

### 1.1 The honest reframing (premise correction)

ROB-469's headline ask is *"MCP 자동 재연결"* (auto-reconnect). **True in-session client auto-reconnect is NOT achievable in this repo** — it is the Claude Code harness/MCP-client's responsibility; the server cannot force a dropped client to re-establish a session or transparently retry the in-flight call. What this repo *can* own is the **server-side half** of resilience: don't wedge in the first place, be detectable when it does, be observable about *why*, and be supervised so the harness has something healthy to reconnect to. Proposal #1 is therefore re-scoped to **server-side resilience + supervision + a health endpoint**, not literal reconnect.

### 1.2 Root-cause assessment (ranked, code-grounded)

"All 128 down at once" means the **single uvicorn event loop stopped servicing everything** (a per-tool fault would not take everything down). Ranked likely causes:

1. **MOST LIKELY — event-loop wedge.** There is **no per-tool timeout** today (`graceful_shutdown_timeout=10` is process-level only). One slow tool blocks the single worker → all tools stall. Confirmed hazards:
   - `app/core/db.py:13-18` uses `poolclass=NullPool` → a fresh DB connection is opened per request; under load the connect itself awaits and piles up.
   - `app/mcp_server/tooling/portfolio_holdings.py:995-1001` — unbounded `asyncio.gather` over crypto positions (task explosion on large portfolios); also `:668` equity-price gather.
   - heavy pandas/indicator compute runs **on the event loop** without `run_in_executor` offload.
   - `redis_max_connections=10` is a tight shared ceiling.
2. **PLAUSIBLE — process died/OOM'd** and was restarted; the in-flight session was already severed. launchd `KeepAlive=true` restarts on **exit**, but the harness session is broken regardless.
3. **PLAUSIBLE — hung-but-alive loop that supervision can't catch today.** launchd `KeepAlive` restarts only on process **exit**; `docker-compose.prod.yml` mcp service has **no** healthcheck; the only `/mcp`→401/400 probe runs at **deploy time**, not continuously.
4. **LESS LIKELY / unverifiable in-repo — transport/SSE idle drop** (harness-side to recover).

**We cannot retro-diagnose which one fired** — there is no shutdown-cause logging today (one startup `logging.info` + a top-level `except`). That gap is itself the strongest reason to **land observability first**.

---

## 2. Goals / Non-goals

### Goals
- Make a wedged/crashed MCP server **detectable** (continuous health signal) and the next incident **diagnosable** (lifecycle logging).
- **Reduce the frequency** of event-loop wedges (per-tool timeout for I/O-bound tools; bounded fan-out; bounded DB/redis pools).
- Provide a **recovery path** for a hung-but-alive process (self-heal watchdog).
- Keep production safe: additive, env-gated where blast radius is wide, **zero DB migration**, no broker/order mutation.

### Non-goals (explicit)
- **Literal in-session client auto-reconnect** — harness's job.
- **External alerting/paging** (Prometheus/PagerDuty heartbeat) — deployment/orchestration layer.
- **`get_news` fallback** (proposal #3) — deferred; **DB-backed fallback** (read recent `NewsArticle` rows the ingestor already populates) is the recorded approach if/when revived.
- **Proposal #4 CDP read-only degrade guide** — docs-only sub-issue, needs a concrete trigger + gated tool-set definition first.
- **Retro-diagnosing the 2026-06-09 incident** — impossible without the logging PR1 adds.
- **Multi-worker uvicorn** — risky with streamable-http session affinity + blue/green ports; a deployment decision, not an in-repo quick fix.

### Honest limitation (stated, not hidden)
`asyncio.wait_for` **cannot cancel a synchronous blocking call** (heavy pandas with no `await`, a blocking C call). The per-tool timeout fixes **I/O-bound** wedges (the majority — Naver scrape / Finnhub / KIS awaiting) but **not** a true sync-blocking wedge. That is precisely why PR3's watchdog (recover) and offloading heavy compute to threads (follow-up) are the backstop. The spec does not oversell the timeout as a total fix.

---

## 3. Architecture: prevent → detect → recover → diagnose

| Layer | Mechanism | PR | Covers |
|---|---|---|---|
| Prevent (I/O wedge) | per-tool timeout middleware (cancels awaiting tools) | PR2 | common case |
| Prevent (resource) | bound hot-path `gather`s; `NullPool→QueuePool`; redis tuning | PR2 | task explosion / conn exhaustion |
| Detect | dependency-free `/health` on the loop (HAProxy `inter 5s`) + lifecycle logs | PR1 | any wedge/crash becomes visible |
| Recover | launchd `KeepAlive` (clean exit/crash) + heartbeat watchdog → `launchctl kickstart -k` (hung-but-alive) | launchd today / PR3 | self-healing incl. hard sync-wedge |
| Diagnose | lifespan startup/shutdown logs + Sentry — presence/absence of shutdown log = graceful vs hard-kill/OOM | PR1 | root-cause the next incident |

---

## 4. PR1 — Observe + Detect (ships first; low-risk; no new process)

### 4.1 `/health` route — `app/mcp_server/main.py`
Add after `register_all_tools(...)` and before `main()`/`mcp.run()`:

```python
from starlette.requests import Request
from starlette.responses import JSONResponse

@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse(
        {"status": "ok", "service": "auto-trader-mcp",
         "version": "0.1.0", "uptime_s": round(time.monotonic() - _STARTED_MONOTONIC, 1)}
    )
```

- **Unauthenticated by construction** (verified against fastmcp 3.2.0 source): app-level `AuthenticationMiddleware` only *populates* auth context; only the `/mcp` route is wrapped in `RequireAuthMiddleware` (`fastmcp/server/http.py:336`); `custom_route` appends to `_additional_http_routes` mounted separately (`transport.py:100-148`, `http.py:357-358`). So `GET /health` → 200 even when `MCP_AUTH_TOKEN` is set.
- **Dependency-free** — no DB / Redis / broker calls. A `/health` served on the same event loop is itself a liveness/wedge signal: if the loop is wedged, `/health` stops responding and HAProxy (`inter 5s`) marks the backend down. It must never hang on a backend blip.
- `_STARTED_MONOTONIC = time.monotonic()` captured at module import.

### 4.2 Lifecycle logging — `app/mcp_server/main.py`
Use the FastMCP lifespan (teardown is the safe shutdown hook; **do not** call `signal.signal()` — uvicorn's `capture_signals()` overrides custom handlers, verified in `uvicorn/server.py:322-340`). FastMCP `_lifespan_manager` shields teardown from `CancelledError` (`fastmcp/server/mixins/lifespan.py:140-190`).

```python
from fastmcp.server.lifespan import lifespan as fastmcp_lifespan

@fastmcp_lifespan
async def server_lifespan(server):
    logging.info("MCP server startup complete: tools=%d transport=%s ...", tool_count, mcp_type, ...)
    # optional Sentry breadcrumb / message: "mcp.lifecycle.startup"
    try:
        yield {}
    finally:
        logging.info("MCP server shutdown initiated (graceful) uptime_s=%.1f", time.monotonic() - _STARTED_MONOTONIC)
        # optional Sentry message: "mcp.lifecycle.shutdown"

mcp = FastMCP(..., lifespan=server_lifespan)
```

- Add a **structured startup log** (tool count, transport, host/port, graceful-timeout, auth on/off) in `main()`.
- **Enrich the existing top-level `except`** (`main.py:116-124`) so an unhandled `mcp.run()` exception is tagged distinctly from a graceful shutdown.
- **Diagnosis property:** *startup logged → shutdown logged* ⇒ graceful. *startup logged → (no shutdown) → new startup* ⇒ hard-kill/OOM/SIGKILL (teardown never ran). This presence/absence pair is the crash-vs-graceful signal we lack today.

### 4.3 Switch health probes to `/health` → 200
Native is authoritative; keep the old `/mcp`→401/400 probe as a **commented fallback** for one release.
- `ops/native/scripts/healthcheck-native.sh:54-58` — probe `http://127.0.0.1:${MCP_PORT}/health`, expect `200` (drop the `Accept: text/event-stream` header).
- `ops/native/haproxy/haproxy.cfg.tmpl:43-55` (`backend bk_mcp`) — `option httpchk GET /health` + `http-check expect status 200`.
- `scripts/deploy-native.sh:~181` — built-in fallback probe → `/health`/200.
- `native_deploy_lib.sh` `probe_color_direct` / `probe_public_stable` — **no change** (data-driven by `healthcheck-native.sh`); just verify the 24×5s window covers MCP startup.
- `docker-compose.prod.yml` mcp service (~lines 89-111) — **add** the missing healthcheck block mirroring `api` (`curl -sf http://127.0.0.1:8765/health`, interval 30s, timeout 10s, retries 3, start_period 40s).

### 4.4 Runbook
`docs/runbooks/mcp-health-supervision.md`: `/health` purpose, probe interpretation, manual check, restart per path (`launchctl kickstart -k gui/$uid/com.robinco.auto-trader.mcp-<color>` / `docker compose restart mcp`), Sentry filter `service:auto-trader-mcp`, blue/green + launchd specifics.

### 4.5 PR1 tests
- `GET /health` returns **200 unauthenticated** even with `MCP_AUTH_TOKEN` set, and `{status:"ok"}`; payload dependency-free (no DB/Redis touched).
- startup + shutdown lifecycle logs emitted (capture via caplog around lifespan enter/exit).
- `healthcheck-native.sh` parses a 200 as healthy (bash test or a small harness).

---

## 5. PR2 — Harden the loop (the real SPOF fix)

### 5.1 `TimeoutMiddleware` (per-tool timeout)
New `app/mcp_server/timeout_middleware.py` subclassing `fastmcp.server.middleware.Middleware`, implementing `on_call_tool`:

```python
from fastmcp.exceptions import ToolError

class ToolTimeoutMiddleware(Middleware):
    async def on_call_tool(self, context, call_next):
        tool = context.message.name
        budget = _budget_for(tool)              # default 45s + elevated/exempt map
        if budget is None:                      # exempt → no timeout
            return await call_next(context)
        try:
            return await asyncio.wait_for(call_next(context), timeout=budget)
        except asyncio.TimeoutError:
            raise ToolError(f"{tool} exceeded {budget:g}s budget") from None
```

- **Registration order (CRITICAL — verified against `fastmcp/server/server.py:448-451`):** the chain is built `chain = tool; for mw in reversed(self.middleware): chain = partial(mw, call_next=chain)`. With append-order `[Sentry, CallerIdentity]`, execution is `Sentry(outer) → CallerIdentity → tool`, i.e. **first-added = OUTERMOST**. To have the timeout (a) wrap the tool and (b) raise its `ToolError` *inside* the Sentry scope, **add `ToolTimeoutMiddleware` LAST** (innermost):
  ```python
  mcp.add_middleware(McpToolCallSentryMiddleware())   # outermost — captures the timeout error w/ context
  mcp.add_middleware(CallerIdentityMiddleware())
  mcp.add_middleware(ToolTimeoutMiddleware())          # innermost — wraps the tool
  ```
  > The integration-mapping agent recommended adding it *first*; that is **wrong** and would put the timeout outside the Sentry scope. Source-verified correction.
- **Budgets** — default **45s**; generous **elevated/exempt** map (env-overridable), kept generous to avoid killing legitimate slow tools (per the "exempt heavy tools" choice):
  - `analyze_stock_batch`, `analyze_portfolio`, `screen_stocks` → 120s
  - `screen_stocks_snapshot` → 90s
  - `get_holdings` → 120s (crypto-signal + price fan-out)
  - `get_financials`, `get_company_profile` → 90s
  - `get_indicators` → 75s
  - `kis_live_reconcile_orders`, `live_reconcile_orders`, kis_mock reconcile → 60s
  - `investment_report_generate_from_bundle`, `investment_report_prepare_bundle`, hermes composition → **exempt (None)** or 240s
  - Map lives in a constant + env overrides (`MCP_TOOL_TIMEOUT_DEFAULT_S`, `MCP_TOOL_TIMEOUT_OVERRIDES`).
- **Behavior:** a timeout raises `ToolError` → clean MCP error for that one call; the server and the other 127 tools stay up. (Caveat from §2: only cancellable for tools blocked on `await`.)

### 5.2 Bound hot-path fan-outs
- `app/mcp_server/tooling/portfolio_holdings.py:995-1001` — wrap the crypto-signals `gather` with `asyncio.Semaphore(4)` (each position does OHLCV I/O + voting-signal compute). Mirror the existing `analysis_tool_handlers.py` `Semaphore(5)` pattern.
- `app/mcp_server/tooling/portfolio_holdings.py:~668` — bound the equity-price `gather` with `Semaphore(5)` (KIS/yfinance rate limits).
- Colder fan-outs (`fundamentals_sources_yfinance.py:607`, `fundamentals/_market_index.py:73`, `screening/enrichment.py:116/331`, `market_data_indicators.py:449`) — noted as **opportunistic follow-up**, not in PR2, to keep the diff focused (those tools also get elevated timeouts).

### 5.3 `NullPool → QueuePool` (env-gated; widest blast radius)
`app/core/db.py` — the engine is **shared** by API + MCP + workers + scheduler across ~90 call sites; each is a **separate process with its own engine instance**. Investigation confirmed: **no pgbouncer/pooler** (direct `localhost:5432`), clean `async with AsyncSessionLocal()` lifecycle, no forking. QueuePool is "highly likely safe."

```python
from sqlalchemy.pool import NullPool, QueuePool

_pool_class_name = os.getenv("DB_POOL_CLASS", "queue").lower()   # "queue" (default) | "null"
if _pool_class_name == "null":
    engine = create_async_engine(settings.DATABASE_URL, echo=_echo, pool_pre_ping=True, poolclass=NullPool)
else:
    engine = create_async_engine(
        settings.DATABASE_URL, echo=_echo, pool_pre_ping=True, poolclass=QueuePool,
        pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
        pool_recycle=int(os.getenv("DB_POOL_RECYCLE_S", "1800")),
        pool_timeout=int(os.getenv("DB_POOL_TIMEOUT_S", "10")),
    )
```

- **Default = QueuePool** with conservative sizing + `pool_pre_ping` + `pool_recycle`. **`DB_POOL_CLASS=null` is an instant rollback.**
- Document `DB_POOL_*` in `env.example` / `env.prod.example`.
- This is the **lower-confidence, hardest-to-review** change — flag it clearly for reviewer + a prod load-check; it can be pulled out of PR2 without affecting the other items.

### 5.4 Redis tuning
- `app/core/config.py` Redis block — `redis_max_connections` 10 → 20; keep `redis_socket_timeout` (fail-fast); optionally add a pool-get timeout. Conservative, low-risk.

### 5.5 PR2 tests
- A deliberately-slow tool (sleeps past its budget) → `ToolError` raised; **other tools still respond** (server not wedged); error captured within Sentry scope (middleware order asserted).
- An exempt/elevated tool gets its higher budget (does not time out under its budget).
- `Semaphore` bounds observed concurrency on the crypto/equity gathers (monkeypatch a counting fetch).
- QueuePool engine builds, round-trips a query; `DB_POOL_CLASS=null` falls back to NullPool.

---

## 6. PR3 — Self-heal watchdog (recover a hung-but-alive process)

The only mechanism that recovers a **hard sync-wedge** (which §2's caveat and launchd `KeepAlive` cannot). Mirrors the existing `websocket_monitor.py` heartbeat precedent (`WS_MONITOR_HEARTBEAT_PATH` + atomic temp-file→rename).

- **Heartbeat writer (app):** a small asyncio task started in the MCP lifespan that writes `{updated_at_unix, color, is_running}` to `MCP_HEARTBEAT_PATH` every N seconds (atomic write). If the loop wedges, the heartbeat goes stale — that staleness is the wedge signal.
- **Watchdog (ops):** new `ops/native/scripts/mcp-watchdog.sh` — loops every ~10–15s, reads both blue/green heartbeat files, and if a file is stale (> 2–3× interval) calls `launchctl kickstart -k gui/$uid/com.robinco.auto-trader.mcp-<color>` to force-restart the wedged-but-alive process.
- **Plist (ops):** new `ops/native/plists/com.robinco.auto-trader.mcp-watchdog.plist` — single non-color-specific instance monitoring both colors; `KeepAlive=true`.
- **Deploy wiring:** add the watchdog label to `deploy-native.sh` `SINGLE_ACTIVE_LABELS` + `restart_single_active_services()`; export `MCP_HEARTBEAT_PATH` from `ops/native/scripts/run-mcp.sh`.
- **Guards against flapping:** heartbeat must be written at startup before the main loop (avoid restart-loop on slow start); stale threshold ≥ 3× interval to absorb startup jitter; respect `ThrottleInterval`.
- **Tests:** stale heartbeat → watchdog emits a kickstart for the correct color; fresh heartbeat → no action; atomic write under termination.

---

## 7. Rollout / operator steps
- **No DB migration** anywhere in PR1/PR2/PR3.
- **PR1:** deploy `/health` route **first**, verify `GET /health`→200 on 8766/8767/8765, then the HAProxy/healthcheck config switch (so the probe change never precedes the endpoint).
- **PR2:** ships QueuePool default-on (rollback `DB_POOL_CLASS=null`); operator load-checks DB pool under prod concurrency; timeout budgets tunable via env without redeploy of code semantics.
- **PR3:** install watchdog plist via the native deploy flow; confirm a synthetic stale heartbeat triggers a kickstart in staging.

## 8. Risks & mitigations
- **Probe change ordering** — endpoint must exist before HAProxy/healthcheck expect 200; mitigation: PR1 sequencing + commented `/mcp` fallback for one release.
- **QueuePool sizing** — too small → queueing; too large → memory; mitigation: conservative env-tunable defaults + instant `DB_POOL_CLASS=null` rollback + load-check.
- **Timeout too aggressive** — could kill legit slow tools; mitigation: generous default + elevated/exempt map + env overrides; report-gen exempt.
- **Watchdog flapping** — restart loop on slow start; mitigation: startup-first heartbeat, ≥3× stale threshold, throttle.
- **Sync-blocking wedge** — timeout can't cancel it; mitigation: PR3 watchdog recovers; offloading heavy pandas to `run_in_executor` is a noted follow-up.

## 9. Out-of-scope follow-ups (tracked, not built here)
- `get_news` DB-backed fallback (proposal #3).
- CDP read-only degrade guide (proposal #4) — needs trigger + gated tool-set definition.
- Offload heavy pandas/indicator compute to `run_in_executor`.
- Bound the colder screener/fundamentals `gather`s.
- External alerting/heartbeat (Prometheus/Sentry cron) at the orchestration layer.
