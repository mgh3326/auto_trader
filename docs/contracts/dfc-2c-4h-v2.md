# DFC-2C-4H-v2 독립 재등록 계약

상태: 설계 봉인. 이 문서는 백테스트 실행, 데이터 수집, Binance Demo 접촉, 계좌 배정 또는 주문을 승인하지 않는다.

- 정본: `/Users/mgh3326/work/herdr-inbox/answer-codexmock-dfc-adjudication-0821.md` §R-3 (직접 열람)
- 계약 ID: `DFC-2C-4H-v2`
- 계약 구현: `research_contracts.dfc_2c_4h_v2` (stdlib-only, I/O 없음)
- canonical hash: `85673c730555816e3c2c6759a0489ed5543396e5ad588aadef4624198a74b99f`

`DFC-2C-4H-v2`는 `dfc-4h`의 patch, row update 또는 supersession이 아니다. 기존
`dfc-4h` row와 seal은 수정·숨김·대체하지 않으며, 이 계약은 독립 새 등록이다.

## 1. 고정 universe와 feature

신호 universe는 정확히 `XRPUSDT`, `DOGEUSDT`, `SOLUSDT` 세 종목이다. decision epoch는
UTC 4시간 경계이고 해당 source interval은 `[e-4h,e)`다.

각 종목·epoch에서 필요한 feature는 정확히 두 개다.

1. Binance USD-M `GET /fapi/v1/klines` (`v1`, `interval=4h`)의 complete Kline에서
   total base volume `V`와 taker-buy base volume `B`를 읽는다. `S=V-B`로 두고
   `OFI=log(B/S)`로 계산한다. `V`, `B`, `S`는 모두 finite 및 strictly positive여야 한다.
   quote volume은 입력이 아니며 epsilon, zero 대체 또는 보간도 허용하지 않는다.
2. Binance USD-M `GET /fapi/v1/premiumIndexKlines` (`v1`, `interval=4h`)의 complete
   candle close를 premium feature로 그대로 사용한다. 5분 close들의 평균, 종가 선택,
   VWAP 등 재집계는 허용하지 않는다.

따라서 `quote_volume_proxy`와 `five_minute_premium_average`는 폐기됐다. 두 endpoint의
host/path/version, schema version, epoch, raw payload SHA-256은 후속 artifact manifest에
공개되어야 한다.

## 2. score와 Q0.75 봉인

각 feature `f∈{OFI, Premium}`에 대하여 현재 epoch를 제외한 prior complete finite 관측치
252개를 `H^f(s,e)`로 둔다. 모든 값이 정확히 252개일 때만 score를 계산한다.

```text
U_f(s,e) = 2 * mean(h <= f(s,e) for h in H^f(s,e)) - 1
C(s,e)   = (U_OFI(s,e) + U_Premium(s,e)) / 2
```

`<=`는 PIT tie rule이다. `C`의 threshold history도 현재값을 제외한 prior score-eligible
`|C|` 252개다. 오름차순 `a[0]..a[251]`에 대해 오직 다음 linear rule만 쓴다.

```text
q(s,e) = a[188] + 0.25 * (a[189] - a[188])
tail(s,e) = abs(C(s,e)) >= q(s,e)
```

비교는 inclusive다. Q0.75는 runtime 설정도, CLI 인자도, strategy parameter도 아니다.
`tail_threshold_q75(prior_abs_composites)`는 quantile 인자를 받지 않고,
`score_symbol(inputs)`도 quantile override를 받지 않는다. 따라서 Q0.70/Q0.80 등의
발화율 맞춤 sweep은 이 계약 구현으로 호출할 수 없으며, extra argument는 `TypeError`다.
다른 quantile, window, score definition을 쓰려면 새 ID와 새 canonical hash가 필요하다.

## 3. basket과 one-winner 규칙

세 symbol의 current input이 같은 UTC epoch에 모두 complete여야 한다. 그 후에만
`candidate_any = any(tail(s,e))`를 계산한다. 하나라도 gap, missing, conflict, invalid 또는
reference-not-ready이면 epoch는 `not_evaluable_*`이고 `candidate_any`는 `None`이다. 이는
0/false로 바꾸지 않으며 incidence denominator에서 조용히 제거하지도 않는다.

candidate가 여러 개면 한 event만 남긴다. winner는 `abs(C)`가 가장 큰 tail candidate이며,
`abs(C)`가 정확히 같으면 다음 사전 고정 순서를 쓴다.

```text
XRPUSDT, DOGEUSDT, SOLUSDT
```

따라서 basket은 epoch당 최대 한 winner를 기록한다. 이 규칙은 상품 방향, 주문 또는 PnL
규칙이 아니며 오직 outcome 관측 단위를 고정한다.

## 4. data integrity와 공개 provenance

complete-only가 hard rule이다. input evidence에는 최소 source ID, endpoint host/path/version,
symbol, interval, epoch start/end, `complete`, `gap_status`, raw payload SHA-256 및 schema
version이 있어야 한다. raw hash는 lowercase 64-hex가 아니면 거부한다.

다음은 모두 금지다.

- missing/gap/conflict 값을 forward fill, zero, 평균 또는 다른 심볼 값으로 대체하는 행위
- incomplete candle을 close로 간주하는 행위
- gap epoch를 no-candidate로 바꾸는 행위
- endpoint/version/raw hash를 생략한 artifact 공개
- v2 candidate rate를 보고 Q0.75 이외의 quantile을 선택하는 행위

`research_contracts.dfc_2c_4h_v2.validate_evidence_manifest`와
`evaluate_basket`은 complete/gap/reference 상태를 명시적으로 보존하는 회귀 경계를 제공한다.
이 모듈은 network, database, collector, broker, ledger 또는 runtime app import를 하지 않는다.

## 5. exploration 격리와 별도 검증

기존 `dfc-retro-probe-v1`의 1,095일 artifact (`2023-08-04T00:00:00Z`부터
`2026-08-03T00:00:00Z`)와 기존 2-of-3 incidence는
`design_only_exploration`이다. 특히 proxy OFI, 5분 premium 평균, 180/540 window,
threshold sweep 결과, 그리고 그 수치로부터의 성과·발화율 주장은 v2의 primary evidence,
promotion evidence 또는 incidence adjudication으로 사용할 수 없다.

새 계약은 뒤늦게 proxy output을 재라벨링하지 않는다. 별도 chronological historical holdout은
다음처럼 고정한다. 이 문서 자체는 수집이나 백테스트 실행을 승인하지 않으므로, 실행에는 설계
승인 후 별도 작업 승인이 필요하다.

| 구간 | UTC | 용도 |
| --- | --- | --- |
| warm-up | `[2021-02-02T00:00:00Z, 2021-05-02T00:00:00Z)` | prior 252 feature/score history 형성 |
| historical holdout | `[2021-05-02T00:00:00Z, 2021-10-29T00:00:00Z)` | 정확히 180일, 1,080 scheduled epoch, 단 한 번의 bounded exact-feature backtest |
| prospective pilot | 새 pilot manifest의 새 T0 뒤 28일 | 168 scheduled epoch의 no-order shadow/paper 관측 |

holdout은 exploratory proxy artifact와 시간적으로 겹치지 않는다. history 또는 outcome이
부족하면 `NA`와 integrity failure를 기록할 뿐, 다른 기간을 고르거나 새 sensitivity run을
같은 hash 아래 추가하지 않는다.

## 6. 새 estimand와 promotion budget

새 estimand는 directional alpha나 거래 PnL이 아니다. 사전 고정된 descriptive outcome은
complete 4h Kline close로 계산한 다음 값이다.

```text
Y(s,e) = abs(log(close(s,e+4h) / close(s,e))) * 10,000
Delta  = mean(Y(winner,e) | candidate_any=true)
         - mean(mean_s Y(s,e) | candidate_any=false)
```

candidate epoch에는 §3의 유일 winner만, non-candidate epoch에는 세 symbol의 cross-sectional
mean만 사용한다. alternative horizon, side, control, cost model 또는 PnL comparator를 추가할
수 없다. candidate가 전혀 없으면 `Delta=NA`로 보고하며, 그 결과로 sweep하거나 기간을
연장하지 않는다.

새 promotion budget은 다음과 같다.

- 한 번의 180일 historical backtest와 한 번의 28일 no-order shadow/paper pilot만 허용한다.
- 주문 0, Binance Demo 접촉 0, 계좌 배정 0, 자동 promotion 0이다.
- 기존 608 effective outcomes와 기존 365일 cap은 모두 상속하지 않는다.

180일은 exact-feature 구현을 검증하면서 범위를 6개월로 제한하기 위해 선택했고, 28일은
4개의 완전한 UTC 주간 cycle에서 data-integrity와 signal logging을 짧게 관찰하기 위해
선택했다. 둘 다 기존 장기 cap·표본수나 원하는 발화율에서 유도한 값이 아니다. 결과가
긍정적이어도 account assignment를 자동으로 만들지 않는다.

## 7. Binance Demo와 OI collector 처분

Binance Demo 계좌는 자동 해제되지 않고 v2에 자동 승계되지 않는다. v2 historical holdout
보고 뒤에도 별도 운영자 배정 상신이 필요하다. 그 전까지 old T0, correlation ID, ledger
identity, order authority는 재사용 금지다. 향후 별도 배정 검토에서만 fresh account-wide
positions/open-orders/ledger truth의 read-only flatness 확인과 새 pilot manifest/T0를 수행할
수 있다. 이 설계는 그런 조회도 수행하지 않았다.

OI collector의 상신용 처분 판정은 **중단 권고**다. 실행은 하지 않았다. 이유는 v2가 OFI와
premium의 두 성분으로 봉인돼 있어 OI를 유지하면 다른 3성분 계약이 되기 때문이다. OI를
필수로 유지하려면 권위 있는 장기 OI history source를 조달하거나 prospective accumulation을
별도 계약으로 받아들여야 하며, 이번 proxy로 기다림을 단축할 수 없다. 이 권고는 DFC lane에
한정되며 기존 collector의 stop, 삭제 또는 변경을 이 문서가 수행하지 않는다.

## 8. 검증 경로

```bash
uv run pytest tests/research_contracts/test_dfc_2c_4h_v2.py -q -ra
uv run ruff check research_contracts/dfc_2c_4h_v2.py tests/research_contracts/test_dfc_2c_4h_v2.py
```

회귀 테스트는 새 ID/hash, old seal 보존, base-volume OFI, complete 4h premium close,
current-excluded PIT, fixed linear Q0.75, sweep 불가 API, 3-symbol inner alignment, one-winner
tie rule, provenance fail-closed, exploration isolation, non-inherited budget, Binance Demo
non-succession 및 OI stop recommendation을 확인한다.
