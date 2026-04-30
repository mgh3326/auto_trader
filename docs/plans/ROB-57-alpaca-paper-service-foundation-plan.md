# ROB-57 — Alpaca Paper Service / Domain Adapter Foundation

Status: plan_ready
Issue: ROB-57
Branch: feature/ROB-57-alpaca-paper-foundation
Worktree: ~/work/auto_trader-worktrees/feature-ROB-57-alpaca-paper-foundation
Planner/Reviewer: Claude Opus
Implementer: Claude Sonnet (same AoE session)
Orchestrator: Hermes (no implementation work)

## 1. Goal

Build the **service-layer foundation** for an Alpaca **paper-trading** broker adapter
under `app/services/brokers/alpaca/`, mirroring the structural pattern already used by
`app/services/brokers/kis/` and `app/services/brokers/upbit/`.

The foundation must provide:

1. An Alpaca **paper-only** configuration namespace in `app/core/config.py`
   (paper endpoint + credential pair only).
2. A typed service interface covering:
   - `account` (account snapshot)
   - `cash` (buying power / cash balances)
   - `positions` (open positions)
   - `assets` (tradable asset universe)
   - `orders` (submit / list / cancel)
   - `fills` (executions / activity)
3. Pure-mock unit tests for each method.
4. Negative tests proving:
   - The live trading endpoint cannot be used as a trading/account/order/fill base URL.
   - The data endpoint cannot be used as a trading/account/order base URL.
   - There is no automatic paper → live fallback.

This is the **adapter foundation only**. It is intentionally not wired into routers,
MCP tools, or Hermes profiles in this issue.

## 2. Non-goals

- ❌ Live Alpaca trading endpoint support (`https://api.alpaca.markets`).
- ❌ Any FastAPI router exposure (`app/routers/*`).
- ❌ Any MCP tool registration (`app/mcp_server/*`).
- ❌ Any Hermes profile / orchestrator wiring.
- ❌ Real-network calls in tests; no recorded HTTP cassettes against Alpaca.
- ❌ Replacing or migrating the existing in-app virtual paper-trading
  (`app/models/paper_trading.py`, `app/services/paper_trading_service.py`) — that is a
  different domain (in-app simulated portfolios), not an external broker adapter.
- ❌ Re-modeling `BrokerAccount` / `BrokerType` in `app/models/manual_holdings.py`.
- ❌ Streaming / WebSocket support.
- ❌ Order routing logic, strategy bindings, sell-signal evaluation.

## 3. Existing repo findings

### 3.1 Broker abstraction landscape

- Per-provider broker packages already exist:
  - `app/services/brokers/kis/`
  - `app/services/brokers/upbit/`
  - `app/services/brokers/yahoo/`
- A generic broker account service exists at
  `app/services/broker_account_service.py`, backed by `BrokerAccount` /
  `BrokerType` enums in `app/models/manual_holdings.py`.
- There is **no shared `BrokerProtocol` / ABC** spanning brokers today; each provider
  package defines its own client and constants. Therefore the Alpaca adapter SHOULD
  define its own protocol locally and avoid attempting a cross-broker refactor in this
  issue.

### 3.2 Paper / mock account model

- `app/models/paper_trading.py` and `app/services/paper_trading_service.py`
  implement an **in-app simulated** portfolio (DB-backed virtual paper trading).
  This is **not** an external broker integration and must not be conflated with
  Alpaca paper trading.
- `kis_mock_*` namespaces in `app/core/config.py` already establish the convention
  of paper/mock credentials living alongside live credentials in typed settings.
  Alpaca paper settings must follow the same Pydantic `BaseSettings` style.

### 3.3 Settings convention

- `app/core/config.py` is the single typed source for credentials and provider config.
- Existing namespaces: `kis_*`, `kis_mock_*`, `upbit_*`.
- New namespace will be `alpaca_paper_*` and **must not** introduce a parallel
  `alpaca_live_*` namespace in this issue.

### 3.4 Test conventions

- `tests/` uses pytest with strict markers (`unit`, `integration`, `slow`).
- All ROB-57 tests are `@pytest.mark.unit` and must be hermetic (no real network).

## 4. ROB-56 conflict assessment

ROB-56 (PR #513, merged) touched the following files:

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

### 4.1 Overlap analysis vs ROB-57 target scope

| ROB-57 work item                           | ROB-56 file touched? | Risk |
|--------------------------------------------|----------------------|------|
| New `app/services/brokers/alpaca/` package | No                   | None |
| New `tests/test_alpaca_paper_*.py`         | No                   | None |
| `app/core/config.py` — add `alpaca_paper_*`| No                   | None |
| `app/models/paper_trading.py`              | **YES — ROB-56**     | Avoid|
| `app/services/paper_trading_service.py`    | **YES — ROB-56**     | Avoid|
| `app/services/brokers/kis/*`               | **YES — ROB-56**     | Avoid|
| `app/mcp_server/*`                         | **YES — ROB-56**     | Avoid (and out of scope) |
| `app/routers/*`                            | **YES — ROB-56**     | Avoid (and out of scope) |

### 4.2 Recommendation: **GO**

The preferred path is fully isolated:

- All new code lives under `app/services/brokers/alpaca/` (new package).
- All new tests live under `tests/test_alpaca_paper_*.py` (new files).
- The only edit to a pre-existing file is **adding** an `alpaca_paper_*` settings
  block to `app/core/config.py`, which ROB-56 did not modify. This is an additive
  change and does not collide.

If during implementation Sonnet finds it must edit any of the ROB-56 files listed in
4.1, it MUST stop and report `AOE_STATUS: waiting_for_user` per the conflict gate.

## 5. Proposed module / API design

### 5.1 New package layout

```
app/services/brokers/alpaca/
├── __init__.py            # public re-exports only
├── config.py              # AlpacaPaperSettings accessor (reads from app.core.config)
├── endpoints.py           # PAPER_TRADING_BASE_URL, DATA_BASE_URL, LIVE_TRADING_BASE_URL (constant for guard tests only)
├── exceptions.py          # AlpacaPaperConfigurationError, AlpacaPaperEndpointError, AlpacaPaperRequestError
├── protocols.py           # AlpacaPaperBrokerProtocol (typing.Protocol)
├── schemas.py             # Pydantic models: AccountSnapshot, CashBalance, Position, Asset, Order, Fill, OrderRequest
├── transport.py           # HTTPTransport interface + httpx-based default impl (injectable for tests)
└── service.py             # AlpacaPaperBrokerService implementing AlpacaPaperBrokerProtocol
```

### 5.2 Settings namespace (additions to `app/core/config.py`)

```
alpaca_paper_api_key: str | None
alpaca_paper_api_secret: SecretStr | None
alpaca_paper_base_url: HttpUrl = "https://paper-api.alpaca.markets"
alpaca_paper_data_base_url: HttpUrl = "https://data.alpaca.markets"
```

Validators:

- `alpaca_paper_base_url` MUST equal `https://paper-api.alpaca.markets`.
  Any other value raises `AlpacaPaperConfigurationError` at settings load time.
- There is **no** `alpaca_live_*` field. None must be added in this issue.

### 5.3 Endpoint constants (`endpoints.py`)

```
PAPER_TRADING_BASE_URL = "https://paper-api.alpaca.markets"
DATA_BASE_URL          = "https://data.alpaca.markets"
LIVE_TRADING_BASE_URL  = "https://api.alpaca.markets"  # exported only as a forbidden-value sentinel for guard tests

FORBIDDEN_TRADING_BASE_URLS = frozenset({LIVE_TRADING_BASE_URL, DATA_BASE_URL})
```

`AlpacaPaperBrokerService.__init__` validates that the resolved trading base URL
is exactly `PAPER_TRADING_BASE_URL` and rejects any value in
`FORBIDDEN_TRADING_BASE_URLS`.

### 5.4 Protocol (`protocols.py`)

```python
class AlpacaPaperBrokerProtocol(Protocol):
    async def get_account(self) -> AccountSnapshot: ...
    async def get_cash(self) -> CashBalance: ...
    async def list_positions(self) -> list[Position]: ...
    async def list_assets(self, *, status: str | None = None,
                          asset_class: str | None = None) -> list[Asset]: ...
    async def submit_order(self, request: OrderRequest) -> Order: ...
    async def list_orders(self, *, status: str | None = None,
                          limit: int | None = None) -> list[Order]: ...
    async def cancel_order(self, order_id: str) -> None: ...
    async def get_order(self, order_id: str) -> Order: ...
    async def list_fills(self, *, after: datetime | None = None,
                         until: datetime | None = None,
                         limit: int | None = None) -> list[Fill]: ...
```

### 5.5 Transport (`transport.py`)

- `HTTPTransport` Protocol: `async def request(method, path, **kwargs) -> Response`.
- Default impl wraps `httpx.AsyncClient` bound to the paper base URL.
- The constructor accepts an injected transport so tests substitute a mock with no
  real network access.

### 5.6 Service (`service.py`)

- `AlpacaPaperBrokerService(transport: HTTPTransport, settings: AlpacaPaperSettings)`.
- Constructor invariants:
  - Raises `AlpacaPaperConfigurationError` if either credential is missing.
  - Raises `AlpacaPaperEndpointError` if the trading base URL is not exactly
    `PAPER_TRADING_BASE_URL`.
- All HTTP errors → `AlpacaPaperRequestError`.
- Method bodies are thin: marshal request, call `transport.request`, parse via
  `schemas.py`. No retry/backoff complexity in this foundation.

### 5.7 Public surface (`__init__.py`)

Re-exports: `AlpacaPaperBrokerProtocol`, `AlpacaPaperBrokerService`,
`AlpacaPaperConfigurationError`, `AlpacaPaperEndpointError`,
`AlpacaPaperRequestError`, schema dataclasses, and the endpoint constants.
**No FastAPI router, no MCP tool registration, no Hermes profile import.**

## 6. Safety invariants

These invariants are enforced by code AND by tests:

- **I1.** Trading base URL is exactly `https://paper-api.alpaca.markets`.
- **I2.** The live endpoint `https://api.alpaca.markets` is NEVER usable as a
  trading/account/order/fill base URL. Any attempt raises
  `AlpacaPaperEndpointError`.
- **I3.** The data endpoint `https://data.alpaca.markets` is NEVER usable as a
  trading/account/order base URL. It exists only to permit a future market-data
  client; this issue does not implement that client.
- **I4.** No automatic paper → live fallback path exists in code.
- **I5.** No real network in tests. Every test uses an injected mock transport.
- **I6.** No router, MCP tool, or Hermes profile imports `app/services/brokers/alpaca/`
  in this issue. CI grep guard test enforces this.
- **I7.** Settings load fails fast if `alpaca_paper_base_url` is overridden to a
  non-paper URL via env.

## 7. Test plan (exact mocked cases)

All tests live in new files; all are `@pytest.mark.unit`. No real network.

### 7.1 `tests/test_alpaca_paper_config.py`

- `test_settings_default_paper_base_url_is_paper_api`
  Asserts default `alpaca_paper_base_url` == `https://paper-api.alpaca.markets`.
- `test_settings_rejects_live_trading_base_url`
  Setting env `ALPACA_PAPER_BASE_URL=https://api.alpaca.markets` raises
  `AlpacaPaperConfigurationError`.
- `test_settings_rejects_data_endpoint_as_trading_base_url`
  Setting env `ALPACA_PAPER_BASE_URL=https://data.alpaca.markets` raises
  `AlpacaPaperConfigurationError`.
- `test_settings_requires_credentials`
  Missing key or secret → `AlpacaPaperConfigurationError` when service is built.

### 7.2 `tests/test_alpaca_paper_service_endpoint_guard.py`

- `test_service_init_accepts_paper_endpoint`
- `test_service_init_rejects_live_endpoint`
  Build with trading base URL = `https://api.alpaca.markets` → raises
  `AlpacaPaperEndpointError`. Confirms invariant **I2**.
- `test_service_init_rejects_data_endpoint_as_trading_base`
  Confirms invariant **I3**.
- `test_service_has_no_live_fallback_attribute`
  Reflective assertion: no `live_*` attribute, no `fallback_*` callable on the
  service. Confirms invariant **I4**.

### 7.3 `tests/test_alpaca_paper_service_methods.py`

Each test injects a mock `HTTPTransport` returning canned JSON.

- `test_get_account_returns_snapshot`
  Mock returns `{"id": "...", "buying_power": "100000", "cash": "50000",
  "portfolio_value": "150000", "status": "ACTIVE"}` → parsed `AccountSnapshot`.
- `test_get_cash_returns_cash_balance`
  Derived from `/v2/account`; asserts cash and buying-power fields.
- `test_list_positions_parses_array`
  Mock returns 2-element array; service returns `list[Position]` of length 2.
- `test_list_positions_empty`
  Mock returns `[]`; service returns `[]`.
- `test_list_assets_passes_status_and_class_query`
  Asserts request was made to `/v2/assets` with `status=active`,
  `asset_class=us_equity`.
- `test_submit_order_marshals_request`
  Asserts POST `/v2/orders` body matches `OrderRequest` serialization.
- `test_list_orders_with_status_filter`
  Asserts query string contains `status=open&limit=50`.
- `test_get_order_by_id`
  Asserts GET `/v2/orders/{id}`.
- `test_cancel_order_returns_none_on_204`
  Mock 204 response; method returns None without raising.
- `test_list_fills_uses_activities_executions_endpoint`
  Asserts GET `/v2/account/activities/FILL` (or equivalent fills path) with
  date range query params propagated.
- `test_request_error_wraps_http_error`
  Mock raises 422; service raises `AlpacaPaperRequestError` carrying status.

### 7.4 `tests/test_alpaca_paper_isolation.py`

CI guard tests (cheap grep + import checks) confirming isolation:

- `test_no_router_imports_alpaca_paper`
  Walks `app/routers/` and asserts no source file imports
  `app.services.brokers.alpaca`.
- `test_no_mcp_tool_imports_alpaca_paper`
  Walks `app/mcp_server/` and asserts the same.
- `test_no_hermes_profile_imports_alpaca_paper`
  Walks Hermes profile modules and asserts the same.
- `test_no_alpaca_live_settings_field`
  Inspects `Settings` model; asserts no field name starting with `alpaca_live_`.

## 8. Implementation steps (Sonnet, in order)

1. Create package skeleton `app/services/brokers/alpaca/` with empty modules listed
   in §5.1.
2. Add `alpaca_paper_*` fields and validators to `app/core/config.py` (additive only).
3. Implement `endpoints.py` with the three constants and `FORBIDDEN_TRADING_BASE_URLS`.
4. Implement `exceptions.py`.
5. Implement `schemas.py` (pydantic models for Account, Cash, Position, Asset,
   Order, Fill, OrderRequest).
6. Implement `protocols.py` exactly as in §5.4.
7. Implement `transport.py` with the Protocol and a default httpx-based class.
8. Implement `service.py` with constructor invariants and the methods listed in §5.4.
9. Add `__init__.py` re-exports per §5.7.
10. Add the four test files in §7. Each test file must be runnable in isolation.
11. Run lint + typecheck + tests locally (commands in §9).
12. Open PR titled
    `feat(alpaca): paper-only broker service foundation (ROB-57)` against `main`.

If at any step Sonnet finds it must edit any file in the ROB-56 list (§4), STOP
and report `AOE_STATUS: waiting_for_user`.

## 9. Verification commands

```bash
# Lint + format + typecheck
make lint
make format
make typecheck

# Targeted unit tests for ROB-57 only
uv run pytest tests/test_alpaca_paper_config.py -v
uv run pytest tests/test_alpaca_paper_service_endpoint_guard.py -v
uv run pytest tests/test_alpaca_paper_service_methods.py -v
uv run pytest tests/test_alpaca_paper_isolation.py -v

# Full unit suite (no integration, no slow)
uv run pytest tests/ -v -m "not integration and not slow"

# Confirm no real network attempts
uv run pytest tests/ -v -k "alpaca_paper" --disable-socket
# (if pytest-socket is not installed, isolation is enforced by injected mock transport)
```

## 10. Follow-up issues (out of scope here)

- **ROB-57.F1** — Hermes profile exposure for the Alpaca paper broker.
- **ROB-57.F2** — FastAPI router exposure (`/brokers/alpaca/paper/*`) with auth.
- **ROB-57.F3** — MCP tool registration for paper account/orders/positions.
- **ROB-57.F4** — Bridging Alpaca paper accounts into `BrokerAccount` /
  `broker_account_service.py`.
- **ROB-57.F5** — Alpaca market-data client built on `DATA_BASE_URL`.
- **ROB-57.F6** — Streaming (WebSocket) trade/account updates.
- **ROB-57.F7** — Live-endpoint support — **explicitly deferred**, requires its own
  approval gate and risk review.
