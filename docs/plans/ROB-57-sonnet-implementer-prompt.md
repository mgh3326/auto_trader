# ROB-57 Implementer Prompt — Claude Sonnet

You are the implementer for ROB-57. Hermes is the orchestrator only. The plan in
`docs/plans/ROB-57-alpaca-paper-service-foundation-plan.md` is authoritative. Read
it before doing anything.

## Mission

Implement the Alpaca paper-trading **service/domain adapter foundation** under
`app/services/brokers/alpaca/`, plus its unit tests. Do not expose it through any
router, MCP tool, or Hermes profile in this issue.

## Branch / Worktree

- Branch: `feature/ROB-57-alpaca-paper-foundation`
- Worktree: `~/work/auto_trader-worktrees/feature-ROB-57-alpaca-paper-foundation`
- Base: `main`
- Single PR, single AoE session, single branch, single worktree.

## Allowed files (you MAY create or edit these)

Create:
```
app/services/brokers/alpaca/__init__.py
app/services/brokers/alpaca/config.py
app/services/brokers/alpaca/endpoints.py
app/services/brokers/alpaca/exceptions.py
app/services/brokers/alpaca/protocols.py
app/services/brokers/alpaca/schemas.py
app/services/brokers/alpaca/transport.py
app/services/brokers/alpaca/service.py
tests/test_alpaca_paper_config.py
tests/test_alpaca_paper_service_endpoint_guard.py
tests/test_alpaca_paper_service_methods.py
tests/test_alpaca_paper_isolation.py
docs/plans/ROB-57-alpaca-paper-service-foundation-plan.md   # already authored
docs/plans/ROB-57-sonnet-implementer-prompt.md              # this file
```

Edit (additive only — do NOT remove existing fields):
```
app/core/config.py   # add alpaca_paper_* fields and validators only
```

## Prohibited files (you MUST NOT edit any of these)

These were touched by ROB-56 (PR #513). Editing them triggers the conflict gate.

```
app/mcp_server/tooling/fundamentals/_valuation.py
app/mcp_server/tooling/fundamentals_handlers.py
app/mcp_server/tooling/paper_analytics_registration.py
app/models/paper_trading.py
app/routers/n8n.py
app/schemas/n8n/sell_signal.py
app/services/brokers/kis/client.py
app/services/brokers/kis/constants.py
app/services/brokers/kis/domestic_market_data.py
app/services/paper_trading_service.py
app/services/sell_signal_service.py
tests/test_mcp_fundamentals_tools.py
tests/test_paper_analytics_tools.py
tests/test_paper_trading_service.py
```

Additionally prohibited (out of scope for this issue):

- Anything under `app/routers/` (no router exposure).
- Anything under `app/mcp_server/` (no MCP tool registration).
- Any Hermes profile / orchestrator wiring.
- `app/models/manual_holdings.py` (no `BrokerType` / `BrokerAccount` changes).
- Any new `alpaca_live_*` setting, constant, or code path.

If the work appears to require editing any prohibited file, STOP and emit
`AOE_STATUS: waiting_for_user` with a brief description of the conflict.

## Hard rules

1. **No real network in tests.** All tests must inject a mocked `HTTPTransport`.
   Tests must pass offline. Do not record/replay real Alpaca traffic.
2. **No live-endpoint fallback.** `https://api.alpaca.markets` must be rejected
   as a trading/account/order/fill base URL with `AlpacaPaperEndpointError`. There
   must be no code path — direct, fallback, retry, or env-driven — that uses the
   live endpoint for trading.
3. **Data endpoint is not a trading endpoint.** `https://data.alpaca.markets` is
   defined as a constant for future use only and must also be rejected as a
   trading/account/order base URL.
4. **No API / MCP / Hermes exposure.** Do not import `app.services.brokers.alpaca`
   from any router, MCP tool, or Hermes profile module. The isolation tests in
   `tests/test_alpaca_paper_isolation.py` enforce this; they MUST pass.
5. **Single namespace.** Add `alpaca_paper_*` settings only. Do not introduce
   `alpaca_live_*`.
6. **Additive config edits only.** In `app/core/config.py`, only add new fields
   and validators. Do not refactor unrelated code.
7. **Follow plan §5 design exactly.** If you believe a design change is needed,
   STOP and emit `AOE_STATUS: waiting_for_user` rather than diverging.

## Test rules

- All ROB-57 tests use `@pytest.mark.unit` only.
- Cover every method on `AlpacaPaperBrokerProtocol` with at least one mocked test.
- Cover every safety invariant (I1–I7) from plan §6 with at least one test.
- Required test files: see plan §7. Do not consolidate them into a single file.
- Required negative tests:
  - Live URL rejected as trading base.
  - Data URL rejected as trading base.
  - No `live_*` / `fallback_*` attribute or method on the service.
  - No `alpaca_live_*` field on settings.
  - No router / MCP / Hermes module imports the Alpaca package.

## Verification before declaring done

```bash
make lint
make format
make typecheck
uv run pytest tests/test_alpaca_paper_config.py -v
uv run pytest tests/test_alpaca_paper_service_endpoint_guard.py -v
uv run pytest tests/test_alpaca_paper_service_methods.py -v
uv run pytest tests/test_alpaca_paper_isolation.py -v
uv run pytest tests/ -v -m "not integration and not slow"
```

All commands must succeed before opening the PR.

## PR requirements

- Title: `feat(alpaca): paper-only broker service foundation (ROB-57)`
- Base: `main`
- Body must include:
  - Link to the plan file.
  - Explicit statement: "Paper endpoint only. No live fallback. No router / MCP /
    Hermes exposure in this PR."
  - List of follow-up issues (plan §10).
  - Co-author trailer: `Co-Authored-By: Paperclip <noreply@paperclip.ing>`

## Final status block (emit when finished)

When implementation, tests, lint, typecheck, and PR are complete, emit exactly:

```
AOE_STATUS: implementation_ready_for_review
AOE_ISSUE: ROB-57
AOE_ROLE: implementer
AOE_PR_BRANCH: feature/ROB-57-alpaca-paper-foundation
AOE_PLAN_PATH: docs/plans/ROB-57-alpaca-paper-service-foundation-plan.md
AOE_NEXT: planner_review
```

If you hit the ROB-56 conflict gate or any blocker, emit instead:

```
AOE_STATUS: waiting_for_user
AOE_ISSUE: ROB-57
AOE_ROLE: implementer
AOE_BLOCKER: <one-line description>
AOE_NEXT: user_decision
```
