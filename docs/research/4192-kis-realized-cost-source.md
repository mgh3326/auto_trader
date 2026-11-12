# KIS 체결별 실현 수수료·세금 출처 조사 (L11)

기준 코드: `origin/main` `3a411fa2538ecce7074e7fa8ea78ff2dbb6be3da`. 이 메모는 저장소에 포함된 클라이언트 코드와 문서만 조사한다. 브로커 API 호출이나 운영 계좌 데이터는 사용하지 않았다.

질문은 설계 문서 Q10 및 L11의 범위다 (`docs/design/4154-candidate-to-retro-linking.md:532`, `563`).

## 결론

저장소에서 확인되는 KIS 읽기 전용 체결 조회는 국내 `TTTC8001R`와 해외 `TTTS3035R`이다. 현재 코드와 저장소 안의 API 설명만으로는 어느 쪽도 **체결별 실현 수수료 및 세금을 반환한다고 입증할 수 없다**. 따라서 L11의 질문에 대한 저장소 근거 기반 답은 “그런 API가 없다”가 아니라 “현재 구현·문서에서 확인되지 않았다”이다. KIS 외부 API가 별도 실현비용 endpoint 또는 관련 필드를 제공하는지는 미검증이다 `[INFERENCE]`.

| 시장 / TR | 저장소의 경로와 동작 | 비용·체결 단위에 관한 관찰 | 판정 |
|---|---|---|---|
| 국내 `TTTC8001R` | `/uapi/domestic-stock/v1/trading/inquire-daily-ccld`; `DOMESTIC_DAILY_ORDER_TR` (`app/services/brokers/kis/constants.py:85-88`). `inquire_daily_order_domestic`는 주문 기간·종목·매매구분·주문번호를 받아 페이지 조회한다 (`app/services/brokers/kis/domestic_orders.py:739-750`, GET 요청 `850-857`). | 코드에 기록된 read-only probe 응답 키는 주문번호, 주문/체결 수량·금액, 잔량, 주문일·시각 등이며 `fee`, `tot_fee`, tax 키는 목록에 없다. 특히 `tot_ccld_qty`/`tot_ccld_amt`는 누적 체결 값이다 (`app/services/brokers/kis/domestic_orders.py:773-787`). 이것은 probe에서 확인한 열거 목록에 비용 키가 없다는 뜻이지, 모든 가능한 API 응답에서 키가 절대 없다는 증명은 아니다 `[INFERENCE]`. | 실현 비용을 주는 per-fill endpoint라는 근거 없음. API 호출 자체는 GET 조회로 구현됨. |
| 해외 `TTTS3035R` | `/uapi/overseas-stock/v1/trading/inquire-ccnl`; `OVERSEAS_DAILY_ORDER_TR` (`app/services/brokers/kis/constants.py:138-141`). `inquire_daily_order_overseas`는 거래소·기간·종목·매매구분으로 조회한다 (`app/services/brokers/kis/overseas_orders.py:673-710`, 인자 `715-756`, GET 요청 `762-770`). | 메서드 반환은 raw `list[dict]`이며 이 코드에는 응답 필드 스키마가 없다 (`app/services/brokers/kis/overseas_orders.py:685-710`). Reconciler normalizer는 `ft_ccld_qty`/`ft_ccld_amt3` 계열과 선택적 `fee`/`tot_fee`를 읽지만, 누락 시 fee를 0으로 바꾼다 (`app/services/execution_ledger/normalizers.py:322-345`). 이 구현은 비용 필드의 실제 존재, tax 필드, 또는 per-fill grain을 입증하지 않는다 `[INFERENCE]`. | 실현 비용을 주는 per-fill endpoint라는 근거 없음. 응답 grain과 비용 필드는 미검증. |

`TTTC8001R`는 코드 주석상 “일별 체결조회 (주문 히스토리)”이며 국내 응답 표에 총 체결 수량·금액을 담는다. 이로부터 주문 단위 집계값과 별개인 각 fill의 실현 비용을 반환한다고 해석할 수 없다. 해외 `TTTS3035R`는 이름과 raw 결과 형태만 확인되므로 per-fill 여부도 미확인이다 `[INFERENCE]`.

## 검색 범위와 증거 한계

- KIS 클라이언트/TR 상수와 체결 정규화 경로를 조사했다 (`app/services/brokers/kis/constants.py:85-141`, `app/services/brokers/kis/domestic_orders.py:739-857`, `app/services/brokers/kis/overseas_orders.py:673-770`, `app/services/execution_ledger/normalizers.py:297-345`).
- 저장소 파일 경로의 KIS/OpenAPI 명세 명명 패턴과 앱·문서·설정의 TR/비용 관련 문자열을 검색했으며, 공식 endpoint 응답 명세를 vendored 형태로 포함한 파일은 찾지 못했다 `[INFERENCE]`. 저장소의 `blog/blog_kis_api.md`는 시세 수집 예시이며 체결 비용 endpoint나 비용 필드 근거를 제공하지 않는다 (`blog/blog_kis_api.md:1-6`, `45-50`, `88-98`).
- 별도 실현비용 endpoint가 저장소에 없다는 검색 결과는 외부 KIS 제품/API 전체에 그런 endpoint가 없다는 사실을 증명하지 않는다 `[INFERENCE]`. 공식 문서나 승인된 비식별 응답 캡처 없이 새 TR ID나 필드 의미를 추정해 채택하지 않는다.

## 연구 비용 입력에 대한 처리

설계 문서의 현재 판정은 KIS 수수료·세금 출처를 확인하지 못했으므로 KIS 비용을 `not_provided`로 두고 읽기 시 `estimated` 비용과 분리하는 것이다 (`docs/design/4154-candidate-to-retro-linking.md:350-375`, `532-533`). 이번 조사는 그 상태를 바꿀 증거를 찾지 못했다. 현재 normalizer는 `fee`/`tot_fee`가 없으면 0을 만들고 이를 upsert의 `fee_amount`로 넘기므로 이는 측정된 0으로 해석하면 안 된다 (`app/services/execution_ledger/normalizers.py:309-310`, `339-340`, `380-383`). 이 PR은 해당 writer 동작을 변경하지 않는다.

향후 출처 채택 전 확인할 증거는 공식 TR·경로, 비용 필드명과 통화/단위, 값의 per-order 또는 per-fill grain, 수수료와 세금의 분리 여부, 취소·정정·부분체결 처리다. 이 메모는 그 확인이나 코드 변경을 수행하지 않는다.
