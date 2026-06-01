# KR staged `/invest/reports` workflow

This contract defines the Korean equity report sequence that should be supported
by `/invest/reports` and private operator prompts. The goal is to preserve a
stage-by-stage decision lineage from early context to live-session confirmation,
without turning the application into an in-process LLM runner.

## Scope

Initial stages:

| Stage | Suggested report key | `market_session` | Purpose | Side effects |
|---|---|---:|---|---|
| Pre-market plan | `kr-pre-report` | `pre` | Build a read-only plan before NXT/regular liquidity confirms candidates. | None |
| NXT open check | `kr-nxt-open-report` | `nxt` | Confirm, downgrade, or reject the pre-market plan using NXT orderbook/trade context when available. | None |
| Regular open report | `kr-regular-open-report` | `regular` | Convert the carried plan into a regular-session advisory report after KRX liquidity opens. | None |
| Intraday delta, later | `kr-intraday-report` | `regular` | Update existing candidates from new evidence and explicitly record changed rationale. | None |

Do not introduce a new `preopen` `market_session` value. Use the existing
vocabulary: `pre`, `nxt`, `regular`, `post`, and `24x7` where applicable.

## Canonical generation flow

The official stored report path is bundle-backed and Hermes-composed:

```text
Phase 1: prepare bundle in auto_trader
  investment_report_prepare_bundle

Phase 2: expose frozen context
  investment_report_get_hermes_context

Phase 3: compose outside auto_trader
  Hermes reads the frozen context and writes structured stage artifacts and a
  final composition.

Phase 4: ingest in auto_trader
  investment_stage_artifacts_ingest_from_hermes
  investment_report_create_from_hermes_composition
```

`auto_trader` should provide deterministic evidence and persist the result. It
must not import an LLM provider or compose the investment narrative in-process.

## Stage responsibilities

### 1. Pre-market plan (`market_session=pre`)

Purpose:

- summarize market context, overnight/news themes, stale-data warnings, and
  holdings/cash availability at an abstract advisory level;
- seed buy/sell/risk candidates that need live-session confirmation;
- define the exact evidence that would confirm or invalidate each candidate;
- avoid previewing or placing orders.

Expected output:

- market context summary;
- portfolio/journal/watch context summary;
- candidate universe seed list;
- per-candidate `confirmation_needed[]`, `invalidation_triggers[]`, and
  `missing_data[]`;
- explicit `advisory_only` constraints.

### 2. NXT open check (`market_session=nxt`)

Purpose:

- compare pre-market candidates against NXT orderbook/trade evidence when the
  bundle contains it;
- carry forward only candidates with live-session support;
- mark candidates as downgraded/rejected/deferred rather than silently dropping
  them;
- keep the result advisory-only.

Expected output:

- NXT liquidity/price-action summary;
- transition records for each pre-market candidate;
- newly observed candidates, if any, marked as `new_session_candidate` and
  clearly separated from pre-market carried candidates;
- missing-data notes when NXT evidence is absent or stale.

### 3. Regular open report (`market_session=regular`)

Purpose:

- re-check carried pre/NXT candidates after regular-market liquidity opens;
- preserve why a candidate changed status;
- rank advisory buy/sell/risk candidates with source citations and uncertainty;
- state what remains unverified.

Expected output:

- market/session summary;
- holdings/account-scope summary without leaking private balances into public
  docs or templates;
- candidate tables grouped by buy review, sell review, risk watch, and deferred
  no-action;
- per-item citations to frozen snapshots and stage artifacts;
- final Korean summary suitable for `/invest/reports` display.

### 4. Intraday delta, later (`market_session=regular`)

Purpose:

- update existing candidates from changed evidence, not regenerate an unrelated
  list;
- make additions rare and explain why they were not visible earlier;
- record deltas against the most recent stored report or stage artifact.

This is a follow-up extension. The initial contract should leave room for it but
not require intraday scheduling or automation.

## Evidence hierarchy

Use this hierarchy when evidence conflicts:

1. `auto_trader`/KIS/account-owned data and frozen bundle snapshots.
2. Durable product read models such as screener, market events, journals,
   watch-context, and news-ingestor data.
3. Read-only live diagnostics that are frozen into the bundle at report time.
4. Toss/Naver/browser/community observations as supplementary or low-trust
   attention signals only.

Toss, Naver, and browser observations must not become final authority for
account truth, order state, or buy/sell conviction. If they repeatedly matter,
create a collector/read-model follow-up rather than depending on operator-local
scraping.

## Required safety constraints

Every stage must preserve these constraints:

- advisory-only unless a separate, explicit operator-approved execution workflow
  consumes the result later;
- no broker/order/watch/order-intent mutation;
- no scheduler or recurring automation activation;
- no production DB backfill/migration as part of report composition;
- no private credential, token, cookie, browser profile, or generated private
  account report committed to the public repository;
- stale, unavailable, and conflicting sources are reported explicitly instead of
  guessed.

## Stage artifact guidance

A Hermes stage artifact should include, at minimum:

- `stage_type` from the existing stage catalog where possible;
- `verdict`, `confidence`, `summary`, and `key_points`;
- buy/sell/risk evidence groups;
- `missing_data`, `freshness_summary`, and `source_conflicts`;
- cited snapshot UUIDs or stable citation paths;
- model/prompt metadata such as `model_name` and `prompt_version`.

Recommended stage catalog for the KR flow:

- `market`
- `news`
- `portfolio_journal`
- `watch_context`
- `candidate_universe`
- `bull_reducer`
- `bear_reducer`
- `risk_review`

Add new stage types only after the source snapshots or collectors exist.
