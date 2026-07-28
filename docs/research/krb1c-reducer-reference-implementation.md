# KR-B1c §7.7 reducer 참조 구현과 독립 재현

상태: `REFERENCE_IMPLEMENTATION PASS / P0-2 COMPLETION NOT CREATED`

이 구현은 부모 canonical
`d5e1246b2072ad227d924e059091e27fa49719e42a5bfba651ae8f2bced9d6f1`과
자식 amendment
`d5da5edd6b49fb759b781c13f627e21a84667ced2be7cf03624a40bb813be389`를
수정하지 않는다. 아래 수치 실행은 `tests/fixtures/krb1_c_stress/`의 명시적
테스트 fixture를 사용한 참조 재현이며 실제 P0-1/P0-2 completion으로 주장하지 않는다.

## 구현 위치

- `research/krb1_c_stress_reducer/model.py`: P0-2 비용 입력, P0-1 tick-table
  transport, 기간 coverage, authority overlap/gap, component uniqueness의 fail-closed
  검증
- `research/krb1_c_stress_reducer/reducer.py`: §4.5 `E_m` 반복, §4.6~4.8
  exit witness, §5 나눗셈형 비용률, §6 시장 결합·bp ceiling·target 자기검산,
  게시 artifact 3종
- `scripts/krb1_c_stress_reducer.py`: 참조 구현 CLI
- `scripts/krb1_c_stress_independent_verify.py`: 참조 package를 import하지 않는
  독립 재현기
- `tests/research/test_krb1_c_stress_reducer.py`: 계약, 경계, 결정성, 독립 재현,
  float 금지 테스트

참조 구현은 비용 JSON에 조문이 열거하지 않은 키가 있으면 거부한다. 따라서
`mock_display_rate_e12` 같은 mock 수치 필드는 reducer numeric 입력에 들어갈 수 없다.
현재 fixture의 실거래 commission은 매수·매도 각각 `rate_e12=150000000`이며,
매도세는 KOSPI
`KOSPI_SECURITIES_TRANSACTION_TAX=500000000` +
`KOSPI_RURAL_SPECIAL_TAX=1500000000`, KOSDAQ
`KOSDAQ_SECURITIES_TRANSACTION_TAX=2000000000`으로 기록한다.

## exact rational 보장

참조 구현의 모든 비율·multiplier·`c_m(P)`·cap·target 곱은
`fractions.Fraction` 또는 정수다. JSON parser는 숫자 token에 소수점이 있으면
즉시 거부한다. 독립 재현기는 `Fraction`도 참조 구현도 사용하지 않고
`(numerator, denominator)` 정수쌍을 `gcd`로 기약화하며 비교는 교차곱으로 한다.

테스트는 다음을 함께 확인한다.

- 양쪽 source AST에 float literal, `float(...)`, `Decimal`/NumPy import가 없음
- 참조 core 실행 중 `builtins.float`를 예외 함수로 바꿔도 전수 실행 성공
- 모든 8,002개 row의 fraction·exit witness·target을 독립 정수 구현이 일치시킴

`c_stress_candidates.csv`의 17개 필드는 조문이 필드 이름을 열거하지 않은 부분에
대한 게시 schema다.

1. `market`
2. `entry_price`
3. `entry_tick`
4. `rho_entry_num`
5. `rho_entry_den`
6. `exit_witness_price`
7. `exit_witness_tick`
8. `rho_exit_num`
9. `rho_exit_den`
10. `entry_multiplier_num`
11. `entry_multiplier_den`
12. `exit_multiplier_num`
13. `exit_multiplier_den`
14. `c_num`
15. `c_den`
16. `target_price`
17. `target_check_passed`

## 사용한 호가표와 universe 경계

테스트 fixture는 KOSPI·KOSDAQ의 2023+ **표준 주권** 표만 사용한다.

| 가격 구간 | tick |
|---|---:|
| `[0, 2,000)` | 1 |
| `[2,000, 5,000)` | 5 |
| `[5,000, 20,000)` | 10 |
| `[20,000, 50,000)` | 50 |
| `[50,000, 200,000)` | 100 |
| `[200,000, 500,000)` | 500 |
| `[500,000, ∞)` | 1,000 |

이 표에서는 `E_m`이 시장당 4,001개다. “약 2,400개”는 봉인 수학 검증 보고서가
구 표의 흔적으로 판정한 값이며 구현 상수로 사용하지 않았다.

현재 `kr_symbol_universe`에는 `security_type`과 `is_common_share`가 있지만 nullable이고,
부모 §2.3의 유니버스 문장 자체는 ETF·ETN·ELW를 명시적으로 제외하지 않는다.
따라서 실제 P0-1이 특수 상품을 포함하면서 표준 주권 표 하나만 `COMPLETE`라고
선언하면 안 된다. 입력 transport의 `symbol_table_mapping_status`가 `COMPLETE`가
아니면 reducer는 실행을 거부한다. 실제 completion 전에는 다음 중 봉인된 P0-1
증거가 하나를 확정해야 한다.

- 허용 universe가 표준 주권만임을 증명하고 표준 주권 표를 사용한다.
- ETF·ETN·ELW를 포함한다면 각 symbol의 별도표 매핑을 완결한다.

§6.2.1의 계산 차원이 시장 `m` 하나뿐이므로 한 시장 안에서 서로 다른 표를 혼합할
때의 결합 규칙은 조문에 없다. 이 경우 구현이 임의로 상품 차원을 추가하지 않고
P0-1 FAIL로 닫는다.

`krb1.reference.tick_tables.v1`은 봉인 조문에 없던 P0-1 파일 형식을 숫자 정책으로
가장하지 않기 위해 `reference` namespace로 둔 로컬 transport다. 호가표 자체와
symbol mapping의 권위는 이 파일 이름이 아니라 sealed P0-1 입력과 그 SHA에 있다.

## 독립 재현 출력

2026-07-28 fixture 실행 결과:

```text
arithmetic=fractions.Fraction+integer
float_used=false
KOSPI candidates=4001 first=5000 last=400000 first_after_cap=400500
KOSDAQ candidates=4001 first=5000 last=400000 first_after_cap=400500
C_raw=146/19907
witness=KOSPI/P=20000/Q=20000
C_stress_cap_bp=74
C_stress_cap_decimal=0.0074
all_target_checks_passed=true
```

독립 재현:

```text
arithmetic=integer cross-products+gcd
candidate_rows_compared=8002
fraction_rows_matched=8002
witness_rows_matched=8002
target_rows_matched=8002
status=PASS
```

경계 실행은 두 시장 모두 다음을 확인했다.

```text
lower=2000   below/at/above tick=1/5/5
lower=5000   below/at/above tick=5/10/10
lower=20000  below/at/above tick=10/50/50
lower=50000  below/at/above tick=50/100/100
lower=200000 below/at/above tick=100/500/500
lower=500000 below/at/above tick=500/1000/1000
open_ended_probe=1734567 tick=1000
```

동일 입력을 두 번 실행한 byte SHA-256:

```text
p0_2_cost_inputs.normalized.json
7ec5b2bb8e5b50c4c617a35e516b9a10780a757881b79a4edd9bade2268b1e73

c_stress_candidates.csv
5b067554dda572ed5352432ec6bfafe62bbd7cd1044a5dbf120ed07dd0e18de7

c_stress_reducer_result.json
f71ffa18d844e36b602198cff76ceb8afed68fa1a6fe5a6562c4a7fb1c539223

independent verification JSON
b1025f8a35a013354fea4fd51cfc17d321d0d316b4edce2fe7e20250a973aabd
```

각 파일은 1회차와 2회차가 `cmp` 및 SHA-256 모두 일치했다.

## 실행 명령

```bash
uv run python scripts/krb1_c_stress_reducer.py \
  --cost-input tests/fixtures/krb1_c_stress/p0_2_real_tariff_cost_inputs.json \
  --tick-input tests/fixtures/krb1_c_stress/p0_1_standard_stock_tick_tables.json \
  --parent-canonical ~/work/herdr-inbox/krb1-combined-canonical-2026-07-28.json \
  --amendment-canonical ~/work/herdr-inbox/krb1c-amendment-canonical-2026-07-28.json \
  --output-dir /tmp/krb1c-output \
  --repo-root "$PWD"

uv run python scripts/krb1_c_stress_independent_verify.py \
  --cost-input tests/fixtures/krb1_c_stress/p0_2_real_tariff_cost_inputs.json \
  --tick-input tests/fixtures/krb1_c_stress/p0_1_standard_stock_tick_tables.json \
  --candidates /tmp/krb1c-output/c_stress_candidates.csv \
  --result /tmp/krb1c-output/c_stress_reducer_result.json \
  --repo-root "$PWD" \
  --verification-output /tmp/krb1c-output/independent-verification.json
```

## 조문에서 닫히지 않은 지점

- 게시 CSV는 “기약분수 필드 17종”이라고만 하고 17개 필드명을 열거하지 않는다.
  위 schema는 수치 계산을 바꾸지 않는 감사용 표현으로 명시했다.
- P0-1 tick 함수의 파일 schema와 SHA 결합 형식은 정하지 않았다. 구현은 별도
  reference transport와 normalized SHA를 게시한다.
- 실제 universe에 서로 다른 상품 호가표가 섞일 때 reducer 결합 차원이 없다.
  임의 확장하지 않고 mapping 불완전으로 fail-closed 한다.
- 독립 재현 PASS 뒤 만드는 `P0-2 completion hash`의 preimage와 직렬화 형식이
  조문에 없다. 또한 저장소에 실제 sealed P0-1/P0-2 입력이 아직 없다. 따라서 이
  fixture 실행은 completion hash를 만들지 않으며 result도
  `AWAITING_INDEPENDENT_REPRODUCTION`, 독립 보고도
  `p0_2_completion_hash_created=false`로 남긴다.

실제 P0 입력이 봉인되고 위 모호성이 권위 문서로 닫히기 전에는 이 참조 PASS를
P0-2 completion으로 승격하지 않는다.
