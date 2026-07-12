# ROB-839 Crypto Policy Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose report-derived crypto recovery, support/resistance, and no-chasing judgment rules through the versioned trading-policy contract without inventing absent numeric thresholds.

**Architecture:** Add a strict typed `market_rules.crypto` YAML block beside the existing scalar `market_overrides`. The loader filters each rule by lane and returns it as an additive `market_rules` response field while preserving the existing version/hash stamp and all execution guards.

**Tech Stack:** Python 3.13+, Pydantic v2, PyYAML, pytest, Ruff, ty, FastMCP tool wrapper.

## Global Constraints

- All new policy rules are advisory and read-only.
- Do not change loss-sale or ladder fail-closed execution guards.
- Use `null` for Fear & Greed, kimchi-premium, same-day-gain, and liquidity thresholds absent from the reports.
- Keep `breadth > 50%`, `BTC L/S <= 1.5`, and `2 of 4` exactly as report-derived values.
- Preserve `{version, content_hash}` where `content_hash` is the first 12 SHA-256 hex characters of raw YAML bytes.
- Version the policy as `2026-07-12.1`.
- No database changes or migrations.
- Keep MCP documentation synchronized with the response contract.

---

### Task 1: Strict Crypto Market-Rule Schema and Seed YAML

**Files:**
- Modify: `tests/schemas/test_trading_policy_schema.py`
- Modify: `app/schemas/trading_policy.py`
- Modify: `config/trading_policy.yaml`

**Interfaces:**
- Consumes: `TradingPolicyDocument.model_validate(raw: object) -> TradingPolicyDocument`
- Produces: `TradingPolicyDocument.market_rules: dict[Literal["crypto"], CryptoMarketRules]`
- Produces: typed `recovery_gate`, `support_resistance`, and `no_chasing` rule objects with `lanes` and `advisory` fields.

- [ ] **Step 1: Write failing shipped-config and nested-validation tests**

Add assertions that version `2026-07-12.1` validates, crypto recovery condition IDs are exactly `fear_greed`, `alt_breadth_24h`, `btc_long_short_ratio`, and `btc_kimchi_premium`, breadth is `gt 50`, L/S is `lte 1.5`, F&G/kimchi thresholds are `None`, no-chasing numeric fields are `None`, and source priority is stable. Add a nested extra-key rejection test:

```python
def test_crypto_market_rules_preserve_report_derived_and_null_thresholds():
    doc = TradingPolicyDocument.model_validate(_raw())
    rules = doc.market_rules["crypto"]
    gate = rules.recovery_gate
    assert gate.min_conditions_met == 2
    assert gate.of == 4
    assert [condition.id for condition in gate.conditions] == [
        "fear_greed",
        "alt_breadth_24h",
        "btc_long_short_ratio",
        "btc_kimchi_premium",
    ]
    assert gate.conditions[0].threshold is None
    assert (gate.conditions[1].operator, gate.conditions[1].threshold) == ("gt", 50)
    assert (gate.conditions[2].operator, gate.conditions[2].threshold) == ("lte", 1.5)
    assert gate.conditions[3].threshold is None
    assert rules.no_chasing.daily_change_pct_threshold is None
    assert rules.no_chasing.min_trade_value_24h_krw is None
    assert rules.support_resistance.source_priority == [
        "fibonacci",
        "value_area",
        "bb_lower",
        "bb_middle",
        "volume_poc",
    ]


def test_extra_crypto_market_rule_key_rejected():
    raw = _raw()
    raw["market_rules"]["crypto"]["no_chasing"]["bogus"] = True
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)
```

- [ ] **Step 2: Run schema tests and verify red**

Run: `uv run pytest tests/schemas/test_trading_policy_schema.py -q`

Expected: FAIL because version is still `2026-07-07.1` and `market_rules` is absent/forbidden.

- [ ] **Step 3: Add strict Pydantic rule models**

Implement models with `ConfigDict(extra="forbid")`:

```python
PolicyComparison = Literal["gt", "gte", "lt", "lte", "eq"]


class PolicyRecoveryCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    metric: str
    sources: list[str]
    operator: PolicyComparison | None
    threshold: int | float | None
    unit: str
    semantics: str


class PolicyRecoveryGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    advisory: bool
    semantics: str
    min_conditions_met: int
    of: int
    missing_or_null_threshold: str
    conditions: list[PolicyRecoveryCondition]


class PolicySupportResistanceRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    advisory: bool
    semantics: str
    selection_rule: str
    source_priority: list[str]
    confluence_examples: list[list[str]]


class PolicyNoChasingRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    advisory: bool
    semantics: str
    daily_change_pct_threshold: float | None
    min_trade_value_24h_krw: int | None
    criteria: list[str]
    follow_up: str


class CryptoMarketRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recovery_gate: PolicyRecoveryGate
    support_resistance: PolicySupportResistanceRule
    no_chasing: PolicyNoChasingRule
```

Add to `TradingPolicyDocument`:

```python
market_rules: dict[Literal["crypto"], CryptoMarketRules]
```

- [ ] **Step 4: Seed the report-derived YAML**

Update metadata to `version: "2026-07-12.1"`, `captured_as_of: "2026-07-12"`, and a source naming ROB-839 plus the four crypto recheck reports. Add `market_rules.crypto` using the exact constraints from the approved design:

```yaml
market_rules:
  crypto:
    recovery_gate:
      lanes: [buy]
      advisory: true
      semantics: reserve deployment recovery frame; otherwise support-conditional only
      min_conditions_met: 2
      of: 4
      missing_or_null_threshold: do_not_infer_or_count_as_met
      conditions:
        - id: fear_greed
          metric: crypto_fear_greed_index
          sources: [alternative_me]
          operator: null
          threshold: null
          unit: index
          semantics: reports cite a recovering trend but define no pass cutoff
        - id: alt_breadth_24h
          metric: upbit_alt_breadth_24h
          sources: [upbit_open_api_ticker_derived]
          operator: gt
          threshold: 50
          unit: percent
          semantics: share of KRW alts outperforming BTC over 24h
        - id: btc_long_short_ratio
          metric: btc_long_short_ratio
          sources: [binance_global_account, binance_top_trader_position]
          operator: lte
          threshold: 1.5
          unit: ratio
          semantics: both report inputs should remain at or below the threshold
        - id: btc_kimchi_premium
          metric: btc_kimchi_premium
          sources: [upbit_binance_fx]
          operator: null
          threshold: null
          unit: percent
          semantics: reports interpret discount and domestic FOMO qualitatively; no pass cutoff
    support_resistance:
      lanes: [buy, sell, discovery]
      advisory: true
      semantics: rank fresh levels by independent-source confluence before source priority
      selection_rule: confluence_first_then_source_priority
      source_priority: [fibonacci, value_area, bb_lower, bb_middle, volume_poc]
      confluence_examples:
        - [fib_0, value_area_low, bb_lower]
        - [bb_middle, fib_23_6]
        - [bb_middle, volume_poc]
    no_chasing:
      lanes: [buy, discovery]
      advisory: true
      semantics: reject pump-like new entries without inventing numeric cutoffs
      daily_change_pct_threshold: null
      min_trade_value_24h_krw: null
      criteria:
        - exclude low-liquidity rotation-pump candidates
        - new alt candidates are ineligible when 24h alt breadth is below 50 percent
        - exclude sharply rising candidates without support structure or decision history
      follow_up: reports contain no quantitative cutoff; operator fills values after live-operation evidence in a follow-up PR
```

Add an adjacent YAML comment explaining that both `null` values prohibit model inference.

- [ ] **Step 5: Run schema tests and verify green**

Run: `uv run pytest tests/schemas/test_trading_policy_schema.py -q`

Expected: all tests PASS.

- [ ] **Step 6: Commit schema and seed policy**

```bash
git add config/trading_policy.yaml app/schemas/trading_policy.py tests/schemas/test_trading_policy_schema.py
git commit -m "feat(ROB-839): define crypto judgment policy rules"
```

### Task 2: Lane-Filtered Service Exposure and Version Stamp

**Files:**
- Modify: `tests/services/test_trading_policy_service.py`
- Modify: `app/services/trading_policy_service.py`

**Interfaces:**
- Consumes: `CryptoMarketRules` instances loaded by `_load()`.
- Produces: `get_policy_for(market: str, lane: str) -> dict[str, Any]` with additive `market_rules: dict[str, Any]`.
- Preserves: `policy_version_stamp() -> dict[str, str]` and raw-YAML hash behavior.

- [ ] **Step 1: Write failing lane-filter and stamp tests**

Add tests with exact rule visibility:

```python
def test_get_policy_for_crypto_buy_exposes_report_derived_market_rules():
    view = svc.get_policy_for("crypto", "buy")
    assert view["version"] == "2026-07-12.1"
    assert set(view["market_rules"]) == {
        "recovery_gate",
        "support_resistance",
        "no_chasing",
    }
    gate = view["market_rules"]["recovery_gate"]
    assert gate["min_conditions_met"] == 2
    assert gate["of"] == 4
    assert "lanes" not in gate
    assert view["market_rules"]["no_chasing"]["daily_change_pct_threshold"] is None


def test_get_policy_for_filters_crypto_market_rules_by_lane():
    discovery = svc.get_policy_for("crypto", "discovery")["market_rules"]
    assert set(discovery) == {"support_resistance", "no_chasing"}
    sell = svc.get_policy_for("crypto", "sell")["market_rules"]
    assert set(sell) == {"support_resistance"}
    assert svc.get_policy_for("kr", "buy")["market_rules"] == {}
```

Update existing hard-coded version assertions to `2026-07-12.1`.

- [ ] **Step 2: Run service tests and verify red**

Run: `uv run pytest tests/services/test_trading_policy_service.py -q`

Expected: FAIL because `get_policy_for` has no `market_rules` field.

- [ ] **Step 3: Implement generic lane filtering**

Before the return value in `get_policy_for`, build the additive view without mutating Pydantic models:

```python
    market_rules: dict[str, Any] = {}
    rules = doc.market_rules.get(market)  # type: ignore[arg-type]
    if rules is not None:
        for key, spec in rules:
            if lane not in spec.lanes:
                continue
            market_rules[key] = spec.model_dump(exclude={"lanes"})
```

Return it as:

```python
        "market_rules": market_rules,
```

If Pydantic model iteration does not preserve `(field_name, value)` typing under ty, use `for key in type(rules).model_fields` plus `getattr(rules, key)`; do not serialize and then re-parse the whole document.

- [ ] **Step 4: Run service tests and verify green**

Run: `uv run pytest tests/services/test_trading_policy_service.py -q`

Expected: all tests PASS, including override and hash tests.

- [ ] **Step 5: Commit service exposure**

```bash
git add app/services/trading_policy_service.py tests/services/test_trading_policy_service.py
git commit -m "feat(ROB-839): expose crypto policy rules by lane"
```

### Task 3: MCP Contract and Documentation

**Files:**
- Modify: `tests/mcp_server/test_trading_policy_tool.py`
- Modify: `app/mcp_server/README.md`

**Interfaces:**
- Consumes: `get_policy_for("crypto", "buy")` response including `market_rules`.
- Produces: `await get_trading_policy(market="crypto", lane="buy")` with `success`, stamp, thresholds, decision rules, and market rules.

- [ ] **Step 1: Write the failing MCP crypto exposure test**

```python
@pytest.mark.asyncio
async def test_get_trading_policy_returns_crypto_market_rules_and_stamp():
    out = await get_trading_policy(market="crypto", lane="buy")
    assert out["success"] is True
    assert out["version"] == "2026-07-12.1"
    assert len(out["content_hash"]) == 12
    assert out["market_rules"]["recovery_gate"]["min_conditions_met"] == 2
    assert out["market_rules"]["no_chasing"]["daily_change_pct_threshold"] is None
```

Update the existing version assertion to `2026-07-12.1`.

- [ ] **Step 2: Run MCP tests and verify the new assertion**

Run: `uv run pytest tests/mcp_server/test_trading_policy_tool.py -q`

Expected: PASS if Task 2 is correct; if it fails, the failure must identify a service-wrapper contract mismatch and be fixed without adding logic to the wrapper.

- [ ] **Step 3: Document the additive field and advisory boundary**

In the existing `get_trading_policy` section of `app/mcp_server/README.md`, add:

```markdown
- `market_rules`: market-specific advisory judgment rules filtered by lane.
  Crypto includes the recovery gate, support/resistance source priority, and
  no-chasing criteria. A `null` threshold is intentional and must not be
  replaced by a caller-inferred number. These rules do not replace code-owned
  fail-closed order guards.
```

- [ ] **Step 4: Run the complete targeted policy suite**

Run:

```bash
uv run pytest \
  tests/schemas/test_trading_policy_schema.py \
  tests/services/test_trading_policy_service.py \
  tests/mcp_server/test_trading_policy_tool.py -q
```

Expected: all tests PASS.

- [ ] **Step 5: Commit MCP contract and docs**

```bash
git add tests/mcp_server/test_trading_policy_tool.py app/mcp_server/README.md
git commit -m "docs(ROB-839): describe crypto policy rule exposure"
```

### Task 4: Final Verification

**Files:**
- Verify only; modify earlier files only if a check finds an in-scope defect.

**Interfaces:**
- Consumes: all contracts produced by Tasks 1-3.
- Produces: evidence that tests and repository quality gates pass.

- [ ] **Step 1: Run formatting and lint/type checks**

Run: `make lint`

Expected: exit code 0 with Ruff and ty clean.

- [ ] **Step 2: Re-run targeted tests after formatter effects**

Run:

```bash
uv run pytest \
  tests/schemas/test_trading_policy_schema.py \
  tests/services/test_trading_policy_service.py \
  tests/mcp_server/test_trading_policy_tool.py -q
```

Expected: all tests PASS.

- [ ] **Step 3: Validate the final diff and migration boundary**

Run:

```bash
git diff --check origin/main...HEAD
git diff --name-only origin/main...HEAD
git status --short
```

Expected: no whitespace errors; only the policy, schema, service, tests, MCP README, and superpowers design/plan docs differ; no Alembic revision exists; worktree is clean.

