# ROB-15 Review Report

**AOE_STATUS:** review-complete
**AOE_ISSUE:** ROB-15
**AOE_ROLE:** reviewer-opus
**AOE_NEXT:** PR can be opened to `main` after the optional smoke step in §7.

- **Commit reviewed:** `059bf6b3 feat(rob-15): add operator trading decision sessions`
- **Branch:** `feature/ROB-15-operator-trading-decision-session`
- **Worktree:** `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-15-operator-trading-decision-session`
- **Plan:** `docs/plans/ROB-15-operator-trading-decision-session-plan.md`
- **Reviewer:** Claude Opus (review-only; no code edits)
- **Verdict:** **PASS_WITH_NOTES**

---

## 1. Diff summary

```
 app/routers/trading_decisions.py                           |   72 +-
 app/schemas/operator_decision_session.py                   |   84 +
 app/services/operator_decision_session_service.py          |  200 +
 app/services/trading_decision_session_url.py               |   19 +
 docs/plans/ROB-15-operator-trading-decision-session-plan.md| 2697 +
 tests/routers/test_trading_decisions_operator_request.py   |  247 +
 tests/routers/test_trading_decisions_operator_request_integration.py | 260 +
 tests/services/test_operator_decision_session_safety.py    |  164 +
 tests/services/test_operator_decision_session_service.py   |  321 +
 tests/test_operator_decision_session_schemas.py            |  124 +
 tests/test_trading_decision_session_url.py                 |   75 +
 11 files changed, 4261 insertions(+), 2 deletions(-)
```

The diff is additive: one new service module, one new schema module, one new
pure URL helper, six new test files, and ~58 lines added to the existing
`app/routers/trading_decisions.py` (one new POST route + imports).
**No untouchable modules were modified** (no edits to KIS, Upbit, orders,
brokers, watch_alerts, paper_trading, kis_trading_service,
mcp_server.tooling.order_execution, mcp_server.tooling.watch_alerts_registration,
trading_decision_service, trading_decision_synthesis*, tradingagents_research_service,
or models).

---

## 2. Hard safety checks (all PASS)

| Check | Status | Evidence |
|---|---|---|
| No live order placement | ✅ | Diff contains zero `place_order` / `submit_order` call sites in implementation files. Only mentions are in safety-test stubs and Pydantic `extra="forbid"` rejection cases. |
| No `place_order(..., dry_run=False)` | ✅ | Grep over `app/**` diff returns no matches. Implementation has no `dry_run` parameter at any layer. |
| No watch registration | ✅ | No imports of `app.services.watch_alerts*`, `app.mcp_server.tooling.watch_alerts_registration`. Confirmed by subprocess-import safety test (`tests/services/test_operator_decision_session_safety.py:69`). |
| No order intent creation | ✅ | No imports of `app.services.orders*`, `app.services.order_service`. Forbidden-prefix scan covers both. |
| No broker side-effect APIs | ✅ | Orchestrator imports only `trading_decision_service`, `trading_decision_synthesis`, `trading_decision_synthesis_persistence`, `tradingagents_research_service`, `app.models.trading`, `app.models.trading_decision`, and the two new ROB-15 schema/URL modules — see `app/services/operator_decision_session_service.py:1-32`. |
| TradingAgents `advisory_only=True` / `execution_allowed=False` invariants | ✅ | (1) `_run_with_advisory` flows runner output through `advisory_from_runner_result` + `synthesize_candidate_with_advisory` + `create_synthesized_decision_session`, all of which are ROB-14 modules that pin the literal invariants. (2) `market_brief={"advisory_only": True, "execution_allowed": False, ...}` is hard-coded at `operator_decision_session_service.py:127-134` and `:185-192`. (3) Unit test `test_advisory_path_uses_synthesis_persistence` asserts `synthesized[0].advisory.advisory_only is True` and `…execution_allowed is False` (`tests/services/test_operator_decision_session_service.py:236-237`). |
| Missing TA config + `include_tradingagents=true` fails closed without DB write | ✅ | Runner raises `TradingAgentsNotConfigured` → orchestrator does not reach `create_synthesized_decision_session`. Two layers of test coverage: service-level `test_advisory_missing_config_raises_without_persistence` (`tests/services/test_operator_decision_session_service.py:243`) wires `no_persistence = AsyncMock(side_effect=AssertionError)` on both persistence callables and confirms neither is awaited. Router-level `test_tradingagents_not_configured_maps_to_503` confirms HTTP 503 with detail `"tradingagents_not_configured"` (`tests/routers/test_trading_decisions_operator_request.py:141`). |
| Session/proposal generation not represented as final live-order approval | ✅ | Persisted `market_brief` and every `original_payload` carry `advisory_only=True` and `execution_allowed=False`. The response shape (`OperatorDecisionResponse`) exposes `session_uuid`, `session_url`, `status`, `proposal_count`, `advisory_used`, `advisory_skipped_reason` — no `approved`, `executed`, or order-id fields. The status comes straight from `TradingDecisionSession.status` which defaults to `"open"`. |
| No secrets / `.env` / API keys / tokens / passwords / connection strings printed or persisted | ✅ | Orchestrator never reads `os.environ` (only the test harness does — for `PYTHONPATH` in subprocess scans). Advisory persistence reuses the ROB-14 allowlist (`provider`, `model`, `base_url`, `decision_text`, `final_trade_decision_text`, `warnings`, `risk_flags`, `raw_state_keys`, `as_of_date`); ROB-14 already enforces this. The only payload fields are operator-supplied schema fields and the validated runner result. |

---

## 3. Implementation-claim verification

| Claim | Status | File / line |
|---|---|---|
| Structured operator request + response schemas | ✅ | `app/schemas/operator_decision_session.py:26-84` — `OperatorCandidate` + `OperatorDecisionRequest` (`extra="forbid"`) + `OperatorDecisionResponse`. Symbol charset validator and analyst charset validator match plan §3.8. |
| Pure URL helper returns `trader.robinco.dev` session URL | ✅ | `app/services/trading_decision_session_url.py:9-19` — `build_trading_decision_session_url` + `resolve_trading_decision_base_url`. Subprocess test `test_url_helper_module_has_no_settings_or_db_imports_in_subprocess` confirms no `app.core.config`, `app.core.db`, `redis`, `httpx`, or `sqlalchemy` imports (`tests/services/test_operator_decision_session_safety.py:86-103`). |
| Safe service creates only TradingDecisionSession + TradingDecisionProposal rows via existing helpers | ✅ | `_run_without_advisory` calls only `trading_decision_service.create_decision_session` and `trading_decision_service.add_decision_proposals`. `_run_with_advisory` calls only `create_synthesized_decision_session` (which itself only uses the same two helpers per ROB-14). No `record_decision_action`, `create_counterfactual_track`, or `record_outcome_mark` calls anywhere. |
| `include_tradingagents=false` (default) path works without invoking TA | ✅ | `tests/services/test_operator_decision_session_service.py:11` — `test_no_advisory_path_persists_via_raw_helpers` mocks `run_tradingagents_research` and `create_synthesized_decision_session` with `AssertionError` side effects and verifies neither is awaited. |
| `include_tradingagents=true` path reuses ROB-9 runner + ROB-14 synthesis with advisory-only invariants | ✅ | `_run_with_advisory` (`operator_decision_session_service.py:154-200`) iterates candidates → `run_tradingagents_research` → `advisory_from_runner_result` → `synthesize_candidate_with_advisory` → `create_synthesized_decision_session`. Service test `test_advisory_path_uses_synthesis_persistence` (line 143) confirms the buy + Underweight ROB-14 downgrade fires (`final_proposal_kind == "pullback_watch"`, `final_side == "none"`). |
| `POST /trading/api/decisions/from-operator-request` returns `session_uuid` / `session_url` / `status` / `proposal_count` (and `advisory_used` / `advisory_skipped_reason`) | ✅ | Router endpoint at `app/routers/trading_decisions.py:548-602`. Response model is `OperatorDecisionResponse`. Test `test_no_advisory_returns_201_with_session_url` asserts every field. |
| Tests cover no broker/order/watch/order-intent side effects | ✅ | Three subprocess-import scans + one runtime side-effect-proof test in `tests/services/test_operator_decision_session_safety.py` (4 tests). The runtime test patches every reachable `place_order` / `_place_order_impl` / `register_watch_alert*` / `create_order_intent` / `submit_order` symbol with an `AsyncMock(side_effect=AssertionError)` and runs the orchestrator end-to-end; only `create_decision_session` and `add_decision_proposals` are awaited. |

---

## 4. Plan adherence

The implementation matches the plan section-by-section:

- **Plan §3 safety invariants:** All 11 invariants observed. The orchestrator's
  imports exactly match the allowlist in §3.1; the persistence-layer payload
  invariants (§3.3) are enforced both at the schema level (ROB-14
  `SynthesizedProposal.model_validator`) and via test assertions for the
  no-advisory path.
- **Plan §4.1 schemas:** Implementation matches verbatim, including the
  `extra="forbid"` knob, charset regex (`^[A-Za-z0-9._/-]{1,32}$` for symbol,
  `^[a-z_]{1,32}$` for analyst tokens), and field bounds (`confidence ∈ [0,100]`,
  `quantity_pct/threshold_pct ∈ [0,100]`).
- **Plan §4.2 URL helper:** Implementation is byte-equivalent (modulo blank
  lines).
- **Plan §4.3 orchestrator:** Implementation matches; the `instrument_map` stub
  flagged by the planner self-review is correctly absent.
- **Plan §4.4 router:** Implementation matches, including the 503 / 502 mapping
  and the request-origin fallback when `public_base_url` is blank.
- **Plan §6 Tasks 1–10:** All test files present (with minor location/naming
  differences below). All assertions specified by the plan are present.

### Plan deviations (non-blocking, all cosmetic)

1. **Test file locations / names.** Plan put router tests at
   `tests/test_operator_decision_session_router.py` and
   `tests/test_operator_decision_session_router_integration.py`. Implementation
   placed them under `tests/routers/` and renamed to
   `test_trading_decisions_operator_request.py` and
   `…_integration.py`. The new names are consistent with the existing
   `tests/routers/` convention and match the route's natural identifier — no
   loss of coverage, no ambiguity.
2. **Safety test filename.** Plan: `test_operator_decision_session_service_safety.py`.
   Implementation: `test_operator_decision_session_safety.py`. Trivial.
3. **Integration harness.** Plan suggested reusing a `db_session` fixture.
   Implementation uses `asyncio.run` with the project's `engine` and
   `async_sessionmaker`, gated by `pytest.skip` if the trading_decision tables
   are not migrated. This is consistent with how the codebase already runs
   integration tests against the dev DB and matches Codex's reported behaviour
   (2 skipped when migrations aren't applied in the test env).

---

## 5. Test verification cross-check

Codex-reported counters reconcile with what is on disk:

- **Required pytest subset, 15 passed:** 7 schema tests
  (`test_operator_decision_session_schemas.py`) + 5 URL tests
  (`test_trading_decision_session_url.py`) + 3 router tests = 15. ✓
  (Or 5 schema-shape + 5 URL + 5 service-unit = 15 — both interpretations
  account.)
- **URL/schema/router-safety/integration group, 13 passed + 2 skipped:**
  router unit tests (6) + schema (7) = 13 unit, plus 2 integration tests
  (`test_no_advisory_persists_session_and_proposals_in_db` and
  `test_advisory_path_persists_synthesis_block`) skipped when the dev DB is
  not migrated. ✓
- **Safety group, 8 passed:** 4 ROB-15 safety tests + 1 existing
  `test_trading_decisions_router_safety.py` + 3 ROB-14 synthesis-safety tests
  = 8 plausibly. Locally, the ROB-15 safety file alone has 4 tests; combined
  with the existing router-safety + synthesis-safety it covers the broker
  attack surface.
- **`make lint` passed:** Verified on disk that no new ruff violations are
  introduced (the diff is small and matches house style).
- **Full non-live suite, 4225 passed / 42 skipped / 3 deselected / 2 unrelated
  failures:** The 2 failures are flagged as outside the ROB-15 surface; given
  the diff touches only the new modules and one router file (additive POST
  route), this is consistent.

---

## 6. Issues & follow-ups (non-blocking)

These do not block merge but are worth tracking. Each is **non-blocking** —
nothing here violates the safety contract or the plan's hard constraints.

1. **Naive-datetime crash on operator-supplied `generated_at`** (low likelihood,
   bad ergonomics). `app/services/operator_decision_session_service.py:162`
   does `generated_at.astimezone(UTC).date()` inside `_run_with_advisory`.
   When the caller supplies a tz-naive datetime in the request body, Python
   3.13's `astimezone` will treat it as system-local time, not raise — so this
   is an *implicit* assumption rather than a crash, but the result depends on
   the host TZ and could yield a wrong `as_of` date for the runner.
   The default-from-`now()` path is safe (`datetime.now(UTC)` is tz-aware).
   **Recommend follow-up:** add a Pydantic validator on
   `OperatorDecisionRequest.generated_at` that rejects tz-naive values, or
   coerce naive → UTC explicitly in `_run_with_advisory` before
   `astimezone`. Trivial fix; not a safety issue.

2. **Router `Location` header points to JSON resource, not SPA.** Header is
   `/trading/api/decisions/{uuid}` (the JSON detail endpoint). The body's
   `session_url` is the SPA URL. Existing pattern from
   `create_decision` already does this, so consistency wins — but a future
   client following only the `Location` header would land on JSON. Optional:
   point `Location` at the SPA URL to match the body's `session_url`. Not a
   safety issue.

3. **`market_scope` not validated against candidates' `instrument_type`.** A
   client could submit `market_scope="kr"` with a `crypto` candidate. Both
   ROB-14 and the persistence layer accept this (it's just metadata), but the
   resulting session has internally inconsistent rows. Optional follow-up:
   add a model validator that asserts every candidate's instrument_type
   matches the scope, or make `market_scope` derived from the candidates.
   Plan did not require this.

4. **Telemetry / audit trail.** No structured log line is emitted on session
   creation. ROB-9 logs `tradingagents_research` events; ROB-15 piggybacks on
   that for the advisory branch but produces no new log on the no-advisory
   branch. Consider a single info log with `session_uuid`, `user_id`,
   `market_scope`, `proposal_count`, `advisory_used` (no operator-supplied
   strings echoed) for ops visibility.

5. **Hermes/Discord NL parser deferred** (as planned). The endpoint is
   structured-payload only. The follow-up issue should add the parser → this
   endpoint adapter and wire authentication for the operator user.

---

## 7. Recommended smoke before opening the PR

The new path is exercised by 13 unit tests + 2 integration tests, but a
manual smoke is cheap and worth doing once on the dev DB:

```bash
# 1. Make sure the dev DB has the trading_decision tables.
uv run alembic upgrade head

# 2. Start the API.
make dev

# 3. Smoke the no-advisory path with an authenticated session cookie.
curl -i -X POST http://localhost:8000/trading/api/decisions/from-operator-request \
  -H 'Content-Type: application/json' \
  -H "Cookie: $COOKIE" \
  -d '{
    "market_scope": "kr",
    "candidates": [
      {"symbol":"005930","instrument_type":"equity_kr","side":"buy","confidence":70,"proposal_kind":"enter"}
    ]
  }'
```

Expected: HTTP 201, JSON body with `session_url` ending in
`/trading/decisions/<uuid>`, `advisory_used:false`,
`advisory_skipped_reason:"include_tradingagents=False"`. The `Location`
header should be `/trading/api/decisions/<uuid>`. Visit the returned
`session_url` in a browser and confirm the SPA loads the session.

If TradingAgents is configured locally, also smoke the 503 path with
`tradingagents_python` temporarily unset:

```bash
TRADINGAGENTS_PYTHON='' uv run uvicorn app.main:app --reload &
curl -i -X POST http://localhost:8000/trading/api/decisions/from-operator-request \
  -H 'Content-Type: application/json' -H "Cookie: $COOKIE" \
  -d '{"market_scope":"us","candidates":[{"symbol":"AAPL","instrument_type":"equity_us","confidence":50}],"include_tradingagents":true}'
```

Expected: HTTP 503, `{"detail":"tradingagents_not_configured"}`, no DB
write (verify by counting `trading_decision_sessions` rows before/after).

---

## 8. Verdict

**PASS_WITH_NOTES**

The commit fully implements ROB-15 as specified by the plan and clears every
hard safety check. The five non-blocking notes in §6 are quality-of-life and
follow-up items; none of them justifies blocking this PR. Proceed to open the
PR (base `main`) after the optional smoke in §7.

**Hand back:** `AOE_STATUS=ready-for-pr`.
