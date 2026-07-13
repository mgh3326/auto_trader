# ROB-861 Buying-Power Pre-Submit Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Block known-underfunded Toss buy proposals before broker POST, keep them retryable, show exact Telegram shortfall amounts, and add a non-blocking create-time pending-buy advisory.

**Architecture:** Add a focused `buying_power.py` boundary for normalized cash calculations, a one-second single-flight Toss cache, atomic provisional claims, and pending-rung advisory queries. Inject the claimer/releaser into revalidation after successful preview matching, and carry structured shortage details through the existing `needs_reconfirm` Telegram flow. Keep `service.py` and `state_machine.py` unchanged.

**Review amendment:** The initial reader-then-reserver plan left a concurrency window between the balance read and broker POST. The implemented boundary atomically reads/checks/provisionally subtracts per account and currency before POST, tracks each claim with its own TTL/token, releases that exact token after explicit rejection, and retains accepted or ambiguous claims until their TTL expires.

**Tech Stack:** Python 3.13, asyncio, Decimal, SQLAlchemy async, pytest/pytest-asyncio, Ruff, ty, uv.

## Global Constraints

- Read Linear ROB-861 before implementation; its acceptance criteria are authoritative.
- Never make a real broker request; every provider and submit path is mocked in tests.
- Use strict TDD: add one behavior test, observe the expected failure, then write minimal production code.
- Insufficient buying power must produce broker POST count zero and rung state `needs_reconfirm`.
- Buying-power read failure must fail open to the existing broker submit path.
- Sell rungs must not invoke the gate.
- ROB-864 loss-cut confirmation must remain confirmation-first and gate-second.
- Do not modify `app/services/order_proposals/service.py` or `state_machine.py`.
- Implement Toss only; preserve broker-agnostic injected hook signatures for later KIS/Upbit support.
- Final required commands are `uv run pytest tests/services/order_proposals/ -q` and `make lint`.

---

### Task 1: Buying-Power Domain Boundary and Toss Cache

**Files:**
- Create: `app/services/order_proposals/buying_power.py`
- Create: `tests/services/order_proposals/test_buying_power.py`

**Interfaces:**
- Produces: `BuyingPowerKey`, `BuyingPowerCache`, `currency_for_market`, `required_cash`, `default_buying_power_reader`, `default_buying_power_reserver`, `pending_buy_requirement`, and `build_create_advisory`.
- Consumes: `TossReadClient.from_settings`, `OrderProposal`, `OrderProposalRung`, and an injected `AsyncSession` for read-only pending-rung aggregation.

- [ ] **Step 1: Write failing pure calculation and cache tests**

```python
def test_required_cash_prefers_preview_value_and_fee():
    assert required_cash(
        quantity=Decimal("3"),
        limit_price=Decimal("71100"),
        preview={"estimated_value": "213300", "fee": "32"},
    ) == Decimal("213332")


@pytest.mark.asyncio
async def test_cache_single_flight_and_reservation_adjustment():
    cache = BuyingPowerCache(ttl_seconds=1.0)
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        return Decimal("100000")

    key = BuyingPowerKey("toss_live", None, "KRW")
    first, second = await asyncio.gather(
        cache.get_or_load(key, loader), cache.get_or_load(key, loader)
    )
    await cache.reserve(key, Decimal("30000"))

    assert (first, second, calls) == (Decimal("100000"), Decimal("100000"), 1)
    assert await cache.get_or_load(key, loader) == Decimal("70000")
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/services/order_proposals/test_buying_power.py -q`

Expected: collection fails because `app.services.order_proposals.buying_power` does not exist.

- [ ] **Step 3: Implement normalized calculations and cache**

```python
@dataclass(frozen=True)
class BuyingPowerKey:
    account_mode: str
    broker_account_id: str | None
    currency: str


class BuyingPowerCache:
    def __init__(self, *, ttl_seconds: float = 1.0, clock=time.monotonic):
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: dict[BuyingPowerKey, tuple[float, Decimal]] = {}
        self._locks: defaultdict[BuyingPowerKey, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def get_or_load(self, key, loader):
        async with self._locks[key]:
            cached = self._entries.get(key)
            now = self._clock()
            if cached is not None and cached[0] > now:
                return cached[1]
            value = Decimal(await loader())
            self._entries[key] = (now + self._ttl_seconds, value)
            return value

    async def reserve(self, key, amount):
        async with self._locks[key]:
            cached = self._entries.get(key)
            if cached is not None and cached[0] > self._clock():
                self._entries[key] = (cached[0], max(cached[1] - amount, Decimal("0")))


def required_cash(*, quantity, limit_price, preview):
    notional = _optional_decimal(preview.get("estimated_value"))
    if notional is None:
        notional = Decimal(quantity) * Decimal(limit_price)
    return notional + (_optional_decimal(preview.get("fee")) or Decimal("0"))
```

Implement `default_buying_power_reader` so only `toss_live` constructs a
`TossReadClient`, calls `buying_power(currency=currency)`, closes the client,
and loads through the process-global cache. Other account modes return `None`.
Implement the reserver against the same cache key.

- [ ] **Step 4: Add failing pending-advisory aggregation tests**

Seed two same-account pending buy rungs and one different-account/sell rung.
Assert `pending_buy_requirement(session, account_mode="toss_live",
broker_account_id=None, currency="KRW")` returns only the two matching limit-buy
notionals and reports the count of skipped market-price rungs. Inject a reader
returning `Decimal("500000")` and assert `build_create_advisory` returns
`status="insufficient"`, exact pending required cash, and exact shortfall.

- [ ] **Step 5: Run aggregation tests and verify RED**

Run: `uv run pytest tests/services/order_proposals/test_buying_power.py -q`

Expected: calculation/cache tests pass and aggregation/advisory assertions fail
because the SQL read functions are absent.

- [ ] **Step 6: Implement read-only pending aggregation and advisory output**

Use one SQLAlchemy `select(OrderProposal, OrderProposalRung)` join filtered by:

```python
OrderProposal.account_mode == account_mode
OrderProposal.broker_account_id == broker_account_id
OrderProposal.market.in_(markets_for_currency(currency))
OrderProposalRung.state == "pending_approval"
OrderProposalRung.side == "buy"
```

Sum `Decimal(rung.quantity) * Decimal(rung.limit_price)` only when the limit
price exists. Return structured `sufficient`, `insufficient`, or `unavailable`
advisory dictionaries without mutating the session.

- [ ] **Step 7: Run Task 1 tests and verify GREEN**

Run: `uv run pytest tests/services/order_proposals/test_buying_power.py -q`

Expected: all tests pass with no provider network access.

- [ ] **Step 8: Commit Task 1**

```bash
git add app/services/order_proposals/buying_power.py tests/services/order_proposals/test_buying_power.py
git commit -m "feat(ROB-861): add buying-power domain boundary"
```

### Task 2: Click-Time Pre-Submit Gate

**Files:**
- Modify: `app/services/order_proposals/revalidation.py`
- Modify: `tests/services/order_proposals/test_revalidation.py`

**Interfaces:**
- Consumes: Task 1 `required_cash`, reader, and reserver callables.
- Produces: `revalidate_and_submit(*, service, proposal_id, now,
  place_order_fn=_default_place_order_fn,
  correlation_mint=_default_correlation_mint, fetch_target_fn=fetch_target_order,
  cancel_target_fn=cancel_target_order,
  fetch_submit_evidence_fn=fetch_submit_evidence,
  buying_power_reader=default_buying_power_reader,
  buying_power_reserver=default_buying_power_reserver)` with structured
  `needs_reconfirm` shortage outcomes.

- [ ] **Step 1: Write failing insufficient-buying-power test**

Create a Toss limit-buy proposal. Inject a preview that returns matching
quantity/price, valid Toss approval fields, `estimated_value="1070300"`, and
`fee="0"`; inject a reader returning `Decimal("400000")`. The injected place
function must raise if called with `dry_run=False`. Assert:

```python
assert outcomes == [
    RungOutcome(0, "needs_reconfirm", {
        "reason": "insufficient_buying_power",
        "currency": "KRW",
        "available": "400000",
        "required": "1070300",
        "shortfall": "670300",
    })
]
assert submit_calls == 0
assert refreshed_rung.state == "needs_reconfirm"
```

- [ ] **Step 2: Run the targeted test and verify RED**

Run: `uv run pytest tests/services/order_proposals/test_revalidation.py -q -k insufficient_buying_power_prevents_submit`

Expected: failure because `revalidate_and_submit` does not accept the injected reader and still submits.

- [ ] **Step 3: Implement the minimal gate**

Add reader/reserver callable aliases and default parameters. Propagate them to
`_revalidate_place_rung`. After preview validation and before `approved`, run:

```python
required = required_cash(
    quantity=Decimal(rung.quantity),
    limit_price=Decimal(rung.limit_price),
    preview=preview,
)
try:
    available = await _maybe_await(buying_power_reader(
        account_mode=group.account_mode,
        broker_account_id=group.broker_account_id,
        currency=currency_for_market(group.market),
    ))
except Exception:
    logger.warning("buying-power pre-submit lookup failed; continuing fail-open", exc_info=True)
    available = None
if available is not None and Decimal(available) < required:
    await service.mark_needs_reconfirm(proposal_id, rung_index, now=now)
    return RungOutcome(rung_index, "needs_reconfirm", {
        "reason": "insufficient_buying_power",
        "currency": currency,
        "available": format(Decimal(available).normalize(), "f"),
        "required": format(required.normalize(), "f"),
        "shortfall": format((required - Decimal(available)).normalize(), "f"),
    })
```

Include the explicit comment that provider failure is fail-open because this is
an operator UX aid and the broker is the authoritative final rejection gate.

- [ ] **Step 4: Add failing sufficient, sell, failure, and deposit-retry tests**

Add four focused tests:

- sufficient reader value reaches the unchanged submitted-resting path;
- sell rung never calls the reader;
- reader exception still reaches submit;
- first low read produces `needs_reconfirm`, transition it back to
  `pending_approval`, second higher read succeeds on the same proposal.

Also add a multi-rung test whose first accepted rung invokes the reserver and
whose second read observes the adjusted cached amount.

- [ ] **Step 5: Run the new tests and verify RED**

Run: `uv run pytest tests/services/order_proposals/test_revalidation.py -q -k 'buying_power or deposit'`

Expected: insufficient test passes; one or more reservation/retry assertions
fail until outcome-aware reservation is implemented.

- [ ] **Step 6: Reserve after accepted or ambiguous submit outcomes**

Capture `_classify_submit` results before return. Call the injected reserver
for `submitted_acked`, `submitted_resting`, and `unverified`; also reserve in a
submit-exception branch because broker state is ambiguous. Do not reserve an
explicit `error`/rejected outcome. Keep the reservation TTL-bound.

- [ ] **Step 7: Run revalidation tests and verify GREEN**

Run: `uv run pytest tests/services/order_proposals/test_revalidation.py -q`

Expected: all tests pass; existing sell, KIS, Upbit, replace/cancel, and ROB-864
tests remain unchanged.

- [ ] **Step 8: Commit Task 2**

```bash
git add app/services/order_proposals/revalidation.py tests/services/order_proposals/test_revalidation.py
git commit -m "feat(ROB-861): gate Toss submits on buying power"
```

### Task 3: Telegram Shortfall Copy and Retry UX

**Files:**
- Modify: `app/services/order_proposals/approval_message.py`
- Modify: `app/services/order_proposals/telegram_callback.py`
- Modify: `tests/services/order_proposals/test_approval_message.py`
- Modify: `tests/services/order_proposals/test_telegram_callback.py`

**Interfaces:**
- Consumes: Task 2 shortage outcome detail.
- Produces: `build_buying_power_shortfall_text(detail) -> str | None` and shortage-aware reconfirm rendering for one or many rungs.

- [ ] **Step 1: Write failing KRW/USD formatter tests**

```python
assert build_buying_power_shortfall_text({
    "reason": "insufficient_buying_power", "currency": "KRW",
    "available": "400000", "required": "1070300", "shortfall": "670300",
}) == "매수가능 400,000원 / 필요 1,070,300원 → 부족 670,300원 — 입금 후 재승인"

assert build_buying_power_shortfall_text({
    "reason": "insufficient_buying_power", "currency": "USD",
    "available": "100", "required": "123.45", "shortfall": "23.45",
}) == "매수가능 $100.00 / 필요 $123.45 → 부족 $23.45 — 입금 후 재승인"
```

Build an approval message with shortage detail and assert it contains the line,
does not contain `변경 전`, and retains approve/deny callback buttons.

- [ ] **Step 2: Run formatter tests and verify RED**

Run: `uv run pytest tests/services/order_proposals/test_approval_message.py -q -k buying_power`

Expected: import/attribute failure because the formatter does not exist.

- [ ] **Step 3: Implement shortage-aware approval rendering**

Add Decimal-safe currency formatting. In `build_approval_message`, identify
`reason == "insufficient_buying_power"`, render a `*매수가능 금액 부족*`
section, and skip the generic before/after diff section for that detail.

- [ ] **Step 4: Add failing callback integration tests**

Return one shortage `needs_reconfirm` outcome from the injected revalidator.
Assert both the edited old-message notice and the new approval message include
Z/Y/X copy, the new message has an approve button, and the database rung remains
`needs_reconfirm`. Add a two-rung test and assert both shortage lines appear.

- [ ] **Step 5: Run callback tests and verify RED**

Run: `uv run pytest tests/services/order_proposals/test_telegram_callback.py -q -k buying_power`

Expected: first shortage may render only in the new message while the old edit
and/or second shortage remains generic.

- [ ] **Step 6: Wire shortage text through callback branches**

Use the formatter for the old-message edit notice. Update
`_build_extra_reconfirm_block` so shortage outcomes render their localized
line while existing price/quantity diffs retain their current rendering.

- [ ] **Step 7: Run Telegram tests and verify GREEN**

Run: `uv run pytest tests/services/order_proposals/test_approval_message.py tests/services/order_proposals/test_telegram_callback.py -q`

Expected: all tests pass, including fresh nonce, repeated approval, mixed
outcome, and ROB-864 two-click tests.

- [ ] **Step 8: Commit Task 3**

```bash
git add app/services/order_proposals/approval_message.py app/services/order_proposals/telegram_callback.py tests/services/order_proposals/test_approval_message.py tests/services/order_proposals/test_telegram_callback.py
git commit -m "feat(ROB-861): show buying-power shortfall in Telegram"
```

### Task 4: Create-Time Pending-Buy Advisory

**Files:**
- Modify: `app/mcp_server/tooling/order_proposal_tools.py`
- Modify: `tests/test_mcp_order_proposal_tools.py`

**Interfaces:**
- Consumes: Task 1 `build_create_advisory`.
- Produces: Toss buy create responses with `buying_power_advisory` and top-level insufficient `warnings`.

- [ ] **Step 1: Write failing insufficient aggregate advisory test**

Create an existing same-account Toss pending buy, then create another proposal
with an injected/default reader monkeypatched to return `Decimal("500000")`.
Assert the second response contains both proposal notionals:

```python
assert created["buying_power_advisory"] == [{
    "status": "insufficient",
    "currency": "KRW",
    "buying_power": "500000",
    "pending_required": "700000",
    "shortfall": "200000",
    "skipped_market_rungs": 0,
    "warning": "매수가능 500,000원 / 승인대기 필요 700,000원 → 부족 200,000원",
}]
assert created["warnings"] == [created["buying_power_advisory"][0]["warning"]]
```

- [ ] **Step 2: Run the targeted test and verify RED**

Run: `uv run pytest tests/test_mcp_order_proposal_tools.py -q -k buying_power_advisory`

Expected: `buying_power_advisory` is absent.

- [ ] **Step 3: Add best-effort post-commit advisory wiring**

After the create transaction commits, open a fresh `AsyncSessionLocal` session,
call `build_create_advisory` for Toss buy/place proposals, attach the structured
list, and copy non-null insufficient warning strings to top-level `warnings`.
Wrap the whole advisory in `except Exception` with logging; never change the
already successful create result to failure.

- [ ] **Step 4: Add sufficient and unavailable regression tests**

Assert sufficient buying power returns `status="sufficient"` with no top-level
warning. Assert a reader exception still returns `success=True` and an
`unavailable` advisory. Assert KIS and sell creates do not call the reader and
retain the existing response shape.

- [ ] **Step 5: Run MCP create tests and verify GREEN**

Run: `uv run pytest tests/test_mcp_order_proposal_tools.py -q`

Expected: all tests pass and no Telegram or broker network calls occur.

- [ ] **Step 6: Commit Task 4**

```bash
git add app/mcp_server/tooling/order_proposal_tools.py tests/test_mcp_order_proposal_tools.py
git commit -m "feat(ROB-861): advise pending buys at proposal create"
```

### Task 5: Full Verification, Documentation, and PR

**Files:**
- Modify if needed: `docs/superpowers/specs/2026-07-13-rob-861-buying-power-pre-submit-gate-design.md`
- Modify if needed: `docs/superpowers/plans/2026-07-13-rob-861-buying-power-pre-submit-gate.md`

**Interfaces:**
- Consumes: all prior task outputs.
- Produces: verified branch, pushed commits, and a GitHub PR against `main`.

- [ ] **Step 1: Run the requested focused suite**

Run: `uv run pytest tests/services/order_proposals/ -q`

Expected: all focused tests pass.

- [ ] **Step 2: Run the external MCP create test file**

Run: `uv run pytest tests/test_mcp_order_proposal_tools.py -q`

Expected: all tests pass.

- [ ] **Step 3: Run lint/type validation**

Run: `make lint`

Expected: Ruff formatting/check and ty checks pass.

- [ ] **Step 4: Inspect the final diff and conflict-sensitive files**

Run:

```bash
git diff origin/main...HEAD --check
git diff --stat origin/main...HEAD
git diff --name-only origin/main...HEAD
git status --short
```

Expected: no whitespace errors; neither `service.py` nor `state_machine.py`
appears in the changed-file list; worktree is clean after the final commit.

- [ ] **Step 5: Run pre-landing review and fix verified findings with TDD**

Use `superpowers:requesting-code-review`, then the project `review` skill. For
each valid finding, write/adjust a failing test before changing production
behavior and rerun the relevant verification.

- [ ] **Step 6: Commit any review fixes**

```bash
git add -u
git commit -m "fix(ROB-861): address pre-submit gate review"
```

Skip this commit only when there are no review fixes.

- [ ] **Step 7: Push and create the PR**

```bash
git push -u origin rob-861
gh pr create --base main --head rob-861 --title "feat(ROB-861): gate approvals on buying power before submit" --body-file /tmp/rob-861-pr.md
```

The PR body must include the problem, pre-submit/data-flow summary, fail-open
rationale, cache/reservation behavior, test results, explicit Toss-only scope,
KIS/Upbit deferral, and the fact that `service.py`/`state_machine.py` were not
changed so no ROB-862 merge ordering is required.

- [ ] **Step 8: Report completion**

Report the PR number/link, focused pytest result, MCP pytest result, `make lint`
result, and exactly one line stating: Toss included; KIS/Upbit deferred behind
the broker-agnostic hook.
