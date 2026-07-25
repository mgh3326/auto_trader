# ROB-646 Trading Policy YAML Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a repo-committed `config/trading_policy.yaml` the single authoritative source of trading judgment thresholds, expose it read-only via a `get_trading_policy` MCP tool, stamp its version into run-start briefing, and add a soft fail-open sector-cluster concentration cap check to buy previews.

**Architecture:** A YAML file (seeded verbatim from the ROB-643 playbook `policy_keys:`) is validated by a pydantic `TradingPolicyDocument` (`extra="forbid"`) and read through a load-once service that resolves `(market, lane)` with per-market overrides and produces a `{version, content_hash}` stamp. A new read-only MCP tool echoes resolved thresholds + the stamp. A pure, always-fail-open `evaluate_sector_concentration` helper reads the cap from the policy, best-effort aggregates current portfolio weight by sector-cluster, and attaches a `sector_concentration` field to both the shared (KIS/crypto) and Toss buy previews — never blocking. `get_operating_briefing` gains a lightweight `policy_version` field.

**Tech Stack:** Python 3.13, pydantic v2, PyYAML, FastMCP, pytest (async), SQLAlchemy async.

## Global Constraints

- **migration 0** — no DB schema changes anywhere in this work.
- **No write tool** for the policy — operator edits via PR only. Read-only surfaces only.
- **No account-specific data** in the YAML (no account numbers, balances, asset size). Order-sizing thresholds are policy values, not balances.
- **Concentration check is fail-open, warning-field-first** — it MUST NOT flip `success` to `False`, MUST NOT raise, and MUST NOT block any order on any market or any data gap.
- **Do NOT migrate the fail-closed code guards** (loss guard `avg×1.01` in `order_validation.py`, ladder near-market `0.3%`, RSI scoring bands) into YAML — they stay in code.
- **Do NOT revive `trade_profile`** (dormant since ROB-488).
- **Canonical lane name is `sell`** (matches playbook `policy_keys` lane tags + ROB-649). `profit_taking` is a documented human alias only, never a key.
- **Markets:** `Literal["kr","us","crypto"]`. **Lanes:** `Literal["buy","sell","discovery"]`.
- All threshold values are transcribed **verbatim** from `docs/playbooks/trading-decision-playbook.md` lines 265–352 (the `policy_keys:` block). Do not invent or round values.
- Tests must be broker-free / DB-free where possible (inject fakes); follow the `tests/_mcp_tooling_support.DummyMCP` + `register_all_tools` matrix pattern for registration tests.

---

## Task 1: Policy YAML file + pydantic schema + dependency

**Files:**
- Modify: `pyproject.toml` (add `pyyaml` to `dependencies`; add `types-PyYAML` to the dev/typing group)
- Create: `config/trading_policy.yaml`
- Create: `app/schemas/trading_policy.py`
- Test: `tests/schemas/test_trading_policy_schema.py`

**Interfaces:**
- Produces:
  - `app/schemas/trading_policy.py`:
    - `Lane = Literal["buy", "sell", "discovery"]`
    - `Market = Literal["kr", "us", "crypto"]`
    - `class PolicyThreshold(BaseModel)` — `model_config = ConfigDict(extra="forbid")`; fields: `lanes: list[Lane]`, `value: int | float | str | list[int | float]`, `unit: str`, `semantics: str`, `of: int | None = None`.
    - `class PolicyAuthority(BaseModel)` — `extra="forbid"`; `scope: str`, `governs: str`, `does_not_govern: list[str]`.
    - `class TradingPolicyDocument(BaseModel)` — `extra="forbid"`; `version: str`, `captured_as_of: str`, `source: str`, `authority: PolicyAuthority`, `sector_clusters: dict[str, list[str]]`, `thresholds: dict[str, PolicyThreshold]`, `market_overrides: dict[Market, dict[str, int | float | str | list[int | float]]]`.
  - `config/trading_policy.yaml` — validates against `TradingPolicyDocument`.

- [ ] **Step 1: Add PyYAML dependency**

In `pyproject.toml`, under `[project]` `dependencies` (the list starting ~line 10), add:
```toml
    "pyyaml>=6.0.1,<7.0.0",
```
And in the dev/typing dependency group (where other `types-*` stubs live — grep `types-` in `pyproject.toml` to find the group), add:
```toml
    "types-PyYAML>=6.0.0",
```
Then sync:
```bash
uv sync --all-groups
```
Expected: resolves without error; `python3 -c "import yaml; print(yaml.__version__)"` prints a 6.x version.

- [ ] **Step 2: Write the schema**

Create `app/schemas/trading_policy.py`:
```python
"""Pydantic schema for config/trading_policy.yaml (ROB-646).

The YAML is the single authoritative source of trading judgment thresholds
(seeded verbatim from the ROB-643 playbook policy_keys block). This module
validates its shape; extra="forbid" everywhere so a typo in the operator PR
fails loudly instead of silently dropping a key.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Lane = Literal["buy", "sell", "discovery"]
Market = Literal["kr", "us", "crypto"]

ThresholdValue = int | float | str | list[int | float]


class PolicyThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    value: ThresholdValue
    unit: str
    semantics: str
    of: int | None = None


class PolicyAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str
    governs: str
    does_not_govern: list[str]


class TradingPolicyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    captured_as_of: str
    source: str
    authority: PolicyAuthority
    sector_clusters: dict[str, list[str]]
    thresholds: dict[str, PolicyThreshold]
    market_overrides: dict[Market, dict[str, ThresholdValue]]
```

- [ ] **Step 3: Write the config YAML**

Create `config/trading_policy.yaml`. Copy every threshold value verbatim from `docs/playbooks/trading-decision-playbook.md` lines 265–352:
```yaml
# Trading policy — single authoritative source (ROB-646).
# Seeded verbatim from docs/playbooks/trading-decision-playbook.md policy_keys
# (captured 2026-07-02). Edited by operator PR ONLY — there is no write tool.
# Repo is public; exposing these values is an accepted decision.
version: "2026-07-02.1"
captured_as_of: "2026-07-02"
source: "ROB-643 playbook policy_keys (seed); this file is now authoritative"

authority:
  scope: judgment_thresholds_only
  governs: "advisory judgment thresholds + the sector-cluster concentration cap"
  does_not_govern:
    - "loss_guard code guard (order_validation.py avg*1.01) — stays fail-closed in code"
    - "ladder near-market guard (ladder_fill_safety.py 0.3%) — code"
    - "RSI scoring bands (scoring.py) — code"
    - "symbol_trade_settings — live per-symbol sizing (separate authority)"
    - "sell_conditions — dormant/test-only (not authoritative)"
    - "trade_profile — dead since ROB-488 (not revived)"

# Raw-sector -> cluster grouping for the concentration cap. A symbol's
# symbol_sectors label (name_kr / name_en / source_key) is matched
# case-insensitively against these entries. Best-effort; unmapped => fail-open.
sector_clusters:
  financials: ["금융", "은행", "증권", "보험", "Financial Services", "Banks", "Insurance"]
  shipbuilding_defense: ["조선", "방산", "항공우주", "Aerospace & Defense"]
  bio: ["제약", "바이오", "의료", "Biotechnology", "Drug Manufacturers", "Healthcare"]
  semis_memory: ["반도체", "메모리", "Semiconductors", "Semiconductor Equipment & Materials"]

thresholds:
  recovery_gate.min_conditions_met:
    lanes: [buy]
    value: 2
    unit: count
    of: 4
    semantics: min recovery-gate conditions to deploy reserve (else support-conditional only)
  portfolio.sector_cluster_cap_pct:
    lanes: [buy, sell]
    value: 10
    unit: percent
    semantics: over-concentration cap per sector cluster (~9-10%)
  portfolio.max_symbols_per_theme:
    lanes: [buy, discovery]
    value: 1
    unit: count
    semantics: one symbol per theme
  order.day_expiry_kst:
    lanes: [buy, sell]
    value: "20:00"
    unit: kst_time
    semantics: DAY order expiry; unfilled -> re-place next day
  buy.deep_limit_pct_range:
    lanes: [buy]
    value: [-12, -3]
    unit: percent
    semantics: deep limit distance below current price (pull-back catch, no chasing)
  buy.per_symbol_notional_krw_range:
    lanes: [buy, discovery]
    value: [200000, 400000]
    unit: krw
    semantics: per-symbol order sizing for new entries (policy threshold, not account balance)
  sell.loss_guard_min_multiple:
    lanes: [buy, sell]
    value: 1.01
    unit: multiple_of_avg_cost
    semantics: minimum sell price as multiple of average cost (loss guard)
  sell.breakeven_near_pct:
    lanes: [sell]
    value: 2
    unit: percent
    semantics: near-breakeven scan band (+/-)
  sell.resistance_near_pct:
    lanes: [sell]
    value: 6
    unit: percent
    semantics: resistance-proximity threshold for PLACE vs WATCH
  sell.rsi_place_min:
    lanes: [sell]
    value: 58
    unit: rsi
    semantics: RSI at/above which PLACE is favored
  sell.upside_place_max_pct:
    lanes: [sell]
    value: 45
    unit: percent
    semantics: honest upside below which PLACE is favored
  sell.watch_rsi_max:
    lanes: [sell]
    value: 52
    unit: rsi
    semantics: RSI below which WATCH (let-it-run) is allowed
  sell.watch_upside_min_pct:
    lanes: [sell]
    value: 50
    unit: percent
    semantics: upside at/above which WATCH (let-it-run) is allowed
  screen.rsi_max:
    lanes: [discovery]
    value: 45
    unit: rsi
    semantics: max RSI for a discovery candidate
  screen.support_within_pct:
    lanes: [discovery]
    value: 8
    unit: percent
    semantics: strong support must be within this distance
  screen.upside_min_pct:
    lanes: [discovery]
    value: 40
    unit: percent
    semantics: minimum honest upside for a candidate

market_overrides:
  kr: {}
  us: {}
  crypto: {}
```

- [ ] **Step 4: Write the failing test**

Create `tests/schemas/test_trading_policy_schema.py`:
```python
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.schemas.trading_policy import TradingPolicyDocument

_CONFIG = Path(__file__).resolve().parents[2] / "config" / "trading_policy.yaml"


def _raw() -> dict:
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def test_shipped_config_validates():
    doc = TradingPolicyDocument.model_validate(_raw())
    assert doc.version == "2026-07-02.1"
    # verbatim seed values from the playbook policy_keys
    assert doc.thresholds["portfolio.sector_cluster_cap_pct"].value == 10
    assert doc.thresholds["sell.loss_guard_min_multiple"].value == 1.01
    assert doc.thresholds["screen.rsi_max"].value == 45
    assert doc.thresholds["buy.deep_limit_pct_range"].value == [-12, -3]
    assert set(doc.market_overrides.keys()) == {"kr", "us", "crypto"}
    assert "semis_memory" in doc.sector_clusters


def test_extra_key_rejected():
    raw = _raw()
    raw["unexpected_top_level"] = 1
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_extra_threshold_key_rejected():
    raw = _raw()
    raw["thresholds"]["screen.rsi_max"]["bogus"] = 1
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/schemas/test_trading_policy_schema.py -v`
Expected: 3 passed. (If `tests/schemas/` lacks `__init__.py`, do not add one — the repo uses rootdir-based collection; confirm with a sibling test dir.)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock config/trading_policy.yaml app/schemas/trading_policy.py tests/schemas/test_trading_policy_schema.py
git commit -m "feat(ROB-646): trading policy YAML + schema (seed from playbook)"
```

---

## Task 2: Policy loader service

**Files:**
- Create: `app/services/trading_policy_service.py`
- Test: `tests/services/test_trading_policy_service.py`

**Interfaces:**
- Consumes: `app.schemas.trading_policy.TradingPolicyDocument`, `config/trading_policy.yaml`.
- Produces:
  - `class TradingPolicyKeyError(ValueError)` — raised for unknown market or lane.
  - `def load_trading_policy() -> TradingPolicyDocument` — load-once, cached by file (mtime,size); re-reads if the file changes.
  - `def policy_content_hash() -> str` — `sha256(raw_bytes)[:12]`.
  - `def policy_version_stamp() -> dict[str, str]` — `{"version": <version>, "content_hash": <hash>}`.
  - `def get_policy_for(market: str, lane: str) -> dict` — resolved view (see shape below); raises `TradingPolicyKeyError` on unknown market/lane.
  - `def sector_cluster_for(label: str | None) -> str | None` — reverse lookup over `sector_clusters`, case-insensitive substring match; `None` when unmapped or `label` is falsy.
  - `_POLICY_PATH: Path` (module-level, for tests to monkeypatch).

  `get_policy_for` return shape:
  ```python
  {
    "market": market, "lane": lane,
    "version": <str>, "content_hash": <str>,
    "thresholds": {
      "<key>": {"value": ..., "unit": ..., "semantics": ..., "of": <int|None>, "source": "default"|"override"},
      ...
    },
  }
  ```
  A threshold is included iff `lane in threshold.lanes`. If `market_overrides[market]` has the key, `value` is the override and `source="override"`, else `source="default"`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_trading_policy_service.py`:
```python
import pytest

from app.services import trading_policy_service as svc


def test_version_stamp_has_version_and_hash():
    stamp = svc.policy_version_stamp()
    assert stamp["version"] == "2026-07-02.1"
    assert len(stamp["content_hash"]) == 12


def test_content_hash_stable_across_calls():
    assert svc.policy_content_hash() == svc.policy_content_hash()


def test_get_policy_for_buy_kr_includes_cap_and_version():
    view = svc.get_policy_for("kr", "buy")
    assert view["version"] == "2026-07-02.1"
    assert view["content_hash"]
    t = view["thresholds"]
    # buy lane references these (playbook lane tags)
    assert t["portfolio.sector_cluster_cap_pct"]["value"] == 10
    assert t["portfolio.sector_cluster_cap_pct"]["source"] == "default"
    assert t["recovery_gate.min_conditions_met"]["value"] == 2
    assert t["sell.loss_guard_min_multiple"]["value"] == 1.01
    # sell-only threshold must NOT appear in the buy lane
    assert "sell.rsi_place_min" not in t


def test_get_policy_for_sell_lane_has_sell_keys():
    t = svc.get_policy_for("kr", "sell")["thresholds"]
    assert t["sell.rsi_place_min"]["value"] == 58
    assert "screen.rsi_max" not in t


def test_unknown_market_raises():
    with pytest.raises(svc.TradingPolicyKeyError):
        svc.get_policy_for("jp", "buy")


def test_unknown_lane_raises():
    with pytest.raises(svc.TradingPolicyKeyError):
        svc.get_policy_for("kr", "scalp")


def test_market_override_applied(monkeypatch, tmp_path):
    import yaml
    from pathlib import Path

    raw = yaml.safe_load(svc._POLICY_PATH.read_text(encoding="utf-8"))
    raw["market_overrides"]["us"]["screen.rsi_max"] = 55
    p = tmp_path / "trading_policy.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(svc, "_POLICY_PATH", Path(p))
    svc.load_trading_policy.cache_clear() if hasattr(svc.load_trading_policy, "cache_clear") else None
    svc._reset_cache_for_tests()
    t = svc.get_policy_for("us", "discovery")["thresholds"]
    assert t["screen.rsi_max"]["value"] == 55
    assert t["screen.rsi_max"]["source"] == "override"


def test_sector_cluster_for():
    assert svc.sector_cluster_for("반도체") == "semis_memory"
    assert svc.sector_cluster_for("Financial Services") == "financials"
    assert svc.sector_cluster_for("정체불명업종") is None
    assert svc.sector_cluster_for(None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_trading_policy_service.py -v`
Expected: FAIL / ImportError (module not defined).

- [ ] **Step 3: Write the service**

Create `app/services/trading_policy_service.py`:
```python
"""Loader for config/trading_policy.yaml — the single authoritative source
of trading judgment thresholds (ROB-646). Read-only; operator edits via PR."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from app.schemas.trading_policy import TradingPolicyDocument

_POLICY_PATH: Path = Path(__file__).resolve().parents[2] / "config" / "trading_policy.yaml"

_cache: dict[str, Any] = {"key": None, "doc": None, "hash": None}


class TradingPolicyKeyError(ValueError):
    """Unknown market or lane requested from the trading policy."""


def _reset_cache_for_tests() -> None:
    _cache["key"] = None
    _cache["doc"] = None
    _cache["hash"] = None


def _load() -> tuple[TradingPolicyDocument, str]:
    stat = _POLICY_PATH.stat()
    key = (str(_POLICY_PATH), stat.st_mtime_ns, stat.st_size)
    if _cache["key"] == key and _cache["doc"] is not None:
        return _cache["doc"], _cache["hash"]
    raw_bytes = _POLICY_PATH.read_bytes()
    doc = TradingPolicyDocument.model_validate(yaml.safe_load(raw_bytes))
    content_hash = hashlib.sha256(raw_bytes).hexdigest()[:12]
    _cache.update(key=key, doc=doc, hash=content_hash)
    return doc, content_hash


def load_trading_policy() -> TradingPolicyDocument:
    return _load()[0]


def policy_content_hash() -> str:
    return _load()[1]


def policy_version_stamp() -> dict[str, str]:
    doc, content_hash = _load()
    return {"version": doc.version, "content_hash": content_hash}


def get_policy_for(market: str, lane: str) -> dict[str, Any]:
    doc, content_hash = _load()
    if market not in doc.market_overrides:
        raise TradingPolicyKeyError(
            f"unknown market {market!r}; valid: {sorted(doc.market_overrides)}"
        )
    valid_lanes = {"buy", "sell", "discovery"}
    if lane not in valid_lanes:
        raise TradingPolicyKeyError(
            f"unknown lane {lane!r}; valid: {sorted(valid_lanes)}"
        )
    overrides = doc.market_overrides[market]
    thresholds: dict[str, Any] = {}
    for key, spec in doc.thresholds.items():
        if lane not in spec.lanes:
            continue
        if key in overrides:
            value = overrides[key]
            source = "override"
        else:
            value = spec.value
            source = "default"
        thresholds[key] = {
            "value": value,
            "unit": spec.unit,
            "semantics": spec.semantics,
            "of": spec.of,
            "source": source,
        }
    return {
        "market": market,
        "lane": lane,
        "version": doc.version,
        "content_hash": content_hash,
        "thresholds": thresholds,
    }


def sector_cluster_for(label: str | None) -> str | None:
    if not label:
        return None
    doc, _ = _load()
    needle = label.strip().casefold()
    for cluster, members in doc.sector_clusters.items():
        for member in members:
            m = member.casefold()
            if m in needle or needle in m:
                return cluster
    return None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_trading_policy_service.py -v`
Expected: all passed. (The `cache_clear()` line in the override test is a no-op guard; `_reset_cache_for_tests()` does the real reset.)

- [ ] **Step 5: Commit**

```bash
git add app/services/trading_policy_service.py tests/services/test_trading_policy_service.py
git commit -m "feat(ROB-646): trading policy loader service (market x lane resolve + version stamp)"
```

---

## Task 3: `get_trading_policy` MCP tool + registration

**Files:**
- Create: `app/mcp_server/tooling/trading_policy_tools.py`
- Create: `app/mcp_server/tooling/trading_policy_registration.py`
- Modify: `app/mcp_server/tooling/registry.py` (import ~line 100–112; call in the always-registered read-only block ~line 127–154)
- Test: `tests/mcp_server/test_trading_policy_tool.py`

**Interfaces:**
- Consumes: `app.services.trading_policy_service.get_policy_for`, `TradingPolicyKeyError`.
- Produces:
  - `async def get_trading_policy(market: str, lane: str) -> dict[str, Any]` — success returns `get_policy_for(...)` plus `"success": True`; unknown key returns `{"success": False, "error": "unknown_key", "detail": <str>}`.
  - `def register_trading_policy_tools(mcp) -> None`, `TRADING_POLICY_TOOL_NAMES: set[str] = {"get_trading_policy"}`.

- [ ] **Step 1: Write the failing test**

Create `tests/mcp_server/test_trading_policy_tool.py`:
```python
from typing import Any, cast

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.trading_policy_tools import get_trading_policy
from tests._mcp_tooling_support import DummyMCP


@pytest.mark.asyncio
async def test_get_trading_policy_returns_thresholds_and_version():
    out = await get_trading_policy(market="kr", lane="buy")
    assert out["success"] is True
    assert out["version"] == "2026-07-02.1"
    assert out["content_hash"]
    assert out["thresholds"]["portfolio.sector_cluster_cap_pct"]["value"] == 10


@pytest.mark.asyncio
async def test_get_trading_policy_unknown_key_explicit_error():
    out = await get_trading_policy(market="jp", lane="buy")
    assert out["success"] is False
    assert out["error"] == "unknown_key"
    assert "jp" in out["detail"]


def test_tool_registered_in_default_profile():
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.DEFAULT)
    assert "get_trading_policy" in mcp.tools


def test_tool_registered_in_crypto_profile():
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.CRYPTO)
    assert "get_trading_policy" in mcp.tools
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/test_trading_policy_tool.py -v`
Expected: FAIL / ImportError.

- [ ] **Step 3: Write the tool handler**

Create `app/mcp_server/tooling/trading_policy_tools.py`:
```python
"""Read-only get_trading_policy MCP tool (ROB-646).

Echoes market x lane judgment thresholds plus the policy version stamp
({version, content_hash}). Consumers cite the stamp so a verdict record can
recover "what criteria did we judge under?". Operator edits via PR only —
there is no write tool."""

from __future__ import annotations

from typing import Any

from app.services.trading_policy_service import (
    TradingPolicyKeyError,
    get_policy_for,
)


async def get_trading_policy(market: str, lane: str) -> dict[str, Any]:
    """Return trading-policy thresholds for a market x lane, plus the version stamp."""
    try:
        view = get_policy_for(market, lane)
    except TradingPolicyKeyError as exc:
        return {"success": False, "error": "unknown_key", "detail": str(exc)}
    return {"success": True, **view}
```

- [ ] **Step 4: Write the registration**

Create `app/mcp_server/tooling/trading_policy_registration.py`:
```python
"""Registration for the read-only get_trading_policy MCP tool (ROB-646)."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.mcp_server.tooling.trading_policy_tools import get_trading_policy

TRADING_POLICY_TOOL_NAMES: set[str] = {"get_trading_policy"}


def register_trading_policy_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="get_trading_policy",
        description=(
            "Read trading judgment thresholds for a market x lane from the "
            "single authoritative config/trading_policy.yaml. Args: "
            "market in {kr, us, crypto}, lane in {buy, sell, discovery} "
            "(sell = profit-taking). Returns resolved thresholds (value/unit/"
            "semantics/source) plus the policy version stamp "
            "{version, content_hash}. VERSION-STAMPING CONTRACT: cite this "
            "stamp when recording a verdict (report item evidence_snapshot, "
            "trade_retrospectives, forecast) so the criteria are recoverable. "
            "Unknown market/lane returns success=false, error=unknown_key. "
            "Read-only — the policy is edited by operator PR, never by a tool."
        ),
    )(get_trading_policy)
```
Confirm the `FastMCP` import path matches the sibling registration modules (grep `from mcp.server.fastmcp import FastMCP` in `app/mcp_server/tooling/session_context_registration.py`; use whatever that file uses).

- [ ] **Step 5: Wire into registry**

In `app/mcp_server/tooling/registry.py`, add the import next to the other registration imports (~line 100):
```python
from app.mcp_server.tooling.trading_policy_registration import (
    register_trading_policy_tools,
)
```
And in the always-registered read-only block (the section around `register_session_context_tools(mcp)` at ~line 141, NOT inside any `if profile is McpProfile.DEFAULT:` branch), add:
```python
    register_trading_policy_tools(mcp)
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/mcp_server/test_trading_policy_tool.py -v`
Expected: 4 passed.

- [ ] **Step 7: Guard against name collision**

Run: `uv run pytest tests/test_playbook_tool_names.py tests/test_mcp_profiles.py -v` (server boots with `on_duplicate="error"`; a name clash would fail here).
Expected: passed.

- [ ] **Step 8: Commit**

```bash
git add app/mcp_server/tooling/trading_policy_tools.py app/mcp_server/tooling/trading_policy_registration.py app/mcp_server/tooling/registry.py tests/mcp_server/test_trading_policy_tool.py
git commit -m "feat(ROB-646): get_trading_policy read-only MCP tool (all profiles)"
```

---

## Task 4: `evaluate_sector_concentration` fail-open helper

**Files:**
- Modify: `app/mcp_server/tooling/order_validation.py` (add helper + two internal seams near the other guard helpers, after `evaluate_market_sell_loss_guard` ~line 149)
- Test: `tests/mcp_server/test_sector_concentration.py`

**Interfaces:**
- Consumes: `app.services.trading_policy_service.get_policy_for`, `sector_cluster_for`.
- Produces (all in `order_validation.py`):
  - `async def compute_sector_cluster_weights(*, market: str, account_ctx: dict[str, Any]) -> dict[str, Any]` — best-effort; returns `{"clusters": {cluster: value_krw}, "total_krw": float, "usd_krw": float}`. May raise; the orchestrator catches.
  - `async def resolve_symbol_cluster(*, symbol: str, market: str) -> str | None` — join universe→`symbol_sectors`, then `sector_cluster_for(label)`. May raise; orchestrator catches.
  - `async def evaluate_sector_concentration(*, symbol, market, order_estimated_value, order_currency, account_ctx, _weights_provider=compute_sector_cluster_weights, _cluster_resolver=resolve_symbol_cluster) -> dict[str, Any]` — NEVER raises, NEVER blocks. Return shapes:
    - within: `{"verdict": "within", "cluster": str, "cap_pct": float, "current_pct": float, "projected_pct": float, "fail_open": False}`
    - over: same as within but `"verdict": "over"` + `"warning": "<cluster> projected <p>% exceeds cap <cap>%"`
    - unknown: `{"verdict": "unknown", "fail_open": True, "reason": <str>}`

- [ ] **Step 1: Write the failing test** (broker-free; inject fakes)

Create `tests/mcp_server/test_sector_concentration.py`:
```python
import pytest

from app.mcp_server.tooling import order_validation as ov


async def _weights_ok(*, market, account_ctx):
    # semis cluster currently 800k of 10M total = 8%
    return {"clusters": {"semis_memory": 800_000.0}, "total_krw": 10_000_000.0, "usd_krw": 1350.0}


async def _cluster_semis(*, symbol, market):
    return "semis_memory"


@pytest.mark.asyncio
async def test_within_cap():
    out = await ov.evaluate_sector_concentration(
        symbol="005930", market="kr", order_estimated_value=100_000.0,
        order_currency="KRW", account_ctx={},
        _weights_provider=_weights_ok, _cluster_resolver=_cluster_semis,
    )
    assert out["verdict"] == "within"
    assert out["cluster"] == "semis_memory"
    assert out["cap_pct"] == 10
    assert out["fail_open"] is False
    # projected = (800k + 100k) / (10M + 100k) ~= 8.9%
    assert 8.5 < out["projected_pct"] < 9.3


@pytest.mark.asyncio
async def test_over_cap_warns_but_does_not_block():
    async def _weights_hot(*, market, account_ctx):
        return {"clusters": {"semis_memory": 950_000.0}, "total_krw": 10_000_000.0, "usd_krw": 1350.0}

    out = await ov.evaluate_sector_concentration(
        symbol="000660", market="kr", order_estimated_value=300_000.0,
        order_currency="KRW", account_ctx={},
        _weights_provider=_weights_hot, _cluster_resolver=_cluster_semis,
    )
    assert out["verdict"] == "over"
    assert "warning" in out
    assert out["fail_open"] is False


@pytest.mark.asyncio
async def test_crypto_fails_open():
    out = await ov.evaluate_sector_concentration(
        symbol="KRW-BTC", market="crypto", order_estimated_value=100_000.0,
        order_currency="KRW", account_ctx={},
        _weights_provider=_weights_ok, _cluster_resolver=_cluster_semis,
    )
    assert out["verdict"] == "unknown"
    assert out["fail_open"] is True
    assert "crypto" in out["reason"]


@pytest.mark.asyncio
async def test_unmapped_cluster_fails_open():
    async def _no_cluster(*, symbol, market):
        return None

    out = await ov.evaluate_sector_concentration(
        symbol="123456", market="kr", order_estimated_value=100_000.0,
        order_currency="KRW", account_ctx={},
        _weights_provider=_weights_ok, _cluster_resolver=_no_cluster,
    )
    assert out["verdict"] == "unknown"
    assert out["fail_open"] is True


@pytest.mark.asyncio
async def test_provider_exception_fails_open():
    async def _boom(*, market, account_ctx):
        raise RuntimeError("broker down")

    out = await ov.evaluate_sector_concentration(
        symbol="005930", market="kr", order_estimated_value=100_000.0,
        order_currency="KRW", account_ctx={},
        _weights_provider=_boom, _cluster_resolver=_cluster_semis,
    )
    assert out["verdict"] == "unknown"
    assert out["fail_open"] is True
    assert "broker down" in out["reason"]


@pytest.mark.asyncio
async def test_missing_order_value_uses_current_only():
    out = await ov.evaluate_sector_concentration(
        symbol="005930", market="kr", order_estimated_value=None,
        order_currency="KRW", account_ctx={},
        _weights_provider=_weights_ok, _cluster_resolver=_cluster_semis,
    )
    # current 8% within cap; projected omitted or equals current
    assert out["verdict"] == "within"
    assert out["current_pct"] == pytest.approx(8.0, abs=0.01)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/test_sector_concentration.py -v`
Expected: FAIL (helpers not defined).

- [ ] **Step 3: Implement the helpers**

In `app/mcp_server/tooling/order_validation.py`, after `evaluate_market_sell_loss_guard` (~line 149), add:
```python
async def compute_sector_cluster_weights(
    *, market: str, account_ctx: dict[str, Any]
) -> dict[str, Any]:
    """Best-effort current portfolio weight by sector cluster (KRW).

    Reuses get_portfolio_allocation_impl(include_positions=True) for position
    KRW values + usd_krw, joins each holding to symbol_sectors, and groups by
    sector cluster. Raises on any data gap; the orchestrator fails open."""
    from app.mcp_server.tooling.portfolio_allocation import (
        get_portfolio_allocation_impl,
    )
    from app.services.trading_policy_service import sector_cluster_for

    alloc = await get_portfolio_allocation_impl(
        account=account_ctx.get("account"),
        market=account_ctx.get("market"),
        include_positions=True,
        is_mock=account_ctx.get("is_mock", False),
    )
    usd_krw = float(alloc.get("currency", {}).get("usd_krw") or 0.0)
    positions = alloc.get("positions") or []
    clusters: dict[str, float] = {}
    total = 0.0
    for pos in positions:
        value_krw = pos.get("value_krw")
        if value_krw is None:
            continue
        total += float(value_krw)
        label = pos.get("sector") or pos.get("sector_name")
        cluster = sector_cluster_for(label)
        if cluster is not None:
            clusters[cluster] = clusters.get(cluster, 0.0) + float(value_krw)
    return {"clusters": clusters, "total_krw": total, "usd_krw": usd_krw}


async def resolve_symbol_cluster(*, symbol: str, market: str) -> str | None:
    """Resolve a symbol's sector-cluster via symbol_sectors (best-effort)."""
    from app.core.db import AsyncSessionLocal
    from app.services.trading_policy_service import sector_cluster_for

    async with AsyncSessionLocal() as db:
        label = await _lookup_symbol_sector_label(db, symbol=symbol, market=market)
    return sector_cluster_for(label)


async def evaluate_sector_concentration(
    *,
    symbol: str,
    market: str,
    order_estimated_value: float | None,
    order_currency: str,
    account_ctx: dict[str, Any],
    _weights_provider=compute_sector_cluster_weights,
    _cluster_resolver=resolve_symbol_cluster,
) -> dict[str, Any]:
    """Fail-open sector-cluster concentration check for buy previews.

    Never raises, never blocks. `over` produces a soft warning field only."""
    try:
        if market == "crypto":
            return {"verdict": "unknown", "fail_open": True, "reason": "crypto (no sectors)"}
        from app.services.trading_policy_service import get_policy_for

        policy = get_policy_for(market, "buy")
        cap = policy["thresholds"]["portfolio.sector_cluster_cap_pct"]["value"]

        cluster = await _cluster_resolver(symbol=symbol, market=market)
        if cluster is None:
            return {
                "verdict": "unknown", "fail_open": True,
                "reason": f"no sector-cluster mapping for {symbol}",
            }

        weights = await _weights_provider(market=market, account_ctx=account_ctx)
        total = float(weights.get("total_krw") or 0.0)
        if total <= 0:
            return {"verdict": "unknown", "fail_open": True, "reason": "empty portfolio total"}
        current_value = float(weights.get("clusters", {}).get(cluster, 0.0))
        current_pct = current_value / total * 100.0

        order_krw = _order_value_to_krw(order_estimated_value, order_currency, weights.get("usd_krw"))
        if order_krw is None:
            projected_pct = current_pct
        else:
            projected_pct = (current_value + order_krw) / (total + order_krw) * 100.0

        result = {
            "verdict": "over" if projected_pct > float(cap) else "within",
            "cluster": cluster, "cap_pct": cap,
            "current_pct": round(current_pct, 2),
            "projected_pct": round(projected_pct, 2),
            "fail_open": False,
        }
        if result["verdict"] == "over":
            result["warning"] = (
                f"{cluster} projected {result['projected_pct']}% exceeds "
                f"sector-cluster cap {cap}%"
            )
        return result
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        return {"verdict": "unknown", "fail_open": True, "reason": str(exc)}
```
Add these small helpers in the same module (near the data-lookup helpers ~line 446):
```python
def _order_value_to_krw(value: float | None, currency: str, usd_krw: Any) -> float | None:
    if value is None:
        return None
    cur = (currency or "").upper()
    if cur in ("KRW", "₩", ""):
        return float(value)
    if cur in ("USD", "$"):
        rate = float(usd_krw or 0.0)
        return float(value) * rate if rate > 0 else None
    return None


async def _lookup_symbol_sector_label(db, *, symbol: str, market: str) -> str | None:
    """Return a symbol's sector label (name_kr/name_en) via the universe join.
    Best-effort; returns None on unknown symbol or missing sector."""
    from sqlalchemy import select

    from app.models.symbol_sectors import SymbolSector

    if market == "kr":
        from app.models.kr_symbol_universe import KRSymbolUniverse as Univ
    elif market == "us":
        from app.core.symbol import to_db_symbol
        from app.models.us_symbol_universe import USSymbolUniverse as Univ

        symbol = to_db_symbol(symbol)
    else:
        return None
    stmt = (
        select(SymbolSector.name_kr, SymbolSector.name_en)
        .join(Univ, Univ.sector_id == SymbolSector.id)
        .where(Univ.symbol == symbol)
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    if not row:
        return None
    return row[0] or row[1]
```
Confirm `Any` is imported at the top of `order_validation.py` (it is used elsewhere; grep `from typing import`). Confirm the `USSymbolUniverse.symbol` / `KRSymbolUniverse.symbol` column names against the models (the exploration report cited `sector_id` on both). Confirm `positions[*]` exposes a `sector`/`sector_name` and `value_krw` from `get_portfolio_allocation_impl(include_positions=True)`; if the position rows don't carry a sector label, `compute_sector_cluster_weights` still works but will map fewer holdings (acceptable — fail-open). If needed, enrich via `_lookup_symbol_sector_label` per position inside `compute_sector_cluster_weights`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/mcp_server/test_sector_concentration.py -v`
Expected: all passed (unit tests inject `_weights_provider`/`_cluster_resolver`, so no DB/broker).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/order_validation.py tests/mcp_server/test_sector_concentration.py
git commit -m "feat(ROB-646): fail-open sector-cluster concentration helper"
```

---

## Task 5: Wire concentration field into buy previews (shared + Toss)

**Files:**
- Modify: `app/mcp_server/tooling/order_execution.py` (`_place_order_impl`, buy branch, after the `_check_balance_and_warn` block ~lines 1086–1103)
- Modify: `app/mcp_server/tooling/orders_toss_variants.py` (`toss_preview_order` response assembly ~line 745; `_toss_place_order_impl` dry-run return ~line 859)
- Test: `tests/mcp_server/test_buy_preview_concentration_field.py`

**Interfaces:**
- Consumes: `order_validation.evaluate_sector_concentration`.
- Produces: buy previews carry a `sector_concentration` dict (the helper's return). `verdict == "over"` never flips `success`.

- [ ] **Step 1: Write the failing test** (monkeypatch the helper — no broker)

Create `tests/mcp_server/test_buy_preview_concentration_field.py`:
```python
import pytest

from app.mcp_server.tooling import order_execution


@pytest.mark.asyncio
async def test_shared_buy_preview_includes_concentration(monkeypatch):
    async def _fake_conc(**kwargs):
        return {"verdict": "within", "cluster": "semis_memory", "cap_pct": 10,
                "current_pct": 8.0, "projected_pct": 8.9, "fail_open": False}

    monkeypatch.setattr(order_execution, "evaluate_sector_concentration", _fake_conc, raising=False)
    # ... drive _place_order_impl with a dry_run buy on a fake KIS/crypto path
    # (mirror the existing dry_run preview tests in tests/mcp_server for the
    # minimal fixture set — assert the response dict has "sector_concentration"
    # and success stays True even when verdict == "over").
```
Look at the existing dry-run preview tests (grep `dry_run` under `tests/mcp_server/` — e.g. tests exercising `kis_live_place_order` or `_place_order_impl`) and reuse their fixture/monkeypatch scaffold to invoke a buy preview. Assert:
- `resp["sector_concentration"]["verdict"]` present.
- With an `over` verdict, `resp["success"] is True` (fail-open contract).

Add a parallel test for Toss:
```python
@pytest.mark.asyncio
async def test_toss_buy_preview_includes_concentration(monkeypatch):
    from app.mcp_server.tooling import orders_toss_variants
    async def _fake_conc(**kwargs):
        return {"verdict": "over", "cluster": "financials", "cap_pct": 10,
                "current_pct": 9.5, "projected_pct": 11.2, "fail_open": False,
                "warning": "financials projected 11.2% exceeds sector-cluster cap 10%"}
    monkeypatch.setattr(orders_toss_variants, "evaluate_sector_concentration", _fake_conc, raising=False)
    # drive toss_preview_order for a buy (reuse existing toss preview test scaffold)
    # assert resp["sector_concentration"]["verdict"] == "over" and resp["success"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/test_buy_preview_concentration_field.py -v`
Expected: FAIL (field absent).

- [ ] **Step 3: Wire the shared path**

In `order_execution.py`, import at top:
```python
from app.mcp_server.tooling.order_validation import evaluate_sector_concentration
```
In `_place_order_impl`, in the **buy** branch, right after the balance pre-check block (~line 1103, before `_build_dry_run_response`), add:
```python
        if side == "buy":
            sector_conc = await evaluate_sector_concentration(
                symbol=symbol,
                market=market,
                order_estimated_value=dry_run_result.get("estimated_value"),
                order_currency=currency,
                account_ctx={"account": account, "market": market, "is_mock": is_mock},
            )
            dry_run_result["sector_concentration"] = sector_conc
            if sector_conc.get("verdict") == "over" and not balance_warning:
                balance_warning = sector_conc.get("warning")
```
Match the actual local variable names in `_place_order_impl` (`side`, `symbol`, `market`, `currency`, `account`, `is_mock`, `dry_run_result`, `balance_warning`) — grep the function body first and adapt. Because `_build_dry_run_response` splats `**dry_run_result`, the field surfaces automatically. Do NOT touch `success`.

- [ ] **Step 4: Wire the Toss path**

In `orders_toss_variants.py`, import `evaluate_sector_concentration` at top. In `toss_preview_order`, when the order is a **buy**, before assembling the response (~line 745), compute:
```python
    sector_conc = None
    if side == "buy":
        sector_conc = await evaluate_sector_concentration(
            symbol=symbol, market=mkt,
            order_estimated_value=<estimated_value_local>,
            order_currency=<currency_local>,
            account_ctx={"account_mode": ACCOUNT_MODE_TOSS_LIVE},
        )
```
and add `"sector_concentration": sector_conc` into the response dict (~line 752, alongside `order_warnings`). If `sector_conc` carries a `warning`, also append it to `order_warnings`. Mirror the same in `_toss_place_order_impl`'s dry-run return (~line 859). Use the estimated-value / currency locals that already exist in those functions (grep the function bodies).

Note: Toss markets map to `kr`/`us`; pass the resolved market string the function already computes (`mkt`). The helper fail-opens for anything it can't map.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/mcp_server/test_buy_preview_concentration_field.py -v`
Expected: passed.

- [ ] **Step 6: Regression — existing preview tests still pass**

Run: `uv run pytest tests/mcp_server/ -k "preview or place_order or toss" -q`
Expected: no regressions (new field is additive; `success` unchanged).

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/order_execution.py app/mcp_server/tooling/orders_toss_variants.py tests/mcp_server/test_buy_preview_concentration_field.py
git commit -m "feat(ROB-646): sector-concentration field on shared + Toss buy previews (fail-open)"
```

---

## Task 6: Briefing policy-version echo

**Files:**
- Modify: `app/schemas/investment_reports.py` (`OperatingBriefingResponse` ~line 1108 — add `policy_version` field)
- Modify: `app/mcp_server/tooling/operating_briefing.py` (`get_operating_briefing_impl` — add `policy_version` to the response dict ~line 376)
- Test: `tests/mcp_server/test_operating_briefing_policy_version.py`

**Interfaces:**
- Consumes: `app.services.trading_policy_service.policy_version_stamp`.
- Produces: `OperatingBriefingResponse.policy_version: dict[str, Any] | None = None`; briefing response includes `policy_version = {version, content_hash}` (or `{"error": <reason>}` fail-open, still non-blocking).

- [ ] **Step 1: Write the failing test**

Create `tests/mcp_server/test_operating_briefing_policy_version.py`:
```python
import pytest

from app.mcp_server.tooling import operating_briefing


@pytest.mark.asyncio
async def test_briefing_includes_policy_version(monkeypatch):
    # stub the heavy sub-calls so this stays a unit test; mirror the scaffold
    # used by the existing operating_briefing tests (grep tests/mcp_server for
    # get_operating_briefing_impl). The only new assertion:
    resp = await operating_briefing.get_operating_briefing_impl(market="kr")
    assert resp["policy_version"]["version"] == "2026-07-02.1"
    assert resp["policy_version"]["content_hash"]
```
If no lightweight scaffold exists, add a `test_policy_version_stamp_shape` unit test on the imported stamp and a smaller assertion that the impl merges it (patch `policy_version_stamp` and assert the response key). Keep it broker-free by monkeypatching `_get_holdings_impl`, `collect_pending_orders_snapshot`, etc., following the closest existing briefing test.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/test_operating_briefing_policy_version.py -v`
Expected: FAIL (key absent).

- [ ] **Step 3: Add the schema field**

In `app/schemas/investment_reports.py`, in `OperatingBriefingResponse` (after `analysis_artifacts`, ~line 1123):
```python
    # ROB-646 — lightweight policy version pin ({version, content_hash}) so a
    # session records which policy it ran under. Defaulted for back-compat.
    policy_version: dict[str, Any] | None = None
```
Confirm `Any` is already imported in that module (it is used above).

- [ ] **Step 4: Populate it in the impl**

In `operating_briefing.py`, near the top add:
```python
from app.services.trading_policy_service import policy_version_stamp
```
In `get_operating_briefing_impl`, before building the response dict (~line 375), add:
```python
    try:
        policy_version = policy_version_stamp()
    except Exception as exc:  # noqa: BLE001 — fail-open, briefing must still return
        policy_version = {"error": str(exc)}
```
Add `"policy_version": policy_version,` as a key in the returned dict (alongside `analysis_artifacts` ~line 426).

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/mcp_server/test_operating_briefing_policy_version.py -v`
Expected: passed.

- [ ] **Step 6: Regression**

Run: `uv run pytest tests/mcp_server/ -k briefing -q`
Expected: no regressions.

- [ ] **Step 7: Commit**

```bash
git add app/schemas/investment_reports.py app/mcp_server/tooling/operating_briefing.py tests/mcp_server/test_operating_briefing_policy_version.py
git commit -m "feat(ROB-646): echo policy_version in get_operating_briefing (fail-open)"
```

---

## Task 7: Docs — authority declaration + version-stamping contract

**Files:**
- Modify: `docs/playbooks/trading-decision-playbook.md` (Policy-key capture section ~line 256–260)
- Modify: `app/mcp_server/README.md`
- Modify: `CLAUDE.md` (add a short ROB-646 section next to the other tool contracts)

**Interfaces:** docs only. No code.

- [ ] **Step 1: Playbook authority note**

In `docs/playbooks/trading-decision-playbook.md`, in the "Policy-key capture (ROB-646 initial values)" section (~line 256), append a note (the playbook already says these become non-authoritative once the YAML lands — close that loop):
```markdown
> **Authority (ROB-646, landed):** `config/trading_policy.yaml` is now the
> single authoritative source of these values; this block is the historical
> seed. The policy governs **judgment thresholds + the sector-cluster
> concentration cap only** — NOT the fail-closed code guards (loss guard,
> ladder near-market, RSI scoring bands), NOT `symbol_trade_settings` (live
> sizing), and it does not revive `trade_profile` (dead since ROB-488). Lane
> `sell` = "profit_taking" (same lane, human alias). Read it via
> `get_trading_policy(market, lane)`.
```

- [ ] **Step 2: README version-stamping contract**

In `app/mcp_server/README.md`, add a `get_trading_policy` entry documenting:
- read-only, single source `config/trading_policy.yaml`, operator-PR-edited (no write tool);
- args `market ∈ {kr,us,crypto}` × `lane ∈ {buy,sell,discovery}`; unknown key → `success=false, error=unknown_key`;
- **version-stamping contract**: consumers cite `{version, content_hash}` (from `get_trading_policy` or the `policy_version` field of `get_operating_briefing`) in `report_item.evidence_snapshot`, `trade_retrospectives`, and forecast records so the judging criteria are recoverable;
- the buy-preview `sector_concentration` field is **fail-open** advisory (never blocks).

- [ ] **Step 3: CLAUDE.md contract section**

In `CLAUDE.md`, add a short section (mirroring the other `### ...` tool-contract blocks):
```markdown
### Trading Policy YAML 단일 소스 (ROB-646)

`config/trading_policy.yaml` = 매매 판단 임계값 단일 소스 (ROB-643 플레이북
policy_keys에서 시드). **operator PR로만 편집 — 쓰기 도구 없음.**

- **스키마/로더**: `app/schemas/trading_policy.py`, `app/services/trading_policy_service.py`
- **MCP 도구**: `get_trading_policy(market, lane)` — market×lane 임계값 + `{version, content_hash}` echo; 없는 키는 `success=false, error=unknown_key`
- **버전 스탬핑 계약**: 판정 기록(evidence_snapshot·trade_retrospectives·forecast)은 `{version, content_hash}` 인용. `get_operating_briefing`가 run-start에 `policy_version` echo.
- **강제 범위**: 섹터 클러스터 집중도 cap만 매수 프리뷰에서 코드 검사 (`sector_concentration` 필드, **fail-open** — 경고만, 차단 안 함). 나머지 임계값은 advisory.
- **관할**: 판단 임계값 전용. fail-closed 코드 가드(손실매도/ladder/RSI 스코어링)·`symbol_trade_settings`(라이브 사이징)·`trade_profile`(dead)와 분리. migration 0.
```

- [ ] **Step 4: Verify playbook drift guard still passes**

Run: `uv run pytest tests/test_playbook_tool_names.py -v`
Expected: passed (docs edits don't touch the `lanes:` blocks).

- [ ] **Step 5: Commit**

```bash
git add docs/playbooks/trading-decision-playbook.md app/mcp_server/README.md CLAUDE.md
git commit -m "docs(ROB-646): policy authority declaration + version-stamping contract"
```

---

## Final verification (before PR)

- [ ] Full lint + type: `make lint` (Ruff + ty). Fix any findings.
- [ ] Format: `uv run ruff format app/ tests/ config/` then `uv run ruff check app/ tests/` (CI lint covers both `app/` and `tests/`).
- [ ] Targeted suite: `uv run pytest tests/schemas/test_trading_policy_schema.py tests/services/test_trading_policy_service.py tests/mcp_server/test_trading_policy_tool.py tests/mcp_server/test_sector_concentration.py tests/mcp_server/test_buy_preview_concentration_field.py tests/mcp_server/test_operating_briefing_policy_version.py tests/test_playbook_tool_names.py tests/test_mcp_profiles.py -v`
- [ ] Confirm `git grep -n "migration"` shows no alembic version added (migration 0).
- [ ] Confirm no runtime LLM provider import was added (the ROB-501 guard: `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py`).

## Self-review notes (author)

- **Spec coverage:** YAML+schema (T1), loader/version/hash/unknown-key (T2), get_trading_policy tool + registration (T3), fail-open concentration helper (T4), buy-preview wiring shared+Toss (T5), briefing echo (T6), docs authority + stamping contract (T7). All spec sections mapped.
- **AC mapping:** market×lane+version + explicit unknown-key error → T2/T3; buy-preview concentration field + fail-open reason → T4/T5; stamping contract in tool desc + README → T3/T7; migration 0 → global constraint + final check.
- **Type consistency:** `get_policy_for` view shape is defined once (T2) and consumed unchanged in T3; `evaluate_sector_concentration` return shape defined in T4 and asserted unchanged in T5; `policy_version_stamp()` shape `{version, content_hash}` consistent across T2/T3/T6.
- **Open impl-time confirmations flagged inline:** `_place_order_impl` local var names (T5 step 3), Toss estimated-value/currency locals (T5 step 4), universe `symbol` column name + position `sector` field (T4 step 3), `FastMCP` import path (T3 step 4), `tests/schemas` package init convention (T1 step 5). These are lookups, not design gaps.
