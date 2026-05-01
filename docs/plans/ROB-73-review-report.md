# ROB-73 — Opus Code Review Report

Reviewer: Claude Opus
Branch: `feature/ROB-73-alpaca-paper-submit-cancel-dev-smoke`
Worktree: `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-73-alpaca-paper-submit-cancel-dev-smoke`
Base: `origin/main`
Initial review commit: `d08dafed`
Final review HEAD: `ad5c6186`
Production baseline reference: `12a80b86aa2d639f30985e8a8e91252deabf61ca`

## 0. Final outcome

**PASS.** All three must-fix documentation issues from the initial review (commit `d08dafed`) have been resolved on `ad5c6186` (`docs(ROB-73): align Alpaca paper safety docs after review`). All ten hard safety constraints remain enforced. 71 tests pass; ruff lint and format are clean. No further must-fix items.

## 1. Scope Reviewed

Diff against `origin/main`: 13 files, 3,215 insertions, 30 deletions. Reviewed every changed file plus adjacent safety surfaces:

- New module `app/mcp_server/tooling/alpaca_paper_orders.py` (242 lines).
- Updated `app/mcp_server/tooling/alpaca_paper_preview.py` (docstring + MCP description alignment).
- Registry change `app/mcp_server/tooling/registry.py` (+4 lines).
- New dev smoke `scripts/smoke/alpaca_paper_dev_smoke.py` (215 lines).
- New tests `tests/test_alpaca_paper_orders_tools.py` (520 lines) and `tests/test_alpaca_paper_dev_smoke_safety.py` (203 lines).
- Updated guard tests `tests/test_mcp_alpaca_paper_tools.py` and `tests/test_alpaca_paper_isolation.py`.
- New runbook `docs/runbooks/alpaca-paper-dev-smoke.md`.
- Updated `app/mcp_server/README.md` (safety boundary paragraph) and `docs/runbooks/alpaca-paper-readonly-smoke.md` (post-ROB-73 forbidden-subset rewrites).
- Plan `docs/plans/ROB-73-alpaca-paper-submit-cancel-dev-smoke.md`.

## 2. Required-safety Verification

| Constraint (from handoff prompt) | Verdict | Evidence |
|---|---|---|
| Explicit paper-only tool names `alpaca_paper_submit_order`, `alpaca_paper_cancel_order` | PASS | `app/mcp_server/tooling/alpaca_paper_orders.py:30-33`, `211-230`. Registered in both `DEFAULT` and `HERMES_PAPER_KIS` profiles via `registry.py:99` (always-on block). |
| No live Alpaca endpoint use; live endpoint fail-closed; no runtime live switch | PASS | `_default_service_factory` returns `AlpacaPaperBrokerService()`; constructor enforces `base_url == PAPER_TRADING_BASE_URL` and rejects `LIVE_TRADING_BASE_URL` via `FORBIDDEN_TRADING_BASE_URLS`. Tool signatures contain no `endpoint`/`base_url`/`live`/`url`/`host`/`env` params (`tests/test_alpaca_paper_orders_tools.py:425-432`). Fail-closed asserted by `test_submit_fails_closed_on_live_endpoint` and `test_cancel_fails_closed_on_live_endpoint`. |
| No secret output / Authorization logging | PASS | New module imports nothing from `app.core.config`; never reads `settings.alpaca_paper_*`. `_model_to_jsonable` operates only on Pydantic order/cash/account models which contain no auth fields. Smoke script bans any secret strings (`tests/test_alpaca_paper_dev_smoke_safety.py:42-46`) and bans direct `print(payload|result|order|...)` (`tests/test_alpaca_paper_dev_smoke_safety.py:49-74`). |
| No generic `place_order`/`cancel_order`/`modify_order` integration | PASS | `app/mcp_server/tooling/orders_registration.py` contains zero references to `alpaca`. `tests/test_alpaca_paper_orders_tools.py:454-466` asserts forbidden tool names. Smoke script also banned from referencing `place_order`/`modify_order`/`replace_order`/`cancel_all`/`cancel_by_symbol` (`tests/test_alpaca_paper_dev_smoke_safety.py:78-88`). |
| Submit no-ops unless explicit confirmation | PASS | `alpaca_paper_orders.py:137-146`: `confirm is not True` returns `submitted: False, blocked_reason: confirmation_required` and skips the broker call. Verified by `test_submit_without_confirm_is_blocked_no_op`. |
| Cancel no-ops unless explicit confirmation and exact `order_id`; no bulk cancel | PASS | `alpaca_paper_orders.py:170-208` rejects blank `order_id`, returns `cancelled: False` unless `confirm is True`, and signature is exactly `(order_id, confirm)` (no `status=`/`symbol=`/`all=`). Verified by `test_cancel_signature_has_no_bulk_or_filter_params`. |
| Automated tests mock side effects | PASS | All 71 tests across the four covered files pass and use `FakeOrdersService`/`FakeAlpacaPaperService` via `set_alpaca_paper_orders_service_factory(...)`. No real broker call is reachable from tests. |
| Dev smoke defaults to preview/no side effects; dual-gate required for actual mutation | PASS | `scripts/smoke/alpaca_paper_dev_smoke.py:176-194` rejects either gate alone (rc=2, no broker call); only when both `--confirm-paper-side-effect` AND `ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS=1` are set does `_side_effect_smoke()` run. Each gate tested in isolation (`test_dev_smoke_flag_without_env_is_blocked`, `test_dev_smoke_env_without_flag_is_blocked`). |
| No `paper_001`/`paper_us_001`, DB registry, or strategy-profile mapping | PASS | Repo grep for `paper_001`/`paper_us_001` returns hits only in plans/runbooks (rule references), not in any new code. No DB migration, no profile/registry table. |
| Pre-existing safety-claim docs aligned with new tool surface | PASS (resolved on `ad5c6186`) | See §3 below. |

## 3. Resolution of prior must-fix items

### MUST-FIX 1 — Stale MCP tool description on `alpaca_paper_preview_order` — RESOLVED

- **File:** `app/mcp_server/tooling/alpaca_paper_preview.py`
- **Resolution:** Module docstring (`1-7`), function docstring (`200-208`), and MCP `description=` string (`280-291`) all updated. Each location now explicitly states: (a) preview is a pure validator + echo, (b) submission goes through `alpaca_paper_submit_order` (paper-only, confirm-gated), (c) cancellation goes through `alpaca_paper_cancel_order`, (d) Alpaca paper is never routed through `place_order` / `replace_order` / `modify_order` / `cancel_order` / bulk-cancel surfaces. The MCP-client-visible description and the in-source documentation now match the live tool inventory.

### MUST-FIX 2 — Stale safety boundary in `app/mcp_server/README.md` — RESOLVED

- **File:** `app/mcp_server/README.md`
- **Resolution:** Lines `140-145` rewritten. The README now states "there are no Alpaca live MCP tools" while explicitly enumerating that ROB-73 adds `alpaca_paper_submit_order` and `alpaca_paper_cancel_order` as paper-only, confirm-gated tools with no runtime live switch and no bulk/by-symbol cancel. Lines `147-150` add forward references to both `docs/runbooks/alpaca-paper-readonly-smoke.md` (read-only) and `docs/runbooks/alpaca-paper-dev-smoke.md` (dev side-effect smoke). The README continues to assert that `place_order` / `replace_order` / `modify_order` / `cancel_all` and the generic Alpaca order-routing surface are not exposed.

### MUST-FIX 3 — Read-only smoke runbook would false-BLOCK once ROB-73 ships — RESOLVED

- **File:** `docs/runbooks/alpaca-paper-readonly-smoke.md`
- **Resolution:**
  - Step 4 (lines `92-99`) now states that after ROB-73, `alpaca_paper_submit_order` and `alpaca_paper_cancel_order` may be registered; instructs the operator not to call them in this read-only smoke; points at `alpaca-paper-dev-smoke.md` for the side-effect smoke; and narrows the BLOCK trigger to the forbidden subset only (`alpaca_live_*`, `place`, `replace`, `modify`, `cancel_all`, `cancel_orders`, `cancel_by_symbol`, generic order route).
  - Step 5 (line `116`) explicitly allows the two new tools to be visible without blocking, while continuing to forbid exercising them.
  - Step 7 BLOCKED rule (line `180`) uses the same forbidden subset.
  - Step 8 report template (line `197`) reads "no submit/cancel calls; no forbidden Alpaca live/generic/place/replace/modify/bulk-cancel tool".
- **Net effect:** the operator runbook will no longer false-BLOCK on a healthy ROB-73 production deployment.

## 4. Test Evidence

```text
$ uv run pytest tests/test_alpaca_paper_orders_tools.py \
                tests/test_alpaca_paper_dev_smoke_safety.py \
                tests/test_mcp_alpaca_paper_tools.py \
                tests/test_alpaca_paper_isolation.py -q
71 passed, 2 warnings in 1.96s
```

```text
$ uv run ruff check app/mcp_server/tooling/alpaca_paper_orders.py \
                    app/mcp_server/tooling/alpaca_paper_preview.py \
                    app/mcp_server/tooling/registry.py \
                    scripts/smoke/alpaca_paper_dev_smoke.py \
                    tests/test_alpaca_paper_orders_tools.py \
                    tests/test_alpaca_paper_dev_smoke_safety.py
All checks passed!

$ uv run ruff format --check ...
6 files already formatted
```

## 5. Non-blocking Observations (advisory only)

These are intentionally NOT must-fix, but worth noting as follow-ups:

- **Cancel `order_id` shape.** `alpaca_paper_cancel_order` only rejects blank `order_id` (`alpaca_paper_orders.py:175-177`). The plan §6.2 also envisioned rejecting literal `*`/`all`/`-`/comma-lists; the implementation relies on URL construction (`f"/v2/orders/{order_id}"`) plus Alpaca's 404 for any non-UUID path to cover those cases. Defense-in-depth could add a UUID-shape check, but Alpaca's cancel-all endpoint (`DELETE /v2/orders`, no trailing id) is unreachable from this code path, so the current implementation does not break the "no bulk cancel" guarantee.
- **Submit caps are hard-coded constants.** `SUBMIT_MAX_QTY = 5` and `SUBMIT_MAX_NOTIONAL_USD = 1000` (`alpaca_paper_orders.py:35-36`) are intentionally not env-driven. Document this in a follow-up if/when operators want to widen.
- **Read-back exception swallowed broadly.** `alpaca_paper_cancel_order` swallows `Exception` after a successful cancel (`alpaca_paper_orders.py:194-198`). This is correct for cancel idempotency; the comment already notes it. Worth a follow-up to narrow to the relevant exception classes only (`AlpacaPaperRequestError`) so unrelated bugs surface.
- **`OrderRequest` schema has no `asset_class` field.** The submit handler validates `asset_class == "us_equity"` via `PreviewOrderInput` but does not propagate it to the broker request, which is fine because Alpaca defaults to `us_equity`. Worth confirming once `OrderRequest` adds non-equity types.

None of the above changes the ship/fix decision.

## 6. Summary

- All ten hard safety constraints from the handoff prompt are enforced by the new code and tests.
- 71 tests pass, ruff lint and format are clean.
- The three pre-existing safety-claim documents (`app/mcp_server/tooling/alpaca_paper_preview.py` docstrings + MCP description, `app/mcp_server/README.md`, `docs/runbooks/alpaca-paper-readonly-smoke.md`) are now consistent with the new tool surface, so the read-only operator runbook will not false-BLOCK once ROB-73 reaches production.
- Branch is ready to merge.

## 7. Status block

```text
AOE_STATUS: review_passed
AOE_ISSUE: ROB-73
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-73-review-report.md
AOE_NEXT: create_pr
```
