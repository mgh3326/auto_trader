# screener_bakeoff — 스크리너 소스 베이크오프 (§140차)

**read-only 연구 + 관측 전용 픽 로깅.** 베이크오프 패키지는 SELECT 만 한다.
주문·워치·제안·정책 mutation 0, 브로커 호출 0. 라이브 fanout 픽 로깅은
`SCREENER_PICK_LOG_ENABLED`(기본 false) 뒤의 바깥 관측 레코더이며 fanout 모듈
자체는 계속 no-write 다.

**이 결과는 정책·가중치 변경 근거가 아니다.** 세 핵심 주장
(가치/수익성 우세, 현행 `tv_rsi45` 중위권, 거래대금·등락상위·거래량급증·쌍끌이
열위)은 r2 적대검증에서 **전부 OOS 근거 사용 불가**로 판정됐다.

---

## §0 전환 사유 (r2 → 전향)

이 베이크오프는 적대검증 **2라운드 연속 실패**했다. 두 번 다 같은 축 —
**연구가 재구성한 소스 정의가 프로덕션과 다르다.**

| 라운드 | 실패 |
|---|---|
| r1 | `tv_rsi45` · `us.high_yield_value` · `kr.double_buy` 3건 전부 |
| r2 | `tv_rsi45` 여전히 틀림(라이브는 소스당 top-10, 연구는 top-100 게이트 후 top-10). r2 가 구조적 수리로 넣은 parity 테스트는 기대값을 **라이브 10이 아니라 100**으로 박았다. 구현과 같은 오독에서 파생된 테스트는 그 오독을 잡지 못한다. `us.high_yield_value` 강등 근거("yahoo=0 이라 재구성 불가")는 프로덕션 쿼리에 `source` 필터가 없어 **거짓**이었다. |

비교 기준이던 `tv_rsi45`(현행 주력)가 가장 재현 불가능한 소스다
(라이브 HTTP 유니버스 + TradingView RSI + 소스당 top-10).

**운영자 결정 (2026-08-23): 역사를 재구성하지 않는다. 지금부터 라이브가
실제로 고른 픽을 기록해 전향 채점한다.** 2~3개월이 걸려도 정직한 답이 늦은
답보다 낫다. 이 실패를 숨기지 않는다 — 다음 사람이 같은 함정에 빠지지 않게.

`tv_rsi45` **비교 라벨·표·문언·parity 테스트는 전면 철회**했다. 철회 정본은
`spec.py`의 `WITHDRAWN_SOURCES`이며, 기본 runner는 해당 builder를 만들지 않고
aggregate/date-level/bootstrap/report도 해당 source ID를 산출물에서 배제한다.
KR/US의 3-인자 역사 builder와 사문화된 RSI 배선은 삭제했다. 현재 남은 역사
builder는 2-인자 `crypto.tv_rsi45`뿐이며, `--include-withdrawn-sources`를 명시한
crypto 진단 실행에서만 도달할 수 있다. 철회 source는 여전히 보고 경로에 실리지
않는다. KR/US에는 도달 가능한 역사 builder가 없다.
남는 역사 절은 스냅샷 소스 상호 비교 + 무작위 대조뿐이며, 아래 라벨을 붙인다.

## 철회 가드의 CI 보호 범위 — 알려진 갭 (2026-08-24, 동결)

철회(`WITHDRAWN_SOURCES`) 가드는 **동작한다.** 적대검증에서 실 CLI 파이프라인에 철회행
120개를 심어도 커밋 산출물 5종에 0건이었고, 가드 지점 11개 + alias 방어 1개가 뮤턴트에
assertion 으로 RED 다.

**CI 가 보호하는 것**
- 가드 로직 제거(집계·부트스트랩·보고 3층, 러너 builder 선택, opt-in 검증, 정본/메타데이터)
- 러너에 철회 소스를 인라인으로 되살리는 회귀 — KR·US·crypto **3시장 모두**
- alias 로 canonical source id 를 우회하는 경로

🔴 **CI 가 보호하지 못하는 것 (이 갭은 알면서 남긴다)**
철회된 source id 를 **실제 프로덕션 builder 를 경유해** 배선하거나, **행 발행 레이어에서
재라벨**하는 형태의 회귀는 현재 테스트가 잡지 못한다.

근인은 가드 로직이 아니라 **테스트 픽스처의 비현실성**이다 —
`test_default_run_market_does_not_call_withdrawn_builder` 가 builder map 을
프로덕션에 없는 단일 키(`{market}.consecutive_gainers`)로 치환하고, crypto 스냅샷을
`rsi=40.0` 한 행만 준다. 그래서 실 builder 키를 경유하는 회귀는 스텁 map 에 그 키가 없어
무음이고, 실 builder 를 직접 호출하는 회귀는 스텁 데이터가 필터(`rsi<=35` 등)에 걸려
빈 pool 이 된다.

적대검증자가 **프로덕션 `_CRYPTO_BUILDERS`(7개 실 builder) + 현실적 3행 스냅샷**으로
실측한 결과, 이 형태의 회귀 4종은 **옵트인 없는 기본 실행에서 `crypto.tv_rsi45` 를 실제로
산출**하는데 테스트는 침묵한다.

**영향 범위**: 출력층 가드(집계·부트스트랩·보고)가 살아 있어 **커밋 산출물은 오염되지
않는다.** 유출은 gitignored `picks.csv` 까지다.

**닫으려면**: 위 테스트가 스텁 map 대신 **프로덕션 builder map** 을 쓰고, **실 builder 의
필터를 통과해 실제로 행을 만드는 스냅샷**을 주면 된다. 이 수리는 하드캡 소진으로
의도적으로 보류했다(라운드 4회 + 연장 1회).

경위: `~/work/herdr-inbox/jobs/s140-screenerbt-prospective-20260823-2230/verify-4-result.md`

1. **전부 연구정의** — 라이브 프리셋이 아니다.
2. **단일 국면** — 표본기간 KR 등가중 −12.3%. 일반화 금지.
3. **freeze provenance 없음** — spec·코드·artifact 가 단일 첫 커밋. 사후 증명 불가.
4. `kr.double_buy` 는 **잔여 보통주 필터 차이 약 17.5%**.
5. `top_gainers` 는 **라이브 change-rate asc(풀백) 분기와 다름** (연구는 desc).
6. `us.high_yield_value` 는 **연구정의(tvscreener) — 라이브 parity 미검증**.
   r2 의 "yahoo=0 이라 재구성 불가" 문장은 삭제했다. 재실행은 하지 않았다.
7. **사용 제한**: 정책·가중치 근거 아님. 세 핵심 주장은 r2 에서 OOS 사용 불가.

사전등록 freeze 가 결과보다 먼저였다는 git 증거는 없다 (S5).
`spec.py` · runner · 첫 artifact 가 단일 첫 커밋
(`de53f1622 research(screener-bakeoff)`)에 함께 들어왔다.

---

## §0.1 전향 로깅 (이번 PR)

- 레코더: `app/services/screener_pick_log.py` — **fanout 바깥**.
  MCP 등록 래퍼가 fanout 반환값을 기록한다. `buy_candidate_fanout.py` 는
  계속 "performs no writes".
- 게이트: `SCREENER_PICK_LOG_ENABLED` 기본 false. fail-open.
- 저장: `review.screener_pick_log` (additive 마이그레이션 1개).
  운영자가 별도로 `alembic upgrade head`. 이 레포는 자동 적용하지 않는다.
- 스케줄러 등록 0. 채점기·집계기·대시보드는 범위 밖 (픽이 쌓인 뒤 별건).
- 가격은 exact decimal **문자열**. float 컬럼 없음.

로깅 시작일 = PR #1940 머지·배포 후 `SCREENER_PICK_LOG_ENABLED=true` 인 상태에서
첫 생산 insert. 그 전 날짜는 in-sample 이 아니다. 백필 금지.

---

## §1 역사 표본에서 남긴 것 (연구정의, 재실행 없음)

스냅샷 프리셋 + 무작위 대조만. `tv_rsi45` 행은 표에서 삭제했고 위 구조 가드가
재실행 시 재생성을 막는다.
게이트 없음, 날짜 단위 중앙 초과수익. **모든 유의 주장은 지평을 붙인다.**
block CI 가 0 을 가르면 "유의하게 나쁨/좋음"이라고 하지 않는다.

### KR — 게이트 없음

| 소스 | D+5 중앙초과 | D+5 block CI∋0 | D+20 중앙초과 | D+20 block CI∋0 |
|---|---:|---|---:|---|
| `high_yield_value` 연구정의 | +2.06% | no | +11.28% | no |
| `cheap_value` 연구정의 | +1.86% | yes | +13.30% | no |
| `growth_expectation_toss` 연구정의 | +1.92% | yes | +8.24% | no |
| `random` | −0.41% | yes | +0.32% | yes |
| `high_volume_surge` | −1.38% | yes | −4.87% | yes |
| `double_buy` 연구정의 (필터차 17.5%) | −3.17% | no | −8.61% | no |
| `top_gainers` 연구정의 (live asc 와 다름) | −3.40% | no | −10.24% | yes |
| `trade_amount` 연구정의 | −4.56% | yes | −17.70% | no |

지평을 붙인 말만 성립한다:

- **D+5 에서** `double_buy`·`top_gainers` 는 무작위보다 유의하게 나빴다
  (block CI 가 0 을 안 가름). `trade_amount`·`high_volume_surge` 의 D+5 는
  block CI 가 0 을 가르므로 유의 열위로 부르지 않는다.
- **D+20 에서** `double_buy`·`trade_amount` 는 무작위보다 유의하게 나빴다.
  `top_gainers`·`high_volume_surge` 의 D+20 는 block CI 가 0 을 가르므로
  유의 열위로 부르지 않는다.
- **"쌍끌이·등락상위가 무작위보다 나쁨"을 지평 없이 쓰지 마라.**
  `top_gainers` 는 D+5 에서만 성립하고 D+20 에서는 성립하지 않는다.
  `trade_amount` 는 그 반대다.

가치 3종의 D+20 중앙 초과는 이 단일 국면에서 크다. 그것이 다른 국면에서도
유지되는지는 이 표본으로 말할 수 없다. OOS 근거로 쓰지 마라.

### US / crypto

`us.high_yield_value` D+20 +7.79%(block CI 가 0 을 안 가름) 는
**연구정의(tvscreener) — 라이브 parity 미검증**. 라이브 Yahoo 프리셋이 아니다.
`cheap_value` / `consecutive_gainers` 는 중앙과 평균의 부호가 반대다
(마이크로캡). "좋다"로 읽으면 안 된다.

crypto 스냅샷 소스 중 두 지평 모두 block CI 가 0 을 안 가르는 양(+)은
`long_short_skew` 의 D+20 뿐이다 (D+5 는 block CI∋0). 라이브 비교가 아니다.

표 전체(철회된 `tv_rsi45` 행 제외) = `artifacts/report_tables.md`.

---

## §2 채점 계약 (동결, 바꾸지 않음)

* 진입가 = 결정일 종가(스냅샷 자신의 `latest_close`).
* 성과 = D+5 / D+20 (KR·US 는 거래일, crypto 는 스냅샷 일자).
* **초과수익 = 픽 수익 − 같은 날 같은 시장의 등가중 벤치마크 수익**.
* 집계는 **날짜 단위**로 먼저 접는다.
* 판정 기준·최소 표본(D+20 유효 40일)·제외 규칙·실패 시 처리는
  전향 실험에서도 **그대로**다. 임계를 느슨하게 만들지 않는다 (Q4 동결).

---

## §3 look-ahead 통제 (계산 유틸, 유지)

* 스냅샷 소스는 결정일 파티션 행만 읽는다.
* 재구성 게이트는 `PricePanel.window()` 로 결정일 이하 봉만 자른다.
* `tests/research/test_screener_bakeoff_contract.py` 가 창 경계·미래봉 불변·
  프로덕션 지표 동일성(RSI·볼린저·피보나치·지지/저항)을 기계 검증한다.
* 패널·지표·채점·부트스트랩은 전향 채점에 재사용될 자산으로 유지한다.

---

## §4 알려진 한계

1. **표본이 한 국면이다.** KR 대폭 하락. 가치·역발상 유리, 모멘텀 불리 쪽으로
   편향될 수 있다. 일반화 금지.
2. **창 중첩.** D+20 결정일들이 19일을 공유. iid null 은 그만큼 낙관적.
3. **upside 게이트 중화.** 게이트 통과 수치는 전부 상한.
4. **커버리지.** `kr.oversold_recovery` 31.8%, `kr.stable_growth` 30.0% 가
   채점 불능. 화려한 숫자는 나머지와 동급이 아니다. KR 절단행은 전체보다
   **나쁘다** (−7.45pp, artifact S2).
5. **문언 차이**는 `spec.py` `SourceSpec.caveats`.
6. **investor_flow_momentum 은 판정 불가.** D+5 음 / D+20 양, 유효일 29/16.
7. **freeze provenance 없음 (S5).**
8. **block bootstrap.** iid null 0 이어도 block CI∋0 이면 같은 강도로
   "유의 열위"라고 하지 않는다.
9. **raw picks 는 git 추적 대상이 아니다 (S6).**

---

## §7 사전등록 — 전향 전용 (`screener-source-weighting-v1`)

아래는 **다음 실험의 사전등록**이다. 역사 재구성 `tv_rsi45` 가 비교자가 아니다.
비교자 = **로깅된 라이브 fanout 픽**. 새 가설을 추가하지 않았다. H1–H4 의
비교자만 재정의한다. 판정 기준·최소 표본·제외 규칙·실패 시 처리는 원문 그대로.

```
실험 id: screener-source-weighting-v1
비교자: 로깅된 라이브 fanout 픽 (review.screener_pick_log).
        재구성 tv_rsi45 가 아니다.
로깅 시작일: PR #1940 머지·배포 후 SCREENER_PICK_LOG_ENABLED=true 인
             상태의 첫 생산 기록일. 백필 없음.
가설 H1 (KR): 가치/수익성 3종 union 소스
   = high_yield_value ∪ cheap_value ∪ growth_expectation_toss
   (중복 심볼은 세 소스의 랭크 평균으로 정렬, 상위 10)
   는 로깅된 라이브 fanout 픽 대비 D+20 날짜단위 중앙 초과수익이 크다.
가설 H2 (US): high_yield_value 단독이 로깅된 라이브 fanout 픽 대비 D+20 우세하다.
가설 H3 (crypto): long_short_skew 가 로깅된 라이브 fanout 픽 대비 D+20 우세하다.
가설 H4 (게이트): B_moderate2 완화 시 H1 의 우세폭이 A_strong 대비 확대된다.

사전 고정:
  - 판정 기간 = 로깅 시작일 이후 신규 결정일만 (본 베이크오프 표본과 겹치지 않는 OOS)
  - 최소 표본 = D+20 유효 결정일 40일. 그 전에는 중간값으로 판정하지 않는다.
  - 판정 기준 = 날짜단위 중앙 초과수익 차 > 0 AND 날짜 승률 ≥ 60% AND
                부트스트랩 null 백분위 ≥ 95, 세 조건 동시 충족.
  - 제외 규칙(사전 고정): 봉 결측률 10% 초과 소스는 판정 대상에서 제외하고 그 사실을 보고.
  - US 가치 프리셋에는 시총 하한 $300M 을 사전 부과한다(마이크로캡 왜곡 차단).
  - 실패 시 처리: 기존 라이브 fanout 소스 구성 유지. 부분 성공 시 시장별로만 반영.
금지: 이 실험 중 어떤 주문·워치·제안 경로에도 편입하지 않는다. 채점 전 정책 변경 0.
      새 소스·새 시장·새 지평·새 가설 추가 0.
```

이 초안은 실행이 아니다. 전향 표본이 최소 기준을 채운 뒤에만 정책 논의로 넘긴다.

---

## §5 재현

```bash
export DATABASE_URL=postgresql://.../auto_trader     # read-only 계정 권장
uv run python -m research.screener_bakeoff.run_bakeoff
uv run python -m research.screener_bakeoff.aggregate
uv run python -m research.screener_bakeoff.bootstrap
uv run python -m research.screener_bakeoff.report
uv run pytest tests/research/test_screener_bakeoff_contract.py tests/research/test_screener_bakeoff_parity.py
uv run pytest tests/services/screener_pick_log/test_recorder.py
```

역사 재실행은 이번 범위 밖이다. 하지 마라.
