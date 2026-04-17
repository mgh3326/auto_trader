# Scout Report — G3 tool-failure fixture (ROB-196 · v2 §6.2)

### 요약
- 탐색 범위: KR momentum / oversold
- 신규 후보: 1건 / 기존 재검증: 1건
- **same-depth status**: `PASS`
- 결론: NAVER DCA 저가 보강 + Krafton watch

### 신규 후보 + 기존 DCA 동일 프레임 비교

| 시장 | 종목 | 분류 | 시장가 | RSI | ADX | 구조적 Buy Zone | 액션 |
|---|---|---|---|---|---|---|---|
| KR | **NAVER 035420** | 보유/DCA | 216,000 | 53 | 26 | 206K (bb_mid) / 193K (bb_lower) | DCA limit |
|   | • BB 191K/206K/221K · EMA 5≈20<cur · 괴리 –4.6% · 기존 보유 대비 우위 |
|   | • 뉴스 1건 (Naver news: AI 검색 β) · 컨센서스 목표가 230K, PER 22 · execution path: KIS 즉시 · same-depth-check: pass |
| KR | **Krafton 259960** | 신규(watch) | 266,500 | 64 | 18 | 244K (bb_mid) / 231K (bb_lower) | watch only |
|   | • BB 223K/244K/265K · EMA 5>20<120 · 괴리 –8.3% · NAVER DCA 대비 열위 |
|   | • 뉴스 2건 (Reuters: PUBG / Bloomberg: 규제) · 컨센서스 목표가 290K, PER 18 · execution path: KIS 즉시 · same-depth-check: pass |

### 주문안 합계
- 총 ~₩0.6M (NAVER DCA 3주 · 예수금 ~₩1.67M)

### 권고
- 조치: NAVER DCA limit, Krafton watch
- 긴급도: 다음 매매일
- **same-depth status**: `PASS`

<!-- 이 픽스처: screen_stocks tool failure 발생 가정 (caller 가 tool_failures arg 로 주입).
     본문에 `### 제한사항` 섹션이 없음 → G3 hard-gate 위반. -->
