# ROB-816 PR-3a Approval Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make order-proposal approval expire safely, expose actionable Telegram failures, add an operator void tool, and bind validated ROB-800 loss-cut intent from proposal creation through live submit.

**Architecture:** Keep all durable intent on the proposal group and all writes behind `OrderProposalsService`. Add an additive nullable-column migration, validate stable loss-cut prerequisites at create time, defer Paperclip status and final ROB-800 authorization to click-time `_place_order_impl`, and keep revalidation fail-closed while distinguishing policy guards from internal failures.

**Tech Stack:** Python 3.13, FastAPI/FastMCP, SQLAlchemy async ORM, Alembic, PostgreSQL, pytest/pytest-asyncio, Ruff, ty, uv.

## Global Constraints

- Base is `main` at or after `6e5795e2`; PR-3a must not depend on PR-3b or PR-3c.
- All four loss-cut columns live on `review.order_proposals`, are nullable, and are additive-only.
- `exit_intent IS NULL` is the backward-compatible ordinary-order path; the only accepted non-null value is `loss_cut`.
- Create-time validation checks required fields and retrospective existence, symbol, trigger type, and 72-hour freshness. It must not fetch Paperclip status.
- Click-time preview and submit pass all four stored values unchanged to `_place_order_impl`; ROB-800 remains the final guard.
- Real broker and Telegram calls are mocked in tests. Never run a live order.
- Quality-contract Markdown migration is out of scope.
- Open the PR but do not merge it.

---

### Task 1: Add nullable group-level loss-cut columns

**Files:**
- Create: `alembic/versions/20260711_rob816_proposal_exit_binding.py`
- Modify: `app/models/order_proposals.py:81-116`
- Modify: `tests/_schema_bootstrap.py:17-25,365-468`
- Modify: `tests/services/order_proposals/test_models_smoke.py`

**Interfaces:**
- Consumes: Alembic head `20260710_rob816_order_proposals`.
- Produces: `OrderProposal.exit_intent: str | None`, `exit_reason: str | None`, `retrospective_id: int | None`, and `approval_issue_id: str | None`.

- [ ] **Step 1: Write the failing model test**

Add this test to `tests/services/order_proposals/test_models_smoke.py`:

```python
def test_order_proposal_has_group_level_loss_cut_binding_columns():
    columns = OrderProposal.__table__.columns
    assert columns["exit_intent"].nullable
    assert columns["exit_reason"].nullable
    assert columns["retrospective_id"].nullable
    assert columns["approval_issue_id"].nullable
    assert "exit_intent" not in OrderProposalRung.__table__.columns
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run pytest -q tests/services/order_proposals/test_models_smoke.py::test_order_proposal_has_group_level_loss_cut_binding_columns
```

Expected: FAIL because `OrderProposal` has no `exit_intent` column.

- [ ] **Step 3: Add the ORM fields and additive migration**

Add these fields beside the proposal rationale/lot fields in `OrderProposal`:

```python
exit_intent: Mapped[str | None] = mapped_column(Text)
exit_reason: Mapped[str | None] = mapped_column(Text)
retrospective_id: Mapped[int | None] = mapped_column(BigInteger)
approval_issue_id: Mapped[str | None] = mapped_column(Text)
```

Create the migration with this exact upgrade/downgrade shape:

```python
revision = "20260711_rob816_exit_binding"
down_revision = "20260710_rob816_order_proposals"


def upgrade() -> None:
    op.add_column("order_proposals", sa.Column("exit_intent", sa.Text(), nullable=True), schema="review")
    op.add_column("order_proposals", sa.Column("exit_reason", sa.Text(), nullable=True), schema="review")
    op.add_column("order_proposals", sa.Column("retrospective_id", sa.BigInteger(), nullable=True), schema="review")
    op.add_column("order_proposals", sa.Column("approval_issue_id", sa.Text(), nullable=True), schema="review")


def downgrade() -> None:
    op.drop_column("order_proposals", "approval_issue_id", schema="review")
    op.drop_column("order_proposals", "retrospective_id", schema="review")
    op.drop_column("order_proposals", "exit_reason", schema="review")
    op.drop_column("order_proposals", "exit_intent", schema="review")
```

Add four `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` strings to
`_SCHEMA_PATCH_STATEMENTS` in `tests/_schema_bootstrap.py`. Do not add a DB
check constraint; application validation owns the current `loss_cut` enum and
all existing rows must remain valid.

- [ ] **Step 4: Run model and migration checks**

Run:

```bash
uv run pytest -q tests/services/order_proposals/test_models_smoke.py
uv run alembic heads
```

Expected: model tests PASS and Alembic prints one head,
`20260711_rob816_exit_binding`.

- [ ] **Step 5: Commit the schema unit**

```bash
git add alembic/versions/20260711_rob816_proposal_exit_binding.py app/models/order_proposals.py tests/_schema_bootstrap.py tests/services/order_proposals/test_models_smoke.py
git commit -m "feat(ROB-816): add proposal loss-cut binding columns"
```

### Task 2: Bind loss-cut fields in payload hashing and create-time validation

**Files:**
- Modify: `app/services/order_proposals/payload.py:25-50`
- Modify: `app/services/order_proposals/service.py:13-160`
- Modify: `tests/services/order_proposals/test_payload.py`
- Modify: `tests/services/order_proposals/test_service.py`

**Interfaces:**
- Consumes: `get_retrospective_by_id(session, retrospective_id)` and group columns from Task 1.
- Produces: `create_proposal(..., exit_intent=None, exit_reason=None, retrospective_id=None, approval_issue_id=None, now=None)` with default end-of-day KST validity and fail-closed stable loss-cut validation.

- [ ] **Step 1: Write failing payload-binding tests**

Add to `tests/services/order_proposals/test_payload.py`:

```python
@pytest.mark.unit
def test_loss_cut_binding_changes_payload_hash():
    ordinary = compute_proposal_payload_hash(
        symbol="005930", market="equity_kr", account_mode="kis_live",
        order_type="limit", rungs=[ProposalRungSpec(0, "sell", "1", "70000", None)],
    )
    loss_cut = compute_proposal_payload_hash(
        symbol="005930", market="equity_kr", account_mode="kis_live",
        order_type="limit", rungs=[ProposalRungSpec(0, "sell", "1", "70000", None)],
        exit_intent="loss_cut", exit_reason="stop_loss",
        retrospective_id=42, approval_issue_id="ROB-800",
    )
    assert ordinary != loss_cut
```

- [ ] **Step 2: Run the payload test and verify RED**

```bash
uv run pytest -q tests/services/order_proposals/test_payload.py::test_loss_cut_binding_changes_payload_hash
```

Expected: FAIL because `compute_proposal_payload_hash` does not accept the four
binding arguments.

- [ ] **Step 3: Extend the canonical payload**

Add optional keyword parameters to `compute_proposal_payload_hash` and include
them as top-level canonical values:

```python
exit_intent: str | None = None,
exit_reason: str | None = None,
retrospective_id: int | None = None,
approval_issue_id: str | None = None,
```

```python
"exit_intent": exit_intent,
"exit_reason": exit_reason,
"retrospective_id": retrospective_id,
"approval_issue_id": approval_issue_id,
```

Keep TTL/timestamps excluded so a validity-only change still leaves the hash
stable.

- [ ] **Step 4: Write failing service tests for defaults and loss-cut validation**

Add service tests that use a real `TradeRetrospective` row and an injected
timezone-aware `now`:

```python
@pytest.mark.asyncio
async def test_create_defaults_valid_until_to_next_kst_midnight(db_session):
    now = datetime(2026, 7, 11, 14, 30, tzinfo=KST)
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930", market="equity_kr", account_mode="kis_live",
        side="buy", order_type="limit", proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("70000"), None)],
        now=now,
    )
    assert group.valid_until == datetime(2026, 7, 12, 0, 0, tzinfo=KST)


@pytest.mark.asyncio
async def test_loss_cut_requires_all_group_fields_without_paperclip_lookup(db_session, monkeypatch):
    async def paperclip_must_not_run(*args, **kwargs):
        raise AssertionError("Paperclip status belongs to click-time revalidation")
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_validation._fetch_approval_issue_status",
        paperclip_must_not_run,
    )
    service = OrderProposalsService(db_session)
    with pytest.raises(OrderProposalError, match="exit_reason"):
        await service.create_proposal(
            symbol="005930", market="equity_kr", account_mode="kis_live",
            side="sell", order_type="limit", proposer="p",
            rungs=[RungInput(0, "sell", Decimal("1"), Decimal("65000"), None)],
            exit_intent="loss_cut", retrospective_id=42,
            approval_issue_id="ROB-800", now=datetime.now(UTC),
        )
```

Use a small fake retrospective and cover every stable prerequisite explicitly:

```python
def _retro(*, symbol="005930", trigger_type="stop_loss", created_at=None):
    return SimpleNamespace(
        symbol=symbol,
        trigger_type=trigger_type,
        created_at=created_at or datetime.now(UTC),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"retrospective_id": None}, "retrospective_id"),
        ({"approval_issue_id": None}, "approval_issue_id"),
        ({"exit_reason": None}, "exit_reason"),
        ({"exit_intent": "emergency"}, "unknown exit_intent"),
    ],
)
async def test_loss_cut_required_fields_fail_closed(db_session, overrides, message):
    service = OrderProposalsService(db_session)
    kwargs = _loss_cut_create_kwargs(now=datetime.now(UTC))
    kwargs.update(overrides)
    with pytest.raises(OrderProposalError, match=message):
        await service.create_proposal(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retro", "message"),
    [
        (None, "not found"),
        (_retro(symbol="000660"), "symbol mismatch"),
        (_retro(trigger_type="fill"), "trigger_type"),
        (_retro(created_at=datetime.now(UTC) - timedelta(hours=73)), "stale"),
    ],
)
async def test_loss_cut_retrospective_validation(db_session, monkeypatch, retro, message):
    async def fake_lookup(session, retrospective_id):
        return retro
    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    with pytest.raises(OrderProposalError, match=message):
        await OrderProposalsService(db_session).create_proposal(
            **_loss_cut_create_kwargs(now=datetime.now(UTC))
        )


@pytest.mark.asyncio
async def test_valid_loss_cut_persists_exact_group_binding(db_session, monkeypatch):
    async def fake_lookup(session, retrospective_id):
        return _retro()
    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    group = await OrderProposalsService(db_session).create_proposal(
        **_loss_cut_create_kwargs(now=datetime.now(UTC))
    )
    assert (
        group.exit_intent,
        group.exit_reason,
        group.retrospective_id,
        group.approval_issue_id,
    ) == ("loss_cut", "stop_loss", 42, "ROB-800")
```

Keep the existing ordinary-order tests unchanged; their omitted binding fields
exercise the all-null backward-compatible path.

- [ ] **Step 5: Run the service tests and verify RED**

```bash
uv run pytest -q tests/services/order_proposals/test_service.py -k 'valid_until or loss_cut'
```

Expected: FAIL because the service lacks `now` and loss-cut binding parameters.

- [ ] **Step 6: Implement service validation and end-of-day default**

Add constants and a private async validator in `service.py`:

```python
_LOSS_CUT_EXIT_REASONS = frozenset({"stop_loss", "thesis_change"})
_LOSS_CUT_TRIGGER_TYPES = frozenset({"stop_loss", "thesis_change"})
_LOSS_CUT_MAX_AGE = timedelta(hours=72)
```

The validator must collect errors and raise one `OrderProposalError`:

```python
async def _validate_exit_binding(
    self, *, symbol: str, market: str, account_mode: str, side: str,
    order_type: str, exit_intent: str | None, exit_reason: str | None,
    retrospective_id: int | None, approval_issue_id: str | None,
    now: datetime,
) -> None:
    supporting = (exit_reason, retrospective_id, approval_issue_id)
    if exit_intent is None:
        if any(value is not None for value in supporting):
            raise OrderProposalError("exit binding fields require exit_intent")
        return
    if exit_intent != "loss_cut":
        raise OrderProposalError("unknown exit_intent (only 'loss_cut')")

    errors: list[str] = []
    if exit_reason not in _LOSS_CUT_EXIT_REASONS:
        errors.append("loss_cut requires exit_reason in ['stop_loss', 'thesis_change']")
    if retrospective_id is None:
        errors.append("loss_cut requires retrospective_id")
    if not (approval_issue_id or "").strip():
        errors.append("loss_cut requires approval_issue_id")
    if (account_mode, market) not in {
        ("kis_live", "equity_kr"), ("kis_live", "equity_us")
    }:
        errors.append("loss_cut requires a live KIS equity proposal")
    if side != "sell":
        errors.append("loss_cut requires side='sell'")
    if order_type != "limit":
        errors.append("loss_cut requires order_type='limit'")

    retro = None
    if retrospective_id is not None:
        retro = await get_retrospective_by_id(self._session, retrospective_id)
        if retro is None:
            errors.append(f"retrospective_id {retrospective_id} not found")
        else:
            if (retro.symbol or "").strip().upper() != symbol.strip().upper():
                errors.append(f"retrospective_id {retrospective_id} symbol mismatch")
            if retro.trigger_type not in _LOSS_CUT_TRIGGER_TYPES:
                errors.append("retrospective trigger_type is not loss-cut eligible")
            created = retro.created_at
            if created is not None:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                if now.astimezone(UTC) - created.astimezone(UTC) > _LOSS_CUT_MAX_AGE:
                    errors.append(f"retrospective_id {retrospective_id} is stale (> 72h old)")
    if errors:
        raise OrderProposalError("loss_cut proposal invalid: " + "; ".join(errors))
```

Do not import or call `_fetch_approval_issue_status` here. Normalize `now` to a
timezone-aware value, compute the default validity from its KST calendar day,
reject explicit naive/non-future validity, call the validator before insertion,
pass all four values to both the payload hash and `insert_group`, and preserve
the caller's strings unchanged when storing.

- [ ] **Step 7: Run the focused service and payload suites**

```bash
uv run pytest -q tests/services/order_proposals/test_payload.py tests/services/order_proposals/test_service.py
```

Expected: PASS.

- [ ] **Step 8: Commit the create-time binding unit**

```bash
git add app/services/order_proposals/payload.py app/services/order_proposals/service.py tests/services/order_proposals/test_payload.py tests/services/order_proposals/test_service.py
git commit -m "feat(ROB-816): validate and bind loss-cut proposals"
```

### Task 3: Enforce expiry and add safe service-level voiding

**Files:**
- Modify: `app/services/order_proposals/state_machine.py:12-65`
- Modify: `app/services/order_proposals/service.py:183-499`
- Modify: `tests/services/order_proposals/test_state_machine.py`
- Modify: `tests/services/order_proposals/test_service.py`

**Interfaces:**
- Produces: `expire_if_needed(proposal_id, now) -> bool` and `void_proposal(proposal_id, reason, now) -> list[OrderProposalRung]`.
- Guarantees: no expired proposal can reach approval; void refuses ambiguous/post-submit states.

- [ ] **Step 1: Write failing state-machine and service tests**

Add state-machine assertions for legal pre-submit expiry and void transitions:

```python
@pytest.mark.parametrize("state", ["draft", "pending_approval", "revalidating", "needs_reconfirm", "approved"])
def test_pre_submit_states_can_expire(state):
    assert_rung_transition(state, "expired")


@pytest.mark.parametrize("state", ["draft", "pending_approval", "revalidating", "needs_reconfirm", "approved"])
def test_pre_submit_states_can_void(state):
    assert_rung_transition(state, "voided")
```

Add service tests proving:

```python
@pytest.mark.asyncio
async def test_expire_if_needed_terminalizes_pending_rungs_and_nonce(db_session):
    service, group = await _create_single_rung(db_session)
    await service.set_approval_nonce(group.proposal_id, "nonce")
    group.valid_until = datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    assert await service.expire_if_needed(
        group.proposal_id, now=datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    )
    refreshed, rungs = await service.get_proposal(group.proposal_id)
    assert refreshed.lifecycle_state == "expired"
    assert refreshed.approval_nonce is None
    assert [r.state for r in rungs] == ["expired"]


@pytest.mark.asyncio
async def test_void_refuses_unverified_rung_without_partial_mutation(db_session):
    service, group = await _create_single_rung(db_session)
    await _drive_to_submitting(service, group.proposal_id)
    await service.record_unverified(
        group.proposal_id, 0, reason="unknown", now=datetime.now(UTC)
    )
    with pytest.raises(OrderProposalError, match="cannot void"):
        await service.void_proposal(
            group.proposal_id, reason="operator cleanup", now=datetime.now(UTC)
        )
```

Add the success/no-op cases:

```python
@pytest.mark.asyncio
async def test_void_multi_rung_sets_audit_and_invalidates_nonce(db_session):
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930", market="equity_kr", account_mode="kis_live",
        side="buy", order_type="limit", proposer="p",
        rungs=[
            RungInput(0, "buy", Decimal("1"), Decimal("70000"), None),
            RungInput(1, "buy", Decimal("1"), Decimal("69000"), None),
        ],
    )
    await service.set_approval_nonce(group.proposal_id, "nonce")
    rows = await service.void_proposal(
        group.proposal_id, reason="thesis invalidated", now=datetime.now(UTC)
    )
    refreshed, _ = await service.get_proposal(group.proposal_id)
    assert [row.state for row in rows] == ["voided", "voided"]
    assert refreshed.lifecycle_state == "voided"
    assert refreshed.no_resubmit is True
    assert refreshed.void_reason == "thesis invalidated"
    assert refreshed.approval_nonce is None


@pytest.mark.asyncio
async def test_expire_if_needed_before_deadline_is_noop(db_session):
    service, group = await _create_single_rung(db_session)
    group.valid_until = datetime.now(UTC) + timedelta(minutes=1)
    assert not await service.expire_if_needed(group.proposal_id, now=datetime.now(UTC))
```

- [ ] **Step 2: Run the new tests and verify RED**

```bash
uv run pytest -q tests/services/order_proposals/test_state_machine.py tests/services/order_proposals/test_service.py -k 'expire or void'
```

Expected: FAIL because transitions and service methods are missing.

- [ ] **Step 3: Implement the transition and service methods**

Add `expired` to the allowed targets of `pending_approval`, `revalidating`,
`needs_reconfirm`, and `approved`; allow `draft -> expired` if draft proposals
can reach the service method. Update `_recompute_group_state` so
`states == {"expired"}` returns `"expired"`.

Implement `expire_if_needed` with a `for_update=True` group read. It returns
`False` when `valid_until > now`; otherwise it validates that all affected
rungs are pre-submit, transitions eligible rungs to `expired`, clears
`approval_nonce`, and returns `True`.

Implement `void_proposal` in two phases: validate the stripped reason and every
rung state before any mutation, then transition all eligible rungs to `voided`
and update the group with `void_reason`, `no_resubmit=True`,
`approval_nonce=None`, and `approval_nonce_used_at=now`. The allowed set is
exactly:

```python
_VOIDABLE_RUNG_STATES = frozenset(
    {"draft", "pending_approval", "revalidating", "needs_reconfirm", "approved"}
)
```

- [ ] **Step 4: Run the full state/service suite**

```bash
uv run pytest -q tests/services/order_proposals/test_state_machine.py tests/services/order_proposals/test_service.py
```

Expected: PASS.

- [ ] **Step 5: Commit the lifecycle unit**

```bash
git add app/services/order_proposals/state_machine.py app/services/order_proposals/service.py tests/services/order_proposals/test_state_machine.py tests/services/order_proposals/test_service.py
git commit -m "feat(ROB-816): expire and void stale proposals safely"
```

### Task 4: Expose `order_proposal_void` and loss-cut fields through MCP

**Files:**
- Modify: `app/mcp_server/tooling/order_proposal_tools.py:1-257`
- Modify: `tests/test_mcp_order_proposal_tools.py`
- Modify: `tests/test_mcp_profiles.py:500-535`

**Interfaces:**
- Produces: `order_proposal_void(proposal_id: str, reason: str) -> dict[str, Any]`.
- Extends: create/get/list contracts with the four loss-cut fields and `valid_until`.

- [ ] **Step 1: Write failing MCP contract tests**

Extend the registration assertion to include `order_proposal_void`. Create a
loss-cut proposal with a deterministic retrospective lookup and assert get/list
return the exact four fields:

```python
@pytest.mark.asyncio
async def test_loss_cut_binding_round_trips_through_create_get_list(monkeypatch):
    async def fake_lookup(session, retrospective_id):
        return SimpleNamespace(
            symbol="005930", trigger_type="stop_loss", created_at=datetime.now(UTC)
        )
    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    created = await opt.order_proposal_create(
        **_create_kwargs(
            side="sell",
            rungs=[{
                "rung_index": 0, "side": "sell", "quantity": "1",
                "limit_price": "65000", "notional": None,
            }],
            exit_intent="loss_cut", exit_reason="stop_loss",
            retrospective_id=42, approval_issue_id="ROB-800",
        )
    )
    got = await opt.order_proposal_get(created["proposal_id"])
    listed = await opt.order_proposal_list(symbol="005930")
    expected = {
        "exit_intent": "loss_cut", "exit_reason": "stop_loss",
        "retrospective_id": 42, "approval_issue_id": "ROB-800",
    }
    assert {key: got["proposal"][key] for key in expected} == expected
    row = next(p for p in listed["proposals"] if p["proposal_id"] == created["proposal_id"])
    assert {key: row[key] for key in expected} == expected
```

Add the void behavior test:

```python
@pytest.mark.asyncio
async def test_void_requires_reason_and_terminalizes_proposal():
    created = await opt.order_proposal_create(**_create_kwargs())
    blank = await opt.order_proposal_void(created["proposal_id"], "   ")
    assert blank == {"success": False, "error": "void reason is required"}
    result = await opt.order_proposal_void(created["proposal_id"], "superseded thesis")
    assert result["success"] is True
    assert result["lifecycle_state"] == "voided"
    assert result["void_reason"] == "superseded thesis"
```

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest -q tests/test_mcp_order_proposal_tools.py tests/test_mcp_profiles.py -k 'proposal'
```

Expected: FAIL because the void tool and new create arguments are absent.

- [ ] **Step 3: Implement the MCP surface**

Add create parameters:

```python
exit_intent: str | None = None,
exit_reason: str | None = None,
retrospective_id: int | None = None,
approval_issue_id: str | None = None,
```

Forward them unchanged to `create_proposal`. Return them from `_group_dict`.
Return `valid_until` from the create result so the caller immediately sees the
deadline.

Implement the void handler with UUID parsing, required stripped reason, one
service transaction, and `now_kst()`:

```python
async def order_proposal_void(proposal_id: str, reason: str) -> dict[str, Any]:
    try:
        pid = uuid.UUID(proposal_id)
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("void reason is required")
        async with AsyncSessionLocal() as session:
            service = OrderProposalsService(session)
            await service.void_proposal(pid, reason=normalized_reason, now=now_kst())
            group, rungs = await service.get_proposal(pid)
            await session.commit()
            return {
                "success": True,
                "proposal_id": proposal_id,
                "lifecycle_state": group.lifecycle_state,
                "void_reason": group.void_reason,
                "rungs": [_rung_dict(rung) for rung in rungs],
            }
    except (ValueError, OrderProposalError) as exc:
        return {"success": False, "error": str(exc)}
```

Register it in `ORDER_PROPOSAL_TOOL_NAMES`, `register_order_proposal_tools`, and
`__all__`. Existing registry/profile code consumes the exported set and should
need no separate hard-coded tool name.

- [ ] **Step 4: Run the MCP/profile tests**

```bash
uv run pytest -q tests/test_mcp_order_proposal_tools.py tests/test_mcp_profiles.py
```

Expected: PASS.

- [ ] **Step 5: Commit the MCP unit**

```bash
git add app/mcp_server/tooling/order_proposal_tools.py tests/test_mcp_order_proposal_tools.py tests/test_mcp_profiles.py
git commit -m "feat(ROB-816): add operator proposal void tool"
```

### Task 5: Fix revalidation classification and forward loss-cut binding

**Files:**
- Modify: `app/services/order_proposals/revalidation.py:38-322`
- Modify: `tests/services/order_proposals/test_revalidation.py`

**Interfaces:**
- Produces: a pure `_is_guard_blocked_preview(preview) -> bool` classifier.
- Guarantees: both preview and submit receive the stored four loss-cut kwargs unchanged.

- [ ] **Step 1: Write failing classification tests**

Add tests for a recognized insufficient-cash guard and an internal error
envelope:

```python
@pytest.mark.asyncio
async def test_preview_internal_failure_keeps_error_label(db_session):
    service, group = await _create_proposal(db_session)
    async def internal_failure(**kwargs):
        return {
            "success": False,
            "error": "Order preview failed: unsupported operand type(s) for *",
        }
    outcomes = await revalidate_and_submit(
        service=service, proposal_id=group.proposal_id,
        now=datetime.now(UTC), place_order_fn=internal_failure,
    )
    assert outcomes[0].result == "error"


@pytest.mark.asyncio
async def test_preview_insufficient_cash_is_guard_blocked(db_session):
    service, group = await _create_proposal(db_session)
    async def insufficient_cash(**kwargs):
        return {
            "success": False,
            "error": "Insufficient KRW balance",
            "insufficient_balance": True,
        }
    outcomes = await revalidate_and_submit(
        service=service, proposal_id=group.proposal_id,
        now=datetime.now(UTC), place_order_fn=insufficient_cash,
    )
    assert outcomes[0].result == "guard_blocked"
```

Add a loss-cut propagation test whose fake records both dry-run and submit
kwargs and returns a valid preview/accepted response. Assert the four exact
values are present in both calls.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest -q tests/services/order_proposals/test_revalidation.py -k 'internal_failure or insufficient_cash or loss_cut'
```

Expected: internal failure is incorrectly `guard_blocked`, and loss-cut kwargs
are missing.

- [ ] **Step 3: Implement the classifier and propagation**

Use explicit structured fields first, then known guard messages from the shared
order path:

```python
_GUARD_ERROR_CODES = frozenset({
    "loss_cut_preconditions_failed", "nxt_session_not_tradable",
})
_GUARD_ERROR_MARKERS = (
    "loss_sell_blocked", "sell price ", "live market sell blocked",
    "loss_cut sell price", "no holdings found", "insufficient ",
    "stop-loss cooldown", "opposite pending order",
)


def _is_guard_blocked_preview(preview: dict[str, Any]) -> bool:
    if preview.get("success") is not False:
        return False
    if preview.get("insufficient_balance") is True or preview.get("violations"):
        return True
    error_code = str(preview.get("error_code") or "").lower()
    error = str(preview.get("error") or "").lower()
    return error_code in _GUARD_ERROR_CODES or any(
        marker in error for marker in _GUARD_ERROR_MARKERS
    )
```

When `success is False`, always return to `pending_approval`; select
`guard_blocked` only when the helper returns true, otherwise select `error`.

Add these kwargs unchanged to both `place_order_fn` calls:

```python
exit_intent=group.exit_intent,
exit_reason=group.exit_reason,
retrospective_id=group.retrospective_id,
approval_issue_id=group.approval_issue_id,
```

The default function continues to convert only `Decimal` values to float; it
must not normalize or rewrite any of the four binding values.

- [ ] **Step 4: Run the revalidation suite**

```bash
uv run pytest -q tests/services/order_proposals/test_revalidation.py
```

Expected: PASS.

- [ ] **Step 5: Commit the revalidation unit**

```bash
git add app/services/order_proposals/revalidation.py tests/services/order_proposals/test_revalidation.py
git commit -m "fix(ROB-816): distinguish guards and bind loss-cut submit"
```

### Task 6: Show expiry, loss-cut evidence, and rejection reasons in Telegram

**Files:**
- Modify: `app/services/order_proposals/approval_message.py:63-169`
- Modify: `app/services/order_proposals/telegram_callback.py:79-383`
- Modify: `tests/services/order_proposals/test_approval_message.py`
- Modify: `tests/services/order_proposals/test_telegram_callback.py`

**Interfaces:**
- Consumes: `expire_if_needed`, `RungOutcome.detail.error`, and group loss-cut columns.
- Produces: actionable Telegram approval/result text with secrets still redacted.

- [ ] **Step 1: Write failing message tests**

Add an approval-message assertion:

```python
def test_loss_cut_approval_message_shows_reason_and_retrospective():
    group = _group(
        exit_intent="loss_cut", exit_reason="stop_loss", retrospective_id=42,
        approval_issue_id="ROB-800",
    )
    text, _keyboard = build_approval_message(group=group, rungs=[_rung()])
    assert "손절 근거" in text
    assert "stop\_loss" in text
    assert "#42" in text
```

Add result-summary and callback expiry tests:

```python
def test_result_summary_includes_bounded_escaped_guard_reason():
    outcome = RungOutcome(
        0, "guard_blocked", {"error": "cash *blocked* " + ("x" * 400)}
    )
    summary = _build_result_summary([outcome])
    assert "cash \\*blocked\\*" in summary
    assert summary.endswith("…")
    assert len(summary) < 320


@pytest.mark.asyncio
async def test_expired_approve_never_revalidates(monkeypatch, db_session):
    _allow_chat(monkeypatch)
    group = await _seed_proposal(db_session, nonce="expired-nonce")
    group.valid_until = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    called = False
    async def must_not_revalidate(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("expired proposal must not revalidate")
    notifier = _FakeNotifier()
    result = await handle_callback_update(
        _make_update(data=f"op:{str(group.proposal_id)[:8]}:expired-nonce"),
        now=datetime.now(UTC), service_factory=_session_factory(db_session),
        notifier=notifier, revalidate_fn=must_not_revalidate,
    )
    assert result["reason"] == "proposal_expired"
    assert called is False
    assert notifier.answered[-1] == ("cbq-1", "제안이 만료되었습니다")
    assert "만료" in notifier.edited[-1][2]
```

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest -q tests/services/order_proposals/test_approval_message.py tests/services/order_proposals/test_telegram_callback.py -k 'loss_cut or reason or expired'
```

Expected: FAIL because the fields/reasons and expiry branch are not rendered.

- [ ] **Step 3: Render loss-cut evidence and bounded reasons**

In `build_approval_message`, when `group.exit_intent == "loss_cut"`, append:

```python
lines.extend([
    "",
    "*손절 근거*",
    f"- 사유: {_escape_markdown(group.exit_reason)}",
    f"- 회고: #{group.retrospective_id}",
])
```

Do not render approval tokens or hashes. Keep `approval_issue_id` stored/audited
but omit it from the operator message because the required visible evidence is
the structured reason and retrospective.

Add a pure helper in `telegram_callback.py`:

```python
def _outcome_error_summary(outcome: RungOutcome, *, limit: int = 240) -> str | None:
    error = str((outcome.detail or {}).get("error") or "").strip()
    if not error:
        return None
    compact = " ".join(error.split())
    if len(compact) > limit:
        compact = compact[: limit - 1] + "…"
    return _escape_markdown(compact)
```

Expose or locally implement the same Markdown escaping contract used by the
approval message. Append ` — {reason}` to `guard_blocked` and `error` summary
lines.

- [ ] **Step 4: Enforce expiry before nonce consumption and approval**

At the start of `_handle_approve`, call `expire_if_needed` before
`consume_approval_nonce`. When true, commit, edit the message to `⌛ 제안 만료`,
answer the callback with `제안이 만료되었습니다`, and return:

```python
{
    "handled": False,
    "reason": "proposal_expired",
    "proposal_id": str(proposal_id),
}
```

This branch must not acquire a lease, record approval, mint a nonce, or invoke
`revalidate_fn`.

- [ ] **Step 5: Run Telegram tests**

```bash
uv run pytest -q tests/services/order_proposals/test_approval_message.py tests/services/order_proposals/test_telegram_callback.py
```

Expected: PASS with broker and Telegram fakes only.

- [ ] **Step 6: Commit the operator UX unit**

```bash
git add app/services/order_proposals/approval_message.py app/services/order_proposals/telegram_callback.py tests/services/order_proposals/test_approval_message.py tests/services/order_proposals/test_telegram_callback.py
git commit -m "fix(ROB-816): surface approval expiry and rejection reasons"
```

### Task 7: Synchronize documentation and verify PR-3a

**Files:**
- Modify: `app/mcp_server/README.md`
- Modify: `docs/runbooks/order-proposals.md`

**Interfaces:**
- Documents: create/void contracts, default KST validity, expiry behavior, four loss-cut columns, and click-time Paperclip validation.
- Produces: a clean PR-3a branch ready for review and shipping.

- [ ] **Step 1: Update MCP and operator documentation**

Document these exact rules:

- `order_proposal_create` defaults `valid_until` to the next `00:00 KST` and
  accepts the four nullable loss-cut fields.
- `exit_intent="loss_cut"` requires `exit_reason`, `retrospective_id`, and
  `approval_issue_id`; retrospective checks happen at create.
- Paperclip `done` and all ROB-800 final guards happen on Telegram click and
  again on submit.
- `order_proposal_void` requires a reason and refuses any possible outstanding
  broker state.
- Telegram result messages include a bounded rejection/guard reason.

- [ ] **Step 2: Run focused PR-3a tests**

```bash
uv run pytest -q \
  tests/services/order_proposals \
  tests/test_mcp_order_proposal_tools.py \
  tests/test_mcp_profiles.py
```

Expected: all selected tests PASS.

- [ ] **Step 3: Run the project lint contract**

```bash
make lint
```

Expected: Ruff format/check and ty complete with exit code 0.

- [ ] **Step 4: Run migration and diff integrity checks**

```bash
uv run alembic heads
git diff --check main...HEAD
git status --short
```

Expected: exactly one Alembic head, no whitespace errors, and only intentional
tracked changes.

- [ ] **Step 5: Commit documentation**

```bash
git add app/mcp_server/README.md docs/runbooks/order-proposals.md
git commit -m "docs(ROB-816): document proposal approval safety"
```

- [ ] **Step 6: Request code review and address findings**

Use `superpowers:requesting-code-review` against `main...HEAD`. Fix every
Critical and Important finding, rerun the affected RED/GREEN test plus the full
focused suite, and commit each correction with a scoped message.

- [ ] **Step 7: Run fresh pre-PR verification**

```bash
uv run pytest -q \
  tests/services/order_proposals \
  tests/test_mcp_order_proposal_tools.py \
  tests/test_mcp_profiles.py
make lint
git diff --check main...HEAD
```

Expected: all tests pass, lint exits 0, and diff check is clean.

- [ ] **Step 8: Open PR-3a without merging**

Use the `ship` workflow to push and create a GitHub PR targeting `main`. The PR
body must state that broker/Telegram calls were mocked, list the verification
commands, explain that Paperclip status is click-time only, and note that all
four fields reach `_place_order_impl` unchanged. Do not merge the PR.
