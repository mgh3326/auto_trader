# ROB-323 Follow-up — Naver Remote-Debug Data-Quality Audit (Operator CLI) — Design

**Status:** Approved design (brainstorming complete). Implementation plan to follow via writing-plans.
**Branch:** `rob-323-remote-debug-collector` (off `origin/main` after PR #971 merged).
**Relationship:** Follow-up to ROB-323. PR #971 shipped the core/external separation + a deterministic `data_quality_audit` block (computed from the fail-open *stub* statuses) embedded in `snapshot_report_diagnostics`. This work adds the **first real external cross-check** behind that contract — as an out-of-band operator tool, not a server-side collector.

---

## 1. Problem & goal

ROB-323 established that Toss/Naver/browser are **external cross-check / data-quality audit** sources, never report-generation sources. The 3 collectors are fail-open stubs (`optional_stubs.py`), and the real `127.0.0.1:9222` remote-debug path was explicitly deferred.

**Goal:** Deliver the first real cross-check — for a given report/bundle, compare each KR symbol's auto_trader quote against the logged-in **Naver** finance page (via the operator's Chrome at `127.0.0.1:9222`) and emit a structured gap/diff JSON. The point is finding **gaps** (does auto_trader miss / mis-resolve / carry stale data that Naver has), not exact price reconciliation.

**Explicitly NOT this work (future slices):** Toss, Upbit, browser_probe, US symbols; persistence of audit results; replacing the `ensure()` registry stubs; any scheduler/automation.

## 2. Key constraints (drive the whole design)

1. **`investment_reports` is append-only.** No `update_report()`; `snapshot_report_diagnostics` is set once at insert. A post-generation audit therefore **cannot write back** into the report row → output is **stdout JSON only** (no persistence in this slice).
2. **Chrome at `127.0.0.1:9222` only exists on the operator's macbook**, not on servers/CI. So the audit is an **operator-run CLI**, not a server-side collector. Running CDP inside `ensure()` would only ever fail-open on a server.
3. **Safety boundaries (CLAUDE.md + ROB-323 issue):** remote-debug is operator-smoke level; tests are fixture/fake; never scrape Toss/Naver from the frontend request path; no trading authority.
4. **Deps:** `httpx` and `websockets` are already in `main` deps → raw CDP, no `playwright` (dev-only) promotion, no browser binaries.

## 3. Run model (chosen)

Operator CLI, read-only, prints JSON to stdout, no DB write. Mirrors `scripts/kiwoom_mock_smoke.py` / `scripts/binance_spot_demo_smoke.py` (default-disabled, env-gated, JSON output, `preflight` mode).

## 4. Approach (chosen)

Raw CDP over `httpx` (discovery) + `websockets` (session). Minimal CDP surface: `Target.createTarget`, `Page.navigate`, `Runtime.evaluate`, `Target.closeTarget`. New tab per symbol, navigate, extract, close — never touches the operator's existing tabs.

## 5. Components

New package `app/services/action_report/remote_debug_audit/`:

| File | Responsibility | Depends on |
|---|---|---|
| `host_allowlist.py` | `CDP_DEBUG_HOSTS = {"127.0.0.1:9222"}`; `assert_cdp_debug_host(host_port)` raises `CdpDebugHostBlocked` on anything else (strict equality, no wildcard). | — |
| `cdp_client.py` | `CdpClient`: `discover()` (`httpx GET http://127.0.0.1:9222/json/version`), `open_tab(url)`, `navigate(load wait)`, `evaluate(expr)`, `close_tab(target_id)`. Host-locked at construction. Closes created tabs in `finally`. | `httpx`, `websockets`, `host_allowlist` |
| `sources/naver_quote.py` | `naver_url(code)`, `NAVER_EXTRACT_JS` (returns `{code,name,price}`), `parse_naver_quote(raw) -> NaverQuote \| None`. Pure parse, unit-testable. | — |
| `cross_check.py` | `cross_check_symbol(at_quote, naver_quote) -> SymbolFinding`; classifies `symbol_resolved`, `name_match`, `at_quote_present`, `at_quote_stale`, `price_within_tolerance`, and derives `gaps`. Tolerance band is a coverage/plausibility check, NOT exact equality. | `sources/naver_quote` |
| `service.py` | `RemoteDebugAuditService.audit_bundle(bundle_uuid, *, max_symbols)`: load bundle + `symbol` snapshots (read-only via `InvestmentSnapshotsRepository`), extract `payload['quote']` per symbol, drive per-symbol CDP cross-check (sequential, per-symbol timeout, fail-open), assemble audit dict. | repo, `cdp_client`, `cross_check`, `sources/naver_quote` |

CLI + config + docs:
- `scripts/remote_debug_audit_smoke.py` — `--mode {preflight,audit}`, `--bundle-uuid` / `--report-uuid`, `--max-symbols` (default e.g. 10). JSON via `print(json.dumps(..., ensure_ascii=False, default=str))`.
- `app/core/config.py` — `remote_debug_audit_enabled: bool = False`; `validate_remote_debug_audit_config()` → list of missing env KEY names (names only).
- `docs/runbooks/remote-debug-audit-smoke.md` — operator runbook incl. the Chrome launch (`open -na "Google Chrome" --args --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 --user-data-dir="$HOME/.hermes/chrome-toss-debug"`).

## 6. Data flow

```
operator runs CLI
  → env gate: REMOTE_DEBUG_AUDIT_ENABLED unset → fail-closed (print missing KEY name, exit 0 in preflight / non-zero in audit)
  → resolve --report-uuid → bundle_uuid (if report-uuid given), else use --bundle-uuid
  → load bundle + its `symbol` snapshots from DB (read-only)
  → extract auto_trader per-symbol quote from snapshot payload['quote']
  → CdpClient.discover() against 127.0.0.1:9222   [fails here → exit 2, "no Chrome remote-debug"]
  → for each symbol (≤ max-symbols, sequential):
        open tab → navigate Naver item page → evaluate price/name → close tab
        build NaverQuote (fail-open: nav/parse error → finding.status="unavailable"/"parse_failed")
        cross_check(at_quote, naver_quote) → SymbolFinding
  → assemble audit dict → print JSON → exit 0
```

## 7. Output shape (stdout JSON)

Reuses the ROB-323 `external_cross_checks` vocabulary so readers already know it. Example:

```json
{
  "source": "naver_remote_debug",
  "snapshot_bundle_uuid": "…",
  "report_uuid": "…",
  "as_of": "2026-05-26T10:00:00Z",
  "affects_report_generation": false,
  "checked_symbols": 8,
  "findings": [
    {
      "symbol": "005930",
      "symbol_resolved": true,
      "name_match": true,
      "at_quote_present": true,
      "at_quote_stale": false,
      "naver_price": 81000,
      "at_price": 80950,
      "price_within_tolerance": true,
      "status": "ok"
    },
    {
      "symbol": "999999",
      "symbol_resolved": false,
      "status": "unavailable",
      "reason_code": "naver_symbol_unresolved"
    }
  ],
  "gaps": [
    {
      "severity": "warning",
      "kind": "naver_price_mismatch",
      "sources": ["000660"],
      "message": "Naver와 auto_trader 가격 차이가 허용범위 초과 — 후속 데이터 점검 검토"
    }
  ]
}
```

`gaps` severities: `info` (e.g. probe-unavailable), `warning` (mismatch / unresolved). **Never `blocking`** — this audit never gates report generation/publish.

## 8. Safety gating (hard boundaries)

- **Default-disabled:** `REMOTE_DEBUG_AUDIT_ENABLED=true` required; otherwise fail-closed. `preflight` reports missing env KEY *names* only — never values.
- **Host-locked:** only `127.0.0.1:9222` (strict equality). Any other endpoint → `CdpDebugHostBlocked` before any connection.
- **Read-only:** zero DB writes (append-only respected). No broker / order / watch / order-intent. Not a trading authority.
- **CLI-only scraping:** Naver access lives only in this operator CLI — never in the frontend request path or server-side `ensure()`. The registry stubs stay unchanged.
- **Bounded:** `--max-symbols` cap, per-symbol timeout, sequential open/close, created tabs always closed in `finally`. Never closes/modifies pre-existing operator tabs.
- **No secrets printed.**

## 9. Error handling

| Condition | Behaviour |
|---|---|
| `REMOTE_DEBUG_AUDIT_ENABLED` unset | fail-closed; `preflight` exit 0 with `missing_env_keys`; `audit` exit 2 |
| No Chrome at `127.0.0.1:9222` (discovery fails) | **fail-closed**, clear message, **exit 2** — never fake a clean audit |
| Bundle / symbols not found | exit 2 with reason |
| Per-symbol navigate/evaluate/parse error | **fail-open**: record `status="unavailable"`/`"parse_failed"` finding + reason_code, continue |
| Completed audit (any number of gaps) | exit 0 |

## 10. Testing (fixture/fake only — no live Chrome in CI)

- `FakeCdpClient` returns canned per-URL eval payloads (incl. unresolved-symbol and parse-failure cases).
- Unit tests:
  - `assert_cdp_debug_host` rejects `localhost:9222`, `127.0.0.1:9223`, remote hosts; accepts `127.0.0.1:9222`.
  - `parse_naver_quote` on fixture payloads (valid, missing price, unresolved).
  - `cross_check_symbol`: name match, price within/over tolerance, stale auto_trader quote, missing auto_trader quote, unresolved Naver symbol → correct findings + gap classification.
  - audit-dict shape (keys, `affects_report_generation:false`, gap severities ≠ `blocking`).
  - CLI `preflight` fail-closed lists missing KEY name only (no values), with env unset.
  - audit orchestration with `FakeCdpClient` + an in-memory/fixture bundle → expected findings; per-symbol fail-open does not abort the run.
- **No test connects to a real browser or network.**

## 11. Scope / YAGNI

- KR symbols only (Naver = KRX). US / Toss / Upbit / browser_probe = future slices.
- stdout only — no persistence. A future increment may add a separate insert-only `report_data_quality_audit` table (respecting append-only) if the operator wants results surfaced in the UI.
- No scheduler/automation. No registry-stub replacement.

## 12. Open follow-ups (out of scope here)

- Persisted audit table + UI surfacing of real cross-check results.
- Additional sources (Toss positions reference-check, Upbit/TradingView via browser_probe) and US market.
- Candidate-universe / screener-gap cross-check dimension.
