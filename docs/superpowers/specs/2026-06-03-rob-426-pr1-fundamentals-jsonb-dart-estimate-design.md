# ROB-426 PR1 — fundamentals raw_payload JSONB safety + DART estimate-only preflight

- **Linear:** ROB-426 ([workstream] /invest/screener production data recovery + snapshot pipeline hardening)
- **Date:** 2026-06-03
- **Status:** design approved (brainstorming), pre-plan
- **Scope:** This document is the umbrella decomposition for ROB-426 **plus** the
  detailed design for **PR1 only**. PR2 and PR3 get their own spec → plan → PR
  cycles.

---

## 0. Umbrella — ROB-426 decomposition (3 stacked PRs)

ROB-426 began as a single bug ("fundamentals `raw_payload` NaN + dry-run
semantics") and was expanded into a `/invest/screener` production data
**recovery + snapshot pipeline hardening** bundle (7 root causes, scopes A–E).
We deliberately split it into three stacked PRs rather than one mega-PR, to keep
review and rollback bounded and to ship value incrementally:

| PR | Title | Issue scope | Why this boundary |
| -- | ----- | ----------- | ----------------- |
| **PR1** *(this doc)* | fundamentals JSONB safety + DART estimate-only | B (fundamentals JSONB + estimate-only), E (regression) for those | Small, independent, fixes the original bug; no dependency on the others |
| **PR2** | partition health selection + commit guards | A (partition health), B (commit guards for screener/quote/valuation), C (refresh runbook), E | Core recovery: stops 20-row partitions shadowing 3.8k-row ones; reuses PR1's shared sanitizer for quote/valuation latent NaN |
| **PR3** | UI/API degraded-state + display enrichment | D (degraded-state reasons, column enrichment, market_cap source), E | Read/presentation layer; depends on PR2 partition-health metadata |

PR2/PR3 are **out of scope for this document** and are referenced only so their
context is not lost. The production data backfill/refresh itself is
operator-gated and is **not** part of any code PR (issue Non-goals).

### Premise verification (code-grounded, 2026-06-03)

All PR1 premises were verified against the worktree before writing this spec:

- `redact_sensitive_payload` (`app/services/market_quote_snapshots/builder.py:34-43`)
  redacts by **key name only**; it does **not** recurse and does **not**
  sanitize non-finite floats. Imported by the fundamentals builder
  (`app/services/financial_fundamentals_snapshots/builder.py:18`). **(bug confirmed)**
- `raw_payload` column is `JSONB`
  (`app/models/financial_fundamentals_snapshot.py:101`). PostgreSQL `jsonb`
  rejects bare `NaN`/`Infinity`/`-Infinity` (RFC 8259). **(failure point confirmed)**
- raw dicts are built via `DataFrame.to_dict(orient="records")` at the annual
  (`builder.py:234-243`, wrapped at `:264`) and quarterly (`builder.py:289-295`,
  wrapped at `:314`) sites — DART income-statement/dividend frames can carry
  `NaN` for missing divisions/periods. **(source confirmed)**
- A proven recursive sanitizer already exists:
  `sanitize_non_finite` (`app/schemas/validated_run_card.py:48-62`, ROB-329). It
  depends only on `math` + `collections.abc.Mapping` (no pydantic/schema
  coupling), so it is safe to relocate. It has **two live external importers** —
  `app/services/investment_snapshots/run_card_ingest.py:35` and
  `tests/test_validated_run_card_citation.py:24` — so the relocation **must**
  keep `validated_run_card.sanitize_non_finite` resolvable (re-export). **(reuse + re-export need confirmed)**
- dry-run still fetches: `run_financial_fundamentals_snapshot_build`
  (`app/jobs/financial_fundamentals_snapshots.py:162-245`) computes a cheap
  arithmetic `projected` (`:199`) but then **always** runs
  `build_financial_fundamentals_for_symbols` with `default_dart_fetcher`
  (`:180`, `:211-218`) regardless of `request.commit`, consuming DART budget on a
  dry-run. There is **no** estimate-only / no-fetch path. **(bug confirmed)**
- The CLI (`scripts/build_financial_fundamentals_snapshots.py`) already prints
  the arithmetic `projected` (`:96-100`) **before** calling the job, but then
  always calls the (fetching) job. `--commit` default-off; `args.dry_run = not
  args.commit` (`:58`). **(confirmed)**

---

## PR1 detailed design

### 1. Goal

Two independent, low-risk fixes in the fundamentals/DART area:

1. **JSONB safety.** Guarantee fundamentals `raw_payload` is strict-JSON /
   `jsonb` safe: every nested non-finite float (`NaN`/`Infinity`/`-Infinity`) →
   `null` before persistence.
2. **DART estimate-only preflight + dry-run transparency.** Provide a true
   no-fetch `--estimate-only` mode that reports the projected DART request count
   without consuming budget, and make the existing `--dry-run` honest about the
   fact that it *does* fetch and consume budget.

### 2. Locked design decisions

- **D1 (sanitizer location).** Relocate `sanitize_non_finite` to a neutral
  shared module **`app/core/json_safe.py`**. `app/schemas/validated_run_card.py`
  re-imports it from there (back-compat, behavior unchanged). Rationale: the
  fundamentals service should not depend on a run-card *schema* module, and
  PR2/PR3 will reuse the same sanitizer for quote/valuation payloads.
- **D2 (sanitize scope).** Apply only at the **two** fundamentals raw_payload
  sites (`builder.py:264`, `:314`). Other snapshot builders share
  `redact_sensitive_payload` and have the **same latent bug**, but fixing them is
  deferred to PR2/PR3 (which touch those families) to keep PR1's blast radius
  minimal. The latent risk is recorded in §7.
- **D3 (dry-run semantics).** Keep `--dry-run` (default) as **fetch-validate,
  no-write** — its fetch is intentional (validates parse/transform). Add a
  **separate** `--estimate-only` no-fetch mode. Make the budget cost of dry-run
  explicit in help text and runtime output. Do **not** silently change what
  `--dry-run` does.
- **D4 (no interactive prompt).** These CLIs are non-interactive. "Operator can
  abort" is satisfied by printing the projected request count **before** any
  fetch (already happens), not by a `[y/N]` prompt.

### 3. Components & changes

#### 3.1 `app/core/json_safe.py` (new)

- Houses `sanitize_non_finite(value: Any) -> Any` — moved verbatim from
  `validated_run_card.py` (recursive; `bool`/`int` untouched; non-finite
  `float` → `None`; returns a new structure, input not mutated).
- `app/schemas/validated_run_card.py` replaces its local definition with
  `from app.core.json_safe import sanitize_non_finite` (re-export). The two live
  external importers — `app/services/investment_snapshots/run_card_ingest.py:35`
  and `tests/test_validated_run_card_citation.py:24` — continue to resolve
  `from app.schemas.validated_run_card import sanitize_non_finite` unchanged via
  the re-export. No behavior change for ROB-329 consumers. (Repointing those
  importers directly at `app.core.json_safe` is optional cleanup, not required.)

#### 3.2 `app/services/financial_fundamentals_snapshots/builder.py`

- Import `sanitize_non_finite` from `app.core.json_safe`.
- Annual (`:264`) and quarterly (`:314`):
  `raw_payload=redact_sensitive_payload(raw)` →
  `raw_payload=redact_sensitive_payload(sanitize_non_finite(raw))`.
  - Order: sanitize **inner** ("clean the data first"), redact outer. For
    fundamentals, `raw` has no sensitive keys, so redact is effectively a
    pass-through; the wrapper is kept for consistency with other builders.

#### 3.3 Estimate-only preflight (job + request + result)

- **Request** (`FinancialFundamentalsSnapshotBuildRequest`, a
  `@dataclass(frozen=True)`): add `estimate_only: bool = False` (additive,
  trailing default field; default preserves current behavior).
- **Result** (`FinancialFundamentalsSnapshotBuildResult`, a
  `@dataclass(frozen=True)`): add `projected_requests: int | None = None`
  (additive, trailing). Populated on the estimate-only short-circuit (and
  harmlessly available otherwise).
- **Job** (`run_financial_fundamentals_snapshot_build`): after resolving symbols
  and computing `projected` (`:199`), if `request.estimate_only` is set,
  **short-circuit before** `reset_request_count()` / the build call: return a
  result with `committed=False`, `snapshots_built=0`,
  `projected_requests=projected`, and a warning
  `("estimate-only: projected <N> DART requests; no fetch performed",)`. The
  fetcher is never constructed/called → **0 budget consumed**.
  - `estimate_only` and `commit` are mutually exclusive (enforced at the CLI,
    §3.4; the job treats `estimate_only` as authoritative and ignores `commit`
    if both somehow arrive).

#### 3.4 `scripts/build_financial_fundamentals_snapshots.py`

- Add `--estimate-only` (store_true). `parse_args` rejects
  `--estimate-only --commit` (mutually exclusive, like `--all`/`--symbol`).
- Pass `estimate_only=args.estimate_only` into the request.
- **Transparency:** update `--commit` / dry-run help and the projected-print
  (`:98-100`) so it states plainly that **`--dry-run` fetches from DART and
  consumes ~`<projected>` requests**, while `--estimate-only` performs **no
  fetch**. The projected line is already printed before the job call, satisfying
  "operator sees cost before any fetch."
- `_print_result` reports estimate-only distinctly (e.g. "estimate-only: no
  fetch, no rows written").

### 4. Data flow

```
CLI args ──▶ FinancialFundamentalsSnapshotBuildRequest(estimate_only?, commit?)
   │                         │
   │ (print projected, cheap arithmetic, no fetch)
   ▼                         ▼
 run_financial_fundamentals_snapshot_build
   │
   ├─ estimate_only ──▶ return {projected_requests, committed=False, built=0}  ← 0 DART calls
   │
   └─ else ──▶ build_financial_fundamentals_for_symbols(default_dart_fetcher)   ← fetches (dry-run or commit)
                 │
                 └─ _payload_from_{annual,quarterly}
                       raw = {... DataFrame.to_dict ...}        ← may contain NaN/Inf
                       raw_payload = redact_sensitive_payload(sanitize_non_finite(raw))  ← JSONB-safe
                       │
                       └─ (commit only) repository.upsert ──▶ jsonb column  ← never sees non-finite
```

### 5. Error handling

- **Estimate-only invariant:** the fetcher is never invoked (so `default_dart_fetcher`
  is never even constructed) and no DB write occurs. The short-circuit returns
  *before* `reset_request_count()` and the build call, so no DART request is
  counted. There is no public request-count getter today; T2 asserts the invariant
  via the spy fetcher's call count (0) rather than a counter read. Asserted by test T2.
- **Sanitize invariant:** output contains no non-finite float;
  `json.dumps(payload, allow_nan=False)` succeeds; structure, `bool`, and `int`
  preserved. Asserted by T1.
- **Unchanged paths:** `DartDailyRequestBudgetExceeded` handling
  (`jobs.py:221-225`), the budget counter / `BudgetedClient`, and idempotency
  classification are untouched.

### 6. Testing (scope E for PR1)

| ID | Test | Asserts |
| -- | ---- | ------- |
| **T1** | sanitizer regression on builder payload | A fundamentals filing whose income-statement/dividend frame carries `NaN`/`Inf`/`-Inf` → built `raw_payload` has `null` in those positions; `json.dumps(..., allow_nan=False)` succeeds; non-affected fields intact |
| **T1b** | `sanitize_non_finite` unit (moved fn) | `inf/-inf/nan → None`; nested dict/list recursion; `bool`/`int` untouched; input not mutated (parity with prior ROB-329 behavior) |
| **T2** | estimate-only = no fetch | job with `estimate_only=True` + a spy fetcher → spy fetcher **call count == 0**, `projected_requests == len(symbols)*(11\|41)`, `committed=False`, `snapshots_built=0`, no DB write |
| **T3** | dry-run still fetches, no write | dry-run (`commit=False`, `estimate_only=False`) + spy fetcher → fetcher **called**, nothing committed |
| **T4** | CLI arg semantics | `--estimate-only --commit` errors; defaults (`estimate_only=False`, `dry_run=True`); `--estimate-only` sets the flag |
| **T5** | validated_run_card re-export | ROB-329 importer still resolves `sanitize_non_finite` and behaves identically (guards against the relocation breaking an existing consumer) |

### 7. Non-goals / deferred / safety

- **Other builders' latent NaN bug** (`market_quote_snapshots`,
  `market_valuation_snapshots`, `investor_flow`, crypto insight/screener — all
  share `redact_sensitive_payload`): **deferred to PR2/PR3**, which will reuse
  `app/core/json_safe.sanitize_non_finite`. Recorded here so it is not lost.
- **No migration.** `raw_payload` is already `JSONB`; this PR only changes what
  we write into it.
- **No partition-health / commit-guard / loader work** (PR2). **No UI/API
  degraded-state or display enrichment** (PR3).
- **No production backfill/refresh** in this PR; that stays operator-gated.
- **No broker/order/watch/order-intent/trade-journal mutation.** No
  env/secret changes. No scheduler activation.
- **DART budget config** (`opendart_daily_request_budget`) and the
  `increment_and_check_budget` mechanism are reused as-is, not redesigned.

### 8. Acceptance criteria

1. Fundamentals build that encounters non-finite floats produces a `jsonb`-safe
   `raw_payload` (no `NaN`/`Infinity`) and commits without a JSONB error.
2. `--estimate-only` reports projected DART requests and performs **0** DART
   calls / **0** budget consumption / **0** DB writes.
3. `--dry-run` behavior is unchanged (still fetch-validate, no write) but its
   budget cost is stated in help text and runtime output before any fetch.
4. `sanitize_non_finite` lives in `app/core/json_safe.py`; `validated_run_card`
   re-exports it; ROB-329 behavior is unchanged.
5. New tests T1–T5 pass; existing fundamentals/run-card tests stay green.
