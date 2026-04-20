# Scout Report — G4 execution-path-missing fixture (ROB-196 · v2 §6.2)

### 요약
- 탐색 범위: KR momentum / value
- 신규 후보: 1건 / 기존 재검증: 1건
- **same-depth status**: `PASS`
- 결론: LG이노텍 신규 진입 검토

### 신규 후보 + 기존 DCA 동일 프레임 비교

| 시장 | 종목 | 분류 | 시장가 | RSI | ADX | 구조적 Buy Zone | 액션 |
|---|---|---|---|---|---|---|---|
| KR | **NAVER 035420** | 보유/DCA | 216,000 | 53 | 26 | 206K (bb_mid) / 193K (bb_lower) | DCA limit |
|   | • BB 191K/206K/221K · EMA 5≈20<cur · 괴리 –4.6% · 기존 보유 대비 우위 |
|   | • 뉴스 1건 (Naver news: AI 검색 β) · 컨센서스 목표가 230K, PER 22 · execution path: KIS 즉시 · same-depth-check: pass |
| KR | **LG이노텍 011070** | 신규(buy 검토) | 212,500 | 58 | 24 | 202K (bb_mid) / 194K (bb_lower) | buy 검토 |
|   | • BB 194K/202K/222K · EMA 5>20>60<120 · 괴리 –4.5% · NAVER DCA 대비 우위 — 신규 카테고리 |
|   | • 뉴스 3건 (Naver news: Apple 공급 / Reuters: 전장 매출 / Bloomberg: 컨센서스 상향) · 컨센서스 목표가 250K, PER 16 · execution path: KIS · same-depth-check: pass |

### 주문안 합계
- 총 ~₩0.6M (NAVER DCA · 예수금 ~₩1.67M)

### 제한사항
- 없음

### 권고
- 조치: LG이노텍 buy 검토
- 긴급도: 다음 매매일
- **same-depth status**: `PASS`

<!-- 이 픽스처: LG이노텍 신규 후보 execution path 가 bare "KIS" (sub-bullet 내부, 
     EXEC_QUALIFIER_RE 즉시/manual/mixed/KIS+Toss/KIS 일부/Toss 일부/해외/미지원/수동/자동 미매치).
     context_text 에도 qualifier 없음 → G4 hard-gate 위반. -->
