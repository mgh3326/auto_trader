# AC5 quote timestamp source 계약 (ROB-1172 D2) — 구현 전 고정본

```
🔴 판정      AC5_IDENTITY_UNPROVABLE (2026-07-30 E1). 이 source 로 교체하지 않는다.
상태        계약 고정만. 코드 미구현. gate source 교체는 identity 확보 + 적대검증 통과 후.
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

🔴 **E1 판정 (2026-07-30, 운영자 결정)**: 이전 판의 규칙 2~4(이름 anchor 를 identity 로 인정)는
**철회한다.** 운영자 판정:

> `hts_kor_isnm` exact match SHALL be recorded only as corroborating weak-name evidence
> and SHALL NOT establish security-code identity. If no official stable identifier or
> provider-guaranteed request-response binding is found, AC5 remains UNPROVABLE;
> no name-based or cross-endpoint fallback is permitted.

4단계 bounded investigation 결과 (원문: `~/work/herdr-inbox/rob1172-a3a4-2026-07-30.md` §1):

```
1. raw response 전 필드 전수      envelope 5 · output1 8 · output2 8 (120행)
                                 🔴 공식 schema 는 확인 실패 — KIS 포털이 JS 셸(script 69개,
                                    본문에 FHKST03010230·gt_uid 0건)
2. body 의 stable code field      부재. 응답 본문 전체에 "005930" 문자열 0건
3. provider 보증 request↔response 결속   확인 불가
   - 우리는 client txn-id 를 보내지 않는다 (headers: appkey/appsecret/authorization/custtype/tr_id)
   - 응답 header 의 gt_uid 는 **서버 생성**이고 symbol 을 담지 않는다
     (헤더에도 "005930" 0건). tr_id 는 TR 종류 echo. x-oracle-dms-* 는 infra 추적 ID
   - gt_uid 로 요청 파라미터를 조회할 배선·문서 없음 → 결국 "우리가 보관한 request 를
     우리가 믿는" self-asserted provenance 로 환원된다 (A1 이 폐기한 형태)
   - provider 보증 문구를 공식 문서에서 확인하지 못함
4. 같은 payload/결속 transaction 에 4요소   symbol identity **false** / price·date·time true
```

결론: `AC5_IDENTITY_UNPROVABLE` 유지. 이름 기반 승격 · local date 보충 ·
cross-endpoint identity/timestamp 조합 전부 하지 않았다. weak anchor 를 쓰려면 **별도
amendment** 로 운영자 승인을 다시 받아야 한다.

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

## 5. 결정 결과

```
D2-a  ✗ 불승인 (E1) — weak-name anchor 는 identity 가 아니다. AC5_IDENTITY_UNPROVABLE 유지
D2-b  ✓ 하한 15:30:00 KST inclusive 유지 (E2). 변경 없음.
      의미: close 이후 quote observation 의 최소 시각일 뿐 provider finality·daily completion
      을 증명하지 않는다. C5 의 15:35 one-shot cutoff 및 provider-finality 축과 분리한다.
```

### 이 source 를 열 수 있는 유일한 경로

```
① 응답에 stable security code 를 담는 다른 TR 을 찾는다 (같은 레코드에 date/time/price 포함)
② provider 가 request↔response 결속을 **문서로 보증**하는 식별자를 찾는다
   (client 가 보낸 txn-id 를 응답이 되돌려주고, 그 결속을 공식 문서가 명시)
③ 위 둘이 없으면 AC5 는 계속 UNPROVABLE — 이름·local date·cross-endpoint 조합 금지
```
🔴 이 문서는 계약 고정본이고, 구현·계약 테스트·적대검증은 별도 단계다. 현재는 ③ 상태다.
