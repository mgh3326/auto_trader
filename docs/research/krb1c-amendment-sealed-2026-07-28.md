# KR-B1c C_stress_cap reducer 자식 amendment 봉인

- 봉인일: 2026-07-28
- 연구 ID: `KRB1-CSM60-H5-v1`
- amendment ID: `KRB1C-CSTRESS-REDUCER-v1`
- 상태: `SEALED`
- 운영자 승인: 완료
- canonical JSON: `krb1c-amendment-canonical-2026-07-28.json`
- canonical JSON SHA-256: `d5da5edd6b49fb759b781c13f627e21a84667ced2be7cf03624a40bb813be389`

## 부모 봉인과의 관계

이 문서는 SHA-256
`d5e1246b2072ad227d924e059091e27fa49719e42a5bfba651ae8f2bced9d6f1`인
`krb1-combined-canonical-2026-07-28.json`의 **append-only 자식 amendment**다.
부모 canonical을 교체·수정·재직렬화하지 않는다. 부모 §6 전체와 기존
`N_stress` 비용 스택, `T_i=tick_ceil(L_i×(1+C_stress_cap))`, 가격 범위
`5,000≤P≤400,000`, 나머지 수치·임계·상수는 모두 불변이다.

신설 위치는 부모 §6.2 바로 아래의 `§6.2.1 C_stress_cap 결정적 reducer`다. 향후
execution canonical은 이 부모 SHA와 이 amendment 전문·SHA, sealed P0 입력,
reducer 결과를 append-only로 결합해야 한다.

## canonical JSON 해시 규칙

SHA-256 대상은 `krb1c-amendment-canonical-2026-07-28.json`의 **파일 전체 바이트**다.
JSON은 UTF-8, object key lexicographic order, `separators=(",",":")`,
`ensure_ascii=False`, `allow_nan=False`로 직렬화했고 파일 끝 newline은 두지 않았다.
정규화 전처리 없이 다음 명령으로 재현한다.

```bash
shasum -a 256 krb1c-amendment-canonical-2026-07-28.json
```

## verbatim 이관과 이력 보존

`gptpro-krb1c-response-2026-07-28.md`의 `## 응답 원문 (verbatim)` 이하를
canonical `normative_sections[id="6.2.1"].text`에 자구 그대로 이관했다. 요약·의역·
재작성·수치 변경을 하지 않았다.

부모 선례와 같은 방식으로 canonical `amendment.amendment_source_text`에 다음 두
이력을 함께 보존했다.

1. 변경 전 부모 canonical §6 원문 전체.
2. 변경 후 자식 amendment에 신설하는 reducer 조문 원문 전체.

따라서 신설 전 상태를 삭제하지 않았고, 부모를 덮어쓰는 replacement가 아니라
`replacement=false`, `append_only=true`인 자식 변경으로 봉인했다.

## C_stress_cap 비용 입력 binding

운영자 확정과 조문에 따라 비용 입력은 다음과 같이 고정한다.

| 필드 | binding 값 |
|---|---|
| `broker_id` | `kiwoom` |
| `account_product_id` | `KIWOOM_DOMESTIC_CASH_STOCK` |
| `order_channel_id` | `KIWOOM_OPENAPI_KRX` |
| `cost_basis` | `REAL_TRADING_TARIFF` |
| 매수 commission | `0.015%`, `rate_e12=150000000` |
| 매도 commission | `0.015%`, `rate_e12=150000000` |
| `mock_cost_relation` | `DIFFERENT` |
| mock 0.35% 용도 | 병기와 reconciliation만; reducer numeric 입력 금지 |
| 권위 | 키움 공식 문서 snapshot + SHA-256 |

`0.015%`는 매수·매도 commission 입력이다. `C_stress_cap` 산출값 자체가 아니다.
최종 cap은 매도세와 진입·청산 각 1틱을 함께 넣어 §6.2.1 exact-rational reducer가
산출한다.

이 binding은 임의 선택이 아니다. `REAL_TRADING_TARIFF` 고정과 mock 표시요율 사용
금지는 §2 입력 계약이 강제하고, `mock_cost_relation=DIFFERENT`여도 실거래표를 쓰라는
규칙 및 §8.4가 같은 결론을 강제한다. `supports_live=False`는 비용표 식별 장애가
아니다. 이 amendment에서는 키움 공식 국내주식 핵심설명서 snapshot의
SHA-256 `8fd195fe7426ab66afca2ff131a08153afd114a2c0912b32e0924ba2434095af`가
권위다.

## 함께 hash한 보완 5건

### ① §8.6 해석 각주

> **§8.6 해석 각주 — “변경”의 기준시점.** §8.6의 “dry-count 시작 후 변경”은
> dry-count 시작 후 새로 공표·인지되었거나 binding P0-2 입력 또는 P0-1 tick table에
> 포함되지 않은 정책환경 변경을 뜻한다. §2.13에 따라 reducer 실행 전에 공표되어
> coverage 내 전후 `effective_from/to` record로 수록되고, §3.4의 기간별 max와
> execution canonical SHA에 반영된 예정 발효는 §8.6의 변경으로 보지 않으며 그
> 발효만으로 `NOT_DISCRIMINABLE` 종료하지 않는다. 사전 공표되었더라도 해당 record·
> tick table·SHA에 반영되지 않은 변경은 이 예외의 대상이 아니며 P0-2 FAIL로 처리한다.

이 각주는 사전 반영된 예정 발효와 dry-count 뒤의 신규·누락 변경을 구분한다. 누락이나
사후변경을 면책하지 않는다.

### ③ §7.7 참조 구현 순서

순서는 다음과 같이 고정한다.

1. 이 child amendment 선봉인.
2. float 없는 exact-rational 전수열거 참조 구현 작성.
3. 참조 구현을 import·복사하지 않는 독립 재현기 작성.
4. 동일 sealed input의 전 후보 fraction·witness·bp·target 검산 전수 대조.
5. 전수 일치와 독립성 검토가 PASS인 뒤에만 P0-2 completion hash 생성.

구현·독립 재현 미완료 또는 단 하나의 불일치라도 있으면 completion hash를 만들지
않는다.

### ⓐ §2.15 해석 각주

`probe_reconciliation_status=PASS`는 mock 체결가·수수료·세금·반올림을 적용했을 때
관측 cash delta가 **mock 표시요율로 일의적으로 설명됨**을 뜻한다. PASS는 mock 비용과
실거래 비용이 같다는 뜻이 아니고 mock 요율을 reducer numeric 입력으로 쓸 권한도
부여하지 않는다. 따라서 `PASS`와 `mock_cost_relation=DIFFERENT`는 동시에 성립한다.

### ⓑ mock NAV 부수효과

`kiwoom_mock` 현금·NAV에는 매수·매도 각 leg의 mock 수수료 `0.35%`가 실제 차감된다.
실거래 `0.015%` 가정보다 NAV가 더 빠르게 감소하므로 부모 §2.8의
`post-fill 노출≤NAV 50%` cap이 약간 더 보수적으로 작동한다. 허용 노출을 늘리지 않는
무해 방향의 편향이다. journal·reconciliation에 병기하며, 이를 mock 요율의 cap 편입이나
장부-채점 결합 근거로 사용하지 않는다.

### ⓒ 매도세 component 분해

binding `sell_tax_components`는 키움 공식 안내에 따라 다음과 같이 기록한다.

| 시장 | component | 세율 | `rate_e12` |
|---|---|---:|---:|
| KOSPI | `KOSPI_SECURITIES_TRANSACTION_TAX` | 0.05% | 500000000 |
| KOSPI | `KOSPI_RURAL_SPECIAL_TAX` | 0.15% | 1500000000 |
| KOSDAQ | `KOSDAQ_SECURITIES_TRANSACTION_TAX` | 0.20% | 2000000000 |

양 시장 매도세 합계는 각각 `0.20%`다. 오늘 `kiwoom_mock` probe도 33,400원 매도에서
66원, 실효 `0.1976%`로 명목 `0.20%`와 일치했다. probe 원보고서의 0.15%/0.05%
component 명칭 역산은 증거 이력으로 그대로 보존하지만, binding 명칭과 귀속은 키움
공식 안내의 KOSPI `0.05% + 0.15%`가 지배한다.

## 오늘 실측 증거

원본:
`/Users/mgh3326/services/auto_trader-operator/reports/kiwoom-mock-fee-probe-2026-07-28.md`

SHA-256:
`350341202f80df97cec84bd266bc501665715057fa04005ffd4a36179a706092`

canonical에는 원보고서 전문과 다음 핵심값을 함께 넣었다.

```text
매수 33,350  수수료 110원  실효 0.3298%
매도 33,400  (지정가 33,300 → 호가 크로스 유리체결)  포지션 flat
mock 요율 0.35%(10원 절사) · 매도세 0.20%
실거래 0.015%  → mock은 약 23배
cash delta 오차 0원 → probe_reconciliation_status=PASS
mock_cost_relation=DIFFERENT
```

mock probe 값은 reconciliation 증거일 뿐 numeric reducer 입력이 아니다.

## 검증 판정 반영

| 검증 | 판정 | 봉인 반영 |
|---|---|---|
| 수학 ① §4.7·현행 KRX 표·`E_m` | `FAIL — 범위 한정` | §4.7 정리·open-ended·종료·전단사는 PASS. 응답 상단 Fable 노트의 구 표 예시 `9,990→10,000: tick 10→50`와 `E_m≈2,400`만 binding 근거에서 제외 |
| 수학 ② §6.9 | `PASS — 대수적 필연` | 정상 실패 케이스 없음; 실패는 표·요율·반올림·구현 결함 신호 |
| 운영 ③ §8.6 | 각주 필수 | 위 ① 각주를 canonical에 hash |
| 운영 ④ 공표 세율 변경 | PASS | 2027-07-31 외곽까지 공표 변경 없음 판정 보존; execution canonical 직전 재조회 의무 유지 |
| 운영 ⑤ 구현 순서 | 구현 선행 필수 | amendment 선봉인 뒤 구현·독립 재현, 그 다음에만 completion hash |

수학 ①의 FAIL은 reducer 조문 본문의 반례가 아니다. 응답 상단의 비규범적 Fable 노트를
현행 KRX 표 검증 증거로 사용할 수 없다는 판정이다. 조문은 자구 수정하지 않았다.

검증 보고서들은 운영자 annotation 전 응답 SHA-256
`ede1f3659f8f55e2affbf9d3a0de70e19feabc8bfbc38ca6a2fa5e0a01a18e49`를
대상으로 기록했다. 현재 정본은 운영자 결정 블록이 추가된 SHA-256
`a429f3b517d9525fb6bdbaf8c40acb791d03418077acc4a3e6b2a59c6373b9b0`이며,
현재 정본의 verbatim 조문을 이관했다.

## 이 자식 amendment의 정당성 심사

심사 항목:

- reducer 조문이 정본의 응답 원문에서 verbatim 이관되었고 수치·임계·상수가 변경되지
  않았는가
- 부모 canonical SHA-256이 일치하고 부모를 덮어쓰지 않는 append-only 자식 관계인가
- `cost_basis=REAL_TRADING_TARIFF`가 §2.7·§2.16·§8.4의 강제인지 운영자의 임의
  선택인지
- mock `0.35%`를 cap numeric 입력에서 배제하고 `DIFFERENT`·reconciliation 증거로만
  보존했는가
- 키움 실거래 `0.015%`가 공식 snapshot+SHA로 식별되고 `rate_e12=150000000`으로
  정확히 환산되었는가
- §8.6 각주가 §2.13·§3.4와의 양립 독해를 고정하면서 누락·사후변경을 면책하지 않는가
- §2.15 PASS 각주가 `PASS`와 `DIFFERENT`의 동시 성립을 보존하는가
- mock NAV 차감의 NAV 50% cap 부수효과를 무해 방향으로 공개했는가
- 매도세 `0.20%`를 KOSPI `0.05%+0.15%` 및 KOSDAQ `0.20%` component로 공식
  근거에 맞게 분해했는가
- amendment 선봉인 → 참조 구현 → 독립 재현 → P0-2 completion hash 순서가
  강제되었는가
- 수학 검증 ① FAIL의 범위와 ② PASS를 정확히 분리했는가
- 봉인 과정에서 production DB write·broker mutation·배포·실주문이 없었는가

판정: `PASS / CLOSED`.

`REAL_TRADING_TARIFF`를 택한 것은 임의 선택이 아니라 조문 강제다. mock probe는 cash
delta reconciliation을 통과했지만 실거래표와 `DIFFERENT`이므로 mock 요율을 cap에 넣을
수 없다. 부모 SHA·정본 이관·보완 5건·공식 snapshot·component 분해·후속 순서를 모두
canonical에 hash했고, 이 봉인 과정은 문서·hash·git 작업만 수행했다.

## 원문·검증 증거 SHA-256 manifest

| artifact | SHA-256 |
|---|---|
| `krb1-combined-canonical-2026-07-28.json` | `d5e1246b2072ad227d924e059091e27fa49719e42a5bfba651ae8f2bced9d6f1` |
| `gptpro-krb1c-response-2026-07-28.md` | `a429f3b517d9525fb6bdbaf8c40acb791d03418077acc4a3e6b2a59c6373b9b0` |
| `krb1c-math-verify-2026-07-28.md` | `7382f94125c5a6f1fbcb143470734a7e0987e9c0d04cd64d88741739b6dcc059` |
| `krb1c-ops-verify-2026-07-28.md` | `36675cbdae349799a060081febe87f280d6603ee5749fde4d4ce20cf3bf0c4cc` |
| `krb1c-reducer-decision-2026-07-28.md` | `f0714fa320715825971e09e038126d84343a23fde856bd6c255b1d38b29bd2f0` |
| `krb1b-runtime-amendment-2026-07-28.md` | `bfb8e4099a80e0ea9ac87d5b0c260edd2d1f0286b59f53ff806e5c64e9bb15d8` |
| `kiwoom-mock-fee-probe-2026-07-28.md` | `350341202f80df97cec84bd266bc501665715057fa04005ffd4a36179a706092` |
| `kiwoom-domestic-stock-key-information-2026-06-12.pdf` | `8fd195fe7426ab66afca2ff131a08153afd114a2c0912b32e0924ba2434095af` |

## 다음 단계

이 봉인은 reducer 조문과 해석·입력 binding의 선봉인이다. numeric reducer는 아직
실행하지 않았고 `C_stress_cap`, `C_raw`, witness, bp 및 P0-2 completion hash는
아직 없다.

다음 단계는 §7.7 순서에 따라 참조 구현과 독립 재현을 완성하고 sealed P0-1/P0-2
입력으로 전수 일치를 확인하는 것이다. 그 PASS 뒤에만 P0-2 completion hash와
execution canonical을 만들 수 있다. 그 전에는 dry-count forecast·preview와 outcome
접근이 금지된다.
