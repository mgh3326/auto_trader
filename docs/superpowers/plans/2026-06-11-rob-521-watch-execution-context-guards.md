# ROB-521 Watch Execution Context Guards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close ROB-521 follow-up bugs after ROB-514 by validating legacy watch `max_action` at ingestion, aligning recommendation attach/commit gates, and correcting public contract docs.

**Architecture:** Keep the behavior narrow and additive: validation stays in `IngestReportItem`, MCP item validation continues to return batched `item_errors`, and recommendation persistence uses one shared verdict gate for both explicit `investment_watch_recommend(commit=True)` and `investment_report_activate_watch(attach_recommendation=True)`. No DB migration, broker call, order submission, scheduler change, or Hermes renderer implementation belongs in this repo change.

**Tech Stack:** Python 3.13, Pydantic v2, FastMCP handler functions, SQLAlchemy async repository, pytest, Ruff, ty.

---

## Starting State And Scope

Current local `rob-521` is behind `origin/main` by three commits. `origin/main` includes ROB-514 (`81d03aa2`) and has the actual bug surface:

- `app/schemas/investment_reports.py`: `_validate_max_action()` validates only `operation in ("create", "modify")`.
- `app/mcp_server/tooling/investment_reports_handlers.py`: `attach_recommendation=True` persists `watch_recommendation` without the `_RECOMMEND_VERDICTS` gate used by `investment_watch_recommend(commit=True)`.
- `app/mcp_server/README.md` and `CREATE_DESCRIPTION` say `account_mode` is optional/supported, but `MaxActionPayload.account_mode` is required.
- `CLAUDE.md` does not mention the ROB-458 item contract additions for `trigger_checklist` and `max_action`.
- `docs/runbooks/watch-trigger-hermes-payload.md` already records Hermes renderer requirements for `planned_action` and `trigger_checklist`; Hermes renderer implementation remains cross-repo/out of scope.

## File Structure

- Modify: `app/schemas/investment_reports.py`
  - Expand watch `max_action` validation to include `operation=None`.
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py`
  - Add one helper for the watch recommendation verdict gate.
  - Use it in both `investment_watch_recommend_impl(commit=True)` and `investment_report_activate_watch_impl(attach_recommendation=True)`.
  - Correct `CREATE_DESCRIPTION`/`ADD_ITEMS_DESCRIPTION`/validation notes so `account_mode` is clearly required when `max_action` is present.
- Modify: `app/mcp_server/README.md`
  - Correct the `investment_report_create` watch execution contract.
- Modify: `CLAUDE.md`
  - Add a concise investment report item contract note for agent authors.
- Modify: `tests/test_investment_reports_schemas.py`
  - Schema regressions for legacy watch `max_action` and required `account_mode`.
- Modify: `tests/mcp_server/test_investment_report_create_handler.py`
  - Batched `item_errors` regression and description text regression.
- Modify: `tests/test_investment_reports_mcp.py`
  - Attach verdict-gate regression.

## Task 0: Sync Branch To The Issue Baseline

**Files:**
- No code files intentionally modified.

- [ ] **Step 1: Confirm clean worktree except this plan**

Run:

```bash
git status --short
```

Expected: either empty, or only `docs/superpowers/plans/2026-06-11-rob-521-watch-execution-context-guards.md` if this plan is already present.

- [ ] **Step 2: Fast-forward `rob-521` to `origin/main`**

Run:

```bash
git fetch origin
git merge --ff-only origin/main
```

Expected: branch advances to include `e2f9ec74`, `81d03aa2`, and `a629cb77`. If ff-only is rejected, stop and inspect local commits with:

```bash
git log --oneline --left-right --cherry-pick HEAD...origin/main
```

- [ ] **Step 3: Verify ROB-514 is present**

Run:

```bash
git branch --contains 81d03aa2
rg -n "attach_recommendation|amount_krw|limit_price_hint|trigger_checklist" app/schemas/investment_reports.py app/mcp_server/tooling/investment_reports_handlers.py tests/test_investment_reports_schemas.py
```

Expected: current branch is listed, and `attach_recommendation` appears in `investment_reports_handlers.py`.

## Task 1: Validate `max_action` For Legacy `operation=None` Watches

**Files:**
- Modify: `tests/test_investment_reports_schemas.py`
- Modify: `tests/mcp_server/test_investment_report_create_handler.py`
- Modify: `app/schemas/investment_reports.py`

- [ ] **Step 1: Add schema regression tests**

Append these tests after `test_ingest_item_validates_max_action_when_present()` in `tests/test_investment_reports_schemas.py`:

```python
def test_ingest_item_validates_max_action_for_legacy_watch_when_present() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IngestReportItem(
            client_item_key="legacy-watch",
            item_kind="watch",
            intent="buy_review",
            rationale="r",
            symbol="005930",
            watch_condition={"metric": "price", "operator": "below", "threshold": "5"},
            valid_until="2026-12-31T00:00:00Z",
            max_action={"side": "buy", "account_mode": "kis_mock"},
        )

    message = str(exc_info.value)
    assert "max_action" in message
    assert "quantity or notional" in message


def test_max_action_requires_account_mode() -> None:
    with pytest.raises(ValidationError) as exc_info:
        MaxActionPayload(side="buy", quantity="10")

    assert "account_mode" in str(exc_info.value)
```

- [ ] **Step 2: Add batched MCP item-error regression**

Append this test after `test_validate_report_items_rejects_unknown_keys()` in `tests/mcp_server/test_investment_report_create_handler.py`:

```python
def test_validate_report_items_rejects_legacy_watch_bad_max_action():
    _validated, error = h._validate_report_items(
        [
            {
                "client_item_key": "legacy-watch",
                "item_kind": "watch",
                "intent": "buy_review",
                "rationale": "r",
                "symbol": "005930",
                "watch_condition": {
                    "metric": "price",
                    "operator": "below",
                    "threshold": "5",
                },
                "valid_until": "2026-12-31T00:00:00Z",
                "max_action": {"side": "buy", "account_mode": "kis_mock"},
            }
        ]
    )

    assert error is not None
    assert error["error"] == "invalid_items"
    assert error["item_errors"][0]["index"] == 0
    assert "max_action" in str(error["item_errors"][0]["errors"])
    assert "quantity or notional" in str(error["item_errors"][0]["errors"])
```

- [ ] **Step 3: Run tests and confirm failure**

Run:

```bash
uv run pytest tests/test_investment_reports_schemas.py::test_ingest_item_validates_max_action_for_legacy_watch_when_present tests/test_investment_reports_schemas.py::test_max_action_requires_account_mode tests/mcp_server/test_investment_report_create_handler.py::test_validate_report_items_rejects_legacy_watch_bad_max_action -q
```

Expected: first and third tests fail because `operation=None` watch `max_action` is not validated yet. `test_max_action_requires_account_mode` may already pass.

- [ ] **Step 4: Expand schema validation**

In `app/schemas/investment_reports.py`, replace `_validate_max_action()` with:

```python
    @model_validator(mode="after")
    def _validate_max_action(self) -> IngestReportItem:
        if (
            self.item_kind == "watch"
            and self.operation in (None, "create", "modify")
            and self.max_action
        ):
            MaxActionPayload.model_validate(self.max_action)
        return self
```

- [ ] **Step 5: Verify Task 1**

Run:

```bash
uv run pytest tests/test_investment_reports_schemas.py::test_ingest_item_validates_max_action_for_legacy_watch_when_present tests/test_investment_reports_schemas.py::test_max_action_requires_account_mode tests/mcp_server/test_investment_report_create_handler.py::test_validate_report_items_rejects_legacy_watch_bad_max_action -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add app/schemas/investment_reports.py tests/test_investment_reports_schemas.py tests/mcp_server/test_investment_report_create_handler.py
git commit -m "fix(ROB-521): validate legacy watch max_action at ingest"
```

## Task 2: Share Verdict Gate Between Attach And Commit

**Files:**
- Modify: `tests/test_investment_reports_mcp.py`
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py`

- [ ] **Step 1: Add attach gate regression test**

Append this test after `test_activate_watch_attach_recommendation_persists()` in `tests/test_investment_reports_mcp.py`:

```python
@pytest.mark.asyncio
async def test_activate_watch_attach_recommendation_rejects_non_watch_verdict(
    session: AsyncSession, _stub_market_data
) -> None:
    item = dict(_review_watch_item_dict())
    item["evidence_snapshot"] = {"action_verdict": "buy_review"}
    created = await investment_report_create_impl(items=[item], **_create_kwargs())
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    watch_uuid = bundle["items"][0]["item_uuid"]
    await investment_report_decide_item_impl(
        item_uuid=watch_uuid, decision="approve", actor="operator"
    )

    response = await investment_report_activate_watch_impl(
        item_uuid=watch_uuid,
        actor="operator",
        watch_condition={"metric": "price", "operator": "below", "threshold": 70000},
        valid_until=future_datetime().isoformat(),
        attach_recommendation=True,
    )

    assert response["success"] is True
    assert response["recommendation_attached"] is False
    assert "watch_only" in response["recommendation_attach_error"]
    assert "limit_wait" in response["recommendation_attach_error"]

    bundle_post = await investment_report_get_impl(created["report"]["report_uuid"])
    assert bundle_post["items"][0]["watch_recommendation"] is None
```

- [ ] **Step 2: Run test and confirm failure**

Run:

```bash
uv run pytest tests/test_investment_reports_mcp.py::test_activate_watch_attach_recommendation_rejects_non_watch_verdict -q
```

Expected: FAIL because attach currently persists a recommendation for `action_verdict="buy_review"`.

- [ ] **Step 3: Add a shared gate helper**

In `app/mcp_server/tooling/investment_reports_handlers.py`, add this helper directly after `_MARKET_MAP`:

```python
def _watch_recommendation_verdict_error(item: Any, *, action: str) -> str | None:
    verdict = None
    evidence_snapshot = getattr(item, "evidence_snapshot", None)
    if isinstance(evidence_snapshot, dict):
        verdict = evidence_snapshot.get("action_verdict")
    if verdict not in _RECOMMEND_VERDICTS:
        return (
            f"{action} requires item action_verdict in "
            f"{{watch_only, limit_wait}}; got {verdict!r}"
        )
    return None
```

- [ ] **Step 4: Apply the helper to `attach_recommendation`**

In `investment_report_activate_watch_impl()`, replace the start of the attach block:

```python
        if request.attach_recommendation and item_row is not None:
            if item_row.watch_recommendation:
                recommendation_attached = True
            elif item_row.symbol is None:
```

with:

```python
        if request.attach_recommendation and item_row is not None:
            gate_error = _watch_recommendation_verdict_error(
                item_row, action="attach_recommendation"
            )
            if gate_error is not None:
                recommendation_attached = False
                recommendation_attach_error = gate_error
            elif item_row.watch_recommendation:
                recommendation_attached = True
            elif item_row.symbol is None:
```

Keep the existing fail-open `try`/`except` around market data computation unchanged.

- [ ] **Step 5: Apply the helper to `commit=True`**

In `investment_watch_recommend_impl()`, replace:

```python
        if item_uuid is None or item is None:
            raise ValueError("commit=True requires an existing item_uuid")
        verdict = None
        if isinstance(item.evidence_snapshot, dict):
            verdict = item.evidence_snapshot.get("action_verdict")
        if verdict not in _RECOMMEND_VERDICTS:
            raise ValueError(
                "commit requires item action_verdict in {watch_only, limit_wait}; "
                f"got {verdict!r}"
            )
```

with:

```python
        if item_uuid is None or item is None:
            raise ValueError("commit=True requires an existing item_uuid")
        gate_error = _watch_recommendation_verdict_error(item, action="commit")
        if gate_error is not None:
            raise ValueError(gate_error)
```

- [ ] **Step 6: Verify Task 2**

Run:

```bash
uv run pytest tests/test_investment_reports_mcp.py::test_watch_recommend_commit_persists_on_watch_only tests/test_investment_reports_mcp.py::test_watch_recommend_commit_rejected_for_non_watch_verdict tests/test_investment_reports_mcp.py::test_activate_watch_attach_recommendation_persists tests/test_investment_reports_mcp.py::test_activate_watch_attach_recommendation_rejects_non_watch_verdict tests/test_investment_reports_mcp.py::test_activate_watch_attach_recommendation_fails_open -q
```

Expected: PASS. Attach remains fail-open for market-data failures, but refuses to persist recommendation JSON for non-`watch_only`/`limit_wait` verdicts.

- [ ] **Step 7: Commit Task 2**

```bash
git add app/mcp_server/tooling/investment_reports_handlers.py tests/test_investment_reports_mcp.py
git commit -m "fix(ROB-521): align watch recommendation attach gate"
```

## Task 3: Correct Contract Documentation For `account_mode`, `trigger_checklist`, And `max_action`

**Files:**
- Modify: `tests/mcp_server/test_investment_report_create_handler.py`
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py`
- Modify: `app/mcp_server/README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add description regression test**

Append this test after `test_create_description_documents_trade_plan_and_unknown_key_policy()` in `tests/mcp_server/test_investment_report_create_handler.py`:

```python
def test_create_description_documents_required_max_action_account_mode():
    combined = h.CREATE_DESCRIPTION + " " + h.ADD_ITEMS_DESCRIPTION
    assert "account_mode is required" in combined
    assert "quantity or notional" in combined
    assert "trigger_checklist" in combined
    assert "planned_action" in combined
```

- [ ] **Step 2: Run description test and confirm failure**

Run:

```bash
uv run pytest tests/mcp_server/test_investment_report_create_handler.py::test_create_description_documents_required_max_action_account_mode -q
```

Expected: FAIL because current descriptions mention `account_mode` but not that it is required.

- [ ] **Step 3: Update MCP descriptions and invalid-item notes**

In `app/mcp_server/tooling/investment_reports_handlers.py`, update the watch execution context sentence in `CREATE_DESCRIPTION` to:

```python
    "Watch execution context: trigger_checklist is string[] and is copied into "
    "watch alert notifications. max_action is the structured execution-plan JSON; "
    "account_mode is required when max_action is present; required keys are side "
    "and exactly one of quantity or notional. "
    "Optional keys include amount_krw, limit_price, limit_price_hint, and "
    "ladder_level. planned_action in Hermes payloads is derived from max_action; "
    "do not send planned_action as an item key. "
```

In `ADD_ITEMS_DESCRIPTION`, keep the same contract wording by replacing the current watch execution sentence with:

```python
    "rewritten. No broker / order / watch mutation. For watch items, trigger_checklist "
    "string[] and max_action execution-plan keys follow the same contract as "
    "investment_report_create: account_mode is required when max_action is present; "
    "max_action also requires side and exactly one of quantity or notional. "
```

In `_validate_report_items()` notes, replace the `max_action` parenthetical with:

```python
                " trigger_checklist must be string[]; watch execution plans belong in "
                "max_action (required: side, account_mode, exactly one of "
                "quantity/notional; optional: amount_krw, limit_price, "
                "limit_price_hint, ladder_level), not in planned_action."
```

- [ ] **Step 4: Update MCP README**

In `app/mcp_server/README.md`, replace the `max_action` bullet under `Watch execution context fields` with:

```markdown
- `max_action`: structured watch execution-plan JSON. `account_mode` is required when `max_action` is present; it also requires `side` and exactly one of `quantity` or `notional`. Optional keys include `amount_krw`, `limit_price`, `limit_price_hint`, and `ladder_level`.
```

- [ ] **Step 5: Add CLAUDE.md contract note**

Add this section after `### Runtime LLM ownership boundary` in `CLAUDE.md`, before the next top-level architecture content:

```markdown
### Investment Report Item Contract

`investment_report_create` / `investment_report_add_items` reject unknown top-level item keys. Use typed fields for current contracts:

- `trigger_checklist`: `string[]`; copied to watch trigger notifications.
- `max_action`: structured execution-plan JSON for watch items. `account_mode` is required when `max_action` is present; it also requires `side` and exactly one of `quantity` or `notional`; optional keys include `amount_krw`, `limit_price`, `limit_price_hint`, and `ladder_level`.
- Do not send `planned_action` as an item key. Hermes payloads derive `planned_action` from `max_action`.
```

- [ ] **Step 6: Verify Task 3**

Run:

```bash
uv run pytest tests/mcp_server/test_investment_report_create_handler.py::test_create_description_documents_required_max_action_account_mode tests/test_investment_reports_mcp.py::test_create_description_mentions_watch_execution_plan_contract -q
rg -n "account_mode is required|required.*account_mode|trigger_checklist|max_action|planned_action" app/mcp_server/tooling/investment_reports_handlers.py app/mcp_server/README.md CLAUDE.md
```

Expected: pytest PASS. `rg` shows the required `account_mode` wording in MCP handler descriptions/README and `trigger_checklist`/`max_action` in `CLAUDE.md`.

- [ ] **Step 7: Commit Task 3**

```bash
git add app/mcp_server/tooling/investment_reports_handlers.py app/mcp_server/README.md CLAUDE.md tests/mcp_server/test_investment_report_create_handler.py
git commit -m "docs(ROB-521): clarify watch max_action account_mode contract"
```

## Task 4: Final Regression And Linear Handoff

**Files:**
- No further implementation files unless final verification exposes a failure.

- [ ] **Step 1: Run focused regression suite**

Run:

```bash
uv run pytest tests/test_investment_reports_schemas.py tests/mcp_server/test_investment_report_create_handler.py tests/test_investment_reports_mcp.py tests/test_hermes_client.py tests/test_investment_watch_scanner.py tests/test_watch_validity_review.py -q
```

Expected: PASS.

- [ ] **Step 2: Run lint/type checks for touched code**

Run:

```bash
uv run ruff check app/schemas/investment_reports.py app/mcp_server/tooling/investment_reports_handlers.py tests/test_investment_reports_schemas.py tests/mcp_server/test_investment_report_create_handler.py tests/test_investment_reports_mcp.py
uv run ty check app/schemas/investment_reports.py app/mcp_server/tooling/investment_reports_handlers.py
```

Expected: PASS.

- [ ] **Step 3: Confirm Hermes remains tracked but out of scope**

Run:

```bash
rg -n "Render `planned_action`|trigger_checklist|do not invent quantity" docs/runbooks/watch-trigger-hermes-payload.md
```

Expected: runbook already documents Hermes renderer requirements. Do not implement Hermes renderer code in this repo.

- [ ] **Step 4: Prepare Linear completion comment**

Post a Linear comment on `ROB-521` with this content after tests pass:

```markdown
Implemented ROB-521 in auto_trader:

- `operation=None` watch items now validate non-empty `max_action` at ingestion and return batched `item_errors`.
- `investment_report_activate_watch(attach_recommendation=True)` now uses the same `{watch_only, limit_wait}` verdict gate as `investment_watch_recommend(commit=True)`.
- MCP descriptions, README, and CLAUDE.md now state that `max_action.account_mode` is required and that `planned_action` is derived, not accepted as item input.
- Hermes renderer remains cross-repo/out-of-scope; auto_trader runbook already tracks the `planned_action` + `trigger_checklist` render contract.

Verification:
- `uv run pytest tests/test_investment_reports_schemas.py tests/mcp_server/test_investment_report_create_handler.py tests/test_investment_reports_mcp.py tests/test_hermes_client.py tests/test_investment_watch_scanner.py tests/test_watch_validity_review.py -q`
- `uv run ruff check ...`
- `uv run ty check ...`
```

- [ ] **Step 5: Label/status guidance**

Before PR/merge, apply labels only if the actual diff expands beyond this plan:

- Routine planned scope: `Bug`, `follow-up`, `auto_trader`, `trading-decision`.
- If implementation changes broker/order execution behavior, live account boundaries, DB migrations, or strategy policy beyond the advisory gate alignment, add `high_risk_change` + `needs_stronger_model_review` and hold merge for stronger review.

## Self-Review

- Spec coverage: requirement 1 is Task 1; requirement 2 is Task 2; requirement 3 is Task 3; requirement 4/Hermes tracking is Task 4 Step 3 and Linear comment.
- Placeholder scan: no placeholder tokens, no unspecified "handle edge cases", and every code change step includes concrete snippets.
- Type consistency: helper uses existing `Any` import; `_RECOMMEND_VERDICTS` remains the single gate vocabulary; `account_mode` remains required in `MaxActionPayload`.
