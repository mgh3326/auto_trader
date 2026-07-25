# ROB-725 — analyze_stock_batch KR 프리마켓/NXT 현재가 오버레이

**날짜:** 2026-07-06
**이슈:** ROB-725 (Medium)
**범위:** 코어만 — NXT 오버레이 미러링 (TTL 단축·premarket_gap warning·quote_staleness_sec 제외)

## 문제

`analyze_stock_batch`의 KR `current_price`가 프리마켓(08:00~09:00 KST)·NXT
세션에서 실시간 호가보다 지연된다. 에이전트가 이 지연된 가격 기준으로 저항/지지
`distance_pct`를 계산해 매도/매수 지정가를 앵커하면 실시장과 괴리된 주문가가 나온다.

**실측 (2026-07-06 프리마켓):** 08:05 `analyze_stock_batch(192820)` →
`current_price 168300`, 저항 171,387. 이 값 기준 매도 지정가 171,000 접수 →
08:10 실제 NXT 체결 173,500 (실시장이 조회 시세보다 +3.1% 위). 08:36 재조회도
동일 168300.

## 근본 원인 (조사 완료 — venue-stale, cache-stale 아님)

두 경로가 독립적으로 응답을 채운다:

1. **Quote 경로 (매 호출 재계산):** `analyze_stock_impl`
   (`analysis_analyze.py:726`) → `_prepare_quote_tasks` → `_resolve_kr_quote`
   (`analysis_analyze.py:106`) → `_fetch_kr_live_quote`
   (`market_data_quotes.py:594`) → **`KISClient.inquire_price(code, market="J")`
   단일 호출.** `market="J"`는 KRX **정규장** 마켓 코드다. 프리마켓엔 정규장이
   닫혀 있어 KIS가 **전일 정규장 종가**를 반환한다. 이 경로엔 NXT 오버레이가 없다.

2. **Fetch-cache 경로 (naver 스냅샷만):** `_fetch_kr_snapshot_cached` →
   `analyze_cache` (TTL 15:35 KST까지, 세션-비인식) → `_apply_fetch_cache_metadata`
   (`analysis_analyze.py:625`)가 `cache_hit=true` + 프리즌된 `derived_as_of`를 생성.

**red herring:** 응답의 `cache_hit:true`/`derived_as_of:08:05`는 **naver
펀더멘털 스냅샷** 캐시 메타데이터로 quote와 **무관**하다. 운영자가 이를 "가격이
캐시됨"으로 오인했으나, 실제 quote는 매 호출 라이브 fetch되며 단지 **잘못된
venue(KRX 정규장)**에서 온다.

**대조 — 독립 `get_quote` 도구엔 이미 오버레이가 있다:** `_get_quote_impl`
(`market_data_quotes.py:1202`)은 ROB-464/ROB-511로 NXT 세션 감지
(`_nxt_quote_session`, `:324`)와 NXT 오버레이(`_fetch_nxt_quote_overlay`, `:379`
→ NXT 호가창 expected_price → mid → best_ask → best_bid)를 이미 적용한다
(`:1225-1237`). **analyze 경로만 이 오버레이가 누락됐다.**

**추가 관측:** `_resolve_kr_quote`는 이미 `is_stale_price`를 태그하지만
(`compute_is_stale`, `freshness.py:16`) **date-granularity**라서 오늘 날짜의 전일
종가는 stale로 잡히지 않는다.

## 수용 기준 (이슈)

프리마켓 조회 시 `current_price`가 실 NXT 호가와 ±0.5% 내 일치하거나, 불일치 시
staleness가 응답에 표기돼 에이전트가 보정 가능. → NXT 오버레이가 첫 번째 조건
(정확한 NXT 가격)을 충족한다.

## 설계

### 1. 공유 오버레이 헬퍼 추출 (`market_data_quotes.py`)

`_get_quote_impl`(`:1230-1237`)에 인라인된 오버레이 적용 로직을 헬퍼로 추출:

```python
async def _apply_nxt_quote_overlay(
    symbol: str, quote: dict[str, Any], *, data_state: str
) -> bool:
    """NXT 세션이면 quote에 NXT 파생가를 오버레이한다. 적용 시 True.

    quote를 in-place 수정: price → NXT expected/mid/best, price_source/session/
    venue 태그, data_state=fresh. NXT 세션이 아니거나 호가창 empty면 no-op(False).
    """
    session = await _nxt_quote_session(data_state)
    if session is None:
        return False
    overlay = await _fetch_nxt_quote_overlay(symbol, session=session)
    if overlay is None:
        return False
    quote.update(overlay)
    quote["regular_session_data_state"] = data_state
    quote["data_state"] = DATA_STATE_FRESH
    return True
```

`_get_quote_impl`은 이 헬퍼를 호출하도록 리팩터한다 (동작 불변 — 기존
`get_quote` 테스트로 회귀 보증). 리팩터 후 `_get_quote_impl`:

```python
data_state = kr_market_data_state()
quote = await _fetch_quote_equity_kr(symbol)
tradability = (await get_kr_nxt_tradability([symbol])).get(symbol)
if tradability is not None:
    quote.update(tradability.public_fields())
if await _apply_nxt_quote_overlay(symbol, quote, data_state=data_state):
    return quote
quote["data_state"] = data_state
return quote
```

### 2. analyze KR quote에 배선 (`analysis_analyze.py::_resolve_kr_quote`)

base quote(라이브 KIS `_fetch_kr_live_quote` 또는 일봉 fallback) + tradability
annotate **후** 오버레이를 적용한다. `kr_market_data_state()`로 `data_state`를 구한다.
오버레이 적용 시 가격이 NXT-fresh이므로 `is_stale_price=False`,
`price_as_of=now_kst().isoformat()`(라이브 호가창 fetch 시각 — 정직한 quote_asof)로
갱신한다. 세션 외/호가창 empty면 기존 KIS 경로 그대로(graceful).

```python
async def _resolve_kr_quote(symbol, ohlcv_df):
    trading_date = datetime.now(_KST).date()

    async def _finalize(quote):
        # tradability annotate (기존 _annotate)
        tradability = (await get_kr_nxt_tradability([symbol])).get(symbol)
        if tradability is not None:
            quote.update(tradability.public_fields())
        # ROB-725: NXT 세션이면 NXT 파생가로 오버레이
        if await _apply_nxt_quote_overlay(
            symbol, quote, data_state=kr_market_data_state()
        ):
            quote["is_stale_price"] = False
            quote["price_as_of"] = now_kst().isoformat()
        return quote

    live = await _fetch_kr_live_quote(symbol)
    if live is not None:
        as_of_dt = ...  # 기존 로직
        live["is_stale_price"] = compute_is_stale("price", as_of_dt, trading_date=trading_date)
        return await _finalize(live)

    fallback = _build_kr_quote_from_ohlcv(symbol, ohlcv_df)
    if fallback is None:
        return None
    ...  # 기존 fallback as_of/is_stale_price 세팅
    return await _finalize(fallback)
```

`_apply_nxt_quote_overlay`, `_nxt_quote_session`, `kr_market_data_state`는
`market_data_quotes` / `market_session`에서 import한다 (`market_data_quotes`는
`analysis_analyze.py:36`에서 이미 import 중).

**distance_pct 자동 보정:** `_recompute_intraday_support_resistance`
(`analysis_analyze.py:536`, `:559`)가 `quote.get("price")`를 basis로 S/R distance를
재산정한다. quote.price가 NXT가로 바뀌면 distance_pct는 **코드 변경 없이 자동
보정**된다.

### 3. compact 포맷터 노출 (`analysis_tool_handlers.py::_summarize_analysis_result`)

기존 `nxt_tradable*` passthrough 루프(`:785-792`) 옆에 오버레이 필드 passthrough를
추가한다 (존재할 때만):

```python
for _px_key in ("price_source", "session", "data_state", "venue"):
    if _px_key in quote:
        summary[_px_key] = quote[_px_key]
```

→ 에이전트가 `current_price`가 NXT 파생가(`price_source=nxt_expected_price` 등)임을
compact 응답에서 인지한다.

## 범위 밖 (의도적 제외)

- **naver TTL 단축 (이슈 제안 #2):** quote와 무관한 red herring. 펀더멘털은
  프리마켓에 안 변하므로 15:35 TTL이 정당하다.
- **premarket_gap warning (제안 #4):** 별도 후속.
- **quote_staleness_sec 명시 필드:** NXT 오버레이가 정확한 가격을 주므로 불필요.
  KRX fallback 잔여 staleness는 기존 `is_stale_price`로 커버.

## 데이터 흐름 (수정 후, KR 프리마켓)

```
_resolve_kr_quote
  → _fetch_kr_live_quote (KIS market="J", 전일 종가)   [base]
  → tradability annotate
  → _apply_nxt_quote_overlay(data_state)
       → _nxt_quote_session → "nxt_premarket"
       → _fetch_nxt_quote_overlay → get_orderbook(venue="nxt")
       → quote.price = NXT expected_price/mid, data_state=fresh, price_source=nxt_*
       → is_stale_price=False, price_as_of=now
  → quote → analysis["quote"]
  → _recompute_intraday_support_resistance (quote.price 기준 distance_pct 재산정)
  → _summarize_analysis_result (current_price + price_source/session 노출)
```

## 테스트

1. **NXT 세션 오버레이:** `_nxt_quote_session`→"nxt_premarket",
   `_fetch_nxt_quote_overlay`→NXT가 모킹. `_resolve_kr_quote` 결과
   `quote.price == NXT가`, `price_source` set, `data_state=fresh`,
   `is_stale_price=False`.
2. **세션 외 유지:** `_nxt_quote_session`→None. quote.price == KIS price,
   오버레이 필드 없음.
3. **호가창 empty graceful:** `_fetch_nxt_quote_overlay`→None. KIS price 유지,
   예외 없음.
4. **distance_pct 재-앵커:** analyze 결과에서 S/R `distance_basis_price` ==
   NXT가, distance_pct가 NXT가 기준.
5. **compact 노출:** `_summarize_analysis_result`가 오버레이 있을 때
   `price_source`/`session` surface.
6. **`_get_quote_impl` 회귀:** 헬퍼 추출 후 기존 get_quote NXT 테스트 green.

## 안전 경계

- **read-only:** 브로커/주문/감시 mutation 없음. `get_orderbook`은 read.
- **migration 0.**
- **추가 네트워크:** NXT 세션(08:00-09:00, 15:30-20:00)에만 심볼당
  `get_orderbook(venue="nxt")` 1콜 추가. 배치는 심볼별 concurrent. 세션 외 오버헤드 0.
- **fail-open:** 오버레이 실패 시 항상 KIS 경로로 degrade.
