# Scout Report — G6 budget-reality fixture (ROB-196)

### 요약
- 탐색 범위: KR momentum / oversold
- 신규 후보: 1건 / 기존 재검증: 1건
- **same-depth status**: `PASS`
- 결론: NAVER + LG이노텍 Tier 1 분할 체결

### 보유 + 신규 후보 동일 깊이 비교

| 종목 | 시장가 | 지표 | BB/EMA | S/R | 뉴스 | 컨센서스/목표가 | DCA 대비 비교 | 실행경로 |
|---|---|---|---|---|---|---|---|---|
| **NAVER 035420** holdings/DCA | 216,000 | RSI 53, ADX 26 | BB 191K/206K/221K, EMA 5≈20<cur | 지지 206K (bb_mid) / 193K (bb_lower) | 뉴스 1건 (Naver: AI 검색 β) | 컨센서스 목표가 230K, PER 22 | 기존 보유 대비 우위 | KIS 즉시 |
| **[신규]** LG이노텍 011070 | 212,500 | RSI 58, ADX 24 | BB 194K/202K/222K, EMA 5>20>60<120 | 지지 202K (bb_mid) / 194K (bb_lower) | 뉴스 3건 (Naver: Apple / Reuters: 전장 / Bloomberg: 컨센서스) | 컨센서스 목표가 250K, PER 16 | NAVER 대비 우위 | KIS 즉시 |

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
