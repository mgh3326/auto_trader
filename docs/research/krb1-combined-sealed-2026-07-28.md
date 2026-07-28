# KR-B1 + KR-B1b 합본 canonical 정책 JSON 봉인

- 봉인일: 2026-07-28
- 연구 ID: `KRB1-CSM60-H5-v1`
- 상태: `SEALED`
- 운영자 승인: 완료
- account/venue: `kiwoom_mock` / `KRX_ONLY`
- canonical JSON: `krb1-combined-canonical-2026-07-28.json`
- canonical JSON SHA-256: `d5e1246b2072ad227d924e059091e27fa49719e42a5bfba651ae8f2bced9d6f1`

## canonical JSON 해시 규칙

SHA-256 대상은 `krb1-combined-canonical-2026-07-28.json`의 **파일 전체 바이트**다. JSON은 UTF-8,
`sort_keys=True`, `separators=(",",":")`, `ensure_ascii=False`,
`allow_nan=False`로 직렬화했고 파일 끝 newline은 두지 않았다. 따라서 위 값은
정규화 전처리 없이 다음 명령으로 재현된다.

```bash
shasum -a 256 krb1-combined-canonical-2026-07-28.json
```

## 절별 출처 매핑

| canonical 절 | 출처 | 원문 위치 |
|---|---|---|
| `verdict_and_summary` | KR-B1 | 응답 원문 서두 |
| `1` | KR-B1 | §1 |
| `2.1` | KR-B1 | §2 데이터 |
| `2.2` | KR-B1b | R-2 §2.2 대체 |
| `2.3` | KR-B1 | §2 유니버스 U_t |
| `2.4` | KR-B1 | §2 점수 |
| `2.5` | KR-B1 | §2 Warm-up |
| `2.6` | KR-B1b | R-2 §2.6 대체 |
| `2.7` | KR-B1 | §2 청산 |
| `2.8` | KR-B1 | §2 사이징 |
| `2.9` | KR-B1 | §2 정책 불변 |
| `3` | KR-B1 | §3 |
| `4.2` | KR-B1 | §4 표본수 |
| `4.3` | KR-B1 | §4 달력 |
| `4.5` | KR-B1b | R-1 §4.5 대체 |
| `4.6.1` | KR-B1b + 운영자 수정 | R-1 §4.6.1; authoritative platform token amended before seal |
| `4.6.1.operator_declarations` | 운영자 수정 | authoritative platform 및 NumPy 독립 pin |
| `4.6.2-4.6.6` | KR-B1b | R-1 §4.6.2~§4.6.6 |
| `5.1` | KR-B1 | §5 봉인 순서 |
| `5.2` | KR-B1b | R-2 §5.2 대체 |
| `5.3` | KR-B1b | R-2 §5.3 대체 |
| `5.4` | KR-B1 | §5 outcome futility |
| `6` | KR-B1 | §6 |
| `7.1-7.2` | KR-B1 | §7 P0-1~P0-2 |
| `7.3` | KR-B1b | R-2 §7.3 대체 |
| `7.4-7.5` | KR-B1 | §7 P0-4~P0-5 |
| `8.heading` | KR-B1 | §8 heading |
| `8.1` | KR-B1b | R-1 §8.1 대체 |
| `8.2-8.4` | KR-B1 | §8 REJECT 이후 유지분 |
| `9` | KR-B1 | §9 |
| `10` | KR-B1b | R-2 §10 변경분 대체 |

`normative_sections[].text`는 각 출처의 원문 문자열을 그대로 이관했다. 유일한 자구
치환은 아래 운영자 수정이 승인한 `§4.6.1`의 플랫폼 토큰
`linux/amd64 little-endian` → `linux/arm64 little-endian`이다. 이 절은 출처를
`KR-B1b + 운영자 수정`으로 표시했다.

## KR-B1b가 대체한 절

| KR-B1에서 제외된 절 | canonical에서 사용하는 대체 출처 |
|---|---|
| KR-B1 §2 결정 시점(KST)·운영 창 | KR-B1b R-2 §2.2 |
| KR-B1 §2 진입·주문 수명·실체결 | KR-B1b R-2 §2.6 |
| KR-B1 §4 효과·비용·week-clustered LCB 미완결 계약 | KR-B1b R-1 §4.5 |
| KR-B1 §4 wild-bootstrap 난수·런타임 미명세 | KR-B1b R-1 §4.6 + 운영자 수정 |
| KR-B1 §5 dry-count 기록·shadow-fill | KR-B1b R-2 §5.2 |
| KR-B1 §5 dry-count 통과조건 | KR-B1b R-2 §5.3 |
| KR-B1 §7 P0-3 | KR-B1b R-2 §7.3 |
| KR-B1 §8 CONFIRM 통계 문장 | KR-B1b R-1 §8.1 |
| KR-B1 §10 변경 대상 상수 | KR-B1b R-2 §10 |

명시 대체 절에는 KR-B1 원문을 병기하지 않았다. 변경되지 않은 KR-B1 조문만 유지했고,
KR-B1b의 대체 조문을 normative text로 사용한다.

## 운영자 수정 반영

반영 위치는 canonical `§4.6.1 authoritative runtime`이다.

```text
변경 전   linux/amd64
변경 후   linux/arm64 little-endian
고정 호스트  OCI Ampere · Oracle Linux 9.7
```

`NumPy 2.3.5`를 의도적으로 고정한다. 이는 R4.1 봉인의 `NumPy 2.4.4`와 독립 pin이며
버전 일치를 요구하지 않는다. 수정은 봉인 전·outcome 미열람 시점에 이루어졌고,
사후조정이 아니다.

핵심 실측 근거는 동일한 참조 구현·고정 합성 fixture의 Darwin/x86_64 ↔
OCI/Linux ARM64 비교다.

```text
cross_arch_hex_result       = MATCH
cluster_score_hex           = 36/36 MATCH
scalar_hex                  = 5/5 MATCH
weight_bits_sha256_equal    = true
reference_source_equal      = true
```

따라서 “PCG64는 아키텍처 무관”이라는 근거는 이 고정 fixture에서 bit stream과 게시용
binary64 hex가 실제로 일치한 측정으로 뒷받침된다. 이 증거를 모든 가능한 입력의 일반
증명으로 확대하지 않는다.

## 이 수정의 정당성 심사

심사 항목은 운영자 수정 정본의 원문 그대로 다음 네 가지로 고정한다.

- 수정 시점이 실제로 outcome 미열람 이전인가 (타임스탬프·산출물 부재로 증명 가능한가)
- 플랫폼 변경이 통계 판정에 영향을 주는가 (PCG64 무관성 주장이 실측으로 뒷받침되는가)
- NumPy 독립 pin 이 R4.1 봉인 계약과 충돌하지 않는가
- 변경 후 재현이 실제로 가능한가 (고정 호스트 접근성·이미지 재빌드 가능성)

판정: `PASS / CLOSED`.

- 시점: KR-B1b는 `2026-07-28T12:32:48+0900`, 운영자 수정은
  `2026-07-28T12:49:21+0900`, OCI 실행 산출물은 `2026-07-28T12:53:58+0900`
  이후 생성됐다. 봉인 시점에 P0는 `NOT_STARTED`이고,
  KR-B1 outcome/PnL/return/dry-count/P0-result 이름의 산출물 검색 결과는 0건이다.
- 통계 영향: Darwin ↔ OCI에서 weight bits가 일치했고 cluster 36/36·scalar 5/5
  hex가 일치했다.
- 독립 pin: KR-B1b 참조 구현용 NumPy pin과 R4.1 calibration용 NumPy pin은 서로 다른
  연구 계약이며, 운영자 수정 정본이 버전 일치 불요를 명시한다.
- 재현성: OCI Ampere 고정 호스트, digest-pinned ARM64 base, hash-pinned NumPy wheel,
  Containerfile, OCI image archive와 rebuild/load/run 절차가 보존됐다.

## 봉인 검증 근거

| 검증 | 판정 |
|---|---|
| `ITEM_1_WILD_BOOTSTRAP_CLOSURE` | `PASS / CLOSED` |
| `ITEM_4_DRY_COUNT_LOOKAHEAD_CLOSURE` | `PASS / CLOSED` |
| §4.6.5 참조 구현 4,563바이트 SHA-256 | `PASS` — `a8c5357905260441f1fbdc95bf43eb06f0fc97d493e0641716ffe3e16419d87f` |
| authoritative 합성데이터 실행 | `PASS / CLOSED` |
| authoritative runtime | `PASS` — Linux/aarch64 little-endian, CPython 3.13.5, NumPy 2.3.5 |
| Darwin ↔ OCI | `MATCH` — cluster 36/36, scalar 5/5 |

원문·검증 증거 SHA-256 manifest:

| artifact | SHA-256 |
|---|---|
| `gptpro-krb1-response-2026-07-28.md` | `9fbfe1be2dd4d0a04300dd82303c843258dc082c2e46e196b41906ff2b28866d` |
| `gptpro-krb1b-response-2026-07-28.md` | `940c6b5e5bbf209f2766898c1259f83a02cdcb6ec2d77e650e7a86aaf7a60bd4` |
| `krb1b-runtime-amendment-2026-07-28.md` | `bfb8e4099a80e0ea9ac87d5b0c260edd2d1f0286b59f53ff806e5c64e9bb15d8` |
| `krb1b-doc-verify-2026-07-28.md` | `77f5db38e03009e5dd8f3512ff1533d20e789015cb75b79c426122156932c1de` |
| `krb1b-exec-verify-2026-07-28.md` | `b5f605d9ad464da8a272a941c7ff937be0f08013f274b038143a48dd0b53e3c4` |
| `krb1b-oci-exec-2026-07-28.md` | `fedfb549af1ae3659f94ab3b0546940d5e51e225cc92ec19f6bbc22e60b7649d` |
| `runtime-hex-comparison.json` | `d7bbf6d73b69bb5f7b80ee9207cb998e42f971eb99a9213a016ebf80af0b86a5` |

## anchor ledger 초기화

```text
encoding            = UTF-8 JSON, object keys lexicographic, no insignificant whitespace
chain concatenation = raw 32-byte previous chain hash || raw 32-byte row hash
genesis             = 0000000000000000000000000000000000000000000000000000000000000000
initial row count   = 1
initial row hash    = 48335298149e92cdfbbb83f7f604b488074d33122f9c5ad15fe8d42b3925d8b8
initial head        = be117294febe0c8280949a37e35baf95246f527049484b2c20ee890591408229
state               = INITIALIZED
P0 state            = NOT_STARTED
```

초기 canonical row:

```json
{"next_stage":"P0_10_KRX_SESSIONS","p0_state":"NOT_STARTED","record_type":"SEAL_INITIALIZED","recorded_date":"2026-07-28","study_id":"KRB1-CSM60-H5-v1"}
```

ledger는 append-only다. 기존 row 수정은 금지하며 다음 append 대상은 P0 시작
anchor/manifest다.

## 다음 단계

봉인 후 다음 단계는 **P0 10 KRX 세션**이다. P0가 완료되고 계약상 선행 조건을 모두
통과하기 전 outcome 접근은 금지된다.
