# US overhang gate — source options (design only, no implementation)

- **Issue context**: ROB-1315 §5-1 (buy-side retro 2026-08-22), blocking ROB-1301
- **Status**: 🔴 **DESIGN — awaiting operator decision. No US shadow logic changes
  until one option is approved.** This document deliberately ships without code.
- **Scope**: how the `overhang` gate bit should be produced for US symbols, or
  what to do if it cannot be.

## 1. The problem, stated exactly

The discovery lane's `standard_tool_sequence` step 6 is *"rights-issue /
overhang filter"* (`app/mcp_server/tooling/route_request_lanes.py`), and the
ROB-1301 shadow experiment consumes it as one of three `other_gate_bits`.

The only tool behind that step is `get_disclosures`, which is **DART-only** —
`get_disclosures_impl` returns `"DART functionality not available"` when the
DART client is absent and otherwise calls `list_filings`, a KR filings reader.
There is **no US equivalent anywhere in this repo**: no SEC EDGAR client, no
S-1/S-3/424B filing reader, no ATM-program tracker.

Measured consequence (2026-08-21 US session, retro §5-1): after the input-key
typo was corrected, the valid call returned `n=7 · a_and_b 0 · b_only 0 ·
neither 7`. CRH and UPST failed **on `overhang` alone** under variant B. The
session correctly refused to invent a pass and set `overhang=false` for all
seven, which pinned B-only at zero. `review.trade_forecasts` holds **one**
`shadow`-family row for the whole window. The 4-week collection that is
supposed to decide §4-ⓐ and §4-ⓒ′ has effectively not started on the US side.

**The gate is not wrong. The data is missing.** Those need different fixes, and
conflating them is how a missing measurement turns into a fabricated pass.

## 2. What "overhang" means here

Borrowed from the KR lane: *known future supply of shares that is likely to cap
the price* — a rights issue, a convertible/CB conversion window, a lock-up
expiry, a registered secondary. It is a **forward-looking supply** signal, not a
past-price signal. Any US substitute must preserve that: it has to name a
future or freshly-announced supply event, not infer one from a chart.

## 3. Candidate sources — what this repo can and cannot reach today

| # | Source | In repo now? | Covers overhang? | Verdict |
| --- | --- | --- | --- | --- |
| A | SEC EDGAR full-text / submissions API (S-1, S-3, 424B5, 8-K Item 3.02) | **No** — no client, no key, no ingester | Directly: this is where US dilution is announced | New external dependency; highest fidelity, highest build cost |
| B | Finnhub company profile `shareOutstanding` | Yes (`fundamentals_sources_finnhub._fetch_company_profile_finnhub`) | **Partly, and backwards** — point-in-time only, no history persisted, so it detects dilution *after* it happened | Not a forward signal. Insufficient alone |
| C | Finnhub insider transactions (Form 4) | Yes (`_fetch_insider_transactions_finnhub`) | **No** — insider selling is a different phenomenon from issuance overhang | Reject as a substitute; useful separately |
| D | `market_events` `lockup_expiry` category | **Category exists, ingester does not** — `taxonomy.CATEGORIES` and `catalyst/polarity.py` both list `lockup_expiry` (polarity `negative`), but `scripts/ingest_market_events.SUPPORTED` has only 5 combos and none produce it | Would cover one real overhang class (IPO lock-ups) | Cheapest real coverage; **partial** by construction |
| E | yfinance | Yes | No dilution/filing surface | Reject |

🔴 **Finding worth recording on its own**: `lockup_expiry` is a *declared but
unfed* category. Anything that queries it today gets an empty result that reads
like "no lock-up expiry" rather than "not ingested". That is the same failure
shape as the one this document is about, one layer down.

## 4. Options

### Option 1 — SEC EDGAR ingester (full fidelity)

Add a read-only EDGAR source under `app/services/market_events/` feeding
`market_events` with US `disclosure` rows for dilution-relevant form types
(S-1/S-3 and their /A amendments, 424B*, 8-K Item 3.02), then derive the
`overhang` bit from "any qualifying filing within N days".

- **Pro**: matches the KR semantics closely; reusable well beyond ROB-1301;
  EDGAR is free, documented, and has no auth.
- **Con**: a genuinely new external dependency with its own rate-limit and
  User-Agent policy, form-type taxonomy work, and backfill. Not a
  four-week-collection-unblocking change — this lands *after* the window it is
  supposed to feed.
- **Cost**: large. New ingester + normalizer + taxonomy entries + runbook + CLI
  combo + tests.

### Option 2 — Neutralize `overhang` for US and flag it in the record ✅ recommended

Treat the bit as **not applicable** on US rows rather than false, and make the
shadow record say so.

- `other_gate_bits.overhang` becomes tri-state for US: `true` / `false` /
  absent-meaning-unavailable, with the shared-gate evaluation skipping an
  unavailable bit **for both variants symmetrically**.
- Every affected evaluation carries `overhang_source: "unavailable_us"` and a
  cohort flag (proposal: `gate_neutralized: ["overhang"]`) into
  `forecast_target`, so the 4-week scorer can partition on it and **cannot**
  silently mix neutralized rows with fully-gated ones.
- **Pro**: unblocks US collection immediately; no new dependency; the weakening
  is explicit, symmetric, per-row, and reversible.
- **Con**: it *is* a weakened gate. The US B-only cohort is then "moderate
  support AND unverified overhang", which is a strictly weaker claim than the
  KR cohort. If the experiment later favors B, the US arm cannot by itself
  justify promotion.
- 🔴 **Non-negotiable conditions if this is chosen**:
  1. The neutralization applies to **variant A and variant B identically**. An
     asymmetric skip would manufacture B-only rows out of nothing and destroy
     the experiment.
  2. It applies **only to the shadow evaluator**, never to the live discovery
     lane. The live US path keeps requiring the step-6 filter as it does today.
  3. The flag is mandatory on the record. A neutralized row that does not say
     it was neutralized is worse than no row.
  4. Promotion on US-only evidence is forbidden; the pre-registration
     amendment must say so before the first flagged row is written.

### Option 3 — Manual operator-supplied bit

The session supplies `overhang` from its own reading (news, filings it happened
to see) and the tool records the provenance.

- **Pro**: zero build.
- **Con**: unreproducible, unauditable, and exactly the "session invents a
  gate verdict" failure the 08-21 session correctly refused. **Reject.**

### Option 4 — Drop US from ROB-1301

Score the experiment on KR only and mark US out of scope.

- **Pro**: honest, zero build, keeps the KR cohort clean.
- **Con**: the pre-registration names both markets; amending scope after seeing
  that US produced nothing is a peeking-adjacent move and must be recorded as a
  formal amendment, not a quiet edit.

## 5. Recommendation

**Option 2 now, Option 1 as a separate follow-up issue**, with Option 1
explicitly *not* blocking the current collection window.

Rationale: the retro's binding constraint is that the experiment is not
collecting at all. Option 2 restores collection this week at the cost of a
clearly-labelled weaker US cohort; Option 1 restores fidelity but only for a
future window. Option 4 stays available as the fallback if the operator judges
a neutralized cohort to be worth less than no cohort — that is a judgment call
about experiment quality, not an engineering one, which is why this document
stops here.

Recording `lockup_expiry` as unfed (§3, finding) should be filed as its own
Linear issue regardless of which option wins.

## 6. What is explicitly NOT decided here, and what must happen first

1. Any change to `PRE_REGISTRATION` bumps `spec_sha256()` and fails the pin
   test in `tests/services/buy_gate_ab_shadow/test_spec_freeze.py`. That is
   the intended alarm. A **written amendment** — new hypothesis text, the
   neutralization rule, the US-only-promotion prohibition — must be approved
   and dated **before** the pin is re-pinned, and rows collected under the old
   spec must keep their old `spec_sha256`.
2. Already-collected rows are not retro-fitted. Mixing pre- and
   post-amendment rows in one cohort without the flag is forbidden.
3. No scheduler, no Prefect, no TaskIQ registration in any option.
4. Nothing here authorizes a proposal, order, or watch. ROB-1301's three
   forbidden acts stand unchanged.

## 7. What was shipped alongside this document (ROB-1315 §5-1 ①)

The **input-key half** of §5-1 is fixed and does not wait on this decision:
`evaluate_buy_gate_ab_shadow` now rejects unknown candidate keys with the
correct key named (`rsi_14 -> rsi`, `nearest_support_strength ->
support_strength`) and echoes a machine-readable `input_contract` on both
success and failure. That was a silent-drop bug; this document is about a
missing data source. Fixing the first does not fix the second, and the US
`b_only=0` result stands until an option above is approved.
