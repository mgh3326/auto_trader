# ROB-690 — 리포트 아이템 손익비(R:R) 계산 구현 플랜

`entry / stop / target` 3종 가격에서 `risk%` / `reward%` / `rr_ratio` 를 **결정론적 산술**로
계산해 리포트 아이템에 부착한다. 새 예측 모델이 아니라 **이미 있는 숫자의 비율 계산**이며,
값(어떤 entry/stop/target을 쓸지) 선택 판단은 Hermes/MCP 소비자 몫으로 유지한다.

> 소스 수정 없음 — 이 문서는 구현 플랜이다. 실제 코드 변경은 후속 구현 세션에서 수행.

---

## 1) 목표

- 리포트 아이템에 이미 존재하는 `entry_plan[]` / `stop_loss` / `target_price` 세 가격에서
  `risk_pct`, `reward_pct`, `rr_ratio` 를 순수 산술로 계산한다.
- **포지션 방향 인지**: 롱(현물 기본) vs 명시적 숏. 부호 규약을 방향으로 결정하고,
  방향과 가격 삼각이 불일치하면 **fail-closed (미산출)** 하여 오해성 카드를 방지한다.
- 순수 청산(롱 sell-exit)에는 R:R 을 강요하지 않는다 — 실현손익 프레임이 맞으며 ROB-691 이 담당.
- 영속화는 **migration-0**: 계산 결과를 `evidence_snapshot["trade_setup"]` 에 부착
  (기존 ROB-459 `entry_plan`/`stop_loss`/`target_price` 병합 패턴과 동일).
- 순수 헬퍼로 분리하여 read-time 소비자(예: `build_recommendation_for_equity`)도 재사용.
- ROB-501 in-process LLM 가드 준수 — 순수 산술, LLM/DB/네트워크 import 없음.

---

## 2) 검증된 현재 상태 (실제 file:line, 참조 교정 포함)

모든 참조를 worktree `/Users/mgh3326/work/auto_trader.rob-690` 소스에서 직접 확인했다.

### 2.1 스키마 — `app/schemas/investment_reports.py`

- `ReportItemPriceLevelPayload` — **L250–265** (참조 정확). 필드: `label`, `price: Decimal`(필수),
  `quantity`, `notional`, `currency`, `condition`, `rationale`. `model_config = ConfigDict(extra="forbid")`.
- `IngestReportItem` — **L300–434**. 관련 필드:
  - `item_kind: ItemKindLiteral` (**L312**) — `action|watch|risk`
  - `side: ItemSideLiteral | None` (**L315**) — `buy|sell|None`
  - `intent: ItemIntentLiteral` (**L316**) — `buy_review|sell_review|risk_review|trend_recovery_review|rebalance_review`
  - `entry_plan: list[ReportItemPriceLevelPayload]` (**L326**), `stop_loss: … | None` (**L327**),
    `target_price: … | None` (**L328**) — **참조 정확**.
  - `evidence_snapshot: dict[str, Any]` (**L321**), `metadata: dict[str, Any]` (**L334**).
  - `model_config = ConfigDict(extra="forbid")` (**L309**) → **새 입력 필드는 반드시 모델에 선언해야** 통과.
  - 예약키 충돌 가드 `_validate_reserved_evidence_snapshot_keys` (**L360–380**) — 타입드 필드와
    `evidence_snapshot` 의 동일 키 동시 지정을 거부. 현재 검사 대상:
    `structured_evidence / item_freshness / entry_plan / stop_loss / target_price / linked_order_ids`.
- 응답: `InvestmentReportItemResponse.evidence_snapshot: dict[str, Any]` (**L788**) → 예약키 포함
  `evidence_snapshot` 전체를 그대로 round-trip. **응답 스키마 변경 불필요**.

### 2.2 영속화 — `app/services/investment_reports/ingestion.py`

- `_insert_item` (**L478–559**) 가 단일 choke point. `ingest`/`add_items_to_draft` 모두 여기로 수렴.
- **L498–526**: ROB-459 패턴 — 타입드 `entry_plan`/`stop_loss`/`target_price`/`linked_order_ids` 를
  `evidence_payload[...]` 예약키로 병합(migration-0). 미지정 시 키를 추가하지 않아 legacy shape 보존.
  - `entry_plan` → `level.model_dump(mode="json", exclude_none=True)` (**L510–512**)
  - `stop_loss`/`target_price` → `model_dump(mode="json", exclude_none=True)` (**L514–521**)
  - `Decimal` 은 `mode="json"` 으로 문자열 직렬화(예: `"70000"`) — 재읽기 시 문자열임에 유의.
- 계산된 `trade_setup` 은 여기서 같은 방식으로 부착한다.

### 2.3 리포트 create 문서 — `app/mcp_server/tooling/investment_reports_handlers.py`

- `CREATE_DESCRIPTION` **L127–173**. 트레이드 플랜 필드(`entry_plan`/`stop_loss`/`target_price`) 설명은
  **L162–166** (참조 "~162-164" **교정**: 실제 162–166).
- `ADD_ITEMS_DESCRIPTION` (**L176–185**) 도 동일 계약 재사용 → 문서 업데이트 시 함께 반영.

### 2.4 `build_recommendation_for_equity` — `app/mcp_server/tooling/shared.py`

- 정의 **L524–808** (참조 정확). `buy_zones`(진입 후보), `sell_targets`(익절 후보),
  `stop_loss`(float) 를 산출.
- 소비처: `app/mcp_server/tooling/analysis_analyze.py::_apply_recommendation` **L683–723**
  (`analyze_stock` 출력에 `recommendation` 부착). 리포트 아이템과 **별개 표면**이며,
  R:R 부착은 여기서 **부차 배선(secondary)** 으로 다룬다.

### 2.5 프런트 — `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx`

- `ItemRow` **L206–350** (참조 정확). **현재 `entry_plan`/`stop_loss`/`target_price` 를 렌더하지 않는다**
  → R:R 표시는 순수 신규 UI(기존 가격표시 확장 아님). `rationale`/`linkedOrders`/`watchCondition` 등만 표시.
- TS 타입 `frontend/invest/src/types/investmentReports.ts` — `InvestmentReportItem.evidenceSnapshot:
  Record<string, unknown>` (**L256, L395**). API 매핑 `api/investmentReports.ts` L186/L258 에서
  `evidenceSnapshot: asRecord(raw.evidence_snapshot)` → **evidenceSnapshot 로 이미 노출됨**.
  R:R 은 `evidenceSnapshot.trade_setup` 로 읽기 가능(추가 배선 없이).

### 2.6 참조 교정 요약

| 이슈 참조 | 실제 | 판정 |
|---|---|---|
| `ReportItemPriceLevelPayload` ~250-265 | L250–265 | 정확 |
| `entry_plan/stop_loss/target_price` ~326-328 | L326–328 | 정확 |
| create 문서 ~162-164 | **L162–166** | 소폭 교정 |
| `build_recommendation_for_equity` ~524-808 | L524–808 | 정확 |
| `build_recommendation` 도 배선 대상 | analyze_stock 표면(리포트와 별개) | **부차 배선으로 강등** |
| `trigger_checklist` 를 additive list 선례로 | trigger_checklist 는 **자체 DB 컬럼**(insert_item L542), evidence_snapshot 병합 아님 | **선례 교정**: R:R 의 정확한 선례는 `entry_plan`/`stop_loss`/`target_price` 의 evidence_snapshot 병합(ROB-459), trigger_checklist 아님 |
| side/intent 를 읽을 위치 | `IngestReportItem` L312/315/316 (item_kind/side/intent) | 확인 |
| risk_reward\|reward_ratio\|risk_pct\|reward_pct\|rr_ratio\|손익비\|trade_setup\|position_direction grep | app/·frontend/·tests/ **0건** | 신규 확정 |
| ROB-501 LLM 가드 | `tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py`, `app/**/*.py` 스캔 | 신규 헬퍼는 stdlib+Decimal → 문제 없음 |

---

## 3) 설계 결정 (근거 포함)

### D1. 영속 위치 — migration-0, `evidence_snapshot["trade_setup"]` (신규 컬럼 아님)

**결정**: 계산 결과를 `evidence_snapshot["trade_setup"]` 예약키로 병합. **마이그레이션 0**.

**근거**:
- R:R 의 입력(`entry_plan`/`stop_loss`/`target_price`)이 이미 `evidence_snapshot` 에 migration-0 로
  산다(ROB-459). 그 파생값을 같은 자리에 두는 것이 일관적이고 round-trip 무료
  (`InvestmentReportItemResponse.evidence_snapshot` 그대로 노출, 응답/프런트 타입 변경 최소).
- 신규 typed 컬럼(`IngestReportItem.trade_setup` + 컬럼) 대안은 alembic 마이그레이션 + operator
  cutover 필요. **파생 산술값**에 컬럼을 추가하는 것은 과잉. CLAUDE.md 도 migration-0 선호.
- 트레이드오프: JSONB 하위라 SQL 필터/정렬 비용↑, 스키마 강제 약함. 하지만 R:R 은 표시/감사용
  파생값이라 인덱스/쿼리 대상이 아님 → JSONB 로 충분. (후속에서 대량 분석 쿼리 수요가 생기면
  그때 컬럼 승격 — 지금은 YAGNI.)

### D2. 계산 시점 — **ingestion 시 계산 + 영속** (write-time), 순수 헬퍼는 read-time 재사용 가능

**결정**: `_insert_item` 에서 write-time 에 계산해 `trade_setup` 로 저장. 동시에 pure 헬퍼를
분리 노출하여 read-time 소비자(build_recommendation 등)도 동일 산술 재사용.

**근거**:
- "리포트 아이템에 붙인다" = 아이템 생성 시점에 확정 부착(감사: 당시 어떤 R:R 이었나).
- 읽기 경로가 trivial(키만 read), 매 read 재계산 불필요.
- 결정론적 산술이라 write-time 값이 read-time 재계산과 항상 동일 → 캐시-불일치 위험 없음.
- 순수 헬퍼 분리로 ActionPacket 류 read-time projection 과도 호환(원하면 재계산 가능).

### D3. 방향 규약 (부호) — 롱 기본, 명시적 숏만 반전, 삼각 불일치 fail-closed

- **롱(long, 기본)**: `stop < entry < target`.
  - `risk_pct  = (entry − stop) / entry × 100`
  - `reward_pct = (target − entry) / entry × 100`
  - `rr_ratio  = (target − entry) / (entry − stop)`  (entry 정규화가 상쇄 → 거리비와 동일)
- **숏(short, 명시적)**: `target < entry < stop`.
  - `risk_pct  = (stop − entry) / entry × 100`
  - `reward_pct = (entry − target) / entry × 100`
  - `rr_ratio  = (entry − target) / (stop − entry)`
- **fail-closed**: 방향이 확정됐는데 가격 삼각이 규약과 불일치하면 **R:R 미산출**(카드 없음).
  퇴화(`entry == stop` → risk 0, 0 나눗셈)도 미산출.

**근거**: ChartPT SELL 카드(손절 진입 위 / 익절 진입 아래)는 숏 산술은 맞지만 auto_trader 의
롱/현물 기본에서 롱 청산 관점과 인버전 → 오해. 방향을 **독립적으로 결정**하고 삼각과 대조해
불일치면 침묵(미산출)해야 잘못된 R:R 카드가 안 뜬다.

### D4. exit 분기 — 순수 청산은 R:R 스킵(ROB-691 실현손익 프레임)

`resolve_direction()` 이 다음으로 분기:
- `explicit_direction == "short"` → **short** (삼각 `target<entry<stop` 요구, 아니면 fail-closed)
- `explicit_direction == "long"` → **long**
- `explicit_direction is None` 인 경우 side/intent 로 추론:
  - `side == "buy"` **또는** `side is None and intent in {buy_review, trend_recovery_review}` → **long**
  - `side == "sell"` **또는** `intent == "sell_review"` → **exit** → **R:R 미부착**(스킵)
  - 그 외(예: `rebalance_review` + side None, `risk_review`) → **unknown** → 스킵(오해 방지)

**근거**: R:R 은 buy(신규 진입)·보유관리 항목 우선. 롱 sell-exit 에 R:R 을 씌우면 실현손익과
개념 충돌. 숏은 auto_trader 에 방향 신호 필드가 없으므로 **명시적 opt-in 만** 허용.

### D5. 숏/방향 신호 — 신규 typed 입력 `position_direction`, 단 컬럼 없음

- `IngestReportItem` 에 `position_direction: Literal["long","short"] | None = None` 추가.
  (`extra="forbid"` 라 신규 입력은 반드시 선언 필요.)
- 이 필드는 **입력 전용**: ingestion 이 계산에 소비하고, 실제 사용된 방향은
  `trade_setup["direction"]` 로 echo. 따라서 **별도 DB 컬럼 불필요**(migration-0 유지).
- account_scope(`kis_live|kis_mock|alpaca_paper|upbit_live`)에는 Binance 선물 등 숏 표면이 없어
  방향을 유추할 수 없음 → 명시 필드가 유일한 안전 신호.

### D6. 다중 진입 레벨 처리 — per-leg 계산 + 대표 headline

`entry_plan` 은 다중 레벨 가능(테스트에 1차/2차 존재). stop/target 은 레벨 공유.
- `trade_setup.legs`: 각 entry 레벨별 `{entry, risk_pct, reward_pct, rr_ratio}`.
- `trade_setup.headline`: 대표 entry 기반 요약.
  - 레벨 1개 → 그 레벨.
  - 레벨 2개+ → **전 레벨에 quantity>0 가 있으면 수량가중 평균 entry**, 아니면 **entry 가격 단순 평균**.
  - 대표 entry 로 다시 순수 산술 → deterministic.

**근거**: 다중 진입은 실제 케이스. 레벨별이 정직한 표현이고, headline 은 카드 한 줄 표시용.
집계 규칙을 문서·테스트로 고정해 비결정성 배제.

### D7. 반올림 — 고정 소수 자릿수 quantize (결정론)

- `risk_pct`/`reward_pct` → `Decimal.quantize(Decimal("0.01"))` (2 dp, ROUND_HALF_UP).
- `rr_ratio` → `Decimal.quantize(Decimal("0.01"))` (2 dp).
- 저장은 `str(Decimal)` (evidence_snapshot 의 Decimal→문자열 관례와 정합).

---

## 4) 단계별 구현 (파일별)

### Step 1 — 순수 헬퍼 모듈 (신규) `app/services/investment_reports/risk_reward.py`

stdlib + `decimal` 만. LLM/DB/네트워크/브로커 import 금지(ROB-501 정합, `live_order_expiry.py` 선례).

```python
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Sequence

Direction = Literal["long", "short"]
# resolve_direction 결과: 계산 대상 방향 또는 스킵 사유
DirectionOrSkip = Literal["long", "short", "exit", "unknown"]
TradeSetupStatus = Literal["computed", "direction_price_mismatch", "degenerate_risk"]

_Q = Decimal("0.01")

@dataclass(frozen=True)
class RiskRewardLeg:
    entry: Decimal
    risk_pct: Decimal
    reward_pct: Decimal
    rr_ratio: Decimal

@dataclass(frozen=True)
class TradeSetup:
    status: TradeSetupStatus
    direction: Direction
    stop: Decimal
    target: Decimal
    legs: tuple[RiskRewardLeg, ...]        # status=="computed" 일 때만 non-empty
    headline: RiskRewardLeg | None         # status=="computed" 일 때만 set
    reason: str | None                     # fail-closed 사유(감사용)

def resolve_direction(
    *, side: str | None, intent: str, item_kind: str,
    explicit_direction: str | None,
) -> DirectionOrSkip:
    """부호 규약 방향 결정. D4 표 그대로. exit/unknown 은 R:R 스킵 신호."""

def compute_leg(
    *, entry: Decimal, stop: Decimal, target: Decimal, direction: Direction,
) -> RiskRewardLeg | None:
    """단일 entry/stop/target 삼각의 R:R. 방향-삼각 불일치나 퇴화(risk<=0)면
    None(fail-closed). 반환 pct 는 entry 기준 %, rr 은 거리비. 전부 2dp quantize."""

def _representative_entry(
    entry_levels: Sequence[Decimal], quantities: Sequence[Decimal | None],
) -> Decimal:
    """D6: 전 레벨 qty>0 면 수량가중 평균, 아니면 단순 평균."""

def build_trade_setup(
    *, entry_levels: Sequence[Decimal],
    quantities: Sequence[Decimal | None],
    stop: Decimal, target: Decimal, direction: Direction,
) -> TradeSetup:
    """per-leg 계산 + headline. 어느 leg 든 불일치면 status=direction_price_mismatch,
    퇴화면 degenerate_risk 로 fail-closed(legs/headline 비움)."""
```

- rr 은 거리비(`reward_distance / risk_distance`)로 계산 — entry 정규화 상쇄로 pct 나눗셈과 동일하되
  수치 안정적. `risk_distance <= 0` → `degenerate_risk`.
- 방향-삼각 검증: long 은 `stop < entry < target`, short 는 `target < entry < stop` 를
  **모든 leg + 공유 stop/target** 에 대해 요구.

### Step 2 — 스키마 `app/schemas/investment_reports.py`

1. `IngestReportItem` 에 필드 추가(L316 side/intent 부근, L328 이후 트레이드 플랜 블록 근처):
   ```python
   position_direction: Literal["long", "short"] | None = None
   ```
   (모듈 상단에 `TradeDirectionLiteral = Literal["long","short"]` 를 두고 재사용해도 됨.)
2. `_validate_reserved_evidence_snapshot_keys` (L360–380) 에 `trade_setup` 예약키 검사 추가:
   ```python
   if "trade_setup" in self.evidence_snapshot:
       conflicts.append("trade_setup")
   ```
   → 계산은 서버가 하므로 caller 가 `evidence_snapshot["trade_setup"]` 를 직접 넣으면 거부
   (신뢰 경계: R:R 은 서버 산술만이 소스).

### Step 3 — 영속화 배선 `app/services/investment_reports/ingestion.py::_insert_item`

`evidence_payload` 구성(L502–526) **직후**, `item_metadata` 조립(L527) **전** 에 삽입:

```python
from app.services.investment_reports.risk_reward import (
    resolve_direction, build_trade_setup,
)
# 입력 3종이 모두 있고 방향이 계산 대상일 때만.
if item_req.entry_plan and item_req.stop_loss is not None and item_req.target_price is not None:
    direction = resolve_direction(
        side=item_req.side, intent=item_req.intent, item_kind=item_req.item_kind,
        explicit_direction=item_req.position_direction,
    )
    if direction in ("long", "short"):
        setup = build_trade_setup(
            entry_levels=[lvl.price for lvl in item_req.entry_plan],
            quantities=[lvl.quantity for lvl in item_req.entry_plan],
            stop=item_req.stop_loss.price,
            target=item_req.target_price.price,
            direction=direction,
        )
        if setup.status == "computed":
            evidence_payload["trade_setup"] = _serialise_trade_setup(setup)
        # fail-closed(direction_price_mismatch / degenerate_risk) → 키 미추가(침묵).
```

- `_serialise_trade_setup(setup)` → JSON-safe dict(Decimal→str), 예:
  ```json
  {"direction":"long","stop":"65000","target":"78000",
   "headline":{"entry":"69000","risk_pct":"5.80","reward_pct":"13.04","rr_ratio":"2.25"},
   "legs":[{"entry":"70000","risk_pct":"7.14","reward_pct":"11.43","rr_ratio":"1.60"},
           {"entry":"68000","risk_pct":"4.41","reward_pct":"14.71","rr_ratio":"3.33"}]}
  ```
- **exit/unknown 또는 fail-closed → `trade_setup` 키 자체를 추가하지 않아** legacy shape 보존
  (`test_no_evidence_leaves_snapshot_keys_absent` 정신 유지).
- `add_items_to_draft` 도 `_insert_item` 경유라 자동 커버.

### Step 4 (부차) — `build_recommendation_for_equity` R:R 부착 `app/mcp_server/tooling/shared.py`

- 함수 말미(L801 `recommendation["stop_loss"] = stop_loss` 이후, L803 reasoning 조립 전) 에:
  entry = `buy_zones[0]["price"]`(있으면), stop = `stop_loss`, target = `sell_targets[0]["price"]`(있으면)
  로 `compute_leg(direction="long")` 호출. 성공 시 `recommendation["risk_reward"] =
  {"entry":..., "stop":..., "target":..., "risk_pct":..., "reward_pct":..., "rr_ratio":...}`.
  삼각 불일치/부재면 키 미추가.
- **순수 float→Decimal 변환만**; 실패해도 recommendation 본체 무영향(옵셔널).
- analyze_stock 표면(`_apply_recommendation`, analysis_analyze.py L683–723)은 이 dict 를 그대로
  실어 나르므로 추가 배선 불필요.

> Step 4 는 리포트 아이템 계약과 독립. 핵심 완료기준(리포트 아이템 R:R)은 Step 1–3 로 충족.
> 리뷰에서 스코프 축소 요구 시 Step 4 는 후속으로 분리 가능.

### Step 5 — 프런트 렌더 `InvestmentReportBundleContent.tsx` `ItemRow` (L206–350)

- `item.evidenceSnapshot.trade_setup` 읽어(있고 `status==="computed"` 일 때만) R:R 칩/라인 렌더.
  `rationale`(L273–275) 아래에 삽입.
- 표시 예: `손익비 R:R 2.3 · 리스크 5.8% · 리워드 13.0%` (headline 기준),
  방향 뱃지(`롱`/`숏`). `status!=="computed"` 또는 부재면 아무것도 렌더 안 함(오해성 카드 방지).
- 값 파싱은 문자열 Decimal → `Number()` 유한성 검사 후 표시(기존 `formatConfidence` L197–204 패턴).
- 타입: `evidenceSnapshot: Record<string, unknown>` 라 별도 타입 추가 불필요(옵션: 좁은
  `TradeSetupView` 타입을 `types/investmentReports.ts` 에 두고 런타임 가드로 좁혀도 됨).

---

## 5) 테스트 계획 (완료기준 커버)

### 5.1 순수 헬퍼 단위 — 신규 `tests/test_investment_report_risk_reward.py` (`@pytest.mark.unit`)

- **롱 정상**: `entry=70000, stop=65000, target=78000` → risk_pct≈7.14, reward_pct≈11.43, rr≈1.60.
- **숏 정상**(explicit): `entry=100, stop=110, target=85` → risk_pct=10, reward_pct=15, rr=1.50.
- **롱 삼각 불일치**: `stop=72000>entry` → `direction_price_mismatch`, legs/headline 비움.
- **숏 삼각 불일치**: `target>entry` → mismatch.
- **퇴화**: `entry==stop` → `degenerate_risk`.
- **다중 leg headline**: qty 있음 → 수량가중; qty 없음 → 단순평균. 값 고정 검증.
- **rr = 거리비 == pct비** 항등 확인(정규화 상쇄).
- **반올림**: 2dp quantize 경계값(ROUND_HALF_UP).
- **resolve_direction 표**: (side, intent, item_kind, explicit)×기대(long/short/exit/unknown) 매트릭스.
  - buy → long; sell → exit; sell_review → exit; explicit short → short; explicit long → long;
    None+buy_review → long; None+trend_recovery_review → long; None+rebalance_review → unknown;
    risk_review → unknown.

### 5.2 스키마 — `tests/test_investment_reports_schemas.py` 및/또는 `test_investment_report_item_evidence.py`

- `position_direction` 수용(`long`/`short`/미지정) 및 잘못된 값 거부.
- `evidence_snapshot={"trade_setup":{...}}` + (entry/stop/target) 동시 → `ValidationError`
  (예약키 충돌, "reserved evidence_snapshot keys" 문구). 기존 L203 테스트와 대칭.

### 5.3 영속 round-trip — `tests/test_investment_report_item_evidence.py` 확장(선례 L217–265)

- **롱 buy**: entry_plan(2레벨)+stop+target+side=buy → 저장 후
  `snap["trade_setup"]["headline"]["rr_ratio"]` 및 `legs` 존재, 값 검증.
- **exit 스킵**: side=sell, intent=sell_review, 동일 3종 가격 → `"trade_setup" not in snap`.
- **fail-closed 스킵**: 롱인데 stop>entry → `"trade_setup" not in snap`(legacy shape 보존).
- **explicit short**: `position_direction="short"`, target<entry<stop → `direction=="short"` 저장.
- **입력 불완전**: target_price 없음 → `"trade_setup" not in snap`.
- `add_items_to_draft` 경유도 동일 부착 확인(선택).

### 5.4 부차(Step 4) — `tests/test_...shared_recommendation` 류

- buy_zones/sell_targets/stop_loss 채워진 analysis → `recommendation["risk_reward"]` 존재+값.
- 삼각 불일치(target<current 등) → `risk_reward` 키 부재, 본체 무영향.

### 5.5 프런트 — `frontend/invest/src/__tests__/InvestmentReportBundleContent.*.test.tsx`

- `evidenceSnapshot.trade_setup(status=computed)` 있는 아이템 → R:R 라인/칩 렌더 텍스트 확인.
- `trade_setup` 부재 또는 status≠computed → 렌더 안 됨(회귀).

### 5.6 가드 회귀

- `tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py` green
  (신규 헬퍼가 provider import 없음 재확인).

---

## 6) 마이그레이션 / 롤아웃 노트

- **마이그레이션 0**. 신규 컬럼 없음. `trade_setup` 은 `evidence_snapshot` JSONB 하위 예약키.
- `position_direction` 은 입력 전용 추가 필드로 **컬럼 미생성**(값은 `trade_setup.direction` 로 echo).
- **하위호환**: 기존 리포트/아이템은 `trade_setup` 부재로 그대로 유효. 응답 스키마 무변경
  (`evidence_snapshot` 그대로 노출). 프런트는 부재 시 렌더 스킵.
- **롤아웃 게이트 불필요**: 순수 산술, 브로커/주문/감시/네트워크 mutation 없음. 즉시 유효.
- 배포 후 별도 데이터 백필 불필요(과거 아이템에 소급 계산 안 함 — 필요 시 후속 배치로 read-time
  재계산 가능, pure 헬퍼 재사용).

---

## 7) 리스크 · 스코프 밖

### 리스크

- **방향 오추론 → 오해성 카드**: side/intent 만으로 롱/숏/청산 구분. 완화 = 숏은 explicit
  `position_direction` opt-in 만, 청산은 스킵, 삼각 불일치는 fail-closed(침묵). 매트릭스 테스트로 고정.
- **다중 leg headline 집계 의미**: 수량가중 vs 단순평균 규칙이 임의적일 수 있음 → 문서(D6)+테스트로
  결정론 고정. leg 배열도 함께 노출해 투명성 확보.
- **Decimal 직렬화(문자열)**: evidence_snapshot 관례상 Decimal→str. 프런트/소비자는 문자열 파싱 필요
  (기존 stop_loss/target_price 와 동일하므로 신규 리스크 아님).
- **예약키 충돌**: caller 가 `trade_setup` 를 직접 주입 시 서버 계산과 충돌 → 스키마 가드로 거부.

### 스코프 밖

- **ROB-691**: 롱 청산의 실현손익(realized P/L) 프레임/표시 — 별도 이슈. 본 작업은 청산에 R:R 미부착까지만.
- **DB 컬럼/마이그레이션**: 대량 R:R 분석 쿼리 수요 발생 시의 컬럼 승격은 후속(현재 YAGNI).
- **on-demand R:R MCP 도구**: 아이템 없이 임의 3가격 R:R 계산 도구는 미포함(필요 시 pure 헬퍼로 후속).
- **숏 계정 배선**: Binance 선물 등 실제 숏 표면의 방향 자동 신호(account_scope 확장)는 미포함 —
  현재는 `position_direction` 명시만.
- **과거 리포트 소급 백필**: 미포함.

### 예상 diff 규모

| 영역 | 대략 LOC |
|---|---|
| `risk_reward.py`(신규 pure 헬퍼) | ~120–160 |
| 스키마(필드+가드) | ~4 |
| ingestion 배선(+직렬화 헬퍼) | ~20–30 |
| shared.py 부차(Step 4) | ~15 |
| 프런트 ItemRow R:R 라인 | ~25–35 |
| 테스트(단위+round-trip+스키마+프런트) | ~250–330 |
| **합계** | **~430–580, 마이그레이션 0** |
