# ROB-326 — US dual-paper premarket readiness (KIS mock US + Alpaca Paper)

- **Issue**: ROB-326 (`auto_trader: US dual-paper premarket readiness for KIS mock and Alpaca`)
- **Date**: 2026-05-26
- **Status**: design approved (Approach A), proceeding to implementation plan
- **Author**: Claude Code

## 1. Goal

Provide a **safe premarket readiness path** that, before the 22:30 KST US regular-session
open, can:

1. Read both paper-broker account states **read-only** (KIS mock US overseas + Alpaca Paper).
2. Take 1–3 selected US symbols (from `/invest/screener` or a snapshot-backed
   `/invest/reports` item) and produce a **dual-broker preview/preflight packet** that
   reports each broker independently as `previewed | blocked | unsupported | error`.
3. Guarantee that **no submit, cancel, modify, or place** path is reachable from this
   premarket flow — submit stays default-disabled and is documented as a separate,
   confirm-gated step in the handoff runbook for after 22:30 KST.

This is **paper/mock only** and makes **no live-trading recommendation**.

## 2. Non-goals / hard safety boundaries (Scope D)

- No live KIS orders. KIS adapter pins `is_mock=True`; no live TR_ID / live host reachable.
- No market orders (`order_type` fixed to `limit`).
- No shorting (`side` fixed to `buy`).
- No automatic submit. `submit_enabled` is always `False` on this path.
- No broker / order / watch / order-intent mutation during premarket preview work.
- No scheduler / Prefect / TaskIQ / cron registration or unpause.
- No recurring daemon.
- No frontend work (CLI + MCP surface only this issue).
- If quote/session data is stale or unavailable, the preview surfaces a warning and the
  submit path (already disabled) stays blocked with a reason; limit-sanity check is skipped.

## 3. Confirmed inputs / decisions

| Decision | Resolution |
|---|---|
| Credentials available on host | **Both** KIS mock US + Alpaca Paper available → AC1 (read both account states) is in scope today |
| Today's deliverable | preview/preflight + capability matrix + tests + runbook (submit code untouched) |
| Surface | **MCP tool + smoke CLI wrapper** (no frontend, no HTTP router) |
| Broker key naming | Canonical `account_scope` tokens `kis_mock` + `alpaca_paper`; market carried as `market="us"` field. **No `kis_mock_us` alias** (ROB-297 canonical model) |
| KIS mock journal fields (thesis/target/stop/min_hold) in preview | **Warning, not block** — these are submit-time gates and submit is disabled on this path |
| PR slicing | **2 PRs** (see §10) |

## 4. Architecture (Approach A — thin orchestrator over per-broker adapters)

```
app/schemas/us_dual_paper.py                       # packet + result schemas (new)
app/services/us_dual_paper/
├── __init__.py
├── capability_matrix.py                           # declarative matrix + getter
├── packet.py                                       # DualBrokerPreviewPacket orchestrator
└── adapters/
    ├── __init__.py
    ├── base.py                                     # BrokerPreviewAdapter protocol + shared types
    ├── alpaca.py                                   # AlpacaPaperPreviewAdapter (wraps existing service)
    └── kis_mock.py                                 # KisMockUsPreviewAdapter (new pure gate)
app/mcp_server/tooling/us_dual_paper_preview.py     # MCP tool(s) + registration
scripts/smoke/us_dual_paper_preview_smoke.py        # operator CLI (default-disabled)
docs/runbooks/us-dual-paper-premarket-preview.md    # runbook incl. 22:30 handoff
tests/test_us_dual_paper_*.py
```

**Reused read-only (no behavior change):**
- `app/services/brokers/kis/account.py::inquire_overseas_margin(is_mock=True)` — USD balance / orderable
- `app/services/brokers/alpaca/service.py::get_cash`, `list_positions`
- `app/services/action_report/us/account_snapshot.py` — builds `KISUSAccountSnapshot` (USHolding/USOpenOrder)
- `app/schemas/us_action_report.py` — snapshot/holding schemas
- **Pattern reference (not reused as-is):** `app/services/action_report/us/order_preview.py::preview_kis_us_live_order`
  is the ROB-244 *live* gate (`accountMode="kis_live"`, submit hard-disabled). The new
  `kis_mock` gate copies the pure-gate shape (imports zero broker order modules) but targets
  `account_mode=kis_mock`.

## 5. Components & interfaces

### 5.1 capability_matrix.py
A declarative, secret-free matrix keyed by `account_scope`. For each of `kis_mock` and
`alpaca_paper` (market `us`):

- broker / account_mode name
- supported asset class: `us_equity`
- supported side (this issue): `buy` only
- supported order type (this issue): `limit` only
- preview / dry-run support: yes
- submit gate: confirm-only, default-disabled
- account cash / buying-power read-only: yes (provider noted)
- positions / open-orders read-only: yes/partial (provider noted; KIS open-orders may be partial)
- market-session availability note
- known unsupported / unknown fields

Exposed via a `get_capability_matrix()` function and surfaced by the MCP tool + CLI. Its
shape is pinned by a test (mirrors `tests/test_broker_capabilities.py`).

### 5.2 adapters/base.py
```python
class BrokerPreviewAdapter(Protocol):
    account_scope: str  # "kis_mock" | "alpaca_paper"
    def is_enabled(self) -> bool: ...                       # creds/flag present
    async def read_account_state(self) -> AccountStateSummary: ...   # read-only
    async def preview(self, req: BrokerPreviewRequest) -> BrokerPreviewResult: ...
```
`AccountStateSummary` carries only counts + numbers (cash, buying_power, position_count,
open_order_count) — **never secrets or raw broker payloads**.

### 5.3 adapters/alpaca.py
Wraps `AlpacaService.get_cash()/list_positions()` (read) and the existing side-effect-free
preview logic (`alpaca_paper_preview_order`). Returns a `BrokerPreviewResult`. No submit.

### 5.4 adapters/kis_mock.py — new pure gate
- Pins `is_mock=True` everywhere; asserts no live TR_ID / live host.
- Imports **zero** broker order modules (no submit/place/cancel/modify).
- Reads account via `inquire_overseas_margin(is_mock=True)` + holdings via the US account
  snapshot builder.
- Validates (buy/limit only): `quantity > 0`, `limit_price > 0`, `notional <= cap`,
  limit-price deviation vs reference/quote within bound, duplicate pending order absent,
  USD buying-power sufficiency.
- Journal fields (thesis/strategy/target/stop/min_hold) → **warnings**, not blockers.
- Returns `BrokerPreviewResult` with `status` ∈ {previewed, blocked} and reasons.

### 5.5 packet.py — orchestrator
`build_packet(symbols, *, notional_cap_usd, limit_price_source, limit_price_usd=None)`:
- For each adapter, **independently**:
  - if `not adapter.is_enabled()` → `unsupported` (reason: broker disabled / creds missing)
  - else `try`: read account state (read-only) + preview → `previewed`/`blocked`
  - `except` broker/transport error → `error` with captured reason
- A failure in one broker **never** changes another broker's status (each in its own
  try/except). Results keyed by `account_scope`.

## 6. Schemas (`app/schemas/us_dual_paper.py`)

```python
class DualPaperBrokerStatus(StrEnum):
    PREVIEWED = "previewed"
    BLOCKED   = "blocked"
    UNSUPPORTED = "unsupported"
    ERROR     = "error"

class AccountStateSummary(BaseModel):
    cash_usd: float | None
    buying_power_usd: float | None
    position_count: int | None
    open_order_count: int | None
    # counts/numbers only — no secrets, no raw payloads

class BrokerPreviewResult(BaseModel):
    account_scope: str               # "kis_mock" | "alpaca_paper"
    status: DualPaperBrokerStatus
    reason: str | None = None
    blocked_reasons: list[str] = []
    warnings: list[str] = []
    quantity: float | None = None
    limit_price_usd: float | None = None
    notional_usd: float | None = None
    account_state: AccountStateSummary | None = None
    check_details: dict = {}         # never secrets

class DualBrokerPreviewPacket(BaseModel):
    symbol: str
    market: str = "us"
    side: str = "buy"                # long/buy only this issue
    order_type: str = "limit"        # limit only this issue
    limit_price_source: str          # "quote" | "operator_input" | "report_item"
    notional_cap_usd: float
    generated_at: datetime
    submit_enabled: bool = False     # always False on premarket path
    brokers: dict[str, BrokerPreviewResult]   # keyed by account_scope
```

## 7. Data flow

```
1-3 US symbols selected (/invest/screener or snapshot-backed report item)
 -> limit_price from quote or operator input
      (stale/missing -> warning + skip limit-sanity; submit stays disabled)
 -> orchestrator.build_packet():
        for each broker adapter (independent try/except):
            read_account_state()  [read-only]
            preview()             [side-effect free]
 -> DualBrokerPreviewPacket (per-broker independent status)
 -> MCP tool returns JSON  /  CLI prints JSON evidence lines
 -> manual operator review
 -> [22:30 KST+] runbook: confirm-gated submit smoke  (NOT in this issue's code)
```

## 8. Safety / error handling

- **Import guard test**: `app/services/us_dual_paper/` imports no broker order/submit module
  (AST guard, mirrors PR #898 pattern).
- `submit_enabled` always `False`; no submit/cancel/modify/place symbol reachable.
- `order_type` fixed `limit` (market rejected); `side` fixed `buy` (short rejected).
- KIS adapter `is_mock=True` pinned; live TR_ID / live host assert-blocked.
- Per-broker isolation + disabled broker → `unsupported` (graceful degradation, not crash).
- No secrets logged: `AccountStateSummary` = counts/numbers only; CLI prints env key
  *names* only on missing creds.
- **Default-disabled**: `US_DUAL_PAPER_PREVIEW_ENABLED=true` required for the CLI/MCP path;
  otherwise fail-closed. No scheduler/Prefect/frontend.

## 9. Testing (all fakes; zero real broker mutation — AC7)

- capability_matrix shape pinned.
- kis_mock gate: blocked-reason matrix (cap, deviation, duplicate pending, insufficient
  buying power, qty/limit ≤ 0); journal-missing → warnings not blockers.
- alpaca adapter preview with fake `AlpacaService` → previewed/blocked.
- **orchestrator isolation** (key AC7 test): inject one adapter that raises → that broker
  `error`, the other `previewed`.
- disabled broker → `unsupported`, other still `previewed`.
- import-guard test (no broker order module imports).
- side/type enforcement: short or market → rejected.

## 10. PR slicing

- **PR1** — capability matrix + schemas + both read-only account-state adapters +
  KIS mock US account-read MCP wrapper + `preflight` CLI mode + tests.
- **PR2** — kis_mock preview gate + alpaca preview adapter + dual packet orchestrator +
  `us_dual_paper_preview` MCP tool + full `preview` CLI mode + isolation/import-guard tests +
  runbook (incl. 22:30 KST confirm-gated handoff section).

## 11. Acceptance criteria mapping

| ROB-326 AC | Covered by |
|---|---|
| Read both broker account states, no secrets | PR1 adapters + `AccountStateSummary` + preflight CLI |
| Dual-broker preview/preflight for 1–3 symbols | PR2 orchestrator + packet |
| Independent `previewed/blocked/unsupported/error` w/ reason | §5.5 + §6 + isolation test |
| Position/open-order checks where available | adapters' `read_account_state` |
| Default path before 22:30 KST = preview only, cannot submit | `submit_enabled=False`, no submit symbol, import guard |
| Regular-session handoff runbook (confirm flags, rollback/cancel) | PR2 runbook |
| Tests cover normalization/fail-closed without broker mutation | §9 |
| Docs state paper/mock only, no live recommendation | runbook + matrix notes |

## 12. Out of scope / deferred

- Actual confirm-gated submit code (documented in runbook only).
- Scheduler / Prefect / TaskIQ registration.
- Frontend integration.
- KIS live anything.
