# ROB-570 — CIO/board-briefing 삭제 Implementation Plan

> 휴면 n8n-era 기능 삭제. ROB-560 2b에서 보존했던 것을 user 결정("안 씀→삭제")으로 정리. 도달 불가(라우터 삭제됨)·60일 정체·live 의존 0 확인.

**Goal:** CIO/board-briefing(암호화폐 보드 브리핑) 코드·스키마·테스트 삭제. `order_brief_formatting`(live 공유)은 보존.

**검증 기반 범위 (origin/main dd5b38a6 grep 실측):** 삭제 후보의 live 비-삭제셋 importer = 0. `order_brief_formatting`만 4 live 서비스 공유 → 보존. `kr_morning_report`는 별개(미포함).

## 삭제 (git rm)
- `app/services/cio_coin_briefing/` (prompts/board_briefing_v2.md, prompts/gate_phrases.py) — dir 전체
- `app/services/n8n_daily_brief_service.py`, `n8n_daily_brief_portfolio.py`, `n8n_daily_brief_rendering.py`
- `app/schemas/n8n/board_brief.py`, `app/schemas/n8n/daily_brief.py`
- 테스트 11: `tests/fixtures/cio_briefing.py`, `tests/test_board_brief_render_v2.py`, `tests/test_board_brief_schema_v2.py`, `tests/test_cio_briefing_fail_closed.py`, `tests/test_cio_briefing_g2_precedence.py`, `tests/test_cio_briefing_g6_only_trigger.py`, `tests/test_cio_briefing_render_invariants.py`, `tests/test_cio_briefing_service_branch_coverage.py`, `tests/test_n8n_daily_brief_formatting.py`, `tests/test_n8n_daily_brief_service.py`, `tests/test_plan_v2_section_g_checklist.py`

## 편집
- `app/schemas/n8n/__init__.py`: `from app.schemas.n8n.daily_brief import *` 줄 삭제 (board_brief는 __init__에 없음).
- `app/services/order_brief_formatting.py`: 라인 174 stale 주석("moved from n8n_daily_brief_service") 정리(코드 무변경).
- `tests/test_dust_accounting.py`: `_build_brief_text`(line 210)·`_build_portfolio_summary`(line 262)를 삭제 서비스에서 import하는 **테스트 함수 2개만 제거**, 나머지 dust/portfolio 테스트 보존.

## 보존 (삭제 금지)
`app/services/order_brief_formatting.py`(+`tests/services/test_order_brief_formatting_extended.py`: 삭제모듈 실import 없음, docstring 언급만 — 유지), `n8n_kr_morning_report_service.py`(별개 dead 기능, 별도 결정), market_context/pending_orders/filled_orders(live).

## 검증
- `grep -rn 'cio_coin_briefing|n8n_daily_brief|board_brief|gate_phrases|build_tc_preliminary|build_cio_pending' app/ tests/` → 0 (정리 주석/완전삭제 후).
- `ruff format && ruff check app/ tests/` clean, `ty check app/` clean.
- `pytest --collect-only -q` → 0 import 에러 (collected 수 감소 정상).
- live 회귀: `pytest -k "order_brief_formatting or market_context or pending_orders or kr_morning_report or dust or reconcil or stock_detail"` 그린.

## 비목표
kr_morning_report 삭제(별개 결정), order_brief_formatting 변경, Prefect/operator 작업.
