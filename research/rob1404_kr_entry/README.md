# ROB-1404 Part 2A — KR 진입 방식 공통 시계 비교 (연구 전용·주문 0)

분석 전용. 런타임·정책·프롬프트 변경 0 (`git diff --stat origin/main -- app config alembic` 빈 출력). 브로커·운영 DB·localhost:5432 미접속. 새 shadow 실험·정책 변경·사전등록 봉인이 아니다. ROB-1391(US 실험) 코드·manifest 는 import 하지 않는다.

```bash
# 재생성 (가격 캐시만 사용, 관측 29–268초 — 장비 부하에 따라)
uv run python -m research.rob1404_kr_entry.run \
  --inputs <b1383 inputs dir> --v1-results <rob1383 results dir> --offline
```

- 입력: ROB-1383 v1 과 같은 2026-10-16 스냅샷(+10-17 `proposals_buy.csv`). 버전표는 `results/versions_inputs.csv`(sha256·행수·수집 시각).
- 코드: v1 `research/rob1383_buy_drought` 를 **import 해서 재사용**한다(`core.leg_filled`/`tick_floor`/`forward_positions`, `q2_q3_orders.reference_price`/`lifetime_low`/`order_session_positions`, `gates.assign_episodes`, `stats.bootstrap_mean_ci`, `prices.PriceStore`). 복제한 판정 로직은 없다.
- 가격: v1 캐시(`.cache/prices`, gitignored)를 그대로 복사해 쓰고, 새로 받은 것은 KOSDAQ 지수 `^KQ11`(일봉·60분봉)과 yfinance 업종 라벨(`sectors_kr.json`)뿐이다. 받은 시각은 `results/run_meta.json`·`versions_price_cache.csv` 에 남는다. v1 일봉은 2026-10-15 까지다(10-16 봉 없음).
- 🔴 행 단위 결과(`results/`)는 커밋하지 않는다(주문번호·예산). at-integrator 가 Drive(비공개)로 옮긴다.

## 대상(원후보)

v1 Q2/Q3 의 KR 모집단 = Toss KR 지정가 매수 중 미체결로 끝난 **79건**(현금 파킹 심볼 459580 의 2건 제외; `population_ledger.csv`). 부모 주문 = v1 `gates.assign_episodes`(같은 종목, 직전 주문 후 5세션 이내, 첫 앵커 ±5%) → **37 부모**. 주 단위는 부모(각 사슬의 첫 주문), 주문 단위는 부모 군집으로 보정한 민감도다.

- `nxt_only` = 주문 수명이 09:00–15:30 KST 정규장 **시계 창**과 겹치지 않음(타임스탬프로 판정). v1 의 봉 기반 판정 16건 중 6건은 10-16 정규장 주문(그날 일봉이 캐시에 없음)이라 여기서는 NXT 전용이 아니다 → 진짜 NXT 시간대 주문은 10건.
- 정책 epoch = 신호 시각에 이미 쓰이던 정책 버전(`forecasts_all.json` 의 버전별 최초 스탬프 시각) → 운영자 표기 이정표(S176 09-07 · §664 09-24 · #876/#877 09-28 · 2026-09-30.1)로 묶음(`policy_epochs.csv`).

## 공통 시계

- 신호 시각 `t` = `ordered_at`(세션 판단 *이후*의 전송 시각 — 판단보다 늦으므로 미래 정보가 아니다).
- `D0` = `t` 이후 처음 마감하는 KRX 정규장 세션(진행 중이면 그 세션, 장후·장전이면 다음 세션). D0·D0+h·재제출 3세션·체결 후 +k 는 모두 **공통 KRX 세션 달력(^KS11 일봉 날짜)** 에서 센다 — 종목 봉이 그 날짜에 없으면 데이터 안이면 `data_missing`, 마지막 캐시 봉 뒤면 `immature`(다음 행으로 밀지 않는다). 모든 방식은 **`D0+h` 종가(h = 5, 20)** 에서 청산한 것으로 같은 시각에 평가한다.
- 예산 `B = q × P_ref` — 승인 정수 주수 `q` × 신호 시점 기준가(v1 `reference_price`: `t` 를 포함한 60분봉 시가, 장 밖이면 다음 60분봉 시가). 한 후보의 모든 방식에 같은 `B` 를 쓴다. 미체결 주식은 현금(0%).

## 방식

| 방식 | 정의 |
|---|---|
| `cash` | 주문 없음 |
| `anchor_actual` | 실제 주문 그대로. 브로커가 미체결로 보고(79/79) → 증거상 미체결 |
| `nep` (다음 실행 가능 가격) | `t` 이후 **시작하는** 첫 60분봉(정확히 다음 정시 봉) 시가 — 캐시에 그 봉이 없으면 결측이며 더 늦은 봉으로 대체하지 않는다. 마지막 시간봉(14:00–15:30) 안이고 15:20 전이면 그날 종가 단일가(아직 체결 전 가격), 그 뒤면 다음 세션 시가. `t` 를 포함한 봉의 시가·저가는 쓰지 않는다 |
| `next_day_open` | `t` 의 KST 날짜 다음 거래일 시가(장후 신호면 `nep` 와 같다) |
| `split_int` | v1 Q2 20/80 의 **정수 주** 판: `floor(0.2q)` 주를 `tick_floor(기준가×0.995)` 에, 나머지는 실제 앵커(미체결). `q < 5` 면 −0.5% 다리가 아예 없다 |
| `split_frac` | v1 Q2 그대로(예산 20% 소수 주) — 대조용 |
| `resubmit3` | v1 Q3: 만료 후 다음 3 정규 세션에 원 앵커 재제출, 갭다운이면 시가 체결 |

체결 상태는 3가지다: `filled`(주문 이후로 정확히 측정된 저가 — 수명 안에 통째로 든 세션의 일봉 저가 또는 주문 이후 시작한 60분봉 — 에서 1틱 trade-through; 시간봉 없는 부분 세션의 일봉 저가는 주문 전 가격일 수 있어 증거가 아니다), `unfilled`(수명이 겹치는 **모든 달력 세션**에 봉이 있고, 시간봉이 그 세션을 빠짐없이 덮으면 낙관 창 저가, 아니면 일봉 저가가 지정가 위), 그 사이(터치만·봉 없음·NaN·NXT 가격 없음)는 **`unknown`** — 0수익(현금)으로 바꾸지 않는다. 주 추정은 `ok` 행만, `unknown` 은 개수와 두 경계값(미체결/체결 가정)을 따로 낸다. `D0+h` 봉이 없으면 `immature` 로 빠진다.

## 비용

- 기본(`base_c0.015_t0.20_s0`): 위탁수수료 편도 0.015%, 매도 거래세 0.20%(ETF 0), 슬리피지 0. 수수료·세율은 **계좌 체결 실측**(`toss_orders.json` KR 체결의 `commission`/`tax` ÷ 체결금액: 체결 29건 수수료 0.0126–0.0150%, 주식 매도 26건 세율 0.1978–0.2000%(원 단위 절사), ETF 매도 1건 0).
- 미확인 슬리피지와 다른 수수료·세율은 민감도 격자(`cost_sensitivity.csv`): 수수료 {0, 0.015, 0.25}% × 세율 {0.20, 0.15}% × 슬리피지 {0, 1틱, 0.1%}. 슬리피지는 시장성 진입과 모든 청산에 적용.
- 부분체결: 시장성 진입은 전량 체결 가정(소액·대형주 위주; 호가 거래량 자료 없음). 지정가는 3상태로만 판정.

## 벤치마크 조정

`y − Σ(다리 금액/B × 같은 구간의 벤치 수익)`. 시장 = `.KS` 는 KOSPI(^KS11), `.KQ` 는 KOSDAQ(^KQ11). 업종 = yfinance `sector` 가 같은 KR 캐시 종목(자기 제외)의 동일가중 평균, 가격 있는 동종 3개 미만이면 결측(0 아님). 업종 라벨 커버리지: 후보 30/33 종목, 유니버스 114/142.

## 불확실성

평균의 95% 구간을 iid·종목 군집·부모 군집(주문 단위)·**날짜 블록**(D0 세션 달력의 10세션 이동 블록, 빈 날 포함 — 같은 날 공통 충격과 보유기간 겹침 일부 보존)으로 계산하고, 블록 5·10·20 세션까지 포함한 **모든 의존 보존 구간의 합집합(가장 낮은 하한 ~ 가장 높은 상한)** 을 대표 구간으로 쓴다(판정이 어느 의존 가정에서도 버텨야 한다). 블록별 값은 `block_sensitivity.csv`. 최소 검출 효과(단측 5%, 검정력 80%) = 2.486 × 그중 가장 큰 SE, 설계효과 = SE²/SE_iid². 구간이 0 을 포함하면 문구는 항상 “판별 불가”다(`inference.verdict`).

## 표

`summary.csv`(방식×시계×범위×측정) · `paired.csv`(같은 후보의 방식 차이) · `mde.csv` · `cost_sensitivity.csv` · `block_sensitivity.csv` · `markouts_summary.csv`(체결 후 +5/+20, day 0 = 체결 세션) · `policy_epochs.csv` · `parents.csv` · `v1_reproduction.csv`(v1 Q2/Q3 상세 재실행 ↔ 저장본 셀 대조) · `canonical_reconcile.csv`(정본 수치 재계산) · `explore_2b_*.csv`(2B 초안 실행 가능성 계수 — **탐색**, 수익 아님).

## 한계

1. 후보 37 부모·성숙 30(+5)/20(+20) — 탐색 표본이다. 외표본 검증을 대신하지 못한다.
2. 표본 기간에 KOSPI 가 크게 하락했다(09-23 → 10-15 약 −22%). 절대수익은 이 국면에 지배된다. 종목 선정·진입 방식·시장 국면의 인과 기여는 분리하지 않는다(못 한다).
3. KRX 정규장 가격만 있다. NXT 저가로 체결시키지 않으며, NXT 전용 주문의 −0.5% 다리는 `unknown` 이다.
4. 시장성 진입 체결가는 60분봉 시가/종가 단일가 근사다. 지정가 체결 시각은 처음 터치한 60분봉 시작으로 근사(벤치 기준점).
5. 업종 벤치는 공개 라벨·후보 유니버스 내 동종 평균이라 거칠다.

## 코드

`population.py`(원후보·부모·epoch) · `clock.py`(D0·다음 실행 가능 가격) · `methods.py`(방식 다리·3상태) · `outcomes.py`(비용·벤치·마크아웃) · `inference.py`(군집·블록 부트스트랩·MDE·판정 문구) · `reconcile.py`(v1 재현·정본 수치) · `explore_2b.py`(2B 탐색 계수) · `sectors.py` · `versions.py` · `run.py`.
테스트: `tests/research/test_rob1404_kr_entry.py` (DB·네트워크 없음, `ci_shards/shard-1.txt` 등록).
