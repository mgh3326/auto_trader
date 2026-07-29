# KR-B1 P0-3 결정적 유동성 selector

`scripts/krb1_p0_liquidity_selector.py`는 주문수명 측정 후보를 위한 read-only
selector다. 정상 전략의 `DV20 → M60 → top2` selector와 별개이며 코드를 공유하지
않는다.

## 실행

프로덕션 데이터베이스를 명시하고, 완료 데이터로 사용할 세션과 측정 대상 세션을
고정한다.

```bash
ENV_FILE=.env.prod uv run python -m scripts.krb1_p0_liquidity_selector \
  --as-of-session 2026-07-29 \
  --target-session 2026-07-30
```

`--target-session`을 생략하면 XKRX calendar의 다음 세션을 결정적으로 사용한다.

## 종료 코드

- `0`: KOSPI 1종목과 KOSDAQ 1종목의 모든 gate가 `proven`
- `2`: 하나 이상의 gate가 `unprovable`; `selected_candidates=[]`

exit `2`는 결손 데이터에서 정상적인 fail-close 결과다. 수동 종목, volume-rank,
generic screener로 대체하지 않는다.

## 증거 계약

JSON은 universe/완료세션 coverage, metadata, 기준가 예외, 완료 종가, 원시 quote
timestamp gate를 각각 `proven` 또는 `unprovable`로 출력한다. quote timestamp는 KIS
원시 `stck_bsop_date`와 `stck_cntg_hour`만 인정한다. wrapper의 `price_as_of`나
`price_freshness`는 gate를 통과시키지 못한다.

지정가는 정수 연산만 사용한다.

```text
raw = (85 * completed_close) // 100
price = (raw // tick) * tick
```

CLI의 DB transaction은 `REPEATABLE READ READ ONLY`이며 외부 호출도 KIS quotation GET
두 종류뿐이다. 주문 preview/place/cancel, DB write, journal append, scheduler 연결은
없다.

현재 repository에는 07-30 기준가 예외를 전수 증명하는 권위 source가 배선되어 있지
않다. source가 없는 동안 해당 gate는 `unprovable`이며 selector는 후보를 반환하지
않는다.
