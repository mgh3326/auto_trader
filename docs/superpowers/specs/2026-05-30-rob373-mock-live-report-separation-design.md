# ROB-373 — mock/live 리포트 분리 + 공통 evidence 재사용 (설계 spec)

- **Linear**: ROB-373 — auto_trader: Claude Code schedule용 mock/live 리포트 분리와 공통 evidence 재사용 구조화
- **작성일**: 2026-05-30
- **상태**: 설계 승인됨 (구현 플랜 대기)
- **관련**: ROB-279(staged snapshot-backed pipeline), ROB-297(US KIS live), ROB-326(US dual-paper premarket), ROB-336(intraday sessions), ROB-364/ROB-368(KIS mock US holdings-delta smoke)

---

## 1. 배경 / 문제

Claude Code schedule job으로 `/invest/reports` 계열 리포트를 자동 실행할 때, KIS live와 KIS official mock US는 대상 종목·포트폴리오·실행 가능성이 다르다. 최종 investment report는 이미
`(report_type, market, market_session, account_scope, execution_mode, kst_date, generator_version)`
로 분리되지만(`app/services/investment_reports/idempotency.py:40-62`), 다음 두 구조적 갭이 있다.

1. **공통 evidence가 scope별로 중복 생성된다.** account-독립 collector(market/news/candidate_universe/symbol)가 `request.account_scope`를 그대로 snapshot에 찍고(`collectors/market.py:119`, `news.py:151`, `candidate_universe.py:494`, `symbol.py:173`), dedupe UNIQUE 키에 `account_scope`가 포함되므로(`models/investment_snapshots.py:122-127`), kis_live와 kis_mock 리포트가 동일 payload의 market/news/candidate/symbol을 **각자 중복 row로 저장**한다. 테스트 픽스처(`tests/services/investment_snapshots/test_bundle_ensure_service.py:55`)는 market을 `account_scope=None`으로 두는 게 **설계 의도**임을 보여주나, 프로덕션 collector엔 미구현이다.

2. **kis_mock 최종 리포트를 기록할 서비스 경로가 없다.** snapshot-backed generator는 kis_mock을 거부하고(`mcp_server/tooling/investment_reports_handlers.py:517-531`, `_SUPPORTED_MARKET_ACCOUNT_PAIRS`) Hermes로 안내하지만, Hermes composition은 `execution_mode="advisory_only"`를 하드코딩한다(`services/investment_stages/hermes_ingest.py:393`). 따라서 `(kis_mock, mock_preview)` 조합을 쓰는 호출자가 **존재하지 않는다**. (DB CHECK·ingestion schema는 이 조합을 이미 허용한다 — `models/investment_reports.py:66,71`, round-trip 테스트 `tests/test_investment_reports_model.py:128-141`.)

또한 ROB-326 `us_dual_paper` preview는 휘발성 in-memory `DualBrokerPreviewPacket`만 만들고(`packet.py`, submit 하드코딩 False) 리포트와 연결되지 않는다. investment_reports item → preview 브리지는 부재(ROB-373 갭).

## 2. 목표 / 비목표

### 목표
- 공통 evidence(market/news/candidate/symbol)를 **한 번 수집해 live·mock 리포트가 재사용**한다.
- 동일 market/session/date에서 **live advisory(kis_live/advisory_only)** 와 **mock preview(kis_mock/mock_preview)** 리포트가 서로 다른 `report_key`로 생성된다.
- mock preview 리포트 item을 **read-only preflight fail-closed** 하에 KIS mock preview 브리지로 연결한다(BUY/SELL 실행은 미포함).
- report provenance에 공통 evidence 재사용이 추적 가능하다.
- Claude Code schedule job이 호출할 **operator CLI 엔트리포인트**를 제공한다(default-disabled).

### 비목표 / 안전 경계
- KIS live 주문 자동 실행 금지 · market order 금지 · shorting 금지.
- Alpaca Paper 증거와 KIS mock US 증거 혼합 금지.
- report 생성 경로에서 broker/order/watch/order-intent mutation 금지. **mock 주문 executor(BUY/SELL)는 이 이슈 범위 밖** — ROB-364/ROB-368 live smoke 검증 후 별도 follow-up.
- snapshot-backed generator의 live-only 가드 변경 금지. Hermes `advisory_only` 하드코딩 변경 금지.
- production scheduler 등록/unpause, prod DB backfill, prod env/secret 변경 범위 밖.
- smoke 로그/evidence에 계정 식별자·비밀값 노출 금지. `.env.prod.native` 전체 source 금지 — `KIS_MOCK_*`만 선택 주입.

### 확정된 설계 분기 (사용자 승인)
| # | 분기 | 결정 |
|---|------|------|
| 1 | 스코프 경계 | 재사용 + 리포트 분리 + preview 브리지까지. **executor 제외**(별도 follow-up) |
| 2 | evidence 재사용 방식 | account-독립 kind를 **`account_scope=NULL`로 생성** |
| 3 | mock 리포트 기록 경로 | **전용 mock-report runner가 ingestion service 직접 호출**(generator 우회) |
| 4 | 오케스트레이터 형태 | **Operator CLI** (`scripts/`, default-disabled) |
| A | mock 리포트 분석 item | **live advisory 리포트 item의 projection**(재분석 아님) |
| B | preview 결과 영속화 | **mock 리포트 item `evidence_snapshot` JSONB**(신규 테이블/kind 없음, 마이그레이션 0) |

## 3. 아키텍처 — 컴포넌트와 경계

마이그레이션 0건. `kis_mock/mock_preview`와 account_scope=NULL은 기존 DB CHECK가 이미 허용한다.

### Unit 1 — Account-독립 snapshot scope 정규화
- **무엇을**: account-독립 snapshot kind를 `account_scope=NULL`로 정규화하여, dedupe가 자연스럽게 cross-scope 공유하게 한다.
- **분류**: `is_account_independent(snapshot_kind) -> bool` 단일 분류 함수.
  - 독립: `market`, `news`, `candidate_universe`, `symbol`.
  - bound(scope 유지): `portfolio`, `journal`, `watch_context`, `pending_orders`, `naver_remote_debug`, `toss_remote_debug`.
  - (그 외 `browser_probe`/`invest_page`/`llm_input_frozen`/`validated_run_card`는 본 리포트 evidence collector 경로 밖 — 현행 동작 유지.)
- **단일 chokepoint**: insert 직전(ensure-service 또는 `repository.insert_snapshot`) 한 곳에서 정규화. collector가 무엇을 넘기든 무관하게 강제 → 어떤 collector도 실수로 re-scope 불가.
- **재사용 위치 = snapshot row 레벨**: live 번들/mock 번들은 여전히 scope별 distinct이나, 둘 다 동일 NULL-scope row를 `bundle_items`로 참조. (snapshot은 전역 재사용 자산, bundle_item은 membership.)
- **provenance**: 동일 NULL-scope uuid가 두 리포트의 `cited_snapshot_uuids`에 동시 등장 → 재사용 추적 자동 충족.
- **read 경로 점검**: account-독립 kind를 `account_scope`로 필터하는 기존 read 쿼리가 있으면 `account_scope IS NULL` (또는 kind-aware)로 갱신. freshness 파생(`overall`)이 account_scope에 의존하지 않는지 확인.
- **마이그레이션 없음 / 전환**: 기존 scope-stamped row는 TTL 만료, 신규는 NULL-scope. 배포 직후 1회는 새 NULL-scope row 생성, 이후 재사용. 백필 불필요.

### Unit 2 — Mock 리포트 runner (`kis_mock` + `mock_preview`)
- **무엇을**: 공유 번들을 ensure/재사용하고, `(account_scope=kis_mock, execution_mode=mock_preview)` 리포트를 `InvestmentReportIngestionService.ingest_with_outcome`로 **직접** 기록. snapshot-backed generator는 호출하지 않음(live-only 가드 무손상).
- **분석 item 출처 (Sub-decision A)**: 직전에 생성된 **live advisory 리포트의 item을 projection**. 분석(symbol/candidate/market)은 generator가 live로 1회만 수행하고, mock runner는 거기에 mock account-bound context(portfolio/cash/sellable)를 부착하고 `apply_policy=requires_user_approval`로 framing. 실행 가능성(어떤 item이 mock cash로 실행 가능한지) 판단은 Unit 3 preview 브리지에서.
- **의존**: ingestion service(쓰기), 공유 번들(읽기), live 리포트(읽기, projection 소스).

### Unit 3 — 리포트 → mock preview 브리지 (fail-closed preflight)
- **무엇을**: mock_preview 리포트 item(side=BUY, qty/notional, limit_price)을 기존 `us_dual_paper` preview 오케스트레이터에 투입. **KIS mock 어댑터만 사용**(Alpaca 제외 — 증거 혼합 금지).
- **fail-closed**: read-only preflight(account/holdings/cash 조회) 실패·부족 시 item `BLOCKED`, BUY/SELL 없음. 기존 `kis_mock.py:133-137`(BLOCKED on insufficient buying power) + `packet.py` `submit_enabled=False` 하드코딩 활용. executor 미포함이므로 **submit 비활성 유지**, 브리지는 preview packet 생성 + 증거 영속화에서 정지.
- **영속화 (Sub-decision B)**: preview 결과(요청·status·blocked 사유)를 mock 리포트 item의 `evidence_snapshot` JSONB에 저장. 신규 테이블/snapshot_kind 없음.
- **의존**: `us_dual_paper` KIS mock 어댑터(읽기/preview), mock 리포트(읽기 item, 쓰기 evidence).

### Unit 4 — Operator CLI 오케스트레이터
- **무엇을**: `scripts/`에 default-disabled CLI. Claude Code schedule job이 호출.
- **순서**: ① `prepare_bundle` 1회 → ② live advisory 생성(`generate_from_bundle`, kis_live; 번들 TTL 재사용) → ③ mock runner(kis_mock; NULL-scope evidence 재사용 + live item projection) → ④ mock preview 브리지(fail-closed, submit off) → ⑤ run summary(비밀/계정ID 없음).
- **"한 번 수집"**: 번들 TTL 재사용 + NULL-scope snapshot 공유로 달성. (generator에 explicit `bundle_uuid` 주입 리팩토링은 nice-to-have, MVP 제외.)
- **안전**: env gate default-disabled, lazy Settings import(`--help`/dry-run/file-parse는 secret 없이 동작), `KIS_MOCK_*`만 주입, read-only preflight fail-closed, 실주문 없음.

### Unit 5 — 테스트 (기존 패턴 확장)
- report key 분리: live vs mock distinct `report_key` (`tests/test_investment_reports_idempotency.py` 확장).
- evidence 재사용 provenance: kis_live+kis_mock ensure 시 market/news/candidate/symbol snapshot uuid가 **동일**, portfolio/journal/watch는 **상이**. 두 리포트 `cited_snapshot_uuids`에 공유 uuid 동시 등장.
- mock runner: ingestion이 `(kis_mock, mock_preview)` 기록 성공 + generator 가드는 여전히 kis_mock 거부(불변).
- preview 브리지 fail-closed: preflight 실패 → item BLOCKED, submit 미발생, evidence에 blocked 사유 기록.
- runner/router safety: report+runner+bridge 경로에 broker/order/watch/order-intent mutation import 0, submit off (`tests/test_no_mutation_imports.py`, `test_generator_safety.py` spy 패턴 확장).

### Unit 6 — Runbook
- `docs/runbooks/`에 schedule job 순서, 안전 경계, env 게이트, `KIS_MOCK_*` 선택 주입, fail-closed 동작 문서화.

## 4. 데이터 흐름

```
Operator CLI (default-disabled)
  └─ prepare_bundle(market=us) ──────────────► SnapshotBundleEnsureService
        │                                          └─ collectors → insert_snapshot
        │                                               └─ Unit1: account-독립 kind → account_scope=NULL
        ▼
  generate_from_bundle(kis_live, advisory_only) ─► SnapshotBackedReportGenerator (live-only 가드, 불변)
        │   └─ live advisory report (NULL-scope evidence 인용)
        ▼
  MockReportRunner(kis_mock, mock_preview) ──────► ingestion.ingest_with_outcome (직접)
        │   └─ live item projection + mock account-bound context (동일 NULL-scope evidence 인용)
        ▼
  MockPreviewBridge ─────────────────────────────► us_dual_paper (KIS mock 어댑터만)
        │   └─ read-only preflight → BLOCKED 또는 preview packet (submit off)
        │   └─ 결과 → mock report item.evidence_snapshot (JSONB)
        ▼
  run summary (no secrets / no account ids)
```

## 5. 에러 처리 / fail-closed
- preflight 조회 실패·계좌 비활성·buying power 부족 → item BLOCKED, 실주문 경로 미진입.
- mock runner가 live 리포트를 찾지 못하면 → mock 리포트 생성 중단(빈 리포트 success 위장 금지).
- env gate 미설정/`KIS_MOCK_*` 부재 → CLI fail-closed, 누락 키 **이름만** 보고(값 출력 없음).
- ingestion 충돌(동일 report_key 재실행) → 기존 idempotency 경로(`ingest_with_outcome`)로 race-safe 재사용.

## 6. 테스트 전략
- 단위: Unit 1 정규화 분류, mock runner projection, preview 브리지 fail-closed 분기.
- 통합: CLI dry-run 경로(secret 없이) + live/mock 리포트 키 분리 end-to-end(가능 범위).
- 안전 가드: mutation import 0, submit off, generator 가드 불변 — spy/AST 패턴.
- 모든 신규 테스트는 기존 성숙 suite의 패턴을 확장(중복 금지).

## 7. 마이그레이션 / 운영
- **DB 마이그레이션 0건**. (NULL-scope, `kis_mock/mock_preview`, evidence JSONB 모두 기존 스키마 허용.)
- production scheduler 등록/unpause는 범위 밖 — CLI는 operator/schedule job이 수동 호출.
- 배포 직후 account-독립 evidence는 1회 새 NULL-scope row 생성 후 재사용(백필 불필요).

## 8. 미해결 / 후속
- KIS mock US BUY/SELL **executor/bridge**는 별도 이슈 — ROB-364/ROB-368 operator live smoke 검증 이후.
- generator에 explicit `bundle_uuid` 주입(이중 collection 완전 제거) — nice-to-have 후속.
- account-독립 read 쿼리 점검 결과에 따라 소규모 read-path 조정 가능(Unit 1 내).
