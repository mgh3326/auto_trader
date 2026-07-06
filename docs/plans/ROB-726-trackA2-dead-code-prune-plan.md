# ROB-726 — Track A round-2 dead-code prune Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete ~2,700 lines of confirmed-dead code (whole-file + function-level) across 3 independent PRs, without touching any live path, migration, or ORM.

**Architecture:** Pure deletion. There is no new behavior and no test-first cycle — the "test" for a deletion is that the existing suite stays green and `import app.main` still succeeds. Each PR is self-contained and independently reviewable/mergeable; they touch disjoint files so ordering between PRs does not matter.

**Tech Stack:** Python 3.13, pytest (`-m "not live" -n 4`), ruff, `uv`.

## Global Constraints

- **migration 0** — no alembic revisions. ORM models and tables are NOT dropped (deferred to a separate issue).
- **No broker/order mutation** — none of these deletions touch a live order/broker path.
- **Dead code is deleted, not `# noqa`/omit-expanded.** No coverage %-number target.
- **Re-grep before every deletion.** For function-level prune (PR-B/C), the deletion is only valid if the symbol has 0 callers in `app scripts tests` (excluding its own file and its dedicated dead test). Evidence below was captured 2026-07-06 against working tree at `fb06a9dc`; re-run each grep at execution time before deleting.
- **Verification gate (every PR, before commit):**
  - `uv run pytest tests/ -m "not live" -n 4` → green
  - `python -c "import app.main"` → no error
  - `make lint` → clean (ruff + ty). Run `uv run ruff format .` if format drifts.
  - `git grep -n "<deleted symbol/module>"` → only forbidden-prefix string literals or comments remain (no live refs).

---

## File Structure

Three disjoint deletion sets:

- **PR-A** — whole-file deletes + `__init__` re-export edits + companion-test/safety-list edits.
- **PR-B** — `app/services/stock_info_service.py` surgical function prune + its dead test.
- **PR-C** — `market_context_service.py` + `stock_alias_service.py` surgical function prune (0 test churn).

---

### Task A: PR-A — mechanical whole-file deletes (~2,100L incl. tests)

Branch off latest `origin/main`: `git switch -c rob-726-pr-a origin/main`.

**Files:**
- Delete: `app/mcp_server/tooling/trade_profile_tools.py` (839L)
- Delete: `app/mcp_server/tooling/trade_profile_registration.py` (88L)
- Delete: `app/services/orders/service.py` (387L)
- Delete: `app/services/account/service.py` (267L)
- Delete: `app/services/n8n_pending_review_service.py` (106L)
- Delete: `app/services/n8n_kr_morning_report_service.py` (438L)
- Delete: `tests/test_mcp_trade_profile_tools.py` (744L) — impl-only test for trade_profile
- Delete: `tests/test_services_account_service.py` — imports `app.services.account.service` (**gap fixed vs issue**)
- Delete: `tests/test_n8n_kr_morning_report.py` — imports `n8n_kr_morning_report_service` (**gap fixed vs issue**)
- Modify: `app/services/orders/__init__.py:2` — remove the `service` re-export
- Modify: `app/services/account/__init__.py:2` — remove the `service` re-export
- Modify: `tests/services/pure_service_safety.py:29` — remove `n8n_pending_review_service` list entry (**gap fixed vs issue**)
- Modify: `tests/services/research_run_safety_helpers.py:31,51` — remove `n8n_pending_review_service` list entries (**gap fixed vs issue**)

**Keep (do NOT delete):** `app/models/trade_profile.py` + migrations `4d9f0b2c7a11`/`a1b2c3d4e5f6` + `tests/models/test_trade_profile.py`; `orders/contracts.py`, `account/contracts.py`, `orders/ladder_fill_safety.py`. `pending_orders_service.py` (live via `jobs/intraday_order_review.py`).

**Grounding evidence (2026-07-06):**
- `register_trade_profile_tools`/`trade_profile_registration` referenced only by `tests/test_mcp_trade_profile_tools.py` (deleted) + a bare comment in `tests/test_alpaca_paper_isolation.py:63` (harmless).
- `from app.services.orders import …` / `from app.services.account import …` → 0 live consumers. `orders/service.py` & `account/service.py` reached only via their own `__init__` re-export (which nobody imports) + `tests/test_services_account_service.py`.
- `n8n_pending_review_service` live importers: 0 (only 3 safety-list entries above). `n8n_kr_morning_report_service` live importers: 0 (only `tests/test_n8n_kr_morning_report.py`).

- [ ] **Step 1: Re-grep to reconfirm dead (abort if any live consumer appears)**

```bash
git grep -n "register_trade_profile_tools\|trade_profile_registration\|trade_profile_tools" -- app scripts | grep -v "tooling/trade_profile_tools.py\|tooling/trade_profile_registration.py"
git grep -n "from app.services.orders import\|from app.services.account import" -- app scripts
git grep -n "n8n_pending_review_service\|n8n_kr_morning_report_service" -- app scripts jobs
```
Expected: no rows from any command (all consumers are tests being deleted or safety-lists being edited).

- [ ] **Step 2: Delete the 6 source files + 3 test files**

```bash
git rm app/mcp_server/tooling/trade_profile_tools.py \
       app/mcp_server/tooling/trade_profile_registration.py \
       app/services/orders/service.py \
       app/services/account/service.py \
       app/services/n8n_pending_review_service.py \
       app/services/n8n_kr_morning_report_service.py \
       tests/test_mcp_trade_profile_tools.py \
       tests/test_services_account_service.py \
       tests/test_n8n_kr_morning_report.py
```

- [ ] **Step 3: Edit `orders/__init__.py` and `account/__init__.py`**

`app/services/orders/__init__.py` — delete line 2 (`from app.services.orders.service import cancel_order, modify_order, place_order`) and drop `cancel_order`/`modify_order`/`place_order` from `__all__` if present. Keep line 1 (`OrderResult` from `contracts`).

`app/services/account/__init__.py` — delete line 2 (`from app.services.account.service import get_cash, get_margin, get_positions`) and drop those names from `__all__` if present. Keep line 1 (`CashBalance, MarginSnapshot, Position` from `contracts`).

- [ ] **Step 4: Edit the two safety-list files**

Remove the `"app.services.n8n_pending_review_service",` entry from:
- `tests/services/pure_service_safety.py` (line ~29)
- `tests/services/research_run_safety_helpers.py` (both `RESEARCH_RUN_FORBIDDEN_PREFIXES` ~31 and `NEWS_BRIEF_FORBIDDEN_PREFIXES` ~51)

- [ ] **Step 5: Import + dangling-ref check**

```bash
python -c "import app.main"
git grep -n "n8n_pending_review_service\|n8n_kr_morning_report_service\|orders\.service\|account\.service" -- app tests | grep -v "invest_home_readers.py"
```
Expected: `import app.main` OK. Second grep: only the benign comment in `invest_home_readers.py:261` remains (filtered) — no import/call rows.

- [ ] **Step 6: Run suite + lint**

```bash
uv run pytest tests/ -m "not live" -n 4
make lint
```
Expected: green + clean. (`tests/test_mcp_tool_registration.py::test_rob488_*` passes unchanged — it uses `isdisjoint` on a retired-set, so absent names still pass.)

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(dead-code): drop trade_profile MCP, orders/account facades, dead n8n services (ROB-726 PR-A)"
```

---

### Task B: PR-B — `stock_info_service.py` surgical function prune (~665L)

Branch off latest `origin/main`: `git switch -c rob-726-pr-b origin/main`.

**Files:**
- Modify: `app/services/stock_info_service.py` (848L → ~185L)
- Delete: `tests/test_services_stock_info.py` (guard test for the deleted 1%-rule buy path)

**Delete these symbols (all 0 callers, confirmed 2026-07-06):**

| Location | Symbols |
| -- | -- |
| Module-level Upbit auto-buy block **L330–848 (contiguous, delete to EOF)** | `get_coin_sell_price`, `get_coin_sell_price_range`, `get_coin_buy_price_ranges`, `check_buy_condition_with_analysis`, `process_buy_orders_with_analysis`, `_place_multiple_buy_orders_by_analysis`, `_place_single_buy_order`, `_place_single_buy_order_by_quantity` |
| `StockAnalysisService` methods **L222–277 (contiguous)** | `get_sell_price_range_by_symbol`, `get_sell_price_by_symbol` |
| `StockInfoService` dead CRUD **(scattered — surgical)** | `get_all_active_stocks` (L38–44), `get_stocks_by_type` (L45–54), `deactivate_stock` (L65–74), `activate_stock` (L75–84), `delete_stock_info` (L85–90), `search_stocks` (L91–105), `get_stock_count_by_type` (L106–114), `bulk_create_stocks` (L115–134) |

**Keep (live, confirmed callers):** module `create_stock_if_not_exists` (7); `StockInfoService.__init__`/`create_stock_info`/`get_stock_info_by_symbol` (3)/`get_stock_info_by_id`/`update_stock_info` (L55–64, interleaved between the deleted CRUD — do NOT remove); `StockAnalysisService.__init__`/`get_latest_analysis_by_symbol` (7)/`get_latest_analysis_results_for_coins` (1, L278–329).

> **Caution:** The `StockInfoService` dead CRUD interleaves with live `update_stock_info` (L55–64). Delete method-by-method, not by a single line range. After each removal re-check imports at top of file — remove any import (e.g. `sqlalchemy` helpers, `datetime`) that becomes unused; ruff `F401` will flag them in Step 3.

- [ ] **Step 1: Re-grep each symbol to reconfirm 0 callers**

```bash
for s in get_coin_sell_price get_coin_sell_price_range get_coin_buy_price_ranges \
  check_buy_condition_with_analysis process_buy_orders_with_analysis \
  _place_multiple_buy_orders_by_analysis get_sell_price_range_by_symbol \
  get_sell_price_by_symbol get_all_active_stocks get_stocks_by_type \
  deactivate_stock activate_stock delete_stock_info search_stocks \
  get_stock_count_by_type bulk_create_stocks; do
  echo -n "$s: "; git grep -n "\b$s\b" -- app scripts tests \
    | grep -v "services/stock_info_service.py\|tests/test_services_stock_info.py" | wc -l
done
```
Expected: every symbol `0`. Abort on any non-zero (dynamic/string/scripts caller appeared).

- [ ] **Step 2: Delete the module-level Upbit block (L330–EOF) and the dead test**

Delete everything from `async def get_coin_sell_price(` (L330) through end of file. Then:
```bash
git rm tests/test_services_stock_info.py
```

- [ ] **Step 3: Delete `StockAnalysisService.get_sell_price_range_by_symbol` + `get_sell_price_by_symbol` (L222–277) and the 8 scattered `StockInfoService` CRUD methods**

Remove each method listed above by its `def`…`return`/end-of-body span, leaving `get_stock_info_by_id`, `update_stock_info`, `get_latest_analysis_by_symbol`, `get_latest_analysis_results_for_coins`, `create_stock_if_not_exists` intact.

- [ ] **Step 4: Import + suite + lint**

```bash
python -c "import app.services.stock_info_service; import app.main"
uv run pytest tests/ -m "not live" -n 4
make lint
```
Expected: import OK; suite green; ruff clean (fix any `F401` unused-import left by the prune).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(dead-code): prune stock_info_service upbit auto-buy + dead CRUD (ROB-726 PR-B)"
```

---

### Task C: PR-C — `market_context_service.py` + `stock_alias_service.py` prune (~340L)

Branch off latest `origin/main`: `git switch -c rob-726-pr-c origin/main`.

**Files:**
- Modify: `app/services/market_context_service.py` (341L → ~165L)
- Modify: `app/services/stock_alias_service.py` (277L → ~150L)
- (Test churn: 0)

**`market_context_service.py` — delete (0 callers):** `fetch_market_context` (L166–341, to EOF), `_fetch_crypto_market_overview`, `_fmt_volume_krw`. Bonus decouple: remove the now-unused top-of-file imports `from app.schemas.n8n.common import (...)` (L18) and `from app.schemas.n8n.market_context import (...)` (L23).
**Keep:** `_normalize_crypto_symbol` (10), `_compute_symbol_indicators` (2), `_classify_trend` (2), `_classify_strength` (internal, called by `_compute_symbol_indicators`) — all reached via `pending_orders_service`.

**`stock_alias_service.py` — delete (0 callers):** `create_alias`, `get_alias_by_id`, `search_by_alias`, `_sanitize_query`, `get_aliases_by_ticker`, `bulk_create_aliases`, `delete_alias`, `resolve_ticker`, `seed_toss_aliases`.
**Keep:** `get_ticker_by_alias` (1 caller — `screenshot_holdings`), `_get_default_ticker_by_alias` (internal), `TOSS_STOCK_ALIASES` (internal).

- [ ] **Step 1: Re-grep each symbol to reconfirm 0 callers**

```bash
for s in fetch_market_context _fetch_crypto_market_overview _fmt_volume_krw \
  create_alias get_alias_by_id search_by_alias _sanitize_query \
  get_aliases_by_ticker bulk_create_aliases delete_alias resolve_ticker seed_toss_aliases; do
  echo -n "$s: "; git grep -n "\b$s\b" -- app scripts tests \
    | grep -v "services/market_context_service.py\|services/stock_alias_service.py" | wc -l
done
```
Expected: every symbol `0`.

- [ ] **Step 2: Prune `market_context_service.py`**

Delete `fetch_market_context`/`_fetch_crypto_market_overview`/`_fmt_volume_krw` (from L166 to EOF is the contiguous dead tail — verify at execution) and remove the two `app.schemas.n8n.*` imports (L18, L23).

- [ ] **Step 3: Prune `stock_alias_service.py`**

Delete the 9 dead alias-CRUD symbols, leaving `get_ticker_by_alias` + `_get_default_ticker_by_alias` + `TOSS_STOCK_ALIASES`.

- [ ] **Step 4: Import + suite + lint**

```bash
python -c "import app.services.market_context_service; import app.services.stock_alias_service; import app.main"
uv run pytest tests/ -m "not live" -n 4
make lint
```
Expected: import OK; suite green; ruff clean (remove any now-unused imports).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(dead-code): prune market_context + stock_alias dead functions (ROB-726 PR-C)"
```

---

## DROP / Deferred (do not touch)

- `pending_orders_service.py` — `fetch_pending_orders` is live via `jobs/intraday_order_review.py`. 0 function-level dead. Skip.
- `trade_profile` **ORM + table drop** — non-zero migration, high risk. PR-A deletes impl only; ORM/table stay. Separate follow-up.

## Self-Review notes

- **Spec coverage:** PR-A/B/C map 1:1 to the issue's three PRs; DROP/Deferred preserved. The three companion-test/safety-list edits (account test, n8n morning-report test, `pending_review` safety-lists) are the corrections the issue's "동반 테스트 확인" left implicit — grounded above.
- **Migration 0 / no ORM drop / no broker mutation:** honored in all three tasks.
- **No placeholders:** every deletion names exact files, symbols, and line spans; every verify step has an exact command + expected result.
