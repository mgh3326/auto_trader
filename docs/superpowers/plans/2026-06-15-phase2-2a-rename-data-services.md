# Phase 2 · Slice 2a — 살아있는 `n8n_*` 데이터 서비스 개명 Implementation Plan (ROB-560)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development 또는 executing-plans. 순수 rename 리팩터라 TDD "실패 테스트" 대신 **기존 스위트 그린 + collect-only 무손상 + grep 잔존 0**을 게이트로 쓴다.

**Goal:** 이름만 `n8n_`인 살아있는 데이터 서비스(체결/대기주문/시장컨텍스트/포맷)를 데이터 역할명으로 개명한다. **behavior 완전 불변**(순수 rename + import 갱신).

**Architecture:** `git mv` 후 전 import 경로 일괄 갱신. 런타임 동작·payload·엔드포인트 무변경. 죽은 n8n 라우터/서비스 삭제는 2b(별도), OpenClaw→Agent는 2c(별도, ROB-558 머지 후).

**Tech Stack:** Python 3.13, ruff, ty, pytest.

**스펙:** `docs/superpowers/specs/2026-06-15-phase2-n8n-decommission-agent-naming-design.md` §3 (2a)

> **이름 (스펙 §6.1 — 리뷰에서 확정, 아래는 제안 기본값):**
> - `n8n_filled_orders_service` → `filled_orders_service`
> - `n8n_pending_orders_service` → `pending_orders_service`
> - `n8n_market_context_service` → `market_context_service`
> - `n8n_formatting` → `order_brief_formatting`
> 이름 변경 시 본 플랜의 해당 토큰을 일괄 치환.

> **순서 주의:** 2a는 2b가 삭제할 router-전용 서비스(`n8n_daily_brief_service` 등)의 import 라인도 갱신한다(곧 삭제될 파일이라 무해). 2b를 먼저 한다면 2a 대상 importer가 줄어든다. 본 플랜은 **2a 단독(2b 이전)** 기준.

---

## Task 1: 사전 스냅샷 (회귀 기준선)

**Files:** 없음(측정만)

- [ ] **Step 1: 기준선 기록**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-560
uv run pytest tests/ --collect-only -q 2>&1 | tail -1   # 기대: "NNNNN tests collected" 에러 0
uv run pytest tests/test_n8n_filled_orders_service.py tests/test_n8n_filled_orders_indicators.py tests/test_n8n_market_context.py tests/test_n8n_formatting.py tests/services/test_n8n_formatting_extended.py tests/test_n8n_daily_brief_formatting.py tests/test_n8n_api.py -q 2>&1 | tail -3
```
Expected: collect 에러 0, 대상 테스트 그린. 이 숫자를 개명 후와 비교.

---

## Task 2: 4개 모듈 파일 git mv

**Files:**
- Move: `app/services/n8n_filled_orders_service.py` → `app/services/filled_orders_service.py`
- Move: `app/services/n8n_pending_orders_service.py` → `app/services/pending_orders_service.py`
- Move: `app/services/n8n_market_context_service.py` → `app/services/market_context_service.py`
- Move: `app/services/n8n_formatting.py` → `app/services/order_brief_formatting.py`

- [ ] **Step 1: git mv (히스토리 보존)**

```bash
cd /Users/mgh3326/work/auto_trader.rob-560
git mv app/services/n8n_filled_orders_service.py app/services/filled_orders_service.py
git mv app/services/n8n_pending_orders_service.py app/services/pending_orders_service.py
git mv app/services/n8n_market_context_service.py app/services/market_context_service.py
git mv app/services/n8n_formatting.py app/services/order_brief_formatting.py
```

- [ ] **Step 2: 테스트 파일도 git mv**

```bash
git mv tests/test_n8n_filled_orders_service.py tests/test_filled_orders_service.py
git mv tests/test_n8n_filled_orders_indicators.py tests/test_filled_orders_indicators.py
git mv tests/test_n8n_market_context.py tests/test_market_context_service.py
git mv tests/test_n8n_formatting.py tests/test_order_brief_formatting.py
git mv tests/services/test_n8n_formatting_extended.py tests/services/test_order_brief_formatting_extended.py
```
> `test_n8n_daily_brief_formatting.py`·`test_n8n_api.py`·`test_n8n_trade_review.py`는 (2b에서 삭제될) 라우터/브리프 테스트라 **이동하지 않고** import만 갱신(Task 3). `tests/services/research_run_safety_helpers.py`·`pure_service_safety.py`도 import만 갱신.

---

## Task 3: import 경로 일괄 갱신

**Files (importers — 모듈 4개의 모든 사용처):**
- `app/routers/n8n.py`
- `app/services/invest_view_model/stock_detail_orders_service.py`
- `app/services/execution_ledger/reconciler.py`
- `app/jobs/intraday_order_review.py`
- `app/services/n8n_daily_brief_service.py`
- `app/services/n8n_pending_review_service.py`
- `app/services/n8n_kr_morning_report_service.py`
- `app/services/n8n_daily_brief_portfolio.py`
- 개명된 모듈 상호참조: `pending_orders_service.py`(↔ market_context, order_brief_formatting), `market_context_service.py`(↔ pending_orders, order_brief_formatting)
- 테스트: 이동된 5개 + `tests/test_n8n_daily_brief_formatting.py`, `tests/test_n8n_api.py`, `tests/test_n8n_trade_review.py`, `tests/services/research_run_safety_helpers.py`, `tests/services/pure_service_safety.py`

- [ ] **Step 1: 토큰 일괄 치환 (정확 매칭)**

```bash
cd /Users/mgh3326/work/auto_trader.rob-560
grep -rl 'n8n_filled_orders_service' app/ tests/ | xargs sed -i '' 's/n8n_filled_orders_service/filled_orders_service/g'
grep -rl 'n8n_pending_orders_service' app/ tests/ | xargs sed -i '' 's/n8n_pending_orders_service/pending_orders_service/g'
grep -rl 'n8n_market_context_service' app/ tests/ | xargs sed -i '' 's/n8n_market_context_service/market_context_service/g'
grep -rl 'n8n_formatting' app/ tests/ | xargs sed -i '' 's/n8n_formatting/order_brief_formatting/g'
```
> macOS `sed -i ''` 형식. 치환은 모듈 경로(`app.services.n8n_filled_orders_service`)와 부분 식별자 모두 잡는다. 함수/클래스 내부 이름(`fetch_filled_orders` 등)은 `n8n_` 접두사가 없으므로 영향 없음.

- [ ] **Step 2: 잔존 참조 0 확인**

```bash
grep -rn 'n8n_filled_orders_service\|n8n_pending_orders_service\|n8n_market_context_service\|n8n_formatting' app/ tests/ websocket_monitor.py scripts/
```
Expected: **0 매치**.

---

## Task 4: 모듈 docstring/주석의 n8n 표현 정리 (선택, 혼란 제거)

**Files:** 개명된 4개 모듈

- [ ] **Step 1: 모듈 상단 docstring에서 "n8n 전용" 류 표현 제거/정정**

각 모듈 상단 docstring이 "n8n ..." 라고 쓰여 있으면, 실제 역할(예: "체결 주문 조회 — execution_ledger 정산·종목상세에서 사용")로 정정. (함수 시그니처·로직 무변경.)

- [ ] **Step 2: 변경 확인**

Run: `git diff --stat app/services/filled_orders_service.py app/services/pending_orders_service.py app/services/market_context_service.py app/services/order_brief_formatting.py`
Expected: docstring 라인만 변경.

---

## Task 5: 전체 검증

**Files:** 없음(검증)

- [ ] **Step 1: 포맷/린트/타입**

```bash
cd /Users/mgh3326/work/auto_trader.rob-560
uv run ruff format app/ tests/
uv run ruff check app/ tests/
uv run ty check app/
```
Expected: 클린.

- [ ] **Step 2: collect-only 무손상 (import 깨짐 0)**

Run: `uv run pytest tests/ --collect-only -q 2>&1 | tail -3`
Expected: Task 1과 동일 collected 수, 에러 0.

- [ ] **Step 3: 영향 테스트 + 라이브 소비처 테스트**

```bash
uv run pytest tests/test_filled_orders_service.py tests/test_filled_orders_indicators.py tests/test_market_context_service.py tests/test_order_brief_formatting.py tests/services/test_order_brief_formatting_extended.py tests/test_n8n_daily_brief_formatting.py tests/test_n8n_api.py -q 2>&1 | tail -3
# 라이브 소비처: execution_ledger reconciler + stock_detail + intraday
uv run pytest tests/ -k "reconcil or stock_detail or intraday_order_review" -q 2>&1 | tail -3
```
Expected: 모두 그린.

- [ ] **Step 4: 커밋**

```bash
git add -A
git commit -m "refactor(ROB-560): rename live n8n_* data services to role-based names

n8n_filled_orders_service -> filled_orders_service (execution_ledger reconciler + /invest/stocks)
n8n_pending_orders_service -> pending_orders_service (intraday_order_review)
n8n_market_context_service -> market_context_service
n8n_formatting -> order_brief_formatting
Pure rename, behavior unchanged. Dead n8n HTTP surface removal is 2b.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## 자체 점검 (작성자 — 완료)

- **스펙 커버리지:** 2a의 4개 live 서비스 개명 = Task 2-3. docstring 정리 = Task 4. behavior 불변 검증 = Task 5(collect-only + 라이브 소비처 테스트).
- **타입 일관성:** 새 모듈명 4개가 Task 2(mv)·3(import)·5(test) 전반 일치.
- **플레이스홀더:** 없음. 단 실행자는 (a) 스펙 §6.1에서 최종 이름 확정 후 토큰 치환, (b) `sed -i ''`(macOS) vs `sed -i`(linux) 환경 확인.

## 미해결 (스펙 리뷰 후)
- 최종 모듈명(§6.1). `order_brief_formatting`이 daily-brief 외 용도면 더 일반적 이름(`order_formatting`) 검토.
- 2a 단독 vs 2b 선행(§6.6) — 본 플랜은 2a 단독.
