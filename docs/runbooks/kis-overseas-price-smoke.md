# KIS 해외 현재가 라이브 스모크 (ROB-471)

`get_quote(market="us")`의 KIS-primary 전환(ROB-471)을 **라이브** 검증하는 read-only 스모크. ROB-471 Done 게이트.

- **CLI**: `scripts/kis_overseas_price_smoke.py`
- **테스트**: `tests/test_kis_overseas_price_smoke.py` (pure helper 단위)
- **관련**: 스펙 `docs/superpowers/specs/2026-06-09-rob471-get-quote-us-kis-overseas-design.md`, PR #1201 (merged)

## 무엇을/왜

운영 코드 `inquire_overseas_price`(TR `HHDFS00000300`)는 라이브 응답 `output`의 `last`→close / `base`→previous_close / `tvol`→volume 를 가정한다. **실 라이브 필드명은 레포에서 검증 불가**(creds 필요) — 이 스모크가 라이브 raw `output`을 떠서 그 가정을 확인한다. 필드가 다르면 가격이 빈값→Yahoo fallback 으로 떨어지며(`source=="yahoo"`), 이게 곧 "파서 매핑 조정 필요" 신호다.

## 안전 경계

- **READ-ONLY 조회 TR**. broker/order/watch/order-intent mutation 없음. 별도 enable 플래그 없음(주문 mutation이 없으므로).
- 라이브 KIS creds 미설정 시 누락 **env 키 NAME만** 보고하고 exit 3 (값 출력 없음).
- 호스트는 `KIS_BASE_URL` 라이브 그대로 사용.

## 사전 조건

라이브 KIS creds (`.env` 또는 환경변수):

```
KIS_APP_KEY=...
KIS_APP_SECRET=...
# (선택) KIS_ACCOUNT_NO — 현재가 조회엔 불필요
```

## 실행

```bash
# 기본: NASDAQ AAPL 현재가
uv run python -m scripts.kis_overseas_price_smoke --symbol AAPL --exchange NASD

# DB로 거래소 해석(운영 get_quote 경로와 동일) + get_quote 전체 경로 점검
uv run python -m scripts.kis_overseas_price_smoke --symbol BRK.B --resolve-exchange --via-get-quote

# NYSE 종목
uv run python -m scripts.kis_overseas_price_smoke --symbol JPM --exchange NYSE
```

## 확인 항목 (ROB-471 Done 체크)

1. **RAW output 필드명**: 출력된 `RAW KIS output dict`에 `last` / `base` / `tvol` 가 존재하는가? `필드명 체크`가 모두 `OK`인가?
   - 일부 `MISSING`이면 → 라이브 응답의 실제 키를 보고 운영 파서(`_build_overseas_price_frame`) 매핑을 조정하는 follow-up 필요.
2. **파싱 결과**: `close`(현재가) > 0, `previous_close` 채워짐.
3. **`--via-get-quote`**: `source == "kis_overseas"`, `delayed == True`, 합리적 `price`.
4. **거래소 분기**: `--exchange NYSE`(또는 `--resolve-exchange`)로 NYSE 종목도 정상 가격.
5. **세션 동작**: 프리마켓/애프터아워에 1회 실행 → KIS가 stale 전일종가/빈값 중 무엇을 주는지, `delayed:true`가 정직한지 확인.

## Exit codes

| code | 의미 |
|------|------|
| 0 | `last` 파싱 성공(price>0) + 필드 매핑 일치 — KIS-primary 라이브 OK |
| 2 | `last` 부재/0 — 필드명 불일치 의심 또는 비거래/세션. **raw output 확인** |
| 3 | 라이브 KIS creds 미설정 / 거래소 미해석 |
| 1 | 예기치 못한 예외 |

## 롤백 레버

라이브 KIS가 유효 티커에 빈값/오가격을 주면 즉시:

```
US_QUOTE_KIS_PRIMARY=false
```

→ Yahoo-primary(레거시)로 즉시 복귀. 이후 필드 매핑 follow-up 진행.

## Done 처리

위 확인 항목 1~3이 통과하면 ROB-471을 Done 처리한다. (필드명이 다르면 매핑 follow-up 이슈를 먼저 처리.)
