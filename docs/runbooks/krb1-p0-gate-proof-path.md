# KR-B1 P0-3 selector gate 증명 경로 (ROB-1172)

07-29 16:37 selector 실행은 데이터가 충분했는데도 `fail_closed` 로 후보 0건을 냈다
(KOSPI coverage 2,100/2,100 · 3,921행 · 16:30 이후 537행 · duplicate 0). 막은 것은
데이터가 아니라 **증명 수단의 부재**였다. 이 문서는 3개 gate 를 증명할 수 있는 경로와,
증명 못 하면 그대로 막히는 계약을 정리한다.

**게이트는 완화되지 않았다.** 상한(`<= decision_at`)이 추가됐고, 권위 source allowlist·
raw provenance 요구·전수 coverage 요구는 그대로다.

## 0. decision_at — 모든 gate 의 상한

selector 는 `--decision-at` (offset 포함 ISO-8601) 를 필수로 받는다. 모든 증거 시계는
`decision_at` 이하여야 하고, `decision_at` 자체도 다음 창 안에 있어야 한다.

```
as_of_session 15:35 KST  <=  decision_at  <  target_session 09:00 KST
```

| 실패 사유 | 의미 |
|---|---|
| `decision_at_must_be_timezone_aware` | naive 시계는 비교 자체가 추측이다 |
| `decision_at_before_completed_session_cutoff` | 완료 세션이 아직 없다 |
| `decision_at_not_before_target_session_open` | 이미 대상 세션이 열렸다 |

**늦게 채우는 것은 그 시점 상태의 증명이 아니다.** 07-30 metadata 로 07-29 결정을 정당화하는
경로는 상한으로 차단된다.

## 1. AC1 — Toss authoritative metadata snapshot (append-only)

```bash
ENV_FILE=.env.prod uv run python -m scripts.krb1_p0_metadata_snapshot_capture \
  --as-of-session 2026-07-29 \
  --decision-at 2026-07-29T18:00:00+09:00
```

🔴 **회수 시각은 권위 시계가 아니다 (2026-07-30 08:33 교정).** 초기 구현은 provider clock
부재를 `metadata_as_of = retrieved_at` 로 메우고 `authority_clock_source=http_retrieval`
라벨을 붙였다. 그러면 **07-28 vintage 본문을 07-29 에 GET 만 해도 07-29 기준 권위로
통과**한다 — 이 gate 가 잡아야 할 stale 그 자체다. 라벨은 권위를 만들지 않는다.

지금 계약:

- 권위 시계는 **provider 응답 필드에서만** 나온다. `ProviderAuthorityClock` 은
  publication 시각·effective session 각각의 **필드명 + 원문 문자열**을 함께 보존하고,
  로컬 시계로는 구성 자체가 불가능하다(빈 필드명·naive 시각 거부).
- 추출은 `extract_provider_authority_clock()` 뿐이며 **envelope 레벨만** 본다. 행 단위
  clock 은 universe-scope 권위가 아니므로 거부한다.
- `PROVIDER_PUBLISHED_AT_FIELDS` / `PROVIDER_EFFECTIVE_SESSION_FIELDS` 는 **빈 집합**이다.
  wired Toss `/api/v1/stocks` 투영(`TossStockInfo`)에 publication/effective 시계가 없고
  `parse_toss_response` 가 envelope 을 bare row list 로 풀어버린다. 따라서 이 capture 는
  현재 **fail-closed 가 정상**이며(`provider_authority_clock_absent`) 아무것도 append 하지
  않는다. 두 집합을 채우는 것은 provider 계약을 실측 확인한 뒤의 **별도 리뷰 변경**이고
  설정 토글이 아니다.
- 보존 항목: `raw_payload_sha256` · `raw_payload_bytes` · provider clock(필드명·원문 포함) ·
  `retrieved_at`(회수 시계, `retrieval_clock_is_not_authority: true` 로 라벨) ·
  `universe_metadata_hash` · `symbol_count` · chain provenance.
- 저장: `var/research/krb1/p0_gate_evidence/toss_metadata_snapshot.jsonl`
  (hash-chain append-only, 봉인 캠페인 저널과 **별도 genesis**).
- 저장 스키마는 `krb1.p0_3.metadata_authority.v2`. v1 row(회수-시계-권위)를 읽으면
  **예외로 거부**한다 — 조용히 읽으면 결함이 되살아난다.
- gate `metadata_authority_snapshot` 강제 조건:

```
source in {toss_openapi}
provider clock 존재 (부재 → metadata_snapshot_provider_authority_clock_missing)
universe_metadata_hash == 선택 시점 metadata 행에서 재계산한 hash
as_of_session <= provider_effective_session <= decision_at 날짜   (하한: provider 세션 기준)
provider_published_at <= retrieved_at <= decision_at              ← 🔴 상한
chain provenance (stream_id / chain_index >= 2 / chain_hash) 존재
```

- gate `metadata_authority_as_of` 는 행 단위로도 상한을 본다
  (`metadata_as_of_after_decision_at`). naive 시계는 실패로 취급한다.
- Toss 가 비활성(`TOSS_API_ENABLED` 미설정)이면 capture 는
  `toss_authoritative_master_source_unavailable` 로 fail-closed 한다.

## 2. AC2 — completion manifest (KIS raw daily ↔ DB exact reconcile)

```bash
# 15:35 KST 이후에만. 스케줄러 등록 없음 — 수동 one-shot.
ENV_FILE=.env.prod uv run python -m scripts.krb1_p0_completed_session_oneshot \
  --as-of-session 2026-07-29 \
  --decision-at 2026-07-29T18:00:00+09:00
```

- endpoint `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice` ·
  TR `FHKST03010100` 을 전 종목에 GET 하고, 종목별로 raw OHLCV·거래대금 문자열을
  DB row 와 **정수 exact** 비교한다 (float 강제 변환 없음).
- manifest 에 들어가는 것: endpoint · TR ID · raw session(`stck_bsop_date`) ·
  raw OHLCV·value · 종목별 `observed_at` · manifest `finalized_at` · `universe_hash` ·
  종목별 reconcile status.
- 저장: `var/research/krb1/p0_gate_evidence/completion_manifest.jsonl`.
- gate `completed_session_completion_manifest` 강제 조건:

```
universe_hash == coverage universe 로 재계산한 hash   (부분 sweep 위장 불가)
reconciled_count == symbol_count, mismatch/missing/extra == 0
first_observed_at >= as_of_session 15:35 KST
last_observed_at <= decision_at
last_observed_at <= finalized_at <= decision_at
manifest_hash == detail 재계산 hash
chain provenance 존재
```

- `row_count` 나 `ingested_at` 은 완료 증명이 아니다 (07-29 32행/07:40/volume=0 장전
  스냅샷 사례). 재upsert 로 갱신되는 값이라 단독으로는 아무것도 못 증명한다.
- 15:35 전에 실행하면 `completed_session_raw_collection_before_daily_completion_cutoff`,
  `decision_at` 이후에 시작하면 `sweep_started_after_decision_at` 로 막힌다.

## 3. AC3 — 기준가 evidence 모델 분리

기존 모델은 단일 `source_as_of` 에 `source_as_of >= target_session` 을 요구해서
**구조적으로 충족 불가**였다 (07-29 18:00 결정이 07-30 스냅샷을 들고 있을 수 없다).
분리된 모델은 서로 다른 두 시계를 각각 요구한다.

```
effective_session == target_session
AND published_at <= decision_at
AND retrieved_at <= decision_at
AND published_at <= retrieved_at
```

`ReferencePriceExceptionRecord` = symbol · effective_session · is_exception ·
source · published_at · retrieved_at · raw_reference_price · raw_reason_code ·
raw_payload_sha256. 권위 source allowlist 는 `{krx_official_base_price}` 그대로이고,
전수 coverage·raw provenance 요구도 그대로다. 즉 요구 강도는 낮아지지 않았고, **결정 이전
공표**라는 충족 가능한 형태로 바뀌었을 뿐이다.

## 4. AC4 — reference-exception adapter 는 막는 stub

KRX/KIS 공식 source 가 배선되기 전까지 `app/services/krb1_reference_exception_adapter.py`
는 어떤 입력에도 다음을 반환한다.

```
status = "unprovable"
reason = "authoritative_target_session_reference_exception_source_not_wired"
records = ()
```

막는 방향임을 보장하는 장치:

- `ReferenceExceptionFetchResult.__post_init__` 이 records 비어있지 않음 · status/reason
  변경 · `source_wired=True` 를 **예외로 거부**한다 (통과 결과를 만들 수 없다).
- env·settings·flag·override kwarg 경로가 없다. 가드 테스트가 AST 로 `os`/config import,
  `environ`/`getenv`, `ReferencePriceExceptionRecord` 생성, `is_exception=` 전달을 전수
  스캔한다.
- selector 는 adapter 의 `reason` 을 받아 `reference_source_unavailable_reason` 으로
  넣고, caller 가 유효한 record 를 함께 넣어도 **source 미배선 사유가 우선**해서 막힌다.
- 동시에 "gate 자체가 영구 불가"가 아님을 증명한다: 권위 record 를 직접 넣으면 gate 는
  `proven` 이 된다 (`test_gate_is_satisfiable_in_principle_so_the_block_is_attributable`).
  그래서 현재의 fail-close 는 **source 부재 탓**으로 귀속 가능하다.

실 source 를 배선하려면 `ReferenceExceptionSource` protocol 을 구현하는 별도 리뷰 변경이
필요하다. stub 를 손대는 것이 아니다.

## 5. AC5 — 장중 quote timestamp 원문 캡처 (GET-only)

```bash
ENV_FILE=.env.prod uv run python -m scripts.krb1_p0_quote_timestamp_capture \
  --symbols 005930 000660
```

- 인정되는 timestamp 증거는 KIS 원문 `stck_bsop_date` + `stck_cntg_hour` 뿐이다.
  wrapper `price_as_of` / `price_freshness` / `is_stale_price` 는 **non-evidence 로
  라벨링해 기록**만 한다.
- ROB-1121 witness: wrapper 가 `fresh` 를 주장하는데 원문이 없거나 어긋나면
  `rob1121_wrapper_witness.reasons` 에 사유를 남긴다
  (`wrapper_claims_fresh_without_raw_broker_timestamp`,
  `wrapper_price_as_of_is_not_raw_broker_timestamp`,
  `wrapper_price_as_of_tracks_local_capture_clock`).
  `compute_is_stale` 은 `as_of.date() != trading_date` 비교라 장중 항상 fresh 다.
- gate 는 원문 시각이 `>= 15:30 KST` 이고 `<= decision_at` 일 때만 proven.
- 저장: `var/research/krb1/p0_gate_evidence/quote_timestamp_capture.jsonl`.

## 6. 실행 순서

```
1) 장중(선택)  quote_timestamp_capture           — GET-only, ROB-1121 원문 확보
2) 15:35 이후  completed_session_oneshot         — 전수 raw + local reconcile manifest
3) decision 전 metadata_snapshot_capture          — Toss 권위 snapshot
4) 결정 시점   krb1_p0_liquidity_selector --decision-at ...
```

selector 는 1~3 의 append-only 증거를 읽어 gate 를 판정한다. 하나라도 없거나 시계가
`decision_at` 을 넘으면 `selected_candidates=[]` + exit 2 다. **fallback·수동 종목·
volume-rank 대체는 금지다.**

## 6.1 🔴 미해결로 남은 것 (2026-07-30 08:33 교정)

이 문서는 **A1·A2 반영분까지**다. 다음 두 건은 아직 닫히지 않았고, 운영자 결정 대기다.

```
A3  completion 을 local_reconcile / provider_finality 로 분리
    → §2 의 exact reconcile 은 local consistency·coverage 증거다.
      provider 가 정규장 row 를 확정본으로 선언했다는 계약·end marker·correction
      semantics 는 만들지 못한다. 현재 wired 표면에 그 필드가 없다
      (RawDailyBar 에 revision/finality 자리 없음, rt_cd 는 전송 성공코드).
      → completed_session 계열 gate 의 proven 을 finality 증명으로 읽지 마라.
    상태: B5(#1729 소유 gate 스코프) 결정 대기

A4  기준가 evidence 를 actual numeric price 와 determination method 로 분리
    (NORMAL_PRIOR_CLOSE / PRECOMPUTED_THEORETICAL / TARGET_DAY_OPENING_CALL / UNKNOWN)
    → 현재 §3 은 모든 record 에 raw_reference_price 를 요구한다. target-day opening
      call 로 기준가가 정해지는 종목(감자·재상장·변경상장)은 전일 18:00 에 숫자가
      존재하지 않으므로, 그런 종목이 1건이라도 eligible 하면 시장 전체가 영구
      unprovable 이다.
    상태: B1~B3(숫자 면제·제외 vs fail-close·UNKNOWN 처분) 운영자 amendment 대기
```

## 7. 안전 경계

```
read-only / GET-only. 주문·preview·place·cancel·DB write·journal append 없음.
DB 접근은 REPEATABLE READ READ ONLY 트랜잭션.
쓰기는 gate evidence chain append 하나뿐이며 서비스 레이어 경유(AGENTS.md #5).
scheduleless — TaskIQ/cron/Prefect 등록 없음(AGENTS.md #6). 가드 테스트가 스캔한다.
봉인 캠페인 저널(krb1_p0_journal)에는 append 하지 않는다 — 운영자 전용.
```
