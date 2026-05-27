# ROB-334 — KIS Mock Scalping Execution-Evidence Gate (Design)

- **Issue:** ROB-334 — "KIS mock scalping execution evidence gate for confirmed runs"
- **Follow-up to:** ROB-321 / PR #981
- **Date:** 2026-05-27
- **Status:** Design (approved approach: A)
- **Branch:** `rob-334`

## 1. Problem

The KIS mock domestic scalping round-trip executor
(`app/services/brokers/kis/mock_scalping_exec/`) is fully wired *except* for one
fail-safe stub: `KisMockBroker.confirm_fill()` (`adapters.py:104–112`) always
returns `None`. The executor's `_await_fill()` therefore polls, times out, and
records an `entry_unfilled` / `exit_unconfirmed` anomaly. This is correct
fail-closed behavior — it never fabricates a fill — but it means **no confirmed
mock scalping run can ever observe a real fill**. Before any
`KIS_MOCK_SCALPING_WS_CONFIRM=true`-style run, we need an authoritative, bounded
way to observe mock-order execution/fill evidence.

## 2. Decisions (locked during brainstorming)

| # | Decision |
|---|---|
| D1 | **Authoritative fill-evidence source = bounded order-execution poll** (KIS daily order/execution inquiry). Holdings/cash delta stays secondary (ROB-102). |
| D2 | **Execution-notice WS (`H0STCNI9`) is OUT of scope** — documented as a deferred gap, fail-closed. No AES-decryption / HTS-ID handshake path this PR. |
| D3 | **This PR delivers code + runbook + tests + read-only preflight only.** The one-off confirmed mock smoke (operator-approved) is left as an operator-gated next step (timing/session-independent). |
| D4 | **Read-only smoke proves the evidence path against the real mock API** (calls the daily-execution inquiry read-only, runs the classifier). No order submission. |
| D5 | **Architecture = Approach A**: a pure `fill_evidence` classifier + a bounded-poll adapter wired into `confirm_fill()`; fail-closed to anomaly. Single PR (no migration). |

### 2.1 Source-of-truth correction (verified in code)

The natural-sounding `inquire_korea_orders()` (정정취소가능주문/미체결 조회, TR
`TTTC8036R`) **raises `RuntimeError` for `is_mock=True`** — it is live-only
(`domestic_orders.py:111–115`). The mock-supported source is:

```
KISClient.domestic_orders.inquire_daily_order_domestic(
    start_date, end_date, stock_code="", side="00",
    order_number="", is_mock=True,
)   # mock TR = constants.DOMESTIC_DAILY_ORDER_TR_MOCK  (domestic_orders.py:571–575)
```

Each returned row carries the fill evidence we need:
`ord_qty` (ordered), `ccld_qty` (체결수량 / filled qty), `ccld_unpr` (체결단가 /
fill price), `ccld_amt` (체결금액), plus `ord_no`, `pdno`, `ord_dt`, `ord_tmd`.
It accepts an `order_number` filter and a `stock_code` filter.

The submit response already surfaces the order number as `odno`
(`domestic_orders.py:344`; `_place_order_impl` → `order_id` at
`order_execution.py:459`).

## 3. Components & responsibilities

| Component | Location (new/edit) | Responsibility |
|---|---|---|
| `fill_evidence` (pure) | **new** `app/services/brokers/kis/mock_scalping_exec/fill_evidence.py` | Map daily-order rows (+ submit response, + ordered qty) → `FillEvidence(verdict, filled_qty, avg_price, category, reason_code, detail)`. No I/O, no broker/DB import. |
| Daily-order poll adapter | **edit** `.../mock_scalping_exec/adapters.py` | Read-only call to `inquire_daily_order_domestic(is_mock=True, order_number=odno, stock_code=symbol, start/end=today_kst)`; return raw rows. No mutation. |
| `confirm_fill()` wiring | **edit** `adapters.py:104–112` | Remove the `None` stub: extract `odno` → poll → classifier → return `Fill(price, qty)` only on `verdict=filled`; else `None` (executor degrades to anomaly). Logs the category; never fabricates. |
| Read-only smoke | **new** `scripts/kis_mock_fill_evidence_smoke.py` | Read-only daily-execution inquiry for the mock account (default: today; optional `--order-no` / `--symbol`); run classifier; print verdict + category. default-disabled; no submit; env-key names only (no secret output). |
| Tests | **new** fixtures + unit tests | classifier verdict × category matrix; `confirm_fill` → `None` degrade when unconfirmed; `unsupported mock API` mapping. |
| Runbook | **edit** `docs/runbooks/kis-mock-scalping-smoke.md` | Confirmed-run prerequisite + operator checklist; 4-way separation (code / smoke[deferred] / unsupported gap[H0STCNI9] / no-live·no-scheduler rationale). |

`confirm_fill()` keeps its `Fill | None` contract (minimal blast radius). The
rich `FillEvidence.category`/`reason_code` is surfaced via logs and the
read-only smoke (which calls the classifier directly); the executor-level
anomaly `detail` stays `entry_unfilled` / `exit_unconfirmed`.

## 4. Data flow (confirmed run — operator-gated)

```
submit limit order (confirm=True)  →  submit_result.odno
  ├─ no odno                → FillEvidence(none, "data-precondition" / order_no_missing)  → None (fail-closed)
  └─ odno present
       → bounded poll inquire_daily_order_domestic(is_mock=True, order_number=odno,
                                                    stock_code=symbol, start=end=today_kst)
            (one query per confirm_fill call; executor _await_fill bounds retries — no infinite loop)
         → classifier over matched row(s):
              ├─ filled   (ccld_qty >= ord_qty)      → Fill(avg_price, filled_qty)  → proceed
              ├─ partial  (0 < ccld_qty < ord_qty)   → treated as pending (conservative) → None
              ├─ pending  (row found, ccld_qty == 0) → retry until poll budget spent → None
              ├─ none     (no row for odno)          → None
              └─ unsupported (TR rejected in mock)   → None  + category "unsupported mock API"
fail-closed: no Fill ⇒ executor records entry_unfilled / exit_unconfirmed anomaly (no fabricated fill)
```

The read-only smoke runs the **poll + classifier only** (no submit step) over
existing mock order history.

`avg_price` is derived from `ccld_unpr`, falling back to
`ccld_amt / ccld_qty` when `ccld_unpr` is absent/zero. All numeric parses are
defensive (`Decimal(str(...))`, `None` on failure); non-finite/unparseable →
`code` category, never a silent zero.

## 5. Fail-closed taxonomy (issue's 5 categories)

| category | trigger |
|---|---|
| `code` | classifier/parse exception, unexpected response shape, non-finite numerics |
| `env/config` | mock not configured (creds/account missing), gate env off |
| `data-precondition` | not regular session / empty history / no order number issued |
| `unsupported mock API` | daily-execution inquiry itself rejected by mock (e.g. TR/`rt_cd != 0`) |
| `operator approval needed` | confirmed execution attempted without operator approval (smoke gate) |

Execution-notice WS (`H0STCNI9`) → **deferred gap**, documented in the runbook
as unimplemented + fail-closed; candidate follow-up issue.

## 6. Testing & safety

- **Unit:** synthetic daily-order fixtures driving the classifier across all 5
  verdicts and all 5 fail-closed categories; a test proving `confirm_fill`
  returns `None` (→ executor anomaly) when fill is unconfirmed; a test for the
  `unsupported mock API` mapping when the inquiry raises.
- **Import guard:** `fill_evidence` is pure — no broker/DB/network import
  (follows the existing `ws_bridge`/`mock_scalping_ws` import-guard pattern).
- **Lint/CI:** `ruff check app/ tests/` + import guards green; PR CI green.
  Read-only live smoke is default-disabled and never runs in CI (no creds);
  CI relies on the synthetic fixtures.
- **Safety boundaries (stated in PR description):** no KIS live; no confirmed
  order submitted (operator-gated); no scheduler / Prefect / TaskIQ / cron /
  launchd activation; no persistent `KIS_MOCK_SCALPING_WS_CONFIRM=true`; no
  production env/secret change or logging; no live broker/order/watch/
  order-intent mutation.

## 7. PR structure

Single focused PR. No DB migration — the round-trip ledger columns
(`correlation_id` / `scalping_role` / `exit_reason` / `gross_pnl` / `net_pnl` /
`fee`) already exist from ROB-321.

PR description separates: (1) code/runbook changes; (2) one-off confirmed mock
smoke evidence — **deferred, operator-gated**; (3) remaining unsupported gap —
execution-notice WS (`H0STCNI9`); (4) why no KIS live / no scheduler / no
recurring scalper activation occurred.

## 8. Acceptance-criteria mapping

| Acceptance criterion | Satisfied by |
|---|---|
| Documented source-of-truth path for fill evidence, or explicit fail-closed gap | §2.1 daily-execution inquiry + §5 taxonomy + deferred `H0STCNI9` gap |
| Confirmed scalping stays gated until evidence path available + operator-approved | `KIS_MOCK_SCALPING_WS_CONFIRM` unchanged; smoke deferred (D3) |
| Read-only/bounded smoke command, secret placeholders, no secret output | §3 `kis_mock_fill_evidence_smoke.py` (D4) |
| Runbook: prereqs, command shape, success signal, failure categories, rollback/no-op | §3 + §5 runbook edits |
| Focused tests pass locally; PR CI passes | §6 |
| PR states no live / no confirmed orders / no scheduler / no prod env changes | §7 |

## 9. Out of scope / non-goals

- Execution-notice WS (`H0STCNI9`) subscription, AES-CBC decryption, HTS-ID
  handshake (deferred gap).
- Acting on partial fills (treated conservatively as pending).
- Any scheduler/recurrence, holdings-delta rework (ROB-102 stays as-is,
  secondary), or live-broker change.
