# A′ Shadow Replay Harness (M1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure whether a headless `claude -p` session, fed ONLY a frozen evidence bundle, reproduces a disciplined buy/sell decision — both across K repeats (self-consistency) and against the decision that was actually shipped for that bundle (ground-truth fidelity).

**Architecture:** Three seams. (1) A new read-only MCP profile `shadow-replay` that exposes only the frozen-context read + policy/lane procedure tools and omits every live-fetch and mutation tool (so the replay can't leak live market data — the load-bearing validity guard). (2) A `scripts/` subprocess driver that launches a dedicated MCP server on that profile and drives `claude -p` K times per bundle, capturing each proposed decision as JSON. (3) A pure-function scorer in `app/services/` (no LLM, no I/O) that computes self-consistency + ground-truth diff. Linear: ROB-697. This is M1 only; M2 (Upbit shadow-sim) and M3 (kis_mock port) are out of scope.

**Tech Stack:** Python 3.13, `uv`, FastMCP (existing MCP server), pytest (markers: unit/integration/slow/live only), ruff + ty, PostgreSQL (`review` schema). `claude -p` (Claude Code headless) as an out-of-process subprocess — the FIRST such driver in this repo, deliberately in `scripts/` not `app/`.

## Global Constraints

- **ROB-501 runtime LLM boundary:** no in-process/subprocess LLM in `app/**`. The `claude -p` driver lives in `scripts/`; the scorer in `app/services/` is pure deterministic functions with zero LLM/network. No `anthropic` SDK is added to `app/`.
- **Read-only:** M1 places no orders and writes no reports. The `shadow-replay` profile exposes NO mutation/order/report-write tools; the headless agent returns its decision as JSON on stdout, it does not persist.
- **`get_hermes_context` gate:** the tool only exists when `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true` on the MCP host. The shadow-replay server must set it.
- **Pytest markers are strict** (`--strict-markers --strict-config`); only `unit`, `integration`, `slow`, `live` are registered. Do not invent a marker.
- **Lint/type gates:** `uv run ruff check app/ tests/` + `uv run ruff format --check app/ tests/` + `uv run ty check app/ --error-on-warning` (ty checks `app/` only). ruff line-length 88, target py313.
- **Determinism caveat (documented, not fully solved in M1):** `HermesContextExporter.export()` reads `dimension_evidence` / `dimension_reports` / `market_session` from LIVE tables (screener/research/valuation/investor-flow snapshots + stage-run rows), NOT the frozen bundle. Only `stage_inputs` + `cited_snapshots` are fully frozen. M1 mitigates by running all K replays of a bundle in one tight batch; the clean fix (thread a fixed `now=` + freeze aux tables) is noted as M1-follow-up, not built here.

---

## File Structure

- `app/mcp_server/profiles.py` — **modify**: add `SHADOW_REPLAY = "shadow-replay"` enum member.
- `app/mcp_server/tooling/registry.py` — **modify**: add an early-return `shadow-replay` branch at the top of `register_all_tools` that registers ONLY the allowlist (frozen-context read + policy + route_request) and returns, skipping the Always block and all order surfaces.
- `app/mcp_server/tooling/investment_hermes_handlers.py` — **modify**: add a narrow `register_hermes_context_read_only(mcp)` that registers ONLY `investment_report_get_hermes_context` (not the 4 Hermes write tools), reused by the shadow-replay branch.
- `app/services/shadow_replay/__init__.py`, `app/services/shadow_replay/scoring.py` — **create**: pure-function scorer (decision extraction + agreement metrics). No I/O.
- `app/services/shadow_replay/corpus.py` — **create**: read-only SQL/ORM helpers to census + select the replay corpus (bundle-backed items, authorship + auto_emit filters).
- `scripts/shadow_replay.py` — **create**: the headless driver (launch server config, run `claude -p` K times, collect JSON, call scorer, emit report).
- `scripts/shadow_replay_mcp.json` — **create**: the `--mcp-config` for `claude -p` (stdio server on the shadow-replay profile).
- `tests/mcp_server/test_shadow_replay_profile.py` — **create**: profile registration allowlist/deny tests (DummyMCP).
- `tests/services/shadow_replay/test_scoring.py` — **create**: pure-function scorer tests.
- `tests/services/shadow_replay/test_corpus.py` — **create**: corpus filter tests (integration, DB).
- `tests/test_shadow_replay_cli.py` — **create**: driver unit tests (monkeypatched subprocess).
- `docs/runbooks/shadow-replay.md` — **create**: how to run P0 census + a replay batch.

---

## Task 0 (P0): Corpus census — decide what "reference decision" means BEFORE building

**Why first:** the design assumed "my human-authored decisions with a frozen bundle." Grounding found these are in TENSION: `investment_report_create` (plain human authoring) NEVER sets `snapshot_bundle_uuid`; non-null bundles come only from `generate_from_bundle` / Hermes ingest. So `created_by_profile='CLAUDE_ADVISOR' AND snapshot_bundle_uuid IS NOT NULL` may be nearly empty. This task measures reality and picks the corpus. It is a read-only investigation, not TDD.

**Files:**
- Create: `app/services/shadow_replay/corpus.py`
- Create: `tests/services/shadow_replay/test_corpus.py`
- Reference (read-only): `app/models/investment_reports.py` (InvestmentReport `review.investment_reports`, InvestmentReportItem `review.investment_report_items`, InvestmentReportItemDecision `review.investment_report_item_decisions`), `app/services/investment_reports/repository.py:175`, `app/services/action_report/snapshot_backed/auto_emit.py:103`.

**Interfaces:**
- Produces: `select_replay_corpus(session, *, min_per_kind: int = 1, limit: int = 40) -> CorpusSelection` where `CorpusSelection = {"source": "claude_bundle"|"hermes_bundle"|"operator_audit", "items": list[CorpusItem]}` and `CorpusItem = {"snapshot_bundle_uuid": str, "report_id": int, "item_uuid": str, "item_kind": str, "intent": str, "reference_decision": dict}`. `reference_decision` is the output of `extract_decision(...)` from Task 3.

- [ ] **Step 1: Run the census SELECT by hand (read-only) against the real DB to see what exists.**

Run (adjust `DATABASE_URL` to the real read replica / local mirror per `docs/runbooks/` conventions):

```sql
-- Census: bundle-backed items by authorship, minus auto_emit machine items
SELECT r.created_by_profile,
       COUNT(*) FILTER (WHERE r.snapshot_bundle_uuid IS NOT NULL)                                   AS with_bundle,
       COUNT(*) FILTER (WHERE r.snapshot_bundle_uuid IS NOT NULL
                        AND COALESCE(it.evidence_snapshot->>'source','') <> 'auto_emit'
                        AND COALESCE(it.evidence_snapshot->>'proposer','') NOT LIKE 'auto_emit/%')  AS with_bundle_non_autoemit,
       COUNT(*) FILTER (WHERE it.side IN ('buy','sell'))                                             AS actionable
FROM review.investment_report_items it
JOIN review.investment_reports r ON r.id = it.report_id
GROUP BY r.created_by_profile
ORDER BY with_bundle_non_autoemit DESC;

-- Also count the truer "operator actually shipped this" signal
SELECT decision, COUNT(*)
FROM review.investment_report_item_decisions
WHERE decision IN ('approve','partial_approve','reprice')
GROUP BY decision;
```

Expected: one of three outcomes decides the corpus source (Step 2).

- [ ] **Step 2: Pick the corpus source from the census (decision gate — record the choice in `docs/runbooks/shadow-replay.md`).**

Decision rule:
1. If `CLAUDE_ADVISOR` has ≥ (3×`min_per_kind`) `with_bundle_non_autoemit` rows spanning buy+sell+watch → **source = `claude_bundle`** (closest to "my decision").
2. Else if `HERMES_ADVISOR` has enough such rows → **source = `hermes_bundle`**. This still directly serves ROB-644's stated goal ("different LLM models should reach the same result"): A′ then measures headless-Claude vs Hermes consistency on identical frozen evidence. Note this framing explicitly in the report.
3. Else → **source = `operator_audit`**: join `investment_report_item_decisions` (`decision IN ('approve','partial_approve','reprice')`, `approved_payload_snapshot` is the verbatim shipped params) back to items with a non-null bundle. Truest "shipped" signal but requires the report to have a bundle; if operator-audit rows also lack bundles, STOP and report to the user that no replayable corpus exists yet (A′ can't run until bundle-backed decisions accumulate).

- [ ] **Step 3: Write `corpus.py` implementing the chosen selection (parameterized so all three sources are code paths; the census picks which one runs).**

```python
# app/services/shadow_replay/corpus.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.investment_reports import (
    InvestmentReport, InvestmentReportItem, InvestmentReportItemDecision,
)
from app.services.shadow_replay.scoring import extract_decision

_HUMAN_PROFILE = "CLAUDE_ADVISOR"
_HERMES_PROFILE = "HERMES_ADVISOR"


@dataclass(frozen=True)
class CorpusItem:
    snapshot_bundle_uuid: str
    report_id: int
    item_uuid: str
    item_kind: str
    intent: str
    reference_decision: dict[str, Any]


@dataclass(frozen=True)
class CorpusSelection:
    source: str  # "claude_bundle" | "hermes_bundle" | "operator_audit"
    items: list[CorpusItem]


def _non_autoemit(item: InvestmentReportItem) -> bool:
    ev = item.evidence_snapshot or {}
    if ev.get("source") == "auto_emit":
        return False
    proposer = str(ev.get("proposer", ""))
    return not proposer.startswith("auto_emit/") and proposer != "intraday_floor"


async def _bundle_items_for_profile(
    session: AsyncSession, profile: str, limit: int
) -> list[CorpusItem]:
    stmt = (
        select(InvestmentReportItem, InvestmentReport.snapshot_bundle_uuid)
        .join(InvestmentReport, InvestmentReport.id == InvestmentReportItem.report_id)
        .where(InvestmentReport.snapshot_bundle_uuid.isnot(None))
        .where(InvestmentReport.created_by_profile == profile)
        .order_by(InvestmentReport.id.desc())
        .limit(limit * 4)  # over-fetch; auto_emit filter is in Python
    )
    rows = (await session.execute(stmt)).all()
    out: list[CorpusItem] = []
    for item, bundle_uuid in rows:
        if not _non_autoemit(item):
            continue
        out.append(
            CorpusItem(
                snapshot_bundle_uuid=str(bundle_uuid),
                report_id=item.report_id,
                item_uuid=str(item.item_uuid),
                item_kind=item.item_kind,
                intent=item.intent,
                reference_decision=extract_decision(item),
            )
        )
        if len(out) >= limit:
            break
    return out


async def select_replay_corpus(
    session: AsyncSession, *, min_per_kind: int = 1, limit: int = 40
) -> CorpusSelection:
    claude = await _bundle_items_for_profile(session, _HUMAN_PROFILE, limit)
    if _covers_kinds(claude, min_per_kind):
        return CorpusSelection(source="claude_bundle", items=claude)
    hermes = await _bundle_items_for_profile(session, _HERMES_PROFILE, limit)
    if _covers_kinds(hermes, min_per_kind):
        return CorpusSelection(source="hermes_bundle", items=hermes)
    # operator_audit fallback intentionally raises for the human to decide (Step 2 rule 3)
    raise CorpusUnavailable(
        "No bundle-backed non-auto_emit corpus with buy+sell+watch coverage; "
        "run the P0 census and decide the corpus manually."
    )


def _covers_kinds(items: list[CorpusItem], min_per_kind: int) -> bool:
    from collections import Counter
    c = Counter(i.item_kind for i in items)
    return all(c.get(k, 0) >= min_per_kind for k in ("action", "watch"))


class CorpusUnavailable(RuntimeError):
    pass
```

- [ ] **Step 4: Write an integration test that seeds 1 bundle-backed CLAUDE_ADVISOR item + 1 auto_emit item and asserts only the former is selected.**

```python
# tests/services/shadow_replay/test_corpus.py
import pytest
from app.services.shadow_replay.corpus import select_replay_corpus, _non_autoemit

@pytest.mark.integration
async def test_autoemit_item_excluded(db_session, seed_report_item):
    human = await seed_report_item(created_by_profile="CLAUDE_ADVISOR", with_bundle=True,
                                   item_kind="action", side="buy", evidence={})
    await seed_report_item(created_by_profile="CLAUDE_ADVISOR", with_bundle=True,
                           item_kind="action", side="buy",
                           evidence={"source": "auto_emit", "proposer": "auto_emit/buy_from_candidate"})
    sel = await select_replay_corpus(db_session, min_per_kind=1, limit=10)
    uuids = {i.item_uuid for i in sel.items}
    assert str(human.item_uuid) in uuids
    assert all(i.reference_decision.get("proposer") != "auto_emit/buy_from_candidate" for i in sel.items)

@pytest.mark.unit
def test_non_autoemit_predicate():
    class _I: evidence_snapshot = {"source": "auto_emit"}
    assert _non_autoemit(_I()) is False
```

- [ ] **Step 5: Run + commit.**

Run: `uv run pytest tests/services/shadow_replay/test_corpus.py -v --no-cov`
Expected: PASS (unit) / PASS (integration if DB reachable; else `-m unit` only).

```bash
git add app/services/shadow_replay/corpus.py tests/services/shadow_replay/test_corpus.py docs/runbooks/shadow-replay.md
git commit -m "feat(ROB-697): P0 replay-corpus selection + census (bundle-backed, non-auto_emit)"
```

---

## Task 1 (T3): Pure-function decision scorer

**Why before the runner:** the scorer has zero external deps (stdlib only, mirror `app/services/brokers/kis/live_order_expiry.py`), so it is pure TDD and both the corpus (Task 0) and the runner (Task 4) consume it.

**Files:**
- Create: `app/services/shadow_replay/scoring.py`
- Create: `tests/services/shadow_replay/test_scoring.py`
- Reference: `app/services/investment_reports/ingestion.py:48` (trade_setup shape), `app/schemas/investment_reports.py:215` (max_action fields).

**Interfaces:**
- Produces:
  - `extract_decision(item_or_dict) -> dict` → normalized `{"side": "buy"|"sell"|None, "notional": Decimal|None, "quantity": Decimal|None, "limit_price": Decimal|None, "entry": Decimal|None, "stop": Decimal|None, "target": Decimal|None, "triggers": frozenset[str], "proposer": str|None}`. Reads `side` column, `max_action` JSONB (quantity/notional/limit_price), `evidence_snapshot.trade_setup` (Decimals are STRINGS → cast), and `trigger_checklist`.
  - `agree(a: dict, b: dict, *, tick: Decimal, atr: Decimal|None = None) -> dict` → `{"side": bool, "size_band": bool, "limit": bool, "triggers_jaccard": float, "same_decision": bool}`.
  - `summarize(decisions: list[dict], reference: dict|None, *, tick, atr) -> dict` → `{"k": int, "no_action_rate": float, "self_same_decision_rate": float, "fidelity": dict|None}`.

- [ ] **Step 1: Write failing tests for `extract_decision` (stringified Decimals + JSONB paths).**

```python
# tests/services/shadow_replay/test_scoring.py
from decimal import Decimal
import pytest
from app.services.shadow_replay.scoring import extract_decision, agree, summarize

@pytest.mark.unit
def test_extract_reads_trade_setup_stringified_decimals():
    item = {
        "side": "buy",
        "max_action": {"notional": "300000", "limit_price": "129600"},
        "evidence_snapshot": {"trade_setup": {"stop": "125000", "target": "150000",
                              "headline": {"entry": "129600"}}},
        "trigger_checklist": ["support_129600_hold", "rsi_below_45"],
    }
    d = extract_decision(item)
    assert d["side"] == "buy"
    assert d["notional"] == Decimal("300000")
    assert d["limit_price"] == Decimal("129600")
    assert d["entry"] == Decimal("129600")
    assert d["stop"] == Decimal("125000")
    assert d["triggers"] == frozenset({"support_129600_hold", "rsi_below_45"})

@pytest.mark.unit
def test_extract_no_action_when_no_side():
    d = extract_decision({"side": None, "max_action": {}, "evidence_snapshot": {}, "trigger_checklist": []})
    assert d["side"] is None
```

- [ ] **Step 2: Run to verify fail.** Run: `uv run pytest tests/services/shadow_replay/test_scoring.py -v --no-cov` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement `scoring.py`.**

```python
# app/services/shadow_replay/scoring.py
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from typing import Any


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def extract_decision(item: Any) -> dict[str, Any]:
    side = _get(item, "side")
    max_action = _get(item, "max_action") or {}
    ev = _get(item, "evidence_snapshot") or {}
    setup = (ev.get("trade_setup") or {}) if isinstance(ev, dict) else {}
    headline = setup.get("headline") or {}
    triggers = _get(item, "trigger_checklist") or []
    return {
        "side": side if side in ("buy", "sell") else None,
        "notional": _dec(max_action.get("notional")),
        "quantity": _dec(max_action.get("quantity")),
        "limit_price": _dec(max_action.get("limit_price")),
        "entry": _dec(headline.get("entry")),
        "stop": _dec(setup.get("stop")),
        "target": _dec(setup.get("target")),
        "triggers": frozenset(str(t) for t in triggers),
        "proposer": (ev.get("proposer") if isinstance(ev, dict) else None),
    }


def _within(a: Decimal | None, b: Decimal | None, tol: Decimal) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


def _size_band(a: dict, b: dict) -> bool:
    # same order of magnitude on notional (or both None); size is coarse by design
    an, bn = a.get("notional"), b.get("notional")
    if an is None or bn is None:
        return an is None and bn is None
    hi, lo = max(an, bn), min(an, bn)
    return lo > 0 and hi / lo <= Decimal("1.5")


def agree(a: dict, b: dict, *, tick: Decimal, atr: Decimal | None = None) -> dict[str, Any]:
    side = a["side"] == b["side"]
    limit_tol = (atr / Decimal(4)) if atr else (tick * 3)
    limit = _within(a.get("limit_price"), b.get("limit_price"), limit_tol)
    ta, tb = a["triggers"], b["triggers"]
    jac = (len(ta & tb) / len(ta | tb)) if (ta or tb) else 1.0
    size_band = _size_band(a, b)
    same = side and size_band and limit and jac >= 0.6
    return {"side": side, "size_band": size_band, "limit": limit,
            "triggers_jaccard": jac, "same_decision": same}


def summarize(decisions: list[dict], reference: dict | None, *, tick: Decimal,
              atr: Decimal | None = None) -> dict[str, Any]:
    k = len(decisions)
    no_action = sum(1 for d in decisions if d["side"] is None)
    # self-consistency: pairwise same_decision vs the modal decision (decisions[0] as anchor)
    anchor = decisions[0] if decisions else None
    self_same = (
        sum(1 for d in decisions if anchor and agree(anchor, d, tick=tick, atr=atr)["same_decision"]) / k
        if k else 0.0
    )
    fidelity = None
    if reference is not None and k:
        matches = [agree(reference, d, tick=tick, atr=atr) for d in decisions]
        fidelity = {
            "side_rate": sum(m["side"] for m in matches) / k,
            "size_band_rate": sum(m["size_band"] for m in matches) / k,
            "limit_rate": sum(m["limit"] for m in matches) / k,
            "same_decision_rate": sum(m["same_decision"] for m in matches) / k,
        }
    return {"k": k, "no_action_rate": (no_action / k if k else 0.0),
            "self_same_decision_rate": self_same, "fidelity": fidelity}
```

- [ ] **Step 4: Add tests for `agree` (tolerance + Jaccard + no-action) and `summarize` (no-action rate visible).**

```python
@pytest.mark.unit
def test_agree_limit_tolerance_and_side():
    a = {"side": "buy", "notional": Decimal("300000"), "limit_price": Decimal("129600"),
         "triggers": frozenset({"x"})}
    b = {"side": "buy", "notional": Decimal("320000"), "limit_price": Decimal("129700"),
         "triggers": frozenset({"x"})}
    r = agree(a, b, tick=Decimal("100"))
    assert r["side"] and r["size_band"] and r["limit"] and r["same_decision"]

@pytest.mark.unit
def test_summarize_exposes_no_action_rate():
    hold = {"side": None, "triggers": frozenset()}
    s = summarize([hold, hold], reference=None, tick=Decimal("100"))
    assert s["no_action_rate"] == 1.0
    assert s["self_same_decision_rate"] == 1.0  # degenerate agreement is VISIBLE via no_action_rate
```

- [ ] **Step 5: Run + lint + commit.**

Run: `uv run pytest tests/services/shadow_replay/test_scoring.py -v --no-cov` — Expected: PASS.
Run: `uv run ruff check app/ tests/ && uv run ty check app/ --error-on-warning` — Expected: clean.

```bash
git add app/services/shadow_replay/scoring.py app/services/shadow_replay/__init__.py tests/services/shadow_replay/test_scoring.py
git commit -m "feat(ROB-697): pure-function decision scorer (extract_decision/agree/summarize)"
```

---

## Task 2 (T1): `shadow-replay` MCP profile (live-tool denial)

**Files:**
- Modify: `app/mcp_server/profiles.py:12` (enum) and `:21` (resolve auto-accepts).
- Modify: `app/mcp_server/tooling/registry.py:125` (early-return branch).
- Modify: `app/mcp_server/tooling/investment_hermes_handlers.py` (narrow read-only registrar).
- Create: `tests/mcp_server/test_shadow_replay_profile.py`.
- Reference: `tests/_mcp_tooling_support.py` (DummyMCP), `tests/test_mcp_profiles.py` (pattern, `TestResolveMcpProfile`, `TestOrderSurfaceMatrix`).

**Interfaces:**
- Produces: profile `McpProfile.SHADOW_REPLAY`; when active, the server registers EXACTLY `{investment_report_get_hermes_context, get_trading_policy, route_request}` and nothing else (no live-fetch, no news, no screeners, no orders, no report-write, no Hermes write tools).

- [ ] **Step 1: Add the enum member.**

```python
# app/mcp_server/profiles.py — inside class McpProfile(StrEnum)
    SHADOW_REPLAY = "shadow-replay"
```

- [ ] **Step 2: Add a narrow read-only Hermes-context registrar (avoid exposing the 4 Hermes WRITE tools).**

In `app/mcp_server/tooling/investment_hermes_handlers.py`, add near `register_investment_hermes_tools`:

```python
def register_hermes_context_read_only(mcp) -> None:
    """Register ONLY the frozen-context read tool (no prepare/compose/ingest write tools)."""
    mcp.tool(
        name="investment_report_get_hermes_context",
        description="Return the frozen Hermes decision context for a snapshot bundle (read-only).",
    )(investment_report_get_hermes_context_impl)
```

- [ ] **Step 3: Add the early-return branch at the TOP of `register_all_tools` (before the Always block).**

```python
# app/mcp_server/tooling/registry.py — first lines inside register_all_tools(mcp, profile)
    if profile is McpProfile.SHADOW_REPLAY:
        # Frozen-context replay ONLY: read the bundle + policy + lane procedure.
        # Deliberately NO live-fetch (market_data/analysis/news), NO mutation,
        # NO report-write. The agent returns its decision as JSON; it does not persist.
        register_hermes_context_read_only(mcp)   # investment_report_get_hermes_context
        register_trading_policy_tools(mcp)        # get_trading_policy (versioned thresholds)
        register_route_request_tools(mcp)         # route_request (lane procedure)
        return
```

(Import `register_hermes_context_read_only` at the top of registry.py alongside the other hermes imports.)

- [ ] **Step 4: Write the profile registration test FIRST-style (allowlist ⊆, live-fetch disjoint).**

```python
# tests/mcp_server/test_shadow_replay_profile.py
from typing import Any, cast
import pytest
from app.mcp_server.profiles import McpProfile, resolve_mcp_profile
from app.mcp_server.tooling.registry import register_all_tools
from tests._mcp_tooling_support import DummyMCP

_ALLOWED = {"investment_report_get_hermes_context", "get_trading_policy", "route_request"}
_FORBIDDEN = {"get_quote", "get_ohlcv", "get_orderbook", "screen_stocks", "get_news",
              "investment_report_create", "place_order", "kis_mock_place_order",
              "investment_report_create_from_hermes_composition"}

@pytest.mark.unit
def test_shadow_replay_exposes_only_allowlist(monkeypatch):
    monkeypatch.setenv("SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", "true")
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.SHADOW_REPLAY)
    names = set(mcp.tools.keys())
    assert names == _ALLOWED, f"unexpected tools: {names ^ _ALLOWED}"
    assert _FORBIDDEN.isdisjoint(names)

@pytest.mark.unit
def test_resolve_shadow_replay():
    assert resolve_mcp_profile("shadow-replay") is McpProfile.SHADOW_REPLAY
```

- [ ] **Step 5: Run to verify (fails until Steps 1-3 applied, then passes). Also update `TestOrderSurfaceMatrix`/`TestResolveMcpProfile` in `tests/test_mcp_profiles.py` so the set-equality matrix (parametrized over `list(McpProfile)`) knows shadow-replay registers zero order surfaces.**

In `tests/test_mcp_profiles.py`, add `McpProfile.SHADOW_REPLAY: set()` to `_ORDER_SURFACE_MATRIX` (empty = no order tools).

Run: `uv run pytest tests/mcp_server/test_shadow_replay_profile.py tests/test_mcp_profiles.py -v --no-cov`
Expected: PASS (including the parametrized matrix — it fails on any drift).

- [ ] **Step 6: Lint + commit.**

Run: `uv run ruff check app/ tests/ && uv run ty check app/ --error-on-warning` — Expected: clean.

```bash
git add app/mcp_server/profiles.py app/mcp_server/tooling/registry.py \
        app/mcp_server/tooling/investment_hermes_handlers.py \
        tests/mcp_server/test_shadow_replay_profile.py tests/test_mcp_profiles.py
git commit -m "feat(ROB-697): shadow-replay MCP profile (frozen-context read + policy + route_request only)"
```

---

## Task 3 (P1): Frozen-context reproducibility check

**Why:** A′ is only valid if the same bundle yields the same decision-bearing context across replays. Grounding: `get_hermes_context` emits no verbatim timestamp; `stage_inputs` + `cited_snapshots` are fully frozen; `dimension_evidence`/`dimension_reports`/`market_session` read LIVE tables and can drift. This task pins the reproducible surface an M1 batch relies on.

**Files:**
- Create: `scripts/shadow_replay_probe.py` (a tiny read-only probe that calls the tool twice and diffs).
- Reference: `app/mcp_server/tooling/investment_hermes_handlers.py:162`, `app/schemas/hermes_composition.py:76`.

- [ ] **Step 1: Add a probe function that calls the impl twice and diffs the FROZEN sections.**

```python
# scripts/shadow_replay_probe.py
import asyncio, json, sys
from app.mcp_server.tooling.investment_hermes_handlers import (
    investment_report_get_hermes_context_impl as get_ctx,
)

_FROZEN_KEYS = ("stage_inputs", "cited_snapshots", "policy_version",
                "market", "market_session", "coverage_summary")

async def probe(bundle_uuid: str) -> int:
    a = await get_ctx(bundle_uuid)
    b = await get_ctx(bundle_uuid)
    if not a.get("success"):
        print(json.dumps({"error": a}, ensure_ascii=False)); return 2
    frozen_a = {k: a.get(k) for k in _FROZEN_KEYS}
    frozen_b = {k: b.get(k) for k in _FROZEN_KEYS}
    identical = json.dumps(frozen_a, sort_keys=True) == json.dumps(frozen_b, sort_keys=True)
    drift = {k: [a.get(k), b.get(k)] for k in ("dimension_evidence", "dimension_reports")
             if json.dumps(a.get(k), sort_keys=True) != json.dumps(b.get(k), sort_keys=True)}
    print(json.dumps({"frozen_identical": identical, "live_section_drift": list(drift)},
                     ensure_ascii=False))
    return 0 if identical else 1

if __name__ == "__main__":
    raise SystemExit(asyncio.run(probe(sys.argv[1])))
```

- [ ] **Step 2: Run against one bundle_uuid from Task 0's corpus (server env: SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true).**

Run: `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true uv run python -m scripts.shadow_replay_probe <bundle_uuid>`
Expected: `{"frozen_identical": true, "live_section_drift": []}` for two calls seconds apart. If `live_section_drift` is non-empty, record it — it means the K-replay batch (Task 4) must run tight in time; the decision prompt (Task 4) must instruct the agent to base its call on `stage_inputs`/`cited_snapshots` (the frozen sections).

- [ ] **Step 3: Commit the probe + record the P1 result in the runbook.**

```bash
git add scripts/shadow_replay_probe.py docs/runbooks/shadow-replay.md
git commit -m "feat(ROB-697): P1 frozen-context reproducibility probe"
```

---

## Task 4 (T2 + T4): Headless `claude -p` driver + report

**Files:**
- Create: `scripts/shadow_replay.py`, `scripts/shadow_replay_mcp.json`.
- Create: `tests/test_shadow_replay_cli.py`.
- Reference: `scripts/kiwoom_mock_smoke.py` (CLI/monkeypatch pattern), `app/mcp_server/main.py:108` (transport), the `.hermes/plans/*.md` `claude -p` invocation shape (planner precedent, docs only).

**Interfaces:**
- Consumes: `select_replay_corpus` (Task 0), `summarize`/`extract_decision` (Task 1), the `shadow-replay` profile (Task 2).
- Produces: `run_batch(corpus, *, k: int, model: str, tick: Decimal) -> list[dict]` and a markdown report writer `write_report(results, path)`.

- [ ] **Step 1: Author the stdio MCP config for `claude -p`.**

```json
// scripts/shadow_replay_mcp.json
{
  "mcpServers": {
    "shadow-replay": {
      "command": "uv",
      "args": ["run", "python", "-m", "app.mcp_server.main"],
      "env": {
        "MCP_TYPE": "stdio",
        "MCP_PROFILE": "shadow-replay",
        "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED": "true"
      }
    }
  }
}
```

(stdio avoids the HTTP `x-paperclip-agent-id` header requirement; DB creds come from the ambient `.env` the server already reads.)

- [ ] **Step 2: Write the driver with a pinned model, JSON output contract, and MCP-reset discard.**

```python
# scripts/shadow_replay.py  (excerpt — full CLI mirrors scripts/kiwoom_mock_smoke.py)
import argparse, json, subprocess
from decimal import Decimal
from pathlib import Path

_MCP_CONFIG = str(Path(__file__).with_name("shadow_replay_mcp.json"))
_RESET_MARKERS = ("get_hermes_context", "connection error", "tool not found")

_PROMPT = (
    "You are replaying a FROZEN trading-decision context. Call "
    "investment_report_get_hermes_context with snapshot_bundle_uuid={uuid}. "
    "Base your decision ONLY on its stage_inputs and cited_snapshots (the frozen "
    "evidence) plus get_trading_policy thresholds and the route_request lane. Do NOT "
    "call any other tool. Output ONLY a JSON object: "
    '{{"side": "buy"|"sell"|null, "max_action": {{"notional": <num|null>, '
    '"limit_price": <num|null>}}, "trade_setup": {{"stop": <num|null>, '
    '"target": <num|null>, "headline": {{"entry": <num|null>}}}}, '
    '"trigger_checklist": [<str>...]}}'
)

def _one_run(uuid: str, model: str) -> dict | None:
    proc = subprocess.run(
        ["claude", "-p", "--model", model, "--mcp-config", _MCP_CONFIG,
         "--allowedTools", "mcp__shadow-replay__investment_report_get_hermes_context,"
         "mcp__shadow-replay__get_trading_policy,mcp__shadow-replay__route_request",
         "--max-turns", "8", "--output-format", "json"],
        input=_PROMPT.format(uuid=uuid), text=True, capture_output=True, timeout=300,
    )
    if proc.returncode != 0 or any(m in proc.stderr.lower() for m in _RESET_MARKERS):
        return None  # discarded sample (MCP reset / error) — NOT a data point
    try:
        # claude --output-format json wraps the assistant text; parse the inner JSON contract
        outer = json.loads(proc.stdout)
        return json.loads(outer["result"]) if isinstance(outer, dict) else json.loads(proc.stdout)
    except (json.JSONDecodeError, KeyError):
        return None

def run_batch(corpus, *, k: int, model: str, tick: Decimal) -> list[dict]:
    from app.services.shadow_replay.scoring import extract_decision, summarize
    results = []
    for item in corpus.items:
        raw = [_one_run(item.snapshot_bundle_uuid, model) for _ in range(k)]
        decisions = [extract_decision(r) for r in raw if r is not None]
        results.append({
            "item_uuid": item.item_uuid, "item_kind": item.item_kind,
            "discarded": sum(1 for r in raw if r is None),
            "summary": summarize(decisions, item.reference_decision, tick=tick),
        })
    return results
```

- [ ] **Step 3: Write CLI tests that monkeypatch `_one_run` (no real `claude -p` in CI) and assert discard + summary wiring.**

```python
# tests/test_shadow_replay_cli.py
from decimal import Decimal
import pytest
from scripts import shadow_replay as sr
from app.services.shadow_replay.corpus import CorpusItem, CorpusSelection

@pytest.mark.unit
def test_run_batch_counts_discards_and_summarizes(monkeypatch):
    ref = {"side": "buy", "max_action": {"notional": "300000", "limit_price": "129600"},
           "evidence_snapshot": {"trade_setup": {"headline": {"entry": "129600"}}},
           "trigger_checklist": ["x"]}
    from app.services.shadow_replay.scoring import extract_decision
    item = CorpusItem("u1", 1, "i1", "action", "buy_review", extract_decision(ref))
    corpus = CorpusSelection("claude_bundle", [item])
    seq = iter([ref, None, ref])  # one MCP-reset discard in the middle
    monkeypatch.setattr(sr, "_one_run", lambda uuid, model: next(seq))
    out = sr.run_batch(corpus, k=3, model="claude-opus-4-8", tick=Decimal("100"))
    assert out[0]["discarded"] == 1
    assert out[0]["summary"]["fidelity"]["side_rate"] == 1.0
    assert out[0]["summary"]["no_action_rate"] == 0.0

@pytest.mark.unit
def test_parser_defaults():
    args = sr.build_parser().parse_args(["--k", "5"])
    assert args.k == 5 and args.model  # model has a pinned default
```

- [ ] **Step 4: Add `build_parser`, `write_report` (markdown table: per-item side/size/limit/same-decision + no-action rate + discards), and a `main()` that requires `--confirm` before spawning any `claude -p` (default is a dry plan that only prints the corpus + would-run count).**

- [ ] **Step 5: Run tests + lint + commit.**

Run: `uv run pytest tests/test_shadow_replay_cli.py -v --no-cov` — Expected: PASS.
Run: `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/ && uv run ty check app/ --error-on-warning` — Expected: clean.

```bash
git add scripts/shadow_replay.py scripts/shadow_replay_mcp.json tests/test_shadow_replay_cli.py docs/runbooks/shadow-replay.md
git commit -m "feat(ROB-697): headless claude -p shadow-replay driver + markdown report"
```

- [ ] **Step 6: One real end-to-end batch (operator, not CI) once P0 corpus is confirmed.**

Run: `uv run python -m scripts.shadow_replay --k 5 --model claude-opus-4-8 --confirm`
Expected: a markdown report with per-decision-type `side_rate / size_band_rate / limit_rate / same_decision_rate` + `no_action_rate` + discard counts. This is the M1 deliverable number.

---

## Roadmap (out of scope — do NOT build in M1)

- **M2:** Upbit shadow-sim live mock loop (crypto; live Upbit data → virtual fills; binance_demo dropped = ≠ Upbit exchange/currency). New `upbit-shadow` account_mode, forecast/retro auto-wiring, safety kit.
- **M3:** kis_mock (KR) port — wire `KisMockBroker.confirm_fill` (holdings-delta) into `kis_mock_reconciliation_run_impl`; add kis_mock to the retro pending due-list scan (`_VALID_ACCOUNT_MODES` already includes kis_mock).

## Self-Review notes

- Spec coverage: P0→Task 0, P1→Task 3, T1→Task 2, T2→Task 4 (steps 1-3, 6), T3→Task 1, T4→Task 4 (step 4). ✓
- The P0 tension (human-authored vs bundle presence) is handled explicitly with a census gate + 3 corpus sources, not assumed away.
- The live-leakage validity risk is enforced structurally (profile allowlist), tested (deny set disjoint).
- Determinism caveat is scoped to frozen sections + a tight batch; the clean `now=`-threading fix is named as M1-follow-up.
