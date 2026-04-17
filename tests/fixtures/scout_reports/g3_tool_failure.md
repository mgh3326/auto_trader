# Scout Report — G3 tool-failure fixture (ROB-196)

### 요약
- 탐색 범위: KR momentum / oversold
- 신규 후보: 1건 / 기존 재검증: 1건
- **same-depth status**: `PASS`
- 결론: NAVER DCA 저가 보강 + Krafton watch

### 보유 + 신규 후보 동일 깊이 비교

| 종목 | 시장가 | 지표 | BB/EMA | S/R | 뉴스 | 컨센서스/목표가 | DCA 대비 비교 | 실행경로 |
|---|---|---|---|---|---|---|---|---|
| **NAVER 035420** holdings/DCA | 216,000 | RSI 53, ADX 26 | BB 191K/206K/221K, EMA 5≈20<cur | 지지 206K (bb_mid) / 193K (bb_lower) | 뉴스 1건 (Naver: AI 검색 β) | 컨센서스 목표가 230K, PER 22 | 기존 보유 대비 우위 | KIS 즉시 |
| **[신규]** Krafton 259960 | 266,500 | RSI 64, ADX 18 | BB 223K/244K/265K, EMA 5>20<120 | 지지 244K (bb_mid) / 231K (bb_lower) | 뉴스 2건 (Reuters: PUBG / Bloomberg: 규제) | 컨센서스 목표가 290K, PER 18 | NAVER 대비 열위 | KIS 즉시 |

### 주문안 합계
- 총 ~₩0.6M (NAVER DCA 3주 · 예수금 ~₩1.67M)

### 권고
- 조치: NAVER DCA limit, Krafton watch
- 긴급도: 다음 매매일
- **same-depth status**: `PASS`

<!-- 이 픽스처: screen_stocks tool failure 발생 가정 (caller 가 tool_failures arg 로 주입).
     본문에 `### 제한사항` 섹션이 없음 → G3 hard-gate 위반. -->
