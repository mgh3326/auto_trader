# SonarCloud Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce the open SonarCloud issues on the new-code period for `mgh3326_auto_trader` to a clean baseline by fixing the few real bugs, suppressing the verified false positives with justifications, hardening security findings, and removing noisy bulk findings — split across **3 phases / 3 PRs**.

**Tech Stack:** Python 3.13 (async / FastAPI), TypeScript/React (frontend/invest, frontend/trading-decision), pytest-asyncio, Ruff/ty, SonarCloud (project key `mgh3326_auto_trader`).

**Branch convention:** Each phase is its own branch off `main` and its own PR. All branches off `main`, base PR target = `main`.

---

## Context

SonarCloud's new-code-period scan currently reports for project `mgh3326_auto_trader`:

| Category | Count | Notes |
|---|---|---|
| New Bugs | 835 | ~815 are `python:S1244` (float `==` in tests). 7 are real bugs. |
| New Vulnerabilities | 24 | 1 CRITICAL (S5542 AES-CBC, false positive), 2 MAJOR (S2068 fake creds in tests, false positive), 21 MINOR (S5145 log injection). |
| New Security Hotspots | 104 | Mostly LOW (test http://, GitHub action SHA pinning). 2 HIGH (docker-compose POSTGRES_PASSWORD), 4 MEDIUM ReDoS, 3 MEDIUM Dockerfile permissions. |
| New Duplicated Lines Density | 3.96% (12,972 lines / 505 blocks) | Out of scope for this plan. |

The signal-to-noise ratio is low: < 1% of bug findings represent real defects, and the bulk false-positive volume is hiding the real ones. After this cleanup the new-code dashboard should be readable, and any genuine new finding stands out.

**Why the split:**
- Phase 1 contains the genuine production bugs and is small enough to merge fast.
- Phase 2 is a security-hardening sweep that can be reviewed by a different reviewer without blocking the bug fixes.
- Phase 3 is a mechanical bulk replace that's noisy in diff but trivial in risk; isolating it keeps Phase 1/2 reviews clean.

---

## Phase 1 — Real Bugs + False-Positive Suppression

**Branch:** `fix/sonarcloud-phase1-bugs`
**Estimated effort:** 1–2 hours
**PR title:** `fix(quality): resolve real bugs and suppress sonar false positives`

### 1.1 Fix `python:S7502` — async task GC risk (4 sites)

The pattern `asyncio.create_task(...)` / `asyncio.ensure_future(...)` without keeping a reference lets the loop GC the task, which can silently drop the work. Hold a strong reference using a module-level `set` plus `add_done_callback(set.discard)`.

**Sites to fix:**
- [ ] `app/services/research_pipeline_service.py:73` — `asyncio.create_task(_run_session_in_background(...))`
- [ ] `app/routers/n8n.py:382` — `asyncio.ensure_future(save_daily_brief_report(result))`
- [ ] `app/routers/n8n.py:646` — `asyncio.ensure_future(save_crypto_scan_report(result))`
- [ ] `app/routers/n8n.py:689` — `asyncio.ensure_future(save_kr_morning_report(result))`

**Recommended pattern (per file):**

```python
# Module-level (top of file, after imports)
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _spawn_background(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task
```

Then:
- `app/services/research_pipeline_service.py:73` →
  ```python
  _spawn_background(
      _run_session_in_background(
          session_id=session_id, symbol=symbol, name=name or symbol,
          instrument_type=instrument_type, research_run_id=research_run_id, user_id=user_id,
      ),
      name=f"research-session-{session_id}",
  )
  ```
- `app/routers/n8n.py:382` → `_spawn_background(save_daily_brief_report(result), name="n8n-daily-brief")`
- `app/routers/n8n.py:646` → `_spawn_background(save_crypto_scan_report(result), name="n8n-crypto-scan")`
- `app/routers/n8n.py:689` → `_spawn_background(save_kr_morning_report(result), name="n8n-kr-morning")`

**Alternative considered:** FastAPI `BackgroundTasks`. Rejected because (1) `research_pipeline_service.py` is a service layer not a route handler, and (2) `n8n.py` already returns `JSONResponse` directly and re-plumbing to BackgroundTasks would touch the response shape. The `set` + callback pattern is the standard asyncio idiom and avoids architectural change.

**Verification:** Run `uv run pytest tests/ -k "research_pipeline or n8n"` — existing tests should still pass. Manually confirm a no-op log line by hitting one n8n endpoint locally and checking the report row is persisted.

### 1.2 Fix `typescript:S3923` — dead branch in `HoldingRow.tsx:98`

Both branches of the ternary return `"주"` — the US branch should be `"shares"` (or `"sh"` to match other displays). Verify against existing US holdings UI (`frontend/trading-decision/`) for the correct label.

- [ ] Check how US shares are labeled elsewhere in the invest frontend (`grep -rn '"shares"\|"sh"\|주식' frontend/invest/src/`). Match the existing convention.
- [ ] Update `frontend/invest/src/components/HoldingRow.tsx:96-99`:
  ```tsx
  function unitFor(market: Market): string {
    if (market === "CRYPTO") return "";
    return market === "KR" ? "주" : "shares";  // or whatever the existing US convention is
  }
  ```
- [ ] If KR/US should actually share the same unit (e.g., always "주"), then drop the conditional entirely: `return market === "CRYPTO" ? "" : "주";` — the dead-branch warning goes away because the structure is honest.

**Verification:** `cd frontend/invest && pnpm build` passes; visual check the holding row for a US holding renders the correct unit.

### 1.3 Fix `typescript:S1082` — keyboard a11y in `OrderPreviewModal.tsx:96-97`

Modal overlay closes on click but has no keyboard handler. Add `Escape` to close.

- [ ] Update `frontend/trading-decision/src/components/OrderPreviewModal.tsx`:
  - Add a `useEffect` that registers `keydown` for `Escape` → calls `onClose()` while `isOpen`.
  - Remove the `onClick={onClose}` from the overlay `<div>` if Sonar still complains, and replace with a `<button>` styled as overlay; OR add `role="dialog"` / `aria-modal="true"` plus `tabIndex={-1}` and an `onKeyDown` for `Escape`.

Minimal patch:
```tsx
useEffect(() => {
  if (!isOpen) return;
  const handler = (e: KeyboardEvent) => {
    if (e.key === "Escape") onClose();
  };
  window.addEventListener("keydown", handler);
  return () => window.removeEventListener("keydown", handler);
}, [isOpen, onClose]);
```

The overlay `<div>` still receives clicks — for the keyboard listener rule itself, add `onKeyDown={(e) => e.key === 'Enter' && onClose()}` and `role="button"` `tabIndex={0}` to the overlay. Sonar wants any keyboard pairing with the click handler.

**Verification:** `cd frontend/trading-decision && pnpm build`. Open the modal, press `Esc`, confirm it closes. Tab into the overlay, press `Enter`, confirm it closes.

### 1.4 Suppress confirmed false positives

For each, add a one-line code comment that makes the suppression discoverable, AND mark the issue in SonarCloud as `Won't Fix` / `Safe` with the same justification.

- [ ] **`python:S5542`** — `app/services/kis_websocket_internal/parsers.py:198` (AES-CBC + PKCS7).
  - Add comment immediately above line 197:
    ```python
    # NOTE: KIS WebSocket protocol mandates AES/CBC + PKCS7. Cannot change cipher mode.
    # SonarCloud S5542: marked Safe (external protocol requirement). See PR <link>.
    ```
  - In SonarCloud UI: change resolution to `Won't Fix` with comment "External protocol (KIS WebSocket) requires AES/CBC/PKCS7."

- [ ] **`python:S2068`** — `tests/integration/test_strategy_event_db_roundtrip.py:40` (`hashed_password="fakehash"`).
  - Add `# noqa: S105` (Ruff equivalent) inline. SonarCloud honors comment-based justifications less reliably, so also mark `Safe` in UI.
  - In SonarCloud UI: `Won't Fix`, comment "Test fixture, not a real credential."

- [ ] **`python:S2068`** — `tests/test_mcp_sentry_middleware.py:96` (`"password": "mypass"`).
  - Same treatment. Add a one-line comment on the dict entry line: `# noqa: S105 — fixture asserts redaction, not a real secret`.
  - SonarCloud UI: `Won't Fix`, comment "Fixture used to verify redaction logic."

**Verification per Phase 1:**
- [ ] `make lint` passes.
- [ ] `make typecheck` passes.
- [ ] `make test-unit` passes.
- [ ] Frontend builds: `cd frontend/invest && pnpm build && cd ../trading-decision && pnpm build`.
- [ ] Push branch; SonarCloud PR scan reports 0 of these specific issues.

---

## Phase 2 — Security Hardening

**Branch:** `fix/sonarcloud-phase2-security`
**Estimated effort:** 3–4 hours
**PR title:** `fix(security): sanitize log inputs, harden docker config, audit regex`
**Depends on:** Phase 1 merged (no hard dependency, but cleaner diff if Phase 1 lands first).

### 2.1 Fix `pythonsecurity:S5145` — log injection (21 occurrences)

User-controlled data (mostly stock symbols) flowing into log format strings. Sonar's taint engine traces the path from HTTP request → variable → `logger.info`. Two fix strategies; **use Strategy A everywhere unless noted**.

**Strategy A (preferred): sanitize at the log call.** Add a single helper and use it on tainted args.

- [ ] Add `app/core/log_sanitize.py`:
  ```python
  import re

  _UNSAFE = re.compile(r"[^\w./\-:@]")
  _MAX_LEN = 64

  def safe_log_value(value: object) -> str:
      """Sanitize a potentially user-controlled value for logging.

      Strips control chars and non-symbol characters, caps length. Returns
      a string suitable for inclusion in log format args.
      """
      s = str(value)
      cleaned = _UNSAFE.sub("_", s)
      if len(cleaned) > _MAX_LEN:
          cleaned = cleaned[:_MAX_LEN] + "..."
      return cleaned
  ```
  Add `from app.core.log_sanitize import safe_log_value` in each file below.

**Sites (each line is a distinct flagged issue):**

- [ ] `app/services/ohlcv_cache_common.py` — lines 419, 431, 484, 495, 504, 516, 524, 577, 606, 616. (Sonar reports 8 issues but flags 10 lines as taint propagation; fix all 10.)
- [ ] `app/services/kis_ohlcv_cache.py:479`
- [ ] `app/routers/n8n.py:852`
- [ ] `app/services/brokers/yahoo/client.py:173`
- [ ] `app/services/market_data/service.py:206, 222`
- [ ] `app/services/upbit_orderbook.py:74, 86, 93`
- [ ] `app/services/sell_signal_service.py` — lines 78, 79, 83, 329, 330, 348 (taint propagation; sanitize the source `symbol` arg at the entry of each affected method).

For each: replace `logger.info("... symbol=%s ...", ..., symbol, ...)` with `logger.info("... symbol=%s ...", ..., safe_log_value(symbol), ...)`. Apply to **all tainted args** (typically `symbol`, `market`, `tr_id`, `payload`).

**Strategy B (fallback for any site where the sanitize call would obscure intent):** Validate at the boundary instead — e.g., if a router accepts `symbol`, validate against `^[A-Z0-9./-]{1,16}$` in the request schema; Sonar's taint then drops because the validator becomes a sink-cleaner. Document in a code comment.

**Verification:** `make test`. Re-scan and confirm S5145 count drops to 0 in new code.

### 2.2 Fix Docker / docker-compose hotspots

- [ ] **`yaml:S2068`** — `docker-compose.yml` lines 7-8 and `docker-compose.full.yml` lines 5-6 (`POSTGRES_PASSWORD: postgres`).
  - Replace literal with env-var reference:
    ```yaml
    POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-postgres}
    ```
  - Add `POSTGRES_PASSWORD=postgres` to `env.example` with a comment "local dev only".
  - In SonarCloud, mark the remaining hotspot (if Sonar still flags `${VAR:-default}`) as `Reviewed → Safe` with comment "Local-dev compose; production uses managed secrets."

- [ ] **`docker:S6470`** (recursive copy) and **`docker:S6471`** (root user) — `Dockerfile.api`, `Dockerfile.ws`, `Dockerfile.caddy`.
  - For each Dockerfile: add a non-root user near the top:
    ```dockerfile
    RUN groupadd --system app && useradd --system --gid app --create-home app
    ```
  - Replace broad `COPY . .` with selective copy if practical (`COPY app/ ./app/`, `COPY pyproject.toml uv.lock ./`, etc.). If the build truly needs the whole tree, add a `.dockerignore` that excludes `.git`, `tests/`, `frontend/*/node_modules`, `*.pyc`, etc., and mark Sonar hotspot `Reviewed → Safe` with the dockerignore link.
  - Final `USER app` directive before `CMD`.
  - **Critical:** Test the image builds and the container starts — this can break entrypoint scripts that need root.

### 2.3 Audit ReDoS regex hotspots

- [ ] **`python:S5852`** — `app/services/cio_quality_gate_service.py:439, 440, 602`.
  - Lines 439-440: `ORDER_TOTAL_RE` / `ACTUAL_CASH_RE` use `[\d.,]+` which is fine — the rule likely flags the surrounding `\s*[~≈]?\s*` quantifier interaction. Add anchors and bound: `[\d.,]{1,20}`. Mark as `Won't Fix` if Sonar still complains after bounding (the actual backtracking risk is minimal because the input is a markdown line, not arbitrary length).
  - Line 602: `r"(대비|우위|열위|기존.*DCA.*대비)"` — the `.*DCA.*` greedy alternation can backtrack. Replace with a non-greedy/atomic form: `r"(대비|우위|열위|기존[^\n]{0,40}DCA[^\n]{0,40}대비)"`.
- [ ] **`typescript:S5852`** — `frontend/.../NewsRiskHeadlineCard.tsx` (find via `grep -rn "S5852\|new RegExp" frontend/`).
  - Most likely a similar `.*` greediness issue. Replace `.*` with a bounded character class.

### 2.4 GitHub Actions hotspots (optional, low risk)

- [ ] `githubactions:S7637` / `S7636` — pin actions to commit SHAs instead of tags.
  - For each `.github/workflows/*.yml`: replace `uses: actions/checkout@v4` with `uses: actions/checkout@<SHA> # v4.x.x`.
  - Tooling: `gh api repos/actions/checkout/git/refs/tags/v4 --jq .object.sha` to look up SHAs.
  - This is a large diff — if reviewer prefers, mark all 40+ as `Reviewed → Acknowledged` with comment "GitHub-managed orgs; tag mutation risk accepted."
  - **Recommended:** mark as Acknowledged, do NOT pin. The org doesn't need this hardening level for an internal tool.

### 2.5 HTTP-not-HTTPS hotspots in tests/probes

- [ ] `python:S5332` — 56 occurrences. Almost all are localhost / mock URLs in test files. Bulk-mark as `Reviewed → Safe` in SonarCloud with comment "Localhost or mock URL in test/probe code."
- [ ] If any non-test file uses `http://`, fix it (likely none — verify with `grep -rn 'http://' app/`).

### 2.6 Misc low-prob hotspots

- [ ] `javascript:S2245` (Math.random in HTML) in `screener_dashboard.html` and `screener_report_detail.html`. If used for cache-busting / animation only: mark `Reviewed → Safe`. If used for any token/ID: replace with `crypto.randomUUID()`.
- [ ] `Web:S5725` (subresource integrity): if external CDN scripts lack SRI, add `integrity` attributes. Otherwise mark Safe.

**Verification per Phase 2:**
- [ ] `make lint && make test`.
- [ ] Docker images build: `docker compose build`.
- [ ] Container runs: `docker compose up -d && docker compose ps` shows healthy.
- [ ] SonarCloud PR scan: vulnerabilities = 0; security hotspots reduced by ≥50% in new code.

---

## Phase 3 — Test Float Comparison Bulk Replace

**Branch:** `chore/sonarcloud-phase3-pytest-approx`
**Estimated effort:** 4–6 hours (mostly mechanical)
**PR title:** `chore(tests): replace float == with pytest.approx for sonar S1244`

### Scope

Bulk-replace 815 `python:S1244` occurrences across 21 test files with `pytest.approx(...)` (or `math.isclose` where `approx` doesn't fit, e.g. dict comparisons).

**File breakdown (count):**
- `tests/test_paper_trading_service.py` (56)
- `tests/test_mcp_portfolio_tools.py` (28)
- `tests/test_paper_account_tools.py` (20)
- `tests/test_paper_portfolio_handler.py` (18)
- `tests/test_paper_analytics_tools.py` (17)
- `tests/test_paper_journal_bridge.py` (8)
- `tests/test_kis_account_fetch_stocks.py` (8)
- `tests/test_portfolio_overview_service.py` (6)
- `tests/test_order_estimation_service.py` (6)
- `tests/test_naver_finance.py` (5)
- `tests/test_kr_hourly_candles_read_service.py` (5)
- `tests/services/test_candidate_screening_service.py` (5)
- `tests/test_mcp_place_order.py` (4)
- `tests/test_sell_signal_service.py` (4)
- `tests/services/test_research_retrospective_service.py` (4)
- `tests/test_screening_common.py` (4)
- `tests/services/test_portfolio_action_service.py` (3)
- `tests/test_mcp_screen_stocks_kr.py` (2)
- `tests/services/kis_websocket/test_events.py` (2)
- `tests/test_mcp_quotes_tools.py` (1)
- `tests/test_watch_intent_policy.py` (1)

### Mechanical patch strategy

**Pattern A — scalar equality:**
```python
# Before
assert result.total == 100.5

# After
assert result.total == pytest.approx(100.5)
```

**Pattern B — dict/list of floats:**
```python
# Before
assert result == {"a": 1.5, "b": 2.5}

# After
assert result == pytest.approx({"a": 1.5, "b": 2.5})
```

**Pattern C — when value is exact int/float and approx feels wrong:**
- If the value comes from arithmetic that should be exact (counting, integer-derived), keep `==` and add `# noqa: S1244` per line. SonarCloud honors `# NOSONAR` comments — use `# NOSONAR S1244 — exact integer count` instead, which Sonar parses.

### Procedure (per file)

- [ ] Open the file. Add `import pytest` at top if not present.
- [ ] For each flagged line (look up via `https://sonarcloud.io/project/issues?files=<file>&rules=python:S1244`), apply Pattern A/B/C as appropriate.
- [ ] If a single test has many comparisons against floats, prefer wrapping the **whole expected dict** in `pytest.approx(...)` (Pattern B) over wrapping each value.
- [ ] Run that file in isolation: `uv run pytest <file> -v`. All tests must still pass.
- [ ] Commit per-file or per-group (e.g., one commit per `tests/test_paper_*.py`) for easier review.

### Automation aid (optional)

A regex-based `sed` is unsafe for Python (multiline expressions, dicts). Instead:
- Use `comby` or a small Python AST script to wrap RHS of `assert <expr> == <float-literal>` patterns.
- A reasonable starting comby template: `assert :[lhs] == :[rhs~[\d.]+]` → `assert :[lhs] == pytest.approx(:[rhs])`.
- **Always run the affected tests after each batch.** Some tests use `==` against `int` that Sonar mis-flagged — those should stay as `==`.

**Verification per Phase 3:**
- [ ] `make test` passes (zero regression).
- [ ] `make lint` passes (no unused `pytest` imports).
- [ ] SonarCloud PR scan: `python:S1244` count = 0 in new code.

---

## Out of Scope (separate future work)

- **Code smells (3,001 new):** Most are minor (cognitive complexity, naming). Address opportunistically when touching a file; not worth a sweep PR.
- **Duplicated lines density (3.96% / 12,972 lines):** Real refactor opportunity. Should be its own design doc — likely targets `app/services/kis.py` (30,000+ lines) and the screening package. Schedule after Phase 3.
- **Overall (non-new-code) bugs (1):** The single non-new-code bug; defer until next refactor in that file touches it naturally.

---

## Critical Files Reference

Real-bug targets (Phase 1):
- `app/services/research_pipeline_service.py:73`
- `app/routers/n8n.py:382, 646, 689`
- `frontend/invest/src/components/HoldingRow.tsx:96-99`
- `frontend/trading-decision/src/components/OrderPreviewModal.tsx:88-110`
- `app/services/kis_websocket_internal/parsers.py:197-200`
- `tests/integration/test_strategy_event_db_roundtrip.py:40`
- `tests/test_mcp_sentry_middleware.py:96`

Security hardening targets (Phase 2):
- `app/core/log_sanitize.py` (NEW)
- `app/services/ohlcv_cache_common.py` (8 fixes)
- `app/services/kis_ohlcv_cache.py:479`
- `app/services/upbit_orderbook.py:74, 86, 93`
- `app/services/market_data/service.py:206, 222`
- `app/services/sell_signal_service.py` (2 fixes)
- `app/services/brokers/yahoo/client.py:173`
- `app/routers/n8n.py:852`
- `docker-compose.yml`, `docker-compose.full.yml`
- `Dockerfile.api`, `Dockerfile.ws`, `Dockerfile.caddy`
- `app/services/cio_quality_gate_service.py:439, 440, 602`

Test cleanup targets (Phase 3): 21 files under `tests/` (listed in §3 Scope).

---

## Verification (overall)

After all three PRs merged to `main` (then to `production` per release flow):

- [ ] SonarCloud project page shows: New Bugs ≤ 5, New Vulnerabilities = 0, New Hotspots reduced by ≥80%.
- [ ] Quality Gate on `main` passes.
- [ ] No regression in `make test` on `main`.
- [ ] Production deploy succeeds (Phase 2 Docker changes are the highest-risk part — verify container health post-deploy).

---

## Reusable Utilities Introduced

- `app/core/log_sanitize.py::safe_log_value` — sanitize user-controlled values before logging.
- `_BACKGROUND_TASKS` set + `_spawn_background` helper pattern — reuse in `app/routers/n8n.py` and `app/services/research_pipeline_service.py`. Consider promoting to `app/core/async_utils.py` if a third use site appears.

---

## Notes for the executing session

- Each phase's PR description should link to this plan and the specific SonarCloud issue search URL filtered to the rules touched.
- Run `make lint && make typecheck && make test-unit` before pushing each phase.
- Phase 2 Docker changes need a manual smoke test — `docker compose up -d` and verify all services healthy. Don't rely solely on CI.
- For SonarCloud "Won't Fix" / "Safe" markings, do them in the SonarCloud web UI after the PR scan completes (so the comment links to the merged commit).
- If any single sub-task takes >2× the rough estimate, stop and ask — likely a hidden gotcha (especially for the regex audit and Dockerfile non-root user changes).
