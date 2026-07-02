# ROB-646 — Trading policy YAML single source + `get_trading_policy` + version stamping + buy-preview concentration cap

**Status:** design (approved decisions from 2026-07-02 scoping session + this brainstorm)
**Issue:** ROB-646 (parent ROB-644, project "auto_trader Trading Decision Workspace")
**Depends on:** ROB-643 (merged `a46b958e`) — the playbook `policy_keys:` block is the initial-value source.
**Migration:** 0 (no DB changes).

## Problem

Verdict/judgment thresholds (RSI bands, upside minimums, sector cap, order sizing,
recovery-gate count, etc.) live **only in prompt/context** today. They drift across
sessions and models because no tool enforces or even echoes them. ROB-643 captured
them **once** in the playbook `policy_keys:` block as an as-is baseline, explicitly
labelled "seed, not a second source of truth — ROB-646 YAML becomes authoritative
when it lands."

This work makes that YAML the single authoritative source, exposes it read-only via
MCP, stamps a policy version into run-start briefing, and adds the first (soft,
fail-open) coded enforcement: a sector-cluster concentration cap check on buy previews.

## Confirmed decisions (carried in)

- **Storage = repo-committed YAML, single source.** PR review is the approval gate;
  git is version/audit. Numbers are public (repo is public) — accepted. **No
  account-specific data** (account numbers, balances, asset size) in the YAML.
- **No write tools.** Operator edits via PR only. No `set_user_setting`-style ungated write.
- **Scoping:** market (kr/us/crypto) × lane (buy/sell/discovery).
- **Enforcement scope = hybrid.** Only the sector-cluster concentration cap (~9–10%)
  is coded, on buy previews, **warning-field-first, fail-open**. All other thresholds
  (RSI, resistance, upside, deep-limit, sizing…) are **advisory** — the tool echoes
  them; the session cites them.
- **Consumer stamping contract:** verdict records cite `policy_version` (explicit
  version + content hash) so "what criteria did we judge under?" is recoverable from
  report-item `evidence_snapshot`, `trade_retrospectives`, and forecast (P3, downstream).
  Run-start briefing echoes the policy version (lightweight pin).
- **Do NOT revive `trade_profile`** (dormant since ROB-488 `6bb1954d`). Declare
  authority instead (see Authority below) to prevent a 4th split-brain.
- **Do NOT migrate the fail-closed code guards** (loss guard `avg×1.01`,
  ladder near-market `0.3%`, RSI scoring bands). Those stay as code. The policy YAML
  is for **judgment thresholds only.**

## Brainstorm decisions

1. **YAML shape:** per-lane `defaults` (the captured values) + optional per-market
   overrides. `get_trading_policy(market, lane)` resolves override→default. No
   fabricated us/crypto numbers — KR-captured values are the shared default.
2. **Cap-check depth:** lean best-effort, fail-open. The raw-sector→cluster grouping
   lives **in the policy YAML** (it is a policy concern). Compute current + projected
   cluster weight from positions joined to `symbol_sectors`; any missing sector data
   or unmapped cluster ⇒ a fail-open field with a reason, never a block.
3. **Buy-path coverage:** wire the shared path (`kis_live_place_order` + crypto) **and**
   the Toss preview (Toss is the preferred, fee-free buy route and has its own preview).
4. **Briefing echo:** in scope — `get_operating_briefing` gains a lightweight
   `policy_version` field.
5. **Lane naming:** canonical lane is **`sell`** (matches the playbook `policy_keys`
   lane tags and ROB-649 `route_request` lane names). `profit_taking` is documented as
   the human alias — not a second key.

## Architecture

### 1. `config/trading_policy.yaml` (new, repo root)

Single authoritative source. Shape:

```yaml
version: "2026-07-02.1"          # explicit, bumped by operator on edit
captured_as_of: "2026-07-02"     # provenance of the seed values
source: "ROB-643 playbook policy_keys (seed); this file is now authoritative"

authority:                       # split-brain prevention (see Authority section)
  scope: judgment_thresholds_only
  governs: "advisory judgment thresholds + the sector-cluster concentration cap"
  does_not_govern:
    - "loss_guard code guard (order_validation.py avg*1.01) — stays fail-closed in code"
    - "ladder near-market guard (ladder_fill_safety.py 0.3%) — code"
    - "RSI scoring bands (scoring.py) — code"
    - "symbol_trade_settings — live per-symbol sizing (separate authority)"
    - "sell_conditions — dormant/test-only (not authoritative)"
    - "trade_profile — dead since ROB-488 (not revived)"

# raw-sector -> cluster grouping used by the concentration cap.
# keys under each cluster are matched against a symbol's symbol_sectors label
# (name_kr / name_en / source_key). Best-effort; unmapped sectors => fail-open.
sector_clusters:
  financials: ["금융", "은행", "증권", "보험", "Financial Services", "Banks"]
  shipbuilding_defense: ["조선", "방산", "Aerospace & Defense", ...]
  bio: ["제약", "바이오", "Biotechnology", "Drug Manufacturers", ...]
  semis_memory: ["반도체", "Semiconductors", ...]

# defaults: captured thresholds, each tagged with the lanes that reference it
# (exactly mirrors the playbook policy_keys `lanes:` membership).
thresholds:
  recovery_gate.min_conditions_met: {lanes: [buy], value: 2, unit: count, of: 4, semantics: "..."}
  portfolio.sector_cluster_cap_pct: {lanes: [buy, sell], value: 10, unit: percent, semantics: "..."}
  portfolio.max_symbols_per_theme: {lanes: [buy, discovery], value: 1, unit: count, semantics: "..."}
  order.day_expiry_kst: {lanes: [buy, sell], value: "20:00", unit: kst_time, semantics: "..."}
  buy.deep_limit_pct_range: {lanes: [buy], value: [-12, -3], unit: percent, semantics: "..."}
  buy.per_symbol_notional_krw_range: {lanes: [buy, discovery], value: [200000, 400000], unit: krw, semantics: "..."}
  sell.loss_guard_min_multiple: {lanes: [buy, sell], value: 1.01, unit: multiple_of_avg_cost, semantics: "..."}
  sell.breakeven_near_pct: {lanes: [sell], value: 2, unit: percent, semantics: "..."}
  sell.resistance_near_pct: {lanes: [sell], value: 6, unit: percent, semantics: "..."}
  sell.rsi_place_min: {lanes: [sell], value: 58, unit: rsi, semantics: "..."}
  sell.upside_place_max_pct: {lanes: [sell], value: 45, unit: percent, semantics: "..."}
  sell.watch_rsi_max: {lanes: [sell], value: 52, unit: rsi, semantics: "..."}
  sell.watch_upside_min_pct: {lanes: [sell], value: 50, unit: percent, semantics: "..."}
  screen.rsi_max: {lanes: [discovery], value: 45, unit: rsi, semantics: "..."}
  screen.support_within_pct: {lanes: [discovery], value: 8, unit: percent, semantics: "..."}
  screen.upside_min_pct: {lanes: [discovery], value: 40, unit: percent, semantics: "..."}

# per-market value overrides (empty at seed; KR is the captured baseline).
market_overrides:
  kr: {}
  us: {}
  crypto: {}   # equity-only keys (sector cap, RSI screens) may be marked N/A here later
```

All values above are transcribed verbatim from the ROB-643 `policy_keys:` block
(playbook lines 265–352). The `semantics:` strings are copied from the playbook.

### 2. `app/schemas/trading_policy.py` (new)

Pydantic DTOs, `extra="forbid"` (matches `app/schemas/session_context.py` convention):

- `PolicyThreshold` — `lanes: list[Lane]`, `value: <int|float|str|list>`, `unit: str`,
  `semantics: str`, optional `of: int`.
- `PolicyAuthority`, `TradingPolicyDocument` — top-level: `version`, `captured_as_of`,
  `source`, `authority`, `sector_clusters: dict[str, list[str]]`,
  `thresholds: dict[str, PolicyThreshold]`, `market_overrides: dict[Market, dict[str, Any]]`.
- `Lane = Literal["buy","sell","discovery"]`, `Market = Literal["kr","us","crypto"]`.
- Response DTO `TradingPolicyView` — `{version, content_hash, market, lane, thresholds: {...resolved...}}`.

### 3. `app/services/trading_policy_service.py` (new)

- `_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "trading_policy.yaml"`
  (`app/services/` → parents[2] = repo root; verify at impl time).
- `load_trading_policy() -> TradingPolicyDocument` — `yaml.safe_load` + validate.
  Load-once cache keyed by file mtime+size (so operator edits are picked up without
  a restart in dev, but no re-read per call).
- `policy_content_hash() -> str` — `sha256(raw_bytes).hexdigest()[:12]`.
- `policy_version_stamp() -> dict` — `{"version": ..., "content_hash": ...}`. This is
  the value consumers cite.
- `get_policy_for(market: str, lane: str) -> TradingPolicyView` — validate market/lane
  against the enums; **unknown market or lane raises `TradingPolicyKeyError`** (explicit,
  not silent). Collect thresholds whose `lanes` contains `lane`, apply
  `market_overrides[market][key]` when present (mark `source: default|override`), return
  the view with `version` + `content_hash`.
- `sector_cluster_for(label: str) -> str | None` — reverse lookup of `sector_clusters`
  (case-insensitive substring/exact match, best-effort). Returns `None` when unmapped.

### 4. `evaluate_sector_concentration(...)` in `app/mcp_server/tooling/order_validation.py`

The shared, fail-open concentration helper (async — needs positions + sector I/O):

```python
async def evaluate_sector_concentration(
    db, *, symbol: str, market: str, order_value_krw: float | None, account_ctx
) -> dict:
    # returns one of:
    #   {"verdict": "within", "cluster": ..., "cap_pct": 10, "current_pct": ..., "projected_pct": ..., "fail_open": False}
    #   {"verdict": "over",   "cluster": ..., "cap_pct": 10, "current_pct": ..., "projected_pct": ..., "fail_open": False, "warning": "..."}
    #   {"verdict": "unknown", "fail_open": True, "reason": "sector data missing for X" | "no cluster mapping" | "crypto (no sectors)" | "<exc>"}
```

- Reads `cap_pct` from `get_policy_for(market, "buy").thresholds["portfolio.sector_cluster_cap_pct"]`.
- Best-effort current sector-cluster weights: reuse existing portfolio positions
  (via `portfolio_allocation_service` positions collector), join each held symbol to
  `symbol_sectors` (universe `sector_id` → `SymbolSector`, the `screener_service.py`
  outerjoin pattern), map label → cluster via `sector_cluster_for`, aggregate
  `value_krw` per cluster / total.
- Projected weight = (current cluster value + this order's KRW value) / (total + order value).
- **Fail-open everywhere:** crypto market, symbol with no sector, unmapped cluster, or
  ANY exception ⇒ `{"verdict": "unknown", "fail_open": True, "reason": ...}`. Never
  raises, never blocks.

### 5. Wire into buy previews (two call sites, one helper)

- **Shared path** — `order_execution._place_order_impl`, buy side only, after the
  `_check_balance_and_warn` block (~lines 1086–1103): call the helper, attach the result
  as `dry_run_result["sector_concentration"]`. `_build_dry_run_response` already splats
  `**dry_run_result`, so it surfaces automatically. `verdict == "over"` sets an
  additional soft `warning` string; it does **not** flip `success` to False.
- **Toss path** — `orders_toss_variants.toss_preview_order` (response assembly ~line 745)
  and `_toss_place_order_impl` dry-run return (~line 859): attach the same
  `sector_concentration` key. Buy side only.

### 6. `get_trading_policy` MCP tool (read-only)

Three-file split (matches session_context convention):
- `app/mcp_server/tooling/trading_policy_tools.py` — `async def get_trading_policy(market, lane) -> dict`.
  Validates via the request DTO; on unknown market/lane returns an **explicit error dict**
  (`{"success": False, "error": "unknown_key", ...}`) rather than raising; on success
  returns `TradingPolicyView.model_dump(mode="json")` including `version` + `content_hash`.
- `app/mcp_server/tooling/trading_policy_registration.py` — `register_trading_policy_tools(mcp)`
  + `TRADING_POLICY_TOOL_NAMES`. Description states the version-stamping contract.
- `app/mcp_server/tooling/registry.py` — import + call in the **always-registered**
  read-only block (appears in DEFAULT and every profile), NOT the profile-gated branch.

### 7. Briefing version echo

`get_operating_briefing` handler gains `policy_version = policy_version_stamp()`
(`{version, content_hash}`) as an additive top-level field. Fail-open: if the policy
file fails to load, the field is `None` with a reason and the briefing still returns.

### 8. Docs

- Update `docs/playbooks/trading-decision-playbook.md`: add an **Authority** note (this
  YAML now authoritative; `profit_taking` = alias of `sell` lane) — the playbook already
  flagged itself as "seed, not source" so this closes that loop.
- Update `app/mcp_server/README.md` (or the tooling README): document `get_trading_policy`
  + the version-stamping contract (consumers cite `{version, content_hash}` from the tool
  or briefing in `evidence_snapshot` / `trade_retrospectives` / forecast).
- Add the authority declaration referenced by the issue (jurisdiction vs
  sell_conditions / symbol_trade_settings / trade_profile).

### 9. Dependency

Add `pyyaml` (+ `types-PyYAML` in the dev/typing group) as explicit deps in
`pyproject.toml`. It is present transitively today but app code should not rely on that.

## Out of scope (explicit)

- Wiring `policy_version` into `report_item.evidence_snapshot` / `trade_retrospectives`
  write paths — this PR provides the **source** + documents the **contract** + briefing
  echo. Downstream consumers cite it in their own issues (forecast = P3).
- Any write/edit tool for the policy (operator PR only).
- Reviving `trade_profile`.
- Migrating the fail-closed code guards into YAML.

## Testing

- **service:** valid load; `content_hash` stable across calls, changes when file bytes
  change; unknown market/lane raises `TradingPolicyKeyError`; override resolution marks
  `source`; `version` present; `sector_cluster_for` mapping + `None` for unmapped.
- **schema:** `extra="forbid"` rejects unknown top-level / threshold keys.
- **tool:** returns thresholds + version for a valid (market, lane); unknown key ⇒
  explicit error dict (AC).
- **concentration helper:** `within` / `over` / `unknown`; fail-open on missing sector,
  unmapped cluster, crypto, and injected exception; never raises; `over` never blocks.
- **preview wiring:** shared path (kis_live/crypto) and Toss preview both include
  `sector_concentration`; crypto ⇒ fail-open field; `over` keeps `success: True`.
- **briefing:** `policy_version` present; fail-open when file unreadable.
- **registration:** `get_trading_policy` registered in DEFAULT profile (reuse the
  `DummyMCP` + `register_all_tools` matrix pattern used by `tests/test_playbook_tool_names.py`).

## Acceptance criteria (from issue)

- [x] `get_trading_policy` returns market×lane thresholds + version; unknown key ⇒ explicit error.
- [x] Buy preview response has a concentration-check field (cap / current weight / verdict);
      fail-open + reason on missing sector data.
- [x] Version-stamping contract stated in tool description + README.
- [x] migration 0.
