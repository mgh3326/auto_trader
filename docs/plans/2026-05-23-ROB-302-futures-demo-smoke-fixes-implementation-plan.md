# ROB-302 — Binance Futures Demo smoke fixes (implementation plan)

**Source spec:** Linear ROB-302 (acts as design doc). Follow-up to ROB-298 PR2 / ROB-299.
**Branch:** `rob-302`
**Deployed main SHA blocked on:** `5786fd52`

## Problem

Hermes ran the Futures Demo smoke (`readiness → preflight → order-test → confirm`)
on the MacBook native runtime and hit three blockers. `--confirm` was never run
(no demo mutation created). Three real bugs + one ergonomics gap:

1. **Credential duplication** — Futures Demo only reads `BINANCE_FUTURES_DEMO_*`;
   operator must duplicate the Spot Demo secret into a second env var. Spot and
   Futures Demo share the same Demo credential.
2. **Preflight 404** — `preflight.py` calls `GET /fapi/v1/account`; `demo-fapi`
   returns 404. `GET /fapi/v2/account` returns 200 with the same credential.
3. **Wrong symbol filters** — `_fetch_symbol_filters()` uses `symbols[0]` from the
   exchangeInfo response. On demo-fapi the `symbol=` query param is not honored and
   the array can lead with BTCUSDT, so XRPUSDT sizing applies BTCUSDT's
   stepSize/precision/min-notional → cap-10 falsely blocked (MIN_NOTIONAL=50),
   cap-60 reaches Binance and fails `-1111 Precision is over the maximum`.

## Bug → fix map (verified against code)

**DECISION (user, D2):** the Demo API key/secret is ONE shared credential, not a
"fallback". Model it as a single canonical `BINANCE_DEMO_API_KEY/SECRET` that both
Spot and Futures Demo read, with the existing per-product vars as optional
overrides. Drop "fallback" wording everywhere.

| # | Location (verified) | Current | Fix |
|---|---------------------|---------|-----|
| 1 | `futures_demo/{readiness,preflight,execution_client}.py` + `spot_demo/{preflight,execution_client}.py` | each reads only its own `BINANCE_{SPOT,FUTURES}_DEMO_API_KEY/SECRET` | shared resolver: per-product var → canonical `BINANCE_DEMO_*` → error. ENABLED gate unchanged. Evidence reports `api_key_source` label only |
| 2 | `preflight.py:58` `_ACCOUNT_PATH = "/fapi/v1/account"` | v1 → 404 on demo-fapi | `_ACCOUNT_PATH = "/fapi/v2/account"`; `_summarize_account` fields (canTrade/assets[].walletBalance/positions[].positionAmt) are identical in v2 → no shape change |
| 3 | `scripts/binance_futures_demo_smoke.py:926` `filters = symbols[0]` | picks index 0 (may be BTCUSDT) | select row where `s["symbol"] == requested`; fail closed if absent; prefer `MARKET_LOT_SIZE.stepSize` over `LOT_SIZE.stepSize` for MARKET orders; honor `quantityPrecision` |

Confirm reconciliation (`execution_client.py` positionRisk + openOrders) does NOT
touch `/account`, so the v2 change is isolated to `preflight.py`.

## Proposed implementation

### Shared canonical Demo credential resolver (DRY — 5 call sites today)
New module `app/services/brokers/binance/demo/credentials.py` (the `demo/` shared
namespace already exists alongside `demo/ledger/`):

```
resolve_demo_credentials(product: "spot" | "futures", env) -> ResolvedDemoCredential
  PAIR resolution by SOURCE (Codex #2 — never mix key/secret across sources):
    1. product-specific source: if EITHER product var is set, the PAIR must come
       from product vars. If only one of the product key/secret is set → FAIL
       CLOSED (BinanceDemoIncompleteCredentialOverride). Never backfill the
       missing half from canonical (that would pair a product key with a
       canonical secret).
    2. else canonical source: both BINANCE_DEMO_API_KEY + BINANCE_DEMO_API_SECRET,
       or fail closed if only one present.
    3. else → MissingCredentials.
  source resolution per product:
    spot:    SPOT_DEMO pair  -> DEMO pair  -> missing
    futures: FUTURES_DEMO pair -> DEMO pair -> missing
  returns:
    api_key, api_secret (held privately, never logged) — always a matched pair
    credential_source in {"futures_demo_env","spot_demo_env","shared_demo_env"}  (one label, the pair came from one source)
```

Isolation invariant (preserved):
- product-specific overrides do NOT cross — a Spot-specific key never resolves for
  Futures and vice-versa. Crossing happens ONLY through the explicit canonical var.
- activation stays independently gated by each lane's `*_ENABLED` flag.
- host allowlists (`demo-api` / `demo-fapi`) untouched, still fail-closed at transport.

Call sites switch to the resolver:
- `futures_demo/readiness.py`, `futures_demo/preflight.py::from_env`,
  `futures_demo/execution_client.py::from_env`
- `spot_demo/preflight.py::from_env`, `spot_demo/execution_client.py::from_env`

**Spot Demo is a deployed, working lane** → the change MUST be strictly additive:
when `BINANCE_SPOT_DEMO_API_KEY` is set, behavior is byte-identical to today
(per-product var wins). Regression tests prove this (see test plan).

- Readiness `to_evidence_dict` adds `api_key_source` / `api_secret_source`
  (presence + source label only, never a value).

### Preflight endpoint
- `preflight.py:58` → `/fapi/v2/account`. Update docstrings (`:4`, `:262`, `:280`).

### Symbol filter/sizing (smoke script)
**DECISION (user, M1 REVERSED after Codex #6 — verified): floor is NOT enough;
add explicit quantityPrecision formatting.**

Root cause (verified, confidence 9/10): `sizing.py:88` floors via
`int(raw/step) * step_size`. When `step_size` comes from the exchangeInfo string
`"0.10000000"` it carries exponent -8, so floored qty = `Decimal("30.00000000")`.
`execution_client.py:290` serializes with `format(qty, "f")` → `"30.00000000"`
(8 decimals) → exceeds XRPUSDT `quantityPrecision=1` → **-1111 persists** even with
the correct symbol row. Numeric value is fine; the submitted STRING is wrong.

- `_fetch_symbol_filters()`:
  - match `s for s in symbols if s["symbol"] == symbol`; `RuntimeError` if absent.
  - read `MARKET_LOT_SIZE.stepSize` first, fall back to `LOT_SIZE.stepSize`.
  - **also return `quantity_precision` from the symbol row.**
  - keep MIN_NOTIONAL/NOTIONAL extraction for the matched row.
- Smoke script quantizes `sizing.qty` to `quantity_precision`
  (`qty.quantize(Decimal(1).scaleb(-quantity_precision), ROUND_DOWN)`) BEFORE
  passing to `order_test` / `submit_order`, so `format(qty, "f")` emits a
  precision-valid string. `sizing.py` and `execution_client.py` signatures
  unchanged (fix contained at the smoke-script boundary). Codex #7: precision is
  used for output formatting, not as the sizing increment (that stays stepSize).

## Test plan (regression coverage required by acceptance criteria)

- `tests/services/brokers/binance/demo/test_credentials.py` (NEW): per-product
  resolution chain (product-specific PAIR wins; canonical PAIR used when product
  vars absent; missing→error); **partial product override (key XOR secret) →
  fail closed, never mixes product key with canonical secret (Codex #2)**;
  `credential_source` label correct for each branch; no secret value in any
  returned dataclass field / repr / evidence dict.
- `futures_demo/test_env_readiness.py`: ready=true via canonical `BINANCE_DEMO_*`;
  source label = `shared_demo_env`.
- `futures_demo/test_spot_demo_env_does_not_activate_futures.py` (UPDATE): a
  Spot-specific key alone still does NOT resolve for Futures (override isolation
  preserved); only canonical or futures-specific does.
- **Spot regression (CRITICAL, IRON RULE — change touches a deployed lane):**
  spot `from_env` byte-identical when `BINANCE_SPOT_DEMO_API_KEY` set; spot resolves
  via canonical when only `BINANCE_DEMO_*` set; spot stays disabled when
  `BINANCE_SPOT_DEMO_ENABLED` unset regardless of creds.
- `futures_demo/test_preflight.py`: update 5 `/fapi/v1/account` URL assertions →
  `/fapi/v2/account`. **Do NOT assert `account_type == "FUTURES"` on v2 (Codex #4 —
  accountType absent from v2 example; `.get()` tolerates None).**
- `tests/scripts/test_binance_futures_demo_smoke.py`: exchangeInfo response where
  BTCUSDT is index 0 and XRPUSDT appears later → sizing uses XRPUSDT row; missing
  symbol → fail closed; `MARKET_LOT_SIZE` preferred over `LOT_SIZE`; **regression
  for Codex #6: stepSize string `"0.10000000"` → submitted `quantity` STRING has
  exactly `quantity_precision` decimals (e.g. `"30"` or `"30.0"`, NOT
  `"30.00000000"`); min-notional correct for matched row.**

## Safety boundaries (unchanged, must stay green)
- `demo-fapi.binance.com` only; live/testnet/spot hosts fail-closed at transport.
- `BINANCE_FUTURES_DEMO_ENABLED=true` still required (fallback does not bypass it).
- No scheduler/TaskIQ/Prefect. `--confirm` only on explicit flag. leverage=1.
  reduce-only close. reconcile requires open orders empty AND position flat.
- No secret values in logs/evidence/comments.

## Out of scope (carried from issue non-goals)
- Recurring automation / scheduler activation.
- Live Binance/KIS/Upbit behavior changes.
- Ed25519 signer fallback (still surfaced as UnsupportedAuth).
- `BINANCE_USDM_FUTURES_DEMO_BASE_URL` legacy alias support (default already correct).
- `/fapi/v3/account` (v2 is the empirically verified working endpoint on demo-fapi).
- Migrating Spot Demo to read canonical-only (existing per-product vars stay as
  overrides; no removal).

## What already exists (reuse, not rebuild)
- `app/services/brokers/binance/demo/` shared namespace (currently holds `ledger/`,
  `errors.py`) → natural home for the new shared `credentials.py`.
- `demo/ledger/repository.py` has an AST import guard pattern → mirror if we want to
  keep the resolver service-internal (optional).
- `respx`-based HTTP mocking in `test_preflight.py` → reuse for v2 endpoint tests.
- `_truthy()` exists (duplicated) in both `spot_demo` and `futures_demo` `from_env`;
  consolidating into `demo/credentials.py` removes the dup (DRY win, optional).
- Transport-layer host allowlists (`spot_demo/host_allowlist.py`,
  `futures_demo/host_allowlist.py`) already fail-closed → untouched.

## Failure modes (new/changed codepaths)
| Codepath | Realistic prod failure | Test? | Error handling? | User-visible? |
|----------|------------------------|-------|-----------------|---------------|
| resolver: both vars absent | operator forgets canonical AND product var | yes (new) | `MissingCredentials` raised | clear (refuse to construct) |
| resolver: canonical set, wrong lane enabled | operator sets canonical but not `*_ENABLED` | yes | `*Disabled` raised | clear |
| v2 preflight | demo-fapi changes account schema | partial (summary fields) | `raise_for_status` / KeyError-safe `.get()` | clear (non-zero exit) |
| symbol filter: requested absent | demo-fapi drops XRPUSDT from exchangeInfo | yes (new) | `RuntimeError` fail-closed | clear |
| symbol filter: MARKET_LOT_SIZE missing | symbol exposes only LOT_SIZE | yes | falls back to LOT_SIZE.stepSize | n/a |

No critical gaps: every new failure mode has a test AND fail-closed error handling
AND surfaces a clear non-zero exit (no silent failures).

## Parallelization strategy
| Step | Modules touched | Depends on |
|------|----------------|------------|
| S1 shared resolver + tests | `binance/demo/credentials.py` | — |
| S2 v2 preflight + test update | `futures_demo/preflight.py`, its tests | — (independent of S1) |
| S3 symbol filter/sizing + tests | `scripts/binance_futures_demo_smoke.py`, its tests | — (independent) |
| S4 wire call sites to resolver | `futures_demo/{readiness,preflight,execution_client}.py`, `spot_demo/{preflight,execution_client}.py` | S1 |
| S5 env.example + runbook | `env.example`, `docs/runbooks/` | S1, S4 |

- Lane A: S1 → S4 → S5 (sequential, share resolver contract).
- Lane B: S2 (independent).
- Lane C: S3 (independent).
- Launch A, B, C in parallel. B and C touch disjoint modules from A. Merge order
  any. S5 docs last.

## Implementation Tasks
Synthesized from this review. Each derives from a finding above.

- [ ] **T1 (P1, human: ~2h / CC: ~20min)** — credentials — add shared `resolve_demo_credentials(product, env)`
  - Surfaced by: Arch §1 (5 duplicated call sites; user D2 = canonical model)
  - Files: `app/services/brokers/binance/demo/credentials.py`, `tests/services/brokers/binance/demo/test_credentials.py`
  - Verify: `uv run pytest tests/services/brokers/binance/demo/test_credentials.py -v`
- [ ] **T2 (P1, human: ~1h / CC: ~10min)** — futures/spot from_env — wire all 5 call sites to resolver, add `*_source` to readiness evidence
  - Surfaced by: Arch §1; spot regression (IRON RULE)
  - Files: `futures_demo/{readiness,preflight,execution_client}.py`, `spot_demo/{preflight,execution_client}.py`, their tests
  - Verify: targeted pytest for env/readiness + spot regression tests
- [ ] **T3 (P1, human: ~20min / CC: ~5min)** — preflight — `/fapi/v1/account` → `/fapi/v2/account`
  - Surfaced by: Bug #2 (`preflight.py:58`)
  - Files: `futures_demo/preflight.py`, `tests/.../test_preflight.py` (5 URL asserts)
  - Verify: `uv run pytest tests/services/brokers/binance/futures_demo/test_preflight.py -v`
- [ ] **T4 (P1, human: ~1.5h / CC: ~15min)** — smoke — select requested symbol row + MARKET_LOT_SIZE stepSize + quantize qty to quantityPrecision before submit
  - Surfaced by: Bug #3 (`scripts/binance_futures_demo_smoke.py:926`) + Codex #6 (verified `-1111` via `format(qty,"f")` trailing zeros)
  - Files: `scripts/binance_futures_demo_smoke.py`, `tests/scripts/test_binance_futures_demo_smoke.py`
  - Verify: multi-symbol exchangeInfo regression + submitted-quantity-string precision test (stepSize `"0.10000000"` → no trailing zeros)
- [ ] **T5 (P2, human: ~20min / CC: ~5min)** — docs — env.example canonical `BINANCE_DEMO_*` + runbook update
  - Surfaced by: Arch §1 (operator-facing); project doc convention
  - Files: `env.example`, `docs/runbooks/binance-futures-demo-smoke.md`
  - Verify: manual read; ruff n/a
- [ ] **T6 (P2, human: ~15min / CC: ~5min)** — local gates
  - Surfaced by: acceptance criteria
  - Verify: `uv run ruff check app/services/brokers/binance scripts/binance_futures_demo_smoke.py tests` + full futures/spot demo pytest

## Cross-model review resolutions (Codex gpt-5.5, read-only)
| Codex finding | Disposition |
|---------------|-------------|
| #2 resolver mixes key/secret across sources → mismatched pair | ACCEPTED (baked in): PAIR-by-source resolution, fail closed on partial product override |
| #6 `format(qty,"f")` emits stepSize trailing zeros → -1111 persists | ACCEPTED (user reversed M1): quantize qty to quantityPrecision before submit |
| #4 accountType absent from v2 response | ACCEPTED (baked in): tests do not assert accountType on v2 |
| #1/#3 canonical-both weakens isolation; touching Spot is risk | REJECTED by user (D2 stands): dedup goal requires both lanes read canonical; mitigated by additive change + pair-resolution + spot regression tests + loud source label |
| #5 exchangeInfo has no symbol param | CONFIRMS plan (symbol-row match required) |

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run (bug fix, not a product change) |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | issues_found | 3 substantive: pair-resolution bug, -1111 string precision, isolation tension |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 4 issues (D2 cred model, M1 sizing, spot regression, v2 endpoint) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | n/a (backend/CLI only) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | n/a |

- **CODEX:** found 1 real bug Eng Review missed (independent key/secret resolution → mismatched pair) + verified `-1111` recurs via `format(qty,"f")` trailing zeros. Both accepted into plan.
- **CROSS-MODEL:** 2 tensions surfaced to user — #6 sizing (user accepted Codex, M1 reversed); #1/#3 credential scope (user kept D2, mitigated). Resolutions table above.
- **UNRESOLVED:** 0.
- **VERDICT:** ENG CLEARED — ready to implement. CEO/Design not required for this bug fix.
