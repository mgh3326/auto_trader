# ROB-902 — get_holdings KR itemchartprice N+1 (ROB-830 잔여)

## 요약 (근본원인 한 줄)

get_holdings 안 KR 현재가 refresh 가 **KIS 국내 잔고 API(`fetch_my_stocks`)가 이미
벌크로 내려주는 스냅샷(prpr/평가금액/평가손익)을 무시하고 보유 종목마다 개별
`inquire-daily-itemchartprice` 를 다시 부른다.** US 는 PR #288(ROB-365)에서
"KIS 스냅샷이 유효하면 live refresh 를 건너뛴다"는 규칙을 얻었지만 **KR 은 그 규칙
대상에서 빠져 있어(#288 이 US-scoped)** 매 호출마다 ~41 KR 종목 × 1 HTTP = N+1 이
남았다. → 가설 **3 (경로 미적용/콜사이트 배선)** 확정. 가설 1(캐시)·2(조건)는 반증.

## 판별 근거 (조사 우선)

### 콜사이트 특정
- get_holdings 의 KR 현재가는 `_fetch_price_map_for_positions.fetch_equity_price`
  (`app/mcp_server/tooling/portfolio_holdings.py:747`) 에서 결정된다.
- ROB-830 이 배선한 DB-first 는 `cache_first_kr(symbol, 2)`
  (`portfolio_holdings.py:749`). **miss 시 fallback** 이 `_fetch_quote_equity_kr`
  (`market_data_quotes.py:688`) → `kis.inquire_daily_itemchartprice(n=2)`
  (`market_data_quotes.py:691`). 이게 Sentry 가 잡은 132/24h 콜의 실제 발원지.

### 가설 1 (DB miss 상시 → 짧은 TTL 캐시가 답) — **기각**
- Sentry 24h 실측: `transaction:"tools/call get_holdings"` itemchartprice **123 spans**,
  그러나 `count_unique(trace) = 3`. 즉 **get_holdings 는 24h 에 3번 호출**되었고
  호출당 **~41 KR 종목**을 개별 fetch. (issue 의 "4 tool-call × ~33" 과 동일 패턴.)
- 콜 시각(모두 KST 장중, 15:35 cutoff 이전):
  - `2026-07-16T02:46 UTC = 11:46 KST` (~50 spans, 단일 호출)
  - `2026-07-16T00:44 UTC = 09:44 KST` (~10 spans)
- 장중이라 `cache_first_kr` 의 `kr_daily_bar_may_be_forming` 게이트
  (`read_service.py:272`) 가 **설계상 None 을 반환** → DB-first 우회. 맞다.
- **그러나** 병목은 "호출당 41개 고유 종목 fan-out" 이다. 3번의 호출은 2h+ 간격
  으로 흩어져 있고 각 호출 내부는 **고유 종목(중복 없음, `equity_pairs` 는 set
  dedup, `portfolio_holdings.py:817`)**. → **짧은 TTL 캐시는 이 접근 패턴을 못
  줄인다** (cold-miss on unique symbols within one call; 호출 간 간격 > any 짧은 TTL).
  캐시는 무효.

### 가설 2 (조건 불일치) — **기각**
- `cache_first_kr` 는 심볼/venue/count 조건 불일치가 아니라 **의도된 장중 게이트**로
  None 을 낸다. 조건 교정으로 장중 오늘 현재가를 DB 에서 낼 수는 없다(오늘 봉 미적재).

### 가설 3 (경로 미적용) — **확정**
- KIS 국내 잔고 수집(`_collect_kis_positions`, `portfolio_holdings.py:352`)는 보유
  종목마다 이미 완전한 스냅샷을 벌크로 담는다:
  `current_price=prpr`(367), `evaluation_amount=evlu_amt`(369),
  `profit_loss=evlu_pfls_amt`(370), `profit_rate=evlu_pfls_rt`(371).
- US 는 동일한 4필드 스냅샷(`now_pric2`/`ovrs_stck_evlu_amt`/...)을 담고,
  `_has_valid_kis_equity_us_snapshot`(623) 로 **유효하면 refresh 를 건너뛴다**
  (`_position_needs_current_price_refresh`, 647). PR #288 README:
  *"KIS US holdings keep KIS-provided snapshot values ... Yahoo is a fallback
  refresh path, not the default for valid KIS US holdings."*
- **KR 은 동일 스냅샷을 갖고도 이 예외에서 빠져 있다** → 매번 itemchartprice N+1.

## 수정 (최소·원칙적)

US 전용 스냅샷 예외를 **KR 로 확장**한다 (PR #288 이 세운 규칙을 KR 에도 적용):

- `_has_valid_kis_equity_us_snapshot` → `_has_valid_kis_equity_snapshot` 로 일반화,
  `equity_kr` **또는** `equity_us` 의 KIS-account(`source == "kis_api"`) 포지션에서
  스냅샷 4필드가 수치상 유효(`current_price>0`, `evaluation_amount>0`,
  `profit_loss`/`profit_rate` 파싱가능)하면 True.
- `_position_needs_current_price_refresh` 가 이를 호출 → 유효 KR KIS 스냅샷은
  `equity_pairs` 에서 제외 → **cache_first_kr / itemchartprice 를 아예 안 부른다.**

효과: KIS-account KR 보유의 itemchartprice 123/24h → **0** (Toss/manual/screenshot
등 스냅샷 없는 KR 만 기존 DB-first→KIS fallback 유지). issue 목표 "132 → 수 콜" 달성.

## 정확성 불변식 (값 동일) — 편차 명시

- **스냅샷 없는 KR(Toss/manual/source≠kis_api)**: 경로 **완전 불변**. 여전히
  `cache_first_kr`(DB-first, ROB-830) → miss 시 `_fetch_quote_equity_kr`. 값 동일.
- **KIS-account KR (스냅샷 유효)**: 표시 `current_price` 소스가
  itemchartprice-close → **잔고 API prpr** 로 바뀐다. 둘 다 KIS 실시간 현재가(같은
  브로커, 같은 순간 ±틱). 이는 **US 가 이미 #288 이후 채택한 동작과 동일한 정렬**
  이며, 잔고 스냅샷(평가금액/평가손익 포함)이 더 authoritative. 엄밀 byte-동일은
  아니나(issue "같은 데이터, 소스만" 의 초과), 장중 오늘 현재가는 본질적으로 live
  per-symbol 소스라 캐시/DB 로는 값 보존+콜감축을 동시 달성 불가 → **#288 설계
  의도에 부합하는 정렬을 선택**. profit_loss/profit_rate/evaluation_amount 도 잔고
  스냅샷 값을 그대로 유지(현재는 itemchartprice-close 로 재계산). 반환 스키마 불변.

## 회귀/테스트 (TDD)

- RED→GREEN: KIS-account KR 유효 스냅샷 → `_fetch_price_map_for_positions` 가
  cache_first_kr/`_fetch_quote_equity_kr` 를 **0회** 호출(카운터 증명).
- 스냅샷 미유효/manual KR → 기존대로 refresh(기존 테스트 green 유지).
- US 스냅샷 skip 계속 유효(기존 테스트 green 유지).
- `_has_valid_kis_equity_snapshot` 단위 표(KR true/false, US true).

## 안전 경계

read/enrichment 전용. 주문/mutation 무변경. migration 0.
