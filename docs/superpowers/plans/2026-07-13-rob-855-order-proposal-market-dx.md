# ROB-855 Order Proposal Market DX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept `kr`/`us` aliases at the order-proposal MCP boundary while preserving canonical validation, hashes, storage, and responses, and expose actionable proposal and retrospective enums.

**Architecture:** A private helper in `order_proposal_tools.py` canonicalizes market before any preflight or service call. The service continues to validate canonical tuples and computes its existing payload hash unchanged; retrospective validation reuses its existing outcome set when formatting errors.

**Tech Stack:** Python 3.13+, FastMCP handlers, SQLAlchemy async sessions, pytest/pytest-asyncio, Ruff, ty, `uv`.

## Global Constraints

- Normalize aliases before target-order preflight, service validation, payload hashing, and persistence.
- Persist and return only `equity_kr`, `equity_us`, or `crypto` for supported proposals.
- Do not add market parameters to `order_proposal_get` or `order_proposal_list`; neither currently has one.
- Do not modify `app/services/order_proposals/revalidation.py` or Toss submit-path helpers.
- Do not modify `no_resolvable_forecast` auto-close behavior.
- Do not add a database migration; existing valid order-proposal rows are canonical.
- Do not make real broker calls in tests.
- Every commit includes `Co-Authored-By: Paperclip <noreply@paperclip.ing>`.

---

### Task 1: Canonicalize order-proposal markets before hashing and storage

**Files:**
- Modify: `tests/test_mcp_order_proposal_tools.py`
- Modify: `tests/services/order_proposals/test_service.py`
- Modify: `app/mcp_server/tooling/order_proposal_tools.py`
- Modify: `app/services/order_proposals/service.py`
- Modify: `app/mcp_server/README.md`

**Interfaces:**
- Consumes: `order_proposal_create(..., market: str, ...) -> dict[str, Any]`.
- Produces: `_normalize_order_proposal_market(market: str) -> str`, canonical arguments for `fetch_target_order` and `create_proposal`, and actionable contract errors.

- [ ] **Step 1: Write failing alias, persistence, hash, error, and docstring tests**

Add to `tests/test_mcp_order_proposal_tools.py`:

```python
import uuid


@pytest.mark.asyncio
async def test_create_normalizes_kr_alias_before_storage_and_payload_hash():
    alias = await opt.order_proposal_create(**_create_kwargs(market="kr"))
    canonical = await opt.order_proposal_create(**_create_kwargs(market="equity_kr"))

    assert alias["success"] is True
    assert canonical["success"] is True
    async with opt.AsyncSessionLocal() as session:
        service = opt.OrderProposalsService(session)
        alias_group, _ = await service.get_proposal(uuid.UUID(alias["proposal_id"]))
        canonical_group, _ = await service.get_proposal(
            uuid.UUID(canonical["proposal_id"])
        )

    assert alias_group.market == "equity_kr"
    assert canonical_group.market == "equity_kr"
    assert alias_group.payload_hash == canonical_group.payload_hash


@pytest.mark.asyncio
async def test_create_rejects_unknown_market_with_allowed_contract_guidance():
    result = await opt.order_proposal_create(
        **_create_kwargs(market="jp", account_mode="toss_live")
    )

    assert result["success"] is False
    assert "allowed: kis_live×equity_kr|equity_us" in result["error"]
    assert "toss_live×equity_kr|equity_us" in result["error"]
    assert "upbit×crypto" in result["error"]
    assert "market aliases kr→equity_kr, us→equity_us" in result["error"]


def test_create_docstring_documents_markets_aliases_and_account_modes():
    doc = opt.order_proposal_create.__doc__ or ""
    for value in (
        "equity_kr", "equity_us", "crypto", "kr", "us",
        "kis_live", "toss_live", "upbit",
    ):
        assert value in doc
```

Also extend an unsupported-tuple assertion in
`tests/services/order_proposals/test_service.py` to require the complete
allowed-combination and alias suffix for direct service callers.

- [ ] **Step 2: Run the new proposal tests and verify RED**

Run:

```bash
uv run pytest \
  tests/test_mcp_order_proposal_tools.py::test_create_normalizes_kr_alias_before_storage_and_payload_hash \
  tests/test_mcp_order_proposal_tools.py::test_create_rejects_unknown_market_with_allowed_contract_guidance \
  tests/test_mcp_order_proposal_tools.py::test_create_docstring_documents_markets_aliases_and_account_modes \
  -q
```

Expected: alias create is rejected, unknown-market guidance is absent, and the
docstring lacks market/account documentation.

- [ ] **Step 3: Implement boundary normalization and contract guidance**

In `order_proposal_tools.py` add:

```python
_MARKET_ALIASES = {"kr": "equity_kr", "us": "equity_us"}


def _normalize_order_proposal_market(market: str) -> str:
    return _MARKET_ALIASES.get(market, market)
```

At the start of the create tool's `try` block, before rung conversion and
target preflight:

```python
market = _normalize_order_proposal_market(market)
```

Add this `Args:` entry:

```text
market: Canonical market in {equity_kr, equity_us, crypto}; aliases
        kr→equity_kr and us→equity_us are accepted. Supported place
        combinations are kis_live/toss_live with equity_kr or equity_us,
        and upbit with crypto.
```

In `service.py` define and use:

```python
_ALLOWED_ACTION_CONTRACT_MESSAGE = (
    "allowed: kis_live×equity_kr|equity_us, "
    "toss_live×equity_kr|equity_us, upbit×crypto; "
    "market aliases kr→equity_kr, us→equity_us"
)
```

```python
raise OrderProposalError(
    "unsupported account_mode/market/action: "
    f"{account_mode}/{market}/{normalized} "
    f"({_ALLOWED_ACTION_CONTRACT_MESSAGE})"
)
```

Update `app/mcp_server/README.md` with canonical markets, aliases, and
supported place combinations.

- [ ] **Step 4: Run focused proposal tests and verify GREEN**

```bash
uv run pytest tests/test_mcp_order_proposal_tools.py \
  tests/services/order_proposals/test_service.py \
  tests/services/order_proposals/test_payload.py -q \
  -k "alias or payload_hash or unsupported or docstring"
```

Expected: all selected tests pass without broker calls.

- [ ] **Step 5: Commit the proposal change**

```bash
git add tests/test_mcp_order_proposal_tools.py \
  tests/services/order_proposals/test_service.py \
  app/mcp_server/tooling/order_proposal_tools.py \
  app/services/order_proposals/service.py app/mcp_server/README.md
git commit -m "fix(ROB-855): normalize order proposal market aliases" \
  -m "Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

### Task 2: Expose the retrospective outcome enum

**Files:**
- Modify: `tests/test_trade_retrospective_tools.py`
- Modify: `tests/mcp_server/tooling/test_trade_retrospective_registration_docs.py`
- Modify: `app/services/trade_journal/trade_retrospective_service.py`
- Modify: `app/mcp_server/tooling/trade_retrospective_tools.py`
- Modify: `app/mcp_server/tooling/trade_retrospective_registration.py`

**Interfaces:**
- Consumes: `_VALID_OUTCOMES` and `save_trade_retrospective(..., outcome: str, ...)`.
- Produces: deterministic invalid-outcome guidance and public documentation containing all five actual outcomes.

- [ ] **Step 1: Write failing outcome-error and documentation tests**

Replace the broad validation assertion and add:

```python
@pytest.mark.asyncio
async def test_save_validation_error_enumerates_outcomes():
    res = await save_trade_retrospective(
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="win",
    )
    assert res["success"] is False
    for value in (
        "filled", "partially_filled", "unfilled", "rejected", "cancelled",
    ):
        assert value in res["error"]


def test_save_docstring_enumerates_outcomes():
    doc = save_trade_retrospective.__doc__ or ""
    for value in (
        "filled", "partially_filled", "unfilled", "rejected", "cancelled",
    ):
        assert value in doc
```

Extend
`tests/mcp_server/tooling/test_trade_retrospective_registration_docs.py` with
the same values asserted against the registered description.

- [ ] **Step 2: Run retrospective tests and verify RED**

```bash
uv run pytest \
  tests/test_trade_retrospective_tools.py::test_save_validation_error_enumerates_outcomes \
  tests/test_trade_retrospective_tools.py::test_save_docstring_enumerates_outcomes \
  tests/mcp_server/tooling/test_trade_retrospective_registration_docs.py -q
```

Expected: validation error and function docstring tests fail because the outcome
set is absent.

- [ ] **Step 3: Implement deterministic error and documentation**

In `trade_retrospective_service.py`:

```python
raise RetrospectiveValidationError(
    f"invalid outcome: {outcome} (allowed: {', '.join(sorted(_VALID_OUTCOMES))})"
)
```

Add this function docstring entry:

```text
outcome: One of filled, partially_filled, unfilled, rejected, cancelled.
```

Add the same outcome enumeration to the explicit FastMCP description in
`trade_retrospective_registration.py`.

- [ ] **Step 4: Run focused retrospective tests and verify GREEN**

```bash
uv run pytest tests/test_trade_retrospective_tools.py \
  tests/mcp_server/tooling/test_trade_retrospective_registration_docs.py \
  -q -k "validation_error or docstring or description"
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the retrospective change**

```bash
git add tests/test_trade_retrospective_tools.py \
  tests/mcp_server/tooling/test_trade_retrospective_registration_docs.py \
  app/services/trade_journal/trade_retrospective_service.py \
  app/mcp_server/tooling/trade_retrospective_tools.py \
  app/mcp_server/tooling/trade_retrospective_registration.py
git commit -m "fix(ROB-855): enumerate retrospective outcomes" \
  -m "Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

### Task 3: Verify, review, and ship the single PR

**Files:**
- Verify only: all modified files from Tasks 1 and 2
- Must remain unchanged: `app/services/order_proposals/revalidation.py`

**Interfaces:**
- Consumes: completed Tasks 1 and 2.
- Produces: a lint-clean, tested branch and one PR based on `main`.

- [ ] **Step 1: Run the required tests**

```bash
uv run pytest tests/services/order_proposals/ tests/mcp_server/ -q \
  -k "proposal or retrospective"
```

Expected: exit code 0, no failures, no live broker calls.

- [ ] **Step 2: Run project lint**

```bash
make lint
```

Expected: exit code 0 from Ruff and ty.

- [ ] **Step 3: Audit scope and trailers**

```bash
git diff origin/main --check
git diff --name-only origin/main
git diff origin/main -- app/services/order_proposals/revalidation.py
git log --format='%h%n%b' origin/main..HEAD
```

Expected: no whitespace errors; only ROB-855 files changed; `revalidation.py`
has no diff; every commit has the Paperclip trailer.

- [ ] **Step 4: Review the complete diff**

```bash
git diff origin/main...HEAD -- app/mcp_server/tooling/order_proposal_tools.py \
  app/services/order_proposals/service.py \
  app/services/trade_journal/trade_retrospective_service.py \
  app/mcp_server/tooling/trade_retrospective_tools.py \
  app/mcp_server/tooling/trade_retrospective_registration.py \
  tests/test_mcp_order_proposal_tools.py tests/test_trade_retrospective_tools.py \
  tests/mcp_server/tooling/test_trade_retrospective_registration_docs.py
```

Expected: normalization occurs once before preflight/service/hash; canonical
values are stored; errors/docs enumerate contracts; excluded behavior is
untouched.

- [ ] **Step 5: Push and create the PR**

Use the `ship` skill. Push `fix/ROB-855-order-proposal-market-dx` and create
one PR against `main` titled
`fix(ROB-855): improve order proposal market DX`. The body summarizes the
2026-07-13 false diagnosis (`kr` rejected, operators misdiagnosed account
mode/deployment after three retries, first KR execution produced zero orders),
lists tests, enumerates normalization callsites, and states no migration is
required because existing valid rows are canonical.
