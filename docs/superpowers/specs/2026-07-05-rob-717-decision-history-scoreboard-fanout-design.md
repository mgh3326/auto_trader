# ROB-717 — decision_history→scoreboard 팬아웃 성능 완화 + ROB-713 검증 minor 일괄

**Date:** 2026-07-05
**Status:** Design approved
**Scope:** backend only, migration 0
**Owned files:** `app/services/trade_journal/aggregates.py`, `app/services/decision_history.py` (+ tests), `tests/test_mcp_trading_scoreboard.py`

## 배경

ROB-711의 `build_decision_context`가 `realized_r_by_tag`를 채우려고 cache-miss마다
`build_trading_scoreboard(market)`를 호출한다 (`decision_history.py:148-150`, lazy import).
이 함수(`aggregates.py::build_trading_scoreboard`)는:

1. 3개 라이브 레저 전체 스캔 (`load_fills`)
2. closed-trade당 태그/스톱 쿼리 (`resolve_setup_tag`, `planned_stop_for` — 각 ~5 쿼리)
3. closed-trade당 일봉 OHLCV 조회 (`compute_excursions` → `get_ohlcv`) — **DB miss 시 브로커
   네트워크 폴백(Yahoo/Toss/KIS/Upbit)**

decision_history는 /invest 종목상세에서 3.0s optional-block timeout 아래에 있다
(`stock_detail_service.py:417-431`). 종결 거래가 쌓이면 TTL(300s) 만료 후 첫 호출이
timeout을 초과 → `decision_history_timeout` 경고와 함께 블록이 5분마다 간헐 소실.
MCP 주입 경로(`analyze_stock_batch`)도 같은 비용. 지금은 거래 수가 적어 안 터지지만 구조적
(ROB-699~702 /invest 슬로우니스 계열의 잠복 병목).

decision_history 주입에는 **realized R·expectancy만** 필요하고 MAE/MFE는 쓰지 않는다
(MAE는 scoreboard MCP·웹 표면에서만 소비).

## 결정 사항 (사용자 승인)

- **팬아웃 완화: Option A** — `include_excursions` 파라미터로 decision_history 경로에서
  excursions 계산 자체를 스킵. scoreboard MCP·웹은 기본 True 유지(MAE 정확도 보존).
- **minor (c) degraded 플래그: Surface** — scoreboard 그룹에 `excursions_degraded` 카운트 추가.

## Part 1 — 팬아웃 완화 (Option A)

### 1a. `build_trading_scoreboard(..., include_excursions: bool = True)`

- 새 keyword 파라미터, 기본 `True` (백컴팻).
- `include_excursions=False`이면 per-trade 루프에서 `compute_excursions` 호출을 **완전히
  생략** → `get_ohlcv` 호출 0. 해당 trade의 `mae`/`mfe`는 `None`.
- `_agg_one`은 빈 MAE/MFE 리스트에서 자연히 `avg_mae=None`, `avg_mfe=None`, `worst_mae=None`,
  `excursions_degraded=0`을 반환한다 (기존 `if maes else None` 가드가 이미 처리).

### 1b. Cache key에 `include_excursions` 포함 (정합성 필수)

- 현재 캐시 키: `(market, account_mode, date_from, date_to, setup_tag, min_sample)`.
- `include_excursions`를 키에 추가한다. 그렇지 않으면 no-excursions 결과(MAE 없음)가
  MCP 호출자(MAE 원함)에게 서빙되거나 그 반대가 발생.

### 1c. `decision_history._realized_r_by_tag` 호출 변경

- `build_trading_scoreboard(db, market=market, include_excursions=False)`로 호출.
- 이 경로는 3s timeout 아래 read-path → **브로커 네트워크 호출 0, OHLCV 0**.
- `_R_KEYS`의 `avg_mae`는 이제 항상 `None` (주입 목적상 허용 — MAE는 scoreboard 표면 전용).

### 1d. MCP/웹 경로 불변

- `get_trading_scoreboard` MCP tool (`trading_scoreboard_tools.py`)은 기본 `True` → MAE
  그대로 계산·정확.

## Part 2 — minor 체크리스트 5건 (전부 소유 파일 내)

### (a) top-3 슬라이스 순서 교정 — `decision_history.py:151-158`

현재: `sorted(...)` → `[:_MAX_TAGS]` 슬라이스 → 루프 안에서 `untagged` skip.
→ untagged가 상위에 있으면 3개 미만/0개 반환 가능.
**Fix:** untagged를 슬라이스 **전에** 제거하고, 그 다음 정렬·슬라이스.

```python
groups = [g for g in board.get("groups", []) if g["tag"] != "untagged"]
ordered = sorted(groups, key=lambda g: (g["tag"] != setup_tag, -int(g["n"])))
out = {g["tag"]: {k: g.get(k) for k in _R_KEYS} for g in ordered[:_MAX_TAGS]}
```

### (b) `load_fills` smoke 필터 확장 — `aggregates.py:152-155`

현재 `_is_smoke(correlation_id, status)`만 검사. `toss_live_smoke.py:88`은 `reason`에 마킹.
3개 레저 모델 모두 `reason`/`thesis`/`strategy`/`notes` 컬럼 보유 (review.py 확인).
**Fix:** `_is_smoke(correlation_id, status, reason, thesis, strategy, notes)` (getattr 방어).
fill-gated라 저위험이나 부분체결 스모크 누수 방지.

### (c) `compute_excursions` degraded surface — `aggregates.py:396-528`

현재 `mae, mfe, _degraded = await compute_excursions(t)` — `degraded` 계산되고 버려짐.
**Fix (Surface):**
- `TradeMetrics`에 `degraded: bool` 필드 추가.
- 루프에서 `degraded`를 `TradeMetrics`에 저장 (스킵 시 `False`).
- `_agg_one`에서 그룹별 `excursions_degraded = sum(1 for r in rows if r.degraded)` 노출.
- `overall`에도 동일 반영 (`_agg_one("__overall__", rows)`가 이미 rows 전체를 받음).

### (d) 모듈 캐시 공유 mutable dict — `aggregates.py:501-504, 540-542`

현재 `_scoreboard_cache`가 동일 dict 객체를 반환 → 호출자가 mutate 시 캐시 오염.
**Fix:** 캐시 hit·store 시 `copy.deepcopy`로 격리 (반환값도 매번 새 객체).

### (e) `test_scoreboard_tool_empty_db_shape` hermetic화 — `tests/test_mcp_trading_scoreboard.py`

현재 실 `AsyncSessionLocal` 사용 → 공유 CI DB에 다른 스위트가 fill을 commit하면
`count==0`/`groups==[]` 가정이 flake + 실 `get_ohlcv` 발화(네트워크).
**Fix:** `build_trading_scoreboard`를 tool 경계에서 mock하여 shape만 단언(`{"groups",
"overall", "as_of", "count"}` 부분집합), DB·네트워크 비의존.

## 테스트

**신규 (aggregates):**
- `include_excursions=False` 경로: `get_ohlcv`를 patch하여 **호출 0** 단언 + realized-R
  그룹은 여전히 반환.
- Cache key 분리: 동일 인자에 `include_excursions` True/False가 서로 오염 안 됨.
- (b): `reason`에 smoke 마킹된 fill row가 필터됨.
- (c): 200일 초과 span trade가 `excursions_degraded` 카운트에 반영.
- (d): 반환 dict를 mutate해도 두 번째 호출 결과 불변.

**신규 (decision_history):**
- (a): untagged가 지배적일 때도 태그 맵이 실 태그를 반환(3개 미만이면 있는 만큼).
- `_realized_r_by_tag`가 `include_excursions=False`로 호출하는지 확인(mock spy).

**수정 (e):** hermetic 재작성.

**로컬 성공 기준 검증:** 종목상세 cache-miss decision_history 호출이 3s 내 안정 수렴,
read-path 브로커 네트워크 호출 0 (실측/로그 확인).

## 성공 기준 (이슈)

- [ ] /invest 종목상세 cache-miss 첫 호출이 3s timeout 안에 안정 수렴, read-path 브로커
  네트워크 호출 0.
- [ ] minor 체크리스트 5건 정리.

## Non-goals

- insights 판단성적표 group_by=setup 컬럼 확장 → ROB-715 계열(웹). 이 이슈 아님.
- `decision_history.py`/`aggregates.py`는 이 이슈 소유; ROB-715는 소비만.
- 스키마 변경 없음 (migration 0).
