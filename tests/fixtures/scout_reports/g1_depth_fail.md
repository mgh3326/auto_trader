# Scout Report — G1 depth-fail fixture (ROB-196 · v2 §6.2)

### 요약
- 탐색 범위: KR momentum / oversold
- 신규 후보: 1건 / 기존 재검증: 1건
- **same-depth status**: `FAIL`
- 결론: 삼성바이오로직스 재분석 필요 — 보드 액션 보류

### 신규 후보 + 기존 DCA 동일 프레임 비교

| 시장 | 종목 | 분류 | 시장가 | RSI | ADX | 구조적 Buy Zone | 액션 |
|---|---|---|---|---|---|---|---|
| KR | **NAVER 035420** | 보유/DCA | 216,000 | 53 | 26 | 206K (bb_mid) / 193K (bb_lower) | DCA limit |
|   | • BB 191K/206K/221K · EMA 5≈20<cur · 괴리 –4.6% · P&L +2.4% · 기존 보유 대비 우위 |
|   | • 뉴스 1건 (Naver news: AI 검색 β) · 컨센서스 목표가 230K, PER 22 · execution path: KIS 즉시 · same-depth-check: pass |
| KR | **삼성바이오로직스 207940** | 신규(watch) | 978,000 | 71 | — | 920K 근처 | watch only |
|   | • EMA 5>20 · 괴리 — · execution path: KIS 즉시 · same-depth-check: fail (news / consensus / S-R 누락) |

### 주문안 합계
- 총 ~₩0.6M (NAVER DCA 3주 · 예수금 ~₩1.67M)

### 제한사항
- 없음

### 권고
- 조치: 삼성바이오로직스 deep-dive 재요청, NAVER DCA 유지
- 긴급도: 참고
- **same-depth status**: `FAIL` (삼성바이오로직스 news/consensus/S-R 누락)
