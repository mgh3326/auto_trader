# ROB-837 Upbit Proposal Submit Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make proposal-driven Upbit order creation single-send and converge submit errors to broker evidence instead of falsely recording live orders as rejected.

**Architecture:** Disable every retry for `POST /v1/orders`, bind a stable proposal/rung identifier through preview and submit, and query Upbit by that identifier before classifying a failed submit. Add a dry-run-first, incident-specific repair CLI that reads the existing order and repairs only the measured rejected ledger row without submitting, cancelling, or replacing an order.

**Tech Stack:** Python 3.13, asyncio, httpx, SQLAlchemy async ORM, pytest/pytest-asyncio, Ruff, ty, uv.

## Global Constraints

- Base commit is `047a88e73729ad53d4a03ee30408936b9a5764f3` (`main`); preserve unrelated user changes if the worktree later becomes dirty.
- No live orders, cancels, replaces, or broker mutations in implementation or verification; all HTTP and broker calls in tests are mocks.
- Upbit `POST /v1/orders` is sent at most once, including HTTP 429 and `httpx.RequestError` outcomes.
- The proposal/rung identifier is stable across preview, submit, and broker lookup; a retry must never mint a new identifier.
- `rejected` is recorded only after definitive evidence that no Upbit order exists; lookup ambiguity records `unverified` (Principle #4).
- ROB-825 attempt/generation semantics and ROB-835 replace-cancel polling remain out of scope.
- No Alembic migration and no public MCP tool parameter change.
- The one-time repair keeps live order `35bee07f…` in place and must contain no resubmit path.
- Run `make lint` clean; do not merge the PR.

---

### Task 1: Make Upbit order creation a one-shot transport operation

**Files:**
- Modify: `app/services/brokers/upbit/client.py:838-903`
- Test: `tests/test_upbit_retry.py`

**Interfaces:**
- Consumes: `_retry_with_backoff(..., max_retries, retry_request_errors)`.
- Produces: `_request_with_auth("POST", ".../v1/orders", ...)` calls `_retry_with_backoff(..., max_retries=0, retry_request_errors=False)`; other methods retain existing retry defaults.

- [ ] **Step 1: Write the failing transport regression test**

Add a test that executes the real retry helper with the order-path retry budget, returns a mocked 429, and proves no second send occurs:

```python
@pytest.mark.asyncio
async def test_order_post_429_is_not_retried(monkeypatch):
    from app.services.brokers.upbit.client import _retry_with_backoff

    response = _make_response(429)
    send = AsyncMock(return_value=response)
    sleep = AsyncMock()
    monkeypatch.setattr("app.services.brokers.upbit.client.asyncio.sleep", sleep)

    with pytest.raises(RateLimitExceededError):
        await _retry_with_backoff(
            _make_limiter(),
            send,
            url="https://api.upbit.com/v1/orders",
            max_retries=0,
            retry_request_errors=False,
        )

    assert send.await_count == 1
```

Keep and extend `test_order_post_disables_request_error_retry` to assert both flags:

```python
assert captured["retry_request_errors"] is False
assert captured["max_retries"] == 0
```

- [ ] **Step 2: Run RED tests**

Run:

```bash
uv run pytest tests/test_upbit_retry.py::test_order_post_429_is_not_retried tests/test_upbit_retry.py::test_order_post_disables_request_error_retry -v -p no:cacheprovider
```

Expected: the one-shot helper test already demonstrates the desired zero-retry budget, while the `_request_with_auth` flag test fails because `max_retries` is absent. This is the RED proof that the order path has not yet selected the one-shot budget.

- [ ] **Step 3: Apply the minimal single-send change**

In `_request_with_auth`, preserve the existing ROB-645 predicate and pass zero retries only for order creation:

```python
    is_order_submission = method.upper() == "POST" and api_path.rstrip("/").endswith(
        "/orders"
    )
    return await _retry_with_backoff(
        limiter,
        send,
        url=url,
        max_retries=0 if is_order_submission else None,
        retry_request_errors=not is_order_submission,
    )
```

Update the adjacent comment to state that both transport errors and 429 responses are non-retryable for creation requests.

- [ ] **Step 4: Run GREEN transport tests**

Run:

```bash
uv run pytest tests/test_upbit_retry.py -v -p no:cacheprovider
```

Expected: PASS; GET retry tests remain green and each order POST test observes one send.

- [ ] **Step 5: Commit the transport fix**

```bash
git add app/services/brokers/upbit/client.py tests/test_upbit_retry.py
git commit -m "fix(ROB-837): make Upbit order POST single-send"
```

---

### Task 2: Thread a proposal-scoped identifier through preview and live submit

**Files:**
- Modify: `app/services/order_proposals/revalidation.py:251-330,347-570`
- Modify: `app/mcp_server/tooling/order_execution.py:1187-1230,1425-1450`
- Test: `tests/services/order_proposals/test_revalidation.py`
- Test: `tests/test_mcp_place_order.py`

**Interfaces:**
- Produces: `_proposal_client_order_id(proposal_id: UUID, rung_index: int) -> str`.
- Produces: `_place_order_impl(..., client_order_id: str | None = None) -> dict[str, Any]`.
- Consumes: `_default_place_order_fn(proposal_client_order_id=...)`; the Upbit `identifier` is the override, while callers without an override keep ROB-653 canonical derivation.

- [ ] **Step 1: Write failing proposal-ID tests**

Add unit coverage beside the existing Toss ID tests:

```python
def test_upbit_proposal_client_ids_are_stable_and_rung_scoped():
    from app.services.order_proposals import revalidation as mod

    proposal_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    first = mod._proposal_client_order_id(proposal_id, 0)
    assert first == mod._proposal_client_order_id(proposal_id, 0)
    assert first != mod._proposal_client_order_id(proposal_id, 1)
    assert first != mod._proposal_client_order_id(uuid.uuid4(), 0)
    assert first.startswith("oprop-")
    assert len(first) <= 40
```

Add an async binding test that patches `_place_order_impl`, invokes `_default_place_order_fn` once for preview and once for live submit, and asserts both calls receive the same `client_order_id`:

```python
assert preview_seen["client_order_id"] == expected
assert submit_seen["client_order_id"] == expected
```

Add `_place_order_impl` coverage that patches `_execute_and_record` and asserts its `idempotency_key` equals an explicit `client_order_id="oprop-fixed"`, while an existing no-override test continues to assert the ROB-653-derived key.

- [ ] **Step 2: Run RED identifier tests**

Run:

```bash
uv run pytest tests/services/order_proposals/test_revalidation.py -k 'proposal_client_id or default_place_order' -v -p no:cacheprovider
uv run pytest tests/test_mcp_place_order.py -k 'client_order_id_override' -v -p no:cacheprovider
```

Expected: FAIL because `_proposal_client_order_id` and `_place_order_impl(client_order_id=...)` do not exist and the default binding currently discards non-Toss proposal IDs.

- [ ] **Step 3: Implement the stable ID and override**

In `revalidation.py`, derive a compact 128-bit identifier:

```python
def _proposal_client_order_id(proposal_id: uuid.UUID, rung_index: int) -> str:
    digest = hashlib.sha256(f"{proposal_id}:{rung_index}".encode()).hexdigest()[:32]
    return f"oprop-{digest}"
```

In `_revalidate_place_rung`, compute it only for Upbit proposals and pass it to both preview and submit:

```python
    proposal_client_order_id = (
        _toss_proposal_client_order_id(proposal_id, rung_index)
        if group.account_mode == "toss_live"
        else _proposal_client_order_id(proposal_id, rung_index)
        if group.account_mode == "upbit"
        else None
    )
```

Replace both Toss-only conditional kwargs blocks with the same generic `proposal_client_order_id` condition. In `_default_place_order_fn`, keep the Toss context behavior and pass the value to the shared implementation:

```python
    if proposal_client_order_id is not None:
        kwargs["client_order_id"] = str(proposal_client_order_id)
    submit = await _place_order_impl(**kwargs)
```

In `_place_order_impl`, add the trailing optional parameter and select the override after building the canonical payload:

```python
    idempotency_key = client_order_id or order_approval.derive_client_order_id(
        canonical, market=salt_market, now=now, rung=rung
    )
```

Validate a supplied value as non-blank and at most 40 characters before any broker mutation; return `_order_error` on invalid input.

- [ ] **Step 4: Run GREEN identifier tests and adjacent Upbit execution tests**

Run:

```bash
uv run pytest tests/services/order_proposals/test_revalidation.py tests/test_rob653_upbit_identifier.py tests/test_mcp_place_order.py -k 'proposal or identifier or client_order_id' -v -p no:cacheprovider
```

Expected: PASS; direct non-proposal callers still derive ROB-653 keys and proposal preview/submit use one stable value.

- [ ] **Step 5: Commit identifier binding**

```bash
git add app/services/order_proposals/revalidation.py app/mcp_server/tooling/order_execution.py tests/services/order_proposals/test_revalidation.py tests/test_mcp_place_order.py
git commit -m "fix(ROB-837): bind proposal ID to Upbit identifier"
```

---

### Task 3: Add read-only Upbit lookup by identifier and normalized evidence

**Files:**
- Modify: `app/services/brokers/upbit/orders.py:279-291`
- Modify: `app/services/brokers/upbit/__init__.py`
- Modify: `app/services/brokers/upbit/client.py:905-920` (lazy re-export list)
- Modify: `app/services/order_proposals/broker_gateway.py`
- Test: `tests/test_upbit_orders.py`
- Test: `tests/services/order_proposals/test_broker_gateway.py`

**Interfaces:**
- Produces: `fetch_order_by_identifier(identifier: str) -> dict[str, Any]`, issuing only `GET /v1/order?identifier=...`.
- Produces: `SubmitEvidence(outcome: Literal["found", "absent", "unknown"], broker_order_id: str | None, broker_state: str | None, reason: str | None)`.
- Produces: `fetch_submit_evidence(*, identifier, account_mode, market, lookup_fn=None) -> SubmitEvidence`.

- [ ] **Step 1: Write failing broker lookup tests**

Add to `tests/test_upbit_orders.py`:

```python
@pytest.mark.asyncio
async def test_fetch_order_by_identifier_uses_read_only_get(monkeypatch):
    request = AsyncMock(return_value={"uuid": "35bee07f-full", "state": "wait"})
    monkeypatch.setattr(orders._client, "_request_with_auth", request)

    result = await orders.fetch_order_by_identifier("oprop-fixed")

    assert result["uuid"] == "35bee07f-full"
    request.assert_awaited_once_with(
        "GET",
        f"{orders._client.UPBIT_REST}/order",
        query_params={"identifier": "oprop-fixed"},
    )
```

Add gateway tests for found, definitive 404, and lookup failure:

```python
found = await fetch_submit_evidence(
    identifier="oprop-fixed",
    account_mode="upbit",
    market="crypto",
    lookup_fn=AsyncMock(return_value={"uuid": "35bee07f-full", "state": "wait"}),
)
assert found == SubmitEvidence("found", "35bee07f-full", "wait", None)
```

Build an `httpx.HTTPStatusError` with status 404 and assert outcome `absent`. Build a 403 and `httpx.ReadTimeout` and assert outcome `unknown`. Also assert blank UUID/state responses are `unknown`, never `absent`.

- [ ] **Step 2: Run RED lookup tests**

Run:

```bash
uv run pytest tests/test_upbit_orders.py::test_fetch_order_by_identifier_uses_read_only_get tests/services/order_proposals/test_broker_gateway.py -k 'submit_evidence' -v -p no:cacheprovider
```

Expected: FAIL because both lookup functions and `SubmitEvidence` are missing.

- [ ] **Step 3: Implement read-only evidence lookup**

In `orders.py`:

```python
async def fetch_order_by_identifier(identifier: str) -> dict[str, Any]:
    candidate = str(identifier or "").strip()
    if not candidate:
        raise ValueError("identifier is required")
    return await _client._request_with_auth(
        "GET",
        f"{_client.UPBIT_REST}/order",
        query_params={"identifier": candidate},
    )
```

Re-export it through `upbit/__init__.py` and `client.__getattr__`. In `broker_gateway.py`, add the frozen dataclass and classify only a 404 as definitive absence:

```python
@dataclass(frozen=True)
class SubmitEvidence:
    outcome: Literal["found", "absent", "unknown"]
    broker_order_id: str | None = None
    broker_state: str | None = None
    reason: str | None = None
```

`fetch_submit_evidence` must return `unknown` for unsupported account/market tuples, import the Upbit lookup lazily, validate non-empty `uuid` and `state`, catch `httpx.HTTPStatusError` with 404 as `absent`, and convert every other exception to `unknown` with `describe_exception`.

- [ ] **Step 4: Run GREEN lookup tests**

Run:

```bash
uv run pytest tests/test_upbit_orders.py tests/services/order_proposals/test_broker_gateway.py -v -p no:cacheprovider
```

Expected: PASS; all calls are GET-only and only 404 proves absence.

- [ ] **Step 5: Commit evidence lookup**

```bash
git add app/services/brokers/upbit/orders.py app/services/brokers/upbit/__init__.py app/services/brokers/upbit/client.py app/services/order_proposals/broker_gateway.py tests/test_upbit_orders.py tests/services/order_proposals/test_broker_gateway.py
git commit -m "feat(ROB-837): query Upbit submit evidence by identifier"
```

---

### Task 4: Gate proposal rejection on broker evidence

**Files:**
- Modify: `app/services/order_proposals/revalidation.py:347-570,926-989`
- Test: `tests/services/order_proposals/test_revalidation.py`

**Interfaces:**
- Consumes: `fetch_submit_evidence` and `SubmitEvidence` from Task 3.
- Produces: `revalidate_and_submit(..., fetch_submit_evidence_fn=fetch_submit_evidence)`.
- Produces: `_classify_submit(..., account_mode, identifier, fetch_submit_evidence_fn)` with evidence-based Upbit failure handling.

- [ ] **Step 1: Write the three required failing regression tests**

Create an Upbit proposal fixture (`market="crypto"`, `account_mode="upbit"`, `symbol="KRW-BTC"`) and a placement fake that records live calls.

Test accepted-first/retry-400 convergence by returning `success=False` from the single live call and returning found evidence from the lookup:

```python
assert live_calls == ["oprop-expected"]
assert outcomes[0].result == "submitted_resting"
_, rungs = await service.get_proposal(group.proposal_id)
assert rungs[0].state == "resting"
assert rungs[0].broker_order_id == "35bee07f-full"
assert rungs[0].idempotency_key == "oprop-expected"
```

Test true rejection with `{"success": False, "error": "insufficient balance"}` and `SubmitEvidence(outcome="absent")`; assert rung `rejected` and one live placement call.

Test evidence lookup failure with `SubmitEvidence(outcome="unknown", reason="timeout")`; assert rung `unverified`, identifier/correlation retained, and not rejected.

Add an exception-form test where the live placement fake raises `httpx.ReadTimeout` and found evidence still converges to resting. This ensures the exception branch uses the same gate instead of immediately parking or rejecting.

- [ ] **Step 2: Run RED classification tests**

Run:

```bash
uv run pytest tests/services/order_proposals/test_revalidation.py -k 'submit_failure_found or true_rejection_absent or evidence_unknown or submit_exception_found' -v -p no:cacheprovider
```

Expected: FAIL because failed responses are immediately rejected and exceptions are immediately unverified without broker lookup.

- [ ] **Step 3: Implement the evidence gate**

Thread `fetch_submit_evidence_fn` from `revalidate_and_submit` into place and replace submit classification. For Upbit failures, call it exactly once with the proposal identifier.

When evidence is found, normalize Upbit states as follows:

```python
status = "resting" if evidence.broker_state in {"wait", "watch"} else "acked"
```

Record the found UUID, correlation ID, proposal identifier, and preview approval digest using `record_resting` or `record_ack`; return `submitted_resting` or `submitted_acked`.

When evidence is absent, call `record_rejected` with the original broker error. When evidence is unknown, call:

```python
await service.record_unverified(
    proposal_id,
    rung_index,
    reason=f"submit_evidence_unknown:{evidence.reason or original_error}",
    now=now,
    correlation_id=corr,
    idempotency_key=identifier,
)
```

Convert a caught submit exception into the same classification input (`success=False`, original error detail) for Upbit. Preserve existing KIS/Toss behavior: definitive `success=False` remains rejected and non-Upbit exceptions remain unverified.

- [ ] **Step 4: Run GREEN proposal suites**

Run:

```bash
uv run pytest tests/services/order_proposals/test_revalidation.py tests/services/order_proposals/test_telegram_callback.py -v -p no:cacheprovider
```

Expected: PASS; the new tests observe one placement call, found evidence binds `35bee07f-full`, true rejection stays rejected, and ambiguity stays unverified.

- [ ] **Step 5: Commit classification gate**

```bash
git add app/services/order_proposals/revalidation.py tests/services/order_proposals/test_revalidation.py
git commit -m "fix(ROB-837): converge proposal submit errors from evidence"
```

---

### Task 5: Add the guarded one-time ledger repair CLI

**Files:**
- Create: `scripts/rob837_reconcile_upbit_proposal.py`
- Create: `tests/scripts/test_rob837_reconcile_upbit_proposal.py`

**Interfaces:**
- Produces: `repair_incident(session, *, proposal_id, rung_index, broker_order_id, commit, fetch_order_fn=fetch_order_detail) -> dict[str, Any]`.
- Produces CLI: `uv run python -m scripts.rob837_reconcile_upbit_proposal --proposal-id <full-uuid-starting-b81ffd0e> --broker-order-id <full-uuid-starting-35bee07f> [--rung-index 0] [--commit]`.
- The module imports only the read-side `fetch_order_detail`; it must not import any place/cancel/replace function.

- [ ] **Step 1: Write failing dry-run, guard, and commit tests**

Monkeypatch `app.services.order_proposals.service.uuid.uuid4` to return `UUID("b81ffd0e-0000-4000-8000-000000000000")`, seed a rejected Upbit BTC proposal through the service, then cover:

```python
result = await repair_incident(
    db_session,
    proposal_id=group.proposal_id,
    rung_index=0,
    broker_order_id="35bee07f-full",
    commit=False,
    fetch_order_fn=AsyncMock(return_value={
        "uuid": "35bee07f-full",
        "identifier": "oprop-fixed",
        "market": "KRW-BTC",
        "state": "wait",
        "side": "bid",
        "ord_type": "limit",
        "price": "88800000",
        "volume": "0.0004",
    }),
)
assert result["mode"] == "dry-run"
assert result["after"]["state"] == "resting"
```

Expire the session and reload to assert dry-run left the rung rejected. Repeat with `commit=True` and assert rung `resting`, `broker_order_id`, `idempotency_key`, cleared `void_reason`, and group lifecycle `submitted`.

Parametrize failures for proposal prefix mismatch, broker prefix mismatch, non-`wait` broker state, symbol/side/type/price/volume mismatch, rung not rejected, duplicate broker ID already bound, and broker lookup exception. Every case must leave the DB unchanged.

Add a static safety assertion:

```python
source = Path("scripts/rob837_reconcile_upbit_proposal.py").read_text()
for forbidden in ("place_order", "cancel_order", "replace_order", "_execute_order"):
    assert forbidden not in source
```

- [ ] **Step 2: Run RED repair tests**

Run:

```bash
uv run pytest tests/scripts/test_rob837_reconcile_upbit_proposal.py -v -p no:cacheprovider
```

Expected: ERROR because the script module does not exist.

- [ ] **Step 3: Implement the fail-closed repair**

Use `argparse`, `AsyncSessionLocal`, `select(...).with_for_update()`, and direct ORM field updates. Validate:

```python
if proposal_id.hex[:8] != "b81ffd0e":
    raise ValueError("proposal id is not the ROB-837 incident")
if not broker_order_id.startswith("35bee07f"):
    raise ValueError("broker order id is not the ROB-837 incident")
```

Require broker evidence `uuid == broker_order_id`, `market == "KRW-BTC"`, `state == "wait"`, `side == "bid"`, `ord_type == "limit"`, non-empty `identifier`, and exact decimal equality between broker `price`/`volume` and rung `limit_price`/`quantity`.

Lock the group and target rung, require `account_mode="upbit"`, `market="crypto"`, target state `rejected`, and no other rung with that broker ID. Build a JSON-serializable before/after diff. If `commit=False`, return without mutating. If `commit=True`, apply exactly:

```python
rung.state = "resting"
rung.broker_order_id = broker_order_id
rung.idempotency_key = broker_order["identifier"]
rung.void_reason = None
rung.validated_at = now
rung.updated_at = now
group.lifecycle_state = "submitted"
group.updated_at = now
await session.commit()
```

On every exception, roll back. The CLI is dry-run by default; only explicit `--commit` mutates the ledger. Print the evidence and diff as sorted JSON.

- [ ] **Step 4: Run GREEN repair tests and inspect CLI help**

Run:

```bash
uv run pytest tests/scripts/test_rob837_reconcile_upbit_proposal.py -v -p no:cacheprovider
uv run python -m scripts.rob837_reconcile_upbit_proposal --help
```

Expected: PASS; help states dry-run default, explicit `--commit`, and “never resubmits/cancels the live order.”

- [ ] **Step 5: Commit the repair procedure**

```bash
git add scripts/rob837_reconcile_upbit_proposal.py tests/scripts/test_rob837_reconcile_upbit_proposal.py
git commit -m "ops(ROB-837): add guarded proposal ledger repair"
```

---

### Task 6: Run complete verification and prepare the unmerged PR

**Files:**
- Modify only if verification exposes a ROB-837 regression: files already listed in Tasks 1-5.
- Verify: `app/mcp_server/README.md` needs no change because no public MCP parameter or response field changes.

**Interfaces:**
- Consumes all prior task deliverables.
- Produces a clean branch, verification evidence, and a PR linked to ROB-837; no merge.

- [ ] **Step 1: Run focused regression suites**

```bash
uv run pytest tests/test_upbit_retry.py tests/test_upbit_orders.py tests/test_rob653_upbit_identifier.py tests/services/order_proposals/test_broker_gateway.py tests/services/order_proposals/test_revalidation.py tests/services/order_proposals/test_telegram_callback.py tests/scripts/test_rob837_reconcile_upbit_proposal.py -v -p no:cacheprovider
```

Expected: PASS with no real-network access.

- [ ] **Step 2: Run the MCP placement regression suite**

```bash
uv run pytest tests/test_mcp_place_order.py -v -p no:cacheprovider
```

Expected: PASS; direct KIS/Upbit placement contracts remain compatible.

- [ ] **Step 3: Run lint and type gates**

```bash
make lint
uv run ty check app/ scripts/rob837_reconcile_upbit_proposal.py
```

Expected: both exit 0 and Ruff formatting is clean.

- [ ] **Step 4: Review the final diff and safety invariants**

```bash
git diff 047a88e73729ad53d4a03ee30408936b9a5764f3...HEAD --check
git diff 047a88e73729ad53d4a03ee30408936b9a5764f3...HEAD --stat
rg -n "place_order|cancel_order|replace_order|_execute_order" scripts/rob837_reconcile_upbit_proposal.py
git status --short --branch
```

Expected: `diff --check` is empty; the script search has no matches; worktree is clean. Review must explicitly confirm: one Upbit POST, same identifier, found→resting, absent→rejected, unknown→unverified, and repair performs no broker mutation.

- [ ] **Step 5: Push and create the PR without merging**

Use the repository's `ship` workflow to push branch `rob-837` and create a PR whose title is:

```text
fix(ROB-837): converge Upbit proposal submits from broker evidence
```

The PR body must include the root cause (429 retry survived ROB-645), the same-identifier finding, the three classification outcomes, mocked test evidence, exact post-deploy dry-run/commit repair commands with full IDs supplied by the operator, and “Do not merge automatically.” Report the PR number and URL; do not invoke any merge command.
