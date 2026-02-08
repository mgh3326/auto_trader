---
name: auto-trader-mcp
description: >
  auto_trader MCP 서버를 통해 주식/코인 시세, 포트폴리오, 기술적 분석 데이터 조회 및 주문 실행.
  "주가 알려줘", "현재가", "차트", "종목 검색", "포트폴리오", "보유 종목",
  "기술적 지표", "뉴스", "재무제표", "밸류에이션", "김치 프리미엄",
  "매수/매도 주문", "DCA 계획" 등의 요청 시 사용.
  MCP 서버가 같은 호스트(127.0.0.1:8765)에서 실행 중일 때 동작.
---

# auto-trader-mcp

auto_trader FastMCP 서버에 연결하여 시장 데이터를 조회하고 주문을 실행하는 스킬.

## 연결 정보

- **엔드포인트**: `http://127.0.0.1:8765/mcp`
- **프로토콜**: streamable-http (기본) / SSE / stdio
- **인증**: 없음 (localhost 전용, network_mode: host)

## MCP 호출 방법

```bash
# MCP 표준 JSON-RPC로 tool 호출
curl -s -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {"name": "TOOL_NAME", "arguments": {ARGS}},
    "id": 1
  }'
```

## 도구 목록

### 시세/검색
| 도구 | 설명 | 주요 파라미터 |
|------|------|--------------|
| `search_symbol` | 종목 검색 | `query`, `limit=20` |
| `get_quote` | 현재가 조회 | `symbol`, `market?` |
| `get_ohlcv` | 캔들/차트 데이터 | `symbol`, `count=100`, `period=day`, `end_date?`, `market?` |

### 포트폴리오
| 도구 | 설명 | 주요 파라미터 |
|------|------|--------------|
| `get_holdings` | 보유 종목 전체 조회 | `account?`, `market?`, `include_current_price=true`, `minimum_value=1000` |
| `get_position` | 특정 종목 포지션 | `symbol`, `market?` |
| `get_cash_balance` | 현금 잔고 | - |

### 기술적 분석
| 도구 | 설명 | 주요 파라미터 |
|------|------|--------------|
| `get_indicators` | 기술적 지표 (RSI, MACD 등) | `symbol`, `market?` |
| `get_volume_profile` | 거래량 프로파일 | `symbol`, `market?`, `period=60`, `bins=20` |
| `get_fibonacci` | 피보나치 되돌림 | `symbol`, `market?` |
| `get_support_resistance` | 지지/저항선 | `symbol`, `market?` |

### 펀더멘털
| 도구 | 설명 | 주요 파라미터 |
|------|------|--------------|
| `get_news` | 뉴스 | `symbol`, `market?` |
| `get_company_profile` | 기업 개요 | `symbol`, `market?` |
| `get_crypto_profile` | 코인 개요 | `symbol` |
| `get_financials` | 재무제표 | `symbol`, `market?` |
| `get_valuation` | 밸류에이션 (PER/PBR 등) | `symbol`, `market?` |
| `get_investment_opinions` | 투자 의견/목표가 | `symbol`, `market?` |
| `get_sector_peers` | 동종 업계 비교 | `symbol`, `market?` |

### 기관/내부자
| 도구 | 설명 | 주요 파라미터 |
|------|------|--------------|
| `get_insider_transactions` | 내부자 거래 | `symbol`, `market?` |
| `get_investor_trends` | 투자자 동향 | `symbol`, `market?` |
| `get_earnings_calendar` | 실적 발표 일정 | `symbol`, `market?` |
| `get_short_interest` | 공매도 비율 | `symbol`, `market?` |

### 코인 전용
| 도구 | 설명 | 주요 파라미터 |
|------|------|--------------|
| `get_kimchi_premium` | 김치 프리미엄 | `symbol?` |
| `get_funding_rate` | 펀딩 비율 | `symbol` |

### 시장 지수
| 도구 | 설명 | 주요 파라미터 |
|------|------|--------------|
| `get_market_index` | 시장 지수 (코스피, S&P500 등) | `symbol`, `market?` |

### 주문 (⚠️ 실거래)
| 도구 | 설명 | 주요 파라미터 |
|------|------|--------------|
| `place_order` | 매수/매도 주문 | `symbol`, `market`, `side`, `quantity`/`amount` |
| `get_open_orders` | 미체결 주문 조회 | - |
| `cancel_order` | 주문 취소 | `order_id` |
| `simulate_avg_cost` | 평단 시뮬레이션 | `symbol`, `market?` |
| `create_dca_plan` | DCA 분할매수 계획 | `symbol`, `market?` |
| `update_manual_holdings` | 수동 보유 종목 업데이트 | `symbol`, `market?` |

## Market 라우팅

`market` 파라미터로 시장을 지정하거나, 심볼로 자동 판별:
- **KR**: `kr`, `kis`, `krx`, `kospi`, `kosdaq` 또는 6자리 숫자 코드
- **US**: `us`, `yahoo`, `nasdaq`, `nyse` 또는 영문 티커
- **Crypto**: `crypto`, `upbit` 또는 `KRW-`/`USDT-` 접두사

## Account 필터 (get_holdings)

`account` 파라미터로 계좌 필터링:
- `kis`: 한국투자증권
- `upbit`: 업비트
- `toss`: 토스증권
- `samsung_pension`: 삼성증권 연금
- `isa`: ISA 계좌

## 주의사항

- **주문 도구**(`place_order`, `cancel_order`)는 실거래이므로 반드시 사용자 확인 후 실행
- `get_holdings`에서 `include_current_price=true`면 실시간 가격 조회로 응답이 느릴 수 있음
- 코인 심볼은 반드시 `KRW-BTC`, `USDT-ETH` 형식으로 접두사 포함
