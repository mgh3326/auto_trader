# R4.1 합본 봉인 정본 — T3

- 봉인일: 2026-07-28
- 연구 ID: `R4.1-DFC-4H`
- 상태: `SEALED`
- 운영자 승인: `fable-to-orch-r41c-seal-approval-2026-07-28.md`
- 내부 봉인 SHA-256: `b3ee7db2f4cd8f76522a9c66ca8201177a01c24bbbd3876f53da4fb2f7c14a94`

## 내부 봉인 해시 규칙

이 문서의 UTF-8 전체 바이트에서 위 `내부 봉인 SHA-256` 값 64자를 64개의 ASCII `0`으로
정규화한 뒤 SHA-256을 계산한다. 이 규칙은 자기참조 없이 완성본 전체를 검증하기 위한
canonical serialization이다. 줄바꿈은 LF, 파일 끝 newline은 1개로 고정한다.

## 절별 출처 매핑

| 합본 절 | 원문 출처 | 이관 규칙 |
|---|---|---|
| R4.1 §0~§2 | `gptpro-r41-response-2026-07-27.md` §3 | 원문 괄호 조문 verbatim |
| R4.1b 유지분 | `gptpro-r41b-response-2026-07-27.md` §6 | power 해석 조문 verbatim |
| R4.1c §3~§5 | `gptpro-r41c-response-2026-07-28.md` 응답 원문 | 원문 조문 verbatim; 운영자 철회 지시 대상인 과거 증거 삭제 목록과 금지된 서비스 확률 표기 문장만 whole-line omission |
| R4.1 §6~§8 | `gptpro-r41-response-2026-07-27.md` §3 | 원문 괄호 조문 verbatim |
| 봉인 계약 1 | `r4-sealed-contracts-2026-07-27.md` 계약 1 | 조문·채택 근거·서술 제약 verbatim |
| 봉인 계약 2 | `r4-sealed-contracts-2026-07-27.md` 계약 2 | 조문·한계·인용 필수 표기 verbatim |

R4.1b의 14줄짜리 §3~§5 요약은 이 합본의 normative text에 포함하지 않는다. 그 요약에
딸린 seed 없는 500k/rate MC 결과, 재현 불가 shrinkage 백분율 결과, 철회된 서비스 확률
표기도 포함하지 않는다. 삭제 대상 문장을 고쳐 쓰거나 요약본으로 대체하지 않고, 줄 전체를
이관 대상에서 제외했다.

## 원문 SHA-256 manifest

| 원문 | SHA-256 |
|---|---|
| `gptpro-r41-response-2026-07-27.md` | `9da52c0666af2f72101c252554e494cc95912d073b8d47deca677560de3316a2` |
| `gptpro-r41b-response-2026-07-27.md` | `9bf0248f9ab93b2cc361daf3d8723066444249389f46ec3c1c4354eb3eacf231` |
| `gptpro-r41c-response-2026-07-28.md` | `f446e9ea37b92df92c62ad90a841d94b8fa6524b5e7f0677202ba7768a0330af` |
| `r4-sealed-contracts-2026-07-27.md` | `51dc929c229186cca780812018f98aab2a0666c89931ec3a3d5e92b96568987f` |

검증 보고서:

- `r41c-numeric-verify-2026-07-28.md`: `d82b0c9f5fe5814ccf383c84857766fa2b3e54563c5962854a3e732972d8de04`
- `r41c-contract-verify-2026-07-28.md`: `9a71d8f1accd39c7737fbfde69ed795e183b5018ee8a3e80d45629f4e6b44d82`

## 초기 history manifest

| artifact | SHA-256 |
|---|---|
| `r4_p0_backfill.sqlite3` | `9eb6080a3f49102802ac3164a1a3f749d584bd607dea9afd02fe06cb319d67b8` |
| `coverage_report.json` | `babef7a54a91d55d7ab6d9400ec75515ba45994613f85e9d4bd30a093c6d5651` |

`event_time<T0`, `received_at≤T0`, 연속 UTC 4시간 경계, 양 끝 OI 양수, finite
`ln(OI_t)-ln(OI_(t-4h))` 규칙으로 고정한 심볼별 `h_s`:

| symbol | `h_s` |
|---|---:|
| XRPUSDT | 179 |
| DOGEUSDT | 179 |
| SOLUSDT | 179 |

## 봉인 달력 제약

```text
T0              = 2026-07-27T00:00:00Z
T_CAND_DEADLINE = 2026-09-19T04:00:00Z
T_WEEK0         = 2026-09-21T00:00:00Z
T_FREEZE        = 2026-12-14T00:00:00Z
T_A1            = 2027-01-11T00:00:00Z
T_A2            = 2027-02-08T00:00:00Z
T_A3            = 2027-03-08T00:00:00Z
T_HARD          = 2027-12-14T00:00:00Z
T0_TO_T_HARD    = 505 elapsed days
```

`T_CAND_DEADLINE` equality는 통과하며, 그때까지 `T_CAND`가 확정되지 않으면
`PRE_FREEZE_ELIGIBILITY_TIMEOUT`으로 자동 종료한다.

## anchor ledger 초기화

```text
encoding              = UTF-8 JSON, object keys lexicographic, no insignificant whitespace
canonical row order   = 아래 index 1 → 2 → 3; 이후 조문상 anchor 확정 순서로 append-only
chain concatenation   = raw 32-byte previous chain hash || raw 32-byte row hash
genesis               = 0000000000000000000000000000000000000000000000000000000000000000
initial row count     = 3
initial head          = 0f8619dfc6e1e41d70ac7f218cbfd6a847fb85dbc410f1e6a723d79dba380dc0
T_CAND state          = null (확정 시 append; 기존 row 수정 금지)
```

| index | row_hash | chain_hash |
|---:|---|---|
| 1 | `b7654b9a75eecdf46c0965394f0891b175216d2bb7ab490fd6e32f05fecdd9d4` | `5e5535a9849fa7a4dc42dd3def3212b52f4a26e6a08714615acbc2dc9d95c82e` |
| 2 | `5690b9c55481aba2f48271e425e10de508b9b4784bd12e8f9e6e4ca336dc745e` | `8f694f07347482817431e3e4b572888e33202b589c92677eda68db5521269f0b` |
| 3 | `d35f331bdecb876c5e18817a23c4408e4f9cc3344dcf1892c642d5f97a0f6e0f` | `0f8619dfc6e1e41d70ac7f218cbfd6a847fb85dbc410f1e6a723d79dba380dc0` |

초기 canonical rows:

```json
{"record_type":"SEAL_INITIALIZED","recorded_date":"2026-07-28","study_id":"R4.1-DFC-4H","t0":"2026-07-27T00:00:00Z"}
{"record_type":"CALENDAR_ANCHORS","study_id":"R4.1-DFC-4H","t_a1":"2027-01-11T00:00:00Z","t_a2":"2027-02-08T00:00:00Z","t_a3":"2027-03-08T00:00:00Z","t_cand":null,"t_cand_allowed_if_uninterrupted":["2026-09-19T00:00:00Z","2026-09-19T04:00:00Z"],"t_cand_deadline":"2026-09-19T04:00:00Z","t_freeze":"2026-12-14T00:00:00Z","t_hard":"2027-12-14T00:00:00Z","t_week0":"2026-09-21T00:00:00Z","total_days_t0_to_t_hard":505}
{"block_anchor":"ISO-8601 Monday 00:00:00 UTC","block_days":7,"deff_plan":"2.06","ratchet_allowed":false,"record_type":"DEFF_BINDING","study_id":"R4.1-DFC-4H"}
```

ISO-week block 앵커는 Monday 00:00:00 UTC, block 길이는 비중첩 7일이다.
`DEFF_plan=2.06`은 고정이며 live diagnostic으로 상향·하향 ratchet하지 않는다.

---

## Normative text

### R4.1 §0~§2

(§0 IMMUTABLE INVARIANTS: SYMBOLS [XRP/DOGE/SOL]·cap 1·[6,10]·1x·one-way·4h cadence·fixed 4h hold·
TP/SL 없음·비용 17(13/22)·forecast-before-occupancy·epoch당 winner 1·imputation 금지·Q0.75(|C|)
current-exclusion·signed 금지·p_tail 항진 서술 제약)

(§1 PRIMARY OUTCOME & ELIGIBILITY: Y_c,j 정의·usable 6조건·reference-missing은 integrity 로그만·
actual demo execution은 별도 transfer sample)

(§2 MDE CONTRACT: 위 Q-A 그대로 — 220bp FIXED_DESIGN_REFERENCE·α .025 단측·power .80·
early efficacy 금지·MDE 152/304/608 = 49.9925/35.3500/24.9962bp)

### R4.1b 유지분

## 6. R-4 — power 문언 (§2 수식·152/304/608 불변, 해석 조항 추가)

"80% power"는 σ_ref=220bp·단측 α=0.025·z 기준 normal-reference 설계 계산에만 해당. 실제 CR1+t
절차의 same-scale reference power ≈72~78.5% (G=12/25/52). 정확 cluster-law power 주장 없음.
incidence DEFF는 이 차이를 보정하지 않음.

### R4.1c §3~§5

§3. Eligibility clock, 데이터 완전성 및 불변 ledger
§3.1 적용 범위와 불변 상수
본 §3~§5는 R4.1b의 §3~§5를 전부 대체한다. R4.1의 §0~§2와 §6~§8, 봉인 계약 1의 Q0.75(|C|)·PIT current-exclusion, 봉인 계약 2의 ISO-week Monday 00:00 UTC 비중첩 7일 block 추정자 및 DEFF_plan=2.06은 변경하지 않는다.

연구 시작시각은 다음 값으로 고정한다.
T0 = 2026-07-27T00:00:00Z
T0는 collector 재시작, source 장애, backfill, 코드 수정, 운영자 판단 또는 시장 regime 변화에 따라 이동시킬 수 없다.

심볼 집합은 변경 불가능한 세 심볼 XRPUSDT, DOGEUSDT, SOLUSDT로 한다. 어느 한 심볼의 source availability가 저하되더라도 나머지 두 심볼만으로 진행하거나 심볼을 교체할 수 없다.

decision epoch는 UTC 기준 매일 00:00, 04:00, 08:00, 12:00, 16:00, 20:00의 4시간 격자로 한다. epoch e에 귀속되는 현재 source interval은 [e−4h,e)이며, e와 같은 timestamp의 원시 event는 다음 epoch에 귀속한다.

코드, feature schema, source 목록, 심볼 집합, candidate 함수, comparator, quantile 함수 및 본 조문의 canonical serialization 규칙은 합본 봉인 시 각각 SHA-256으로 고정한다. 봉인 후 그중 하나라도 바뀌면 동일 연구의 patch나 retry로 처리하지 않고 CONTRACT_VERSION_CHANGED로 종료한다.

T0 직전 canonical snapshot에 존재하는 심볼별 finite delta_log_oi_4h observation 수를 h_s라 한다. h_s에는 event_time<T0이고 received_at≤T0인 관측만 포함하며, T0 이후 도착한 pre-T0 backfill은 clock 계산에 포함하지 않는다. 합본 봉인 전 manifest는 각 심볼에 대해 h_s≥179임을 증명하여야 한다. 어느 하나라도 이를 증명하지 못하면 연구는 INITIAL_HISTORY_FAIL로 시작 전에 종료한다.

§3.2 Source ledger의 primary key, 중복제거 및 finalization
모든 원시 source observation은 다음 primary key로 기록한다.
source_key = (study_id, policy_hash, source_name, symbol, source_event_id)
source가 자체 불변 event ID를 제공하지 않는 경우에는 다음 composite key를 사용한다.
source_key = (study_id, policy_hash, source_name, symbol, source_event_time, source_interval_start, source_interval_end, record_type)

같은 source_key로 byte-identical canonical payload가 두 번 이상 도착하면 최초 한 건만 계산에 사용하고 나머지는 EXACT_DUPLICATE로 append-only 기록한다. 중복 도착은 observation 수, history 길이 또는 candidate 수를 증가시키지 않는다.

같은 source_key에 서로 다른 canonical payload가 도착한 경우에는 다음과 같이 처리한다.
a. source가 불변 revision sequence를 제공하고 모든 revision이 finalization 전에 도착한 경우에는 가장 큰 revision sequence를 canonical payload로 한다.
b. revision sequence가 없거나 같은 revision sequence의 payload가 충돌하면 해당 source bundle을 CONFLICT로 확정한다.
c. CONFLICT가 하나라도 포함된 심볼-epoch는 complete로 판정할 수 없다. "마지막 값 우선", 운영자 수기 선택, 평균 또는 임의의 tie-break는 금지한다.

심볼별 epoch ledger의 primary key는 다음과 같다.
symbol_epoch_key = (study_id, policy_hash, symbol, decision_epoch_utc)
basket epoch ledger의 primary key는 다음과 같다.
basket_epoch_key = (study_id, policy_hash, decision_epoch_utc)
따라서 한 심볼은 한 epoch에 최대 한 개의 symbol row를, basket은 한 epoch에 최대 한 개의 basket row를 가진다.

epoch e의 source row는 collector가 OPEN 상태로 생성하고, 모든 fetch·retry 시도는 별도 attempt row로 append한다. retry는 기존 attempt를 수정하거나 삭제할 수 없다.

epoch e의 최종화 시각은 다음과 같다.
finalize_at(e) = e + 4h
finalize_at(e) 이전의 retry 횟수와 간격은 집행 구현이 정할 수 있으나, 모든 attempt의 attempted_at, request identity, response hash, terminal status를 기록하여야 한다. 통계적 포함 여부는 retry 횟수가 아니라 finalize_at(e)의 canonical 상태만으로 결정한다.

finalize_at(e)에 deterministic evaluator는 각 symbol row를 정확히 다음 셋 중 하나로 확정한다.
FINAL_COMPLETE: 필요한 모든 current source가 존재하고 finite이며 schema·PIT·dedup 검사를 통과함.
FINAL_MISSING: 필요한 source 중 하나 이상이 없거나 non-finite임.
FINAL_CONFLICT: 같은 source identity에 서로 충돌하는 canonical payload가 존재함.

FINAL_MISSING 또는 FINAL_CONFLICT로 확정된 epoch는 사후 backfill로 FINAL_COMPLETE가 될 수 없다. finalization 후 도착한 자료는 LATE_ONLY correction ledger에 append하되 eligibility, score, threshold, candidate, event count, phase bucket 또는 판정을 변경할 수 없다.

결측의 "회복"은 과거 epoch를 고치는 것을 뜻하지 않는다. 결측 이후 최초로 모든 current source가 정상인 새로운 epoch가 독립적으로 FINAL_COMPLETE가 되는 것만을 회복으로 인정한다.

§3.3 History, eligibility, current-exclusion 및 comparator
심볼 s, epoch e의 OI feature reference history는 event-time이 e보다 작은 finite observation 중 가장 최근 252개로 한다.
H_OI(s,e) = latest_252{oi_feature(s,u): u<e, FINAL_COMPLETE}
history는 "직전 252개 scheduled epoch"가 아니라 "직전 252개 immutable finite observation"으로 정의한다. 결측 epoch를 0으로 삽입하거나 다른 값으로 대체하지 않으며, 한 observation을 둘 이상의 event-time identity로 복제할 수 없다.

|score| reference history도 같은 방식으로 정의한다.
H_C(s,e) = latest_252{|C_s,u|: u<e, strict_score_eligible(s,u)=1}
현재 epoch의 |C_s,e|는 H_C(s,e)에 포함하지 않는다.

oi_reference_ready(s,e)=1은 |H_OI(s,e)|=252일 때에만 성립한다. current source가 complete하더라도 prior observation이 252개 미만이면 상태는 REFERENCE_NOT_READY이며 MISSING 또는 candidate false로 기록하지 않는다.

strict_score_eligible(s,e)=1은 다음 조건이 모두 참일 때에만 성립한다.
a. epoch e의 symbol row가 FINAL_COMPLETE이다.
b. oi_reference_ready(s,e)=1이다.
c. 변경 불가능한 §0~§2 feature 함수로 계산한 현재 score가 finite이다.

candidate_reference_ready(s,e)=1은 strict_score_eligible(s,e)=1이고 |H_C(s,e)|=252일 때에만 성립한다.

세 심볼 모두에 대해 candidate_reference_ready(s,e)=1일 때에만 basket_candidate_evaluable(e)=1로 한다. 두 심볼만 준비된 epoch, source가 완전하지만 한 심볼의 reference가 부족한 epoch 및 한 심볼의 score가 non-finite인 epoch는 모두 basket candidate를 판정하지 않는다.

H_C(s,e)를 오름차순으로 정렬한 값을 0-based index로 a[0],…,a[251]이라 할 때 linear-interpolated Q0.75는 다음과 같이 계산한다.
q_s,e = a[188] + 0.25 × (a[189] − a[188])
quantile 계산 전에 값을 반올림하거나 binning할 수 없다.

심볼별 tail comparator는 inclusive로 한다.
tail_s,e = 1  iff  |C_s,e| >= q_s,e
현재 값이 threshold와 정확히 같은 tie는 tail에 포함한다. >로 바꾸거나 tie를 무작위 처리할 수 없다.

basket candidate indicator x_e의 심볼 결합법과 방향 정의는 변경하지 않은 §0~§2의 candidate 함수에 따른다. 다만 x_e는 basket_candidate_evaluable(e)=1인 epoch에서만 0 또는 1을 가질 수 있다. 그렇지 않은 epoch의 원래 값은 NA이다.

feasibility count를 계산할 때 사용할 값은 다음과 같이 별도로 정의한다.

   x_e^gate = 1 if basket_candidate_evaluable(e)=1 and x_e=1;
   x_e^gate = 0 otherwise.
   따라서 missing·reference-not-ready epoch를 denominator에서 삭제하여 incidence를 높일 수 없다. 해당 epoch는 gate count에는 0을 기여하고 동시에 completeness ledger에는 결측 또는 비평가 가능 상태로 남는다.
§3.4 T_SCORE, T_CAND 및 pre-freeze hard bound
최초 synchronized strict-score epoch는 다음과 같다.
T_SCORE = min{e>=T0: strict_score_eligible(s,e)=1 for all three symbols}

최초 synchronized strict tail-candidate 평가 가능 epoch는 다음과 같다.
T_CAND = min{e>=T0: basket_candidate_evaluable(e)=1}
T_CAND는 x_e=1인 최초 epoch가 아니다. T_CAND에서 candidate가 false이더라도 그 epoch가 최초 평가 가능 epoch이면 T_CAND로 확정한다.

pre-T0 clock history의 봉인 floor min_s h_s=179를 사용한 절대 상한은 다음과 같다.
T_SCORE_DEADLINE = T0 + (252−179)×4h = T0 + 12d4h
T_CAND_DEADLINE = T_SCORE_DEADLINE + 252×4h = T0 + 54d4h
따라서:
T_CAND_DEADLINE = 2026-09-19T04:00:00Z

T_CAND <= T_CAND_DEADLINE이면 deadline 조건을 통과한다. equality는 통과한다.

T_CAND_DEADLINE까지 T_CAND가 확정되지 않으면 즉시 PRE_FREEZE_ELIGIBILITY_TIMEOUT으로 종료한다. 이 경우:
a. T0, T_CAND, T_WEEK0, T_FREEZE를 뒤로 이동시킬 수 없다.
b. 새 market regime를 기다렸다가 같은 seal로 다시 시작할 수 없다.
c. 두 심볼 또는 한 심볼만으로 진행할 수 없다.
d. later backfill로 timeout 판정을 취소할 수 없다.
e. 재시도하려면 새로운 T0, 새로운 contract hash 및 새로운 사전등록을 가진 별도 연구로 시작하여야 한다.

§3.5 ISO-week calendar와 절대 종료시각
T_WEEK0는 event-time 기준으로 T_CAND 이상인 가장 이른 ISO-8601 Monday 00:00:00 UTC로 한다.
T_WEEK0 = min{t>=T_CAND: t is Monday 00:00:00 UTC}
T_CAND가 정확히 Monday 00:00 UTC이면 T_WEEK0=T_CAND이다. evaluator가 T_CAND를 실제로 알게 된 wall-clock 시각은 이 앵커를 바꾸지 않는다.

[T_CAND,T_WEEK0)의 partial head는 ledger에 기록하되 run-in count, DEFF, feasibility calibration, outcome sample 및 최종 추론에는 포함하지 않는다.

freeze 시각은 다음과 같다.
T_FREEZE = T_WEEK0 + 84d
[T_WEEK0,T_FREEZE)는 정확히 12개의 complete ISO week, 504개의 scheduled 4h epoch로 구성되어야 한다.

세 incidence audit 시각은 다음과 같다.
T_A1 = T_FREEZE + 28d
T_A2 = T_FREEZE + 56d
T_A3 = T_FREEZE + 84d

freeze-anchored hard deadline은 다음과 같다.
T_HARD_FREEZE = T_FREEZE + 365d

T0 기준 universal hard cap은 다음과 같다.
T_HARD_ABSOLUTE = T0 + 510d = 2027-12-19T00:00:00Z
실제 hard deadline은 다음 중 이른 값이다.
T_HARD = min(T_HARD_FREEZE, T_HARD_ABSOLUTE)
어떠한 missingness, collector 지연, 재처리 또는 calendar reset도 T_HARD를 늦출 수 없다.

T0=2026-07-27T00:00Z와 봉인된 초기 history에서 uninterrupted 조건의 실제 달력은 다음과 같다.
T_CAND = 2026-09-19T00:00Z 또는 2026-09-19T04:00Z
T_WEEK0 = 2026-09-21T00:00Z
T_FREEZE = 2026-12-14T00:00Z
T_A1 = 2027-01-11T00:00Z
T_A2 = 2027-02-08T00:00Z
T_A3 = 2027-03-08T00:00Z
T_HARD_FREEZE = 2027-12-14T00:00Z
따라서 실제 T0→T_HARD는 505일, 365.25/12 일/월 기준 16.5914개월이다.

일반적인 ISO-week alignment를 포함한 계약상 범위는 503~510일, 즉 16.53~16.76개월이며 510일이 절대 상한이다. 이 범위는 planning estimate가 아니라 deadline 조문에서 직접 귀결되는 상한이다.

§3.6 Event-time phase assignment과 수정 불가 필드
모든 basket epoch는 오직 decision_epoch_utc에 따라 다음 phase 중 하나에 귀속한다.
PRE_CAND: e<T_CAND
PARTIAL_HEAD: T_CAND<=e<T_WEEK0
RUNIN: T_WEEK0<=e<T_FREEZE
TRIAL_A1: T_FREEZE<=e<T_A1
TRIAL_A2: T_A1<=e<T_A2
TRIAL_A3: T_A2<=e<T_A3
POST_A3: T_A3<=e<T_HARD

interval은 모두 half-open이다. 따라서 e=T_FREEZE인 event는 run-in이 아니라 post-freeze trial event이다. e=T_A1은 A1 window에 포함하지 않고 A2 구간에 포함한다.

phase assignment에는 received_at, retry 시각, evaluator 실행시각 또는 backfill 시각을 사용할 수 없다.

다음 필드는 finalization 또는 해당 anchor 확정 후 수정할 수 없다.
study_id, policy_hash, code_hash, schema_hash, symbol, decision_epoch_utc, source primary keys, source payload hashes, attempt timestamps, final source state, completeness flags, reference-ready flags, score, |score|, quantile threshold, tail boolean, basket evaluability, x_e, x_e^gate, phase, run-in/trial bucket, exclusion reason, canonical event hash.

T_CAND, T_WEEK0, T_FREEZE, T_A1, T_A2, T_A3, T_HARD가 확정되면 anchor ledger에 append하고 수정 불가로 한다.

운영자와 분석자는 기존 row를 UPDATE 또는 DELETE할 수 없다. 설명이 필요한 경우 기존 row의 hash를 참조하는 audit_annotation을 append할 수 있을 뿐이며 annotation은 계산 입력이 아니다.

§3.7 Completeness, critical gap 및 symbol share의 분자·분모
window W=[a,b)의 scheduled epoch 집합은 다음과 같다.
E(W) = {e: e is a 4h UTC decision epoch and a<=e<b}
N_sched(W)=|E(W)|

심볼별 source completeness는 다음과 같다.
N_complete_s(W)=sum_e 1[symbol row (s,e) is FINAL_COMPLETE]
Comp_s(W)=N_complete_s(W)/N_sched(W)

synchronized source completeness는 다음과 같다.
N_sync(W)=sum_e 1[all three symbol rows at e are FINAL_COMPLETE]
Comp_sync(W)=N_sync(W)/N_sched(W)

candidate-evaluable completeness는 다음과 같다.
N_eval(W)=sum_e 1[basket_candidate_evaluable(e)=1]
Comp_eval(W)=N_eval(W)/N_sched(W)

feasibility OC가 매 ISO week 42개의 trial을 전제로 하므로 [T_WEEK0,T_A3)에서는 다음 조건을 요구한다.
N_complete_s(W)=N_sched(W) for every symbol;
N_sync(W)=N_sched(W);
N_eval(W)=N_sched(W).
즉 comparator는 정확한 100% equality이다. 95%, 반올림된 100.0% 또는 available-case denominator는 허용하지 않는다.

critical_gap은 [T_WEEK0,T_A3)에서 basket_candidate_evaluable(e)=0인 연속 scheduled epoch들의 maximal nonempty run으로 정의한다.
critical_gap_count(W)은 그러한 maximal run의 수이고,
critical_gap_epochs(W)는 그 run들에 속한 epoch 수의 합이며,
critical_gap_rate(W)=critical_gap_epochs(W)/N_sched(W)이다.
통과 조건은 다음과 같다.
critical_gap_count(W)=0
critical_gap_epochs(W)=0
equality 0만 통과하며 한 epoch라도 존재하면 DATA_INTEGRITY_FAIL이다.

T_WEEK0 이후 어떤 epoch가 FINAL_MISSING, FINAL_CONFLICT 또는 candidate non-evaluable로 확정되면 다음 audit까지 기다리지 않고 즉시 DATA_INTEGRITY_FAIL을 기록할 수 있다. 이 조기 종료는 calendar를 정지하거나 deadline을 연장하지 않는다.

symbol contribution indicator를 z_s,e라 한다. z_s,e=1은 변경 불가능한 candidate 함수에서 symbol s가 basket candidate x_e=1에 실제로 기여했음을 뜻한다. 둘 이상의 심볼이 같은 basket event에 기여하면 각 심볼에 한 contribution을 기록한다.

누적 window W의 symbol contribution count와 denominator는 다음과 같다.
A_s(W)=sum_e x_e^gate × z_s,e
A_total(W)=sum_s A_s(W)
Share_s(W)=A_s(W)/A_total(W)
따라서 여러 심볼이 동시에 기여한 event는 numerator와 denominator 양쪽에 같은 수의 contribution을 추가하며, 세 Share_s의 합은 1이다.

A_total(W)=0이면 symbol share는 평가 불능이며 해당 concentration 조건은 FAIL로 판정한다. A_total(W)>0이면 통과 조건은 다음과 같다.

   max_s Share_s(W) <= 0.60
   equality 60%는 통과한다. floating-point 반올림 대신 각 심볼에 대해 정수식 5×A_s <= 3×A_total을 사용한다.
completeness와 symbol-share 조건은 §4의 beta-binomial count-gate OC에 포함된 확률변수가 아니다. 따라서 §4의 90.023146%는 본 §3.7 조건이 충족된다는 조건부 count-gate OC이며 overall operational survival probability로 확대 해석할 수 없다.
§3.8 Pre-candidate 구간의 허용 산출물
[T0,T_SCORE)에서는 source arrival, schema validation, source completeness, conflict, latency 및 history count만 보고할 수 있다.

[T_SCORE,T_CAND)에서는 score 분포, |score| 분포, 세 심볼 score 상관, source completeness 및 reference-history 증가를 보고할 수 있다.

[T_SCORE,T_CAND)에서는 strict tail candidate incidence, candidate/day, P(continue), alpha outcome, PnL, forward return 또는 방향 적중률을 계산하거나 보고할 수 없다.

[T_CAND,T_WEEK0)에서는 candidate를 shadow ledger에 기록할 수 있으나 feasibility count, DEFF, outcome sample 또는 calibration에 포함하지 않는다.

위 어느 구간의 관찰도 candidate threshold, 심볼 결합법, DEFF_plan, stage threshold, deadline 또는 outcome 정의를 변경하는 근거로 사용할 수 없다.

§4. 고정 DEFF, feasibility gate 및 재현 가능한 calibration
§4.1 DEFF binding rule
표본수·기간·feasibility gate에 사용하는 유일한 binding factor는 다음 상수이다.
D_BIND = DEFF_plan = 2.06

raw target은 다음과 같다.
n_raw = ceil(608×2.06) = 1,253

D_BIND는 T_FREEZE, T_A1, T_A2, T_A3 또는 이후 outcome observation에서 재추정하거나 상향·하향 rebinding하지 않는다.

live incidence에서 계산한 D_hat_m, Bartlett/HAC 값, phase sensitivity 및 shrinkage posterior는 모두 diagnostic이다. 그 값이 2.06보다 작거나 크더라도 n_raw, H_j, T_HARD 또는 outcome threshold를 변경할 수 없다.

ISO-week diagnostic estimator는 봉인 계약 2를 그대로 따른다. m개의 complete ISO week가 있을 때 weekly event count를 K_1,…,K_m이라 하고:
p_hat_m = sum(K_w)/(42m)
s_m² = sample_variance(K_1,…,K_m, ddof=1)
D_hat_m = s_m²/[42 p_hat_m(1−p_hat_m)]
로 계산한다.

m<2이거나 p_hat_m∈{0,1}이면 operational diagnostic D_hat_m=NA로 보고한다. 이를 0, 1 또는 2.06으로 impute하지 않는다. D_BIND에는 영향이 없다.

D_BIND=2.06은 forecast-incidence dependence의 planning multiplier이며 alpha outcome의 최종 DEFF 추정치가 아니다. outcome DEFF는 현재 미측정 상태로 유지한다.

§4.2 Stage count, projection statistic 및 comparator
네 stage를 다음과 같이 고정한다.
stage j    m_j    d_j    stage time
F    12    0    T_FREEZE
A1    16    28    T_A1
A2    20    56    T_A2
A3    24    84    T_A3

stage j의 누적 전체 candidate count는 다음과 같다.
C_all,j = sum{x_e^gate: T_WEEK0 <= e < T_FREEZE+d_j}

stage j의 post-freeze candidate count는 다음과 같다.
C_trial,j = sum{x_e^gate: T_FREEZE <= e < T_FREEZE+d_j}
F에서는 empty sum이므로 C_trial,F=0이다.

stage statistic은 다음 정수식으로 계산한다.
J_j = (365−d_j)×C_all,j + 7m_j×C_trial,j

동치식은 다음과 같다.
J_j = (365−d_j)×C_runin + 449×C_trial,j
단 C_runin=sum{x_e^gate:T_WEEK0<=e<T_FREEZE}이다.

event의 run-in/trial 가중치는 오직 immutable decision_epoch_utc로 정한다. receive-time, late correction 또는 계산시각으로 run-in event를 trial event로 재분류할 수 없다.

stage threshold는 다음 literal integer로 고정한다.
H_F  = 93,805
H_A1 = 127,584
H_A2 = 161,643
H_A3 = 206,770

stage j는 J_j >= H_j일 때 통과한다. equality는 통과한다. 어느 stage에서든 J_j<H_j이면 즉시 종료하며 이후 stage로 갈 수 없다.

threshold는 live count, observed rate, live D_hat, market regime 또는 이전 stage의 통과 margin을 사용하여 재calibration하지 않는다.

§4.3 Error trade-off와 authoritative OC
count-gate calibration의 good/service point는 다음과 같다.
r_CAL = 3.55 candidate/day

false-continue audit의 bad point는 다음과 같다.
r_BAD = 0.85×r_CAL = 3.0175 candidate/day

count-gate 설계 목표는 다음과 같다.
P(false stop | r=r_CAL) <= 10%
P(false continue through A3 | r=r_BAD) <= 1%

exact beta-binomial dynamic programming으로 계산한 stage별 conditional survival은 다음과 같다.

r/day    F    A1    A2    A3
3.55    99.540499%    99.198975%    98.917945%    90.023146%
3.0175    42.559607%    23.131561%    12.540302%    0.497892%

따라서 count-gate false-stop은 9.976854%, false-continue는 0.497892%이다. 90.0368% MC audit를 true OC로 인용해서는 안 되며 true OC는 90.023146%이다.

r_CAL에서 365일 동안 N_365>=1,253일 service probability는 다음과 같다.
exact convolution: 90.4675417929%
sealed MC audit: 904,693/1,000,000 = 90.4693%

authoritative P(continue) curve는 simulation이 아니라 같은 exact DP로 계산한 다음 벡터이다.

rate/day    exact P(continue through A3)
2.6    0.000001%
2.8    0.001669%
3.0    0.342934%
3.1    2.375694%
3.2    10.395910%
3.3    29.605926%
3.4    57.521051%
3.5    82.060533%
3.55    90.023146%
3.6    95.042275%
3.7    99.143974%
3.8    99.910611%
4.0    99.999799%

위 곡선은 24개의 independent weekly beta-binomial block과 constant rate를 가정한 conditional OC이다. 실제 missingness, symbol concentration, rate drift 또는 front-loaded/back-loaded regime의 확률모형을 포함하지 않는다.
§4.4 Beta-binomial model
rate r에 대해 다음 값을 binary64로 계산한다.
p = r/6.0
rho = (2.06−1.0)/(42−1)
alpha = p×(1/rho−1)
beta = (1−p)×(1/rho−1)

각 complete week w에 대해:
q_w ~ Beta(alpha,beta)
K_w | q_w ~ Binomial(42,q_w)
로 한다.

서로 다른 q_w는 독립이다. 한 주의 q_w를 다른 주나 다른 simulation repetition에 재사용하지 않는다.

stage simulation에서는 각 repetition에 24개 weekly count를 만들고:
C_all,j = sum(K_1,…,K_mj)
C_trial,j = sum(K_13,…,K_mj)
로 계산한다.

§4.5 Threshold calibration 알고리즘
calibration runtime은 다음으로 고정한다.
CPython 3.13.x
NumPy 2.4.4
numpy.random.Generator(numpy.random.PCG64(seed))
IEEE-754 binary64, single process, C-order full-array generation, chunking 없음.

calibration repetition 수는 R=1,000,000이고 seed는 다음과 같다.
CALIBRATION_SEED = 41012027

하나의 fresh Generator를 만든 다음 정확히 다음 순서로 RNG를 소비한다.
a. Q = rng.beta(alpha,beta,size=(1_000_000,24))를 전량 생성한다.
b. 그 후에만 K = rng.binomial(42,Q)를 전량 생성한다.
c. beta와 binomial draw를 week별로 interleave하지 않는다.
d. array를 chunk로 나누거나 repetition별 loop로 생성하지 않는다.

각 repetition과 stage에서 §4.2의 J_j를 계산한다.

F, A1, A2 threshold는 각각 모든 1,000,000개 marginal J_j의 lower-tail 0.5% empirical quantile로 정한다. quantile 함수는 다음과 동일하여야 한다.
numpy.quantile(J_j,0.005,method="inverted_cdf")
동치인 order-statistic 정의는 오름차순 정렬한 1,000,000개 값의 0-based index 4,999 값이다.

F threshold를 확정한 뒤 J_F>=H_F mask를 만들고, 이어 A1과 A2를 순차 적용한다. 이때 확인되어야 하는 cumulative survivor count는 다음과 같다.
995,336 -> 991,920 -> 989,089

A3 threshold는 앞의 세 threshold를 통과한 repetition에 한해 다음 규칙으로 정한다.
H_A3 = max{h in integers: count(J_A3>=h and prior stages pass) >= 900,000}
이 규칙에서 H_A3=206,770이면 survivor count는 900,012, H_A3=206,771이면 899,981이어야 한다.

위 알고리즘이 literal threshold 또는 survivor count를 재현하지 못하면 threshold를 새 결과로 교체하지 않고 CALIBRATION_REPRODUCTION_FAIL로 봉인 활성화를 중단한다.

§4.6 모든 simulation의 seed·shape·소비 순서
service-level MC는 다음 계약을 따른다.
SERVICE_SEED = 41012026
R = 1,000,000
Q = rng.beta(alpha,beta,size=(1_000_000,53))를 전량 생성한다.
n = int64([42 repeated 52 times, 6])로 한다.
그 뒤 K = rng.binomial(n,Q)를 전량 생성한다.
N_365 = row_sum(K)로 하고 N_365>=1,253의 raw count를 게시한다.
기대 raw count는 904,693이다.

good-point MC audit는 다음 계약을 따른다.
GOOD_AUDIT_SEED = 41012028
r=3.55, R=1,000,000, shape (1_000_000,24).
beta array 전량 생성 후 binomial array 전량 생성한다.
cumulative stage survivor count는:
995,484 -> 992,115 -> 989,378 -> 900,368
이어야 한다.

bad-point MC audit는 다음 계약을 따른다.
BAD_AUDIT_SEED = 41012029
r=3.0175, R=1,000,000, shape (1_000_000,24).
같은 소비 순서를 사용한다.
cumulative stage survivor count는:
425,825 -> 231,328 -> 125,545 -> 5,117
이어야 한다.

optional curve MC audit를 게시할 경우 다음 계약만을 사용할 수 있다.
CURVE_SEED = 41012030
R_curve = 500,000 per rate
rate order는 정확히:
[2.6,2.8,3.0,3.1,3.2,3.3,3.4,3.5,3.55,3.6,3.7,3.8,4.0]
로 한다.

curve MC는 하나의 fresh Generator를 CURVE_SEED로 한 번만 초기화하고, 위 rate order대로 각 rate의 (500_000,24) beta array 전량, 같은 shape binomial array 전량을 생성한 후 다음 rate로 이동한다. rate마다 Generator를 재초기화하거나 병렬 실행하거나 interleave할 수 없다.

curve의 의사결정상 authoritative 값은 §4.3의 exact DP이다. curve MC는 구현 audit일 뿐 threshold 또는 판정을 변경하지 않는다.

R4.1b에 실렸던 seed 없는 500k/rate MC curve vector는 봉인 대상에서 삭제한다. 그 vector에 사후적으로 seed를 맞추거나 conclusion-aligned seed를 검색할 수 없다.

simulation 결과를 게시할 때에는 percentage만 게시하지 않고 반드시 seed, runtime, array shape, draw order, raw numerator, repetition denominator를 함께 게시한다.

§4.7 Shrinkage family의 완전 명세와 비binding 지위
shrinkage family는 operational rule이 아니라 "live D_hat를 binding target에 결합했을 경우의 same-sample feedback"을 진단하기 위한 사전 고정 audit family로만 둔다.

family grid는 다음과 같이 고정한다.
nu in {5,26,100,250,infinity}
결과를 본 뒤 grid를 추가·삭제하거나 10% 경계를 통과하는 nu를 탐색할 수 없다.

simulated path의 첫 m주 weekly count를 K_1,…,K_m이라 할 때:
p_hat_m = sum(K_w)/(42m)
s_m² = sample_variance(K_1,…,K_m,ddof=1)
로 한다.

0<p_hat_m<1이면:
D_hat_m = s_m²/[42 p_hat_m(1−p_hat_m)]
로 한다.

p_hat_m=0 또는 p_hat_m=1이면 D_hat_m=+infinity로 한다. 이 edge에서 denominator를 0, 1, 2.06 또는 임의 cap으로 대체할 수 없다.

finite nu>2에 대한 shrinkage family는 다음과 같다.
D_m^(nu) = [(nu−2)×2.06 + (m−1)×D_hat_m]/(nu+m−3)

nu=infinity는 limit notation으로 다음과 같이 정의한다.
D_m^(infinity)=2.06
이 family에서는 D_hat_m를 target에 사용하지 않는다.

path·stage별 raw target은 다음과 같다.
N_m^(nu)=ceil(608×D_m^(nu))
D_m^(nu)=+infinity이면 N_m^(nu)=+infinity이다.

stage target과 projection statistic의 결합은 다음과 같다.
L_j^(nu)=J_j−7m_j×N_mj^(nu)
N=+infinity인 path는 L=-infinity로 하며 해당 stage를 통과하지 못한다.

family별 calibration offset B_j^(nu)는 r_CAL=3.55에서 다음 순서로 정한다.
a. F, A1, A2의 B_j^(nu)는 각 marginal L_j^(nu)의 numpy.quantile(...,0.005,method="inverted_cdf")로 한다.
b. 이 세 threshold를 순차 적용한다.
c. B_A3^(nu)는 prior-stage survivor 중 전체 원 repetition 기준 survivor가 최소 900,000이 되는 가장 큰 정수로 한다.
d. family의 stage criterion은 L_j^(nu)>=B_j^(nu)이다. equality는 통과한다.

family calibration simulation은 다음 계약을 따른다.
SHRINK_CALIBRATION_SEED = 41012031
R=1,000,000, r=3.55, shape (1_000_000,24).
beta array 전량 생성 후 binomial array 전량 생성한다.
하나의 K array를 모든 nu family가 공통으로 사용하며 nu별 추가 RNG를 소비하지 않는다.

bad-point family audit은 다음 계약을 따른다.
SHRINK_BAD_AUDIT_SEED = 41012032
R=1,000,000, r=3.0175, shape (1_000_000,24).
같은 generation·consumption 순서를 사용한다.

historical 42-phase 값은 D0=2.06의 provenance와 sensitivity로만 사용한다. 42개 값을 random prior sample로 resample하거나 live path에 맞춰 weighting하지 않는다.

이 family의 어떠한 결과도 D_BIND, n_raw, operational H_j 또는 calendar를 변경하지 않는다.

R4.1b에 게시된 shrinkage percentage vector는 정확한 family·edge·coupling·seed가 없었으므로 봉인 근거에서 철회한다. "shrinkage 실험이 DEFF ratchet 폐기와 fixed 2.06을 입증했다"고 서술할 수 없다.

fixed 2.06의 근거는 다음 두 가지로 제한한다.
a. 봉인 계약 2가 정한 독립적인 planning multiplier라는 점.
b. live rate와 live D_hat를 같은 표본에서 target에 feedback하는 경로를 차단한다는 계약적 독립성.

§5. Stage adjudication, audit 및 terminal rule
§5.1 판정 순서
F, A1, A2, A3 판정은 각 stage time에 정확히 한 번 수행한다. stage window는 stage time 미만의 epoch만 포함하므로 마지막 포함 epoch는 stage time 4시간 전에 시작하며 stage time에 finalization이 완료되어야 한다.

각 stage adjudicator는 다음 순서를 변경하지 않는다.
a. stage window에 포함되는 모든 source·symbol·basket row의 finalization 여부를 확인한다.
b. ledger chain과 manifest hash를 검증한다.
c. §3.7 completeness와 critical-gap 조건을 계산한다.
d. symbol contribution count와 max_symbol_share를 계산한다.
e. C_all,j, C_trial,j, J_j를 계산한다.
f. literal H_j와 inclusive comparator로 count-gate를 판정한다.
g. stage adjudication record를 append하고 hash를 확정한다.

단계 a~d 중 하나라도 FAIL이면 J_j가 threshold를 넘더라도 stage는 FAIL이다. 단계 a~d가 PASS이고 J_j>=H_j일 때에만 해당 stage를 통과한다.

stage 판정 후 raw source correction, 재실행, 다른 quantile method 또는 다른 seed로 더 유리한 판정을 선택할 수 없다.

F에서 FAIL이면 A1~A3를 계산하지 않는다. A1에서 FAIL이면 A2~A3를 계산하지 않는다. A2에서 FAIL이면 A3를 계산하지 않는다. A3에서 PASS한 경우에만 변경하지 않은 §6~§8의 outcome 절차로 이동한다.

outcome, forward return, PnL 및 방향 적중 label은 A3 adjudication record의 hash가 확정될 때까지 접근할 수 없다. A3 FAIL 후 outcome을 열더라도 그 행위는 본 연구 밖의 사후 분석이며 판정을 변경하지 않는다.

§5.2 Adjudication record와 감사
각 stage record에는 최소한 다음 필드를 포함한다.
study_id, stage_id, stage_time, policy_hash, code_hash, ledger_root_hash, source_manifest_hash, N_sched, 각 N_complete_s, N_sync, N_eval, 각 completeness ratio의 정수 분자·분모, critical_gap_count, critical_gap_epochs, 각 A_s, A_total, max_symbol_share, C_all, C_trial, J, H, comparator, integrity decision, concentration decision, count-gate decision, cumulative decision, computed_at, evaluator identity.

percentage나 rounded display는 판정 입력이 아니다. 모든 comparator는 원래 integer numerator·denominator 또는 literal integer J·H로 수행한다.

ledger는 각 canonical row에 대해 다음 hash를 가진다.
row_hash = SHA256(canonical_json(row_without_hash))
시간순 chain은 다음과 같이 계산한다.
chain_hash_i = SHA256(chain_hash_(i−1) || row_hash_i)
genesis 값과 canonical row order는 합본 봉인 시 고정한다.

collector는 raw attempt와 source row를 기록하는 주체이고, deterministic evaluator는 eligibility·score·candidate·phase row를 기록하는 주체이며, stage adjudicator는 stage record를 기록하는 주체이다. 한 주체가 다른 주체의 기존 row를 수정할 수 없다.

독립 감사자는 raw source ledger에서 다음을 재구현하여야 한다.
source deduplication, epoch completeness, reference histories, current-excluded quantile, tail comparator, basket evaluability, x_e^gate, phase assignment, completeness 분자·분모, symbol contribution, C_all, C_trial, J_j, stage decision.

독립 감사 결과가 canonical stage record와 하나라도 다르면 운영자의 수기 판정으로 어느 쪽을 선택하지 않는다. 연구는 LEDGER_REPRODUCTION_FAIL로 fail-closed 종료하며 같은 seal에서 코드를 고쳐 다시 판정할 수 없다.

WORM 저장, DB constraint 또는 그와 동등한 저장장치로 UPDATE·DELETE를 차단하여야 한다. 기술적으로 이를 보장하지 못하면 합본 seal을 활성화할 수 없다.

§5.3 Terminal state와 무연장 원칙
다음 상태 중 하나가 발생하면 연구는 즉시 terminal이다.
INITIAL_HISTORY_FAIL
PRE_FREEZE_ELIGIBILITY_TIMEOUT
CONTRACT_VERSION_CHANGED
DATA_INTEGRITY_FAIL
CALIBRATION_REPRODUCTION_FAIL
LEDGER_REPRODUCTION_FAIL
FEASIBILITY_FAIL_F
FEASIBILITY_FAIL_A1
FEASIBILITY_FAIL_A2
FEASIBILITY_FAIL_A3
T_HARD_REACHED

terminal state 이후에는 collector를 계속 운영할 수 있으나 추가 자료는 본 연구의 판정·표본·calendar에 포함하지 않는다.

source 장애, 낮은 candidate rate 또는 불리한 market regime은 deadline 연장 사유가 아니다.

terminal 후 다음 행위를 금지한다.
a. T0 또는 T_CAND를 새로 선택하는 행위.
b. 더 유리한 ISO-week anchor를 선택하는 행위.
c. run-in event를 trial event로 재분류하는 행위.
d. 실패한 심볼을 제거하거나 대체하는 행위.
e. DEFF_plan 또는 raw target을 낮추는 행위.
f. stage threshold를 낮추거나 comparator를 >/>= 사이에서 바꾸는 행위.
g. outcome을 본 뒤 missing row를 복구하거나 eligibility를 바꾸는 행위.

T_HARD까지 변경하지 않은 §6~§8의 최종 표본·판정 조건을 충족하지 못하면 T_HARD_REACHED로 종료한다. T_HARD 이후 표본을 보충하여 같은 연구의 판정을 내릴 수 없다.

§5.4 합본 시 supersession
본 §3~§5가 합본에 들어가면 R4.1b의 14줄짜리 §3~§5 요약은 전부 삭제하고 본 조문만을 normative text로 사용한다.

다음 수치는 그대로 유지한다.
DEFF_plan=2.06
n_raw=1,253
H_F=93,805
H_A1=127,584
H_A2=161,643
H_A3=206,770
r_CAL=3.55/day
exact A3 survival at r_CAL = 90.023146%
exact A3 survival at r_BAD = 0.497892%

봉인 계약 1·2, R4.1 §0~§2 및 §6~§8과 본 조문이 충돌할 경우, 봉인 계약 1·2와 변경 금지 절이 우선하고 본 §3~§5는 그 범위 안에서만 해석한다.

### R4.1 §6~§8

(§6 OUTCOME DEPENDENCE & LOOKS: ISO week 배정·CR1 SE·t_{G−1}·min 12 clusters·LOOK1=3 audit 통과
AND raw≥ceil(152·D_sched)·LOOK2=raw≥ceil(304·D_sched)·early success 금지·HARM=UCB97.5<0·
σ_CP=max(220,SE_CR1√I_k)·CP<0.10 futility·stop 시 파라미터 변경 일체 금지)

(§7 FINAL ANALYSIS: raw≥ceil(608·D_sched) AND 성숙 AND ≤T_F+365d AND integrity·미달 시
NOT_DISCRIMINABLE_CALENDAR(미달 상태 alpha 검정 금지)·PRIMARY/ECONOMIC/PF17(+∞·0 엣지 정의)/
28d block(T_F 앵커 non-overlap complete만·≥60%)/month concentration(분모 0=실패·≤50%)/symbol
share ≤60%/transfer(RT≥100·drag≤2bp)/integrity(결정론적 멱등 재시도는 비치명·eligibility/reference/
방향/outcome 변경 재시도는 치명)·FINAL_TRACK_LABEL 5종·composite에 80% joint power 주장 금지·
true +25bp에서 mean-gate joint 확률 50% 상한 명시)

(§8 REPORTING & PROHIBITIONS: 2-of-3 수치는 창·계약 1·가정 여부 병기·p_tail=realized calibration
한정·DEFF=incidence 기준+ISO 앵커+outcome 미측정 병기. 금지: threshold 완화·signed 대체·방향 반전·
TP/SL 추가·심볼 확장·히스토리컬 재개·outcome imputation·2-of-3를 strict로 취급·incidence DEFF를
outcome DEFF로 취급·composite 80% power 주장·point≥25만으로 "true≥25 확증" 주장)

### 봉인 계약 1

## 계약 1 — tail 판정 기준 = `Q0.75(|C|)` (봉인 완료)

### 조문

```
tail 판정 기준 = 직전 252개 4h 관측의 |score| 분포에 대한 linear-interpolated Q0.75.
현재 관측치는 분포에서 제외한다(PIT current-exclusion).
signed score 의 Q0.75 는 채택하지 않는다.
```

### 채택 근거 (운영자 승인, 4개)

1. GPT R4 원문이 `|C|` 를 명시한다
2. GPT 의 빈도 산술 전체가 `p_tail ≈ 25%` 를 전제한다 — signed 해석이면 약 51% 가 되어 **자기모순**
3. T2.5 구현이 `|C|` 기준과 일치한다
4. 이 선택은 **PnL 을 열람하지 않은 상태에서 이루어졌다** — 결과 기반 선택이 아니다

signed 해석은 "다른 해석"이 아니라 **다른 설계**이므로 기각한다.

### 🔴 이 계약이 필수인 이유 (적대검증 발견)

해석을 signed 로 반전하면 측정값이 이렇게 바뀐다:

```
              basket        forecasts/day    p_tail
|C| 기준      566/1116      3.043011         24.28~24.73%
signed 기준   946/1116      5.086022         50.90~52.96%
```

**빈도가 67% 증가하고 p_tail 이 두 배가 된다.** 계약을 명시하지 않으면 어떤 빈도도 인용할 수 없다.

### 🔴 함께 봉인할 서술 제약 — 항진명제 주의

`p_tail ≈ 24.3~24.7%` 를 **"외부 가정 25% 가 데이터로 독립 확증됐다"고 서술하면 안 된다.**

규칙 자체가 rolling `Q0.75(|C|)` tail 이므로, 정상성·교환가능성 아래 **약 25% 가 기계적으로 유도된다.**
이 수치는 **해당 adaptive rule 의 realized calibration** 으로만 유효하다.

→ R4.1 의 빈도 주장은 **비항진 측정치**(basket incidence `3.043011/day` · score 상관 `0.456062` ·
DEFF)에만 세운다. `p_tail` 은 보조 관찰로만 인용한다.

### 봉인 계약 2

## 계약 2 — DEFF 추정자 규약 (봉인 · 운영자 승인 2026-07-27)

### 조문

```
[봉인용 추정자]
  비중첩 7일 block variance ratio.
  block 경계 앵커 = ISO week Monday 00:00 UTC.
  (이 corpus 에서 phase 27, complete block 25개, ratio 1.981994695058)

[기획용 수치]
  DEFF_plan = 2.06
  = 42-phase 관측 최대치(2.055978156119)의 올림
```

### 🔴 이 수치가 답하지 못하는 것 (함께 봉인)

이 corpus 는 **forecast 발생 binary series** 다. 따라서 위 DEFF 는
**forecast-incidence dependence input / 기획치**이지 **alpha outcome 의 최종 DEFF 가 아니다.**

outcome dependence 는 outcome 을 열지 않고는 원리적으로 식별할 수 없고,
이 트랙은 PnL-blind 이므로 **측정되지 않았다.** R4.1 은 수치와 함께 이 제한을 봉인해야 하며,
GPT 에게 **outcome DEFF 를 어떻게 처리할지**를 명시적으로 묻는다.

## 인용 시 필수 표기

패킷의 모든 수치는 다음을 병기한다:

```
①측정 창  ②구현 계약(계약 1·2 참조)  ③가정 여부(ASSUMPTION 명시)
```

`3.043011/day` · `0.456062` 는 **"계약 1 조건부"** 로만 인용한다.
`p_tail` 은 realized calibration 보조 관찰로만 인용한다.
DEFF 는 **"forecast-incidence 기준, outcome DEFF 미측정"** 을 병기한다.
