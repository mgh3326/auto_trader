# ROB-397 — snapshot 기반 `get_symbol_analysis` contract (foundation)

- **이슈**: ROB-397 (오케스트레이션 ROB-411 C라인의 foundation contract)
- **상태**: design (브레인스토밍 승인 완료)
- **날짜**: 2026-06-01
- **선행/관련**: ROB-396(비결정성·stale 증상), ROB-392(스테이지 미활용·스코프), ROB-287(스냅샷 번들), ROB-340(데이터소스 contract 선례), ROB-301(decision_bucket 5-value lock), ROB-323(core-aware freshness)

---

## 1. 문제와 목표

`analyze_stock_batch`(MCP)는 호출마다 KIS/research 파이프라인을 **라이브 합성**한다. 그 결과 세 가지 결함이 ROB-396에서 실증됐다.

1. **source·판정 flip (증상 1)** — `analysis_analyze.py:559-576`에서 `RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED` 게이트 + `try/except` 폴백 때문에, 같은 날 같은 종목이 KIS 적중 여부에 따라 `source:"kis"`(풍부, buy)와 `source:"research_pipeline"`(빈약, sell)로 **조용히 뒤집힌다**.
2. **stale current_price (증상 2)** — KR 경로(`market_data_quotes.py:417-438`)는 `inquire_daily_itemchartprice(n=1)`의 마지막 **일봉 종가**만 반환해, 정규장 중에도 전일종가를 준다.
3. **리포트 정합 (ROB-392)** — invest_report 합성이 이 도구에 의존해 운영자 직접분석과 어긋난다.

**목표**: "계산 시점 ≠ 조회 시점"을 분리하는 **읽기 전용 read-model 계약**을 고정한다. collector가 미리 머티리얼라이즈한 스냅샷을 결정적으로 읽어 반환하는 `get_symbol_analysis`로 전환하고, 각 필드가 자신의 출처·신선도를 데이터로 들고 다니게 만든다.

## 2. 범위 (deliverable boundary)

ROB-340 선례(PR #991)를 따른 **contract foundation**:

- ✅ 설계 문서(본 문서)
- ✅ 타입드 스키마 (`@dataclass(frozen=True)` 계층) + 권위/freshness 레지스트리 상수
- ✅ `derived` 순수 함수(기존 규칙 재사용) + 결정성/회귀 테스트
- ❌ collector / 실제 머티리얼라이즈 / DB 마이그레이션 — **후속 이슈(ROB-398 등)**
- ❌ `get_symbol_analysis` 런타임 구현 — **계약(시그니처+docstring+타입)만** 고정, stub도 만들지 않음

산출물 기준은 *ROB-396 fix가 즉시 인용할 수 있는 실행 가능한 계약*이다.

**안전 경계** (ROB-411 상속): broker/order/watch/order-intent mutation 금지, production DB backfill/commit ingest 금지, scheduler activation 금지, Toss/Naver/CDP 신호는 reference/calibration이며 KIS authority를 대체하지 않음, stale/부재는 숨기지 않고 노출.

## 3. 타입드 스키마

신규 모듈 `app/services/symbol_analysis/contract.py`. `invest_data_source_contract.py`의 frozen-dataclass 패턴을 따른다.

```python
@dataclass(frozen=True)
class FieldBlock(Generic[T]):
    value: T | None          # None = 부재
    source: str              # 권위 레지스트리의 source_name
    as_of: datetime | None   # 이 값이 관측/계산된 시점
    is_stale: bool           # freshness 규칙(§5)의 결과

@dataclass(frozen=True)
class SymbolAnalysis:
    symbol: str
    name: str | None
    market: str                      # "kr" | "us" | "crypto"
    price: FieldBlock[PriceData]     # last
    valuation: FieldBlock[ValuationData]   # per, pbr, roe
    technicals: FieldBlock[TechnicalData]  # rsi14, atr, sma, bb_lower, supports[], resistances[]
    consensus: FieldBlock[ConsensusData]   # buy/hold/sell, target_avg/median/min/max, upside_pct
    flow: FieldBlock[FlowData]             # foreign_net, inst_net, double_buy/sell, consec_days
    derived: DerivedBlock                  # §6
    provenance: Provenance                 # §4/§5
```

핵심 불변식: **모든 데이터 카테고리는 `FieldBlock`로 감싸 `source`/`as_of`/`is_stale`를 카테고리 단위로 강제**한다. 값과 provenance를 타입으로 분리 불가능하게 묶어, "값은 있는데 출처/신선도를 모르는" 상태를 컴파일/생성 단계에서 봉쇄한다.

```python
@dataclass(frozen=True)
class Provenance:
    snapshot_uuid: UUID | None       # 머티리얼라이즈 스냅샷(없으면 None + is_stale 전파)
    primary_source: str
    freshness: Freshness             # §5

@dataclass(frozen=True)
class Freshness:
    overall: str                     # "fresh" | "partial" | "stale" | "unavailable"
    stale_fields: tuple[str, ...]    # stale 카테고리 이름들
```

## 4. 필드별 권위 (authority) 레지스트리

카테고리별 고정 primary source를 contract 상수로 선언하고 기존 `invest_data_source_contract`의 `stocks/kis_live(collector_snapshot_kind="symbol")` 엔트리와 정합시킨다.

| 카테고리 | primary | fallback (강등) | 부재 시 |
|---|---|---|---|
| price | `kis_live` (당일 체결) | `stock_info` 전일종가 → **`is_stale=true` 강제** | `value=None`, label `확인 불가` |
| valuation | `stock_info` | `naver_finance` (reference) | `value=None` |
| technicals | 파생(일봉 OHLCV, `kis_live`) | — | `value=None` |
| consensus | `kis_live` (opinions) | `naver_finance` (reference) | `value=None` |
| flow | `investor_flow_snapshots` | `naver_finance` (reference) | `value=None` |

**불변 규칙**:

- fallback 값으로 치환할 때는 **반드시 `source`가 바뀌고 `is_stale=true`** 가 동반된다. → 같은 종목이 호출 타이밍에 따라 다른 source로 *조용히* flip되는 ROB-396 증상 1을 타입+테스트로 봉쇄한다.
- Toss/Naver/browser 신호는 `reference` / `low_trust_attention` 등급으로만 등재한다. **authority 대체 금지** (ROB-411 안전경계 및 프로젝트 원칙).
- 권위 레지스트리는 `invest_data_source_contract`와 drift-guard 테스트로 정합을 강제한다(§7).

## 5. freshness — core-aware

ROB-323에서 freshness `overall`을 worst-across-all로 깔면 보조 stub(toss/naver)이 전체를 stale로 오염시킨 anti-pattern이 확인됐다. 따라서 **core-aware** 파생을 채택한다.

- `overall ∈ {fresh, partial, stale, unavailable}`.
- **core 필드 = {price, consensus, technicals}** 만 `overall`을 결정한다. flow/valuation/theme(보조)는 stale이어도 `overall`을 다운그레이드하지 않는다(단, `stale_fields[]`에는 나열).
- 파생 규칙 (위에서부터 첫 매치, 결정적):
  1. `price.value is None` (사용 가능한 가격 앵커 없음) → `overall="unavailable"`.
  2. 그 외, core 필드 중 하나라도 `value=None` 또는 `is_stale=true` → `overall="stale"`.
  3. 그 외, 보조 필드(flow/valuation)만 stale/None → `overall="partial"`.
  4. 그 외 전부 fresh → `overall="fresh"`.
- 카테고리별 TTL/stale 규칙(상수로 명시, 구현 시 코드에 맞춰 정밀화):
  - **price**: 정규장 세션에서 `as_of`가 당일 체결이 아니면(=전일종가/일봉 출처) `is_stale=true`. → ROB-396 증상 2의 정면 회귀 가드.
  - **consensus / valuation**: `as_of`의 trading_date가 당일이 아니면 stale.
  - **technicals**: 최신 일봉 기준 당일이 아니면 stale.
- `stale_fields[]`는 machine-readable metadata와 Korean-facing copy 양쪽에 그대로 노출된다(숨김 금지).

## 6. `derived` — 결정성 + insufficient-data floor

```python
@dataclass(frozen=True)
class DerivedBlock:
    action: str                      # VERDICTS 재사용 (아래)
    confidence: str                  # "low" | "medium" | "high"
    buy_zones: tuple[PriceZone, ...]
    sell_targets: tuple[PriceLevel, ...]
    stop: Decimal | None
    rule_version: str                # 재현/감사용
    insufficient_inputs: tuple[str, ...]   # floor 사유(있으면)
```

- `derived = f(stored_inputs, rule_version)` — **순수 함수**: 라이브 호출/랜덤 없음, 입력 동일 → 출력 동일, 모든 리스트는 안정 정렬.
- `action`은 **새 enum 금지**. 기존 `VERDICTS = ("buy", "sell", "hold", "risk", "unavailable")` (`app/models/investment_symbol_intermediate_reports.py:57`)를 재사용한다. `decision_bucket`(5-value, ROB-301 lock) 매핑은 리포트 레이어 소관이므로 **본 계약 범위 밖**.
- **fail-closed floor** (ROB-396 증상 1 봉쇄):
  - price 자체 부재 → `action="unavailable"`, `confidence="low"`, `insufficient_inputs=("price",)`.
  - price는 있으나 consensus/technicals가 stale/null → 확신적 buy/sell 금지, `action="hold"`, confidence cap, `insufficient_inputs` 명시.
- 임계값 로직은 새로 발명하지 않고 기존 `build_recommendation_for_equity`(`app/mcp_server/tooling/shared.py:504`)를 **순수화해 재사용**한다(이미 `quote`/`price` 부재 시 `None`/`hold,low`로 떨어지는 부분적 floor 존재 — 이를 명시적 계약으로 승격).

## 7. 읽기 도구 계약 (구현은 후속)

```python
async def get_symbol_analysis(
    symbols: list[str], session: str | None = None
) -> list[SymbolAnalysis]:
    """캐시/DB의 최신 머티리얼라이즈 read-model을 반환. 없으면
    마지막 스냅샷 + is_stale=true. **라이브 합성 금지.**"""
```

- ROB-397에서는 **시그니처 + docstring 계약 + 타입만** 고정한다(런타임 DB 읽기/collector 없음 → stub도 만들지 않음).
- 머티리얼라이즈 seam: 기존 `snapshot_kind="symbol"`(이미 `investment_snapshots.py:130-134` CHECK 제약 + `invest_data_source_contract.py:246`에 존재) 위에 후속 collector(`refresh_symbol_analysis`)가 올라가도록 문서에 명시. **마이그레이션 0**.

## 8. 테스트 (ROB-340식)

1. **권위 레지스트리 완전성/일관성**: 모든 카테고리에 primary 존재; Toss/Naver는 authority 등급 아님; `invest_data_source_contract`와 drift-guard.
2. **freshness core-aware**: 보조 필드(flow/valuation) stale이어도 `overall`이 떨어지지 않음 / core 필드 stale이면 떨어짐.
3. **stale price 회귀 (증상 2)**: 전일종가/일봉 출처 price → 정규장 세션에서 `is_stale=true`.
4. **derived 결정성 (증상 1)**: 동일 입력 2회 호출 = 바이트 동일 출력; consensus=null이면 buy/sell이 나오지 않고 `hold`/`unavailable` floor + `insufficient_inputs`.
5. **action enum 재사용**: `derived.action` 값이 항상 `VERDICTS` 안에 있음.

## 9. 비목표 (YAGNI)

- collector / 스케줄러 / DB 테이블 / 마이그레이션 (후속).
- US/crypto 권위 세부(본 계약은 market 필드로 확장 가능한 형태만; KR 우선).
- 리포트 레이어의 `decision_bucket` 매핑(ROB-301 소관).
