# Phase 2 · Slice 2b — 죽은 n8n HTTP 표면 삭제 Implementation Plan (ROB-560)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. 삭제 리팩터 — 게이트는 **collect-only 무손상 + 살아있는 소비처 테스트 그린 + `/api/n8n` 라우트 부재**.

**Goal:** 소비처 0인 `/api/n8n/*` HTTP 표면(라우터 2 + router-전용 서비스 ~10)과 부속(미들웨어 auth, `N8N_API_KEY`, `docker-compose.n8n.yml`, `n8n/` 디렉터리, n8n 테스트)을 삭제한다. **살아있는 데이터 서비스(2a에서 개명한 filled/pending/market_context/formatting/indicators)는 보존.**

**Architecture:** n8n 은퇴 후 `/api/n8n/*` 소비처 0 (Prefect repo grep 0건). 라우터+router-전용 서비스 삭제. 살아있는 데이터 서비스는 reconciler/intraday/stock-detail가 직접 사용하므로 유지.

**전제:** 2a(데이터 서비스 개명) 머지 후 진행 권장(개명된 이름 기준). 2a 미진행 시 이름은 `n8n_*` 그대로 — 본 플랜은 **2a 완료 기준** 이름 사용.

**스펙:** `docs/superpowers/specs/2026-06-15-phase2-n8n-decommission-agent-naming-design.md` §3 (2b)

> **⚠️ OPERATOR 게이트 (구현 전 필수):** `/api/n8n/*` 외부 소비처가 0인지 확인.
> - 잔존 n8n 컨테이너/워크플로우가 호출하지 않는가 (n8n 은퇴 확인)
> - 수동 cron/curl/타 도구가 호출하지 않는가
> - 액세스 로그에 최근 `/api/n8n/*` 히트 0
> 확인 전에는 본 슬라이스를 머지하지 않는다.

---

## Task 1: 삭제 대상 확정 (사전 grep 게이트)

- [ ] **Step 1: router-전용(삭제) 서비스에 살아있는 비-n8n 소비처 0 재확인**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-560
for mod in n8n_crypto_scan_service n8n_daily_brief_portfolio n8n_daily_brief_rendering \
           n8n_daily_brief_service n8n_kr_morning_report_service n8n_news_service \
           n8n_pending_review_service n8n_pending_snapshot_service n8n_response_builder \
           n8n_trade_review_service; do
  echo "[$mod]"; grep -rln "$mod" app/ websocket_monitor.py scripts/ 2>/dev/null \
    | grep -v "app/routers/n8n" | grep -v "app/services/n8n_" ;
done
```
Expected: 각 모듈 비-n8n 소비처 **0** (코드). `.md` 프롬프트 문서 참조는 무시(런타임 아님). 코드 소비처가 있으면 멈추고 재평가.

- [ ] **Step 2: `/api/n8n/*` 코드 소비처 0 재확인 (auto_trader + Prefect)**

Run:
```bash
grep -rn '/api/n8n/' app/ tests/ scripts/ 2>/dev/null | grep -v 'app/routers/n8n' | grep -v 'middleware/auth'
grep -rn '/api/n8n' /Users/mgh3326/services/prefect 2>/dev/null
```
Expected: 0 (자체 라우터/미들웨어/테스트 제외, Prefect 0).

---

## Task 2: 라우터 + main 등록 삭제

**Files:**
- Delete: `app/routers/n8n.py`, `app/routers/n8n_scan.py`
- Modify: `app/main.py` (include_router 제거)

- [ ] **Step 1: main.py에서 라우터 등록 제거**

`app/main.py`에서 다음 라인 삭제(정확 매칭 후):
```python
app.include_router(n8n.router)
app.include_router(n8n_scan.router)
```
그리고 상단 `from app.routers import ... n8n, n8n_scan ...` import에서 `n8n`, `n8n_scan` 토큰 제거(다른 라우터 import는 보존).

- [ ] **Step 2: 라우터 파일 삭제**

```bash
git rm app/routers/n8n.py app/routers/n8n_scan.py
```

- [ ] **Step 3: 검증 — 앱 import 성립**

Run: `uv run python -c "import app.main"` 2>&1 | tail -3`
Expected: 에러 없음(누락 import 없음).

---

## Task 3: router-전용 n8n 서비스 삭제

**Files (Delete — Task 1에서 비-n8n 소비처 0 확인된 것만):**
- `app/services/n8n_crypto_scan_service.py`
- `app/services/n8n_daily_brief_service.py`
- `app/services/n8n_daily_brief_portfolio.py`
- `app/services/n8n_daily_brief_rendering.py`
- `app/services/n8n_kr_morning_report_service.py`
- `app/services/n8n_news_service.py`
- `app/services/n8n_pending_review_service.py`
- `app/services/n8n_pending_snapshot_service.py`
- `app/services/n8n_response_builder.py`
- `app/services/n8n_trade_review_service.py`

> **보존(2a에서 개명됨)**: `filled_orders_service`, `filled_orders_indicators`, `pending_orders_service`, `market_context_service`, `order_brief_formatting`. 이들은 삭제 금지.

- [ ] **Step 1: 삭제**

```bash
cd /Users/mgh3326/work/auto_trader.rob-560
git rm app/services/n8n_crypto_scan_service.py app/services/n8n_daily_brief_service.py \
  app/services/n8n_daily_brief_portfolio.py app/services/n8n_daily_brief_rendering.py \
  app/services/n8n_kr_morning_report_service.py app/services/n8n_news_service.py \
  app/services/n8n_pending_review_service.py app/services/n8n_pending_snapshot_service.py \
  app/services/n8n_response_builder.py app/services/n8n_trade_review_service.py
```

- [ ] **Step 2: 살아있는 서비스가 삭제된 것을 import하지 않는지 확인**

Run:
```bash
grep -rn 'n8n_crypto_scan_service\|n8n_daily_brief\|n8n_kr_morning_report_service\|n8n_news_service\|n8n_pending_review_service\|n8n_pending_snapshot_service\|n8n_response_builder\|n8n_trade_review_service' app/ websocket_monitor.py scripts/
```
Expected: **0** (살아있는 데이터 서비스는 이들을 역으로 import하지 않음). 매치가 있으면 해당 살아있는 서비스에서 죽은 의존 제거 필요 → 재평가.

---

## Task 4: 미들웨어 auth + config 삭제

**Files:**
- Modify: `app/middleware/auth.py` (`/api/n8n/` 분기 제거)
- Modify: `app/core/config.py` (`N8N_API_KEY` 제거)
- Modify: `env.example` (N8N_API_KEY 등 n8n 키 제거)

- [ ] **Step 1: 미들웨어 `/api/n8n/` 분기 제거**

`app/middleware/auth.py`의 `if path.startswith("/api/n8n/"):` 블록 전체(X-N8N-API-KEY 검증) 삭제.

- [ ] **Step 2: config + env.example에서 N8N_API_KEY 제거**

`app/core/config.py`에서 `N8N_API_KEY: str = ""` 삭제. `env.example`에서 N8N_API_KEY 줄 삭제.
> `N8N_FILL_WEBHOOK_URL`은 ROB-558에서 이미 제거됨. `N8N_WATCH_ALERT_WEBHOOK_URL`은 2c(openclaw watch 삭제)에서 함께 제거 — 2b는 건드리지 않음(2c와 충돌 회피).

- [ ] **Step 3: 검증**

Run: `grep -rn 'N8N_API_KEY\|/api/n8n/' app/` → Expected: 0.

---

## Task 5: docker-compose.n8n.yml + n8n/ 디렉터리 삭제

**Files:**
- Delete: `docker-compose.n8n.yml`, `n8n/` (workflows, README, data)

- [ ] **Step 1: 삭제**

```bash
git rm docker-compose.n8n.yml
git rm -r n8n/
```
> `n8n/data/`가 .gitignore면 git rm 대상 아님 — `rm -rf n8n/` 로 로컬 정리만(operator). 추적 파일만 git rm.

- [ ] **Step 2: 참조 잔존 확인**

Run: `grep -rn 'docker-compose.n8n\|n8n/workflows\|n8nio/n8n' . --include=*.md --include=*.yml --include=*.sh 2>/dev/null | grep -v docs/superpowers`
Expected: 0 (DEPLOYMENT.md/DOCKER_USAGE.md 등에 n8n 안내 있으면 함께 정정).

---

## Task 6: n8n 테스트 삭제

**Files (Delete):** router/서비스 단위 n8n 테스트.

- [ ] **Step 1: 삭제 대상 식별 (2a에서 개명·이동된 살아있는 테스트 제외)**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-560
ls tests/test_n8n_*.py tests/services/*n8n* 2>/dev/null
```
삭제: `test_n8n_api.py`, `test_n8n_api_key_auth.py`, `test_n8n_crypto_scan_api.py`, `test_n8n_crypto_scan_service.py`, `test_n8n_daily_brief_api.py`, `test_n8n_daily_brief_service.py`, `test_n8n_daily_brief_formatting.py`, `test_n8n_followup_endpoints.py`, `test_n8n_kr_morning_report.py`, `test_n8n_news.py`, `test_n8n_prod_configuration.py`, `test_n8n_scan_api.py`, `test_n8n_tc_briefing_discord.py`, `test_n8n_trade_review.py`, `test_n8n_market_context.py`(2a에서 `test_market_context_service.py`로 이동했으면 제외).
> **보존**: 2a에서 개명된 `test_filled_orders_service.py`, `test_filled_orders_indicators.py`, `test_market_context_service.py`, `test_order_brief_formatting.py`, `tests/services/test_order_brief_formatting_extended.py`.

- [ ] **Step 2: 삭제**

```bash
git rm tests/test_n8n_api.py tests/test_n8n_api_key_auth.py tests/test_n8n_crypto_scan_api.py \
  tests/test_n8n_crypto_scan_service.py tests/test_n8n_daily_brief_api.py \
  tests/test_n8n_daily_brief_service.py tests/test_n8n_daily_brief_formatting.py \
  tests/test_n8n_followup_endpoints.py tests/test_n8n_kr_morning_report.py \
  tests/test_n8n_news.py tests/test_n8n_prod_configuration.py tests/test_n8n_scan_api.py \
  tests/test_n8n_tc_briefing_discord.py tests/test_n8n_trade_review.py
```
> 실제 존재하는 파일만 — Step 1 `ls` 결과로 목록 맞춤. `tests/services/research_run_safety_helpers.py`·`pure_service_safety.py`는 헬퍼라 삭제 아님(2a에서 import만 갱신됨); 삭제된 서비스 참조가 남았으면 해당 참조만 정리.

---

## Task 7: 전체 검증

- [ ] **Step 1: collect-only 무손상**

Run: `uv run pytest tests/ --collect-only -q 2>&1 | tail -3`
Expected: import 에러 0 (삭제된 모듈을 참조하는 잔존 테스트 없음).

- [ ] **Step 2: 살아있는 소비처 회귀**

```bash
uv run pytest tests/ -k "reconcil or stock_detail or intraday_order_review or filled_orders or pending_orders or market_context" -q 2>&1 | tail -3
```
Expected: 그린(2a 보존 서비스 정상).

- [ ] **Step 3: 린트/타입/앱 import**

```bash
uv run ruff check app/ tests/ ; uv run ty check app/ ; uv run python -c "import app.main"
```
Expected: 클린.

- [ ] **Step 4: 커밋**

```bash
git add -A
git commit -m "refactor(ROB-560): remove dead n8n HTTP surface (routers, router-only services, infra)

No consumer after n8n retirement (Prefect doesn't call /api/n8n/*). Keeps the
live data services (filled/pending/market_context/order_brief_formatting).
Removes routers, ~10 router-only services, /api/n8n auth branch, N8N_API_KEY,
docker-compose.n8n.yml, n8n/ dir, n8n tests. Operator confirmed no external
consumer of /api/n8n/*.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## 자체 점검 (작성자 — 완료)
- **스펙 커버리지:** 죽은 라우터(Task2)·router-전용 서비스(Task3)·미들웨어/config(Task4)·인프라(Task5)·테스트(Task6). 살아있는 서비스 보존 명시.
- **삭제 안전:** Task1 grep 게이트로 비-n8n 소비처 0 재확인 후 삭제. operator 외부 소비처 게이트 명시.
- **플레이스홀더:** 없음. 실제 파일 목록은 Step `ls`로 환경 대조.

## 미해결
- uncertain 엔드포인트(daily-brief/trade-reviews/crypto-scan/kr-morning-report/news/sell-signal/scan)의 외부 소비처는 operator 확인(스펙 §3 2b).
- `N8N_WATCH_ALERT_WEBHOOK_URL`은 2c에서 제거(중복 회피).
