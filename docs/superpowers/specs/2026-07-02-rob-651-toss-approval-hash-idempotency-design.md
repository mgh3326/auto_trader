# ROB-651 P6-A — Toss preview→place approval-hash 바인딩 + 콘텐츠 기반 clientOrderId (KST/ET 거래일 salt) 멱등키

- **Linear**: ROB-651 (parent ROB-644; blocks ROB-653 P6-B; blocked-by ROB-645 — merged `75ee8878`)
- **Date**: 2026-07-02
- **Scope**: Toss 라이브 주문(`toss_preview_order` / `toss_place_order`)만. 공유 경로(KIS/Upbit)는 P6-B(ROB-653) 후속.

## 문제

`toss_preview_order`와 `toss_place_order`는 **독립 호출**로 각자 payload를 구성한다. 프리뷰한 것과 다른 주문이 나가도 아무것도 막지 않는다. `clientOrderId`는 place마다 uuid4 신규 발급(`orders_toss_variants.py:191-192`, `:827`)이라 동일 주문을 실수로 두 번 제출해도 브로커/레저가 dedupe하지 못한다.

정확한 선례가 리포에 존재한다: `alpaca_paper_orders.py:74-93` — `_canonical_payload` + `client_order_id = prefix + sha256(canonical JSON)[:16]`, preview/submit 동일 재계산 = 콘텐츠 바인딩 멱등(paper 전용).

## 확정 스펙 결정 (2026-07-02)

1. **TTL = 5분** — 프리뷰 후 재프리뷰 없이 place 가능한 유효시간.
2. **amount 기반 매수(orderAmount, quantity 없음)** = wire payload 그대로 해시. orderAmount는 wire payload에 결정적으로 존재하므로 그대로 sha256. quantity 강제 없음.
3. **롤아웃 기본값 = `optional`** — config 게이트로 `off / optional / warn / required` 승격.
4. **거래일 salt = 시장별** — KR = KST 날짜, US = ET 날짜(America/New_York, DST 반영). US 세션이 KST 자정을 넘어도 같은 거래일 → 자정경계 재시도 이중제출 방지.
5. **승인 주체 = operator confirm-as-approval** (이슈 §5). 제2 주체가 없어 creator≠approver 분리는 collapse. hash의 실질 가치는 "프리뷰한 것만 실행됨" 보장 — 사고(프리뷰≠실행) 방지용이지 악의적 호출자 방어가 아님.

## 설계

### 1. Canonical payload (프리뷰/플레이스 공통 재계산)

tick-snap **이후** wire payload에서 경제적 의도를 정의하는 필드만 정규화. `clientOrderId`(파생값이라 순환), `confirmHighValueOrder`(오퍼레이터 플래그, 경제적 의도 아님)는 **제외**.

```python
canonical = {
    "market": mkt,             # kr | us  (동일 심볼 KR/US 구분)
    "symbol": symbol,
    "side": side.upper(),      # BUY | SELL
    "orderType": order_type.upper(),   # LIMIT | MARKET
    "timeInForce": time_in_force,      # DAY | CLS
    "quantity": <str|None>,
    "price": <snap된 str|None>,
    "orderAmount": <str|None>,
}
canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
```

두 값을 파생:

- **approval_digest (content digest)** = `f"p6a-{sha256(canonical_json)[:16]}"` — 레저 `approval_hash` 컬럼에 저장, 로그·프리뷰 응답에 노출. "프리뷰한 것 = 실행되는 것" 바인딩.
- **clientOrderId (멱등키)** = `f"tossp6-{sha256(f'{canonical_json}|{trading_day}|{rung}'.encode())[:16]}"` (`rung` 없으면 빈 문자열). 23자, 영숫자+dash → `_client_order_id_error` safe-segment 통과.
  - `trading_day` = `_trading_day_salt(market)`: KR → `now_kst().date().isoformat()`, US → `now.astimezone(ZoneInfo("America/New_York")).date().isoformat()`.

### 2. TTL/diff 전달 — stateless token (Redis 미사용)

`toss_place_order`의 `approval_hash` 파라미터는 프리뷰가 돌려준 **self-contained 토큰**:

```
approval_hash = "p6a1." + base64url(json.dumps(
    {"iat": <issued_epoch_int>, "canon": <canonical dict>},
    sort_keys=True, separators=(",", ":"),
))
```

place 검증 흐름:

1. 토큰 파싱 실패/버전 불일치 → fail-closed(`invalid_approval_hash`).
2. `now_epoch - iat > 300` → fail-closed(`approval_expired`, "re-preview required").
3. place 자기 파라미터로 `canonical`을 **재계산** → 토큰의 `canon`과 필드별 비교.
   - 불일치 → fail-closed(`approval_hash_mismatch`) + `diff`(프리뷰 canon vs 재계산 canon의 상이 필드 dict).
4. 일치 & TTL 유효 → 진행. 레저에 `approval_digest` 저장.

**stateless 선택 근거**: place 경로에 Redis 의존 추가 없음(Redis 다운이 라이브 주문을 차단하는 실패모드 회피), 완전 결정적 테스트(`now` 주입), 재계산으로 content 바인딩. 단일 오퍼레이터 신뢰모델(§5)이라 토큰 위변조는 위협모델 밖. `diff`는 토큰에 canon을 임베드하므로 정확한 필드 단위로 산출 가능.

### 3. 결정적 clientOrderId — 기본 ON

uuid4 대신 결정적 해시가 **기본 동작**. hash 미제공(optional 단계)이어도 적용 — 이게 실질 이중제출 방지 핵심이고 strictly safer.

- 같은 거래일 · 같은 rung · 같은 payload → 같은 clientOrderId → 브로커 dedupe + 레저 `record_send` replay(기존 행 반환).
- 익일 → `trading_day` salt 변경 → 신규 clientOrderId → 신규 주문 성공.
- **같은 날 진짜 동일한 두 번째 주문**이 필요하면 `rung` discriminator로 분리(문서화).

### 4. 롤아웃 게이트 (config)

`Settings`에 `toss_approval_hash_mode: str = "optional"` 추가 (env `TOSS_APPROVAL_HASH_MODE`), 값 ∈ `{off, optional, warn, required}`:

| mode | hash 제공 | hash 미제공 |
|------|-----------|-------------|
| `off` | 무시(검증 안 함) | 진행 |
| `optional`(기본) | 검증(불일치/만료 → fail-closed) | 진행(백컴팻) |
| `warn` | 검증 | 경고 로그 후 진행 |
| `required` | 검증 | fail-closed(`approval_hash_required`) |

결정적 clientOrderId는 mode와 무관하게 항상 ON.

### 5. 레저 & 마이그레이션

- `review.toss_live_order_ledger`에 `approval_hash` **additive nullable Text 컬럼** (마이그레이션 1개, `down_revision` = 현재 head).
- `TossLiveOrderLedgerService.record_send` + `record_toss_place_order` 래퍼에 optional `approval_hash: str | None = None` 파라미터 추가.
- **replay 계약 불변**: `client_order_id` 기준 query-first replay(`toss_live_order_ledger_service.py:66-155`) 그대로. 기존 행 반환 시 approval_hash 덮어쓰지 않음.

### 6. 도구 표면 변경

- `toss_preview_order`: 응답에 `approval_hash`(토큰), `approval_expires_at`(ISO KST, now+5min), `payload_preview.clientOrderId`(결정적). optional `rung: str | int | None = None` 파라미터.
- `toss_place_order`: optional `approval_hash: str | None = None`, optional `rung: str | int | None = None`.
- `_toss_place_order_impl`이 실제 검증/파생 수행. `toss_place_order`는 두 신규 인자를 통과시킴(기존 `client_order_id_override=None` 봉인 유지 — override는 이제 결정적 파생으로 대체되므로 계속 None).

## 테스트 계획 (TDD)

**순수 유닛 (DB/네트워크 불필요):**
- `canonical` 안정성: 동일 입력 → 동일 digest; tick-snap 반영; quantity/price/orderAmount None 조합.
- 토큰 encode/decode roundtrip; 버전/base64 손상 → 파싱 실패.
- content 불일치 → `diff`에 상이 필드만.
- TTL: `iat` 주입, `now` 주입 → 300s 경계 만료/유효.
- clientOrderId 결정성 + `trading_day` 변화(KST/ET, `now` 주입) + `rung` 변화로 키 분기.
- amount 기반(orderAmount만) 해시 성립.

**도구 레벨:**
- preview→place happy path: preview의 approval_hash를 place에 전달 → 일치 통과(dry_run).
- place 파라미터가 preview와 다름 → mismatch fail-closed + diff.
- 만료 토큰 → `approval_expired`.
- mode `optional` + hash 없음 → 통과; `required` + hash 없음 → fail-closed; `warn` + 없음 → 경고 후 통과.
- 같은 거래일 재제출 → 동일 clientOrderId(레저 replay); 익일(`now` 주입) → 신규 clientOrderId.

**레저:**
- `record_send`에 approval_hash 저장; replay 시 기존 행 approval_hash 불변.

## 수용 기준 (이슈)

- ✅ hash 불일치 place가 fail-closed + diff 사유
- ✅ 동일 payload 같은 날 재제출 → 브로커/레저 dedupe, 익일 재제출 → 신규 주문 성공
- ✅ TTL 만료 hash → 재프리뷰 요구 에러
- ✅ 기존 `toss_place_order` 호출(optional 단계) 백컴팻

## 비목표 / 후속

- KIS/Upbit 공유 `_place_order_impl` 포팅 → P6-B(ROB-653).
- `warn`/`required` 승격 = 운영자 cutover 결정(이 PR은 `optional` 기본).
- `toss_modify_order`는 이번 스코프 밖(place 바인딩 우선).
