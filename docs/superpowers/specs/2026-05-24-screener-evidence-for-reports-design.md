# Screener Evidence for `/invest/reports` — `candidate_universe` Contract Redesign

- **Linear**: ROB-304 (re-scoped from "browser-backed crypto screener enrichment" to "make existing screener evidence actually reach reports")
- **Date**: 2026-05-24
- **Status**: Design approved, pending spec review

## Context & problem

ROB-304 originally proposed browser/CDP scraping of Toss/TradingView/Naver/Upbit to enrich `/invest/screener` and feed `/invest/reports`. Investigation showed the real deficiency is **not data collection** — it is that the rich evidence `/invest/screener` already computes never reaches `/invest/reports`.

Concrete findings (code-grounded):

- `/invest/screener` already builds, per row: candidate context (`scoreLabel`, **Korean reasons**, `source`), source provenance (`tvscreener_upbit / upbit_official / coingecko_reference / snapshot_cache`), Korean risk labels, and ROB-277 served-time vs data-as-of freshness. (`app/services/invest_view_model/screener_service.py`, `app/schemas/invest_screener.py`)
- The report `candidate_universe` collector reduces all of that to **counts only** (`fresh_count / actionable_count / stale_count / usefulness / no_data_reason`). It never carries the actual candidate rows. (`app/services/action_report/snapshot_backed/collectors/candidate_universe.py`)
- The `CandidateUniverseStage` reads `payload_json.get("candidates", [])`, but **nothing in production writes a `candidates` key** → it is always `[]` → always `NEUTRAL`, confidence 20, summary `"no candidates returned by screener"`. The `score >= 7.0 → BULL` branch is dead code in production. (`app/services/investment_stages/stages/candidate_universe.py:24`)
- `EvidenceAutoEmitter` uses `candidate_universe` only as a binary `usefulness == "useful"` gate; buy candidates actually come from `symbol_quotes`, so the screener never expands the candidate universe. (`app/services/action_report/snapshot_backed/auto_emit.py:151,198`)
- `no_data_reason` is an **English** raw string; confidence is hardcoded fixed bands.
- Crypto production snapshots are currently empty (ROB-282 backlog) — out of scope here, noted as a dependency.

Two consumers read `candidate_universe`, **both blind to real candidate data**:
1. `CandidateUniverseStage` → `StageRunner` → Hermes context (`app/services/investment_stages/hermes_context.py`). Deterministic evidence feeding Hermes composition (ROB-287).
2. `EvidenceAutoEmitter` → snapshot-backed generator when `auto_emit_from_evidence=True` (`app/services/action_report/snapshot_backed/generator.py:227`).

## Goal

Make `/invest/reports` consume the candidate evidence `/invest/screener` already produces — top movers/candidates with normalized scores, Korean reasons, source provenance, and freshness — plus held-symbol cross-check and freshness-driven confidence caps. Backward compatibility is explicitly **not required**: the `candidate_universe` payload contract is replaced outright (no transitional shim).

## Non-goals / safety boundaries

- **No in-process LLM.** All evidence/scoring/Korean copy is deterministic. Hermes still does composition (ROB-287 boundary; PR #898 static import guard must continue to pass).
- No broker/order/watch/order-intent mutation. Collector/stage/auto_emit stay read-only w.r.t. broker state; auto_emit keeps `operation="review"` + `requires_user_approval`.
- No browser/CDP scraping (deferred; the existing remote-debug stubs stay stubs).
- No new data source. We read existing `invest_screener_snapshots` / `invest_crypto_screener_snapshots` rows.
- No DB migration (see §6).
- No crypto snapshot refresh activation (ROB-282); crypto path is implemented but will be empty until ROB-282 ships.

## 1. Architecture — shared deterministic evidence builder

New neutral package `app/services/screener_evidence/`:

- `models.py` — `CandidateEvidence` (frozen dataclass / pydantic model).
- `builder.py` — `build_candidate_evidence(market, preset, rows) -> list[CandidateEvidence]`. **Pure function, no DB access.** Callers load rows; the builder only normalizes.
- `scoring.py` — preset-specific deterministic 0–10 scoring.

Single source of truth, consumed by three callers:
- `screener_service.build_screener_results()` — maps `CandidateEvidence → ScreenerCandidateContext` for the view-model.
- `collectors/candidate_universe.py` — serializes top-N into the report payload.
- `stages/candidate_universe.py` — consumes the payload (does not re-run the builder; reads the serialized evidence).

Rationale: builder is pure → fixture-testable, reusable, keeps the report decoupled from view-model display formatting, and honors the "snapshots are reusable evidence" principle.

## 2. Data contract — `CandidateEvidence`

```python
symbol: str            # DB symbol (KRW-BTC for crypto, ticker for equity)
market: str            # "kr" | "us" | "crypto"
name: str
score: float           # normalized 0–10 (so stage score>=7.0 branch is meaningful)
score_label: str       # Korean display, e.g. "RSI 28.3", "거래대금 12,345백만"
change_rate: float | None
price: float | None
volume_value: float | None     # turnover / 24h trade amount
reasons: list[str]     # Korean reason strings (reuse screener logic)
source: str            # provenance: tvscreener_upbit / kis / yahoo / upbit_official ...
risk_flags: list[str]  # Korean risk labels, e.g. "Upbit 유의 종목"
```

### Scoring (deterministic, preset-specific)

- `crypto_momentum` / equity momentum: `change_rate` mapped to 0–10.
- `crypto_oversold`: inverse RSI (lower RSI → higher score).
- `crypto_high_volume`: turnover rank within the batch → 0–10.
- equity `consecutive_gainers`: `consecutive_up_days` + `change_rate` blend.

Exact mapping curves are locked in the implementation plan; the contract only requires a stable, monotonic, documented 0–10 score per preset.

## 3. Collector payload (replaced outright)

`CandidateUniverseSnapshotCollector` payload becomes:

```python
{
  "market": "...",
  "preset": "...",                # which screen produced these
  "as_of": "...",                 # data-as-of
  "freshness_status": "fresh|partial|stale|missing",
  "source_coverage": {"tvscreener_upbit": 180, ...},   # provenance counts (G3)
  "candidates": [<CandidateEvidence dict>, ...],        # TOP-N (default 10) (G1)
  "fresh_count": int, "stale_count": int,
  "usefulness": "useful|stale_only|empty",
  "missing_data": {<structured Korean>} | None,         # G4
}
```

`TOP_N` is a module constant (default 10). `payload_json` is JSONB → no schema migration.

## 4. Consumer updates (both, no shim)

### `CandidateUniverseStage` (Hermes context path)
- Read the populated `candidates`; build `key_points` / `buy_evidence` from real symbols + Korean reasons + scores.
- Revive `score >= 7.0 → BULL` using normalized scores.
- Emit confidence per §5 and structured Korean `missing_data` when degraded.

### `EvidenceAutoEmitter`
- Use the `candidates` list to **expand/cite** the buy candidate universe (currently buy candidates only come from `symbol_quotes`).
- Keep existing guards: held excluded from buy, fail-closed when `usefulness != "useful"`, `operation="review"`, `requires_user_approval`.

## 5. Confidence model (G5)

Replace the hardcoded bands (20/35/40–75) with:

```
base = f(top_score)                 # top_score>=7 → up to 75; else lower band
cap by freshness_status:  fresh → no cap | partial → 60 | stale → 40 | missing → 20
single-source coverage → small additional cap
```

Deterministic and documented. `missing` always pairs with a populated `missing_data`.

## 5b. Structured Korean missing-data (G4)

Replace English `no_data_reason` with:

```python
missing_data = {
  "what": "무엇이 비어 있는지 (예: 암호화폐 스크리너 스냅샷이 비어 있음)",
  "why":  "그게 판단에 왜 중요한지 (후보 유니버스/모멘텀 교차검증 불가)",
  "next": "다음 리포트를 개선할 데이터 (예: crypto 스냅샷 리프레시 ROB-282)",
  "confidence_impact": "cap 20",
}
```

## 6. Held-symbol cross-check (G2 — PR2)

- Builder stays held-agnostic (pure).
- Cross-check happens where portfolio data is available:
  - **Stage**: `StageContext.snapshots_for("portfolio")` ∩ `candidates` → separate "보유+추세 동시" from "신규 후보" in key_points.
  - **auto_emit**: reuse its existing `held` set to annotate held overlap.

## 7. No DB migration

Both `candidate_universe` (`payload_json`) and stage artifacts persist JSON. The crypto/equity snapshot tables are read-only here and unchanged. No alembic revision.

## 8. Testing strategy (TDD)

- `screener_evidence/builder` + `scoring`: fixture rows for kr, us, and the three crypto presets → expected `CandidateEvidence` (scores monotonic, Korean reasons, source). Edge: missing fields, empty rows.
- Collector: top-N selection, `source_coverage`, `missing_data` on empty/stale, freshness propagation; still fail-open on exception.
- Stage: populated candidates → BULL/confidence; freshness caps; held overlap separation; Korean `missing_data` on empty.
- auto_emit: candidate-driven buy universe expansion + held exclusion preserved + fail-closed retained.
- Regression: existing KR/US screener view-model tests pass, honoring ROB-288 injected-`now` determinism. Static import guard (PR #898) still blocks in-process LLM.

## 9. PR staging

- **PR1 — §1–§5b (G1 + G3 + G4 + G5):** shared evidence builder, collector payload replacement, both consumers, provenance, confidence model, structured Korean missing-data. The contract is cut once.
- **PR2 — §6 (G2):** held-symbol cross-check in stage + auto_emit.

## 10. Assumptions to verify during implementation

- `StageArtifactPayload` (`app/schemas/investment_stages.py`) can carry a structured `missing_data` field (additive) and a normalized confidence; if not, add the field (no backward-compat constraint).
- Switching `screener_service` to the shared builder does not change `ScreenerResultsResponse` shape in a way that breaks frontend expectations beyond intended enrichment.
- `crypto` builder path is exercised by fixtures even though production crypto snapshots are empty (ROB-282).
