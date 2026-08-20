# Negative-class (rejected-candidate) recording — ROB-1283

**한 줄**: "매수 후보가 정말 없었나"에 정본 데이터로 답하기 위한 기각 코호트 기록 경로.
🔴 **관측 전용** — 주문·승인 경로에 영향 0.

---

## 1. 근본 원인 (AC1) — 실측 확정

### 관측 (prod DB, 2026-08-20 read-only SELECT)

| 관측 | 값 |
|---|---|
| `investment_report_items[deferred_no_action]` 마지막 | **2026-06-15** (77건 전부 05-26~06-15) |
| `investment_report_items` 테이블 전체 마지막 | 2026-07-28 (1건) |
| 아이템 생산 프로파일(CLAUDE_ADVISOR/HERMES_*/claude_crypto_operator) 마지막 | **2026-06-15** |
| 그 이후 리포트 | claude_ops 3건(06-23/27/29, deferred **0**) + CLAUDE_ADVISOR 1건(07-28, 아이템 1) |
| `trade_forecasts` **첫** 행 | **2026-07-03** |
| `trade_forecasts` 마지막 | 2026-08-20 (정상 가동, 410건) |
| `report_uuid` / `report_item_uuid` | **전건 NULL** |

### 판정: **핸들러 파손 아님 — 플레이북 드리프트(호출 지점 부재)**

근거 3가지:

1. **핸들러는 살아 있다.** 07-28 CLAUDE_ADVISOR 리포트가 아이템까지 정상 생성됐다.
   `register_investment_report_tools`는 프로파일 분기 없이 **무조건 등록**되므로
   도구 자체는 세션에 노출돼 있다.

2. **두 표면은 겹친 적이 없다.** 아이템은 06-15에 죽고 forecast는 **07-03에 시작**했다.
   → `report_uuid` 100% NULL은 "링크 배선이 끊긴 것"이 아니라 **연결할 리포트가 애초에
   존재한 적이 없는 것**이다. 🔴 이 구분이 중요하다: 링크 배선만 고쳐도 아무것도 안 채워진다.

3. **라이브 프롬프트 5개 전부에 호출 지점이 없다.** (`auto_trader-operator/prompts/`)

   | 프롬프트 | `deferred_no_action` 언급 | `investment_report_create`/`add_items` 언급 |
   |---|---|---|
   | kr-open-trade.md | O (L77) | **0** |
   | us-open-trade.md | O (L46) | **0** |
   | crypto-session-trade.md | O (L49) | **0** |
   | kr-nxt-prep.md | O (L38) | **0** |
   | kr-nxt-preopen-trade.md | O (L87) | **0** |

   전부 `forecast_save`는 이름을 대고, `decision_bucket`을 **쓸 수 있는 유일한 도구**는
   이름을 대지 않는다. 세션은 계약의 forecast 절반만 이행할 수 있었고, 그것을 성실히 했다.

**즉 결함은 세션에도 핸들러에도 없다. 어휘만 있고 호출 지점이 없는 계약에 있었다.**
그리고 아무도 보지 않았기 때문에 **66일간 침묵**했다.

### 재현

```bash
ENV_FILE=... uv run python -m scripts.diagnose_negative_class_recording        # 사람용
ENV_FILE=... uv run python -m scripts.diagnose_negative_class_recording --json # 기계용
```
읽기 전용(SELECT만). exit 0 = 기록 정상, exit 1 = stalled/never_recorded.

---

## 2. 복구 (AC2) — 세션이 실제로 부르는 표면에 negative class를 얹는다

`forecast_save`가 라이브 표면이므로 어휘를 그쪽으로 가져왔다.

```python
forecast_save(
    created_by="kr-open-trade",
    symbol="005930",
    instrument_type="equity_kr",
    forecast_target={...},         # resolvable target
    probability=0.30,
    review_date="2026-09-01",
    decision_bucket="deferred_no_action",   # ← 기각 코호트
    report_item_uuid="<item_uuid>",         # ← 리포트 아이템이 있으면 함께
)
```

- 컬럼: `review.trade_forecasts.decision_bucket` (nullable Text + CHECK + index)
- 어휘 정본: `app/models/decision_vocabulary.DECISION_BUCKETS` — 리포트 아이템 CHECK,
  Pydantic 스키마, forecast CHECK가 **같은 튜플**에서 생성돼 3층이 드리프트할 수 없다
- 🔴 오타는 **거부**된다(`ForecastValidationError` + DB CHECK 이중). 조용히 저장돼서
  코호트에서 사라지는 경로가 없다 — 그게 이 이슈의 원래 실패 형태다
- `NULL` = "미분류"이지 **"기각 아님"이 아니다**

### 왜 forecast 한 건만으로도 가치가 있나 (고아 기록 금지)

bucket + resolvable target + review_date + outcome/brier가 **한 행에** 있다.
그래서 기각 forecast는 실행한 판단과 **동일한 경로로** resolve·채점된다.
리포트 아이템이 없어도 고아가 아니다. (`tests/test_rob1283_negative_class_db.py::
test_negative_class_forecast_is_gradeable_like_any_other`가 기계 증명)

ROB-1301 A/B 채점기가 필요로 하는 (코호트, 결과, 귀속)은 전부 이 한 테이블에서 나온다:
`decision_bucket` × `session_label`/`created_by` × `outcome`/`brier_score`.

---

## 3. 링크 (AC3)

- `report_uuid` / `report_item_uuid`는 **UUID 문자열로 검증·정규화**된다.
  조인 불가능한 값은 **쓰기 시점에 거부** — 링크된 것처럼 보이면서 영원히 조인 안 되는
  행을 만들지 않는다(그건 링크 없음보다 나쁘다).
- 🔴 **기존 351건 백필은 하지 않았다.** 연결 대상 리포트 아이템이 존재하지 않으므로
  백필할 진실이 없다. 별건이 아니라 **불가능**이다 — §1 판정 참조.
- negative class인데 `report_item_uuid`가 없으면 `forecast_save` 응답의 `warnings`가
  알려준다(차단 아님, 조언).

### 조회

```
get_forecasts(decision_bucket="deferred_no_action")   # 기각 코호트
```
`summary.by_decision_bucket`은 `unclassified`를 **별도 카운트**한다 — 그 숫자가
사각지대의 크기이지 0이 아니다.

---

## 4. 회귀 가드 (AC4) — 런스타트에서 보인다

`get_operating_briefing` 응답에 `negative_class_recording` 섹션이 추가됐다.

```json
{
  "status": "stalled",           // ok | stalled | never_recorded | unavailable
  "market": "kr",
  "last_recorded_at": "2026-06-15T00:00:00+00:00",
  "last_source": "report_item",  // forecast | report_item
  "stale_days": 66,
  "stall_threshold_days": 7,
  "gap": { "starts_at": "...", "ends_at": null, "open": true,
           "days": 66, "backfilled": false },
  "notes": ["..."]
}
```

🔵 **운영자 레포 변경 0으로 세션 준수 스탬프에 도달한다.**
route-stamp(`operator-compliance/v1`)가 `briefing_capture.response`에 브리핑 응답을
**통째로 박제**하므로, 이 섹션은 자동으로 스탬프에 들어간다.

주의:
- 🔴 임계 7일은 **판단값**이다. 시장별 세션이 하루 여러 번 돌고 매 회차가 사지 않은
  후보를 평가하므로, 일주일 침묵은 조용한 주가 아니라 계약 미이행이다.
  연휴로 우는 늑대가 되지 않을 만큼은 느슨하게 잡았다.
- 🔴 **buckets 없는 일반 forecast는 카운트되지 않는다.** 실제 정지가 숨은 방식이
  정확히 이거다 — `trade_forecasts`는 66일 내내 바빴다. "forecast 쓰이고 있나?"라고
  물었으면 66일 동안 "예"라고 답했을 것이다.
- 프로브 실패는 `status="unavailable"` + 사유. 🔴 **fail-open이지 fail-silent가 아니다** —
  깨진 프로브가 깨끗한 `ok`로 세탁되지 않는다.

---

## 5. 결손 구간 — 🔴 메우지 않는다

06-15 → (첫 bucketed forecast) 구간에는 구조화된 기각 기록이 **없다**.

- `gap`으로 **명시 보고**된다. 끝점은 하드코딩이 아니라 데이터에서 파생되므로
  구멍이 닫히면 설명도 따라 닫힌다.
- `backfilled: false` 고정. 🔴 **추론 백필 금지** — 가짜 연속성은 눈에 보이는 구멍보다
  나쁘다. 믿어지기 때문이다.
- 이 구간을 가로지르는 코호트 분석은 **정확히 이 창만큼 불완전**하다. 인용 시 명시할 것.
- §89차 gatecal(N=17)이 프록시 한정이었던 이유가 이 구간이다. 🔴 그 숫자로 게이트
  완화/유지를 결정하지 말 것 — 자료이지 결론이 아니다.

---

## 6. 남은 일 (이 PR 범위 밖)

1. 🔴 **운영자 레포 프롬프트 5종에 `decision_bucket` 인자를 명시**해야 실제 기록이 재개된다.
   이 PR은 기록을 *가능·가시*하게 만들었을 뿐, 아웃오브프로세스 세션의 호출을 강제할 수 없다.
   §1의 판정이 정확하다면 **이것이 유일한 잔여 차단 요인**이다.
   (auto_trader-operator는 별도 repo·PR 관할)
2. 마이그레이션 적용: `alembic upgrade head` — 운영자 소유 작업. 이 PR은 실행하지 않았다.
3. 임계 7일은 실측 후 조정 가능(`STALL_THRESHOLD_DAYS`).

## 7. 안전 경계

- 브로커/주문/watch/승인/order-intent mutation **도달 불가**. 관측 전용.
- 스케줄러 등록 **0**. 진단 스크립트는 수동 실행만.
- 마이그레이션은 additive(1 컬럼 + 1 CHECK + 1 인덱스). 기존 컬럼·제약·인덱스 무변경,
  행 재작성 없음. downgrade 왕복 검증 완료.
- 진단 스크립트는 SELECT만 발행한다.
