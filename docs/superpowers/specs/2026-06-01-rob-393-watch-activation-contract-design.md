# ROB-393 — watch 항목 생성–활성화 계약 모순 해소 (설계)

- **이슈**: ROB-393 (오케스트레이션 D라인 ROB-412의 우선 처리 항목)
- **날짜**: 2026-06-01
- **상태**: 설계 승인됨 → 플랜 작성 단계
- **범위**: advisory/report/watch metadata 정합성. broker/order/order-intent mutation·scheduler activation·자동집행 배선 **없음**.

## 1. 문제

`/invest/reports` watch 항목의 생성–승인–활성화 3단계 계약이 비대칭이다.

| 단계 | watch_condition / valid_until | 강제 위치 |
|---|---|---|
| **생성** `operation="review"` | **면제** | `app/schemas/investment_reports.py:179` (`_validate_watch_invariants`가 `operation in (None,"create","modify")`에만 적용); DB CHECK도 `operation IN ('cancel','keep','review')`는 NULL 허용 (ROB-274 마이그레이션 `20260520_rob274_p1_*`) |
| **승인** approve | **검증 없음** | `app/services/investment_reports/decisions.py` — 그냥 `approved`로 전이 |
| **활성화** activate | **필수 (하드 raise)** | `app/services/investment_reports/watch_activation.py:68-71` |

결과: `operation="review"`로 정상 생성한 watch는 condition 없이 `approved`까지 도달하지만, `activate_watch`에서
`"watch_condition missing on item (corrupt state)"`로 **영구히 활성화 불가**.
2026-06-01 NXT 개장 리포트(`report_uuid=4e20c131-...`, 삼성전자 005930 / SK하이닉스 000660)에서 실제 재현됨.
"corrupt state" 문구는 운영자에게 데이터 손상으로 오인을 유발하나 실제로는 **정상 생성 결과**다.

## 2. 결정한 방향 — (b)+(c)

ROB-274가 `review/keep/cancel`을 의도적으로 condition-면제로 완화한 설계를 **보존**한다.
따라서 비대칭은 **생성 시 강제(a)** 가 아니라 **활성화 시점에서 메운다**:

- **(b)** `activate_watch`가 명시적 `watch_condition`(+`valid_until`) 인자를 받아 review-watch를 활성화할 수 있게 한다.
- **(c)** 조건 없이 활성화 시도 시 `"corrupt state"`가 아니라 **actionable** 메시지로 거부한다.
- **approve 단계는 변경하지 않는다.** condition 없는 review-watch의 approve는 audit 기록으로 그대로 허용.

조건 **자동 파생**(매수가 기준·유효기간 정책)은 본 이슈 범위가 아니며 **ROB-337**의 seam으로 남긴다.

## 3. 변경 단위

### 3.1 스키마 — `ActivateWatchRequest` 확장
`app/schemas/investment_reports.py` (현 라인 310)

```python
class ActivateWatchRequest(BaseModel):
    item_uuid: UUID
    actor: str
    idempotency_key: str | None = None
    # ROB-393 — review-watch는 생성 시 condition 면제. 활성화 시 주입 허용.
    watch_condition: WatchConditionPayload | None = None
    valid_until: datetime | None = None
```

기존 `WatchConditionPayload`를 재사용 → metric/operator/threshold/action_mode Literal 검증이 그대로 적용된다.

### 3.2 서비스 — `WatchActivationService.activate`
`app/services/investment_reports/watch_activation.py` (현 라인 68-71)

`item.watch_condition` / `item.valid_until`가 `None`일 때:

1. **request에 해당 값이 제공됨** → 그 값을 **item에 영속화**한 뒤 진행.
   - items = source of truth 원칙 유지. 재활성화(idempotent) 시에도 item이 일관된 상태.
   - 영속화 후 기존 alert-build 로직(`condition["metric"]` 등)이 그대로 동작.
2. **제공 안 됨** → actionable `ValueError`:
   - `watch_condition`: `"watch_condition not set (operation='review' watch); pass watch_condition to activate, or recreate the watch with a condition"`
   - `valid_until`: `"valid_until not set (operation='review' watch); pass valid_until to activate, or recreate the watch with an expiry"`
   - 문구에 `"corrupt state"` 미포함.
3. **충돌 가드**: `item.watch_condition`이 **이미 not-null인데** request에도 `watch_condition`이 오면 → 거부
   (`"watch_condition already set on item; refusing to override at activation"`). silent override 금지.
   `valid_until`도 동일.

`item_kind != "watch"` / `status != "approved"` 기존 가드는 condition 체크보다 **앞에** 유지(순서 불변).

### 3.3 리포지토리 — 영속화 메서드 추가
`app/services/investment_reports/repository.py`

```python
async def update_item_watch_condition(
    self, item_id: int,
    watch_condition: dict | None,
    valid_until: datetime | None,
) -> None
```

주입된 condition/valid_until을 item 행에 기록. 둘 중 주어진 값만 갱신(None은 미변경). DB CHECK는 review에 not-null을 허용하므로 안전.

### 3.4 MCP 핸들러 — 파라미터 패스스루
`app/mcp_server/tooling/investment_reports_handlers.py` (현 라인 387 `investment_report_activate_watch_impl`)

`watch_condition: dict | None = None`, `valid_until: str | None = None` 파라미터 추가 → `ActivateWatchRequest.model_validate`로 전달. MCP 툴 등록부 시그니처/스키마도 동기화.

## 4. 테스트 (TDD — 재현 우선)

`tests/test_investment_reports_mcp.py`:

1. **재현(RED)**: review-watch(condition 없음) 생성 → approve → activate **인자 없이** → `ValueError`이고 메시지에 `"corrupt state"` **미포함**, actionable 문구 포함.
2. **주입(GREEN)**: 같은 경로 + activate에 `watch_condition`/`valid_until` 제공 → 성공, alert 생성 + **item에 condition 영속화** 확인.
3. **충돌**: item에 이미 condition 있는 정상 watch에 activate로 또 condition 주면 거부.
4. **회귀**: 기존 `test_activate_watch_copies_snapshot`(정상 경로, 인자 없이 활성화) 무손상.

모델 단 CHECK 테스트(`tests/test_investment_reports_model.py`)는 변경 없음(스키마/제약 불변).

## 5. 안전 경계 / 비범위

- **마이그레이션 없음** — 컬럼(`watch_condition`/`valid_until` nullable)·CHECK 제약 모두 그대로 활용.
- broker/order/order-intent mutation 없음. live/mock 자동집행 배선 없음(ROB-402와 분리).
- recurring scheduler activation 없음.
- approve 상태머신 변경 없음.
- **조건 자동 파생은 ROB-337 seam**: activate가 "명시 인자 수용 또는 명확한 거부"까지만 책임진다.

## 6. 완료 기준 매핑 (ROB-412)

- ✅ watch 생성/승인/활성화 계약 모순이 **재현 테스트와 함께** 해결 (§4-1,2).
- ✅ 활성화 불가 상태가 **명확한 actionable reason**으로 표현 (§3.2-2). (approve 차단 대신 activate 시점 해소 — 완료기준의 "또는" 절 충족.)
- ✅ ROB-337/376으로 넘어갈 **contract seam 문서화** (§2, §5).
- ✅ scheduler/automation 비활성 상태 유지 명시 (§5).
- ✅ no broker/order/order-intent mutation + no scheduler activation 경계 → Linear 댓글에 테스트/CI evidence와 함께 기록.
