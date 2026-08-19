# Funding advisory 운영 계약

## 안전 경계

Funding advisory는 조회·검토 전용이다. 외부 현금 선언은 운영자가 보고한
snapshot이며 broker 잔고 증거가 아니다. 선언값은 주문가능 금액, 필요 금액,
부족액, sizing, cap, eligibility, auto-approve, cash claim 또는 submit 입력으로
사용하지 않는다. 이 기능 범위에는 기존 available/required/shortfall 산식 변경이
없다.

현재 PR-1 기반은 `review.external_cash_declarations` DDL과 서비스 내부 read/write
계약만 제공한다. API router, 페이지, Telegram, candidate producer 연결은 없으므로
마이그레이션을 적용하지 않은 기존 runtime 동작도 바뀌지 않는다. 마이그레이션
실행과 배포는 운영자 cutover 대상이며 이 구현 작업에서는 수행하지 않는다.

## 최초 640,000원 선언

마이그레이션에는 business row가 없다. 초기값은 후속 `/invest/funding` admin 폼이
연결된 뒤 다음 값으로 한 번 선언한다.

- location: `parking_primary`
- label: `파킹통장`
- currency/amount: `KRW 640000`
- source note: `토스증권 → 파킹통장 이동`
- as-of date: `2026-08-15` KST

정확한 as-of 시각은 알려져 있지 않다. 운영자가 timezone이 포함된 실제 관측 시각을
확인하기 전에는 submit하지 않는다. `build_initial_parking_declaration()`은 시각을
기본값으로 만들지 않고, KST 날짜와 timezone을 확인한 요청만 만든다. 요청 생성은
DB write가 아니며 실제 write는 `ExternalCashDeclarationService.declare()`만 한다.

최초 선언의 `expected_head_declaration_id`는 명시적으로 `null`이다. 폼에서 발급한
non-secret idempotency key는 submit 재시도에도 그대로 재사용한다. 같은 key와 같은
payload는 기존 행을 반환하고, 다른 payload는 conflict로 끝난다.

마이그레이션 재실행은 값을 넣지 않는다. downgrade는 테이블과 그 안의 선언을
제거하고, 이후 upgrade는 빈 테이블을 만든다. 따라서 rollback/upgrade가 640,000원
행을 자동 재생성하지 않으며, 복구가 필요하면 운영자가 새 idempotency key와 정확한
관측 사실을 다시 확인해야 한다.

## 선언 수정과 freshness

수정은 UPDATE가 아니라 이전 head UUID를 `supersedes_declaration_id`로 지정한 INSERT다.
owner/location/currency가 달라지면 DB trigger가 거절한다. 서비스는 scope advisory
lock과 expected-head compare-and-set을 사용하며, head가 둘 이상이면 임의로 최신값을
선택하지 않는다.

freshness는 `recorded_at`이 아니라 `as_of + 24h`다. stale/future/ambiguous 값은 이력
문맥으로만 보이고 조달 가능액은 `금액 미상`이다. 실제 이체나 조달 완료는 target
broker buying power의 새 관측으로만 확인한다.
