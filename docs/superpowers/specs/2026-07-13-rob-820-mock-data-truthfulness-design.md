# ROB-820 Mock Data Truthfulness Design

## Goal

Restore the existing KIS MCP read plane so `account_mode="kis_mock"` cannot
consume live/manual account truth, provider failures always carry a stable
reason and evidence, stale or missing quote timestamps are never presented as
fresh, and empty fundamentals are explicitly unavailable.

## Scope boundaries

- Keep the existing KIS client and MCP portfolio/quote/fundamentals handlers as
  the only truth sources. Do not create a ROB-851 adapter or a second
  holdings/cash source.
- Do not change order mutation behavior owned by ROB-843/ROB-853.
- Do not implement ROB-819 or ROB-826 terminal-history/US-cancel work.
- Tests use deterministic fakes only; no credentials, real account data, or
  live provider mutation.

## Design

### Mock account isolation

The MCP portfolio server boundary will treat `is_mock=True` as a KIS-only
scope. Holdings will collect only KIS positions, and cash will collect only KIS
domestic cash plus the explicit KIS mock overseas unsupported diagnostic.
Upbit, Toss API, manual holdings, and manual cash will not be queried. An
explicit incompatible account (`upbit`, `toss`, `samsung_pension`, or `isa`) or
`market="crypto"` combined with `kis_mock` will raise a stable `ValueError`
instead of returning an apparently valid empty/mixed response. Nested KIS cash
rows and errors will carry `account_mode="kis_mock"`; holdings already stamp
that provenance through the routing metadata path.

### Error truthfulness

ROB-600 already routes KIS mock cash exceptions through
`describe_exception`, records `ReadTimeout`, omits a fabricated zero row, and
adds `summary.unavailable_sources`. The implementation remains unchanged and
gets regression coverage. The current `KISCircuitOpen` exception already has a
stable message; MCP holdings coverage will prove that the message plus
source/market evidence survives the catch boundary. No retry or mutation
behavior changes are in scope.

### Quote and tradability freshness

KR daily/live quote timestamps will be parsed only from a real date value or a
datetime index. Missing/invalid timestamps and Unix epoch zero normalize to
`None`, not 1970. Quote envelopes will add `price_freshness` (`fresh`, `stale`,
or `unavailable`), `price_usable`, and a stable reason when unusable while
retaining the observed price as diagnostic reference data. NXT overlays set a
fresh current timestamp. Stale or missing NXT master timestamps will expose
`nxt_tradable=None`, preserve the raw observation as
`nxt_tradable_observed`, and provide `nxt_tradable_reason`; order preflight
internals remain untouched.

### Fundamentals availability

`get_financials` will normalize all provider shapes (`metrics`, `reports`, or
`data`). A payload containing no non-empty metric/report data will add
`status="unavailable"`, `scoreable=false`,
`reason="financial_metrics_unavailable"`, and compact evidence describing the
provider, statement, frequency, and period count. Non-empty payloads receive
`status="available"` and `scoreable=true`; no synthetic values are produced.

## Verification

Issue-focused tests cover the account/provenance matrix, KR mock timeout,
circuit-open propagation, epoch/missing/stale quote timestamps, stale NXT
metadata, and empty/non-empty fundamentals. Then run portfolio, allocation,
quote, fundamentals, KIS circuit/read, and MCP broad regressions, followed by
Ruff lint/format, ty, and `git diff --check`.

