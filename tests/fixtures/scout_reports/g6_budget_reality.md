# Scout Report — G6 budget-reality fixture (ROB-196 · v2 §6.2)

### 요약
- 탐색 범위: KR momentum / oversold
- 신규 후보: 1건 / 기존 재검증: 1건
- **same-depth status**: `PASS`
- 결론: NAVER + LG이노텍 Tier 1 분할 체결

### 신규 후보 + 기존 DCA 동일 프레임 비교

| 시장 | 종목 | 분류 | 시장가 | RSI | ADX | 구조적 Buy Zone | 액션 |
|---|---|---|---|---|---|---|---|
| KR | **NAVER 035420** | 보유/DCA | 216,000 | 53 | 26 | 206K (bb_mid) / 193K (bb_lower) | DCA limit |
|   | • BB 191K/206K/221K · EMA 5≈20<cur · 괴리 –4.6% · 기존 보유 대비 우위 |
|   | • 뉴스 1건 (Naver news: AI 검색 β) · 컨센서스 목표가 230K, PER 22 · execution path: KIS 즉시 · same-depth-check: pass |
| KR | **LG이노텍 011070** | 신규(buy) | 212,500 | 58 | 24 | 202K (bb_mid) / 194K (bb_lower) | buy |
|   | • BB 194K/202K/222K · EMA 5>20>60<120 · 괴리 –4.5% · NAVER DCA 대비 우위 |
|   | • 뉴스 3건 (Naver news: Apple / Reuters: 전장 / Bloomberg: 컨센서스) · 컨센서스 목표가 250K, PER 16 · execution path: KIS 즉시 · same-depth-check: pass |

### 주문안 합계
- NAVER DCA 10주 × 206K + LG이노텍 buy 15주 × 202K
- 총 ~₩5.0M

### 제한사항
- 없음

### 권고
- 조치: 위 주문안 분할 체결
- 긴급도: 다음 매매일
- **same-depth status**: `PASS`

<!-- 이 픽스처: 주문안 총액 5.0M vs 예수금 1.67M (caller-supplied) = 배수 2.99x > 1.5x.
     본문에 get_cash_balance 호출 흔적 없음 + BUDGET_DISCLOSURE_RE 매치 문구 없음
     → G6 hard-gate 위반. -->
