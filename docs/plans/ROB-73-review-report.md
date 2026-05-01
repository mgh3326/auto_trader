# ROB-73 — Opus Code Review Report

Reviewer: Claude Opus
Branch: `feature/ROB-73-alpaca-paper-submit-cancel-dev-smoke`
Worktree: `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-73-alpaca-paper-submit-cancel-dev-smoke`
Base: `origin/main`
Head commit reviewed: `d08dafed`
Production baseline reference: `12a80b86aa2d639f30985e8a8e91252deabf61ca`

## 1. Scope Reviewed

Diff against `origin/main`: 9 files, 3,039 insertions, 10 deletions. Reviewed every changed file plus adjacent safety surfaces:

- New module `app/mcp_server/tooling/alpaca_paper_orders.py` (242 lines).
- Registry change `app/mcp_server/tooling/registry.py` (+4 lines).
- New dev smoke `scripts/smoke/alpaca_paper_dev_smoke.py` (215 lines).
- New tests `tests/test_alpaca_paper_orders_tools.py` (520 lines) and `tests/test_alpaca_paper_dev_smoke_safety.py` (203 lines).
- Updated guard tests `tests/test_mcp_alpaca_paper_tools.py` (-/+) and `tests/test_alpaca_paper_isolation.py`.
- New runbook `docs/runbooks/alpaca-paper-dev-smoke.md`.
- Plan `docs/plans/ROB-73-alpaca-paper-submit-cancel-dev-smoke.md`.

Adjacent files I read for cross-checks (unchanged, but used as the safety baseline):
`app/mcp_server/tooling/alpaca_paper_preview.py`, `app/mcp_server/tooling/alpaca_paper.py`, `app/services/brokers/alpaca/service.py`, `app/services/brokers/alpaca/schemas.py`, `app/services/brokers/alpaca/endpoints.py`, `app/mcp_server/README.md`, `docs/runbooks/alpaca-paper-readonly-smoke.md`.

## 2. Required-safety Verification

| Constraint (from handoff prompt) | Verdict | Evidence |
|---|---|---|
| Explicit paper-only tool names `alpaca_paper_submit_order`, `alpaca_paper_cancel_order` | PASS | `app/mcp_server/tooling/alpaca_paper_orders.py:30-33`, `211-230`. Registered in both `DEFAULT` and `HERMES_PAPER_KIS` profiles via `registry.py:99` (always-on block). |
| No live Alpaca endpoint use; live endpoint fail-closed; no runtime live switch | PASS | `_default_service_factory` returns `AlpacaPaperBrokerService()`; constructor enforces `base_url == PAPER_TRADING_BASE_URL` (`app/services/brokers/alpaca/service.py:42-54`) and rejects `LIVE_TRADING_BASE_URL` via `FORBIDDEN_TRADING_BASE_URLS`. Tool signatures contain no `endpoint`/`base_url`/`live`/`url`/`host`/`env` params (`tests/test_alpaca_paper_orders_tools.py:425-432`). Fail-closed asserted by `test_submit_fails_closed_on_live_endpoint` and `test_cancel_fails_closed_on_live_endpoint`. |
| No secret output / Authorization logging | PASS | New module imports nothing from `app.core.config`; never reads `settings.alpaca_paper_*`. `_model_to_jsonable` operates only on Pydantic order/cash/account models which contain no auth fields. Smoke script bans any secret strings (`tests/test_alpaca_paper_dev_smoke_safety.py:42-46`) and bans direct `print(payload|result|order|...)` (`tests/test_alpaca_paper_dev_smoke_safety.py:49-74`). |
| No generic `place_order`/`cancel_order`/`modify_order` integration | PASS | `app/mcp_server/tooling/orders_registration.py` contains zero references to `alpaca`. `tests/test_alpaca_paper_orders_tools.py:454-466` asserts forbidden tool names. Smoke script also banned from referencing `place_order`/`modify_order`/`replace_order`/`cancel_all`/`cancel_by_symbol` (`tests/test_alpaca_paper_dev_smoke_safety.py:78-88`). |
| Submit no-ops unless explicit confirmation | PASS | `alpaca_paper_orders.py:137-146`: `confirm is not True` returns `submitted: False, blocked_reason: confirmation_required` and skips the broker call. Verified by `test_submit_without_confirm_is_blocked_no_op`. |
| Cancel no-ops unless explicit confirmation and exact `order_id`; no bulk cancel | PASS (with one minor follow-up; see §4) | `alpaca_paper_orders.py:170-208` rejects blank `order_id`, returns `cancelled: False` unless `confirm is True`, and signature is exactly `(order_id, confirm)` (no `status=`/`symbol=`/`all=`). Verified by `test_cancel_signature_has_no_bulk_or_filter_params`. |
| Automated tests mock side effects | PASS | All 71 tests across the four covered files pass and use `FakeOrdersService`/`FakeAlpacaPaperService` via `set_alpaca_paper_orders_service_factory(...)`. No real broker call is reachable from tests. |
| Dev smoke defaults to preview/no side effects; dual-gate required for actual mutation | PASS | `scripts/smoke/alpaca_paper_dev_smoke.py:176-194` rejects either gate alone (rc=2, no broker call); only when both `--confirm-paper-side-effect` AND `ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS=1` are set does `_side_effect_smoke()` run. Each gate tested in isolation (`test_dev_smoke_flag_without_env_is_blocked`, `test_dev_smoke_env_without_flag_is_blocked`). |
| No `paper_001`/`paper_us_001`, DB registry, or strategy-profile mapping | PASS | Repo grep for `paper_001`/`paper_us_001` returns hits only in plans/runbooks (rule references), not in any new code. No DB migration, no profile/registry table. |

## 3. Test Evidence

```text
$ uv run pytest tests/test_alpaca_paper_orders_tools.py \
                tests/test_alpaca_paper_dev_smoke_safety.py \
                tests/test_mcp_alpaca_paper_tools.py \
                tests/test_alpaca_paper_isolation.py -q
71 passed, 2 warnings in 1.95s
```

```text
$ uv run ruff check app/mcp_server/tooling/alpaca_paper_orders.py \
                    app/mcp_server/tooling/registry.py \
                    scripts/smoke/alpaca_paper_dev_smoke.py \
                    tests/test_alpaca_paper_orders_tools.py \
                    tests/test_alpaca_paper_dev_smoke_safety.py
All checks passed!

$ uv run ruff format --check ...
5 files already formatted
```

## 4. Must-fix Findings

The implementation safety-gates are sound, but ROB-73 has shipped without updating three pre-existing safety-claim documents that previously asserted no `alpaca_paper_submit_order` / `alpaca_paper_cancel_order` would ever exist. After this PR ships, those claims are factually wrong and one of them turns the read-only operator runbook into a false-positive blocker the moment ROB-73 reaches production. They must be updated as part of this slice.

### MUST-FIX 1 — Stale MCP tool description on `alpaca_paper_preview_order`

- **File:** `app/mcp_server/tooling/alpaca_paper_preview.py`
- **Lines:**
  - `1-6` (module docstring)
  - `199-204` (function docstring)
  - `277-283` (the `description=...` string passed to `mcp.tool(...)`, which is the actual MCP-client-visible documentation)
- **Severity:** Important (must-fix). The string at line 281 is shown to LLM clients of the MCP server and currently asserts: *"There is no alpaca_paper_submit_order / place_order / cancel_order / modify_order / replace_order tool."* After this PR, both `alpaca_paper_submit_order` and `alpaca_paper_cancel_order` are registered, so the description directly contradicts the live tool list and could lead an LLM consumer to skip the new tools, or worse, to fall back to the legacy generic order tools because it believes the explicit Alpaca paper write surface does not exist.
- **Suggested fix:** rewrite the three locations to reflect the new contract. Example for the `description=` string:
  ```python
  description=(
      "Preview and validate an Alpaca paper US equity order without submitting it. "
      "Pure validator + echo — preview only, no side effects, does not submit. "
      "Does NOT call POST /v2/orders. "
      "Submission goes through the explicit, paper-only, confirm-gated tool "
      "alpaca_paper_submit_order; cancellation through alpaca_paper_cancel_order. "
      "There is no alpaca_paper_place_order / replace_order / modify_order tool, "
      "and Alpaca paper is never routed through the generic place_order / "
      "cancel_order / modify_order surface."
  ),
  ```
  Apply equivalent edits to the module docstring (lines 1-6) and the function docstring (lines 199-204).

### MUST-FIX 2 — Stale safety boundary in `app/mcp_server/README.md`

- **File:** `app/mcp_server/README.md`
- **Lines:** `140-143`
- **Severity:** Important (must-fix). The README declares: *"Safety boundary: there are no Alpaca live MCP tools in this issue and no `alpaca_paper_submit_order`, `alpaca_paper_cancel_order`, replace/modify tool, or generic Alpaca order-routing surface."* This is now false on `alpaca_paper_submit_order` and `alpaca_paper_cancel_order` and it sits in the canonical README that operators consult when triaging. Future readers cannot reconcile the README with the actual tool inventory.
- **Suggested fix:** replace lines 140-143 with a paragraph that:
  1. keeps the no-live-tool claim,
  2. explicitly enumerates that `alpaca_paper_submit_order` / `alpaca_paper_cancel_order` exist as paper-only, confirm-gated, adapter-specific tools (no live switch, no bulk cancel, no generic order route),
  3. adds a forward reference to `docs/runbooks/alpaca-paper-dev-smoke.md` for ROB-73's dev smoke,
  4. continues to state that `place_order`/`replace_order`/`modify_order`/`cancel_all`/`cancel_by_symbol` and the generic order route are NOT exposed.

### MUST-FIX 3 — Read-only smoke runbook will false-BLOCK once ROB-73 ships

- **File:** `docs/runbooks/alpaca-paper-readonly-smoke.md`
- **Lines:**
  - `92` ("If any registered Alpaca paper MCP name includes `submit`, `place`, `cancel`, `replace`, or `modify`, mark **BLOCKED**.")
  - `108` ("No Alpaca submit/place/cancel/replace/modify tool is visible.")
  - `172` ("A forbidden tool name appears (`submit`, `place`, `cancel`, `replace`, or `modify`).")
  - `189` (report template: "no submit/place/cancel/replace/modify calls; ...")
- **Severity:** High (must-fix). This is the runbook ops follows on the production host. After ROB-73 reaches `production` and the operator runs this read-only smoke, Step 4 / Step 5 / Step 7 will instruct them to mark **BLOCKED** because `alpaca_paper_submit_order` and `alpaca_paper_cancel_order` are now registered. That is a self-inflicted operational regression: the runbook would block a healthy deployment.
- **Suggested fix:** update the four locations so the BLOCK trigger is the *forbidden subset only*: `place`, `replace`, `modify`, `cancel_all`, `cancel_orders`, `cancel_by_symbol`, and any `alpaca_live_*` name. Explicitly allow `alpaca_paper_submit_order` and `alpaca_paper_cancel_order` while restating that they (a) require `confirm=True`, (b) are paper-only by construction, and (c) are *not* exercised by this read-only smoke. Add a forward link to `docs/runbooks/alpaca-paper-dev-smoke.md` so an operator who sees the new tools knows where the side-effect smoke lives. Step 4 expected output ("exactly 7 names") and Step 5/Step 7 forbidden-name lists need the same treatment.

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
- Three pre-existing safety-claim documents must be updated in this slice so the rest of the repository stays consistent with the new tool surface; without those edits, the read-only operator runbook becomes a false-positive blocker as soon as ROB-73 reaches production.

## 7. Status block

```text
AOE_STATUS: review_must_fix
AOE_ISSUE: ROB-73
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-73-review-report.md
AOE_MUST_FIX_COUNT: 3
AOE_NEXT: start_fix_implementer
```
