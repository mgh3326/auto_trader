# Screener Evidence → `/invest/reports` (PR1: G1/G3/G4/G5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the report `candidate_universe` snapshot carry real screener candidate evidence (symbols, normalized 0–10 scores, Korean reasons, source provenance, freshness) end-to-end, so the Hermes-facing stage and the auto-emitter consume movers instead of a bare count — with freshness-driven confidence caps and structured Korean missing-data.

**Architecture:** A new pure package `app/services/screener_evidence/` is the single source of truth that normalizes snapshot rows into `CandidateEvidence`. The report `candidate_universe` collector loads top-N rows (crypto via existing `list_latest`, equity via a new `list_top_candidates`), runs them through the builder, and replaces its count-only payload with a candidate-bearing one. `CandidateUniverseStage`, `EvidenceAutoEmitter`, and the crypto view-model path all consume the same evidence. No DB migration, no in-process LLM, no broker mutation.

**Tech Stack:** Python 3.13, SQLAlchemy async, Pydantic v2, pytest (`pytest-asyncio`, `db_session` fixture), `uv`.

**Spec:** `docs/superpowers/specs/2026-05-24-screener-evidence-for-reports-design.md`

**Conventions:**
- Run tests with `uv run pytest ... -v`.
- Commit trailer: `Co-Authored-By: Paperclip <noreply@paperclip.ing>`.
- Branch: `rob-304` (already at `origin/main`).

---

## File Structure

**Create:**
- `app/services/screener_evidence/__init__.py` — public exports
- `app/services/screener_evidence/models.py` — `CandidateEvidence`
- `app/services/screener_evidence/scoring.py` — deterministic 0–10 scoring
- `app/services/screener_evidence/builder.py` — `build_candidate_evidence`
- `tests/services/screener_evidence/__init__.py`
- `tests/services/screener_evidence/test_scoring.py`
- `tests/services/screener_evidence/test_builder.py`
- `tests/services/action_report/test_candidate_universe_collector_evidence.py`
- `tests/services/investment_stages/test_candidate_universe_stage_evidence.py`

**Modify:**
- `app/services/invest_screener_snapshots/repository.py` — add `list_top_candidates`
- `app/services/action_report/snapshot_backed/collectors/candidate_universe.py` — candidate-bearing payload
- `app/services/investment_stages/stages/candidate_universe.py` — consume candidates + confidence + Korean missing-data
- `app/services/action_report/snapshot_backed/auto_emit.py` — cite candidate evidence on buy items
- `app/services/invest_view_model/screener_service.py` — crypto `candidateContext` delegates to the builder
- `tests/test_invest_screener_snapshots_repository.py` — `list_top_candidates` test

---

## Task 1: `screener_evidence` package + `CandidateEvidence` model

**Files:**
- Create: `app/services/screener_evidence/__init__.py`
- Create: `app/services/screener_evidence/models.py`
- Create: `tests/services/screener_evidence/__init__.py`
- Test: `tests/services/screener_evidence/test_builder.py` (model round-trip portion)

- [ ] **Step 1: Write the failing test**

Create `tests/services/screener_evidence/__init__.py` (empty file).

Create `tests/services/screener_evidence/test_builder.py`:

```python
from app.services.screener_evidence.models import CandidateEvidence


def test_candidate_evidence_to_payload_dict_round_trips():
    ev = CandidateEvidence(
        symbol="KRW-BTC",
        market="crypto",
        name="비트코인",
        score=8.4,
        score_label="+4.20%",
        change_rate=4.2,
        price=95_000_000.0,
        volume_value=123_456_000_000.0,
        reasons=["단기 상승 모멘텀 후보"],
        source="tvscreener_upbit",
        risk_flags=[],
    )
    payload = ev.to_payload_dict()
    assert payload == {
        "symbol": "KRW-BTC",
        "market": "crypto",
        "name": "비트코인",
        "score": 8.4,
        "score_label": "+4.20%",
        "change_rate": 4.2,
        "price": 95_000_000.0,
        "volume_value": 123_456_000_000.0,
        "reasons": ["단기 상승 모멘텀 후보"],
        "source": "tvscreener_upbit",
        "risk_flags": [],
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/screener_evidence/test_builder.py::test_candidate_evidence_to_payload_dict_round_trips -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.screener_evidence'`.

- [ ] **Step 3: Write minimal implementation**

Create `app/services/screener_evidence/models.py`:

```python
"""Normalized candidate evidence shared by the screener view-model and the
report candidate_universe path (ROB-304). Deterministic; no LLM, no I/O."""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True)
class CandidateEvidence:
    symbol: str
    market: str  # "kr" | "us" | "crypto"
    name: str
    score: float  # normalized 0–10
    score_label: str  # Korean display, e.g. "RSI 28.3", "+4.20%"
    change_rate: float | None
    price: float | None
    volume_value: float | None  # turnover / 24h trade amount
    reasons: list[str]  # Korean reason strings
    source: str  # provenance: tvscreener_upbit / upbit_official / kis / yahoo / ...
    risk_flags: list[str]  # Korean risk labels, e.g. "Upbit 유의 종목"

    def to_payload_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
```

Create `app/services/screener_evidence/__init__.py`:

```python
"""Deterministic screener candidate-evidence builder (ROB-304)."""

from app.services.screener_evidence.builder import build_candidate_evidence
from app.services.screener_evidence.models import CandidateEvidence

__all__ = ["CandidateEvidence", "build_candidate_evidence"]
```

> Note: `__init__.py` imports `builder` (created in Task 3). Until then, run the model test by importing `models` directly (the test above imports `app.services.screener_evidence.models`, not the package root, so it passes independently).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/screener_evidence/test_builder.py::test_candidate_evidence_to_payload_dict_round_trips -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/screener_evidence/models.py tests/services/screener_evidence/__init__.py tests/services/screener_evidence/test_builder.py
git commit -m "feat(rob-304): add CandidateEvidence model

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: `scoring.py` — deterministic 0–10 preset scoring

Scoring kinds and their monotonic curves (all clamped to `[0, 10]`):
- `momentum` (crypto_momentum + equity top_gainers): `clamp(5 + change_rate/2, 0, 10)` where `change_rate` is in percent. (+10% → 10, 0% → 5, −10% → 0.)
- `oversold` (crypto_oversold): `clamp((50 - rsi) / 5 + 5, 0, 10)`. (RSI 30 → 9, 50 → 5, 70 → 1.)
- `high_volume` (crypto_high_volume): rank-based within the batch — handled in the builder (Task 3), not here, because it needs the full batch. `scoring.py` only exposes the per-row curves; the builder calls `rank_score` for volume.

**Files:**
- Create: `app/services/screener_evidence/scoring.py`
- Test: `tests/services/screener_evidence/test_scoring.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/screener_evidence/test_scoring.py`:

```python
import pytest

from app.services.screener_evidence import scoring


@pytest.mark.parametrize(
    ("change_rate", "expected"),
    [(10.0, 10.0), (0.0, 5.0), (-10.0, 0.0), (4.2, 7.1), (None, 0.0)],
)
def test_momentum_score(change_rate, expected):
    assert scoring.momentum_score(change_rate) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("rsi", "expected"),
    [(30.0, 9.0), (50.0, 5.0), (70.0, 1.0), (10.0, 10.0), (None, 0.0)],
)
def test_oversold_score(rsi, expected):
    assert scoring.oversold_score(rsi) == pytest.approx(expected)


def test_rank_score_descending_positions():
    # 4 items: best gets 10, worst gets 2.5 (10 * (1 - idx/n)).
    assert scoring.rank_score(0, 4) == pytest.approx(10.0)
    assert scoring.rank_score(3, 4) == pytest.approx(2.5)


def test_rank_score_single_item_is_max():
    assert scoring.rank_score(0, 1) == pytest.approx(10.0)


def test_clamp_bounds():
    assert scoring.clamp(12.0) == 10.0
    assert scoring.clamp(-1.0) == 0.0
    assert scoring.clamp(6.3) == 6.3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/screener_evidence/test_scoring.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.screener_evidence.scoring'`.

- [ ] **Step 3: Write minimal implementation**

Create `app/services/screener_evidence/scoring.py`:

```python
"""Deterministic 0–10 candidate scoring curves (ROB-304).

Each curve is monotonic and documented so the report's
``score >= 7.0 → BULL`` branch is meaningful and reproducible."""

from __future__ import annotations


def clamp(value: float, low: float = 0.0, high: float = 10.0) -> float:
    return max(low, min(high, value))


def momentum_score(change_rate: float | None) -> float:
    """+10% → 10, 0% → 5, −10% → 0. ``None`` → 0."""
    if change_rate is None:
        return 0.0
    return clamp(5.0 + change_rate / 2.0)


def oversold_score(rsi: float | None) -> float:
    """Lower RSI → higher score. RSI 30 → 9, 50 → 5, 70 → 1. ``None`` → 0."""
    if rsi is None:
        return 0.0
    return clamp((50.0 - rsi) / 5.0 + 5.0)


def rank_score(index: int, count: int) -> float:
    """Rank-based score for batch metrics (volume). Best (index 0) → 10."""
    if count <= 1:
        return 10.0
    return clamp(10.0 * (1.0 - index / count))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/screener_evidence/test_scoring.py -v`
Expected: PASS (6 cases).

- [ ] **Step 5: Commit**

```bash
git add app/services/screener_evidence/scoring.py tests/services/screener_evidence/test_scoring.py
git commit -m "feat(rob-304): deterministic 0-10 candidate scoring curves

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: `builder.py` — `build_candidate_evidence`

Input: a list of normalized row dicts (caller-supplied; ORM→dict adapters live in the callers). Expected dict keys (all optional except `symbol`): `symbol`, `name`, `source`, `change_rate`, `price`, `rsi`, `adx`, `trade_amount_24h`, `volume_24h`, `market_cap`, `market_warning`, `consecutive_up_days`, `daily_volume`.

Report preset constants (caller passes one):
- crypto: `"crypto_momentum"` (default), `"crypto_oversold"`, `"crypto_high_volume"`
- equity: `"top_gainers"`

Korean reasons / score labels exactly match the existing view-model (`screener_service._crypto_candidate_context`) so Task 8 produces identical output.

**Files:**
- Create: `app/services/screener_evidence/builder.py`
- Test: `tests/services/screener_evidence/test_builder.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/services/screener_evidence/test_builder.py`)

```python
from app.services.screener_evidence import build_candidate_evidence


def _crypto_row(symbol, name, change_rate, rsi, trade_amount, *, warning=False):
    return {
        "symbol": symbol,
        "name": name,
        "source": "tvscreener_upbit",
        "change_rate": change_rate,
        "price": 100.0,
        "rsi": rsi,
        "trade_amount_24h": trade_amount,
        "market_warning": warning,
    }


def test_builder_crypto_momentum_scores_and_sorts_desc():
    rows = [
        _crypto_row("KRW-AAA", "에이", 2.0, 55.0, 10.0),
        _crypto_row("KRW-BBB", "비이", 8.0, 60.0, 20.0),
    ]
    out = build_candidate_evidence(market="crypto", preset="crypto_momentum", rows=rows)
    assert [e.symbol for e in out] == ["KRW-BBB", "KRW-AAA"]  # higher change first
    top = out[0]
    assert top.score == 9.0  # clamp(5 + 8/2)
    assert top.score_label == "+8.00%"
    assert top.reasons == ["단기 상승 모멘텀 후보"]
    assert top.source == "tvscreener_upbit"
    assert top.market == "crypto"


def test_builder_crypto_oversold_uses_rsi_label_and_reason():
    rows = [_crypto_row("KRW-CCC", "씨이", -1.0, 28.0, 5.0)]
    out = build_candidate_evidence(market="crypto", preset="crypto_oversold", rows=rows)
    assert out[0].score_label == "RSI 28.0"
    assert out[0].reasons == ["RSI 저점권 후보"]
    assert out[0].score == 9.4  # clamp((50-28)/5 + 5)


def test_builder_crypto_high_volume_rank_score_and_label():
    rows = [
        _crypto_row("KRW-HI", "하이", 1.0, 50.0, 999.0),
        _crypto_row("KRW-LO", "로우", 1.0, 50.0, 1.0),
    ]
    out = build_candidate_evidence(market="crypto", preset="crypto_high_volume", rows=rows)
    assert out[0].symbol == "KRW-HI"
    assert out[0].score == 10.0
    assert out[0].reasons == ["24시간 KRW 거래대금 상위"]
    assert out[0].score_label == "거래대금 999"


def test_builder_marks_market_warning_risk_flag():
    rows = [_crypto_row("KRW-WARN", "워언", 1.0, 50.0, 5.0, warning=True)]
    out = build_candidate_evidence(market="crypto", preset="crypto_momentum", rows=rows)
    assert out[0].risk_flags == ["Upbit 유의 종목"]


def test_builder_equity_top_gainers_uses_change_rate_and_source():
    rows = [
        {"symbol": "005930", "name": "삼성전자", "source": "kis",
         "change_rate": 3.0, "price": 78500.0, "daily_volume": 14_000_000,
         "consecutive_up_days": 3},
    ]
    out = build_candidate_evidence(market="kr", preset="top_gainers", rows=rows)
    assert out[0].source == "kis"
    assert out[0].score == 6.5  # clamp(5 + 3/2)
    assert out[0].score_label == "+3.00%"
    assert out[0].reasons == ["단기 상승 모멘텀 후보", "3일 연속 상승"]
    assert out[0].volume_value == 14_000_000.0


def test_builder_empty_rows_returns_empty():
    assert build_candidate_evidence(market="crypto", preset="crypto_momentum", rows=[]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/screener_evidence/test_builder.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_candidate_evidence'`.

- [ ] **Step 3: Write minimal implementation**

Create `app/services/screener_evidence/builder.py`:

```python
"""Pure transformer from normalized screener rows to CandidateEvidence
(ROB-304). No DB access, no I/O — fixture-testable."""

from __future__ import annotations

from typing import Any

from app.services.screener_evidence import scoring
from app.services.screener_evidence.models import CandidateEvidence

_MOMENTUM_REASON = "단기 상승 모멘텀 후보"
_OVERSOLD_REASON = "RSI 저점권 후보"
_HIGH_VOLUME_REASON = "24시간 KRW 거래대금 상위"
_WARNING_FLAG = "Upbit 유의 종목"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _source_of(row: dict[str, Any], market: str) -> str:
    raw = str(row.get("source") or "").strip().lower()
    if market == "crypto":
        if raw in {"tvscreener", "tvscreener_upbit"}:
            return "tvscreener_upbit"
        if raw in {"upbit", "upbit_official"}:
            return "upbit_official"
        return "external_reference" if raw else "mcp_screen_stocks"
    # equity
    if raw in {"kis", "yahoo"}:
        return raw
    return "external_reference" if raw else "mcp_screen_stocks"


def _risk_flags(row: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if row.get("market_warning") or row.get("warning"):
        flags.append(_WARNING_FLAG)
    return flags


def build_candidate_evidence(
    *, market: str, preset: str, rows: list[dict[str, Any]]
) -> list[CandidateEvidence]:
    """Normalize rows into scored, sorted (desc) CandidateEvidence."""
    if not rows:
        return []

    # high_volume needs batch ranking by turnover.
    volume_rank: dict[int, int] = {}
    if preset == "crypto_high_volume":
        ordered = sorted(
            range(len(rows)),
            key=lambda i: _to_float(rows[i].get("trade_amount_24h")) or 0.0,
            reverse=True,
        )
        volume_rank = {row_idx: rank for rank, row_idx in enumerate(ordered)}

    out: list[CandidateEvidence] = []
    for idx, row in enumerate(rows):
        change_rate = _to_float(row.get("change_rate"))
        rsi = _to_float(row.get("rsi"))
        price = _to_float(row.get("price") or row.get("latest_close"))

        if preset == "crypto_oversold":
            score = scoring.oversold_score(rsi)
            score_label = f"RSI {rsi:.1f}" if rsi is not None else "-"
            reasons = [_OVERSOLD_REASON]
            volume_value = _to_float(row.get("trade_amount_24h"))
        elif preset == "crypto_high_volume":
            volume_value = _to_float(row.get("trade_amount_24h"))
            score = scoring.rank_score(volume_rank.get(idx, idx), len(rows))
            score_label = (
                f"거래대금 {int(volume_value):,}" if volume_value is not None else "-"
            )
            reasons = [_HIGH_VOLUME_REASON]
        else:  # crypto_momentum + equity top_gainers
            score = scoring.momentum_score(change_rate)
            score_label = f"{change_rate:+.2f}%" if change_rate is not None else "-"
            reasons = [_MOMENTUM_REASON]
            volume_value = _to_float(
                row.get("trade_amount_24h") or row.get("daily_volume")
            )
            up_days = row.get("consecutive_up_days")
            if isinstance(up_days, int) and up_days >= 2:
                reasons.append(f"{up_days}일 연속 상승")

        out.append(
            CandidateEvidence(
                symbol=str(row.get("symbol")),
                market=market,
                name=str(row.get("name") or row.get("symbol") or ""),
                score=round(score, 4),
                score_label=score_label,
                change_rate=change_rate,
                price=price,
                volume_value=volume_value,
                reasons=reasons,
                source=_source_of(row, market),
                risk_flags=_risk_flags(row),
            )
        )

    out.sort(key=lambda e: e.score, reverse=True)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/screener_evidence/ -v`
Expected: PASS (all model + scoring + builder tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/screener_evidence/builder.py app/services/screener_evidence/__init__.py tests/services/screener_evidence/test_builder.py
git commit -m "feat(rob-304): screener candidate-evidence builder

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 4: equity repo `list_top_candidates`

Loads the latest available partition's top movers for a market, ordered by `change_rate` desc. Decoupled from the `snapshot_date == today` "fresh" definition (freshness is derived separately by the collector via `coverage`).

**Files:**
- Modify: `app/services/invest_screener_snapshots/repository.py`
- Test: `tests/test_invest_screener_snapshots_repository.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_invest_screener_snapshots_repository.py`)

```python
@pytest.mark.asyncio
async def test_list_top_candidates_orders_by_change_rate_from_latest_partition(db_session):
    repo = InvestScreenerSnapshotsRepository(db_session)
    base = dict(market="us", snapshot_date=dt.date(2026, 5, 22), source="yahoo")
    await repo.upsert(SnapshotUpsert(symbol="T_TOP_A", latest_close=Decimal("10"),
                                     change_rate=Decimal("1.0"), closes_window=[10], **base))
    await repo.upsert(SnapshotUpsert(symbol="T_TOP_B", latest_close=Decimal("10"),
                                     change_rate=Decimal("9.0"), closes_window=[10], **base))
    # An older partition row that must be excluded (not latest).
    await repo.upsert(SnapshotUpsert(symbol="T_TOP_OLD", latest_close=Decimal("10"),
                                     change_rate=Decimal("50.0"), closes_window=[10],
                                     market="us", snapshot_date=dt.date(2026, 5, 1),
                                     source="yahoo"))
    await db_session.commit()

    rows = await repo.list_top_candidates(market="us", limit=10)
    syms = [r.symbol for r in rows if r.symbol in {"T_TOP_A", "T_TOP_B", "T_TOP_OLD"}]
    assert syms == ["T_TOP_B", "T_TOP_A"]  # latest partition only, change_rate desc
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_screener_snapshots_repository.py::test_list_top_candidates_orders_by_change_rate_from_latest_partition -v`
Expected: FAIL — `AttributeError: 'InvestScreenerSnapshotsRepository' object has no attribute 'list_top_candidates'`.

- [ ] **Step 3: Write minimal implementation** (add method to `InvestScreenerSnapshotsRepository`, after `coverage`)

```python
    async def latest_partition(self, *, market: str) -> dt.date | None:
        result = await self._session.execute(
            select(func.max(InvestScreenerSnapshot.snapshot_date)).where(
                InvestScreenerSnapshot.market == market
            )
        )
        return result.scalar_one_or_none()

    async def list_top_candidates(
        self, *, market: str, limit: int = 10
    ) -> list[InvestScreenerSnapshot]:
        latest = await self.latest_partition(market=market)
        if latest is None:
            return []
        result = await self._session.execute(
            select(InvestScreenerSnapshot)
            .where(
                InvestScreenerSnapshot.market == market,
                InvestScreenerSnapshot.snapshot_date == latest,
            )
            .order_by(
                InvestScreenerSnapshot.change_rate.desc().nullslast(),
                InvestScreenerSnapshot.symbol.asc(),
            )
            .limit(limit)
        )
        return list(result.scalars().all())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_invest_screener_snapshots_repository.py::test_list_top_candidates_orders_by_change_rate_from_latest_partition -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_screener_snapshots/repository.py tests/test_invest_screener_snapshots_repository.py
git commit -m "feat(rob-304): equity list_top_candidates from latest partition

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 5: collector — candidate-bearing payload (replaces counts-only)

The collector keeps `coverage()` for fresh/stale counts + `usefulness`, but now also loads top-N rows, runs the builder, and emits `candidates`, `source_coverage`, `preset`, and structured `missing_data`. ORM→dict adapters live here. `TOP_N = 10`.

Freshness mapping (PR1, deliberately mirrors existing `usefulness`; does not redefine equity freshness semantics): `useful → "fresh"`, `stale_only → "stale"`, `empty → "missing"`.

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/candidate_universe.py`
- Test: `tests/services/action_report/test_candidate_universe_collector_evidence.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/action_report/test_candidate_universe_collector_evidence.py` (create `tests/services/action_report/__init__.py` first if missing — `test -f` it; create empty if absent):

```python
import datetime as dt
from decimal import Decimal

import pytest

from app.models.invest_crypto_screener_snapshot import InvestCryptoScreenerSnapshot
from app.services.action_report.snapshot_backed.collectors.candidate_universe import (
    CandidateUniverseSnapshotCollector,
)
from app.services.investment_snapshots.collectors import CollectorRequest


@pytest.mark.asyncio
async def test_crypto_collector_emits_candidate_evidence(db_session):
    db_session.add(
        InvestCryptoScreenerSnapshot(
            symbol="KRW-XRP", snapshot_date=dt.date(2026, 5, 23), name="리플",
            latest_close=Decimal("3000"), change_rate=Decimal("8.0"),
            trade_amount_24h=Decimal("500000000"), source="tvscreener_upbit",
        )
    )
    await db_session.commit()

    collector = CandidateUniverseSnapshotCollector(db_session)
    results = await collector.collect(
        CollectorRequest(market="crypto", account_scope=None, symbols=[])
    )
    payload = results[0].payload_json
    assert payload["candidates"], "expected candidate evidence rows"
    top = payload["candidates"][0]
    assert top["symbol"] == "KRW-XRP"
    assert top["score"] == 9.0
    assert top["reasons"] == ["단기 상승 모멘텀 후보"]
    assert payload["source_coverage"] == {"tvscreener_upbit": 1}
    assert payload["usefulness"] == "useful"


@pytest.mark.asyncio
async def test_crypto_collector_empty_sets_structured_missing_data(db_session):
    collector = CandidateUniverseSnapshotCollector(db_session)
    results = await collector.collect(
        CollectorRequest(market="crypto", account_scope=None, symbols=[])
    )
    payload = results[0].payload_json
    assert payload["candidates"] == []
    assert payload["usefulness"] == "empty"
    assert payload["missing_data"]["confidence_impact"] == "cap 20"
    assert "암호화폐" in payload["missing_data"]["what"]
```

> `CollectorRequest` field names: verify with `grep -n "class CollectorRequest" app/services/investment_snapshots/collectors.py` before writing — adjust the kwargs (`market`, `account_scope`, `symbols`) to match the dataclass exactly.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py -v`
Expected: FAIL — `KeyError: 'candidates'` (current payload has no `candidates`).

- [ ] **Step 3: Write implementation** — replace the body of `app/services/action_report/snapshot_backed/collectors/candidate_universe.py` below the imports. Add these imports at top:

```python
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.services.invest_crypto_screener_snapshots.repository import (
    InvestCryptoScreenerSnapshotsRepository,
)
from app.services.screener_evidence import build_candidate_evidence
```

Add module constant and adapters near the top of the module:

```python
TOP_N = 10

_FRESHNESS_BY_USEFULNESS = {"useful": "fresh", "stale_only": "stale", "empty": "missing"}


def _equity_row_to_input(row: InvestScreenerSnapshot) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "name": row.symbol,
        "source": row.source,
        "change_rate": row.change_rate,
        "price": row.latest_close,
        "daily_volume": row.daily_volume,
        "consecutive_up_days": row.consecutive_up_days,
    }


def _crypto_row_to_input(row: InvestCryptoScreenerSnapshot) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "name": row.name,
        "source": row.source,
        "change_rate": row.change_rate,
        "price": row.latest_close,
        "rsi": row.rsi,
        "adx": row.adx,
        "trade_amount_24h": row.trade_amount_24h,
        "volume_24h": row.volume_24h,
        "market_cap": row.market_cap,
        "market_warning": row.market_warning,
    }


def _source_coverage(evidence: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ev in evidence:
        counts[ev.source] = counts.get(ev.source, 0) + 1
    return counts


def _missing_data(market: str, usefulness: str) -> dict[str, str] | None:
    if usefulness == "useful":
        return None
    market_ko = {"crypto": "암호화폐", "kr": "국내", "us": "미국"}.get(market, market)
    if usefulness == "stale_only":
        return {
            "what": f"{market_ko} 스크리너 스냅샷이 최신 거래일 기준이 아닙니다 (stale).",
            "why": "최신 모멘텀/거래대금 교차검증이 제한되어 신규 후보 판단 신뢰도가 낮아집니다.",
            "next": "스크리너 스냅샷 리프레시가 최신 거래일로 갱신되면 개선됩니다.",
            "confidence_impact": "cap 40",
        }
    return {
        "what": f"{market_ko} 스크리너 스냅샷이 비어 있습니다.",
        "why": "후보 유니버스를 평가할 수 없어 신규 매수 후보 판단 신뢰도가 제한됩니다.",
        "next": "스크리너 스냅샷 리프레시가 활성화되면 개선됩니다.",
        "confidence_impact": "cap 20",
    }
```

Replace `_collect_equity` and `_collect_crypto` so each builds candidate evidence:

```python
    async def _collect_equity(
        self, request: CollectorRequest, now: dt.datetime
    ) -> list[SnapshotCollectResult]:
        today = now.date()
        coverage = await self._equity_repo.coverage(
            market=request.market, today_trading_date=today
        )
        usefulness, _reason = _classify_usefulness(
            actionable=coverage.fresh_count, stale=coverage.stale_count
        )
        rows = await self._equity_repo.list_top_candidates(
            market=request.market, limit=TOP_N
        )
        evidence = build_candidate_evidence(
            market=request.market,
            preset="top_gainers",
            rows=[_equity_row_to_input(r) for r in rows],
        )
        return [
            self._build_candidate_result(
                request=request,
                now=now,
                market=request.market,
                preset="top_gainers",
                evidence=evidence,
                fresh_count=coverage.fresh_count,
                stale_count=coverage.stale_count,
                last_computed_at=coverage.last_computed_at,
                usefulness=usefulness,
            )
        ]

    async def _collect_crypto(
        self, request: CollectorRequest, now: dt.datetime
    ) -> list[SnapshotCollectResult]:
        crypto_repo = InvestCryptoScreenerSnapshotsRepository(self._session)
        cov = await crypto_repo.coverage(today=now.date())
        usefulness, _reason = _classify_usefulness(
            actionable=cov.latest_partition_count, stale=cov.stale_count
        )
        rows = await crypto_repo.list_latest(preset_id="crypto_momentum", limit=TOP_N)
        evidence = build_candidate_evidence(
            market="crypto",
            preset="crypto_momentum",
            rows=[_crypto_row_to_input(r) for r in rows],
        )
        return [
            self._build_candidate_result(
                request=request,
                now=now,
                market="crypto",
                preset="crypto_momentum",
                evidence=evidence,
                fresh_count=cov.latest_partition_count,
                stale_count=cov.stale_count,
                last_computed_at=cov.last_computed_at,
                usefulness=usefulness,
            )
        ]

    def _build_candidate_result(
        self,
        *,
        request: CollectorRequest,
        now: dt.datetime,
        market: str,
        preset: str,
        evidence: list,
        fresh_count: int,
        stale_count: int,
        last_computed_at: dt.datetime | None,
        usefulness: str,
    ) -> SnapshotCollectResult:
        freshness_status = _FRESHNESS_BY_USEFULNESS.get(usefulness, "partial")
        candidates = [e.to_payload_dict() for e in evidence]
        missing = _missing_data(market, usefulness)
        payload: dict[str, Any] = {
            "market": market,
            "preset": preset,
            "as_of": now.isoformat(),
            "freshness_status": freshness_status,
            "source_coverage": _source_coverage(evidence),
            "candidates": candidates,
            "fresh_count": fresh_count,
            "actionable_count": fresh_count,
            "stale_count": stale_count,
            "last_computed_at": last_computed_at,
            "usefulness": usefulness,
            "missing_data": missing,
        }
        return build_result(
            snapshot_kind=self.snapshot_kind,
            market=request.market,
            account_scope=request.account_scope,
            payload=payload,
            origin="auto_trader_db",
            as_of=now,
            freshness_status=freshness_status if usefulness != "useful" else "fresh",
            coverage={
                "actionable_count": fresh_count,
                "stale_count": stale_count,
                "usefulness": usefulness,
                "candidate_count": len(candidates),
            },
        )
```

Remove the now-unused `_classify_usefulness` second return value usages and the old inline payload builders. Keep the top-level `collect()` try/except fail-open wrapper unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/action_report/test_candidate_universe_collector_evidence.py -v`
Expected: PASS (2 cases).

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/candidate_universe.py tests/services/action_report/
git commit -m "feat(rob-304): candidate_universe collector emits candidate evidence + provenance + missing-data

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 6: stage — consume candidates, freshness-capped confidence, Korean missing-data

**Files:**
- Modify: `app/services/investment_stages/stages/candidate_universe.py`
- Test: `tests/services/investment_stages/test_candidate_universe_stage_evidence.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/investment_stages/__init__.py` if missing (empty). Create `tests/services/investment_stages/test_candidate_universe_stage_evidence.py`:

```python
import uuid

import pytest

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)
from app.services.investment_stages.stages.candidate_universe import (
    CandidateUniverseStage,
)


class _Snap:
    def __init__(self, payload):
        self.snapshot_uuid = uuid.uuid4()
        self.payload_json = payload


def _ctx(payload):
    return StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={"candidate_universe": [_Snap(payload)]},
        bundle_metadata={},
    )


@pytest.mark.asyncio
async def test_stage_bull_from_high_score_candidate():
    payload = {
        "freshness_status": "fresh",
        "source_coverage": {"tvscreener_upbit": 2},
        "candidates": [
            {"symbol": "KRW-BTC", "score": 8.5, "reasons": ["단기 상승 모멘텀 후보"],
             "source": "tvscreener_upbit"},
        ],
        "missing_data": None,
    }
    out = await CandidateUniverseStage().run(_ctx(payload))
    assert out.verdict == StageVerdict.BULL
    assert out.buy_evidence == ["KRW-BTC"]
    assert any("KRW-BTC" in kp for kp in out.key_points)
    assert out.confidence >= 40


@pytest.mark.asyncio
async def test_stage_stale_caps_confidence_and_sets_korean_missing_data():
    payload = {
        "freshness_status": "stale",
        "source_coverage": {"tvscreener_upbit": 1},
        "candidates": [
            {"symbol": "KRW-BTC", "score": 9.0, "reasons": ["단기 상승 모멘텀 후보"],
             "source": "tvscreener_upbit"},
        ],
        "missing_data": {"what": "암호화폐 스크리너 스냅샷이 최신 거래일 기준이 아닙니다 (stale).",
                          "why": "x", "next": "y", "confidence_impact": "cap 40"},
    }
    out = await CandidateUniverseStage().run(_ctx(payload))
    assert out.confidence <= 40
    assert out.missing_data and "stale" in out.missing_data[0]
    assert out.freshness_summary["candidate_universe"]["confidence_impact"] == "cap 40"


@pytest.mark.asyncio
async def test_stage_empty_is_neutral_low_confidence():
    payload = {"freshness_status": "missing", "source_coverage": {}, "candidates": [],
               "missing_data": {"what": "암호화폐 스크리너 스냅샷이 비어 있습니다.",
                                "why": "x", "next": "y", "confidence_impact": "cap 20"}}
    out = await CandidateUniverseStage().run(_ctx(payload))
    assert out.verdict == StageVerdict.NEUTRAL
    assert out.confidence == 20


@pytest.mark.asyncio
async def test_stage_missing_snapshot_raises():
    ctx = StageContext(bundle_uuid=uuid.uuid4(), snapshots_by_kind={}, bundle_metadata={})
    with pytest.raises(UnavailableStageError):
        await CandidateUniverseStage().run(ctx)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/investment_stages/test_candidate_universe_stage_evidence.py -v`
Expected: FAIL — current stage reads `candidates` but ignores `freshness_status`/`missing_data`; confidence + missing_data assertions fail.

- [ ] **Step 3: Write implementation** — replace `app/services/investment_stages/stages/candidate_universe.py` body of `run`:

```python
    async def run(self, context: StageContext) -> StageArtifactPayload:
        snapshots = context.snapshots_for("candidate_universe")
        if not snapshots:
            raise UnavailableStageError("candidate_universe snapshot missing")
        snap = snapshots[0]
        payload = snap.payload_json or {}
        candidates = payload.get("candidates", [])
        freshness_status = payload.get("freshness_status", "missing")
        source_coverage = payload.get("source_coverage", {}) or {}
        missing = payload.get("missing_data")

        top = sorted(candidates, key=lambda c: c.get("score", 0.0), reverse=True)[:5]

        if not top:
            verdict = StageVerdict.NEUTRAL
            base = 20
            summary = "스크리너 후보 없음"
        elif top[0].get("score", 0.0) >= 7.0:
            verdict = StageVerdict.BULL
            base = min(40 + len(top) * 8, 75)
            summary = "상위 후보: " + ", ".join(c.get("symbol", "?") for c in top)
        else:
            verdict = StageVerdict.NEUTRAL
            base = 35
            summary = "후보는 있으나 점수 낮음"

        confidence = _cap_confidence(base, freshness_status, len(source_coverage))

        key_points = [
            f"{c.get('symbol', '?')} (score={c.get('score', 0):.1f}): "
            f"{', '.join(c.get('reasons', []))} [{c.get('source', '?')}]"
            for c in top
        ]
        missing_lines: list[str] = []
        freshness_summary = None
        if missing:
            missing_lines = [missing.get("what", ""), missing.get("why", "")]
            missing_lines = [m for m in missing_lines if m]
            freshness_summary = {"candidate_universe": missing}

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=verdict,
            confidence=confidence,
            summary=summary,
            key_points=key_points,
            buy_evidence=[c.get("symbol", "?") for c in top]
            if verdict == StageVerdict.BULL
            else [],
            missing_data=missing_lines,
            freshness_summary=freshness_summary,
            cited_snapshots=[
                StageCitation(
                    snapshot_uuid=snap.snapshot_uuid,
                    snapshot_kind="candidate_universe",
                    payload_path="$.candidates",
                )
            ],
        )
```

Add the confidence-cap helper at module level (above the class):

```python
_FRESHNESS_CAP = {"fresh": 100, "partial": 60, "stale": 40, "missing": 20}


def _cap_confidence(base: int, freshness_status: str, source_count: int) -> int:
    cap = _FRESHNESS_CAP.get(freshness_status, 40)
    confidence = min(base, cap)
    if source_count <= 1:
        confidence = min(confidence, 65)
    return confidence
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/investment_stages/test_candidate_universe_stage_evidence.py -v`
Expected: PASS (4 cases).

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/stages/candidate_universe.py tests/services/investment_stages/
git commit -m "feat(rob-304): candidate_universe stage consumes evidence + freshness-capped confidence + Korean missing-data

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 7: auto_emit — cite candidate evidence on buy items

Build a `symbol → candidate` map from the new payload and attach `candidate_score`/`candidate_reasons`/`candidate_source` to the buy-item evidence. Keeps all lockdown invariants (buy still requires actionable quote; held still excluded; `operation="review"`).

**Files:**
- Modify: `app/services/action_report/snapshot_backed/auto_emit.py`
- Test: `tests/test_auto_emit_candidate_citation.py` (new) — or extend an existing auto_emit test file if one exists (`grep -rl EvidenceAutoEmitter tests/`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_auto_emit_candidate_citation.py`:

```python
import pytest

from app.services.action_report.snapshot_backed.auto_emit import EvidenceAutoEmitter


class _Snap:
    def __init__(self, kind, payload, symbol=None):
        self.snapshot_kind = kind
        self.payload_json = payload
        self.symbol = symbol
        self.snapshot_uuid = "11111111-1111-1111-1111-111111111111"


_OK_QUOTE = {"status": "ok", "best_bid": 100, "best_ask": 101,
             "bid_depth": 5, "ask_depth": 5, "spread_bps": 10}


def test_buy_item_cites_candidate_evidence():
    snaps = [
        _Snap("portfolio", {"primary_source": "kis", "holdings": []}),
        _Snap("symbol", {"symbol": "005930", "quote": _OK_QUOTE}, symbol="005930"),
        _Snap("candidate_universe", {
            "usefulness": "useful",
            "candidates": [
                {"symbol": "005930", "score": 8.0,
                 "reasons": ["단기 상승 모멘텀 후보"], "source": "kis"},
            ],
        }),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="kr", account_scope=None
    )
    buys = [i for i in items if i.side == "buy"]
    assert buys, "expected a buy candidate"
    ev = buys[0].evidence_snapshot
    assert ev["candidate_score"] == 8.0
    assert ev["candidate_source"] == "kis"
    assert ev["candidate_reasons"] == ["단기 상승 모멘텀 후보"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_auto_emit_candidate_citation.py -v`
Expected: FAIL — `KeyError: 'candidate_score'`.

- [ ] **Step 3: Write implementation**

In `auto_emit.py`, in the loop that records `candidate_snapshot` (the `elif kind == "candidate_universe":` branch around line 137), also capture the candidates list:

```python
            elif kind == "candidate_universe":
                candidate_snapshot = snapshot
                candidate_usefulness = (
                    payload.get("usefulness") if isinstance(payload, dict) else None
                )
                raw_candidates = (
                    payload.get("candidates", []) if isinstance(payload, dict) else []
                )
                for cand in raw_candidates:
                    if isinstance(cand, dict) and isinstance(cand.get("symbol"), str):
                        candidate_by_symbol[cand["symbol"]] = cand
```

Declare `candidate_by_symbol: dict[str, dict[str, Any]] = {}` next to the other accumulators (near `candidate_usefulness: str | None = None`).

In the buy-candidate `extra=` dict (around line 209), add the citation fields:

```python
                cand = candidate_by_symbol.get(sym, {})
                evidence = _make_evidence(
                    symbol_snapshot,
                    extra={
                        "candidate_snapshot_uuid": _snapshot_uuid(candidate_snapshot),
                        "candidate_usefulness": candidate_usefulness,
                        "candidate_score": cand.get("score"),
                        "candidate_reasons": cand.get("reasons"),
                        "candidate_source": cand.get("source"),
                        "news_matches": news_matches.get(sym, 0),
                        "quote_status": quote.get("status"),
                        "best_bid": quote.get("best_bid"),
                        "best_ask": quote.get("best_ask"),
                        "spread_bps": quote.get("spread_bps"),
                        "proposer": "auto_emit/buy_from_candidate",
                    },
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_auto_emit_candidate_citation.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/snapshot_backed/auto_emit.py tests/test_auto_emit_candidate_citation.py
git commit -m "feat(rob-304): auto_emit cites candidate evidence on buy items

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 8: view-model — crypto `candidateContext` delegates to the builder (single source of truth)

Replace the body of `_crypto_candidate_context` so it calls `build_candidate_evidence` and maps the first result to `ScreenerCandidateContext`. Output must remain identical to today's labels/reasons (the builder was designed to match), so existing screener tests stay green.

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py`
- Test: existing `tests/test_invest_view_model_screener_service.py` (regression) + a focused equivalence test.

- [ ] **Step 1: Write the failing test** — append to `tests/test_invest_view_model_screener_service.py`:

```python
def test_crypto_candidate_context_matches_builder_labels():
    from app.services.invest_view_model.screener_service import _crypto_candidate_context

    row = {"symbol": "KRW-BTC", "source": "tvscreener_upbit", "change_rate": 4.2,
           "rsi": 40.0, "trade_amount_24h": 123456}
    ctx = _crypto_candidate_context(row, "crypto_momentum")
    assert ctx is not None
    assert ctx.scoreLabel == "+4.20%"
    assert ctx.reasons == ["단기 상승 모멘텀 후보"]
    assert ctx.source == "tvscreener_upbit"
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py::test_crypto_candidate_context_matches_builder_labels -v`
Expected: PASS already (current code produces these labels). This test pins the contract before the refactor — if it fails, the builder labels diverge and must be reconciled before proceeding.

- [ ] **Step 3: Refactor `_crypto_candidate_context`** to delegate:

```python
def _crypto_candidate_context(
    row: dict[str, Any], preset_id: str
) -> ScreenerCandidateContext | None:
    from app.services.screener_evidence import build_candidate_evidence

    evidence = build_candidate_evidence(market="crypto", preset=preset_id, rows=[row])
    if not evidence:
        return None
    ev = evidence[0]
    if not ev.reasons:
        return None
    return ScreenerCandidateContext(
        scoreLabel=ev.score_label,
        reasons=ev.reasons,
        source=ev.source,  # type: ignore[arg-type]
    )
```

> The builder always returns reasons for known presets, so the `crypto_high_volume` "no trade_amount → None" edge differs slightly: the old code returned `None` when `trade_amount` was missing. If `tests/test_invest_view_model_screener_service.py` has a case asserting `None` for a missing-metric crypto row, preserve it by guarding: return `None` when `ev.score_label == "-"`. Add that guard if the regression run in Step 4 surfaces it.

- [ ] **Step 4: Run the full screener view-model suite**

Run: `uv run pytest tests/test_invest_view_model_screener_service.py -v`
Expected: PASS (including the ROB-288 injected-`now` tests). If any candidate-context case fails, apply the `score_label == "-"` guard noted above and re-run.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/screener_service.py tests/test_invest_view_model_screener_service.py
git commit -m "refactor(rob-304): crypto candidateContext delegates to shared evidence builder

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 9: Full targeted suite + lint/type + import-guard

**Files:** none (verification only).

- [ ] **Step 1: Run the full affected test surface**

Run:
```bash
uv run pytest \
  tests/services/screener_evidence/ \
  tests/services/action_report/ \
  tests/services/investment_stages/ \
  tests/test_invest_screener_snapshots_repository.py \
  tests/test_auto_emit_candidate_citation.py \
  tests/test_invest_view_model_screener_service.py -v
```
Expected: all PASS.

- [ ] **Step 2: Run the LLM/import static guard (ROB-287 / PR #898)**

Run: `grep -rln "import-guard\|static.*import.*guard\|no_inprocess_llm\|in_process_llm" tests/ | head` to find the guard test, then run it (e.g. `uv run pytest <that_file> -v`).
Expected: PASS — the new `screener_evidence` package and edits introduce no LLM provider imports.

- [ ] **Step 3: Lint + typecheck**

Run: `make lint` (Ruff + ty). Fix any findings.
Expected: clean.

- [ ] **Step 4: Run the broader report/stage suites for regressions**

Run: `uv run pytest tests/ -k "candidate_universe or auto_emit or screener" -v`
Expected: all PASS.

- [ ] **Step 5: Final commit (if lint/format changed anything)**

```bash
git add -A
git commit -m "chore(rob-304): lint/format pass for screener evidence PR1

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Self-Review (against the spec)

**Spec coverage:**
- §1 shared builder → Tasks 1–3; consumed by collector (T5), stage (T6), view-model (T8). ✓
- §2 `CandidateEvidence` + scoring → Tasks 1–2. ✓
- §3 collector payload (candidates/source_coverage/preset/missing_data/freshness) → Task 5. ✓ (G1, G3)
- §4 both consumers updated → stage (T6), auto_emit (T7). ✓
- §5 freshness-capped confidence → Task 6 `_cap_confidence`. ✓ (G5)
- §5b structured Korean missing-data → collector `_missing_data` (T5) surfaced by stage (T6). ✓ (G4)
- §6 held cross-check → **PR2, not in this plan.** ✓ (intentionally deferred)
- §7 no DB migration → confirmed; only JSONB payload + read-only new query. ✓
- §8 tests → Tasks 1–9. ✓
- §10 assumptions → `StageArtifactPayload` already has `missing_data`/`freshness_summary` (used as-is, no type change); view-model equivalence pinned in T8 Step 1. ✓

**Placeholder scan:** No TBD/TODO. Two "verify before writing" notes (CollectorRequest field names in T5; guard-test filename in T9) are explicit grep instructions, not deferred work.

**Type consistency:** `build_candidate_evidence(*, market, preset, rows)` signature identical across T3/T5/T8. `CandidateEvidence.to_payload_dict()` keys match the collector payload and stage/auto_emit readers (`symbol`, `score`, `reasons`, `source`). `_cap_confidence` / `_FRESHNESS_CAP` defined once in T6. `list_top_candidates(*, market, limit)` defined in T4, called in T5.
