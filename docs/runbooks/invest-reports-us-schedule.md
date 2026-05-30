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
1. prepare_bundle(market=us): account-독립 evidence를 NULL-scope로 수집.
2. live advisory report: account_scope=kis_live / execution_mode=advisory_only.
3. mock_preview runner: account_scope=kis_mock / execution_mode=mock_preview
   (live item projection + cited_snapshot_uuids 보존; mock 번들은 live 번들의
   account-independent 스냅샷을 재사용/링크하고, account-bound evidence만 새로 수집).
4. mock preview 브리지: KIS-mock 단독 read-only preflight, submit OFF.

### 계정 독립적 evidence 재사용 (ROB-380)

`mock_preview` 리포트 경로는 live advisory 리포트의 계정 독립적(account-independent) 스냅샷을 재수집하지 않고 **재사용**합니다. `--run` 1회 실행 시 동작:

- `market` / `news` / `candidate_universe` / `symbol` 스냅샷은 live 번들 생성 시 **단 한 번만 수집**되며, 생성된 **동일한** `snapshot_uuid` 행들이 `kis_mock` 번들에 **링크**됩니다.
- `portfolio` / `journal` / `watch_context` / `pending_orders`는 `kis_mock` 계정 범위에 맞게 **새로 수집**되어 각 스코프별로 고유한 행을 유지합니다.

mock runner는 live 리포트 행에 기록된 `snapshot_bundle_uuid`를 읽고 `SnapshotBundleEnsureService.ensure_reusing_account_independent`를 통해 이 재사용 과정을 처리합니다. 만약 live 리포트가 ROB-373 이전에 생성되어 번들이 없는 경우에는 예외 없이 독립 수집 방식으로 안전하게 fallback합니다.

**운영자 검증 방법 (Read-Only SQL)**
`--run` 실행 후, live 번들과 mock 번들의 계정 독립적 스냅샷이 동일한 행을 공유하는지 확인하려면 아래 쿼리를 실행해 `shared_count`가 존재함을 확인하십시오:

```sql
-- 두 번들이 공유하는 계정 독립적 스냅샷 조회 (1 이상이어야 정상)
SELECT s.snapshot_kind, COUNT(DISTINCT bi.bundle_id) AS bundles
FROM review.investment_snapshots s
JOIN review.investment_snapshot_bundle_items bi ON bi.snapshot_id = s.id
JOIN review.investment_snapshot_bundles b ON b.id = bi.bundle_id
WHERE s.account_scope IS NULL
  AND s.snapshot_kind IN ('market','news','candidate_universe','symbol')
  AND b.bundle_uuid IN (:live_bundle_uuid, :mock_bundle_uuid)
GROUP BY s.snapshot_kind
HAVING COUNT(DISTINCT bi.bundle_id) = 2;  -- 두 번들 모두에 링크된 경우
```

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

## Operator smoke (live/mock report render) — preflight & stop rule

이 섹션은 `--run`으로 live advisory + mock_preview 리포트를 실제 렌더해 runtime evidence를 확보하는 **operator-gated** smoke 절차다. ROB-373 code-side와 별개의 runtime validation이다.

### 1. Preflight (값 출력 금지 — 이름/존재만 확인)
- **DB target**: `DATABASE_URL`이 dev/research를 가리키는지 확인(예: `localhost:5432/...`). prod면 중단.
- **gate env (둘 다 필요)**: `INVEST_REPORTS_US_SCHEDULE_ENABLED=true`, `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true`. 후자 미설정 시 live 생성이 `snapshot_backed_report_generator_disabled`로 실패(exit 3).
- **mock creds (선택 주입, `.env.prod.native` 전체 source 금지)**: `KIS_MOCK_ENABLED`, `KIS_MOCK_APP_KEY`, `KIS_MOCK_APP_SECRET`, `KIS_MOCK_ACCOUNT_NO`. 없으면 mock preview 브리지는 `status=unsupported`로 fail-closed(리포트는 생성되나 mock_preview evidence는 unsupported).
- **live evidence**: `user_id`는 `MCP_USER_ID`(기본 1)로 auto-resolve → 해당 유저의 KIS live US 포트폴리오를 읽음. KIS live creds 부재 시 portfolio는 partial/unavailable이고 generation은 intraday floor로 ≥1 item 유지.
- 누락 env는 **이름만** 보고(값 금지).

### 2. Dry-run (zero side effect, 항상 먼저)
```bash
INVEST_REPORTS_US_SCHEDULE_ENABLED=true \
uv run python -m scripts.invest_reports_us_schedule \
  --dry-run --market-session regular --kst-date YYYY-MM-DD
```
4-step plan 출력 + exit 0 확인.

### 3. 실제 run (operator approval 후 정확히 1회)
```bash
INVEST_REPORTS_US_SCHEDULE_ENABLED=true \
SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true \
KIS_MOCK_ENABLED=true KIS_MOCK_APP_KEY=… KIS_MOCK_APP_SECRET=… KIS_MOCK_ACCOUNT_NO=… \
uv run python -m scripts.invest_reports_us_schedule \
  --run --market-session regular --kst-date YYYY-MM-DD
```
필요한 `KIS_MOCK_*`만 선택 주입한다. `.env.prod.native` 전체 source 금지.

### 4. 예상 side effect
- `investment_reports` row 2개 생성: kis_live/advisory_only(status=published) + kis_mock/mock_preview(status=draft, 항목별 `evidence_snapshot["mock_preview"]`).
- snapshot bundle/snapshot 생성(NULL-scope 공유 evidence + account-bound). **broker/order/watch mutation 없음**, KIS-mock 계좌는 read-only.

### 5. Exit code 분류 / stop rule
- `0` 성공(또는 dry-run/disabled/guidance)
- `1` unexpected exception
- `2` misconfiguration(예: `--kst-date` 누락)
- `3` live advisory generation 실패(예: generator gate off)
- 실패 시 **retry 금지** — exit code + 로그 요약/분류만 보고하고 중단. 성공 시 live report UUID / mock_preview report UUID / item count / exit code / safety non-actions 보고.
