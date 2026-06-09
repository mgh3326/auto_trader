# ROB-392 Slice A — portfolio NAV scope 라벨 + code-as-name 매핑 설계

- **이슈:** ROB-392 (오케스트레이션 ROB-394의 4번) — **Slice A만**
- **날짜:** 2026-06-01
- **상태:** 설계 승인됨 → 구현 계획 대상
- **base:** `origin/main` `dcac00ef` (ROB-390 머지 직후)

## 목표

"invest_report 결과 = 운영자가 MCP로 직접 분석한 결과" 목표에서, 번들이 이미 정확히 담은 portfolio 수치가
**scope 라벨 부재**로 더 넓은 `get_holdings` total과 혼동되고, 일부 종목이 **코드=이름**으로 노출되는 두
정합성 결함을 좁게 고친다. read-only, 신규 HTTP surface 없음, 수치 로직 불변.

## 범위 결정 (분해)

ROB-392는 5개 증상의 멀티파트 이슈다. 본 PR은 **Slice A = 증상4 + 증상5**만 다룬다.

* **증상4 (portfolio NAV scope 불일치)** — IN. 라벨/metadata 정리.
* **증상5 (종목명=코드)** — IN. 이름 매핑 보정.
* **증상1/2 (KIS RSI/consensus/level을 symbol evidence로)** — OUT → **별도 이슈**. (아래 "증상1 전제
  정정" 참고: 큰 작업.)
* **증상3 (news pos/neg 합성)** — OUT. 이슈가 명시한 대로 by-design Hermes(out-of-process LLM) 담당.

### 증상1 전제 정정 (integrity)

증상1은 "012450 symbol **스냅샷에** KIS RSI 33.2·컨센서스·레벨이 **있는데** stage가 평탄화"라고 기술하나,
실측 결과 **`SymbolSnapshotCollector`는 RSI/consensus/support/resistance/upside를 전혀 담지 않는다**
(`quote`=bid/ask/spread/depth/venue만). 그 KIS evidence는 별도 MCP tool `analyze_stock_batch`가 생성하며
symbol 스냅샷으로 캡처되지 않는다. 따라서 "이미 있는 evidence를 승격"이 아니라 "evidence를 먼저 캡처"가
필요하며(collector enrichment + 신규 HTTP surface 가능성 + 안전 검토), 이는 Slice A 밖의 큰 작업이다.
ROB-389 candidate_universe와 동일한 "증상 전제 부정확" 패턴. → 별도 이슈로 분리하고 ROB-392/394에 기록.

## 코드 현황 (근거)

* `app/services/investment_stages/stages/portfolio_journal.py:43` `_krw_totals` —
  NAV = `sum(holdings[].value_krw) + cash.krw`. **`holdings`(KIS primary/매도가능)만 포함**,
  `reference_holdings`(ISA/Toss)는 NAV에서 제외(ROB-297 "no sellable merge"와 일치). 즉 NAV=30.9M은
  정확하나 scope 라벨이 없어 `get_holdings(kr)` total=51.5M(ISA+Toss 31종목)과 혼동된다.
* `app/services/action_report/snapshot_backed/collectors/portfolio.py` —
  `holdings`(KIS primary) / `reference_holdings`(manual·Toss) 분리, payload에 `count`/`reference_count`/
  `provenance.account_scope` 존재. 행 dict는 `display_name`을 담음(`_manual_row_to_dict`:row.display_name,
  `_reader_holding_to_dict`:h.displayName). display_name이 null이거나 ticker와 같으면 코드=이름 노출.
* `app/services/kr_symbol_universe_service.py:484` `get_kr_names_by_symbols(db, symbols)` — 심볼→이름 배치
  조회(DB universe). 이름 폴백의 정규 헬퍼. (신규 HTTP 없음.)

## 설계

### 변경 1 — portfolio NAV scope 라벨 (증상4)

수치 로직은 **전혀 건드리지 않는다**. portfolio collector payload(live KIS-primary 경로)에 scope metadata만
추가:

```python
"nav_scope": "kis_primary_sellable",
"nav_scope_label": (
    "NAV는 KIS 실거래(매도가능) 보유 + 현금 기준 · "
    "ISA/Toss 참조분(reference_holdings)은 제외"
),
```

`portfolio_journal` stage가 NAV를 surface할 때 이 라벨을 key_point(또는 summary 접미)로 함께 노출해,
운영자가 `get_holdings` total과의 차이를 scope 차이로 이해하게 한다. `reference_count`가 >0이면 "참조분 N건
별도"를 덧붙인다.

> crypto(upbit_live)/US 경로는 동일 라벨 키를 추가하되 라벨 문구는 시장에 맞게(또는 KR 전용으로 한정).
> 1차 구현은 **KR kis_live 경로에 집중**하고, 타 시장은 키 부재 시 stage가 라벨 없이 기존대로 동작(안전).

### 변경 2 — code-as-name 매핑 (증상5)

portfolio collector에서 행 dict(holdings + reference_holdings)를 만든 뒤, `display_name`이 falsy이거나
ticker와 동일한 행들을 모아 `get_kr_names_by_symbols(session, symbols)`로 이름을 폴백 해석하여 `display_name`을
채운다.

* 해석된 이름이 있으면 채우고, 없으면 **코드 유지**(거짓 이름 금지).
* KR 시장에 한정(`get_kr_names_by_symbols`는 KR universe). US/crypto는 이번 범위 밖.
* 신규 HTTP 없음(DB universe 조회). collector는 이미 `self._session`을 보유.

> symbol stage key_points의 코드 노출은 symbol 스냅샷 `name`(stock_info 유래) 부재 시 발생 →
> universe-sync 데이터 갭이므로 본 Slice 밖(비목표).

## 테스트 (fake/unit, read-only)

* **T1 (NAV scope 라벨):** KIS-live portfolio collect payload에 `nav_scope=="kis_primary_sellable"` +
  `nav_scope_label`(문자열) 존재. holdings/cash 수치는 라벨 추가 전후 동일(회귀).
* **T2 (portfolio_journal surface):** NAV가 있는 payload에 scope 라벨이 들어오면 stage의 key_points/summary에
  scope 문구가 노출된다(fake payload).
* **T3 (name 폴백 - 해석 성공):** reference 행 `display_name=None`(또는 ticker와 동일) + fake
  `get_kr_names_by_symbols`가 `{"035420": "NAVER"}` 반환 → 행 `display_name=="NAVER"`.
* **T4 (name 폴백 - 해석 실패):** lookup이 빈 dict 반환 → 행 `display_name`은 코드 유지(거짓 이름 없음).

## 안전 경계

* read-only. broker/order/watch/order-intent mutation 없음.
* **신규 HTTP surface 없음** — DB universe lookup(`get_kr_names_by_symbols`)만.
* **NAV 수치/merge 로직 불변** — 라벨/metadata만 추가. reference는 여전히 NAV에 미합산(ROB-297 유지).
* **DB 마이그레이션 없음** — payload additive 필드만.
* deterministic stage가 LLM 합성을 대체하지 않음 — 라벨/이름 보정만, 방향 판정(bull/bear) 생성 없음.
* `recommend_stocks` 무관.

## 산출물 / 핸드오프

* 독립 PR (base `origin/main` `dcac00ef`, worktree `auto_trader.rob-392`/branch `rob-392`).
* 검증 명령/결과 → PR + ROB-394 handoff 코멘트.
* **증상1/2(KIS evidence 캡처)는 별도 이슈로 분리** + 증상1 전제 부정확을 ROB-392/394에 명시.
* 다음 순서 ROB-391로 인계.

## 비목표 (Out of scope)

* 증상1/2 — symbol collector에 KIS RSI/consensus/support/resistance/upside 캡처 (별도 큰 이슈, 신규 HTTP
  surface 검토 필요).
* 증상3 — news 테마/감성 합성 (by-design Hermes compose).
* symbol stage key_points의 코드 노출 (universe-sync 데이터 갭).
* NAV 계산/merge 로직 변경 (수치 불변).
* US/crypto 이름 폴백 (KR 한정).
