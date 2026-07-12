# ROB-838 — 재점검 분석 스냅샷 번들 고정 입력

- Date: 2026-07-12
- Linear: ROB-838
- Base: `main`
- Scope: server-side capture/read path only; comparison harness and ROB-833 runner wiring are follow-ups

## Problem

여러 모델이 같은 프롬프트를 받아도 각 세션이 라이브 도구를 다시 호출하면 수집 시각과 provider 성공 여부가 달라진다. 특히 crypto breadth처럼 분 단위로 변하는 값과 provider-off 실패가 모델 차이처럼 보인다. 재점검의 순수한 모델 차이를 비교하려면 모든 모델이 하나의 사전 수집 결과를 그대로 소비해야 한다.

## Existing infrastructure audit

ROB-287의 `investment_report_prepare_bundle` / `SnapshotBundleEnsureService`는 다음 기반을 이미 제공한다.

- `review.investment_snapshots`와 bundle/item 테이블의 append-only repository
- snapshot별 canonical JSON SHA-256 (`canonical_payload_hash`)
- 수집 `as_of`, `collected_at`, `source_kind`, source timestamps, freshness, coverage, error 저장
- production collector registry를 통한 read-only portfolio, market, symbol, journal, watch context, investor flow 수집
- `portfolio` payload의 holdings/cash, `symbol` payload의 quote/orderbook, `market` payload의 index/crypto breadth
- default-off MCP registration과 persisted bundle read 패턴

그러나 Hermes exporter는 재점검 입력 전체를 보존하지 않는다. 지표와 지지·저항은 `analyze_stock` 파이프라인에서 조회 시 계산되고, decision history와 recovery gate 원재료도 Hermes context의 고정 계약이 아니다. 기존 여러 snapshot kind를 그대로 노출하면 읽는 쪽이 재조합/재계산해야 하고 계약 버전에 따라 같은 bundle ID의 표현이 달라질 수 있다.

## Considered approaches

1. 기존 Hermes context 확장: 중복이 적지만 조회 시 stage/exporter가 파생값을 다시 구성하므로 “저장본 그대로” 원칙을 깨기 쉽다.
2. 재점검용 snapshot kind를 여러 개 추가: 표면별 provenance는 깔끔하지만 schema CHECK/migration과 조합 manifest가 커지고, bundle item 후첨가가 출력 집합을 바꿀 수 있다.
3. 기존 `llm_input_frozen` snapshot kind에 versioned analysis document 한 건 저장: 기존 append-only/hash 기반을 그대로 쓰고, 전체 입력 문서의 단일 content hash를 검증할 수 있다.

3번을 채택한다. `llm_input_frozen`은 이미 schema/model CHECK에 존재하므로 migration 없이 목적에 맞게 사용할 수 있다.

## Architecture

### 1. Frozen document

`AnalysisBundleCaptureService`는 한 번의 capture에서 아래 문서를 만든다.

```json
{
  "schema_version": "analysis-snapshot-bundle.v1",
  "captured_at": "...",
  "request": {
    "market": "crypto",
    "account_scope": "upbit_live",
    "symbols": ["KRW-BTC"]
  },
  "sections": {
    "portfolio": {"status": "ok", "as_of": "...", "source": {...}, "data": {...}},
    "quotes_orderbooks": {"status": "ok", "as_of": "...", "source": {...}, "data": {...}},
    "indicators_support_resistance": {"status": "ok", "as_of": "...", "source": {...}, "data": {...}},
    "market_gate_inputs": {"status": "ok", "as_of": "...", "source": {...}, "data": {...}},
    "investor_flow": {"status": "unavailable", "as_of": "...", "source": {...}, "error": "provider off"},
    "decision_history": {"status": "ok", "as_of": "...", "source": {...}, "data": {...}}
  }
}
```

Section names are the stable public contract. Holdings and cash remain together in `portfolio` because the existing collector already obtains them as one account-consistent read. Quote and orderbook remain together because the venue adapter returns them as one observation. Indicators and support/resistance use the existing analysis pipeline result without reimplementing formulas. `market_gate_inputs` stores raw gate inputs, including breadth and source diagnostics; it does not store an LLM judgment.

Every section is present. A failed or disabled provider is stored as `status="unavailable"` with the original exception text in `error`; it is not omitted and does not fail the whole capture. No section is retried by the read path.

### 2. Capture and persistence

The capture service receives explicit market/account/symbol scope. It calls only existing read services/collectors and analysis reads; it cannot import or call order, proposal, watch, or report mutation services. Broker reads are injected and mocked in unit tests.

Capture converts each result into a section envelope containing:

- `status`: `ok`, `partial`, or `unavailable`
- `collected_at`: when this surface collection completed
- `as_of`: provider/domain observation time when available, otherwise the collection time with an explicit provenance note
- `source`: provider/service identifier and any upstream timestamp metadata
- exact `data`, or exact `error` on failure

The completed document is stored once as an `InvestmentSnapshot` with `snapshot_kind="llm_input_frozen"`, `source_kind="combined"`, and purpose `analysis_recheck`. `SnapshotBundleEnsureService` is extended with an explicit create-new mode so an event capture never silently reuses a previous bundle. The existing repository computes `canonical_payload_hash` over the whole document. That full 64-character SHA-256 is exposed as `content_hash`.

Correction means capture again and receive a new bundle UUID. There is no update method. `create_new` assigns a per-capture idempotency discriminator, so even byte-identical captures receive distinct immutable snapshot rows and bundle identities; their canonical document hashes may still match because the discriminator is not part of the stored payload.

### 3. Read path

New MCP tools, behind a new default-off env gate, are:

- `analysis_bundle_create(market, account_scope, symbols, user_id?, market_session?)`
- `analysis_bundle_get(bundle_id, sections?)`

`analysis_bundle_get` performs DB reads only. It does not instantiate collectors, provider clients, analysis functions, or exporters. It resolves exactly one linked `llm_input_frozen` snapshot, recomputes canonical SHA-256 from the persisted JSON, and compares it with `canonical_payload_hash`. Mismatch, wrong purpose/kind, or multiple frozen snapshots returns a structured integrity error and no payload.

`sections=[...]` only projects named keys from the persisted `sections` object. It never transforms section values. Unknown section names return a validation error instead of silently dropping them. The top-level request, schema version, capture metadata, integrity metadata, and completeness summary remain in every response.

### 4. Freshness and completeness

Freshness is response metadata, never a rewritten payload. The read response includes:

- bundle `created_at`, document `captured_at`, and current `read_at`
- bundle age in seconds
- for every returned section: persisted `as_of`, computed `age_seconds`, persisted status and source
- bundle status and a completeness summary listing unavailable/partial sections
- `stale_warning` when bundle or any section exceeds the frozen policy TTL

Age is computed at read time from persisted timestamps. The underlying section payload is unchanged. Staleness never triggers refresh or fallback.

### 5. Gate and safety boundary

Use `ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED: bool = False`. When false, both tools are absent from MCP registration. The tools are classified read-only/advisory for routing; creation is a DB append of evidence only, not a trading mutation. The default profile may expose create and get when enabled, while the `analysis_readonly` consumer profile exposes get only so model sessions cannot create a competing input.

## Error handling

- Individual source failure: persist an unavailable section with original error; capture succeeds with bundle status `partial`.
- Invalid scope or no symbols: request validation error before collection.
- Database failure: capture fails and transaction rolls back; no half-linked bundle is returned.
- Missing bundle: structured `analysis_bundle_not_found`.
- Hash mismatch or malformed frozen document: structured `analysis_bundle_integrity_error`, fail-closed.
- Unknown section filter: structured `unknown_analysis_bundle_section`.

## Tests (TDD)

1. Capture service tests first: all surfaces captured; broker/provider calls mocked; failure text persisted; every section carries source/as-of/collected-at; no mutation imports/calls.
2. Persistence/integrity tests: canonical hash stamps the exact full document; changed payload changes hash; repository exposes no update/delete; correction creates a new bundle; tampered payload fails read.
3. Read service tests: DB-only read; exact raw payload equality; section filtering is projection-only; unknown sections fail; age/stale metadata computed without modifying payload; partial sections remain unavailable.
4. MCP tests: default-off tools absent; gate-on registration; create delegates to capture service; get delegates to read service; structured errors and docs.
5. Regression tests: existing ROB-287/Hermes bundle tools and policies remain unchanged.

## ROB-833 reuse contract

This PR supplies the runner input seam but does not implement the runner. The intended ROB-833 sequence is:

`watch/fill event → analysis_bundle_create once → same bundle_id + prompt/recipe/version passed to each claude -p session → analysis_bundle_get only → mechanical model diff`

The PR description must state this explicitly. Models must not receive live market/provider tools in that frozen-input phase.

## Non-goals

- N-model comparison harness or `claude -p` process management
- ROB-833 watch/fill daemon wiring
- policy judgment or proposal/order mutation
- retroactive repair of an existing bundle
- replacing general investment report bundles or Hermes composition
