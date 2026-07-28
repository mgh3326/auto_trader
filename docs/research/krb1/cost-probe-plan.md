# KR-B1 P0 비용 probe 계획

상태: `PLAN_ONLY / NOT_EXECUTED`

정본: `KRB1-CSM60-H5-v1`, 봉인 JSON SHA-256
`d5e1246b2072ad227d924e059091e27fa49719e42a5bfba651ae8f2bced9d6f1`.
이 문서는 봉인 조문의 수치·임계·절차를 바꾸지 않으며 현재 probe를 실행하지 않는다.

## 금지 경계

- P0 probe는 `kiwoom_mock` / `KRX_ONLY`에서만 한다.
- 실계좌·실주문·production DB write·배포는 하지 않는다.
- broker mock이 개장 전 접수, closing auction 생존, 정규장 종료 후 DAY 자동 만료 중
  하나라도 지원하지 않으면 대체 절차를 만들지 않고 P0 실패 +
  `NOT_DISCRIMINABLE`로 끝낸다.
- P0 전에 `C_stress_cap`, 비용표, tick 함수, 상·하한가 산식의 값을 확정하거나
  canonical hash에 넣지 않는다.

## P0 표본과 원증거

10개 동결 KRX 세션 동안 KOSPI와 KOSDAQ을 포함해 1주 왕복 probe를 합계 20건 이상
수집한다. 두 시장이 모두 포함되어야 하며, 봉인 조문에 없는 시장별 할당 수는 새로
만들지 않는다. 각 probe는 다음 원증거를 같은 correlation 단위로 보존한다.

- 시장·종목·상품유형, KRX 거래일, broker order/execution ID
- 매수·매도 요청/접수/체결 시각, 가격, 수량, venue, validity
- 매수 전·reserve 후·체결 후·매도 체결 후 cash와 그 delta
- broker가 보고한 매수 수수료, 매도 수수료, 매도세, 평균단가
- 요청가·체결가에서 broker가 수용한 호가단위
- 취소 probe의 취소 전/후 reserved cash와 해제 시각
- 모든 broker 응답 원문은 별도 immutable evidence 파일로 보존하고 SHA-256만
  journal row에 넣는다.

왕복 1주 표본마다 `cash delta = 매도대금 - 매수대금 - 수수료 - 매도세`와 broker
평단을 정수 KRW 원장으로 대사한다. 불일치, 결손, 중복 correlation, venue 이탈은
임의 보정하지 않고 결함으로 기록한다.

## tick_floor / tick_ceil와 상·하한가

P0-1에서 KOSPI·KOSDAQ 각각에 대해 가격 band 경계의 바로 아래/경계/바로 위를
포함하는 표를 만든다. 각 가격에서 broker preview/acceptance가 수용하는 호가단위를
원증거로 기록하고 다음 두 연산을 그 표에 대조한다.

- 진입: `L_i = tick_floor(1.01 × C_t)`
- forecast target: `T_i = tick_ceil(L × (1 + C_stress_cap))`

상·하한가는 동일 종목·세션의 broker 기준가, broker 표시 상한가·하한가, 주문
preview 수용가격을 함께 저장해 산식을 검증한다. 청산 주문은 5번째 세션 15:10의
당일 유효 하한가 지정가라는 정본 절차를 그대로 사용한다. 정본은 호가 band별 표와
상·하한가의 반올림 세부식을 명시하지 않으므로 P0 실측 전에는 구현 하나를 정답으로
선언하지 않는다. P0 증거가 단일 산식을 확정하지 못하면 fail-closed 한다.

## 비용표와 C_stress_cap 동결

20건 이상을 모두 대사한 뒤 시장/상품/side별 관측 수수료·세금·cash delta 표를 만든다.
모의 비용이 실제 적용 비용과 다르면 정본 지시대로 실제 비용표로 재계산한다.
`stress_pnl_num_krw_i`에는 동결 비용정책·반올림 뒤의 정수 KRW로 실청산대금,
실진입대금, 매수/매도 수수료, 매도세, 진입 1틱과 청산 1틱 adverse shortfall을
execution 합산한다.

정본에는 비용 실측값을 `C_stress_cap` 하나로 축약하는 reducer/반올림 식이 없다.
따라서 probe 표를 보기 전에 reducer를 추가하지 않는다. P0 증거와 별도의 승인된
정본이 그 식을 닫은 뒤에만 `C_stress_cap`을 계산하고, 닫히지 않으면 canonical
hash를 만들지 않고 모호성으로 보고한다.

## P0 → canonical hash 순서

순서는 다음과 같이 고정한다.

1. 봉인 정책 JSON과 코드, P0 calendar, journal schema를 입력 권위로 고정한다.
2. P0 10세션에서 비용·tick·상하한가 증거를 수집한다.
3. 20건 이상 왕복 원장을 대사하고 mock 비용과 실제 비용표 차이를 판정한다.
4. tick_floor/ceil 및 상·하한가 산식을 증거로 확정한다.
5. 승인된 reducer가 있을 때만 `C_stress_cap`을 확정한다.
6. 비용표·반올림·tick 함수·상하한가 산식·`C_stress_cap`과 각 원증거 SHA-256을
   P0 비용계약 JSON에 기록한다.
7. 그 P0 비용계약 JSON을 포함한 실행정책 manifest를 canonical 직렬화하고
   SHA-256을 계산한다. 기존 봉인 JSON 해시는 바꾸지 않는다.
8. 이 hash를 journal P0 완료 anchor와 이후 forecast의
   `policy_version=v1:<canonical_sha256>`에 사용한다.
9. 그 다음에만 20세션 PnL-blind dry-count를 시작한다.

즉 비용값은 P0의 입력이 아니라 P0 산출물이며, P0 산출물 확정 뒤 hash가 만들어진다.
