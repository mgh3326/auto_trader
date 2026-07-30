# AC5 quote timestamp source 계약 (ROB-1172 D2) — 구현 전 고정본

```
상태        계약 고정만. 코드 미구현. gate source 교체는 계약 테스트 + 적대검증 통과 후.
승인 근거    codex-mock D2 조건부 승인 (2026-07-30T10:11:42+09:00)
우선 source  FHKST03010230 (당일분봉, inquire-time-dailychartprice)
🔴 금지      FHKST01010300 단독 · local date 결합 · 다른 endpoint 의 date/time 결합 · wrapper timestamp
```

## 0. 왜 교체하는가 (C1 관측)

현재 AC5 gate 가 인정하는 source 는 `FHKST01010100`(주식현재가 시세)인데, 2026-07-30 09:52
KST 장중 실측에서 그 응답 `output` 80키에 **`stck_bsop_date`·`stck_cntg_hour` 가 둘 다
없다**(`rt_cd=0`, 가격은 정상). 즉 gate 가 요구하는 provider 원문 timestamp 를 그 TR 은
제공하지 않으므로 **영구 unprovable** 이다. 증거: `rob1172-c1c4-diagnostic-2026-07-30.md` §1.

## 1. 실측한 raw schema — FHKST03010230

2026-07-30 10:37:13 KST, 005930, `.env.dev`, GET only. `rt_cd="0"`, `msg_cd="MCA00000"`.

```
endpoint  /uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice
TR        FHKST03010230
params    FID_COND_MRKT_DIV_CODE=J · FID_INPUT_ISCD=<6자리> · FID_INPUT_HOUR_1=HHMMSS
          FID_INPUT_DATE_1=YYYYMMDD · FID_PW_DATA_INCU_YN=N
          FID_FAKE_TICK_INCU_YN="" · FID_ETC_CLS_CODE=""
          🔴 FID_INPUT_DATE_1 누락 시 "ERROR INPUT FIELD NOT FOUND [FID_INPUT_DATE_1]"
envelope  rt_cd · msg_cd · msg1 · output1 · output2
output1   acml_tr_pbmn · acml_vol · hts_kor_isnm · prdy_ctrt · prdy_vrss ·
          prdy_vrss_sign · stck_prdy_clpr · stck_prpr
output2   행당 8키 — stck_bsop_date · stck_cntg_hour · stck_oprc · stck_hgpr ·
          stck_lwpr · stck_prpr · cntg_vol · acml_tr_pbmn      (98행 관측)
정렬      🔴 최신 → 과거 (descending). 관측: 행[0]=103700, 행[-1]=090000
세션      관측된 distinct stck_bsop_date = {"20260730"} (단일 세션)
```

**승인 조건 충족**: 같은 원시 레코드(`output2` 한 행)에 세션일자·체결시각·가격이 함께 있다.
→ D2 의 "같은 원시 레코드에 symbol·price·`stck_bsop_date`·`stck_cntg_hour`" 중 **price/date/time
세 개는 충족**. symbol 은 아래 §2 참조.

## 2. 🔴 request↔response symbol identity — 이 TR 은 symbol 을 돌려주지 않는다

```
output2 행에 symbol 계열 필드 없음      (stck_shrn_iscd / mksc_shrn_iscd / symbol 전부 부재)
output1 에도 종목코드 없음              (있는 것은 hts_kor_isnm = 한글 종목명뿐)
```
`FHKST01010100` 은 `stck_shrn_iscd` 를 돌려주므로 요청↔응답 결속이 payload 로 가능한데,
**분봉 TR 은 불가능하다.** 요청 symbol 을 그대로 신뢰하는 것은 "우리가 그 symbol 로
요청했다"는 우리 쪽 주장이고, provider 가 확인해 준 identity 가 아니다 — A1 에서 폐기한
치환과 같은 종류의 논리다.

고정 규칙:

```
1. 요청 symbol 은 request-side provenance 로만 기록한다 (requested_symbol).
2. provider-origin identity anchor 는 output1.hts_kor_isnm 뿐이다 (한글 종목명).
   이를 DB kr_symbol_universe.name 과 exact 일치로 교차검증한다.
   불일치 · 부재 → identity unproven → fail-close.
3. 🔴 이름 교차검증은 "약한 anchor" 임을 evidence 에 명시한다
   (name_identity_is_weaker_than_code_identity: true).
   동명 종목 가능성을 부정하지 않는다.
4. 🔴 이름 교차검증만으로 code identity 를 주장하지 않는다. 필요하면 code identity 를
   제공하는 별도 TR 로의 이중 확인이 후속 과제다 (이번 계약에 포함하지 않음).
```

## 3. 고정 규칙 — latest-row · empty/gap · 상한

```
latest-row     stck_bsop_date == target_session 인 행만 후보.
               그 중 stck_cntg_hour 최대값 1행. 🔴 index 0 을 신뢰하지 않는다
               (관측상 descending 이지만 문서 보장이 없다 — 리포의 기존 관례와 동일).
               동일 (date,time) 중복 행이 2개 이상이면 → duplicate → fail-close.
empty          output2 = [] 또는 target_session 행 0개 → unprovable
               (reason: quote_timestamp_no_row_for_target_session). 다른 세션 행으로
               대체하지 않는다.
gap            분봉 결손(특정 분 누락)은 결함이 아니다 — 우리는 "최신 1행"만 쓴다.
               단 선택된 행의 8필드 중 하나라도 부재/비정수문자열이면 fail-close.
세션 경계      선택 행의 KST datetime = stck_bsop_date + stck_cntg_hour (둘 다 원문).
               🔴 date 와 time 을 서로 다른 응답/다른 endpoint 에서 조합 금지.
               local date 보완 금지 (부재 → fail-close).
상한           raw_observed_at <= decision_at        (결정 시계 상한)
               captured_at    <= decision_at        (회수 시계 상한)
               raw_observed_at.date() == target_session
하한           raw_observed_at >= 세션 09:00 KST (정규장 개시). 완료세션 증거로 쓸 때는
               기존 QUOTE_EVIDENCE_AT_OR_AFTER(15:30) 를 그대로 유지한다.
rate-limit     symbol 당 1 GET. 기존 KIS async rate limiter 경유(관측: 1/0.2s 대기 발생).
               전수 sweep 은 이 source 의 용도가 아니다 (선정 후보 1종목 검증용).
raw hash       sha256(canonical_json_bytes({"endpoint","tr_id","params","output1","output2"}))
               — 선택 행만이 아니라 응답 전체를 해싱해 latest-row 선택을 재현 가능하게 한다.
```

## 4. schema version · 소급 금지

```
새 버전    krb1.p0_3.quote_timestamp_capture.v2
🔴 기존 v1 evidence(FHKST01010100 기반, raw 필드 null)를 v2 로 소급 승격하지 않는다.
   v1 row 는 rehydrate 시 거부한다 (metadata authority v2 와 같은 규칙).
🔴 v2 는 endpoint/tr_id 가 FHKST03010230 인 행만 인정한다.
```

## 5. 남은 결정 (구현 전 필요)

```
D2-a  hts_kor_isnm 이름 교차검증을 identity 로 인정할지 — 이름 anchor 의 강도 판단은
      계약 결정이다. 인정하지 않으면 이 source 도 identity unprovable 로 fail-close 된다.
      (내 권고: 인정하되 weak anchor 라벨 + code identity 이중확인을 후속 과제로)
D2-b  완료세션 증거로 쓸 때의 하한(15:30) 유지 여부 — 분봉은 15:30 이후 행이 존재하므로
      기존 하한을 그대로 쓰면 된다. 변경 불필요로 판단하나 확인 요청.
```

🔴 위 두 결정 전에는 gate source 를 교체하지 않는다. 이 문서는 계약 고정본이고,
구현·계약 테스트·적대검증은 별도 단계다.
