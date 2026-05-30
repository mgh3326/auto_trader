# Runbook — ROB-373 US /invest/reports schedule (mock/live 분리 + 공통 evidence 재사용)

## 개요
Claude Code schedule job이 US 리포트를 자동 실행한다. 공통 evidence(market/news/
candidate/symbol)는 `account_scope=NULL` snapshot으로 한 번 수집해 재사용하고, 최종
리포트는 `kis_live`(advisory_only)와 `kis_mock`(mock_preview)로 분리한다. mock preview
리포트 item은 read-only fail-closed preflight 하에 KIS-mock preview 브리지로 연결된다.
**실주문 executor는 범위 밖(ROB-364/368 live smoke 검증 후 별도 follow-up).**

## 엔트리포인트
`uv run python -m scripts.invest_reports_us_schedule [--dry-run | --run] --kst-date YYYY-MM-DD`

- 기본: default-disabled. `INVEST_REPORTS_US_SCHEDULE_ENABLED=true` 필요.
- `--dry-run`: secret/네트워크 없이 실행 계획만 출력.
- `--run`: live advisory 생성 → mock_preview runner.

## 실행 순서
1. prepare_bundle(market=us): 공통 NULL-scope evidence 1회 수집.
2. live advisory report: account_scope=kis_live / execution_mode=advisory_only.
3. mock_preview runner: account_scope=kis_mock / execution_mode=mock_preview
   (공유 evidence 재사용 + live item projection + cited_snapshot_uuids 보존).
4. mock preview 브리지: KIS-mock 단독 read-only preflight, submit OFF.

> 주의: 공통 evidence 재사용은 snapshot payload-hash dedup 기반의 best-effort다. live ensure와 mock ensure 사이에 market/news 소스가 변하면 동일 종류라도 별도 NULL-scope row가 생겨 두 리포트가 다른 snapshot을 인용할 수 있다. 결정적 1회 수집(bundle_uuid 주입)은 후속 개선 항목이다.

## 안전 경계
- KIS live 주문 자동 실행 금지 / market order 금지 / shorting 금지.
- Alpaca Paper 증거와 KIS mock US 증거 혼합 금지(브리지는 KIS-mock 어댑터 단독).
- report 생성 경로 broker/order/watch/order-intent mutation 금지(AST guard 테스트).
- preflight 실패·buying power 부족 시 item BLOCKED, 실주문 미진입.
- `.env.prod.native` 전체 source 금지 — `KIS_MOCK_*`만 선택 주입.
- 로그에 계정 식별자/비밀값 노출 금지(누락 env는 이름만 보고).

## 환경 변수
- `INVEST_REPORTS_US_SCHEDULE_ENABLED` (gate, default off)
- `KIS_MOCK_ENABLED`, `KIS_MOCK_APP_KEY`, `KIS_MOCK_APP_SECRET`, `KIS_MOCK_ACCOUNT_NO`
  (mock 번들/브리지용)
- `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED` (live 생성 게이트)

## 범위 밖
production scheduler 등록/unpause, prod DB backfill, prod env/secret 변경.
KIS mock US BUY/SELL executor/bridge(별도 이슈).
