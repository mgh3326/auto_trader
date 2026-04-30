# ROB-56 — KIS Official Mock Hard-Separation (Implementation Plan)

**Planner:** Claude Opus 4.7 (planner/reviewer)
**Implementer (handoff):** Claude Sonnet (same AoE session, same worktree)
**Issue:** ROB-56
**Branch / worktree:** `feature/ROB-56-kis-mock-hard-separation`
(`/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-56-kis-mock-hard-separation`)
**Status:** plan_ready

---

## 1. Executive summary

ROB-19/28/31/37 already established a strong **runtime-parameter** boundary
between KIS live and KIS official mock: a single `KISClient(is_mock=...)`
with a non-fallback `_KISSettingsView`, a fail-closed `validate_kis_mock_config()`
gate, separate Redis token namespaces, a frozen `mock_unsupported` TR set,
and a fully isolated `review.kis_mock_order_ledger` write path. The remaining
risk is that this separation lives **inside one MCP tool** that selects mode
from a `account_mode` argument — a misconfigured agent, a prompt-injected
caller, or a buggy router can still reach the live order surface from a tool
that an operator intended to be mock-only.

ROB-56 closes that gap by:

1. Splitting the **side-effect** KIS order MCP tools into **explicitly-named
   `kis_mock_*` and `kis_live_*` tool registrations** (separate functions
   registered as separate MCP tool names), so paper/mock deployments load
   only the mock variants and the live order surface is **physically absent**
   from the tool list.
2. Introducing a small **profile-based registration shim**
   (`register_order_tools_for_profile(...)`) that drives the existing
   `app/mcp_server/main.py` setup. Profiles: `default` (today's behavior),
   `hermes-paper-kis` (mock + read-only only).
3. Keeping shared **side-effect-free** research tools (market data,
   fundamentals, news, analysis, watch alerts, market reports) registered in
   both profiles, after a per-tool side-effect audit that pins this in a
   test.
4. Adding a **broker capability metadata** module so KIS (and forward-looking
   Kiwoom) declare KR/US support without changing routing today.
5. Pinning all of the above with focused pytest targets, including a
   regression guard that fails if a `kis_live_*` order tool is ever
   registered in `hermes-paper-kis`.

The implementation is **read/write code only inside `app/mcp_server/`,
`app/core/config.py`, and tests**. No broker behavior changes, no migrations,
no live order code path changes, no Kiwoom client. `dry_run=True` defaults
remain. No autonomous trading enablement.

---

## 2. Prior issue audit (ROB-19 / 28 / 31 / 37)

Search performed against repo (`docs/plans/`, `git log --all`, source).
All four issues are **completed and merged** as of `main` at this worktree's
fork point (`6cb8656a feat: add preopen news readiness section (#629)`).

### 2.1 ROB-19 — Normalize simulated vs KIS mock account routing
- **Plan / report:** `docs/plans/ROB-19-kis-mock-account-routing-plan.md`,
  `docs/plans/ROB-19-final-review-report.md`,
  `docs/plans/ROB-19-review-report.md`.
- **Commits (search):** `git log --all --oneline --grep='ROB-19'`
  (e.g. `f9f1d883 fix(rob-19): harden kis mock routing safeguards`).
- **Decisions that constrain ROB-56:**
  - Live and mock credentials live in distinct `Settings` fields
    (`kis_app_*` vs `kis_mock_*`); access goes through
    `_KISSettingsView` with property routing (no `__getattr__` fallback for
    those four fields). See `app/services/brokers/kis/client.py:43-88`.
  - Mock client uses `RedisTokenManager("kis_mock")` namespace
    (`client.py:108-109`).
  - Default `account_mode` resolves to `kis_live`
    (`tests/test_mcp_account_modes.py:8`,
    `app/mcp_server/tooling/account_modes.py:107-108`).
  - `validate_kis_mock_config()` fails closed without leaking values
    (`app/core/config.py:484-496`,
    `tests/test_mcp_account_modes.py:56-72`).
- **Phase-2 follow-up flagged in the final review:** standardize the
  fail-closed error shape, tighten `_KISSettingsView` against future leaky
  `__getattr__` regressions, add a token-namespace isolation test, run an
  operator smoke against the actual mock server. ROB-56 is the right
  carrier for the first three (test-pinned).

### 2.2 ROB-28 — Harden KIS mock `account_mode` routing for order lifecycle
- **Plan / report:** `docs/plans/ROB-28-kis-mock-routing-plan.md`,
  `docs/plans/ROB-28-review-report.md`.
- **Commits (search):** `git log --all --oneline --grep='ROB-28'`.
- **Decisions:** `cancel_order` / `modify_order` MCP tools accept
  `account_mode`, validate via `_kis_mock_config_error()` and pass
  `is_mock=routing.is_kis_mock` to
  `cancel_order_impl` / `modify_order_impl`
  (`app/mcp_server/tooling/orders_registration.py:241-343`,
  `tests/test_mcp_account_modes.py:75-251`).
  Mock-side `inquire_integrated_margin(is_mock=True)` and
  `inquire_overseas_orders(is_mock=True)` raise structured errors instead of
  falling back to live.
- **Constraint on ROB-56:** new `kis_mock_*` tool variants must continue to
  produce these structured errors and the same fail-closed metadata
  envelope, **without re-implementing the cash-routing logic**.

### 2.3 ROB-31 — KIS mock TR routing matrix + KR pending fail-closed
- **Doc:** `docs/kis-mock-tr-routing-matrix.md`, README updates in
  `app/mcp_server/README.md`.
- **Commit:** `151fd9fa ROB-31 KIS mock TR routing matrix + KR pending fail-closed`.
- **Decisions:** `mock_unsupported` set frozen in
  `tests/test_kis_constants.py::test_mock_unsupported_tr_set_is_documented`
  → `{TTTC8036R, TTTS3018R, TTTC0869R, TTTC2101R}`. `inquire_korea_orders`
  (`TTTC8036R`) returns `EGW02006`-tagged error under mock.
- **Constraint on ROB-56:** the new mock-only `kis_mock_get_order_history`
  tool must not silently route around `mock_unsupported` endpoints; the
  underlying `orders_history.get_order_history_impl(..., is_mock=True)`
  already tags responses with `mock_unsupported=true`. Test pinning must
  remain green.

### 2.4 ROB-37 — Isolate KIS mock order ledger
- **Commit:** `aced64c7 feat(ROB-37): isolate KIS mock order ledger`.
- **Source:** `app/mcp_server/tooling/kis_mock_ledger.py:1-100`,
  `app/models/review.py` (`KISMockOrderLedger`),
  Alembic `d3703007a676` (down_revision `d34d6def084b`).
- **Decisions:** `review.kis_mock_order_ledger` enforces
  `account_mode='kis_mock'` and `broker='kis'` via DB CHECK constraints;
  unique index `uq_kis_mock_ledger_order_no` per
  `app/mcp_server/tooling/kis_mock_ledger.py:79`. Live execution path
  (`order_execution._execute_and_record`) branches on `is_mock=True` and
  skips `_save_order_fill`, `_create_trade_journal_for_buy`,
  `_close_journals_on_sell`, `_link_journal_to_fill`.
- **Constraint on ROB-56:** the data-layer split is already complete. ROB-56
  must not re-route mock writes back through `review.trades`. The new
  `kis_mock_*` MCP tools delegate to the **same** underlying
  `_place_order_impl(..., is_mock=True)` so this isolation is preserved.

### 2.5 Cross-cutting risks affecting ROB-56
- **Do not** introduce a fallback path from `kis_mock_*` tools to live
  credentials or live execution — even on missing-config errors.
- **Do not** widen `_KISSettingsView.__getattr__` to expose live values from
  a mock view; ROB-56 will add a regression test that pins the
  property-only routing for the four hot fields.
- **Do not** modify the `mock_unsupported` TR set; ROB-31's frozen test must
  still pass.

---

## 3. Current code routing audit

Working directory:
`/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-56-kis-mock-hard-separation`.

### 3.1 KIS clients / adapters
- `app/services/brokers/kis/client.py:43-88` — `_KISSettingsView` exposes
  live or mock credentials per `is_mock` flag without cross-account fallback
  for `kis_app_key`, `kis_app_secret`, `kis_account_no`, `kis_base_url`,
  `kis_access_token`. All other settings still come from the global object
  via `__getattr__`.
- `app/services/brokers/kis/client.py:90-114` — `KISClient(is_mock: bool=False)`;
  `is_mock=True` swaps the token manager to `RedisTokenManager("kis_mock")`.
- `app/services/brokers/kis/{domestic_orders,overseas_orders,account}.py`
  — used unchanged from both live and mock clients.

### 3.2 Credentials / env
- `app/core/config.py:166-180` — `Settings` fields:
  `kis_app_key`, `kis_app_secret`, `kis_base_url`, `kis_access_token`,
  `kis_account_no`, plus mock siblings `kis_mock_enabled`,
  `kis_mock_app_key`, `kis_mock_app_secret`, `kis_mock_base_url`
  (`https://openapivts.koreainvestment.com:29443`),
  `kis_mock_account_no`, `kis_mock_access_token`.
- `app/core/config.py:484-496` — `validate_kis_mock_config(settings_obj)`
  returns names of missing required mock vars without exposing values.
- `env.example:8-26` — documents both live and mock vars; commits the
  warning "Keep credential values in operator-managed runtime env files;
  do not commit them."

### 3.3 Order execution boundary
- `app/mcp_server/tooling/order_execution.py:56-66` — `_create_kis_client`
  and `_call_kis` thread `is_mock` to `KISClient(is_mock=True)` and
  `method(..., is_mock=True)`.
- `app/mcp_server/tooling/order_execution.py:100-116` — `_execute_order`
  passes `is_mock` to `_execute_kr_order` / `_execute_us_order`. Crypto
  path (`_execute_crypto_order`) ignores `is_mock`.
- `app/mcp_server/tooling/kis_mock_ledger.py` — `_save_kis_mock_order_ledger`
  always sets `account_mode="kis_mock"` and `broker="kis"`; row insert
  isolated to `review.kis_mock_order_ledger` (DB CHECK constraints enforce
  this).

### 3.4 MCP tool registration
- Entry: `app/mcp_server/main.py:39-51` instantiates one `FastMCP` and calls
  `register_all_tools(mcp)` (no profile selection today).
- Orchestrator: `app/mcp_server/tooling/registry.py:51-71` calls 17
  registration helpers in fixed order, including
  `register_order_tools(mcp)` and `register_portfolio_tools(mcp)`.
- Side-effect order tools (today, single-tool routing):
  - `place_order` (`orders_registration.py:110-239`)
  - `cancel_order` (`orders_registration.py:241-285`)
  - `modify_order` (`orders_registration.py:287-343`)
  - `get_order_history` (`orders_registration.py:48-108`)
  All four normalize `account_mode` (preferred) or `account_type`
  (deprecated) → `AccountRouting{is_db_simulated, is_kis_mock, is_kis_live}`
  (`app/mcp_server/tooling/account_modes.py:32-54, 90-132`). Default routing
  is `kis_live` (line 107-108). Each tool calls `_kis_mock_config_error()`
  on the kis_mock branch; `db_simulated` for `cancel_order`/`modify_order`
  returns `not supported`.
- Read-only / research tools (no `account_mode`-driven side effects today):
  registered by `register_market_data_tools`, `register_fundamentals_tools`,
  `register_analysis_tools`, `register_watch_alert_tools`,
  `register_trade_profile_tools`, `register_market_report_tools`,
  `register_user_settings_tools`, `register_news_tools`,
  `register_market_brief_tools`. Some portfolio tools
  (`get_holdings`, `portfolio_cash`, `portfolio_avg_cost`) accept
  `account_mode` for routing read paths to mock vs live KIS.

### 3.5 Hermes / profile config
- **No profile config exists in the repo.** `hermes` is only a label:
  - `app/models/trading_decision.py` enum value
    `source_profile="hermes"`
  - `app/schemas/strategy_events.py` source allowlist contains `"hermes"`
  - 35 docs/plans mention "Hermes" as the operator agent identity
- **No file in the repo contains `hermes-paper-kis`, `paper-kis`, or
  `paper_kis`** (verified by grep). ROB-56 introduces this profile
  identifier for the first time.

### 3.6 Capability model
- **No broker capability registry exists.** Market routing in
  `order_execution._execute_order` is hard-coded by `market_type` string
  (`crypto`, `equity_kr`, `equity_us`). No `BrokerCapability`,
  `supports_kr`, `supports_us` types. No `Kiwoom` directory under
  `app/services/brokers/`. Kiwoom is only mentioned twice, both in ROB-28
  docs as a non-goal.

### 3.7 Existing tests touching this surface
- `tests/test_kis_mock_routing.py` — `KISClient(is_mock=True)` URL/token
  paths.
- `tests/test_mcp_account_modes.py` — covers default `kis_live`, alias
  warnings, conflicting selectors, fail-closed config, `is_mock` propagated
  to `cancel_order_impl` / `modify_order_impl`.
- `tests/test_kis_mock_order_ledger.py` — DB ledger isolation.
- `tests/test_kis_constants.py::test_mock_unsupported_tr_set_is_documented`
  — frozen `mock_unsupported` TR set.
- `tests/test_orders_history_kis_mock.py`,
  `tests/test_portfolio_cash_kis_mock.py`,
  `tests/test_kis_integrated_margin_mock.py` — read-side coverage.
- Test harness: `tests/_mcp_tooling_support.py` exposes `DummyMCP`, used
  by `test_mcp_account_modes.py:75-251`.

---

## 4. Proposed architecture

### 4.1 Goals
- **G1:** A `hermes-paper-kis` MCP profile in which the live KIS order
  surface is **not registered**. A test must fail if any
  `kis_live_*`-tagged side-effect tool is registered in that profile.
- **G2:** Make the per-account-mode boundary visible at the **MCP tool name**
  level for KIS side-effect tools, so logs/audit/typed clients can see
  exactly which surface was called.
- **G3:** Preserve all existing behavior in the `default` profile,
  including the existing `place_order` / `cancel_order` / `modify_order`
  tool names and `account_mode` semantics. Backwards compatibility for
  callers that already use `account_mode="kis_mock"` is non-negotiable —
  do not break ROB-19/28/31/37 callers.
- **G4:** Add forward-looking broker capability metadata for KIS and
  Kiwoom, scoped to metadata only.

### 4.2 Profile definition

Profiles are an **enum + a registration switch**, not a config file. They
gate which subset of `register_*_tools(mcp)` runs and which side-effect
order variants register.

```
app/mcp_server/profiles.py  (new)

class McpProfile(StrEnum):
    DEFAULT = "default"
    HERMES_PAPER_KIS = "hermes-paper-kis"
```

Profile selection: `MCP_PROFILE` env var (default `"default"`), parsed
in `app/mcp_server/main.py` and threaded into a new
`register_all_tools(mcp, profile=...)` signature. No new YAML/TOML.

### 4.3 Side-effect KIS order tool split

Add a new module
`app/mcp_server/tooling/orders_kis_variants.py` exposing two factories:

- `register_kis_live_order_tools(mcp)` → registers
  - `kis_live_place_order`
  - `kis_live_cancel_order`
  - `kis_live_modify_order`
  - `kis_live_get_order_history`
- `register_kis_mock_order_tools(mcp)` → registers
  - `kis_mock_place_order`
  - `kis_mock_cancel_order`
  - `kis_mock_modify_order`
  - `kis_mock_get_order_history`

Each variant is a **thin wrapper** that:
1. Hard-pins `is_mock` (live=False, mock=True) and **rejects** any
   `account_mode` argument other than its own (or accepts only its own
   value as a redundancy check). For mock variants only, calls
   `_kis_mock_config_error()` first.
2. Delegates to the existing implementations
   `order_execution._place_order_impl`, `cancel_order_impl`,
   `modify_order_impl`, `orders_history.get_order_history_impl`.
3. Reuses `apply_account_routing_metadata` to keep the response envelope
   identical to today.

The original `place_order`/`cancel_order`/`modify_order`/
`get_order_history` tools (in `orders_registration.py`) remain unchanged
and continue to support `account_mode` switching for the `default` profile.

### 4.4 Profile-driven registration

```
app/mcp_server/tooling/registry.py

def register_all_tools(mcp, profile: McpProfile = McpProfile.DEFAULT) -> None:
    # Always: side-effect-free research + read-only tools
    register_market_data_tools(mcp)
    register_fundamentals_tools(mcp)
    register_analysis_tools(mcp)
    register_watch_alert_tools(mcp)
    register_market_report_tools(mcp)
    register_news_tools(mcp)
    register_market_brief_tools(mcp)
    register_user_settings_tools(mcp)
    register_trade_profile_tools(mcp)

    # Read-only with account_mode (mock-safe) — keep in both profiles
    register_portfolio_tools(mcp)             # holdings/cash/avg-cost
    register_trade_journal_tools(mcp)
    register_paperclip_comment_tools(mcp)
    register_execution_comment_tools(mcp)
    register_paper_account_tools(mcp)         # db_simulated only
    register_paper_analytics_tools(mcp)
    register_paper_journal_tools(mcp)

    if profile is McpProfile.DEFAULT:
        # Today's behavior preserved.
        register_order_tools(mcp)             # ambiguous account_mode tools
        register_kis_live_order_tools(mcp)    # additive — typed callers
        register_kis_mock_order_tools(mcp)
    elif profile is McpProfile.HERMES_PAPER_KIS:
        register_kis_mock_order_tools(mcp)
        # explicitly NOT: register_order_tools, register_kis_live_order_tools
```

Notes:
- In `default`, all three registration paths coexist; nothing breaks for
  legacy callers using `place_order(account_mode="kis_mock")`. The new
  `kis_*_*` typed tools are additive.
- In `hermes-paper-kis`, the legacy ambiguous `place_order` / `cancel_order`
  / `modify_order` / `get_order_history` are **not registered** at all,
  removing the "pass `account_mode='kis_live'` and reach live" surface.
- Crypto (Upbit) order routing today flows through `place_order`. In
  `hermes-paper-kis` Upbit ordering is therefore unavailable. This matches
  the profile's intent (KIS-paper-only). Out of scope to add a
  `upbit_place_order` for ROB-56; document as follow-up.

### 4.5 Fail-closed envelope (ROB-19 phase-2 carry)

Standardize the structured fail-closed return for KIS mock missing-config
across all four mock variants by reusing `_kis_mock_config_error()` plus
`apply_account_routing_metadata`. Concrete shape (already used by
`orders_registration.py:33-45`):

```
{
  "success": False,
  "error": "KIS mock account is disabled or missing required configuration: KIS_MOCK_ENABLED, KIS_MOCK_APP_KEY",
  "source": "kis",
  "account_mode": "kis_mock",
}
```

ROB-56 must not change this shape; it must propagate it from every
`kis_mock_*` tool.

### 4.6 `_KISSettingsView` regression guard

Add a unit test that constructs `_KISSettingsView(is_mock=True)` and asserts
that `kis_app_key`, `kis_app_secret`, `kis_account_no`, `kis_base_url`, and
`kis_access_token` route to the mock fields, regardless of whether the
underlying live `Settings.kis_app_key` is set. (Phase-2 carry from ROB-19.)

---

## 5. MCP split design — shared read-only vs side-effect live/mock order

### 5.1 Side-effect audit (per-tool determination)

For each registered tool, classify as **side-effect order** (must split),
**read-only with account scoping** (keep in both profiles), or
**side-effect-free research** (keep in both profiles).

| Registration helper | Classification | Profile inclusion |
|---|---|---|
| `register_order_tools` (`place_order`, `cancel_order`, `modify_order`, `get_order_history`) | side-effect order, ambiguous mode | `default` only |
| `register_kis_mock_order_tools` (new) | side-effect order, mock-only | `default`, `hermes-paper-kis` |
| `register_kis_live_order_tools` (new) | side-effect order, live-only | `default` only |
| `register_market_data_tools` | side-effect-free | both |
| `register_fundamentals_tools` | side-effect-free | both |
| `register_analysis_tools` | side-effect-free | both |
| `register_news_tools` | side-effect-free | both |
| `register_market_report_tools` | read DB only | both |
| `register_market_brief_tools` | read DB only | both |
| `register_watch_alert_tools` | read/write internal alerts (no broker) | both |
| `register_trade_profile_tools` | DB CRUD on user-owned profiles, no broker | both |
| `register_user_settings_tools` | DB CRUD on user settings, no broker | both |
| `register_portfolio_tools` (`get_holdings`, `portfolio_cash`, `portfolio_avg_cost`) | read-only, accepts `account_mode` (already mock-safe via ROB-28) | both |
| `register_trade_journal_tools` | DB read/write on trade journal | both |
| `register_paper_account_tools` | `db_simulated` only — no broker | both |
| `register_paper_analytics_tools` | `db_simulated` only — no broker | both |
| `register_paper_journal_tools` | `db_simulated` only — no broker | both |
| `register_paperclip_comment_tools` | external Paperclip API; not a broker order | both |
| `register_execution_comment_tools` | DB read/write on execution comments | both |

The implementer must verify the "side-effect-free" classification by
reading each helper before adding it to the always-on list. If any helper
turns out to issue broker mutations, exclude it from `hermes-paper-kis` and
note it in the test.

### 5.2 Tool naming choice

Repo precedent uses snake_case verb-noun (`get_holdings`, `place_order`,
`portfolio_cash`, `kis_websocket_*`). Prefix-style **`kis_live_*`** /
**`kis_mock_*`** matches both readability and the existing
`kis_websocket_*` family. Suffix-style (`place_order_kis_live`) is rejected
because it places the most important semantic distinction at the end of the
name.

Final names (must exactly match in the implementation):
- `kis_live_place_order`, `kis_live_cancel_order`, `kis_live_modify_order`, `kis_live_get_order_history`
- `kis_mock_place_order`, `kis_mock_cancel_order`, `kis_mock_modify_order`, `kis_mock_get_order_history`

### 5.3 Argument compatibility

The new tools have the **same signature** as their canonical counterparts
in `orders_registration.py`, with **two differences**:
1. `account_mode`/`account_type` parameters are accepted **only** if equal
   to the tool's pinned mode. Mismatches return:
   ```
   {"success": False,
    "error": "kis_mock_place_order does not accept account_mode='kis_live'",
    "source": "mcp",
    "account_mode": "kis_mock"}
   ```
   Omitted is the common case and proceeds with the pinned mode.
2. The `kis_mock_*` variants reject crypto / `db_simulated` paths the same
   way `cancel_order`/`modify_order` already do for `db_simulated`.

This contract must be unit-tested per tool.

---

## 6. Capability model update plan

Scope: **metadata only.** No order routing change in this issue.

Add `app/services/brokers/capabilities.py`:

```
class Market(StrEnum):
    KR_EQUITY = "kr_equity"
    US_EQUITY = "us_equity"
    CRYPTO = "crypto"

class Broker(StrEnum):
    KIS = "kis"
    KIWOOM = "kiwoom"
    UPBIT = "upbit"

@dataclass(frozen=True)
class BrokerCapability:
    broker: Broker
    markets: frozenset[Market]
    supports_paper: bool
    supports_live: bool

BROKER_CAPABILITIES: Mapping[Broker, BrokerCapability] = {
    Broker.KIS: BrokerCapability(
        broker=Broker.KIS,
        markets=frozenset({Market.KR_EQUITY, Market.US_EQUITY}),
        supports_paper=True,   # official KIS mock
        supports_live=True,
    ),
    Broker.KIWOOM: BrokerCapability(
        broker=Broker.KIWOOM,
        markets=frozenset({Market.KR_EQUITY, Market.US_EQUITY}),
        supports_paper=False,  # not yet integrated
        supports_live=False,
    ),
    Broker.UPBIT: BrokerCapability(
        broker=Broker.UPBIT,
        markets=frozenset({Market.CRYPTO}),
        supports_paper=False,
        supports_live=True,
    ),
}
```

Tests pin the registry; **no production code consumes it yet**. This
prepares ROB-56's stated capability claim without changing routing.

---

## 7. Step-by-step implementation tasks (for Sonnet)

Each task is intended to be a single commit on
`feature/ROB-56-kis-mock-hard-separation`. After every task, run the
focused pytest set in §8.

**T1. Add `McpProfile` enum and env wiring.**
- Create `app/mcp_server/profiles.py` with the `McpProfile` StrEnum and a
  helper `resolve_mcp_profile(env: str | None) -> McpProfile` that defaults
  to `DEFAULT` and validates strings.
- Wire `MCP_PROFILE` env var in `app/mcp_server/main.py` and pass the
  resolved profile into `register_all_tools(mcp, profile=...)`. Do **not**
  read `MCP_PROFILE` directly anywhere else.
- Update `app/mcp_server/env_utils.py` only if needed (likely not).

**T2. Refactor `registry.register_all_tools` to accept `profile`.**
- Add `profile: McpProfile = McpProfile.DEFAULT` parameter.
- Move `register_order_tools(mcp)` behind a `profile is DEFAULT` branch.
- Keep the rest of the registration order intact for `default`.
- Add a docstring listing which helpers run in each profile, mirroring the
  table in §5.1.

**T3. Implement `register_kis_mock_order_tools` and
`register_kis_live_order_tools`.**
- New file `app/mcp_server/tooling/orders_kis_variants.py`.
- For each of the eight tool names in §5.2, register a thin wrapper that:
  - Validates the optional `account_mode`/`account_type` arg against the
    tool's pinned mode (mismatch → structured error, no delegation).
  - For mock variants: call `_kis_mock_config_error()` and short-circuit if
    config is missing.
  - Delegate to the existing `_place_order_impl` / `cancel_order_impl` /
    `modify_order_impl` / `orders_history.get_order_history_impl` with
    `is_mock` hard-pinned.
  - Wrap the result in `apply_account_routing_metadata(routing)` so the
    response envelope matches today.
- Export `register_kis_mock_order_tools` and `register_kis_live_order_tools`
  via `app/mcp_server/tooling/__init__.py` if that module currently
  re-exports the registration helpers; otherwise import them directly in
  `registry.py`.

**T4. Wire the new variants into `registry.py`.**
- In `default`: call both `register_kis_live_order_tools` and
  `register_kis_mock_order_tools` after `register_order_tools`.
- In `hermes-paper-kis`: call only `register_kis_mock_order_tools`.

**T5. Add broker capability registry.**
- Create `app/services/brokers/capabilities.py` per §6.
- Do **not** import it from any production code path. Only tests.

**T6. Add the `_KISSettingsView` regression test.**
- New file `tests/test_kis_settings_view_isolation.py`. Pin: when
  constructed with `is_mock=True`, the five hot fields return mock values,
  and (with monkeypatched `settings.kis_app_key`) live values are not
  observable through the view.

**T7. Add MCP profile registration tests.**
- New file `tests/test_mcp_profiles.py`. See §8 for cases.

**T8. Add tool-naming/contract tests for the new variants.**
- New file `tests/test_mcp_kis_order_variants.py`. See §8 for cases.

**T9. Add capability registry test.**
- New file `tests/test_broker_capabilities.py`. Pin the per-broker market
  sets and `supports_paper` / `supports_live` flags.

**T10. Documentation.**
- Append a new section to `app/mcp_server/README.md` documenting the
  profile env var, the `kis_live_*` / `kis_mock_*` tool names, and the
  fact that `hermes-paper-kis` omits live order surfaces.
- Add a brief operator note to `env.example` near the existing `KIS_MOCK_*`
  block: "Set MCP_PROFILE=hermes-paper-kis on paper-only deployments."

**T11. Run the full focused test set in §8 and `ruff` / `ruff format`.**
Do not run the integration or slow markers.

**T12. Update CHANGELOG.md** with a brief entry under "Unreleased / Added".

**Out of scope reminders:**
- Do not change any `_place_order_impl` / `cancel_order_impl` / behavior.
- Do not modify Alembic migrations.
- Do not register `kis_live_*` tools in the `hermes-paper-kis` profile
  for any reason.
- Do not introduce a Kiwoom client.

---

## 8. Test plan

All tests in this plan are unit/lightweight and use the existing
`tests/_mcp_tooling_support.py::DummyMCP` harness. None require a live
broker connection or PostgreSQL.

### 8.1 New tests

**`tests/test_mcp_profiles.py`**
- `test_default_profile_registers_legacy_order_tools`: build `DummyMCP`,
  call `register_all_tools(mcp, profile=McpProfile.DEFAULT)`. Assert
  `{place_order, cancel_order, modify_order, get_order_history}` ⊆ tools.
- `test_default_profile_also_registers_typed_kis_variants`: in DEFAULT,
  assert all eight `kis_live_*` and `kis_mock_*` names are present.
- `test_hermes_paper_kis_does_not_register_live_order_tools`: build
  `DummyMCP`, call `register_all_tools(mcp, profile=HERMES_PAPER_KIS)`.
  Assert **none of** `{place_order, cancel_order, modify_order,
  get_order_history, kis_live_place_order, kis_live_cancel_order,
  kis_live_modify_order, kis_live_get_order_history}` are in `mcp.tools`.
- `test_hermes_paper_kis_registers_kis_mock_order_tools`: same setup,
  assert all four `kis_mock_*` tool names present.
- `test_hermes_paper_kis_registers_readonly_research_tools`: assert at
  least `get_holdings`, `portfolio_cash`, plus a representative read-only
  tool from each helper (e.g. `get_quote`, `get_company_profile` if they
  exist) are present.
- `test_resolve_mcp_profile_default_and_explicit_and_invalid`: covers
  `None`, `""`, `"default"`, `"hermes-paper-kis"`, and an invalid string
  raising `ValueError`.

**`tests/test_mcp_kis_order_variants.py`** (mock with monkeypatched impls)
- For each of `kis_mock_place_order` / `kis_mock_cancel_order` /
  `kis_mock_modify_order` / `kis_mock_get_order_history`:
  - `..._fails_closed_when_config_missing`: monkeypatch
    `validate_kis_mock_config` to return missing names; assert the call
    short-circuits with the standardized envelope (`success=False`,
    `account_mode="kis_mock"`, error string contains the missing names).
  - `..._passes_is_mock_true_to_impl`: monkeypatch the underlying
    `_place_order_impl` / `cancel_order_impl` / `modify_order_impl` /
    `get_order_history_impl` and assert it was called with
    `is_mock=True`.
  - `..._rejects_account_mode_kis_live_argument`: pass
    `account_mode="kis_live"` and assert structured rejection (no
    delegation).
- For each of `kis_live_place_order` / `kis_live_cancel_order` /
  `kis_live_modify_order` / `kis_live_get_order_history`:
  - `..._passes_is_mock_false_to_impl`.
  - `..._rejects_account_mode_kis_mock_argument`.

**`tests/test_kis_settings_view_isolation.py`**
- `test_mock_view_does_not_leak_live_app_key`: monkeypatch
  `settings.kis_app_key` to `"LIVE-KEY"` and `settings.kis_mock_app_key` to
  `"MOCK-KEY"`; assert `_KISSettingsView(is_mock=True).kis_app_key ==
  "MOCK-KEY"`.
- Repeat for `kis_app_secret`, `kis_account_no`, `kis_base_url`,
  `kis_access_token`.
- `test_live_view_does_not_leak_mock_app_key`: symmetric assertion for
  `is_mock=False`.

**`tests/test_broker_capabilities.py`**
- `test_kis_supports_kr_and_us`: assert
  `BROKER_CAPABILITIES[Broker.KIS].markets == {Market.KR_EQUITY,
  Market.US_EQUITY}`.
- `test_kiwoom_capability_metadata_only`: assert Kiwoom is registered with
  `supports_paper=False` and `supports_live=False` and the same KR+US
  markets.
- `test_upbit_supports_only_crypto`.

### 8.2 Regression coverage to keep green (no edits expected)

- `tests/test_mcp_account_modes.py` — full file (default routing, alias
  handling, fail-closed envelope, `is_mock=True` propagation).
- `tests/test_kis_mock_routing.py`, `tests/test_kis_mock_order_ledger.py`.
- `tests/test_kis_constants.py::test_mock_unsupported_tr_set_is_documented`.
- `tests/test_orders_history_kis_mock.py`,
  `tests/test_portfolio_cash_kis_mock.py`,
  `tests/test_kis_integrated_margin_mock.py`.

### 8.3 Focused pytest target

After each task and at the end of T11:

```
uv run pytest \
  tests/test_mcp_profiles.py \
  tests/test_mcp_kis_order_variants.py \
  tests/test_kis_settings_view_isolation.py \
  tests/test_broker_capabilities.py \
  tests/test_mcp_account_modes.py \
  tests/test_kis_mock_routing.py \
  tests/test_kis_mock_order_ledger.py \
  tests/test_kis_constants.py \
  tests/test_orders_history_kis_mock.py \
  tests/test_portfolio_cash_kis_mock.py \
  tests/test_kis_integrated_margin_mock.py \
  tests/test_paper_order_handler.py \
  tests/test_mcp_order_tools.py \
  tests/test_mcp_place_order.py \
  -q
```

Lint:

```
uv run ruff check app tests
uv run ruff format --check app tests
```

`ty` typecheck if it's part of the standard make target:

```
uv run ty check app/mcp_server/profiles.py \
                app/mcp_server/tooling/orders_kis_variants.py \
                app/mcp_server/tooling/registry.py \
                app/services/brokers/capabilities.py
```

(Or `make typecheck` if it doesn't take a path.)

---

## 9. Rollout / safety notes

- **`MCP_PROFILE` defaults to `default`.** Existing deployments are
  unaffected unless an operator explicitly sets the env var.
- **`hermes-paper-kis` requires `KIS_MOCK_ENABLED=true` and the four mock
  vars** to do anything useful. With them missing, the `kis_mock_*` tools
  return the standardized fail-closed envelope; the deployment is then
  effectively read-only KIS, which is the intended safe state.
- **No live order code path changes.** The new live variants
  (`kis_live_*`) delegate to the same `_place_order_impl` etc. used by
  today's `place_order`. Behavior on `default` is unchanged.
- **`dry_run=True` defaults preserved** in all wrappers (must be verified
  in the contract tests).
- **Audit log surface.** With distinct tool names, the `CallerIdentityMiddleware`
  / `McpToolCallSentryMiddleware` will record tool name = `kis_live_place_order`
  vs `kis_mock_place_order`, making "which surface was called" trivially
  visible in Sentry traces.
- **Operator runbook** (suggested addendum to
  `app/mcp_server/README.md`): when validating a paper-only environment,
  hit the MCP `/mcp` listing and confirm absence of `kis_live_*` and the
  legacy ambiguous tools.
- **Secrets:** never print KIS keys. The fail-closed envelope already
  returns names only; reuse it.

---

## 10. Out-of-scope / follow-ups

- **F1.** Upbit / crypto in `hermes-paper-kis`. Today crypto routes
  through `place_order`, which won't be in the paper-only profile. If
  paper-only crypto is needed, file a follow-up to add `db_simulated`
  Upbit variants or split similarly.
- **F2.** Kiwoom broker integration (live or paper). ROB-56 only adds
  capability metadata; no live client.
- **F3.** Capability-driven routing in `_execute_order`. Today it's still
  hard-coded by `market_type`; once Kiwoom or a second KR broker exists,
  follow up to consume `BROKER_CAPABILITIES`.
- **F4.** Operator smoke test against the real KIS mock server. Phase-2
  carry from ROB-19. Out of scope here because it requires a live mock
  account.
- **F5.** Move the legacy ambiguous `place_order` / `cancel_order` /
  `modify_order` / `get_order_history` to deprecated status once typed
  callers have migrated. ROB-56 only adds the typed variants alongside.

---

## Handoff

When Sonnet picks this up:

- Read this plan top-to-bottom before starting T1.
- Implement task-by-task; do not skip ahead.
- After each task, run the focused pytest set in §8.3 and only then
  commit. Use commit messages of the form
  `feat(ROB-56): <task summary>` or `test(ROB-56): <task summary>`.
- Do **not** rebase, force-push, or amend any merged commits.
- If you find an inconsistency between this plan and the current code,
  stop and surface it before deviating.
