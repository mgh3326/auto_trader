# ROB-660 Sell Lane Account-Routing Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `route_request(profit_taking)` allow selling KIS holdings (`kis_live_place_order`) and clearing a same-symbol Toss buy-pending (`toss_cancel_order`) inside the sell lane, plus surface the order-history confirmation helpers, so the advisory lane definition matches the "sells execute from the holding account" rule.

**Architecture:** Purely additive change to the static lane definitions in `app/mcp_server/tooling/route_request_lanes.py` (pure, no IO) and its single-source playbook `docs/playbooks/trading-decision-playbook.md`. Two tools become ordered sequence steps in the sell lane (mirroring the buy lane's Toss+KIS dual-place symmetry); two read-only-in-reality order-history tools become allowed-only via a new `LANE_EXTRA_ALLOWED` supplement (parallel to ROB-658's `MARKET_EXECUTION_TOOLS`). Sell-lane `HARD_CONSTRAINTS` gain two self-documenting routing lines.

**Tech Stack:** Python 3.13, pytest, uv, ruff, ty. No new dependencies. No DB migration.

## Global Constraints

- **Migration: 0** — no DB / ORM changes.
- **Scope: sell lane only** — do NOT modify the buy, discovery, or bootstrap lanes. The buy lane already lists both `toss_place_order` and `kis_live_place_order`; its only issue-flagged gap is already met.
- **Sync invariant:** `tests/test_route_request_registry_diff.py::test_lane_sequences_match_playbook` asserts exact set-equality between `lane_tool_names("sell")` (code) and the playbook YAML `tool:` refs. Any change to `LANE_SEQUENCES["sell"]` MUST land in the same commit as the matching playbook YAML edit, or CI breaks.
- **Allowed-only tools are NOT sequenced:** `kis_live_get_order_history` / `toss_get_order_history` go in `LANE_EXTRA_ALLOWED`, never in `LANE_SEQUENCES` or the playbook YAML.
- **Do not touch `READ_ONLY_ADVISORY_TOOLS` / `MUTATION_TOOLS` buckets** — all four tools are already classified in `MUTATION_TOOLS`.
- End commit messages with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

## File Structure

- `app/mcp_server/tooling/route_request_lanes.py` — sell lane sequence, new `LANE_EXTRA_ALLOWED` constant, `build_route_plan` wiring, sell `HARD_CONSTRAINTS`, `__all__`.
- `docs/playbooks/trading-decision-playbook.md` — sell lane YAML block (§2) + sell prose + allowed-helpers divergence note.
- `tests/test_route_request_lanes.py` — new unit tests for the sell-lane behavior.

---

### Task 1: Sell lane sequence + allowed helpers + playbook sync

**Files:**
- Modify: `app/mcp_server/tooling/route_request_lanes.py` (`LANE_SEQUENCES["sell"]`, new `LANE_EXTRA_ALLOWED`, `build_route_plan`, `__all__`)
- Modify: `docs/playbooks/trading-decision-playbook.md` (§2 sell YAML block + prose)
- Test: `tests/test_route_request_lanes.py`

**Interfaces:**
- Consumes: existing `build_route_plan(intent, market, *, registered_tools, verdict_thresholds, policy_version) -> dict`, module constants `MUTATION_TOOLS`, `READ_ONLY_ADVISORY_TOOLS`, `ALL_KNOWN_TOOLS`, `lane_tool_names(lane)`.
- Produces: new module-level `LANE_EXTRA_ALLOWED: dict[str, frozenset[str]]`; sell lane now surfaces `toss_cancel_order` + `kis_live_place_order` as sequence steps and `kis_live_get_order_history` + `toss_get_order_history` as allowed-only.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_route_request_lanes.py` (uses existing `_ALL`, `_VERSION`, `_fake_thresholds`, `_CRYPTO_REGISTERED` helpers already defined in that file):

```python
# --- ROB-660: sell lane account routing ---------------------------------------


def test_sell_lane_surfaces_kis_place_and_toss_cancel_as_steps():
    plan = L.build_route_plan(
        "profit_taking",
        "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "sell"),
        policy_version=_VERSION,
    )
    steps = [s["tool"] for s in plan["standard_tool_sequence"]]
    for tool in ("toss_cancel_order", "kis_live_place_order"):
        assert tool in steps, tool
        assert tool in plan["allowed_tools"], tool
        assert tool not in plan["blocked_actions"], tool
    # step numbers stay contiguous 1..n after the two inserts
    assert [s["step"] for s in plan["standard_tool_sequence"]] == list(
        range(1, len(steps) + 1)
    )


def test_sell_lane_history_helpers_allowed_but_not_sequenced():
    plan = L.build_route_plan(
        "profit_taking",
        "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "sell"),
        policy_version=_VERSION,
    )
    steps = [s["tool"] for s in plan["standard_tool_sequence"]]
    for tool in ("kis_live_get_order_history", "toss_get_order_history"):
        assert tool in plan["allowed_tools"], tool
        assert tool not in plan["blocked_actions"], tool
        assert tool not in steps, tool


def test_sell_lane_routing_does_not_leak_into_buy_lane():
    buy = L.build_route_plan(
        "buy_analysis",
        "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "buy"),
        policy_version=_VERSION,
    )
    # sell-lane-only tools remain blocked in the buy lane (no cross-lane leak)
    assert "toss_cancel_order" in buy["blocked_actions"]
    assert "kis_live_get_order_history" in buy["blocked_actions"]
    assert "toss_get_order_history" in buy["blocked_actions"]


def test_sell_lane_new_kr_tools_dropped_when_unregistered_on_crypto():
    plan = L.build_route_plan(
        "profit_taking",
        "crypto",
        registered_tools=_CRYPTO_REGISTERED,
        verdict_thresholds=_fake_thresholds("crypto", "sell"),
        policy_version=_VERSION,
    )
    steps = [s["tool"] for s in plan["standard_tool_sequence"]]
    assert "toss_cancel_order" not in steps
    assert "kis_live_place_order" not in steps
    assert "toss_cancel_order" not in plan["allowed_tools"]
    # ROB-658 generic execution injection still fires on crypto sell
    assert "place_order" in steps
    assert "place_order" not in plan["blocked_actions"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_route_request_lanes.py -k "sell_lane" -v`
Expected: FAIL — `toss_cancel_order` / `kis_live_place_order` currently in `blocked_actions`, not in sequence; `LANE_EXTRA_ALLOWED` not referenced yet (helpers still blocked).

- [ ] **Step 3: Add the two new sequence steps to `LANE_SEQUENCES["sell"]`**

In `app/mcp_server/tooling/route_request_lanes.py`, replace the `"sell"` entry of `LANE_SEQUENCES` with:

```python
    "sell": [
        {
            "tool": "toss_get_positions",
            "purpose": "scan in-the-money / near-breakeven names",
        },
        {
            "tool": "analyze_stock_batch",
            "purpose": "confirm distance to resistance, RSI, upside",
        },
        {
            "tool": "toss_cancel_order",
            "purpose": "clear same-symbol buy pending first (Toss two-sided constraint)",
        },
        {
            "tool": "toss_place_order",
            "purpose": "sell-into-strength split ladder just under resistance (Toss holdings)",
        },
        {
            "tool": "kis_live_place_order",
            "purpose": "sell KIS holdings from the holding account; dry_run preview -> live",
        },
        {
            "tool": "sell_ladder_fill_preview",
            "purpose": "ROB-477 bottom-anchor rung, fill-safety",
        },
    ],
```

- [ ] **Step 4: Add the `LANE_EXTRA_ALLOWED` constant**

In the same file, immediately after the `PREVIEW_TOOLS` definition (the block ending `PREVIEW_TOOLS: frozenset[str] = frozenset({"toss_preview_order"})`), insert:

```python
# ROB-660: per-lane allowed-only helper tools. The order-status tools
# (kis_live_get_order_history / toss_get_order_history) are read-only in reality
# but bucketed in MUTATION_TOOLS for registry partitioning, so build_route_plan
# would otherwise block them even in the lane that needs them. The sell lane needs
# them to confirm a cancel took effect and to check sell-order fill status. They
# are un-blocked here (allowed) WITHOUT entering the ordered sequence (confirmation
# helpers, not workflow steps) or the playbook YAML. Parallels MARKET_EXECUTION_TOOLS
# (ROB-658) as an allowed supplement.
LANE_EXTRA_ALLOWED: dict[str, frozenset[str]] = {
    "sell": frozenset({"kis_live_get_order_history", "toss_get_order_history"}),
}
```

- [ ] **Step 5: Wire `LANE_EXTRA_ALLOWED` into `build_route_plan`**

In `build_route_plan`, find the `lane_preview` / `allowed` / `blocked` block:

```python
    lane_preview = PREVIEW_TOOLS if lane_place_tools else frozenset()
    allowed = (
        lane_tools | market_exec | lane_preview | set(READ_ONLY_ADVISORY_TOOLS)
    ) & registered_tools
    blocked = (MUTATION_TOOLS - lane_own_mutation - lane_preview) & registered_tools
```

Replace it with:

```python
    lane_preview = PREVIEW_TOOLS if lane_place_tools else frozenset()
    # ROB-660: allowed-only confirmation helpers (order-status tools) — un-blocked
    # for the lane but never added to the ordered sequence.
    lane_extra = LANE_EXTRA_ALLOWED.get(lane, frozenset())
    allowed = (
        lane_tools
        | market_exec
        | lane_preview
        | lane_extra
        | set(READ_ONLY_ADVISORY_TOOLS)
    ) & registered_tools
    blocked = (
        MUTATION_TOOLS - lane_own_mutation - lane_preview - lane_extra
    ) & registered_tools
```

- [ ] **Step 6: Export `LANE_EXTRA_ALLOWED` in `__all__`**

In the `__all__` list, add `"LANE_EXTRA_ALLOWED",` immediately after `"PREVIEW_TOOLS",`.

- [ ] **Step 7: Update the playbook sell lane YAML block**

In `docs/playbooks/trading-decision-playbook.md`, replace the sell lane YAML `steps:` block (under `# playbook-machine-readable: sell lane (ROB-649 source)`) so it reads:

```yaml
    steps:
      - tool: toss_get_positions
      - tool: analyze_stock_batch
      - tool: toss_cancel_order       # clear same-symbol buy pending (two-sided) before sell
      - tool: toss_place_order        # sell-into-strength split ladder (Toss holdings)
        confirm: true
      - tool: kis_live_place_order    # sell KIS holdings from holding account; dry_run preview -> live
      - tool: sell_ladder_fill_preview  # ROB-477 bottom-anchor rung, fill-safety
```

Leave `verdicts:` and `gates:` unchanged. Do NOT add the history tools here.

- [ ] **Step 8: Update the sell prose (§2) and add the allowed-helpers note**

In `## 2) Sell (profit-taking) pipeline`, replace step `4.` (the "Execute: sell-into-strength ..." bullet) with:

```markdown
4. Execute from the **holding account**: Toss holdings via `toss_place_order`, KIS
   holdings via `kis_live_place_order` (`dry_run` preview → live). Sell-into-strength
   **split ladder** just under resistance;
   [ROB-477](https://linear.app/mgh3326/issue/ROB-477) requires a bottom-anchor rung
   (`sell_ladder_fill_preview` checks fill-safety), preserve the core lot. Trim
   over-concentrated sectors first when in the money. If the name has a **pending buy
   limit on Toss**, `toss_cancel_order` **first** — no two-sided (buy+sell) resting
   orders on one symbol.
```

Then, immediately after the sell lane ```` ```yaml ```` block's closing fence, add:

```markdown
> **Allowed helpers (not sequenced, ROB-660).** `kis_live_get_order_history` /
> `toss_get_order_history` are allowed in this lane for cancel/fill confirmation, but
> they are read-only confirmation helpers rather than ordered steps, so they live in
> `LANE_EXTRA_ALLOWED` (`app/mcp_server/tooling/route_request_lanes.py`) — not the
> YAML sequence above.
```

- [ ] **Step 9: Run the new tests + the sync/registry tests to verify they pass**

Run: `uv run pytest tests/test_route_request_lanes.py tests/test_route_request_registry_diff.py -v`
Expected: PASS — including `test_lane_sequences_match_playbook`, `test_lane_tools_registered_in_default`, and the four new `sell_lane` tests. (If `test_lane_tools_registered_in_default` fails on `toss_cancel_order`, that means it is not in the DEFAULT profile — stop and report; the design assumes it is registered alongside `toss_place_order`.)

- [ ] **Step 10: Run the playbook tool-name guard**

Run: `uv run pytest tests/test_playbook_tool_names.py -v`
Expected: PASS — both new YAML tools exist in the DEFAULT profile.

- [ ] **Step 11: Commit**

```bash
git add app/mcp_server/tooling/route_request_lanes.py docs/playbooks/trading-decision-playbook.md tests/test_route_request_lanes.py
git commit -m "feat(ROB-660): sell lane allows KIS-holdings sell + Toss cancel + order-history helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Sell lane hard-constraint routing lines

**Files:**
- Modify: `app/mcp_server/tooling/route_request_lanes.py` (`HARD_CONSTRAINTS["sell"]`)
- Test: `tests/test_route_request_lanes.py`

**Interfaces:**
- Consumes: `build_route_plan(...)["hard_constraints"]` (list[str]).
- Produces: sell lane `hard_constraints` now includes an account-routing line and a cancel-first line.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_route_request_lanes.py`:

```python
def test_sell_lane_hard_constraints_document_routing_and_cancel_first():
    plan = L.build_route_plan(
        "profit_taking",
        "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "sell"),
        policy_version=_VERSION,
    )
    joined = " ".join(plan["hard_constraints"])
    assert "holding account" in joined
    assert "kis_live_place_order" in joined
    assert "toss_cancel_order" in joined
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_route_request_lanes.py::test_sell_lane_hard_constraints_document_routing_and_cancel_first -v`
Expected: FAIL — the phrases are not yet in `HARD_CONSTRAINTS["sell"]`.

- [ ] **Step 3: Add the two hard-constraint lines**

In `HARD_CONSTRAINTS`, replace the `"sell"` list with (appending the two new lines at the end):

```python
    "sell": [
        "loss guard: sell price >= avg * sell.loss_guard_min_multiple",
        "KRX tick rounding",
        "no two-sided (buy+sell) resting orders on same Toss symbol",
        "DAY order expiry at order.day_expiry_kst -> re-place next day",
        "preserve core lot; trim over-concentrated sectors first (portfolio.sector_cluster_cap_pct)",
        "sell from holding account: Toss holdings -> toss_place_order, KIS holdings -> kis_live_place_order",
        "same-symbol buy pending on Toss -> toss_cancel_order first (two-sided constraint)",
    ],
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_route_request_lanes.py::test_sell_lane_hard_constraints_document_routing_and_cancel_first -v`
Expected: PASS

- [ ] **Step 5: Confirm the policy-key guard still holds**

Run: `uv run pytest tests/test_route_request_lanes.py::test_hard_constraints_reference_policy_keys_not_numbers -v`
Expected: PASS — the new lines contain no bare numbers (that test targets the buy lane, but confirm nothing regressed).

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/route_request_lanes.py tests/test_route_request_lanes.py
git commit -m "feat(ROB-660): sell lane hard_constraints document holding-account routing + cancel-first

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Full verification (route_request suite + lint + typecheck)

**Files:** none modified (verification only; fix inline if something fails).

- [ ] **Step 1: Run the full route_request test surface**

Run: `uv run pytest tests/test_route_request.py tests/test_route_request_lanes.py tests/test_route_request_registry_diff.py tests/test_playbook_tool_names.py -v`
Expected: PASS (all). Confirm the pre-existing determinism test `test_deterministic_same_input_same_output[...profit_taking...]` and `test_executing_lane_surfaces_toss_preview_precursor` still pass.

- [ ] **Step 2: Format**

Run: `uv run ruff format app/mcp_server/tooling/route_request_lanes.py tests/test_route_request_lanes.py`
Expected: files unchanged or reformatted; if reformatted, re-run Step 1.

- [ ] **Step 3: Lint**

Run: `uv run ruff check app/mcp_server/tooling/route_request_lanes.py tests/test_route_request_lanes.py`
Expected: no errors.

- [ ] **Step 4: Typecheck**

Run: `uv run ty check app/mcp_server/tooling/route_request_lanes.py`
Expected: no new errors.

- [ ] **Step 5: Commit any format/lint fixups (only if Steps 2–4 changed files)**

```bash
git add -A
git commit -m "chore(ROB-660): ruff format + lint fixups

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Sell lane place+cancel sequence steps → Task 1 (Steps 3, 7, 8). ✓
- Allowed-only history helpers via `LANE_EXTRA_ALLOWED` → Task 1 (Steps 4, 5, 6). ✓
- `HARD_CONSTRAINTS["sell"]` routing + cancel-first → Task 2. ✓
- Playbook YAML sync + prose + divergence note → Task 1 (Steps 7, 8). ✓
- Behaviour-preserved (crypto injection, cross-lane non-leak, buy untouched) → Task 1 tests (Steps 1, 9). ✓
- Migration 0, sell-only scope → Global Constraints. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full replacement blocks and exact commands. ✓

**Type consistency:** `LANE_EXTRA_ALLOWED: dict[str, frozenset[str]]` referenced identically in Steps 4/5/6 and Task 2 helper test. `build_route_plan` signature unchanged. Tool names (`toss_cancel_order`, `kis_live_place_order`, `kis_live_get_order_history`, `toss_get_order_history`) spelled consistently across all tasks. ✓
