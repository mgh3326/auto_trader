# Funding advisory 운영 계약

## 안전 경계

Funding advisory는 조회·검토 전용이다. 외부 현금 선언은 운영자가 보고한
snapshot이며 broker 잔고 증거가 아니다. 선언값은 주문가능 금액, 필요 금액,
부족액, sizing, cap, eligibility, auto-approve, cash claim 또는 submit 입력으로
사용하지 않는다. 이 기능 범위에는 기존 available/required/shortfall 산식 변경이
없다.

PR-1 기반은 `review.external_cash_declarations` DDL과 서비스 내부 read/write 계약만
제공하며 아무 runtime 호출자가 없다. 따라서 PR-1 자체는 기존 동작을 바꾸지 않는다.
PR-2가 typed candidate evidence, 조회 API, 페이지와 Telegram delivery claim을 연결한다.
두 마이그레이션의 실행과 배포는 운영자 cutover 대상이며 구현 작업에서는 수행하지
않는다.

## 최초 640,000원 선언

마이그레이션에는 business row가 없다. 초기값은 `/invest/funding` admin 폼에서 다음
값으로 한 번 선언한다.

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

## Candidate와 숫자 계약

producer가 넘기는 `PassedNonFundingGateEvidence`는 비-자금 gate 전건 통과와
broker-authoritative funding snapshot을 함께 묶는다. mock·paper account mode는 이
계약을 만들 수 없다. `gate_version`은 평가마다 바뀌는 해시가 아니라 gate
계약/스키마 버전이다. 평가별 무결성 값은 별도 `evidence_hash`다.

후보 shortfall은 해당 후보 한 건의 `required_cash - target_buying_power`다. 기존
Toss 경로처럼 같은 계좌의 다른 pending limit buy가 존재할 수 있으므로 화면과
Telegram에는 `other_pending_required`, `reserved_cash`, 그리고 둘을 포함한 운영상
gap을 별도 행으로 항상 공개한다. 외부 현금 선언은 경로 카드의 운영자 선언 근거일
뿐이며 counted 금액은 0이다. 기존 `buying_power.py`와 `revalidation.py`의 available,
required, shortfall 산식은 이 기능 범위에서 바꾸지 않는다.

## 발화와 fail-open

Telegram 발송은 상류 candidate event에서만 delivery ledger의 당일 row를 claim한 뒤
가능하다. 같은 날 내용이 바뀌면 기존 message edit만 시도한다. 상태가 불명확한 send
또는 edit은 자동 재시도하지 않는다. 페이지 GET/refresh는 evidence가 유효한 동안
revision을 다시 계산할 수 있지만 delivery claim이나 Telegram send를 호출하지 않는다.

advisory `evaluate()`는 proposal create 전에 호출되지만 완전한 fail-open 경계 안에
있다. validation, DB, Telegram 등 어떤 예외도 구조화된 unavailable 상태로 기록하고
기존 proposal create를 계속한다. proposal commit 뒤 provenance link가 실패해도 이미
성공한 create 결과는 바뀌지 않는다.

## 경로 비교와 기존 승인 분류

모든 단일 경로는 금액, 비용, 시간, 실현 영향, 가역성의 다축 지배 규칙으로 비교한다.
부분 조달 조합도 그 비교 결과에서 만든 참고 시나리오일 뿐 `selected=false`이며 자동
선택되지 않는다. 저비용만으로 느리고 비가역적인 경로를 1순위로 만들지 않는다.

`source_funding_advisory_id`는 별도 append-only link의 provenance 전용 값이다. proposal
payload, source_asof, payload hash, dispatch classifier, sizing, eligibility에는 들어가지
않는다. 수익 종목 축소가 별도 create 확인으로 ordinary limit sell이 되면 기존 dispatch
분류와 승인/veto를 그대로 받는다. 손실 종목 교체는 기존 `loss_cut_intent` 거절과
2-click nonce를 그대로 유지한다. create 확인은 곧 기존 승인/veto 경로 진입이며
funding 예외 태그는 없다.

Telegram과 `/invest/funding`의 두 매도 경로는 다음 고정 문구를 쓴다.

`경로 설명 · 이 화면에서 주문 안 만듦`

카드 버튼은 읽기 전용 URL뿐이며 callback이나 proposal create 동작이 없다. 외부현금
폼의 submit은 append-only 선언만 만들며 돈을 이동시키지 않는다.
