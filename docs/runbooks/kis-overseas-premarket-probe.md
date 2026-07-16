# KIS 해외 프리마켓 실측 프로브 (ROB-922)

`HHDFS00000300`(KIS 해외 현재가)이 US 프리마켓 창(21:00~22:25 KST, 서머타임 기준
08:00~09:25 ET)에 **실제 프리마켓 가격**을 주는지, 아니면 **전일종가를 그대로**
돌려주는지 실측하는 read-only 진단 스크립트. ROB-922 AC(3) 게이트.

- **CLI**: `scripts/kis_overseas_premarket_probe.py`
- **관련**: ROB-922 (Linear, 에픽 ROB-921), `docs/runbooks/kis-overseas-price-smoke.md`(ROB-471, 인접 스모크)

## 무엇을/왜

07-16 실측(us_research 리서치)에서 MAN이 08:00 ET 프리마켓에 +11.7% 움직였지만,
운영 표면(`get_quote(market="us")`) 어디서도 그 가격이 보이지 않았다. 원인은
4경로 전부에 프리마켓 파라미터가 없었기 때문(`app/mcp_server/tooling/market_data_quotes.py`,
`app/services/brokers/yahoo/client.py`, `app/services/brokers/kis/constants.py`).

ROB-922는 두 opt-in 경로를 추가했다:

1. Yahoo `fetch_prepost_quote` (yfinance `Ticker.history(prepost=True)` 기반, 검증됨)
2. KIS `HHDFS00000300`이 프리마켓 시간대에 실제로 무엇을 반환하는지는 **레포에서
   검증 불가**(라이브 creds 필요) — 이 프로브가 그 사실을 라이브로 확인한다.

같은 심볼에 대해 Yahoo prepost 결과를 대조군으로 나란히 출력해서, KIS 가격이
프리마켓 실가격에 가까운지(신뢰 가능) 전일종가에 고정돼 있는지(신뢰 불가) 사람이
판단할 근거를 만든다.

## 안전 경계

- **READ-ONLY 조회 TR만 호출**: `KISClient().inquire_overseas_price`
  (`HHDFS00000300`). 주문/계좌 TR 절대 호출하지 않음.
- 브로커/주문/워치/order-intent mutation 없음. 스케줄러/TaskIQ 연결 없음
  (명시 실행에서만 동작).
- 라이브 KIS creds 미설정 시 누락 **env 키 NAME만** 보고하고 exit 3 (값 출력 없음).
- 호스트는 `KIS_BASE_URL` 라이브 그대로 사용.

## 사전 조건

라이브 KIS creds (`.env` 또는 환경변수):

```
KIS_APP_KEY=...
KIS_APP_SECRET=...
```

## 실행 절차

**창: 21:00~22:25 KST** (US 프리마켓, `us_market_session()`이 `premarket`을
반환하는 구간 — 서머타임 여부에 따라 ±1시간 이동 가능하니 스크립트 출력의
`us_market_session` 필드로 실제 세션을 확인할 것).

```bash
# 기본 심볼(AAPL, NVDA)
uv run python -m scripts.kis_overseas_premarket_probe

# 특정 심볼
uv run python -m scripts.kis_overseas_premarket_probe --symbols AAPL,NVDA,MAN

# JSON 출력(기록/비교용)
uv run python -m scripts.kis_overseas_premarket_probe --symbols AAPL --json
```

## 출력 해석

각 심볼에 대해:

- `us_market_session` — 로컬 시각이 실제로 프리마켓 창에 있는지 확인.
- `kis.price` vs `kis.previous_close` — **동일하면** KIS가 전일종가를 그대로
  돌려주고 있다는 강한 의심 신호. **다르면** 실제 프리마켓 가격을 반영하고
  있다는 신호.
- `yahoo_prepost.price` — 대조군. `kis.price`와 근접하면 KIS 프리마켓 가격이
  신뢰 가능하다는 근거가 강해진다.
- `kis.quote_asof` — KIS가 준 타임스탬프가 당일 프리마켓 시각을 가리키는지
  (전일 날짜/시각이면 stale 신호).

## 실측 결과

> **pending — operator 실행 후 기입.** (자동으로 "실측했다"고 기입하지 않는다 —
> 21:00~22:25 KST 창에서 실제로 실행한 사람이 아래 표를 채운다.)

| 날짜(KST) | 심볼 | us_market_session | kis.price | kis.previous_close | yahoo_prepost.price | 판정(실가격 / 전일종가 고정) |
|-----------|------|--------------------|-----------|---------------------|----------------------|-------------------------------|
| pending   | pending | pending | pending | pending | pending | pending |

## 후속 조치

- KIS가 전일종가 고정으로 판정되면: `get_quote(market="us", include_extended_hours=True)`의
  Yahoo prepost 오버레이(ROB-922)가 이미 정직한 라벨(`price_source="yahoo_prepost_last"`)로
  실가격을 채워주므로 별도 코드 변경 불필요 — 이 문서에 실측 결과만 남기면 충분.
- KIS가 실제 프리마켓 가격을 준다고 판정되면: follow-up 이슈로 KIS를
  premarket/afterhours opt-in 경로의 primary 소스로 승격하는 안을 검토
  (현재는 Yahoo prepost만 opt-in 오버레이로 배선돼 있음).
